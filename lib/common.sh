#!/bin/bash
# shellcheck shell=bash
#
# Shared helpers for omacleaner: logging, paths, configuration, ignore
# list, size parsing and the guarded deletion primitives every module uses.
#
# This file is sourced by bin/omacleaner; it never runs on its own.

# ---------------------------------------------------------------------------
# Locations (XDG-aware). Nothing here is created until it is actually needed.
# ---------------------------------------------------------------------------
OC_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omacleaner"
OC_CONFIG_FILE="$OC_CONFIG_DIR/config.json"
OC_STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omacleaner"
OC_LAST_SCAN="$OC_STATE_DIR/last-scan.json"
OC_HISTORY="$OC_STATE_DIR/history.jsonl"
OC_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
OC_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
# shellcheck disable=SC2034 # used by bin/omacleaner
OC_LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/omacleaner-$UID.lock"

# Set by the CLI from --json / --quiet.
OC_JSON=${OC_JSON:-0}
OC_QUIET=${OC_QUIET:-0}

# ---------------------------------------------------------------------------
# Logging. Human messages go to stderr so stdout stays clean for --json.
# Prefixes follow the Omarchy convention: "==>" step, "  -" done, "  !" warning.
# ---------------------------------------------------------------------------
oc_step() { [[ $OC_QUIET == 1 ]] || printf '==> %s\n' "$*" >&2; }
oc_info() { [[ $OC_QUIET == 1 ]] || printf '  - %s\n' "$*" >&2; }
oc_warn() { printf '  ! %s\n' "$*" >&2; }
oc_die() {
  local code=${2:-1}
  if [[ $OC_JSON == 1 ]]; then
    jq -cn --arg error "$1" --argjson code "$code" '{ok:false,error:$error,code:$code}'
  else
    printf 'omacleaner: %s\n' "$1" >&2
  fi
  exit "$code"
}

oc_require() {
  local bin
  for bin in "$@"; do
    command -v "$bin" >/dev/null 2>&1 || oc_die "required command not found: $bin"
  done
}

# Human-readable size (IEC) for a byte count.
oc_human() {
  numfmt --to=iec-i --suffix=B --format='%.1f' "${1:-0}" 2>/dev/null || printf '%sB' "${1:-0}"
}

# Convert sizes printed by other tools to bytes:
#   "830.2 MiB" (paccache), "1.2GB" / "12kB" (docker, decimal),
#   "1.4G" / "512K" (journalctl, du -h: single-letter units are binary).
# Prints 0 for anything it cannot parse.
oc_to_bytes() {
  local raw="${1//[[:space:]]/}"
  [[ $raw =~ ^([0-9]+([.][0-9]+)?)([A-Za-z]*)$ ]] || { echo 0; return; }
  local num=${BASH_REMATCH[1]} unit=${BASH_REMATCH[3]} mult
  case "${unit,,}" in
    '' | b) mult=1 ;;
    kb) mult=1000 ;;
    k | kib) mult=1024 ;;
    mb) mult=1000000 ;;
    m | mib) mult=1048576 ;;
    gb) mult=1000000000 ;;
    g | gib) mult=1073741824 ;;
    tb) mult=1000000000000 ;;
    t | tib) mult=1099511627776 ;;
    *) echo 0; return ;;
  esac
  awk -v n="$num" -v m="$mult" 'BEGIN { printf "%.0f\n", n * m }'
}

