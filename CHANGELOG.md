# Omacleaner changelog

## Unreleased

- Use the same Bash exclusion matcher for scanning and recursive cleanup,
  preserving saved character classes, escaped wildcards and extended patterns.
- Stop on unreadable, malformed or invalid saved restrictions; preserve an
  explicitly empty selection and exclusions containing newlines.
- Confine user-file operations through directory descriptors, refuse symlinked
  ancestors and mounted children, including same-device bind mounts.
- Preserve exact filename bytes in encoded scan records and panel Trash actions.
- Keep personal scan paths out of subprocess arguments, including scan merges.
- Preserve pre-existing CLI links and verify timer ownership before disabling.
- Keep restore metadata for excluded or partially cleaned Trash payloads.
- Add Python 3 as an explicit dependency for filesystem guards and regression tests.

## 1.0.0

- New Omacleaner identity, author Zizor, MIT license.
- Expandable details for all 13 disk categories: includes, cleanup effects,
  exclusions and practical advice, including unavailable categories.
- User caches open expanded with per-application sizes, full paths, purpose
  and clearing effects. Unknown folders are marked for review.
- Full scan item lists in JSON and saved state; panel pagination in groups of 12.
- Folder opening, keyboard-accessible details buttons, and confirmation before
  moving individual large files to Trash.
- Refuse signaling PID 0 or 1, including user-owned container init processes.
- Local source installer; independent CLI, state and configuration namespaces.
- Adwaita Sans panel text for clearer digits, using the installed system font.
- Publication documentation and repository metadata.

Based on Omarchy Cleaner 0.2.1 by momoledev (MIT).

---

# Upstream changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [0.2.1] — 2026-10-21

### Fixed
- **Ignore list: excluded paths nested inside a cleaned folder were deleted.**
  Example: ignoring `~/.cache/myapp/keep` while cleaning `user-cache`. The
  exclusion was checked only on the item being cleaned (`~/.cache/myapp`), and
  then all its children were removed recursively. Cleaning now prunes around
  excluded paths at any depth: they and their parent folders stay, the rest is
  removed. Moving a folder that contains an excluded path to Trash is refused.
  Scan sizes no longer count protected data. Reported by the marketplace
  reviewers.
- `ignore add` stored glob entries as paths relative to the current
  directory; globs are now stored as written.

### Added
- Regression tests for nested, deeply nested and glob exclusions, for
  `oc_delete` pruning and for the Trash refusal.

## [0.2.0] — 2026-10-21

### Added
- **Memory** tab in the panel:
  - RAM and swap gauges, with the reclaimable file cache shown separately;
  - applications ranked by real memory use (PSS summed per systemd unit),
    expandable to their heaviest processes;
  - clean quit (SIGTERM) and force quit (SIGKILL) with confirmation;
  - stuck processes (stopped, zombie, disk-wait) with an explanation.
- `omacleaner memory`, `memory quit UNIT` and `memory kill PID`
  commands, with `--force` and JSON output.
- **Drop file cache** maintenance task (`drop-caches` root verb), behind a
  warning explaining why it rarely helps.
- IPC methods `showMemory` and `showDisk`.
- Tests for the memory view and quit safety, including quitting a real app scope.

### Changed
- The confirmation dialog now handles every destructive action (clean, quit,
  drop cache).
- Preview image shows both tabs.

## [0.1.0] — 2026-10-21

### Added
- Bar widget and popup panel for the Omarchy Quattro shell:
  - categories with sizes, pre-selection and risk hints;
  - item previews;
  - review-only large files with "open folder" and "move to Trash" actions;
  - a confirmation dialog;
  - maintenance buttons.
- `omacleaner` CLI with JSON output: `scan`, `clean` (including
  `--dry-run` and `--defaults`), `maintain`, `trash`, `status`, `modules`,
  `tasks`, `ignore`, `config`, `schedule`, `history`, `link`/`unlink`, `open`.
- 13 modules: user caches, developer caches, AUR build cache, downloaded
  models and browsers, Trash, pacman cache, journal, crash dumps, orphan
  packages, snapper snapshots, Docker leftovers, broken launchers, large files.
- 5 maintenance tasks: font cache, launcher database, DNS flush, SSD trim,
  file search index.
- Privileged helper `omacleaner-root`, run through pkexec, limited to a
  fixed list of verbs.
- Remote control:
  - shell IPC target `omacleaner`
    (`open`, `close`, `toggle`, `scan`, `cleanDefaults`, `status`);
  - an opt-in systemd user timer for scheduled scans with notifications.
- Ignore list, an optional config file and a clean history.
- Test suite (`tests/run.sh`) that runs in a throw-away home directory.
