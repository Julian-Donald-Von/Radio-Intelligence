"""
漫游推荐引擎：融合 AI 导听的智能随机推荐。
两种策略：
  - continuous（连续相似）：基于音频/元数据向量，推荐与当前最相似的曲目
  - leap（风格跳跃）：推荐向量相似度最低（风格异质）的曲目，打破信息茧房

结合 AI 大脑生成的偏好标签做加权过滤，并向 AIBrain 索取口播/推荐理由。
"""
from __future__ import annotations

from core.op_timer import timed

import logging
import os
import random
from pathlib import Path
from typing import Callable

from core.audio_features import AudioFeatureExtractor, FeatureIndex
from ai.brain import AIBrain
from config.settings import AIConfig

logger = logging.getLogger("MusicAI.Recommend")

_REMOTE_PREFIX = ("http://", "https://", "netease://", "qq://", "spotify://", "kugou://")

# ★ R119-3j：漫游索引重建（`_build()`，整库 CLAP 编码）的「等空闲」上限（秒）。
#   实测单次 **12.5 秒**、在**本进程内**跑 ⇒ 持 GIL ⇒ 播放中做会让整进程反复冻一下。
#   回退：`MUSICAI_ROAM_IDLE_ONLY=0`（恢复旧行为：播放中也建）。
ROAM_IDLE_WAIT_S = 300.0


def _has_local_audio(tracks) -> bool:
    """曲库里是否存在真实可读的本地音频 —— 决定要不要预加载 CLAP 权重。

    在线-only 曲库（netease://、qq:// 等）本就无法做音频语义编码，预加载纯属
    白占显存/内存；只有存在本地文件时后台加载才有意义。
    """
    for t in tracks or []:
        p = getattr(t, "path", None)
        if not p:
            continue
        s = str(p)
        if s.startswith(_REMOTE_PREFIX):
            continue
        try:
            if Path(s).exists():
                return True
        except Exception:  # noqa
            continue
    return False


