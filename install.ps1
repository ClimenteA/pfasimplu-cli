# One-command installer for pfasimplu-cli on Windows.
#
#   irm https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/install.ps1 | iex
#
# Options (download the file first, or set the environment variables when using irm):
#
#   $env:PFASIMPLU_REF = 'v0.1.0'; irm .../install.ps1 | iex
#
#   --ref REF        branch, tag or commit to install (default: main)
#   --upgrade        reinstall/refresh an existing installation
#   --python PY      Python version for the tool environment (default: 3.12)
#   --repo URL       install from a fork (git URL, or a local path)
#   --help
#
# Environment: PFASIMPLU_REPO_URL, PFASIMPLU_REF, PFASIMPLU_PYTHON.

$ErrorActionPreference = 'Stop'

$Pkg = 'pfasimplu-cli'
$DefaultRepo = 'https://github.com/ClimenteA/pfasimplu-cli.git'

$RepoUrl = if ($env:PFASIMPLU_REPO_URL) { $env:PFASIMPLU_REPO_URL } else { $DefaultRepo }
$Ref = if ($env:PFASIMPLU_REF) { $env:PFASIMPLU_REF } else { 'main' }
$PythonRequest = if ($env:PFASIMPLU_PYTHON) { $env:PFASIMPLU_PYTHON } else { '3.12' }
$Upgrade = $false

function Write-Step($Message) { Write-Host "`n==> $Message" }
function Write-Info($Message) { Write-Host "    $Message" }
function Write-Warn($Message) { Write-Host "warn: $Message" -ForegroundColor Yellow }

function Show-Usage {
    Write-Host @"
Install $Pkg

  irm <url>/install.ps1 | iex

Options:
  --ref REF        branch, tag or commit to install (default: main)
  --upgrade        reinstall/refresh an existing installation
  --python PY      Python version for the tool environment (default: 3.12)
  --repo URL       install from a fork (git URL, or a local path)
  --help           show this help

Environment: PFASIMPLU_REPO_URL, PFASIMPLU_REF, PFASIMPLU_PYTHON.
"@
}

for ($i = 0; $i -lt $args.Count; $i++) {
    $arg = [string]$args[$i]
    switch -Regex ($arg) {
        '^(-r|--ref)$'      { if ($i + 1 -ge $args.Count) { throw '--ref needs a value' }; $i++; $Ref = [string]$args[$i] }
        '^--ref=(.+)$'      { $Ref = $Matches[1] }
        '^(-u|--upgrade)$'  { $Upgrade = $true }
        '^(-p|--python)$'   { if ($i + 1 -ge $args.Count) { throw '--python needs a value' }; $i++; $PythonRequest = [string]$args[$i] }
        '^--python=(.+)$'   { $PythonRequest = $Matches[1] }
        '^(--repo)$'        { if ($i + 1 -ge $args.Count) { throw '--repo needs a value' }; $i++; $RepoUrl = [string]$args[$i] }
        '^--repo=(.+)$'     { $RepoUrl = $Matches[1] }
        '^(-h|--help)$'     { Show-Usage; return }
        default             { throw "Unknown option: $arg (try --help)" }
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

# --- uv ---------------------------------------------------------------------

Write-Step 'Checking uv'
Update-Path
$uv = Get-Uv
if (-not $uv) {
    Write-Info 'uv not found, installing it from astral.sh'
    irm https://astral.sh/uv/install.ps1 | iex
    Update-Path
    $uv = Get-Uv
}
if (-not $uv) {
    throw 'uv was installed but is not on PATH. Open a new terminal and run this script again.'
}
Write-Info "uv: $(& $uv --version)"

# --- package ----------------------------------------------------------------

$spec = $RepoUrl
if ($spec -match '^git\+') {
    # already a uv style git URL
} elseif ($spec -match '^git@([^:]+):(.+)$') {
    $spec = "git+ssh://git@$($Matches[1])/$($Matches[2])"
} elseif ($spec -match '^(https?|ssh|git)://') {
    $spec = "git+$spec"
}
if ($spec -like 'git+*') {
    if ($Ref) { $spec = "$spec@$Ref" }
}

if (($spec -like 'git+*') -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git is required to install from a repository. Install Git for Windows (https://git-scm.com/download/win) and run this script again.'
}

Write-Step "Installing $Pkg"
Write-Info "source: $spec"
Write-Info "python: $PythonRequest"

$installArgs = @('tool', 'install', '--python', $PythonRequest)
$listed = (& $uv tool list 2>$null) -join "`n"
if ($Upgrade -or ($listed -match "(?m)^$([regex]::Escape($Pkg))\s")) {
    $installArgs += '--force'
}
$installArgs += $spec

& $uv @installArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host @"

error: installation failed.

If the repository is private, git needs credentials. Either make the
repository public, or install over SSH:

    `$env:PFASIMPLU_REPO_URL = 'git@github.com:ClimenteA/pfasimplu-cli.git'
    irm .../install.ps1 | iex

A proxy or a missing Python build toolchain can also break the build.
Run 'uv tool install $spec' by hand to see the full log.
"@ -ForegroundColor Red
    return
}

$toolBinDir = (& $uv tool dir --bin 2>$null)
if (-not $toolBinDir) { $toolBinDir = Join-Path $env:USERPROFILE '.local\bin' }
$cli = Join-Path $toolBinDir "$Pkg.exe"
if (-not (Test-Path $cli)) {
    $plain = Join-Path $toolBinDir $Pkg
    if (Test-Path $plain) { $cli = $plain }
}
if (-not (Test-Path $cli)) {
    $cmd = Get-Command $Pkg -ErrorAction SilentlyContinue
    if ($cmd) { $cli = $cmd.Source }
}
if (-not (Test-Path $cli)) {
    throw "$Pkg was installed but its command was not found. Check $toolBinDir."
}
Write-Info "installed: $(& $cli version) ($cli)"

if ($env:Path -notlike "*$toolBinDir*") {
    Write-Warn "$toolBinDir is not on PATH in this session."
    Write-Info 'Add it once with:  uv tool update-shell  (then open a new terminal)'
}

# --- done -------------------------------------------------------------------

Write-Step 'Ready'
Write-Info "$Pkg --help"
Write-Info "$Pkg init              # create pfasimplu.toml"
Write-Info "$Pkg doctor            # check config, key and dependencies"
Write-Info "$Pkg process .\documente.zip"

Write-Host @"

Before the first run, set the Gemini API key in the current directory:

    `$env:GOOGLE_GENAI_KEY = 'your-key'

or create .\.env with:

    GOOGLE_GENAI_KEY=your-key

The first `process` run asks for the PFA name, CUI/CIF and model, and writes
`pfasimplu.toml` in the current directory. Results go to .\output\ by default
(override with --output-dir). The input ZIP is never modified.

To uninstall later:

    irm https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/uninstall.ps1 | iex
"@
