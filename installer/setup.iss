; Inno Setup script for WhatsApp Printer.
;
; Build the payload first with installer\build.ps1, which runs PyInstaller and
; stages Tesseract. Then compile this with ISCC.exe (build.ps1 does that too).
;
; Two things this installer deliberately does NOT do:
;
;   * It ships no print driver. The queue is built on Microsoft's inbox
;     "Microsoft Print To PDF" driver (see provision.ps1), so there is nothing to
;     sign, no WHQL submission, and nothing for Windows Protected Print mode to
;     uninstall.
;   * It installs no Windows service. The agent has to show a window when someone
;     prints, and a service runs in session 0 with no desktop. It starts at logon
;     in the user's own session instead.

#define AppName        "WhatsApp Printer"
#define AppVersion     "0.1.0"
#define AppPublisher   "Sunrise Software"
#define DataDir        "C:\ProgramData\WAPrinter"

[Setup]
AppId={{8E3B4C21-9A7D-4F62-B1E5-7C9D2A6F4B83}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\WhatsAppPrinter
DefaultGroupName={#AppName}
OutputBaseFilename=WhatsAppPrinter-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
; Creating a printer queue is a machine-wide change.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\waprinter-agent.exe
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full";   Description: "Full installation (recommended)"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "core"; Description: "WhatsApp Printer"; Types: full custom; Flags: fixed
Name: "ocr";  Description: "Read scanned invoices (OCR) - adds about 50 MB"; Types: full

[Files]
Source: "..\dist\waprinter-agent\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs; Components: core
Source: "..\dist\cli\waprinter\*";   DestDir: "{app}\cli"; \
    Flags: ignoreversion recursesubdirs; Components: core
Source: "provision.ps1";             DestDir: "{app}"; Flags: ignoreversion; Components: core
Source: "..\README.md";              DestDir: "{app}"; Flags: ignoreversion isreadme; Components: core

; Tesseract, for invoices that print as an image rather than as text.
Source: "vendor\tesseract\*"; DestDir: "{app}\tesseract"; \
    Flags: ignoreversion recursesubdirs skipifsourcedoesntexist; Components: ocr

[Dirs]
Name: "{#DataDir}";       Permissions: users-modify
Name: "{#DataDir}\spool"; Permissions: users-modify
Name: "{#DataDir}\inbox"; Permissions: users-modify
Name: "{#DataDir}\logs";  Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\waprinter-agent.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\waprinter-agent.exe"; \
    Tasks: desktopicon
; The agent must be running for a print to be noticed, so it starts at logon.
; {commonstartup}, not {userstartup}: this installer requires admin, so it runs
; as whoever supplied the admin password. At a client that is rarely the clerk
; who will use the machine, and a per-user Startup entry would land in the
; administrator's profile — the agent would then never start for the person
; actually printing, and printing would appear to do nothing.
Name: "{commonstartup}\{#AppName}"; Filename: "{app}\waprinter-agent.exe"; \
    Parameters: "--hidden"

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop"; \
    GroupDescription: "Shortcuts"

[Registry]
; Point the agent at the bundled tessdata, so OCR works without Tesseract being
; on PATH.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
    ValueType: string; ValueName: "TESSDATA_PREFIX"; \
    ValueData: "{app}\tesseract\tessdata"; \
    Flags: preservestringtype uninsdeletevalue; Components: ocr

[Run]
; Create the printer queue and its ports.
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\provision.ps1"""; \
    StatusMsg: "Creating the WhatsApp Printer queue..."; \
    Flags: runhidden waituntilterminated

; Start the agent now so the first print works without a reboot.
Filename: "{app}\waprinter-agent.exe"; Description: "Start {#AppName}"; \
    Flags: postinstall nowait skipifsilent


[UninstallRun]
; Stop the agent before pulling the printer out from under it.
Filename: "taskkill.exe"; Parameters: "/F /IM waprinter-agent.exe"; \
    Flags: runhidden; RunOnceId: "StopAgent"
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\provision.ps1"" -Uninstall"; \
    Flags: runhidden waituntilterminated; RunOnceId: "RemovePrinter"

[Code]
// Job history and the captured PDFs are the record of what was sent to whom.
// Ask before deleting rather than taking it away silently.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Also delete the job history and captured documents in {#DataDir}?' + #13#10 + #13#10 +
              'This is the record of every message that was sent. Keep it unless ' +
              'you are sure you no longer need it.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{#DataDir}'), True, True, True);
  end;
end;
