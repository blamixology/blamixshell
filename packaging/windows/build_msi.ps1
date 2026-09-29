<#
  Builds BlamixShell-<version>-x64.msi from an existing dist\BlamixShell folder and,
  with -Test, installs it silently (all users AND per user), checks that installing
  the other kind over an existing copy is blocked and that upgrading to a newer
  version works in both modes, runs the app self-test
  from the installed location, checks no data is written into the install folder,
  then uninstalls.

  Needs: .NET SDK 8+ (for the `wix` tool). First run installs WiX v5 automatically.
  Usage:  powershell -ExecutionPolicy Bypass -File packaging\windows\build_msi.ps1 [-Version 1.2.3] [-Test]
#>
param([string]$Version = "", [switch]$Test)
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..\..")

if (-not (Test-Path dist\BlamixShell\BlamixShell.exe)) { throw "dist\BlamixShell\BlamixShell.exe not found - run build.bat first" }

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

# the marker tells BlamixShell it is installed -> keep data in %APPDATA%, not next to the exe
New-Item dist\BlamixShell\installed.marker -ItemType File -Force | Out-Null
Remove-Item -Recurse -Force dist\BlamixShell\data -ErrorAction SilentlyContinue

$out = "BlamixShell-$Version-x64.msi"
# absolute paths: WiX resolves <Files Include> relative to the .wxs file, not the current folder
$src  = (Resolve-Path dist\BlamixShell).Path
$icon = (Resolve-Path blamixshell\assets\app.ico).Path
$lic  = (Resolve-Path packaging\windows\license.rtf).Path
function Build-Msi($ver, $file) {
  wix build packaging\windows\BlamixShell.wxs -arch x64 -ext WixToolset.UI.wixext -ext WixToolset.Util.wixext `
    -d Version=$ver -d "SourceDir=$src" -d "IconFile=$icon" -d "LicenseRtf=$lic" `
    -o $file
  if ($LASTEXITCODE -ne 0) { throw "wix build failed" }
}
Build-Msi $Version $out
# guard: an MSI without the app inside is tiny - fail instead of shipping it
$mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
if ($mb -lt 30) { throw "MSI is only $mb MB - the app files were not included" }
Write-Host "Built $out ($mb MB)"

# -Test also needs a second package one version higher: a different version gets a new
# ProductCode, like a real update (installing the *same* MSI again is just a repair)
if ($Test) {
  $p = $Version.Split("."); $p[2] = [string]([int]$p[2] + 1); $Next = $p -join "."
  $nextOut = "BlamixShell-$Next-x64-test.msi"
  Build-Msi $Next $nextOut
}

# keep the portable folder portable
Remove-Item dist\BlamixShell\installed.marker -Force

if (-not $Test) { exit 0 }

