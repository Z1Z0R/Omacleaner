#!/usr/bin/env python3
"""User-file operations. Paths/policy arrive on stdin, never in argv.

Resolve ancestors using directory descriptors and O_NOFOLLOW. Recursive walks
stay on the selected directory's mount ID (including same-device bind mounts).
No recursive rm, pathname-based shutil walk, or privileged operations.
"""

import base64
import contextlib
import datetime
import fnmatch
import os
import stat
import sys
import urllib.parse


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def mount_id(fd):
    # st_dev alone cannot detect bind mounts on the same filesystem.
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
        for line in info:
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise OSError("cannot establish mount boundary")


@contextlib.contextmanager
def directory_at(parent, name, boundary=None):
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    try:
        if boundary is not None and mount_id(fd) != boundary:
            raise OSError("refusing to cross a mount boundary")
        yield fd
    finally:
        os.close(fd)


class Filesystem:
    def __init__(self, home, cache, data, config, ignored):
        if not all(os.path.isabs(p) for p in (home, cache, data, config)):
            raise ValueError("filesystem roots must be absolute")
        home, cache, data, config = map(os.path.normpath, (home, cache, data, config))
        self.home, self.data = home, data
        self.protected = {home, cache, data, home + "/.config", home + "/.local"}
        self.config = config
        self.ignored = ignored

    def is_ignored(self, path):
        # Match ancestors too, so a glob matching a directory protects its tree.
        while path != "/":
            if any(path == rule or fnmatch.fnmatchcase(path, rule) for rule in self.ignored):
                return True
            path = os.path.dirname(path)
        return "/" in self.ignored

    def anchor(self, path):
        if not path.startswith("/") or os.path.normpath(path) != path or "//" in path:
            raise ValueError("non-canonical path")
        if path in self.protected or path == self.config or path.startswith(self.config + "/"):
            raise ValueError("protected path")
        if self.is_ignored(path):
            raise ValueError("excluded path")
        if path.startswith(self.home + "/"):
            return self.home
        # Mounted-volume trash is the only allowed exception to HOME.
        parts = path.split("/")
        trash = f".Trash-{os.getuid()}"
        if len(parts) >= 5 and parts[1] == "media" and parts[3] == trash:
            return "/".join(parts[:4])
        if (len(parts) >= 7 and parts[1:3] == ["run", "media"]
                and parts[3] == os.environ.get("USER") and parts[5] == trash):
            return "/".join(parts[:6])
        raise ValueError("path outside allowed roots")

    @contextlib.contextmanager
    def parent(self, path):
        anchor = self.anchor(path)
        # Resolve even the anchor without symlinks. Once inside it, ancestors
        # must remain on its mount; mounted descendants are never selected.
        with contextlib.ExitStack() as stack:
            fd = stack.enter_context(directory_at(None, "/"))
            current, boundary = "", None
            for part in os.path.dirname(path).split("/")[1:]:
                current += "/" + part
                fd = stack.enter_context(directory_at(fd, part, boundary))
                if current == anchor:
                    boundary = mount_id(fd)
            if boundary is None:
                raise ValueError("missing allowed root")
            yield fd, os.path.basename(path), boundary

    def size_at(self, parent, name, path, boundary):
        if self.is_ignored(path):
            return 0
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            return st.st_size
        try:
            with directory_at(parent, name, boundary) as fd:
                return st.st_size + sum(self.size_at(fd, child, path + "/" + child, boundary)
                                        for child in os.listdir(fd))
        except OSError:
            return 0

    def size(self, path):
        with self.parent(path) as (parent, name, boundary):
            return self.size_at(parent, name, path, boundary)

    def remove_at(self, parent, name, path, boundary, keep=False):
        if self.is_ignored(path):
            return False
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            with directory_at(parent, name, boundary) as fd:
                complete = True
                for child in os.listdir(fd):
                    removed = self.remove_at(fd, child, path + "/" + child, boundary)
                    complete = removed and complete
                    if removed and (path.endswith("/Trash/files") or path.endswith(f"/.Trash-{os.getuid()}/files")):
                        self.remove_trash_info(path, child)
                if keep or not complete:
                    return False
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)
        return True

    def remove_trash_info(self, files, name):
        # Only remove metadata after its exact payload is completely gone.
        info = os.path.dirname(files) + "/info/" + name + ".trashinfo"
        try:
            with self.parent(info) as (parent, leaf, _):
                st = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode):
                    os.unlink(leaf, dir_fd=parent)
        except (FileNotFoundError, ValueError):
            pass

    def inspect_at(self, parent, name, path, boundary):
        if self.is_ignored(path):
            raise ValueError("folder contains an excluded path")
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            with directory_at(parent, name, boundary) as fd:
                for child in os.listdir(fd):
                    self.inspect_at(fd, child, path + "/" + child, boundary)

    def trash(self, path):
        with self.parent(path) as (src, name, boundary):
            self.inspect_at(src, name, path, boundary)
            trash = self.data + "/Trash"
            if not trash.startswith(self.home + "/"):
                raise ValueError("Trash must be inside HOME")
            self.anchor(trash + "/files/.omacleaner-new")
            if path == trash or path.startswith(trash + "/"):
                raise ValueError("already inside Trash")
            # Create the destination through descriptors as well. Never follow
            # a redirected Trash/files or Trash/info directory.
            with contextlib.ExitStack() as stack:
                fd = stack.enter_context(directory_at(None, "/"))
                current, dst_boundary = "", None
                for part in trash.split("/")[1:]:
                    current += "/" + part
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    fd = stack.enter_context(directory_at(fd, part, dst_boundary))
                    if current == self.home:
                        dst_boundary = mount_id(fd)
                if not trash.startswith(self.home + "/") or dst_boundary is None:
                    raise ValueError("Trash must be inside HOME")
                for part in ("files", "info"):
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                files = stack.enter_context(directory_at(fd, "files", dst_boundary))
                info = stack.enter_context(directory_at(fd, "info", dst_boundary))
                if os.fstat(src).st_dev != os.fstat(files).st_dev:
                    raise ValueError("cross-filesystem Trash move refused")
                candidate, suffix = name, 0
                while True:
                    try:
                        os.stat(candidate, dir_fd=files, follow_symlinks=False)
                    except FileNotFoundError:
                        try:
                            meta = os.open(candidate + ".trashinfo", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                           0o600, dir_fd=info)
                            break
                        except FileExistsError:
                            pass
                    suffix += 1
                    candidate = f"{name}.{suffix}"
                try:
                    with os.fdopen(meta, "w", encoding="ascii") as stream:
                        encoded = urllib.parse.quote_from_bytes(os.fsencode(path), safe="/")
                        stream.write(f"[Trash Info]\nPath={encoded}\nDeletionDate={datetime.datetime.now():%Y-%m-%dT%H:%M:%S}\n")
                    os.rename(name, candidate, src_dir_fd=src, dst_dir_fd=files)
                except BaseException:
                    os.unlink(candidate + ".trashinfo", dir_fd=info)
                    raise

    def large_files(self, minimum):
        def walk(fd, path, boundary):
            for name in os.listdir(fd):
                full = path + "/" + name
                if name in ("node_modules", ".git") or (path == self.home and name.startswith(".")) or self.is_ignored(full):
                    continue
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        with directory_at(fd, name, boundary) as child:
                            walk(child, full, boundary)
                    elif stat.S_ISREG(st.st_mode) and st.st_size > minimum:
                        label = full[len(self.home) + 1:] + f" (modified {datetime.datetime.fromtimestamp(st.st_mtime):%Y-%m-%d})"
                        print(f"{st.st_size}\t{base64.b64encode(os.fsencode(full)).decode()}\t{base64.b64encode(os.fsencode(label)).decode()}")
                except OSError:
                    continue
        with contextlib.ExitStack() as stack:
            fd = stack.enter_context(directory_at(None, "/"))
            for part in self.home.split("/")[1:]:
                fd = stack.enter_context(directory_at(fd, part))
            walk(fd, self.home, mount_id(fd))


