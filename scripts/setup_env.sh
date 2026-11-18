#!/usr/bin/env bash
# Build Delibra's Python environment from requirements.lock with conda, pip or uv.
#
# Usage: scripts/setup_env.sh [--tool conda|pip|uv] [ENV_PATH]
#   --tool    conda, pip (default) or uv
#   ENV_PATH  where the environment goes (default ./envs)
# It writes only inside ENV_PATH (plus the tool's own package cache).
set -euo pipefail

tool="pip"
env_path="./envs"
script="${BASH_SOURCE[0]}"
case "$script" in
    */*) repo="${script%/*}/.." ;;
    *) repo=".." ;;  # run from inside scripts/
esac

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

need() {
    command -v "$1" > /dev/null 2>&1 || fail "$1 not found; install it or choose another --tool"
}

lock="$repo/requirements.lock"
[ -f "$lock" ] || fail "requirements.lock not found next to scripts/"

case "$tool" in
    conda)
        need conda
        if [ -d "$env_path/conda-meta" ]; then
            conda env update -p "$env_path" -f "$repo/environment.yml" --prune
        else
            conda env create -p "$env_path" -f "$repo/environment.yml"
        fi
        ;;
    pip)
        need python3.12
        python3.12 -m venv "$env_path"
        "$env_path/bin/python" -m pip install -r "$lock"
        ;;
    uv)
        need uv
        uv venv "$env_path" --python 3.12
        uv pip sync --python "$env_path/bin/python" "$lock"
        ;;
esac

printf 'setup_env: %s environment ready at %s\n' "$tool" "$env_path"
