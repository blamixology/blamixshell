<#
  Builds ShellDeck-<version>-x64.msi from an existing dist\ShellDeck folder and,
  with -Test, installs it silently (all users AND per user), checks that installing
  the other kind over an existing copy is blocked, runs the app self-test
  from the installed location, checks no data is written into the install folder,
  then uninstalls.

  Needs: .NET SDK 8+ (for the `wix` tool). First run installs WiX v5 automatically.
  Usage:  powershell -ExecutionPolicy Bypass -File packaging\windows\build_msi.ps1 [-Version 1.2.3] [-Test]
#>
param([string]$Version = "", [switch]$Test)
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..\..")

if (-not (Test-Path dist\ShellDeck\ShellDeck.exe)) { throw "dist\ShellDeck\ShellDeck.exe not found - run build.bat first" }

# MSI versions must be numeric (major.minor.patch)
if (-not $Version) {
  $Version = (Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
}
$m = [regex]::Match($Version.TrimStart("v"), '^\d+\.\d+\.\d+')
if (-not $m.Success) { throw "Version '$Version' is not x.y.z" }
$Version = $m.Value

if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
  dotnet tool install --global wix --version 5.0.2
  $env:PATH += ";$env:USERPROFILE\.dotnet\tools"
}
wix extension add -g WixToolset.UI.wixext/5.0.2 | Out-Null
wix extension add -g WixToolset.Util.wixext/5.0.2 | Out-Null

# the marker tells ShellDeck it is installed -> keep data in %APPDATA%, not next to the exe
New-Item dist\ShellDeck\installed.marker -ItemType File -Force | Out-Null
Remove-Item -Recurse -Force dist\ShellDeck\data -ErrorAction SilentlyContinue

$out = "ShellDeck-$Version-x64.msi"
# absolute paths: WiX resolves <Files Include> relative to the .wxs file, not the current folder
$src  = (Resolve-Path dist\ShellDeck).Path
$icon = (Resolve-Path shelldeck\assets\app.ico).Path
$lic  = (Resolve-Path packaging\windows\license.rtf).Path
wix build packaging\windows\ShellDeck.wxs -arch x64 -ext WixToolset.UI.wixext -ext WixToolset.Util.wixext `
  -d Version=$Version -d "SourceDir=$src" -d "IconFile=$icon" -d "LicenseRtf=$lic" `
  -o $out
if ($LASTEXITCODE -ne 0) { throw "wix build failed" }
# guard: an MSI without the app inside is tiny - fail instead of shipping it
$mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
if ($mb -lt 30) { throw "MSI is only $mb MB - the app files were not included" }
Write-Host "Built $out ($mb MB)"

# keep the portable folder portable
Remove-Item dist\ShellDeck\installed.marker -Force

if (-not $Test) { exit 0 }

function Invoke-Msi($msiArgs, $log) {
  $p = Start-Process msiexec.exe -ArgumentList $msiArgs -Wait -PassThru
  if ($p.ExitCode -ne 0) { Get-Content $log -Tail 60; throw "msiexec $msiArgs failed ($($p.ExitCode))" }
}
function Invoke-MsiBlocked($msiArgs, $log, $what) {
  # an install over a copy of the other kind must stop cleanly with our message
  $p = Start-Process msiexec.exe -ArgumentList $msiArgs -Wait -PassThru
  if ($p.ExitCode -eq 0) { throw "$what was NOT blocked" }
  if (-not (Select-String -Path $log -Pattern "ShellDeck is already installed" -Quiet)) {
    Get-Content $log -Tail 60; throw "$what failed ($($p.ExitCode)) but not with the expected message"
  }
  Write-Host "Blocked as expected: $what"
}
function Test-Installed($dir) {
  $exe = Join-Path $dir "ShellDeck.exe"
  if (-not (Test-Path $exe)) {
    # show where it actually went, to make failures easy to diagnose
    $found = @("${env:ProgramFiles}", "${env:ProgramFiles(x86)}", "$env:LOCALAPPDATA\Programs", "$env:LOCALAPPDATA\Apps") |
      ForEach-Object { Get-ChildItem -Path $_ -Filter ShellDeck.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue } |
      ForEach-Object { $_.FullName }
    throw "ShellDeck.exe not found at $exe. Found instead: $($found -join ', ')"
  }
  $env:SHELLDECK_SELFTEST = "1"; $env:QT_QPA_PLATFORM = "offscreen"
  $p = Start-Process $exe -Wait -PassThru
  Remove-Item Env:SHELLDECK_SELFTEST, Env:QT_QPA_PLATFORM
  if ($p.ExitCode -ne 0) { throw "self-test failed from $dir ($($p.ExitCode))" }
  if (Test-Path (Join-Path $dir "data")) { throw "installed app wrote data into $dir" }
  Write-Host "Self-test OK from $dir"
}
$msi = (Resolve-Path $out).Path

Write-Host "== all-users install =="
Invoke-Msi "/i `"$msi`" /qn ALLUSERS=1 /l*v install-machine.log" "install-machine.log"
Test-Installed "$env:ProgramFiles\ShellDeck"
Invoke-MsiBlocked "/i `"$msi`" /qn ALLUSERS=2 MSIINSTALLPERUSER=1 /l*v block-user.log" "block-user.log" "per-user install over all-users copy"
Invoke-Msi "/x `"$msi`" /qn /l*v uninstall-machine.log" "uninstall-machine.log"
if (Test-Path "$env:ProgramFiles\ShellDeck\ShellDeck.exe") { throw "uninstall left files behind" }

Write-Host "== per-user install =="
Invoke-Msi "/i `"$msi`" /qn ALLUSERS=2 MSIINSTALLPERUSER=1 /l*v install-user.log" "install-user.log"
Test-Installed "$env:LOCALAPPDATA\Programs\ShellDeck"
Invoke-MsiBlocked "/i `"$msi`" /qn ALLUSERS=1 /l*v block-machine.log" "block-machine.log" "all-users install over per-user copy"
Invoke-Msi "/x `"$msi`" /qn ALLUSERS=2 MSIINSTALLPERUSER=1 /l*v uninstall-user.log" "uninstall-user.log"
if (Test-Path "$env:LOCALAPPDATA\Programs\ShellDeck\ShellDeck.exe") { throw "per-user uninstall left files behind" }

Write-Host "MSI tests passed"
