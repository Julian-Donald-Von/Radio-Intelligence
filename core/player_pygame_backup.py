"""
播放引擎：基于 pygame 的"双通道"实现。

架构说明（对应需求中的 Channel 0 / Channel 1）：
  - 音乐通道 (music bus)：使用 pygame.mixer.music 做流式播放，
    天然支持进度/暂停/seek/音量，作为"音乐通道"。
  - TTS 通道 (Channel 1)：使用 pygame.mixer.Channel(1) 播放由 edge-tts
    生成的口播音频(Sound)，与音乐相互独立。
  - Ducking：口播开始时把音乐通道音量压到 DUCK_VOLUME(默认0.15)，
    口播结束后渐强恢复到目标音量 —— 即"双通道音量Ducking"。

健壮性：若运行环境无音频设备（如无声卡服务器、音频被占用），
mixer 初始化会失败，此时自动降级为"静默模式"——UI 与逻辑照常运行，
仅不出声。你本机有声卡则完全正常。
"""
from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from pathlib import Path

import pygame

from config.settings import PlayerConfig
from utils.network_security import validate_media_url

logger = logging.getLogger("MusicAI.Player")


class PlayerState(str, Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class PlayerEngine:
    def __init__(self) -> None:
        self._audio_ok = True
        try:
            pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=2048)
            pygame.mixer.init()
            self._tts_channel: pygame.mixer.Channel = pygame.mixer.Channel(1)
            # 独立音效通道（测试朗读 / 提示音），不与音乐鸭音通道冲突
            self._fx_channel: pygame.mixer.Channel = pygame.mixer.Channel(2)
            pygame.mixer.music.set_volume(PlayerConfig.MUSIC_VOLUME)
        except Exception as e:  # noqa
            self._audio_ok = False
            logger.warning("音频设备初始化失败，进入静默模式: %s", e)

        self._state = PlayerState.STOPPED
        self._current_path: str | None = None
        self._target_volume = PlayerConfig.MUSIC_VOLUME
        self._ducking = False

        self._position_thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._position_cb = None
        self._ended_cb = None
        self._ever_played = False
        # 跳转偏移：pygame.mixer.music.get_pos() 返回「自上次 play() 起」的时长，
        # 不含 play(0, start) 的 start 偏移。手动 seek 后必须显式记账，否则
        # get_position 会回退到「距上次起播 0 秒」，导致进度条弹回、转场错位。
        self._seek_offset_ms = 0
        self._duration_ms = 0       # 当前曲目总时长（秒×1000），用于位置兜底钳制
        self._seek_guard_until = 0.0  # seek 后短暂保护窗口，避免刚跳转、流未起播时
                                      # 位置线程误判「播放结束」而跳到下一首。
        # 串行化音量渐变：淡入/淡出/恢复可能在不同线程并发触发，避免互相打架。
        self._ramp_lock = threading.Lock()
        # 串行化所有 pygame.mixer 调用：本程序的音乐加载/播放（音频线程）、
        # 转场/口播 TTS（定时器线程与口播生成线程）、进度轮询（位置线程）都会
        # 并发触碰 pygame mixer。pygame 并非线程安全，多线程同时调用 mixer 会
        # 偶发死锁、表现为「播几首后界面卡死」。用一把锁让 mixer 调用互斥执行。
        self._mix_lock = threading.Lock()

    # ---------------- 基础控制 ----------------
    def load(self, path: str) -> bool:
        self._current_path = path
        if path and (path.startswith("http://") or path.startswith("https://")):
            local = self._download_stream(path)
            if not local:
                logger.warning("远程音源下载失败: %s", path)
                self._state = PlayerState.STOPPED
                return False
            path = local
        if not path or not Path(path).exists():
            logger.warning("音源文件不存在，跳过加载: %s", path)
            self._state = PlayerState.STOPPED
            return False
        if not self._audio_ok:
            self._state = PlayerState.STOPPED
            return True
        try:
            with self._mix_lock:
                pygame.mixer.music.load(path)
            self._state = PlayerState.STOPPED
            return True
        except pygame.error as e:
            logger.error("加载音源失败: %s", e)
            self._state = PlayerState.STOPPED
            return False

    def _download_stream(self, url: str) -> str | None:
        """下载远程音源到临时文件，返回本地路径；失败返回 None。"""
        max_bytes = 256 * 1024 * 1024
        try:
            import requests
            import tempfile
            url = validate_media_url(url)
            resp = requests.get(url, timeout=(5, 30), stream=True,
                                allow_redirects=True)
            resp.raise_for_status()
            if resp.url != url:
                validate_media_url(resp.url)
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared > max_bytes:
                raise ValueError("远程音源超过 256 MB 限制")
            ct = (resp.headers.get("Content-Type") or "").lower()
            suffix = ".mp3"
            if "flac" in ct or url.lower().endswith(".flac"):
                suffix = ".flac"
            elif "wav" in ct or url.lower().endswith(".wav"):
                suffix = ".wav"
            d = Path(tempfile.gettempdir()) / "musicai_stream"
            d.mkdir(parents=True, exist_ok=True)
            fp = d / f"{abs(hash(url))}{suffix}"
            with open(fp, "wb") as f:
                total = 0
                for chunk in resp.iter_content(1 << 16):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("远程音源超过 256 MB 限制")
                    f.write(chunk)
            return str(fp)
        except Exception as e:  # noqa
            logger.error("下载远程音源失败: %s", e)
            return None

    def play(self, duration: float | None = None) -> None:
        if not self._current_path:
            return
        self._ever_played = False
        self._seek_offset_ms = 0        # 从头开始播放，清空跳转偏移
        self._duration_ms = int((duration or 0) * 1000)
        if not self._audio_ok:
            # 静默模式：用总时长兜底触发"播放结束"，保证自动续播可用
            self._state = PlayerState.PLAYING
            if duration:
                try:
                    threading.Timer(max(1.0, float(duration)), self._fire_ended).start()
                except Exception:  # noqa
                    pass
            return
        if self._state == PlayerState.PAUSED:
            with self._mix_lock:
                pygame.mixer.music.unpause()
        else:
            with self._mix_lock:
                pygame.mixer.music.play()
        self._state = PlayerState.PLAYING
        self._start_position_tracker()

    def set_ended_callback(self, cb) -> None:
        """注册'一首播放结束'回调，供电台自动续播。"""
        self._ended_cb = cb

    def _fire_ended(self) -> None:
        if self._state != PlayerState.PLAYING:
            return
        self._state = PlayerState.STOPPED
        cb = self._ended_cb
        if cb:
            try:
                cb()
            except Exception as e:  # noqa
                logger.warning("ended 回调异常: %s", e)

    def pause(self) -> None:
        if self._state == PlayerState.PLAYING and self._audio_ok:
            with self._mix_lock:
                pygame.mixer.music.pause()
            self._state = PlayerState.PAUSED

    def resume(self) -> None:
        if self._state == PlayerState.PAUSED and self._audio_ok:
            with self._mix_lock:
                pygame.mixer.music.unpause()
            self._state = PlayerState.PLAYING

    def stop(self, unduck: bool = False) -> None:
        if self._audio_ok:
            with self._mix_lock:
                pygame.mixer.music.stop()
        self._state = PlayerState.STOPPED
        self._stop_flag.set()
        if unduck:
            # 手动切歌等场景：上一首可能处于 ducking / fade_out 途中，
            # 直接复位音量状态，避免残留的低音量/淡出标志污染新歌。
            self._ducking = False
            if self._audio_ok:
                with self._mix_lock:
                    pygame.mixer.music.set_volume(self._target_volume)

    def toggle(self) -> None:
        if self._state == PlayerState.PLAYING:
            self.pause()
        elif self._state == PlayerState.PAUSED:
            self.resume()
        else:
            self.play()

    # ---------------- 进度 / 音量 ----------------
    def seek(self, ms: int) -> None:
        """跳转：pygame 的 music.set_pos() 对 mp3/wav/ogg 普通文件普遍无效，
        必须用 music.play(0, start) 的 start 参数才能真正跳转（SDL2_mixer 可靠）。
        跳转后 get_pos() 会从头起算，故显式记 _seek_offset_ms 让 get_position
        返回绝对位置，避免进度条弹回、转场错位。"""
        if not self._audio_ok or not self._current_path:
            return
        target = max(0, int(ms))
        with self._mix_lock:
            try:
                pygame.mixer.music.play(0, target / 1000.0)
                self._seek_offset_ms = target
                # play() 会把混音器音量重置为默认，需恢复用户目标音量并退出鸭音态。
                pygame.mixer.music.set_volume(self._target_volume)
                self._ducking = False
            except pygame.error as e:  # noqa
                logger.warning("seek 失败（play 起点跳转）: %s", e)
                return
        self._state = PlayerState.PLAYING
        self._ever_played = True       # 已播放过，防止位置线程误判「播放结束」
        self._seek_guard_until = time.monotonic() + 0.7   # 跳转后 0.7s 内忽略 ended
        self._start_position_tracker()

    def get_position(self) -> int:
        if not self._audio_ok or self._state == PlayerState.STOPPED:
            return 0
        with self._mix_lock:
            pos = pygame.mixer.music.get_pos()
        # 绝对位置 = 跳转偏移 + 自上次 play() 起经过的时长。
        abs_pos = self._seek_offset_ms + max(0, pos)
        if self._duration_ms and abs_pos > self._duration_ms:
            abs_pos = self._duration_ms
        return max(0, abs_pos)

    def music_busy(self) -> bool:
        """线程安全的「音乐是否正在播放」查询（供位置轮询线程使用）。"""
        if not self._audio_ok:
            return False
        with self._mix_lock:
            return bool(pygame.mixer.music.get_busy())

    @property
    def volume(self) -> float:
        return self._target_volume

    def set_volume(self, v: float) -> None:
        self._target_volume = max(0.0, min(1.0, v))
        if self._audio_ok and not self._ducking:
            pygame.mixer.music.set_volume(self._target_volume)

    # ---------------- Ducking（双通道核心） ----------------
    def duck(self, level: float | None = None) -> None:
        if not self._audio_ok:
            self._ducking = True
            return
        level = level if level is not None else PlayerConfig.DUCK_VOLUME
        self._ducking = True
        with self._mix_lock:
            pygame.mixer.music.set_volume(level)

    def unduck(self, fade_ms: int | None = None) -> None:
        """口播结束后平滑把音乐音量恢复到目标值（不会重启歌曲）。"""
        if not self._audio_ok:
            self._ducking = False
            return
        fade_ms = fade_ms if fade_ms is not None else PlayerConfig.RECOVER_FADE_MS
        self._ducking = False
        self._ramp_to(self._target_volume, fade_ms)

    # ---------------- 平滑过渡（转场用） ----------------
    def fade_to(self, target: float, fade_ms: int) -> None:
        """在 fade_ms 内把音乐音量渐变到 target（不重启歌曲）。"""
        if not self._audio_ok:
            return
        self._ramp_to(max(0.0, min(1.0, target)), max(1, fade_ms))

    def fade_out(self, fade_ms: int | None = None) -> None:
        """音乐淡出到静音（转场口播前）。"""
        if not self._audio_ok:
            self._ducking = True
            return
        self._ducking = True
        self._ramp_to(0.0, fade_ms if fade_ms is not None else PlayerConfig.RECOVER_FADE_MS)

    def fade_in(self, fade_ms: int | None = None, target: float | None = None) -> None:
        """从静音渐强到目标音量（下一首开始）。

        音量渐变在后台线程进行，避免在 UI 主线程（手动切歌 next/prev/jump_to）
        阻塞约 1.5s 导致界面冻结。
        """
        if not self._audio_ok:
            self._ducking = False
            return
        self._ducking = False
        tgt = target if target is not None else self._target_volume
        dur = fade_ms if fade_ms is not None else PlayerConfig.RECOVER_FADE_MS
        threading.Thread(
            target=self._ramp_to, args=(tgt, dur), daemon=True).start()

    def _ramp_to(self, target: float, fade_ms: int) -> None:
        """在 fade_ms 内把音乐音量从当前值渐变到 target（后台线程安全）。"""
        if not self._audio_ok:
            return
        with self._ramp_lock:
            try:
                with self._mix_lock:
                    start = pygame.mixer.music.get_volume()
            except Exception:  # noqa
                with self._mix_lock:
                    pygame.mixer.music.set_volume(target)
                return
            steps = max(1, fade_ms // 50)
            step_ms = max(1, fade_ms / steps)
            for i in range(1, steps + 1):
                try:
                    with self._mix_lock:
                        pygame.mixer.music.set_volume(
                            start + (target - start) * (i / steps))
                except Exception:  # noqa
                    break
                time.sleep(step_ms / 1000.0)
            try:
                with self._mix_lock:
                    pygame.mixer.music.set_volume(target)
            except Exception:  # noqa
                pass

    # ---------------- TTS 通道 ----------------
    def play_tts(self, wav_path: str | Path) -> None:
        if not self._audio_ok:
            return
        with self._mix_lock:
            sound = pygame.mixer.Sound(str(wav_path))
            self._tts_channel.play(sound)

    def is_tts_playing(self) -> bool:
        if not self._audio_ok:
            return False
        return bool(self._tts_channel.get_busy())

    def stop_tts(self) -> None:
        """立即打断 TTS 通道（手动切歌时停掉上一首的迟到口播，避免旧旁白
        叠在新歌上继续念）。静默模式无通道，置空即可。"""
        if not self._audio_ok:
            return
        try:
            with self._mix_lock:
                self._tts_channel.stop()
        except Exception:  # noqa
            pass

    def wait_tts(self) -> None:
        if not self._audio_ok:
            return
        while self._tts_channel.get_busy():
            time.sleep(0.05)

    def play_sound(self, path: str | Path) -> None:
        """独立播放一段音效（如设置里的"测试朗读"试听），不走鸭音通道。"""
        if not self._audio_ok:
            logger.info("静默模式，跳过试听播放: %s", path)
            return
        try:
            with self._mix_lock:
                sound = pygame.mixer.Sound(str(path))
                self._fx_channel.play(sound)
        except Exception as e:  # noqa
            logger.error("试听播放失败: %s", e)

    # ---------------- 进度回调线程 ----------------
    def _start_position_tracker(self) -> None:
        if not self._audio_ok:
            return
        if self._position_thread and self._position_thread.is_alive():
            return
        self._stop_flag.clear()
        self._position_thread = threading.Thread(
            target=self._track_position, daemon=True
        )
        self._position_thread.start()

    def restart_position_tracker(self) -> None:
        """换歌后强制重启位置跟踪：旧线程因 stop() 已置 stop_flag 即将退出，
        但 play() 时它可能仍活着（is_alive 仍 True），导致新歌没有进度回调 /
        结束检测。做法：请求旧线程退出并等它结束（最多 1s），再清标志启新线程。"""
        if not self._audio_ok:
            return
        old = self._position_thread
        if old is not None and old.is_alive():
            self._stop_flag.set()
            old.join(timeout=1.0)
        self._position_thread = None
        self._stop_flag.clear()
        self._position_thread = threading.Thread(
            target=self._track_position, daemon=True
        )
        self._position_thread.start()

    def _track_position(self) -> None:
        while not self._stop_flag.is_set():
            if self._state == PlayerState.PLAYING and self._audio_ok:
                busy = self.music_busy()
                pos = self.get_position()
                if pos > 0:
                    self._ever_played = True
                if not busy and self._ever_played and time.monotonic() > self._seek_guard_until:
                    # 真实播放结束（已播放过、当前不再 busy，且不在 seek 保护窗口内）
                    self._fire_ended()
                elif self._position_cb and pos >= 0:
                    try:
                        self._position_cb(pos)
                    except Exception:  # noqa
                        pass
            time.sleep(0.25)

    def set_position_callback(self, cb) -> None:
        self._position_cb = cb

    @property
    def state(self) -> PlayerState:
        return self._state

    @property
    def current_path(self) -> str | None:
        return self._current_path

    @property
    def audio_ok(self) -> bool:
        return self._audio_ok

    def shutdown(self) -> None:
        self._stop_flag.set()
        if self._audio_ok:
            try:
                pygame.mixer.music.stop()
                pygame.mixer.quit()
            except Exception:  # noqa
                pass
