"""
TTS 公共入口。

向后兼容：旧代码 `from core.tts import TTSEngine` 现在得到 TTSManager（分发器），
其接口（synthesize / available / reconfigure）与原 TTSEngine 一致，
因此 radio.py / main_window.py / settings_dialog.py 无需改动即可获得
「运行时切换 ChatTTS / MeloTTS / Edge 云」的能力。
"""
from core.tts_base import TTSEngine as TTSEngineBase  # 抽象基类
from core.tts_manager import TTSManager
from core.voice_manager import VoiceManager

# 向后兼容别名：TTSEngine 现指向分发器
TTSEngine = TTSManager

__all__ = ["TTSEngine", "TTSManager", "TTSEngineBase", "VoiceManager"]
