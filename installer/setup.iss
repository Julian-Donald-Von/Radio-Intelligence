; Radio Intelligence 安装器 — Inno Setup 6
; 单 exe：装代码+原生二进制+启动器，装时下载 Python/建 venv，模型按勾选下载。
; 艺术/视觉创作内容 All Rights Reserved，不在任何开源授权范围内。

#define MyAppName "Radio Intelligence"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Mr. Unpunctual"
#define MyAppURL "https://github.com/Julian-Donald-Von"
#define MyAppExeName "start_radio.bat"

[Setup]
AppId={{8F3C2A11-6B4D-4E7A-9C21-0A1B2C3D4E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
; 默认装到 D 盘；目录选择页强制可见且可编辑（用户明确“得能选装在哪里，不能直接安 C 盘”）
DefaultDirName=D:\Radio Intelligence
UsePreviousAppDir=no
DefaultGroupName={#MyAppName}
DisableDirPage=no
DisableProgramGroupPage=yes
OutputDir=..\installer_output
OutputBaseFilename=RadioIntelligence-Setup-{#MyAppVersion}
SetupIconFile=..\webapi\static\brand\logo.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
MinVersion=10.0

[Languages]
; Inno Setup 6 官方包不含 ChineseSimplified.isl，故仅用英文（中文引导在应用 UI 内）
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "compact"; Description: "Compact — core app + TTS model (recommended)"
Name: "full"; Description: "Full — all models including 21.2GB LLM"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "app"; Description: "Application core + native binaries (required)"; Types: compact full custom; Flags: fixed
Name: "ml"; Description: "ML dependencies: torch + faiss + CLAP runtime (~5GB download)"; Types: full custom
Name: "model_clap"; Description: "LAION-CLAP audio model (1.86GB, auto-download)"; Types: compact full custom
Name: "model_tts"; Description: "Qwen3-TTS voices (~1.4GB, download from mirror)"; Types: compact full custom
Name: "model_others"; Description: "MERT / MuQ / RoBERTa models (~3GB, optional)"; Types: full custom
Name: "model_llm"; Description: "Qwen3.6-35B LLM Q4_K_M (21.2GB, download from mirror)"; Types: full custom

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: app

[Icons]
Name: "{group}\Radio Intelligence"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall Radio Intelligence"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Radio Intelligence"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 1) 环境引导：下载 Python + 建 venv + pip（阻塞等待，用户能看到进度）
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\scripts\bootstrap_env.ps1"" -AppDir ""{app}"" -SourcesJson ""{app}\installer\model_sources.json"" {code:GetMLFlag}"; \
  Description: "Setting up Python environment and virtualenv..."; \
  Flags: waituntilterminated; Components: app

; 2) 模型下载（每项独立，按组件勾选决定 ids）
Filename: "{app}\venv\Scripts\python.exe"; \
  Parameters: """{app}\installer\scripts\download_models.py"" --sources ""{app}\installer\model_sources.json"" --app ""{app}"" --ids clap --endpoint https://hf-mirror.com"; \
  Description: "Downloading CLAP model..."; Flags: waituntilterminated; Components: model_clap
Filename: "{app}\venv\Scripts\python.exe"; \
  Parameters: """{app}\installer\scripts\download_models.py"" --sources ""{app}\installer\model_sources.json"" --app ""{app}"" --ids tts --endpoint https://hf-mirror.com"; \
  Description: "Downloading TTS model..."; Flags: waituntilterminated; Components: model_tts
Filename: "{app}\venv\Scripts\python.exe"; \
  Parameters: """{app}\installer\scripts\download_models.py"" --sources ""{app}\installer\model_sources.json"" --app ""{app}"" --ids mert330,muq,roberta --endpoint https://hf-mirror.com"; \
  Description: "Downloading optional models..."; Flags: waituntilterminated; Components: model_others
Filename: "{app}\venv\Scripts\python.exe"; \
  Parameters: """{app}\installer\scripts\download_models.py"" --sources ""{app}\installer\model_sources.json"" --app ""{app}"" --ids llm --endpoint https://hf-mirror.com"; \
  Description: "Downloading LLM (21.2GB, please wait)..."; Flags: waituntilterminated; Components: model_llm

; 3) 打开 README
Filename: "{app}\RELEASE_README.md"; Description: "Open getting-started guide"; Flags: postinstall skipifsilent nowait

[UninstallDelete]
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\Python313"
Type: filesandordirs; Name: "{app}\data_runtime"

[Code]
function GetMLFlag(Param: String): String;
begin
  if WizardIsComponentSelected('ml') then
    Result := '-WithML'
  else
    Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  d: String;
begin
  if CurStep = ssPostInstall then
  begin
    d := ExpandConstant('{app}');
    SaveStringToFile(d + '\installed_version.txt', '{#MyAppVersion}', False);
  end;
end;