function Invoke-Msi($msiArgs, $log) {
  $p = Start-Process msiexec.exe -ArgumentList $msiArgs -Wait -PassThru
  if ($p.ExitCode -ne 0) { Get-Content $log -Tail 60; throw "msiexec $msiArgs failed ($($p.ExitCode))" }
}
function Invoke-MsiBlocked($msiArgs, $log, $what) {
  # an install over a copy of the other kind must stop cleanly with our message
  $p = Start-Process msiexec.exe -ArgumentList $msiArgs -Wait -PassThru
  if ($p.ExitCode -eq 0) { throw "$what was NOT blocked" }
  if (-not (Select-String -Path $log -Pattern "BlamixShell is already installed" -Quiet)) {
    Get-Content $log -Tail 60; throw "$what failed ($($p.ExitCode)) but not with the expected message"
  }
  Write-Host "Blocked as expected: $what"
}
function Get-Registered {
  # BlamixShell entries in Apps & features (machine and current user)
  Get-ChildItem "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
                "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue |
    Get-ItemProperty | Where-Object { $_.DisplayName -eq "BlamixShell" }
}
function Assert-Registered($version) {
  $r = @(Get-Registered)
  if ($r.Count -ne 1) { throw "expected 1 BlamixShell in Apps & features, found $($r.Count): $($r.DisplayVersion -join ', ')" }
  if (-not "$($r[0].DisplayVersion)".StartsWith($version)) { throw "expected version $version, found $($r[0].DisplayVersion)" }
}
function Test-Installed($dir) {
  $exe = Join-Path $dir "BlamixShell.exe"
  if (-not (Test-Path $exe)) {
    # show where it actually went, to make failures easy to diagnose
    $found = @("${env:ProgramFiles}", "${env:ProgramFiles(x86)}", "$env:LOCALAPPDATA\Programs", "$env:LOCALAPPDATA\Apps") |
      ForEach-Object { Get-ChildItem -Path $_ -Filter BlamixShell.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue } |
      ForEach-Object { $_.FullName }
    throw "BlamixShell.exe not found at $exe. Found instead: $($found -join ', ')"
  }
  $env:BLAMIXSHELL_SELFTEST = "1"; $env:QT_QPA_PLATFORM = "offscreen"
  $p = Start-Process $exe -Wait -PassThru
  Remove-Item Env:BLAMIXSHELL_SELFTEST, Env:QT_QPA_PLATFORM
  if ($p.ExitCode -ne 0) { throw "self-test failed from $dir ($($p.ExitCode))" }
  if (Test-Path (Join-Path $dir "data")) { throw "installed app wrote data into $dir" }
  Write-Host "Self-test OK from $dir"
}
$msi = (Resolve-Path $out).Path
$nextMsi = (Resolve-Path $nextOut).Path   # (PowerShell names are case-insensitive: not $next)
$machineDir = "$env:ProgramFiles\BlamixShell"
$userDir = "$env:LOCALAPPDATA\Programs\BlamixShell"

Write-Host "== all users: install $Version, block a per-user $Next, upgrade to $Next =="
Invoke-Msi "/i `"$msi`" /qn ALLUSERS=1 /l*v install-machine.log" "install-machine.log"
Test-Installed $machineDir
Assert-Registered $Version
Invoke-MsiBlocked "/i `"$nextMsi`" /qn ALLUSERS=2 MSIINSTALLPERUSER=1 /l*v block-user.log" "block-user.log" "per-user install over all-users copy"
if (Test-Path "$userDir\BlamixShell.exe") { throw "the blocked per-user install left files in $userDir" }
Invoke-Msi "/i `"$nextMsi`" /qn ALLUSERS=1 /l*v upgrade-machine.log" "upgrade-machine.log"
Test-Installed $machineDir
Assert-Registered $Next
Invoke-Msi "/x `"$nextMsi`" /qn /l*v uninstall-machine.log" "uninstall-machine.log"
if (Test-Path "$machineDir\BlamixShell.exe") { throw "uninstall left files behind" }
if (@(Get-Registered).Count) { throw "uninstall left an Apps & features entry" }

Write-Host "== just me: install $Version, block an all-users $Next, upgrade to $Next =="
Invoke-Msi "/i `"$msi`" /qn ALLUSERS=2 MSIINSTALLPERUSER=1 /l*v install-user.log" "install-user.log"
Test-Installed $userDir
Assert-Registered $Version
Invoke-MsiBlocked "/i `"$nextMsi`" /qn ALLUSERS=1 /l*v block-machine.log" "block-machine.log" "all-users install over per-user copy"
if (Test-Path "$machineDir\BlamixShell.exe") { throw "the blocked all-users install left files in $machineDir" }
Invoke-Msi "/i `"$nextMsi`" /qn ALLUSERS=2 MSIINSTALLPERUSER=1 /l*v upgrade-user.log" "upgrade-user.log"
Test-Installed $userDir
Assert-Registered $Next
Invoke-Msi "/x `"$nextMsi`" /qn ALLUSERS=2 MSIINSTALLPERUSER=1 /l*v uninstall-user.log" "uninstall-user.log"
if (Test-Path "$userDir\BlamixShell.exe") { throw "per-user uninstall left files behind" }
if (@(Get-Registered).Count) { throw "per-user uninstall left an Apps & features entry" }

Remove-Item $nextOut -Force
Write-Host "MSI tests passed"
