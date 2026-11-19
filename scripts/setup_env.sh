#!/usr/bin/env bash
# Build Delibra's Python environment from requirements.lock with conda, pip or uv.
#
# Usage: scripts/setup_env.sh [--tool conda|pip|uv] [ENV_PATH]
#   --tool    conda, pip (default) or uv
#   ENV_PATH  where the environment goes (default: envs/ in this repository)
# It installs into ENV_PATH only; the tools keep their own package caches, and
# conda writes a temporary requirements file while it installs the pip section.
# An existing ENV_PATH must already be an environment of the chosen kind: a conda
# env is never turned into a venv or the other way round. A conda update only adds.
set -euo pipefail

tool="pip"
env_path=""
script="${BASH_SOURCE[0]}"
case "$script" in
    */*) script_dir="${script%/*}" ;;
    *) script_dir="." ;;
esac
repo="$(cd "$script_dir/.." && pwd)"

fail() {
    printf 'setup_env: %s\n' "$1" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --tool)
            [ "$#" -ge 2 ] || fail "--tool needs a value (conda, pip or uv)"
            tool="$2"
            shift 2
            ;;
        --tool=*)
            tool="${1#--tool=}"
            shift
            ;;
        -h|--help)
            printf 'Usage: scripts/setup_env.sh [--tool conda|pip|uv] [ENV_PATH]\n'
            exit 0
            ;;
        -*)
            fail "unknown option '$1'"
            ;;
        *)
            env_path="$1"
            shift
            ;;
    esac
done

case "$tool" in
    conda|pip|uv) ;;
    *) fail "unknown tool '$tool' (use conda, pip or uv)" ;;
esac

# The default is this repository's envs/, wherever the script is run from.
[ -n "$env_path" ] || env_path="$repo/envs"

need() {
    command -v "$1" > /dev/null 2>&1 || fail "$1 not found; install it or choose another --tool"
}

# A missing or empty path is fine; otherwise it must be this tool's kind of env.
check_kind() {
    local marker="$1" kind="$2"
    [ -e "$env_path" ] || return 0
    [ -d "$env_path" ] || fail "$env_path is not a $kind environment"
    [ -e "$env_path/$marker" ] && return 0
    shopt -s nullglob dotglob
    local contents=("$env_path"/*)
    shopt -u nullglob dotglob
    [ "${#contents[@]}" -eq 0 ] || fail "$env_path is not a $kind environment; choose another path or tool"
}

lock="$repo/requirements.lock"
[ -f "$lock" ] || fail "requirements.lock not found in $repo"

case "$tool" in
    conda)
        need conda
        check_kind conda-meta conda
        if [ -d "$env_path/conda-meta" ]; then
            conda env update -p "$env_path" -f "$repo/environment.yml"
        else
            conda env create -p "$env_path" -f "$repo/environment.yml"
        fi
        ;;
    pip)
        need python3.12
        check_kind pyvenv.cfg venv
        python3.12 -m venv "$env_path"
        "$env_path/bin/python" -m pip install -r "$lock"
        ;;
    uv)
        need uv
        check_kind pyvenv.cfg venv
        uv venv "$env_path" --python 3.12
        uv pip sync --python "$env_path/bin/python" "$lock"
        ;;
esac

printf 'setup_env: %s environment ready at %s\n' "$tool" "$env_path"
