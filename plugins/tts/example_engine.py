"""
示例 TTS 引擎插件：演示如何新增一个 TTS 引擎（无需改核心代码）。

使用方法：把本文件放在 plugins/tts/ 下，重启应用即被自动加载，
并出现在 TTS 引擎注册表中（设置页 engine 可选值会包含本引擎的 name）。

实现一个 TTS 引擎只需：
  1. 继承 core.tts_base.TTSEngine，填 name（唯一 id）。
  2. 实现 _check()：返回 True 表示该引擎当前可用（依赖/模型就绪）。
  3. 实现 synthesize(text) -> 音频文件路径 或 None：返回合成出的 wav/mp3 路径。
     返回 None 表示合成失败（管理器会按回退链切换到其它引擎）。

注意：下方 _check 默认返回 False，避免被当成「可用引擎」意外接管口播。
真实引擎应改为根据自身依赖（如某个 SDK 是否安装）返回 True。
"""
from core.tts_base import TTSEngine


class ExampleEngine(TTSEngine):
    # 插件唯一 id（也支持 plugin_key）。不要与内置 edge/chattts/melotts/voicevox 冲突。
    name = "example"

    def _check(self) -> bool:
        # TODO: 根据自身依赖返回 True，例如：return importlib.util.find_spec("my_tts") is not None
        # 默认 False：仅作模板，不参与口播（避免被误用）。
        return False

    def synthesize(self, text: str):
        # TODO: 调用你的 TTS，把文本合成为音频文件并返回其路径。
        # 例：out = TTS_TMP_DIR / f"{uuid.uuid4().hex}.wav"
        #     my_tts.synthesize(text, str(out)); return str(out) if out.exists() else None
        return None

    # 如需 GPU 显存释放，重写 empty_cache()；需要运行时读配置，重写 reconfigure()。
