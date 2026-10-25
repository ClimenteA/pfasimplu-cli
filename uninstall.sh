#!/bin/sh
# One-command uninstaller for pfasimplu-cli.
#
#   curl -LsSf https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/uninstall.sh | sh
#
# It removes what install.sh installed: the `uv tool` environment with the CLI.
# `uv` itself, your Gemini key (.env) and the generated results are left alone
# unless you ask for --purge-data.
#
# Works with any POSIX shell: Ubuntu/Debian, macOS, and Windows through
# Git Bash / MSYS2 / WSL. On native Windows PowerShell use uninstall.ps1.
#
# When piping, pass options after `sh -s --`:
#
#   ... | sh -s -- --purge-data
#
# Options:
#   -d, --purge-data      also delete ./pfasimplu.toml and ./output
#   -n, --dry-run         print what would be removed, change nothing
#   -h, --help            show this help
#
# Environment variables: PFASIMPLU_CONFIG (config path),
# PFASIMPLU_OUTPUT_DIR (output folder used by --purge-data).

set -eu

PKG="pfasimplu-cli"

purge_data=0
dry_run=0
config_path="${PFASIMPLU_CONFIG:-./pfasimplu.toml}"
output_dir="${PFASIMPLU_OUTPUT_DIR:-./output}"

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Uninstall pfasimplu-cli

  curl -LsSf <url>/uninstall.sh | sh

Options (when piping, pass them after `sh -s --`):
  -d, --purge-data      also delete ./pfasimplu.toml and ./output
  -n, --dry-run         print what would be removed, change nothing
  -h, --help            show this help

Environment variables: PFASIMPLU_CONFIG, PFASIMPLU_OUTPUT_DIR.

Removes the CLI installed with `uv tool`. `uv` itself and your .env file are
not removed. Generated results stay unless --purge-data is given.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--purge-data) purge_data=1; shift ;;
        -n|--dry-run)    dry_run=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

# --- uv and paths -----------------------------------------------------------

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

is_safe_to_delete() {
    [ -n "$1" ] || return 1
    case "$1" in
        /|.|..|./|"$HOME"|"$HOME/") return 1 ;;
    esac
    if [ -d "$1" ]; then
        resolved="$(cd "$1" 2>/dev/null && pwd -P)" || return 1
    else
        resolved="$1"
    fi
    cwd="$(pwd -P)"
    case "$resolved" in
        /|"$HOME"|"$HOME/"|"$cwd") return 1 ;;
    esac
    return 0
}

step "Checking uv"
refresh_path
uv_bin="$(find_uv)"
if [ -n "$uv_bin" ]; then
    info "uv: $("$uv_bin" --version)"
else
    warn "uv is not installed, so there is no uv tool environment to remove."
    info "If a $PKG command still answers, it was installed another way (pip, pipx, a checkout)."
fi

tool_bin_dir=""
if [ -n "$uv_bin" ]; then
    tool_bin_dir="$("$uv_bin" tool dir --bin 2>/dev/null || true)"
fi
[ -n "$tool_bin_dir" ] || tool_bin_dir="$HOME/.local/bin"

# --- package ----------------------------------------------------------------

step "Removing $PKG"
if [ -z "$uv_bin" ]; then
    info "nothing to remove through uv."
elif ! "$uv_bin" tool list 2>/dev/null | grep -q "^$PKG "; then
    info "$PKG is not installed in uv; nothing to remove."
elif [ "$dry_run" = "1" ]; then
    info "would run: uv tool uninstall $PKG"
else
    "$uv_bin" tool uninstall "$PKG"
    info "removed $PKG"
fi

# --- local files ------------------------------------------------------------

if [ "$purge_data" = "1" ]; then
    step "Removing local config and results"

    if [ -e "$config_path" ]; then
        if is_safe_to_delete "$config_path"; then
            if [ "$dry_run" = "1" ]; then
                info "would delete: $config_path"
            else
                rm -f "$config_path"
                info "deleted: $config_path"
            fi
        else
            warn "refusing to delete unsafe path: $config_path"
        fi
    else
        info "no config file at $config_path"
    fi

    if [ -e "$output_dir" ]; then
        if is_safe_to_delete "$output_dir"; then
            if [ "$dry_run" = "1" ]; then
                info "would delete: $output_dir"
            else
                rm -rf "$output_dir"
                info "deleted: $output_dir"
            fi
        else
            warn "refusing to delete unsafe path: $output_dir"
        fi
    else
        info "no output folder at $output_dir"
    fi
else
    info "config and results stay in $(pwd) (use --purge-data to delete them)"
fi

# --- done -------------------------------------------------------------------

step "Done"
if [ "$dry_run" = "1" ]; then
    info "dry run: nothing was changed."
fi
if [ -n "$uv_bin" ]; then
    uv_note="still installed in $tool_bin_dir (it may serve other tools)"
else
    uv_note="not found on this machine"
fi
cat <<EOF

Uninstalled. What was left in place:

    uv                 $uv_note
    Gemini key         .env in $(pwd) is not touched
    config/results     ${config_path} and ${output_dir} (unless --purge-data)

To drop uv as well:  uv self uninstall
EOF
