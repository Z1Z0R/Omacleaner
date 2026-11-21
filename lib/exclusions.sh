#!/bin/bash
# Shared Bash pattern semantics for shell scans and descriptor-based cleanup.
# Patterns are data in [[ ]], never shell source or eval input.

oc_ignore_matches() {
  local path=$1 entry
  shift
  while :; do
    for entry in "$@"; do
      [[ $entry == / ]] && return 0
      # shellcheck disable=SC2053 # saved Bash patterns are intentional
      if [[ $path == "$entry" || $path == "$entry"/* || $path == $entry ]]; then
        return 0
      fi
    done
    [[ $path == / || $path != */* ]] && return 1
    path=${path%/*}
    [[ -n $path ]] || path=/
  done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  # Persistent private pipe protocol: count, rules, then paths, all terminated
  # by NUL. Replies are exactly "1\0" (excluded) or "0\0" (not excluded).
  IFS= read -r -d '' count || exit 1
  [[ $count =~ ^[0-9]+$ ]] || exit 1
  rules=()
  for ((i=0; i<count; i++)); do
    IFS= read -r -d '' entry || exit 1
    rules+=("$entry")
  done
  while IFS= read -r -d '' path; do
    if oc_ignore_matches "$path" "${rules[@]}"; then
      printf '1\0'
    else
      printf '0\0'
    fi
  done
fi
