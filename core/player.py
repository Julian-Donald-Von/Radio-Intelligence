"""
播放引擎门面（薄壳）。

对外仍暴露 `PlayerEngine` 与 `PlayerState`（类名/API 与旧 pygame 版完全一致），
真实实现已迁移到 core/audio_engine.py（sounddevice 单流软件混音）。

旧 pygame 实现保留在 core/player_pygame_backup.py 以便回滚对照。
"""
from core.audio_engine import PlayerEngine, PlayerState

__all__ = ["PlayerEngine", "PlayerState"]
