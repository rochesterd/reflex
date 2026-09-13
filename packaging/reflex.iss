; Inno Setup script for the Reflex clinic-machine installer -- see
; PACKAGING.md for the full build procedure and DECISIONS.md's "Frozen-exe
; installer built" entry for why this exists instead of shipping
; Python source + setup.ps1 to clinic machines.
;
; Expects three things to already exist before compiling (see PACKAGING.md):
;   1. packaging\dist\app\ and packaging\dist\settings\ -- built via
;      `pyinstaller packaging\app.spec` / `pyinstaller packaging\settings.spec`.
;   2. vendor\ids-peak-win-extended-setup-64.exe -- the current IDS peak
;      *extended* setup installer (not standard/runtime -- see
;      DECISIONS.md's "Retired ROADMAP entries" entry for why
;      extended), manually placed there by whoever's building
;      this. Gitignored, same convention this repo already uses for
;      vendor/ids_peak_api.txt. Always use this exact filename regardless
;      of the IDS SDK version currently bundled, so this script never
;      needs editing just because IDS shipped a new version.
;   3. vendor\ids-peak-response.iss -- an InstallShield silent-install
;      response file recorded from a real Custom install of the *same*
;      vendor\ids-peak-win-extended-setup-64.exe currently in vendor\,
;      with the uEye Transport Layer component enabled. Recorded via
;      `ids-peak-win-extended-setup-64.exe /r /f1"<output path>"` on a
;      machine with no prior IDS peak install (a fresh Windows Sandbox
;      session is the easiest way to guarantee that) -- see PACKAGING.md
;      for the exact recording steps. Must be re-recorded, against that
;      new installer, every time vendor\ids-peak-win-extended-setup-64.exe
;      is bumped to a new IDS release -- see DECISIONS.md's "Silent IDS
;      peak install" entry for why a stale response file is the real risk
;      here, and how IdsPeakAlreadyInstalled()'s post-install re-check
;      guards against it silently going unnoticed if that step is missed.

#define AppVersion "1.0"
; ids_peak.dll's own FileVersion, as actually installed by
; vendor/ids-peak-win-extended-setup-64.exe (currently packaging IDS peak
; 26.06.1) -- confirmed empirically on the build machine via
; (Get-Item '...\ids_peak\program\ids_peak.dll').VersionInfo.FileVersion,
; not assumed. IDS's installer-package version string (e.g. "26.06.1")
; and this DLL's own FileVersion are unrelated numbering schemes -- see
; DECISIONS.md's "IdsPeakAlreadyInstalled() checks a version, not just a
; folder" entry for why this specific file+field is what gets compared,
; and why a bare folder-existence check isn't enough. Update this
; whenever vendor/ids-peak-win-extended-setup-64.exe is bumped to a new
; IDS release, by checking that release's own ids_peak.dll the same way.
#define IdsPeakMinDllVersion "1.16.0.0"
#define IdsPeakResponseFile "ids-peak-response.iss"

