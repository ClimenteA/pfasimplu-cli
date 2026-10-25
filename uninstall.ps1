# One-command uninstaller for pfasimplu-cli on Windows.
#
#   irm https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/uninstall.ps1 | iex
#
# It removes what install.ps1 installed: the `uv tool` environment with the CLI.
# `uv` itself, your Gemini key (.env) and the generated results are left alone
# unless you ask for --purge-data.
#
# Options (download the file first, or set the environment variables with irm):
#
#   --purge-data      also delete .\pfasimplu.toml and .\output
#   --dry-run         print what would be removed, change nothing
#   --help
#
# Environment: PFASIMPLU_CONFIG, PFASIMPLU_OUTPUT_DIR.

$ErrorActionPreference = 'Stop'

$Pkg = 'pfasimplu-cli'
$PurgeData = $false
$DryRun = $false
$ConfigPath = if ($env:PFASIMPLU_CONFIG) { $env:PFASIMPLU_CONFIG } else { '.\pfasimplu.toml' }
$OutputDir = if ($env:PFASIMPLU_OUTPUT_DIR) { $env:PFASIMPLU_OUTPUT_DIR } else { '.\output' }

function Write-Step($Message) { Write-Host "`n==> $Message" }
function Write-Info($Message) { Write-Host "    $Message" }
function Write-Warn($Message) { Write-Host "warn: $Message" -ForegroundColor Yellow }

function Show-Usage {
    Write-Host @"
Uninstall $Pkg

  irm <url>/uninstall.ps1 | iex

Options:
  --purge-data      also delete .\pfasimplu.toml and .\output
  --dry-run         print what would be removed, change nothing
  --help            show this help

Environment: PFASIMPLU_CONFIG, PFASIMPLU_OUTPUT_DIR.

Removes the CLI installed with `uv tool`. `uv` itself and your .env file are
not removed. Generated results stay unless --purge-data is given.
"@
}

for ($i = 0; $i -lt $args.Count; $i++) {
    $arg = [string]$args[$i]
    switch -Regex ($arg) {
        '^(-d|--purge-data)$' { $PurgeData = $true }
        '^(-n|--dry-run)$'    { $DryRun = $true }
        '^(-h|--help)$'       { Show-Usage; return }
        default               { throw "Unknown option: $arg (try --help)" }
    }
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # PowerShell 7 already negotiates TLS by itself.
}

function Update-Path {
    $bin = Join-Path $env:USERPROFILE '.local\bin'
    if ((Test-Path $bin) -and ($env:Path -notlike "*$bin*")) {
        $env:Path = "$bin;$env:Path"
    }
}

function Get-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-SafePath($Path) {
    if (-not $Path) { return $false }
    try {
        $full = [IO.Path]::GetFullPath($Path)
    } catch {
        return $false
    }
    $trimmed = $full.TrimEnd([char[]]@('\', '/'))
    $root = [IO.Path]::GetPathRoot($full)
    if ($trimmed -eq $root.TrimEnd([char[]]@('\', '/'))) { return $false }
    if ($trimmed -eq $env:USERPROFILE.TrimEnd([char[]]@('\', '/'))) { return $false }
    if ($trimmed -eq (Get-Location).Path.TrimEnd([char[]]@('\', '/'))) { return $false }
    return $true
}

# --- uv and paths -----------------------------------------------------------

Write-Step 'Checking uv'
Update-Path
$uv = Get-Uv
if ($uv) {
    Write-Info "uv: $(& $uv --version)"
} else {
    Write-Warn 'uv is not installed, so there is no uv tool environment to remove.'
    Write-Info "If a $Pkg command still answers, it was installed another way (pip, pipx, a checkout)."
}

$toolBinDir = $null
if ($uv) {
    $toolBinDir = (& $uv tool dir --bin 2>$null)
}
if (-not $toolBinDir) { $toolBinDir = Join-Path $env:USERPROFILE '.local\bin' }

# --- package ----------------------------------------------------------------

Write-Step "Removing $Pkg"
$listed = ''
if ($uv) { $listed = (& $uv tool list 2>$null) -join "`n" }

if (-not $uv) {
    Write-Info 'nothing to remove through uv.'
} elseif ($listed -notmatch "(?m)^$([regex]::Escape($Pkg))\s") {
    Write-Info "$Pkg is not installed in uv; nothing to remove."
} elseif ($DryRun) {
    Write-Info "would run: uv tool uninstall $Pkg"
} else {
    & $uv tool uninstall $Pkg
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool uninstall failed (exit $LASTEXITCODE)."
    }
    Write-Info "removed $Pkg"
}

# --- local files ------------------------------------------------------------

if ($PurgeData) {
    Write-Step 'Removing local config and results'

    if (Test-Path $ConfigPath) {
        if (Test-SafePath $ConfigPath) {
            if ($DryRun) {
                Write-Info "would delete: $ConfigPath"
            } else {
                Remove-Item -Force $ConfigPath
                Write-Info "deleted: $ConfigPath"
            }
        } else {
            Write-Warn "refusing to delete unsafe path: $ConfigPath"
        }
    } else {
        Write-Info "no config file at $ConfigPath"
    }

    if (Test-Path $OutputDir) {
        if (Test-SafePath $OutputDir) {
            if ($DryRun) {
                Write-Info "would delete: $OutputDir"
            } else {
                Remove-Item -Recurse -Force $OutputDir
                Write-Info "deleted: $OutputDir"
            }
        } else {
            Write-Warn "refusing to delete unsafe path: $OutputDir"
        }
    } else {
        Write-Info "no output folder at $OutputDir"
    }
} else {
    Write-Info "config and results stay in $(Get-Location) (use --purge-data to delete them)"
}

# --- done -------------------------------------------------------------------

Write-Step 'Done'
if ($DryRun) {
    Write-Info 'dry run: nothing was changed.'
}
$uvNote = if ($uv) { "still installed in $toolBinDir (it may serve other tools)" } else { 'not found on this machine' }
Write-Host @"

Uninstalled. What was left in place:

    uv                 $uvNote
    Gemini key         .env in $(Get-Location) is not touched
    config/results     $ConfigPath and $OutputDir (unless --purge-data)

To drop uv as well:  uv self uninstall
"@
