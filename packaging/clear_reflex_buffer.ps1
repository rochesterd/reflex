<#
    Clears Reflex's recording buffer for every user profile on this machine.

    Reflex records into a buffer under the user's temp directory and deletes
    it at start and at exit (session_buffer.py). This script is the third
    case, the one the app cannot cover itself: a crash, a power cut, or a
    forced reboot, after which a recording containing two students' faces
    would otherwise sit on disk until someone next launched the app.

    reflex.iss registers it as a scheduled task that runs as SYSTEM at
    logon -- as SYSTEM because it sweeps every profile, not just the one
    logging on, and at logon because that is the first moment after a crash
    that is guaranteed to happen.

    Deliberately silent and deliberately never failing: it runs with nobody
    watching, and a buffer it cannot delete this time is one the app itself
    will delete at its next start.
#>
$ErrorActionPreference = 'SilentlyContinue'

# Profile paths come from the registry, not C:\Users -- a machine with
# redirected or relocated profiles still gets swept.
$profileList = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList'

foreach ($key in (Get-ChildItem -Path $profileList -ErrorAction SilentlyContinue)) {
    $profilePath = (Get-ItemProperty -Path $key.PSPath -ErrorAction SilentlyContinue).ProfileImagePath
    if ([string]::IsNullOrWhiteSpace($profilePath)) { continue }

    # Must match session_buffer.py's buffer_root().
    $buffer = Join-Path $profilePath 'AppData\Local\Temp\Reflex\buffer'
    if (Test-Path -LiteralPath $buffer) {
        Remove-Item -LiteralPath $buffer -Recurse -Force -ErrorAction SilentlyContinue
    }
}

exit 0