[Setup]
; Pinned rather than left to default: Inno derives an unset AppId from
; AppName, so the display name could not change without orphaning every
; existing install. See DECISIONS.md's 2026-09-11 rename entry.
AppId=reflex
AppName=Reflex
AppVersion={#AppVersion}
AppPublisher=NECO
DefaultDirName={autopf}\Reflex
DefaultGroupName=Reflex
DisableProgramGroupPage=yes
; Writes to Program Files and chain-launches the IDS installer (which
; itself needs admin for driver installation) -- both need elevation.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer_output
OutputBaseFilename=reflex-setup
Compression=lzma2
SolidCompression=yes
SetupIconFile=..\assets\reflex.ico
; Without this, Add/Remove Programs shows unins000.exe's generic icon.
UninstallDisplayIcon={app}\app\app.exe

[Files]
; dontcopy, listed first (solid-compression decompression cost grows with
; position -- see PACKAGING.md): these two are pulled out early via
; ExtractTemporaryFile() in CurStepChanged's ssInstall handler below,
; *before* Reflex's own Files/Icons are written, not during Setup's
; normal automatic copy phase. That's what lets the silent IDS install run
; early enough for NeedRestart() to see its result -- see DECISIONS.md's
; "Silent IDS peak install: native restart page" entry. dontcopy-extracted
; files are auto-deleted when Setup exits, regardless of outcome, so no
; deleteafterinstall flag is needed here.
Source: "..\vendor\ids-peak-win-extended-setup-64.exe"; DestDir: "{tmp}"; Flags: dontcopy
Source: "..\vendor\{#IdsPeakResponseFile}"; DestDir: "{tmp}"; Flags: dontcopy
Source: "dist\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\settings\*"; DestDir: "{app}\settings"; Flags: ignoreversion recursesubdirs createallsubdirs
; The WinUSB driver package for the older Vantage Plus BIO's camera --
; built and self-signed by packaging\net2860_winusb\build_driver_package.ps1,
; which must be run before compiling this script (the .cat and .cer are
; gitignored build outputs, so a fresh checkout does not have them).
;
; Installed permanently under {app} rather than extracted to {tmp}: a
; technician who needs to re-bind the camera later (say after someone
; reinstalls Keeler's Kapture over it) can then do it from the installed
; copy, without the setup exe.
Source: "net2860_winusb\reflex_net2860.inf"; DestDir: "{app}\driver"; Flags: ignoreversion
Source: "net2860_winusb\reflex_net2860.cat"; DestDir: "{app}\driver"; Flags: ignoreversion
Source: "net2860_winusb\reflex_net2860.cer"; DestDir: "{app}\driver"; Flags: ignoreversion
; The logon sweep of the recording buffer. Installed rather than run once:
; the scheduled task below invokes it at every logon, which is the only
; thing that clears a buffer left by a crash -- see the script's own header
; and session_buffer.py.
Source: "clear_reflex_buffer.ps1"; DestDir: "{app}\cleanup"; Flags: ignoreversion

[Icons]
; app.exe only on the Desktop -- this is what closes the "how does a
; student launch this" gap (see DECISIONS.md's "Frozen-exe installer
; built" entry: no such shortcut existed anywhere before this).
; settings.exe gets a Start Menu entry only, no Desktop icon --
; CLAUDE.md: "never point a student at it."
Name: "{autodesktop}\Reflex"; Filename: "{app}\app\app.exe"
Name: "{autoprograms}\Reflex"; Filename: "{app}\app\app.exe"
Name: "{autoprograms}\Reflex Settings"; Filename: "{app}\settings\settings.exe"

[Run]
; Reflex deletes its recording buffer at start and at exit, but an app
; cannot clean up after its own crash. This task is that third case: it
; runs as SYSTEM at logon and sweeps every profile's buffer, so a machine
; that was power-cycled mid-session does not keep two students' faces on
; disk until someone next launches the app.
;
; The nested quoting is schtasks', not Inno's: /TR takes one quoted command
; line, and the path inside it needs \" escapes of its own. Verify after
; any edit with:  schtasks /Query /TN "Reflex buffer cleanup" /V /FO LIST
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /TN ""Reflex buffer cleanup"" /RU SYSTEM /SC ONLOGON /TR ""powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \""{app}\cleanup\clear_reflex_buffer.ps1\"""""; StatusMsg: "Registering the recording-buffer cleanup task..."; Flags: runhidden waituntilterminated

[UninstallRun]
; Unlike the recordings this installer used to leave alone, the task is
; ours and nothing else uses it -- a scheduled task pointing at a deleted
; script is litter, and one deleting folders is worse than litter.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""Reflex buffer cleanup"""; Flags: runhidden; RunOnceId: "RemoveReflexBufferCleanupTask"

[Code]
var
  (* Set True only after InstallIdsPeakSilently() actually performs and
     verifies a fresh IDS peak install this run -- read by NeedRestart()
     below. This works now (it didn't the first time it was tried -- see
     DECISIONS.md's "Silent IDS peak install: the restart prompt never
     showed" entry) specifically because InstallIdsPeakSilently() runs
     from ssInstall, confirmed empirically to be before Inno's one
     internal NeedRestart() query, not from ssPostInstall, confirmed to
     be after it. *)
  IdsPeakInstalledThisRun: Boolean;

function IdsPeakAlreadyInstalled(): Boolean;
var
  DllPath: String;
  VersionMS, VersionLS: Cardinal;
  InstalledVersion, MinRequiredVersion: Int64;
begin
  (* A bare DirExists check (this function's original form) treats *any*
     version as good enough, including a much older one silently installed
     by other IDS-camera-adjacent software already on the machine before
     Reflex ever runs -- confirmed for real, not hypothetical: Keeler's
     own Kinexis/Vantage Plus Digital installer silently drives IDS peak
     2.9.0.0 (see DECISIONS.md) into this exact Program Files\IDS\ids_peak
     location, a version far older than what
     vendor/ids-peak-win-extended-setup-64.exe currently bundles. A
     technician on a machine that's only ever run Kinexis would have this
     check wrongly skip the bundled installer, silently leaving that old
     SDK in place instead of the current one Reflex's pinned
     requirements-ids.txt bindings actually expect.
     ids_peak\program\ids_peak.dll's own FileVersion is what's compared --
     the extended setup's actual install location for that specific file,
     confirmed on a real IDS-peak-installed machine during this project's
     EULA/licensing investigation (DECISIONS.md's "Retired ROADMAP
     entries") -- rather than a
     specific product GUID in the Uninstall registry key, which changes
     across IDS peak releases. *)
  Result := False;
  DllPath := ExpandConstant('{pf}\IDS\ids_peak\program\ids_peak.dll');
  if FileExists(DllPath) and GetVersionNumbers(DllPath, VersionMS, VersionLS) then
  begin
    InstalledVersion := PackVersionNumbers(VersionMS, VersionLS);
    StrToVersion('{#IdsPeakMinDllVersion}', MinRequiredVersion);
    Result := ComparePackedVersion(InstalledVersion, MinRequiredVersion) >= 0;
  end;
end;

procedure InstallIdsPeakSilently();
(* Silent, via InstallShield's /s /f1<response file> replay -- not
   interactive. See DECISIONS.md's "Silent IDS peak install" entry for
   the full reasoning; summary: a real device vendor (Keeler, for its own
   Kinexis/Vantage Plus Digital installer) ships exactly this mechanism
   in production, and the two failure modes it risks are both guarded
   against below rather than trusted blindly the way Kinexis's own
   install script does (it doesn't check its own exit code or log at
   all):
     1. A hard failure (Windows blocks it, install crashes) -- caught via
        Exec's own ResultCode and the /f2 log file's ResultCode line.
     2. A "successful" replay that silently picked the wrong components,
        because vendor\ids-peak-win-extended-setup-64.exe moved on to a
        version whose dialog layout no longer matches
        vendor\ids-peak-response.iss -- Exec/the log can't see this at
        all, since from the installer's own point of view nothing went
        wrong. Caught by re-running IdsPeakAlreadyInstalled()'s real
        ids_peak.dll version check afterward, not by trusting the exit
        code.

   Runs from ssInstall, before Reflex's own Files/Icons -- a
   deliberate reversal of the original ssPostInstall-based design (see
   DECISIONS.md's "Silent IDS peak install" and "...: native restart
   page" entries for the full back-and-forth). The original ordering
   protected Reflex's own install from an indefinite hang in this
   step; that protection is given up here in exchange for the restart
   choice working as Inno's real native Finished-page mechanism instead
   of a separate popup -- a deliberate call, not an oversight, made after
   weighing how a genuine hang actually resolves (a technician force-
   killing the process either way, with no real rollback possible in
   Inno regardless of ordering) against how much that ordering was
   actually buying. *)
var
  ExePath, ResponsePath, LogPath, Params: String;
  ResultCode: Integer;
begin
  if IdsPeakAlreadyInstalled() then
    Exit;

  (* Both dontcopy-flagged in [Files] specifically so they're available
     this early -- Setup's normal automatic copy phase (and the {app}
     directory's own creation) doesn't happen until after ssInstall
     returns, confirmed empirically, not assumed (see DECISIONS.md). *)
  ExtractTemporaryFile('ids-peak-win-extended-setup-64.exe');
  ExtractTemporaryFile('{#IdsPeakResponseFile}');
  ForceDirectories(ExpandConstant('{app}'));

  ExePath := ExpandConstant('{tmp}\ids-peak-win-extended-setup-64.exe');
  ResponsePath := ExpandConstant('{tmp}\{#IdsPeakResponseFile}');
  LogPath := ExpandConstant('{app}\ids-peak-install.log');
  Params := Format('/s /f1"%s" /f2"%s"', [ResponsePath, LogPath]);

  WizardForm.StatusLabel.Caption := 'Installing the IDS peak SDK...';

  if not Exec(ExePath, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('The IDS peak SDK installer could not be launched (Windows error ' +
      IntToStr(ResultCode) + '). Reflex needs IDS peak to control the ' +
      'instrument cameras -- see SETUP.md to install it manually, then ' +
      'restart Reflex.', mbError, MB_OK);
    Exit;
  end;

  (* InstallShield's own ResultCode=0 in the /f2 log only rules out a hard
     failure -- see the procedure comment above for why a nonzero code
     here still isn't the whole story, and why the IdsPeakAlreadyInstalled
     recheck below is the real verification. *)
  if ResultCode <> 0 then
  begin
    MsgBox('The IDS peak SDK installer reported an error (exit code ' +
      IntToStr(ResultCode) + '). Reflex needs IDS peak to control the ' +
      'instrument cameras -- see SETUP.md to install it manually, then ' +
      'restart Reflex.' + #13#10 + #13#10 + 'Installer log: ' + LogPath,
      mbError, MB_OK);
    Exit;
  end;

  if not IdsPeakAlreadyInstalled() then
  begin
    MsgBox('The IDS peak SDK installer finished, but Reflex could not ' +
      'verify it installed the expected components (this can happen if ' +
      'vendor\ids-peak-response.iss is out of date for the bundled IDS ' +
      'peak version). Please install IDS peak manually via SETUP.md ' +
      'before using Reflex, or contact the developer.', mbError, MB_OK);
    Exit;
  end;

  IdsPeakInstalledThisRun := True;
end;

procedure InstallLegacyBioDriver();
(* Installs the WinUSB driver package that binds the older Vantage Plus
   BIO's camera (NET GmbH KS722OUP / eMPIA EM2860). Two steps: trust the
   certificate that signed the package, then stage the package itself.

   WHY A CERTIFICATE HAS TO BE TRUSTED AT ALL: the package contains no
   binaries -- every install section is an Include/Needs into the inbox
   winusb.inf, so the only kernel driver involved is Microsoft's own
   already-signed winusb.sys. That means Kernel Mode Code Signing (the
   gate needing an EV certificate and a Partner Center submission) never
   applies, and what remains is ordinary PnP *package* signing, which a
   self-signed certificate satisfies. Adding it to Root and
   TrustedPublisher is a narrow grant -- "trust driver packages from this
   publisher" -- and specifically NOT machine-wide test-signing, which
   DECISIONS.md rejected for a different driver on exactly this axis.

   NOT FATAL ON FAILURE, deliberately: this camera is optional hardware.
   A clinic with only IDS instruments must not have its install aborted
   because an optional driver step failed. But it is reported rather than
   swallowed -- a silently missing driver would surface much later as a
   camera that simply never appears in settings.py's dropdown.

   Staged even when no such camera is attached. pnputil staging is
   harmless without the device -- the package just sits in the driver
   store and binds if one is ever plugged in -- and that is what makes
   the camera plug-and-play afterwards rather than needing a second
   visit. *)
var
  DriverDir, CerPath, InfPath: String;
  ResultCode: Integer;
begin
  DriverDir := ExpandConstant('{app}\driver');
  CerPath := DriverDir + '\reflex_net2860.cer';
  InfPath := DriverDir + '\reflex_net2860.inf';

  if not FileExists(InfPath) or not FileExists(CerPath) then
  begin
    (* #13#10 kept mid-line, never at the start of one: ISPP reads a '#'
       in the first non-whitespace position as a preprocessor directive
       and aborts the compile. *)
    MsgBox('The legacy BIO camera driver was not included in this installer, ' +
      'so that camera will not be available. Every other camera is ' +
      'unaffected.' + #13#10 + #13#10 + 'This means the installer was built ' +
      'without running packaging\net2860_winusb\build_driver_package.ps1 ' +
      'first -- see PACKAGING.md.', mbInformation, MB_OK);
    Exit;
  end;

  (* Root and TrustedPublisher both: Root makes the chain verify at all,
     TrustedPublisher stops Windows prompting the technician to confirm
     the publisher during a silent install. *)
  Exec(ExpandConstant('{sys}\certutil.exe'), '-addstore -f Root "' + CerPath + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode = 0 then
    Exec(ExpandConstant('{sys}\certutil.exe'), '-addstore -f TrustedPublisher "' + CerPath + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  if ResultCode <> 0 then
  begin
    MsgBox('Could not trust the driver signing certificate (exit code ' +
      IntToStr(ResultCode) + '). The older BIO camera will not be available; ' +
      'every other camera is unaffected.', mbError, MB_OK);
    Exit;
  end;

  (* /install binds it to a matching device that is already attached, as
     well as staging it for one plugged in later. Confirmed to take over
     from Keeler's vendor driver in a single call, with no removal step
     needed -- see DECISIONS.md's 2026-09-10 entry. *)
  Exec(ExpandConstant('{sys}\pnputil.exe'), '/add-driver "' + InfPath + '" /install',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode <> 0 then
    MsgBox('Could not install the older BIO camera driver (pnputil exit code ' +
      IntToStr(ResultCode) + '). That camera will not be available; every other ' +
      'camera is unaffected. The driver can be installed later from ' + DriverDir + '.',
      mbError, MB_OK);
end;

function FindPublishedDriverName(): String;
(* pnputil renames a staged package to oemNN.inf, and the number is
   assigned at install time -- observed changing from oem360 to oem24
   across two installs on the same machine as Windows reused a freed slot.
   So it cannot be recorded at install time and trusted later, and it
   certainly cannot be hardcoded. Look it up by the original filename
   instead, which is stable.

   Deliberately done at uninstall time rather than remembered from the
   install: that also covers a package staged by
   packaging\net2860_winusb\build_driver_package.ps1 -Install on a dev
   box, which this installer never saw. *)
var
  TempFile, Line, PubName: String;
  Lines: TArrayOfString;
  I, ResultCode: Integer;
begin
  Result := '';
  TempFile := ExpandConstant('{tmp}\reflex-pnputil-enum.txt');
  Exec(ExpandConstant('{cmd}'), '/c ""' + ExpandConstant('{sys}\pnputil.exe') +
    '" /enum-drivers > "' + TempFile + '""', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not LoadStringsFromFile(TempFile, Lines) then
    Exit;

  PubName := '';
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lines[I];
    if Pos('Published Name:', Line) > 0 then
      PubName := Trim(Copy(Line, Pos(':', Line) + 1, Length(Line)));
    (* Only the "Original Name:" line carries our filename -- the published
       name is always oemNN.inf -- so this cannot match the wrong block. *)
    if Pos('reflex_net2860.inf', Line) > 0 then
    begin
      Result := PubName;
      Exit;
    end;
  end;
end;

procedure RemoveLegacyBioDriver();
(* Undoes exactly what InstallLegacyBioDriver() did, and nothing else.

   WHAT THIS DELIBERATELY DOES NOT TOUCH, because leaving it was previously
   an accident of what was absent from [Files] rather than a decision (see
   DECISIONS.md's 2026-09-10 uninstall-scope entry):

     - Nothing to do with recordings. There is no recordings folder any
       more: sessions live in a buffer the app deletes (session_buffer.py),
       and [UninstallRun] removes the logon task that sweeps it.
     - config.json. Cheap to keep, and it holds a technician's camera
       assignments and calibration -- a reinstall that finds them intact is
       strictly better than one that does not.
     - The IDS peak SDK. Shared: other software (Keeler's own Kinexis) uses
       the same install, so removing it could break an unrelated
       application. It also has its own uninstaller.

   WHAT IT DOES REMOVE, because this installer added them and nothing else
   uses them: the WinUSB driver package it staged, and the certificate it
   trusted. Leaving a self-signed root certificate behind after the
   software that justified it is gone would quietly undercut the whole
   argument for self-signing -- that it is a narrow, revocable grant. A
   grant nothing ever revokes is not narrow. *)
var
  DriverName: String;
  ResultCode: Integer;
begin
  DriverName := FindPublishedDriverName();
  if DriverName <> '' then
    Exec(ExpandConstant('{sys}\pnputil.exe'), '/delete-driver ' + DriverName + ' /uninstall',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  (* Matches CertSubject in build_driver_package.ps1. Failures are ignored:
     the certificate may already be gone, and an uninstall that halts
     because a cleanup step found nothing to clean would be worse than one
     that quietly finishes. *)
  Exec(ExpandConstant('{sys}\certutil.exe'), '-delstore Root "NECO Reflex driver signing"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\certutil.exe'),
    '-delstore TrustedPublisher "NECO Reflex driver signing"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  (* usUninstall runs before Inno removes {app}, which this needs: the
     driver lookup reads pnputil's own enumeration rather than {app}, but
     keeping the order explicit means a future step that does read {app}
     is already in the right place. *)
  if CurUninstallStep = usUninstall then
    RemoveLegacyBioDriver();
end;

function NeedRestart(): Boolean;
begin
  (* Inno's real native mechanism -- shows the actual restart-choice
     radio buttons on the Finished page, the same UI IDS's own installer
     uses, rather than a separate popup. Works here (confirmed
     empirically, see DECISIONS.md) because IdsPeakInstalledThisRun is
     set from ssInstall, before Inno's single internal query of this
     function -- setting it from ssPostInstall, tried first, was
     confirmed too late for Inno to ever see. *)
  Result := IdsPeakInstalledThisRun;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  (* ssInstall, not ssPostInstall -- see InstallIdsPeakSilently()'s own
     comment and DECISIONS.md for why this ordering was deliberately
     reversed. *)
  if CurStep = ssInstall then
    InstallIdsPeakSilently();

  (* ssPostInstall, unlike the IDS step above: this one reads files out of
     {app}\driver, which Inno has not copied yet at ssInstall time. It also
     has no NeedRestart() interaction to be early for -- binding WinUSB
     takes effect immediately. *)
  if CurStep = ssPostInstall then
    InstallLegacyBioDriver();
end;
