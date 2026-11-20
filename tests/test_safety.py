#!/usr/bin/env python3
"""Listing-review regressions, using temporary homes and no privileged cleanup."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
CLI = str(REPO / "bin/omacleaner")
spec = importlib.util.spec_from_file_location("filesystem", REPO / "lib/filesystem.py")
filesystem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(filesystem)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="omacleaner-safety-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), XDG_RUNTIME_DIR=str(self.root))
        for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            self.env.pop(key, None)
        self.config = self.home / ".config/omacleaner/config.json"

    def cli(self, *args, ok=True):
        result = subprocess.run([CLI, *args], env=self.env, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result

    def file(self, relative, contents=b"keep this data"):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def save(self, data):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(data))

    def fs(self):
        return filesystem.Filesystem(str(self.home), str(self.home / ".cache"),
                                     str(self.home / ".local/share"),
                                     str(self.config.parent), [])

    def test_invalid_config_blocks_confirmed_cleanup(self):
        cache = self.file(".cache/app/data")
        trash = self.file(".local/share/Trash/files/personal")
        for raw in ('{', '', '[]', 'null', '{} {}', '{"ignore":null}',
                    '{"ignore":[false]}', '{"ignore":["a\\u0000b"]}',
                    '{"selection":null}', '{"selection":["trash\\nuser-cache"]}',
                    '{"journalMaxMB":"200"}'):
            with self.subTest(raw=raw):
                self.config.parent.mkdir(parents=True, exist_ok=True)
                self.config.write_text(raw)
                self.assertNotEqual(self.cli("clean", "user-cache", "trash", "--yes", "--json", ok=False).returncode, 0)
                self.assertTrue(cache.exists())
                self.assertTrue(trash.exists())

    def test_unreadable_config_blocks_cleanup(self):
        if os.geteuid() == 0:
            self.skipTest("root can read mode-000 files")
        cache = self.file(".cache/app/data")
        self.save({"ignore": [str(cache)]})
        self.config.chmod(0)
        self.addCleanup(self.config.chmod, 0o600)
        self.assertNotEqual(self.cli("clean", "user-cache", "--yes", ok=False).returncode, 0)
        self.assertTrue(cache.exists())

    def test_unreadable_config_parent_is_not_absence(self):
        if os.geteuid() == 0:
            self.skipTest("root can traverse mode-000 directories")
        cache = self.file(".cache/app/data")
        self.save({"ignore": [str(cache)]})
        self.config.parent.chmod(0)
        self.addCleanup(self.config.parent.chmod, 0o700)
        self.assertNotEqual(self.cli("clean", "user-cache", "--yes", ok=False).returncode, 0)
        self.assertTrue(cache.exists())

    def test_dangling_config_link_blocks_cleanup(self):
        cache = self.file(".cache/app/data")
        self.config.parent.mkdir(parents=True)
        self.config.symlink_to(self.root / "missing")
        self.assertNotEqual(self.cli("clean", "user-cache", "--yes", ok=False).returncode, 0)
        self.assertTrue(cache.exists())

    def test_invalid_config_bytes_cannot_be_normalized_into_valid_settings(self):
        cache = self.file(".cache/app/data")
        for raw in (b'{"ignore":[]}\0', b'{"ignore":["invalid-\xff"]}'):
            self.config.parent.mkdir(parents=True, exist_ok=True)
            self.config.write_bytes(raw)
            self.assertNotEqual(self.cli("clean", "user-cache", "--yes", ok=False).returncode, 0)
            self.assertTrue(cache.exists())

    def test_empty_selection_never_falls_back(self):
        trash = self.file(".local/share/Trash/files/personal")
        self.save({"selection": []})
        self.cli("clean", "--defaults", "--yes", ok=False)
        self.assertTrue(trash.exists())
        result = json.loads(self.cli("scan", "trash", "user-cache", "--json").stdout)
        self.assertTrue(all(not module["selected"] for module in result["modules"]))

    def test_selection_setter_rejects_non_ids_and_ignore_types(self):
        for key, value in (("selection", '["trash\\nuser-cache"]'),
                           ("selection", '[{}]'), ("ignore", '[false]'),
                           ("ignore", '["a\\u0000b"]')):
            self.assertNotEqual(self.cli("config", "set", key, value, ok=False).returncode, 0)
        self.assertFalse(self.config.exists())

    def test_newline_exclusion_is_preserved(self):
        keep = self.file(".cache/app/keep\nfolder/data")
        remove = self.file(".cache/app/remove")
        self.save({"ignore": [str(keep.parent)]})
        self.cli("clean", "user-cache", "--yes")
        self.assertTrue(keep.exists())
        self.assertFalse(remove.exists())

    def test_literal_brackets_and_overlapping_exclusions(self):
        keep = self.file(".cache/app/keep[1]/data")
        remove = self.file(".cache/app/remove")
        self.save({"ignore": [str(keep.parent), str(keep)]})
        self.cli("clean", "user-cache", "--yes")
        self.assertTrue(keep.exists())
        self.assertFalse(remove.exists())

    def test_symlinked_parents_cannot_redirect_cleanup_or_trash(self):
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "_cacache/data"
        victim.parent.mkdir()
        victim.write_text("valuable")
        (self.home / ".npm").symlink_to(outside, target_is_directory=True)
        self.cli("clean", "dev-cache", "--yes")
        self.assertTrue(victim.exists())
        self.assertNotEqual(self.cli("trash", str(self.home / ".npm/_cacache"), "--yes", ok=False).returncode, 0)
        self.assertTrue(victim.exists())

    def test_symlink_leaf_removed_without_following(self):
        target = self.file("Documents/keep")
        link = self.home / ".cache/app/link"
        link.parent.mkdir(parents=True)
        link.symlink_to(target.parent, target_is_directory=True)
        self.cli("clean", "user-cache", "--yes")
        self.assertTrue(target.exists())
        self.assertFalse(link.is_symlink())

    def test_redirected_trash_destination_is_refused(self):
        victim = self.file("Documents/personal")
        outside = self.root / "outside"
        outside.mkdir()
        trash = self.home / ".local/share/Trash"
        trash.mkdir(parents=True)
        (trash / "files").symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.cli("trash", str(victim), "--yes", ok=False).returncode, 0)
        self.assertTrue(victim.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_trash_destination_cannot_escape_through_dotdot(self):
        victim = self.file("Documents/keep")
        self.env["XDG_DATA_HOME"] = str(self.home) + "/../outside"
        self.assertNotEqual(self.cli("trash", str(victim), "--yes", ok=False).returncode, 0)
        self.assertTrue(victim.exists())
        self.assertFalse((self.root / "outside").exists())

    def test_mount_id_guard_detects_same_device_child(self):
        victim = self.file(".cache/app/mounted/keep")
        fs = self.fs()
        original = filesystem.mount_id

        def different_mount(fd):
            path = os.readlink(f"/proc/self/fd/{fd}")
            return original(fd) + (100000 if path.endswith("/mounted") else 0)

        with mock.patch.object(filesystem, "mount_id", side_effect=different_mount):
            with self.assertRaises(OSError):
                path = str(victim.parent.parent)
                with fs.parent(path) as (parent, name, boundary):
                    fs.remove_at(parent, name, path, boundary, keep=True)
        self.assertTrue(victim.exists())

    def test_mount_boundary_is_checked_for_selected_and_parent_directories(self):
        victim = self.file(".cache/app/mounted/child/keep")
        fs = self.fs()
        original = filesystem.mount_id

        def different_mount(fd):
            path = os.readlink(f"/proc/self/fd/{fd}")
            return original(fd) + (100000 if "/mounted" in path else 0)

        with mock.patch.object(filesystem, "mount_id", side_effect=different_mount):
            for path in (victim.parent.parent, victim.parent):
                with self.assertRaises(OSError):
                    with fs.parent(str(path)) as (parent, name, boundary):
                        fs.remove_at(parent, name, str(path), boundary, keep=True)
        self.assertTrue(victim.exists())

    def test_real_bind_mount_boundary(self):
        # A private mount namespace is optional; never mount in the user's host
        # namespace. Exercise the real same-device mount when the host allows it.
        if not shutil.which("unshare") or not shutil.which("mount"):
            self.skipTest("namespace/mount tools unavailable")
        probe = subprocess.run(["unshare", "-Urnm", "true"], capture_output=True)
        if probe.returncode:
            self.skipTest("unprivileged mount namespaces unavailable")
        source = self.file("Documents/source/keep")
        mounted = self.home / ".cache/app/mounted"
        mounted.mkdir(parents=True)
        code = '''import os, subprocess, sys
subprocess.run(["mount", "--bind", sys.argv[1], sys.argv[2]], check=True)
r = subprocess.run([sys.argv[3], "clean", "user-cache", "--yes"], env=os.environ, capture_output=True)
assert r.returncode != 0
assert os.path.isfile(sys.argv[1] + "/keep")
'''
        result = subprocess.run(["unshare", "-Urnm", "python3", "-c", code, str(source.parent), str(mounted), CLI], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(source.exists())

    def test_cache_filenames_round_trip_without_touching_decoy(self):
        cache = os.fsencode(self.home / ".cache")
        os.makedirs(cache)
        names = [b"pip\ttab", b"line\nbreak", b"trailing\n", b"back\\slash", b"carriage\rreturn", b"invalid-\xff"]
        for name in names:
            os.mkdir(cache + b"/" + name)
            with open(cache + b"/" + name + b"/data", "wb") as stream:
                stream.write(b"cache data")
        decoy = self.file(".cache/pip/keep")
        result = json.loads(self.cli("scan", "user-cache", "--json").stdout)
        items = result["modules"][0]["items"]
        self.assertEqual({base64.b64decode(item["pathBase64"]) for item in items}, {cache + b"/" + name for name in names})
        invalid = next(item for item in items if base64.b64decode(item["pathBase64"]).endswith(b"\xff"))
        self.assertFalse(invalid["pathIsUtf8"])
        plan = json.loads(self.cli("clean", "user-cache", "--dry-run", "--json").stdout)
        self.assertEqual(len(plan["modules"][0]["items"]), len(names))
        self.cli("clean", "user-cache", "--yes")
        self.assertTrue(decoy.exists())
        for name in names:
            self.assertEqual(os.listdir(cache + b"/" + name), [])

    def test_large_file_encoded_trash_uses_exact_identity(self):
        folder = os.fsencode(self.home / "Videos")
        os.makedirs(folder)
        names = [b"film\tpart\n", b"invalid-\xff"]
        self.save({"largeFileMinMB": 0})
        for name in names:
            with open(folder + b"/" + name, "wb") as stream:
                stream.write(b"valuable")
        decoy = self.file("Videos/film", b"")
        result = json.loads(self.cli("scan", "large-files", "--json").stdout)
        items = result["modules"][0]["items"]
        self.assertEqual(len(items), 2)
        for item in items:
            self.cli("trash", "--path-base64", item["pathBase64"], "--yes", "--json")
            self.assertFalse(os.path.lexists(base64.b64decode(item["pathBase64"])))
        self.assertTrue(decoy.exists())
        self.assertEqual(set(os.listdir(os.fsencode(self.home / ".local/share/Trash/files"))), set(names))

    def test_encoded_trash_rejects_nul_before_acting(self):
        victim = self.file("Documents/keep")
        encoded = base64.b64encode(os.fsencode(victim) + b"\0suffix").decode()
        self.assertNotEqual(self.cli("trash", "--path-base64", encoded, "--yes", ok=False).returncode, 0)
        self.assertTrue(victim.exists())

    def test_excluded_trash_payload_keeps_restore_metadata(self):
        keep = self.file(".local/share/Trash/files/keep/data")
        keep_info = self.file(".local/share/Trash/info/keep.trashinfo")
        gone = self.file(".local/share/Trash/files/gone")
        gone_info = self.file(".local/share/Trash/info/gone.trashinfo")
        self.save({"ignore": [str(keep)]})
        self.cli("clean", "trash", "--yes")
        self.assertTrue(keep.exists())
        self.assertTrue(keep_info.exists())
        self.assertFalse(gone.exists())
        self.assertFalse(gone_info.exists())

    def test_scan_paths_never_enter_external_command_arguments(self):
        secret = "PRIVATE_FILE_SENTINEL_7f53"
        self.file(f".cache/{secret}/data")
        self.file(f"Videos/{secret}")
        self.file(f".local/share/applications/{secret}.desktop", b"[Desktop Entry]\nExec=/nonexistent/program\n")
        self.save({"largeFileMinMB": 0})
        wrappers = self.root / "tools"
        wrappers.mkdir()
        log = self.root / "argv.jsonl"
        # Capture exec arguments deterministically instead of racing /proc.
        for tool in ("jq", "python3", "base64", "du", "find", "stat", "basename", "grep", "sort"):
            real = shutil.which(tool)
            wrapper = wrappers / tool
            wrapper.write_text("#!/usr/bin/python3\nimport json,os,sys\n"
                               f"with open({str(log)!r}, 'a') as log: log.write(json.dumps(sys.argv) + '\\n')\n"
                               f"os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n")
            wrapper.chmod(0o755)
        self.env["PATH"] = str(wrappers) + os.pathsep + self.env["PATH"]
        self.cli("scan", "--json", "--quiet")
        result = self.cli("scan", "user-cache", "large-files", "broken-entries", "--json", "--quiet")
        self.assertIn(secret.encode(), result.stdout)
        self.assertTrue(log.exists())
        self.assertNotIn(secret, log.read_text())

    def test_link_preserves_existing_dangling_symlink(self):
        target = self.home / ".local/bin/omacleaner"
        target.parent.mkdir(parents=True)
        target.symlink_to("/missing/other-cli")
        self.assertNotEqual(self.cli("link", ok=False).returncode, 0)
        self.assertEqual(os.readlink(target), "/missing/other-cli")

    def test_owned_link_is_idempotent(self):
        self.cli("link")
        self.cli("link")
        self.cli("unlink")
        self.assertFalse((self.home / ".local/bin/omacleaner").is_symlink())

    def test_schedule_verifies_ownership_before_systemctl(self):
        tools = self.root / "tools"
        tools.mkdir()
        log = self.root / "systemctl-called"
        systemctl = tools / "systemctl"
        systemctl.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n")
        systemctl.chmod(0o755)
        self.env["PATH"] = str(tools) + os.pathsep + self.env["PATH"]
        for variant in ("unowned", "missing", "symlink"):
            with self.subTest(variant=variant):
                unitdir = self.home / ".config/systemd/user"
                shutil.rmtree(unitdir, ignore_errors=True)
                unitdir.mkdir(parents=True)
                if variant == "unowned":
                    (unitdir / "omacleaner-scan.timer").write_text("[Timer]\nOnCalendar=daily\n")
                elif variant == "symlink":
                    (unitdir / "omacleaner-scan.timer").symlink_to(self.root / "missing")
                self.assertNotEqual(self.cli("schedule", "disable", ok=False).returncode, 0)
                self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
