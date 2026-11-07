[CmdletBinding()]
param(
  [string]$AppName = "intenserp-next-v2",
  [string]$PackageName = "intenserp-next-v2-win32-x64",
  [string]$PackageAppDirName = "intense-rp-next",
  [string]$PackageOptionalDirName = "optional"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$EntryPoint = Join-Path $RepoRoot "main.py"
$UpdaterEntryPoint = Join-Path $RepoRoot "updater/main.py"
$IconPath = Join-Path $RepoRoot "ui/assets/brand/newlogo.ico"
$VersionPath = Join-Path $RepoRoot "version.json"

if (!(Test-Path $EntryPoint)) { throw "Entry point not found: $EntryPoint" }
if (!(Test-Path $UpdaterEntryPoint)) { throw "Updater entry point not found: $UpdaterEntryPoint" }
if (!(Test-Path $IconPath)) { throw "Icon not found: $IconPath" }
if (!(Test-Path $VersionPath)) { throw "version.json not found: $VersionPath" }

$BuildDir = Join-Path $RepoRoot "build"
$DistDir = Join-Path $RepoRoot "dist"
$SpecPath = Join-Path $RepoRoot "$AppName.spec"

foreach ($path in @($BuildDir, $DistDir, $SpecPath)) {
  if (Test-Path $path) {
    Remove-Item -Recurse -Force $path
  }
}

$pyinstallerArgs = @(
  "--noconfirm"
  "--clean"
  "--onedir"
  "--noconsole"
  "--name", $AppName
  "--icon", $IconPath
  "--add-data", "version.json;."
  "--add-data", ".github/state;.github/state"
  "--add-data", "remote_control/assets;remote_control/assets"
  "--add-data", "remote_control/templates;remote_control/templates"
  "--add-data", "ui/assets;ui/assets"
  "--add-data", "ui/fonts;ui/fonts"
  "--collect-all", "patchright"
  "--collect-all", "playwright"
  "--collect-all", "desktop_notifier"
  "--collect-all", "h2"
  "--collect-all", "hpack"
  "--collect-all", "hyperframe"
  $EntryPoint
)

python -m PyInstaller @pyinstallerArgs

$BuiltAppDir = Join-Path $DistDir $AppName
if (!(Test-Path $BuiltAppDir)) { throw "PyInstaller output folder not found: $BuiltAppDir" }

# version.json must be present at the package root (convenience copy; runtime uses bundled data).
Copy-Item -Force $VersionPath (Join-Path $BuiltAppDir "version.json")

# Build updater (onefile) into dist/updater.exe
$updaterWorkDir = Join-Path $BuildDir "updater-work"
$updaterSpecDir = Join-Path $BuildDir "updater-spec"
if (Test-Path $updaterWorkDir) { Remove-Item -Recurse -Force $updaterWorkDir }
if (Test-Path $updaterSpecDir) { Remove-Item -Recurse -Force $updaterSpecDir }
New-Item -ItemType Directory -Path $updaterWorkDir | Out-Null
New-Item -ItemType Directory -Path $updaterSpecDir | Out-Null

$updaterArgs = @(
  "--noconfirm"
  "--clean"
  "--onefile"
  "--noconsole"
  "--name", "updater"
  "--icon", $IconPath
  "--distpath", $DistDir
  "--workpath", $updaterWorkDir
  "--specpath", $updaterSpecDir
  $UpdaterEntryPoint
)

python -m PyInstaller @updaterArgs

$UpdaterExe = Join-Path $DistDir "updater.exe"
if (!(Test-Path $UpdaterExe)) { throw "Updater output not found: $UpdaterExe" }

# Make sure logs/configs are not shipped (even if present locally).
foreach ($root in @($BuiltAppDir, (Join-Path $BuiltAppDir "_internal"))) {
  if (!(Test-Path $root)) { continue }
  foreach ($forbiddenDir in @("logs", "config_data")) {
    $p = Join-Path $root $forbiddenDir
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
  }
  foreach ($forbiddenFile in @("config_dir.txt", ".env")) {
    $p = Join-Path $root $forbiddenFile
    if (Test-Path $p) { Remove-Item -Force $p }
  }
}

$StagingDir = Join-Path $DistDir $PackageName
if (Test-Path $StagingDir) { Remove-Item -Recurse -Force $StagingDir }
New-Item -ItemType Directory -Path $StagingDir | Out-Null

$MainStage = Join-Path $StagingDir $PackageAppDirName
$OptionalStage = Join-Path $StagingDir $PackageOptionalDirName
New-Item -ItemType Directory -Path $MainStage | Out-Null
New-Item -ItemType Directory -Path $OptionalStage | Out-Null

Copy-Item -Recurse -Force (Join-Path $BuiltAppDir "*") $MainStage
Copy-Item -Force $UpdaterExe (Join-Path $OptionalStage "updater.exe")

$ZipPath = Join-Path $DistDir "$PackageName.zip"
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path (Join-Path $StagingDir "*") -DestinationPath $ZipPath -Force

Write-Output "Created release asset: $ZipPath"