class RoamEngine:
    def __init__(self, brain: AIBrain, tracks: list) -> None:
        self.brain = brain
        self.tracks = tracks
        # 后台预加载 CLAP 权重（不阻塞构造）：未就绪期间特征走同维元数据兜底
        self.extractor = AudioFeatureExtractor(preload=_has_local_audio(tracks))
        self.index = FeatureIndex()
        self._vectors = []
        self._build()
        if self.extractor.clap_status() == "loading":
            self.rebuild_async()

    def _build(self) -> None:
        """逐首编码 + 建索引。

        ⚠ 内存纪律（2026-09-06 实机 crash 后加）：
          重建一次要给整库（实测 138 首）做 CLAP 编码。若不主动回收，
          torch 的缓存分配器 + numpy 临时数组会持续吃内存，最终
          `[low_level_alloc.cc] VirtualAlloc failed` → **整进程被杀**。
          所以每编码 16 首做一次 gc + torch.cuda.empty_cache()。
        """
        if not self.tracks:
            return
        vecs = []
        for i, t in enumerate(self.tracks):
            vecs.append(self.extractor.extract_features(t))
            if (i + 1) % 16 == 0:
                self._release_memory()
        self._vectors = vecs
        self.index.build(self._vectors)

    @staticmethod
    def _release_memory() -> None:
        """主动回收：Python 垃圾回收 + torch CUDA 缓存分配器。"""
        try:
            import gc
            gc.collect()
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa
            pass

    def refresh(self, tracks: list) -> None:
        self.tracks = tracks
        self._build()

    def rebuild_async(self) -> None:
        """CLAP 权重后台加载完成后重建索引：特征从「元数据兜底」升级为真实音频语义。

        为什么必须重建：加载未完成时 _build() 用的是同维元数据向量（可用但粗糙），
        权重就绪后重算才能让「相似漫游」真正基于听感。索引维度不变，重建是安全的。
        """
        import threading
        import time as _time

        def _run():
            try:
                for _ in range(600):                       # 最多等 10 分钟
                    st = self.extractor.clap_status()
                    if st in ("ready", "failed", "disabled"):
                        break
                    _time.sleep(1.0)
                if self.extractor.clap_ready():
                    # ★ R119-3j：**等空闲再建索引**。实测 `_build()` 单次 **12.5 秒**，
                    #   整库 CLAP 编码、跑在**本进程内** ⇒ 持 GIL ⇒ 播放中做会让整进程
                    #   每隔一会儿冻一次（用户听到的卡顿）。与 beats / 特征索引同处置：
                    #   协作式让路 + 有界等待；**等不到就放弃本轮**（幂等 —— 下次空闲
                    #   或下次启动会再来）。回退开关：`MUSICAI_ROAM_IDLE_ONLY=0`。
                    if os.environ.get("MUSICAI_ROAM_IDLE_ONLY", "1") != "0":
                        from core.playback_state import wait_until_idle
                        if not wait_until_idle(timeout_s=ROAM_IDLE_WAIT_S, poll_s=0.5):
                            logger.info(
                                "[Roam] 播放中，跳过本轮音频语义索引重建"
                                "（%d 首暂用元数据特征；下次空闲再补）", len(self.tracks))
                            return
                    self._build()
                    self._release_memory()
                    logger.info("[Roam] CLAP 就绪，音频语义索引已重建（%d 首）",
                                len(self.tracks))
            except Exception as e:  # noqa
                logger.warning("[Roam] 索引重建失败（沿用元数据特征）: %s", e)

        threading.Thread(target=timed(_run, "roam_rebuild"), daemon=True,
                         name="roam-rebuild").start()

    def next_track(self, current_idx: int | None, strategy: str | None = None,
                   exclude: set[int] | None = None) -> tuple[int, str]:
        """
        返回 (推荐曲目下标, 推荐理由文本)。
        strategy: continuous | leap，缺省用 AIConfig.ROAM_STRATEGY。
        """
        strategy = strategy or AIConfig.ROAM_STRATEGY
        exclude = (exclude or set()) | (
            {current_idx} if current_idx is not None else set())
        if not self.tracks:
            return -1, "曲库为空"

        if current_idx is None or not self._vectors:
            cand = [i for i in range(len(self.tracks)) if i not in exclude]
            pick = random.choice(cand) if cand else 0
            return pick, "随机为你挑一首。"

        vec = self._vectors[current_idx]
        idxs = self.index.query(vec, k=min(10, len(self.tracks)), mode=strategy)
        # 过滤已播放
        pool = [i for i in idxs if i not in exclude] or \
               [i for i in range(len(self.tracks)) if i not in exclude]
        pick = pool[0] if pool else current_idx

        track = self.tracks[pick]
        hint = "连续相似推荐" if strategy == "continuous" else "风格跳跃，换个心情"
        reason = self.brain.explain_recommendation(
            {"title": track.title, "artist": track.artist, "genre": track.genre},
            hint,
        )
        return pick, reason

    def suggest(self, candidates: list, n: int, strategy: str | None = None,
                current=None) -> list:
        """
        离线/降级用的"向量相似选歌"：从候选里挑与 current 最相似的 n 首。
        strategy=leap 时反过来挑"最不相似"（风格跳跃）。
        返回挑选出的 Track 列表（最多 n 首）。
        """
        strategy = strategy or AIConfig.ROAM_STRATEGY
        if not candidates:
            return []
        if current is None or not self._vectors:
            return random.sample(candidates, min(n, len(candidates)))
        try:
            cur_idx = self.tracks.index(current)
            cur_vec = self._vectors[cur_idx]
        except (ValueError, IndexError):
            return random.sample(candidates, min(n, len(candidates)))
        cand_vecs = [self.extractor.extract_features(t) for t in candidates]
        # 与 current 的相似度（余弦）
        sims = [self._cosine(cur_vec, v) for v in cand_vecs]
        order = sorted(range(len(candidates)),
                       key=lambda i: sims[i], reverse=(strategy != "leap"))
        return [candidates[i] for i in order[:n]]

    @staticmethod
    def _cosine(a, b) -> float:
        try:
            import math
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            if na == 0 or nb == 0:
                return 0.0
            return dot / (na * nb)
        except Exception:  # noqa
            return 0.0

