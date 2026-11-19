# Radio Intelligence 封装设计（2026-11-19）

> 本文是设计+待拍板清单，**不是已实施结论**。发布树在
> `D:\radio intelligence backup\MusicAI-Release-20261119\`，源树 `D:\MusicAI` 不动。

## 1. 实测硬约束

| 项 | 实测值 | 对封装的含义 |
|---|---|---|
| 发布树总体积 | ~37 GB | 不可能做成单文件绿色 exe |
| models/ | 29.9 GB（LLM 21.72 / CLAP 1.86 / MuQ 1.33+1.33 / MERT 1.26 / TTS 1.51 / RoBERTa 0.5 …） | 模型必须可选下载，不能默认全塞 |
| venv/ | 7.3 GB（torch 2.6.0+cu124 4.46 / PySide6 0.63 / onnxruntime 0.30 …），234 个包 | 安装时创建，或做可移植运行时 |
| venv `pyvenv.cfg` | `home = C:\Users\DonaldVon\.workbuddy\binaries\python\versions\3.13.12` | **整包搬到别的机器必坏**，不能靠 copy venv |
| Python | 3.13.14（系统只注册 3.11 / 3.12） | 安装器需自带或静默装 3.13 |
| 安装器工具 | Inno Setup / NSIS / 7-Zip 均未安装 | 需先取工具（Inno Setup 为推荐） |
| 模型来源 | CLAP=`lukewys/laion_clap`、MuQ=`OpenMuQ/MuQ-large-msd-iter`、MERT=`m-a-p/MERT-v1-330M`、RoBERTa=`FacebookAI/roberta-base`；**LLM 是 ollama blob `sha256-d372…`，公开 URL 未考证** | 下载清单只写已考证来源；LLM 先列为"手动放置/待考证"，不编造 URL |

## 2. "一个 exe" 的准确含义

一个 exe = **安装器 exe**，不是把软件压成一个文件。理由：37 GB 单文件不可分发；
且 venv 含绝对路径，搬运即坏。安装器装完后，用户从开始菜单/桌面快捷方式启动。

## 3. 推荐方案：小体积在线安装器

安装器 exe 本体只装"代码 + 原生二进制 + 启动器 + 安装脚本"（压缩包 ~几十 MB），
环境与模型在安装向导里完成：

1. 选择安装目录（默认 `D:\Radio Intelligence`，不写 C 系统目录）。
2. 静默安装 Python 3.13 到应用目录，或探测已存在的 3.13。
3. 在该目录 `python -m venv venv`，`pip install -r requirements_pinned.txt`
   （镜像源可配，默认清华；torch 走 cu124 或 CPU 版二选一）。
4. **组件页**：精简（无 torch/CLAP，功能降级但能播） / 完整（全依赖）。
5. **模型页**：逐项勾选 + 断点续传 + sha256 校验；TTS 建议必选，
   CLAP/MuQ/MERT/RoBERTa 可选，21.7 GB LLM 默认不选。
6. 写桌面/开始菜单快捷方式，写卸载器（卸载 = 删安装目录，不动用户音乐库）。
7. 把插件/模型扫描清单（`release_catalog.json`）作为安装后自检输出。

## 4. 备选方案对比

| 方案 | exe 体积 | 断网可用 | 换机可靠 | 评价 |
|---|---|---|---|---|
| A 在线安装器（推荐） | ~几十 MB | 否 | 高（装时装环境） | 符合"安装时建 venv + 按需下模型" |
| B 离线全量 exe | ~20 GB 压缩 | 是 | 中（venv 路径要修） | 体积劝退，不推荐 |
| C 混合：exe 带代码+wheelhouse+TTS，只在线下 torch/LLM | ~2–3 GB | 部分 | 高 | A 的稳健变体，可作 A 的"离线档" |
| D 便携 zip 目录（非 exe） | ~37 GB | 是 | 低 | 不满足"一个 exe" |

## 5. 待拍板（决定后才能动代码）

1. **exe 形态**：在线安装器（推荐） / 混合离线档 / 先只产出安装器工程不打包。
2. **安装器框架**：Inno Setup（推荐，需联网取工具） / NSIS / 自写 Python 自解压。
3. **Python 策略**：静默装 3.13 到应用目录（推荐） / 要求用户预装 / 打包嵌入式运行时。
4. **模型下载**：安装向导勾选（推荐） / 首次启动再下 / 只下 TTS 必备项。

## 6. 已考证 vs 未考证

- 已考证：`download_clap.py` 的 CLAP URL、`small_backbone_research.md` 的 MuQ/MERT/RoBERTa 来源。
- 未考证：21.7 GB LLM 的公开下载地址、Qwen3-TTS gguf 的确切 repo。
  这两项在拍板后需单独查证，**查不到就标为手动放置**，禁止编造 URL。

---

## 7. 已实施（2026-11-19 下午，用户四项拍板后）

产物：`installer_output\RadioIntelligence-Setup-1.0.0.exe`（1160.9 MB，SHA256 `46B40A96…B417`，未签名）。
- 框架：Inno Setup 6.7.4（本机临时装在 `.tmp\installer_tools\InnoSetup6`，官方 GitHub 下载、签名已验 Pyrsys B.V.）。
- **安装位置**：`DefaultDirName=D:/Radio Intelligence` + `DisableDirPage=no` + `UsePreviousAppDir=no`（用户明确"不能装 C 盘、要能选"）；`PrivilegesRequired=lowest`，普通用户权限即可装 D 盘用户目录。
- 组件页：核心必装；ML 依赖（torch/faiss）、CLAP 自动下载、TTS/其它模型、21.7GB LLM 分组件勾选。
- 安装期流程：`bootstrap_env.ps1`（下载 Python 3.13.14 到应用目录、校验 SHA、建 venv、pip 装依赖）→ `download_models.py`（已考证来源自动下载+断点续传+sha256；manual 项只提示不编造 URL）。
- **考证结果（严守不编造）**：CLAP URL+SHA 与 `tools/download_clap.py` 一致；Python 3.13.14 URL+SHA 来自 python.org；MERT/MuQ/RoBERTa repo 来自 `training/download_models.py`；**TTS 两个 gguf 与公开 HF 仓库字节数均不符（不同构建）、LLM 是 ollama blob 无公开 URL ⇒ 两项标 manual，只给放置说明**。
- 静态验证：编译成功、载荷无 venv/models/legacy/research/.bak、下载器 py_compile 通过且"已校验文件跳过"逻辑实测通过。
- ⚠️ 未做：用户选择"只做静态检查，不实装"，故完整静默安装未跑；exe 未签名（无代码签名证书），分发时会触发 SmartScreen。