# Apparent size in bytes of a file or directory tree, without crossing
# filesystems. Unreadable sub-entries are skipped rather than failing.
# Filesystem helper input is NUL-delimited; no personal path enters argv.
oc_fs() {
  oc_ignore_load
  { printf '%s\0' "$2" "$HOME" "$OC_CACHE_HOME" "$OC_DATA_HOME" "$OC_CONFIG_DIR";
    if ((${#OC_IGNORE[@]})); then printf '%s\0' "${OC_IGNORE[@]}"; fi;
  } | python3 "$OC_FS_HELPER" "$1"
}

oc_path_bytes() { oc_fs size "$1"; }

# Read one validated snapshot. Only a genuinely absent config uses defaults.
OC_FS_HELPER="${OC_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/lib/filesystem.py"
OC_CONFIG_JSON='{}'
OC_CONFIG_LOADED=0
oc_config_load() {
  ((OC_CONFIG_LOADED == 1)) && return 0
  local current
  current=$(printf '%s\0' "$OC_CONFIG_FILE" "$HOME" "$OC_CACHE_HOME" "$OC_DATA_HOME" "$OC_CONFIG_DIR" | python3 "$OC_FS_HELPER" config) || oc_die "configuration cannot be read; refusing to use defaults" 1
  if [[ $current != '{}' ]]; then
    OC_CONFIG_JSON=$(jq -ces '
      if length != 1 then error("expected one object") else .[0] end
      | if type != "object" then error("expected an object") else . end
      | if (has("ignore") and (.ignore | type != "array" or any(.[]; type != "string" or length == 0 or contains("\u0000"))))
          or (has("selection") and (.selection | type != "array" or any(.[]; type != "string")))
          then error("invalid restrictions") else . end
    ' <<<"$current" 2>/dev/null) || oc_die "configuration is invalid; refusing to use defaults" 1
    local key value
    for key in pacmanKeep journalMaxMB largeFileMinMB notifyThresholdMB; do
      jq -e --arg k "$key" 'has($k) | not' <<<"$OC_CONFIG_JSON" >/dev/null && continue
      jq -e --arg k "$key" '.[$k] | type == "number" and . >= 0 and . <= 999999 and floor == .' <<<"$OC_CONFIG_JSON" >/dev/null || oc_die "invalid configuration value: $key" 1
    done
    if declare -F validate_modules >/dev/null; then
      while IFS= read -r -d '' value; do validate_modules "$value"; done < <(jq -j '.selection // [] | .[] | ., "\u0000"' <<<"$OC_CONFIG_JSON")
    fi
  fi
  OC_CONFIG_LOADED=1
}

oc_config_get() {
  oc_config_load
  jq -r --arg k "$1" --arg fallback "$2" 'if has($k) then .[$k] | tostring else $fallback end' <<<"$OC_CONFIG_JSON"
}

oc_config_write() {
  oc_config_load
  mkdir -p "$OC_CONFIG_DIR"
  local tmp
  tmp=$(mktemp "$OC_CONFIG_DIR/.config.XXXXXX")
  if ! jq "${@:2}" "$1" <<<"$OC_CONFIG_JSON" >"$tmp"; then
    rm -f "$tmp"
    oc_die "could not update configuration"
  fi
  mv -f "$tmp" "$OC_CONFIG_FILE"
}

oc_ignore_list() {
  oc_ignore_load
  if ((${#OC_IGNORE[@]})); then printf '%s\n' "${OC_IGNORE[@]}"; fi
}

OC_IGNORE=()
OC_IGNORE_LOADED=0
oc_ignore_load() {
  ((OC_IGNORE_LOADED == 1)) && return 0
  oc_config_load
  mapfile -d '' -t OC_IGNORE < <(jq -j '.ignore // [] | .[] | ., "\u0000"' <<<"$OC_CONFIG_JSON")
  local i entry
  for i in "${!OC_IGNORE[@]}"; do
    entry=${OC_IGNORE[$i]}
    entry=${entry/#\~/$HOME}
    while [[ $entry == */ && $entry != / ]]; do entry=${entry%/}; done
    OC_IGNORE[$i]=$entry
  done
  OC_IGNORE_LOADED=1
}

oc_is_glob() { [[ $1 == *[\*?[]* ]]; }

# True when a path is covered by the ignore list: equal to an entry, inside
# an entry, or matching an entry used as a glob.
# shellcheck source=lib/exclusions.sh
source "${OC_FS_HELPER%/*}/exclusions.sh"
oc_is_ignored() {
  oc_ignore_load
  oc_ignore_matches "$1" "${OC_IGNORE[@]}"
}

# Size and removal use the same mount and exclusion policy.
oc_reclaimable_bytes() { oc_fs size "$1"; }

# ---------------------------------------------------------------------------
# Guarded deletion. Every user-level removal goes through these helpers, which
# refuse anything outside $HOME (or a mounted volume's own trash), the home
# directory itself, top-level XDG directories, and ignored paths.
# ---------------------------------------------------------------------------
oc_path_allowed() {
  local p=$1
  [[ $p == /* ]] || return 1
  [[ $p != *"/../"* && $p != *"/.." && $p != *"/./"* ]] || return 1
  case "$p" in
    "$HOME" | "$HOME/" | "$OC_CACHE_HOME" | "$OC_DATA_HOME" | "$HOME/.config" | "$HOME/.local" | "$OC_CONFIG_DIR"*) return 1 ;;
    "$HOME"/* | "/run/media/$USER"/*/.Trash-"$UID"/* | /media/*/.Trash-"$UID"/*) ;;
    *) return 1 ;;
  esac
  oc_is_ignored "$p" && return 1
  return 0
}

# The helper checks the actual parents, mount IDs and exclusions again for
# every operation. Path checks above are only a fast lexical rejection.
oc_empty_dir() { oc_fs empty "$1"; }
oc_delete() { oc_fs delete "$1"; }
oc_trash() { oc_fs trash "$1"; }

# ---------------------------------------------------------------------------
# Item emission. A module scan prints one item per line as
#   <bytes>\t<base64-path>\t<base64-label>
# Path-based items that are ignored or empty are filtered out here.
# ---------------------------------------------------------------------------
oc_emit() {
  local bytes=$1 path=$2 label=$3
  [[ $bytes =~ ^[0-9]+$ ]] || bytes=0
  printf '%s\t%s\t%s\n' "$bytes" "$(printf %s "$path" | base64 -w0)" "$(printf %s "$label" | base64 -w0)"
}

oc_emit_path() {
  local path=$1 label=${2:-${1##*/}} bytes
  [[ -e $path ]] || return 0
  oc_is_ignored "$path" && return 0
  bytes=$(oc_reclaimable_bytes "$path") || return 0
  ((bytes > 0)) || return 0
  oc_emit "$bytes" "$path" "$label"
}

# ---------------------------------------------------------------------------
# State: last scan + clean history (used by the bar widget and `history`).
# ---------------------------------------------------------------------------
oc_state_write() {
  mkdir -p "$OC_STATE_DIR"
  local tmp
  tmp=$(mktemp "$OC_STATE_DIR/.scan.XXXXXX")
  cat >"$tmp"
  mv -f "$tmp" "$OC_LAST_SCAN"
}

oc_history_append() {
  mkdir -p "$OC_STATE_DIR"
  printf '%s\n' "$1" >>"$OC_HISTORY"
}

# Ask for confirmation on a terminal; refuse (exit 3) when nobody can answer.
oc_confirm() {
  local prompt=$1
  [[ ${OC_YES:-0} == 1 ]] && return 0
  if [[ ! -t 0 || ! -t 2 ]]; then
    oc_die "refusing to clean without a terminal; pass --yes to confirm" 3
  fi
  if command -v gum >/dev/null 2>&1; then
    gum confirm "$prompt"
  else
    local answer
    read -r -p "$prompt [y/N] " answer
    [[ $answer == [yY]* ]]
  fi
}
