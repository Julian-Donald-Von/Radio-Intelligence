"""
MusicAI 插件目录。

把插件放进对应子目录，应用启动时自动发现并合并进内置注册表，无需改核心代码：

  plugins/sources/   音源插件（单 .py 文件，继承 data.sources.base.MusicSource）
  plugins/tts/       TTS 引擎插件（单 .py 文件，继承 core.tts_base.TTSEngine）
  plugins/skins/     UI 皮肤插件（单 .py 或子目录包，继承 core.skin.Skin）

插件唯一 id 取自 plugin_key / key / name 之一；同一个文件里可以有多个类，
但每个要被加载的类必须填一个非空且唯一的 id。导入失败的文件仅告警并跳过，
不会拖垮应用。

参考示例：
  plugins/sources/example_source.py
  plugins/tts/example_engine.py
  plugins/skins/midnight/skin.py
"""
