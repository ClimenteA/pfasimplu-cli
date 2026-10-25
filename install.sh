#!/bin/sh
# One-command installer for pfasimplu-cli.
#
#   curl -LsSf https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/install.sh | sh
#
# Works with any POSIX shell: Ubuntu/Debian, macOS, and Windows through
# Git Bash / MSYS2 / WSL. On native Windows PowerShell use install.ps1.
#
# When piping, pass options after `sh -s --`:
#
#   ... | sh -s -- --ref v0.1.0
#
# Options:
#   -r, --ref REF        branch, tag or commit to install (default: main)
#   -u, --upgrade        reinstall/refresh an existing installation
#   -p, --python PY      Python version for the tool environment (default: 3.12)
#       --repo URL       install from a fork (git URL, or a local path)
#   -h, --help           show this help
#
# Environment variables: PFASIMPLU_REPO_URL, PFASIMPLU_REF, PFASIMPLU_PYTHON.

set -eu

PKG="pfasimplu-cli"
DEFAULT_REPO="https://github.com/ClimenteA/pfasimplu-cli.git"

repo_url="${PFASIMPLU_REPO_URL:-$DEFAULT_REPO}"
ref="${PFASIMPLU_REF:-main}"
python_req="${PFASIMPLU_PYTHON:-3.12}"
upgrade=0

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Install pfasimplu-cli

  curl -LsSf <url>/install.sh | sh

Options (when piping, pass them after `sh -s --`):
  -r, --ref REF        branch, tag or commit to install (default: main)
  -u, --upgrade        reinstall/refresh an existing installation
  -p, --python PY      Python version for the tool environment (default: 3.12)
      --repo URL       install from a fork (git URL, or a local path)
  -h, --help           show this help

Environment variables: PFASIMPLU_REPO_URL, PFASIMPLU_REF, PFASIMPLU_PYTHON.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -r|--ref)
            [ $# -ge 2 ] || die "--ref needs a value"
            ref="$2"; shift 2 ;;
        --ref=*)   ref="${1#*=}"; shift ;;
        -u|--upgrade) upgrade=1; shift ;;
        -p|--python)
            [ $# -ge 2 ] || die "--python needs a value"
            python_req="$2"; shift 2 ;;
        --python=*) python_req="${1#*=}"; shift ;;
        --repo)
            [ $# -ge 2 ] || die "--repo needs a value"
            repo_url="$2"; shift 2 ;;
        --repo=*)  repo_url="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

# --- platform ---------------------------------------------------------------

os="$(uname -s)"
case "$os" in
    Linux*)   platform=linux ;;
    Darwin*)  platform=macos ;;
    MINGW*|MSYS*|CYGWIN*) platform=windows ;;
    *) die "Unsupported operating system: $os" ;;
esac

if command -v curl >/dev/null 2>&1; then
    fetch_show() { curl -LsSf "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch_show() { wget -qO- "$1"; }
else
    die "curl or wget is required."
fi

# --- helpers ----------------------------------------------------------------

refresh_path() {
    # uv writes ~/.local/bin/env on Unix; read it when present.
    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.local/bin/env"
    fi
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
    esac
}

find_uv() {
    command -v uv 2>/dev/null || true
}

build_spec() {
    case "$repo_url" in
        git+*)        spec="$repo_url" ;;
        http://*|https://*|ssh://*|git://*) spec="git+$repo_url" ;;
        git@*)
            host_path="$(printf '%s' "$repo_url" | sed 's/^git@//')"
            host="${host_path%%:*}"
            path="${host_path#*:}"
            spec="git+ssh://git@$host/$path"
            ;;
        *)            spec="$repo_url" ;;   # local path or pip-style requirement
    esac
    case "$spec" in
        git+*)
            if [ -n "$ref" ]; then
                spec="$spec@$ref"
            fi
            ;;
    esac
}

is_installed() {
    uv tool list 2>/dev/null | grep -q "^$PKG "
}

# --- uv ---------------------------------------------------------------------

step "Checking uv"
refresh_path
uv_bin="$(find_uv)"
if [ -z "$uv_bin" ]; then
    info "uv not found, installing it from astral.sh"
    fetch_show https://astral.sh/uv/install.sh | sh
    refresh_path
    uv_bin="$(find_uv)"
fi
[ -n "$uv_bin" ] || die "uv is not available. The automatic install failed; install uv from https://docs.astral.sh/uv/ and run this script again."
info "uv: $("$uv_bin" --version)"

build_spec

case "$spec" in
    git+*)
        if ! command -v git >/dev/null 2>&1; then
            case "$platform" in
                linux)
                    die "git is required. Install it with: sudo apt install git"
                    ;;
                macos)
                    die "git is required. Run: xcode-select --install"
                    ;;
                *)
                    die "git is required. Install Git and run this script again."
                    ;;
            esac
        fi
        ;;
esac

# --- package ----------------------------------------------------------------

step "Installing $PKG"
info "source: $spec"
info "python: $python_req"

set -- --python "$python_req"
if [ "$upgrade" = "1" ] || is_installed; then
    set -- "$@" --force
fi
set -- "$@" "$spec"

if ! "$uv_bin" tool install "$@"; then
    cat >&2 <<EOF

error: installation failed.

If the repository is private, git needs credentials. Either make the
repository public, or install over SSH:

    curl -LsSf https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/install.sh \\
      | sh -s -- --repo git@github.com:ClimenteA/pfasimplu-cli.git

A proxy or a missing Python build toolchain can also break the build;
re-run with -v (or run 'uv tool install $spec' by hand) to see the details.
EOF
    exit 1
fi

tool_bin_dir="$("$uv_bin" tool dir --bin 2>/dev/null || true)"
[ -n "$tool_bin_dir" ] || tool_bin_dir="$HOME/.local/bin"

cli=""
for candidate in "$tool_bin_dir/$PKG" "$tool_bin_dir/$PKG.exe"; do
    if [ -x "$candidate" ]; then
        cli="$candidate"
        break
    fi
done
if [ -z "$cli" ]; then
    cli="$(command -v "$PKG" 2>/dev/null || true)"
fi
[ -n "$cli" ] || die "$PKG was installed but its command was not found. Check $tool_bin_dir."

installed_version="$("$cli" version 2>/dev/null || printf 'unknown')"
info "installed: $installed_version ($cli)"

case ":$PATH:" in
    *":$tool_bin_dir:"*) ;;
    *)
        warn "$tool_bin_dir is not on PATH in this shell."
        info "Add it once, then open a new terminal:"
        info "  echo 'export PATH=\"$tool_bin_dir:\$PATH\"' >> ~/.profile"
        ;;
esac

# --- done -------------------------------------------------------------------

step "Ready"
info "pfasimplu-cli --help"
info "pfasimplu-cli init              # create pfasimplu.toml"
info "pfasimplu-cli doctor            # check config, key and dependencies"
info "pfasimplu-cli process documente.zip"

cat <<'EOF'

Before the first run, set the Gemini API key in the current directory:

    export GOOGLE_GENAI_KEY="your-key"      # or create ./.env with:
    # GOOGLE_GENAI_KEY=your-key

The first `process` run asks for the PFA name, CUI/CIF and model, and writes
`pfasimplu.toml` in the current directory. Results go to `./output/` by default
(override with --output-dir). The input ZIP is never modified.

To uninstall later:

    curl -LsSf https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/uninstall.sh | sh
EOF
