#!/bin/bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
plugin_root="$HOME/.config/omarchy/plugins"
target="$plugin_root/omacleaner"
for dependency in omarchy omarchy-shell bash jq gawk find du flock; do
  command -v "$dependency" >/dev/null || { echo "Missing dependency: $dependency" >&2; exit 1; }
done
[[ ! -e $target && ! -L $target ]] || { echo "Already installed: $target" >&2; exit 1; }
omarchy plugin validate "$source_dir"
omarchy-shell shell ping >/dev/null
mkdir -p "$plugin_root"
stage=$(mktemp -d "$plugin_root/.omacleaner-install.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
for file in manifest.json Panel.qml DetailText.qml README.md LICENSE CHANGELOG.md; do
  cp -- "$source_dir/$file" "$stage/"
done
cp -r -- "$source_dir/bin" "$source_dir/lib" "$source_dir/docs" "$stage/"
omarchy plugin validate "$stage"
if [[ -f $HOME/.config/omarchy/shell.json ]]; then
  cp -- "$HOME/.config/omarchy/shell.json" "$HOME/.config/omarchy/shell.json.omacleaner-backup-$(date +%Y%m%d-%H%M%S)"
fi
mv -- "$stage" "$target"
omarchy-shell shell rescanPlugins >/dev/null
for ((attempt=0; attempt<80; attempt++)); do
  if omarchy plugin list --json | jq -e 'any(.[]; .id == "omacleaner")' >/dev/null; then
    omarchy plugin enable omacleaner --section right
    echo "Omacleaner is ready. Click the broom icon in your bar."
    exit 0
  fi
  sleep 0.1
done
echo "Installed, but discovery timed out. Enable with: omarchy plugin enable omacleaner --section right" >&2
exit 1