def main():
    fields = sys.stdin.buffer.read().split(b"\0")
    if fields[-1] != b"" or len(fields) < 6:
        raise ValueError("invalid filesystem request")
    path, home, cache, data, config, *ignored = map(os.fsdecode, fields[:-1])
    action = sys.argv[1]
    if action == "config":
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if os.path.lexists(path):
                raise ValueError("dangling configuration link")
            print("{}")
            return
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("configuration is not a regular file")
            contents = stream.read()
            contents.decode("utf-8", errors="strict")
            if b"\0" in contents:
                raise ValueError("configuration contains a literal NUL")
            sys.stdout.buffer.write(contents)
        return
    fs = Filesystem(home, cache, data, config, ignored)
    if action == "size":
        print(fs.size(path))
    elif action == "list":
        with fs.parent(path + "/.omacleaner-list") as (parent, _, _):
            for name in os.listdir(parent):
                sys.stdout.buffer.write(os.fsencode(path + "/" + name) + b"\0")
    elif action == "large":
        fs.large_files(int(path) * 1048576)
    elif action == "trash":
        fs.trash(path)
    elif action in ("empty", "delete"):
        with fs.parent(path) as (parent, name, boundary):
            fs.remove_at(parent, name, path, boundary, keep=action == "empty")
    else:
        raise ValueError("unknown filesystem action")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        # Do not put personal paths in subprocess arguments or diagnostics.
        print(f"filesystem operation refused ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
