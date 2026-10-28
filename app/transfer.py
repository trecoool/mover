# Delivers a unit: one rename when source and target share a disk, else a copy into the temp sibling
# then a rename into the target. Merges into existing folders, never overwrites: conflicts stay behind.

import errno
import hashlib
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field

import scan
from config import TEMP_SENTINEL

log = logging.getLogger("mover")


@dataclass
class Result:
    placed: list = field(default_factory=list)  # rel paths now in the target
    conflicts: list = field(default_factory=list)  # rel paths whose target already exists


def is_dir(path):
    return os.path.isdir(path) and not os.path.islink(path)


def all_files(path, rel):
    if not is_dir(path):
        return [rel]
    out = []
    for root, _, names in os.walk(path):
        sub = os.path.relpath(root, path)
        out += [os.path.normpath(os.path.join(rel, sub, n)) for n in names]
    return out


def has_hidden(rule, path):
    return any(scan.hidden(rule, n) for _, dirs, names in os.walk(path) for n in dirs + names)


def place(rule, src, dst, rel, res):
    """Renames src to dst, or merges level by level when dst exists. Never replaces a file."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if is_dir(src):
        if not has_hidden(rule, src):
            files = all_files(src, rel)
            try:
                os.rename(src, dst)  # whole folder arrives at once
                res.placed += files
                return
            except OSError as e:
                if e.errno not in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR):
                    raise
        if os.path.lexists(dst) and not is_dir(dst):
            res.conflicts += all_files(src, rel)
            return
        os.makedirs(dst, exist_ok=True)
        for e in scan.visible(rule, src):
            place(rule, e.path, os.path.join(dst, e.name), os.path.join(rel, e.name), res)
        try:
            os.rmdir(src)
        except OSError:
            pass
        return
    try:
        os.link(src, dst, follow_symlinks=False)  # fails if dst exists: no overwrite race
    except FileExistsError:
        res.conflicts.append(rel)
        return
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EOPNOTSUPP):
            raise
        if os.path.lexists(dst):  # filesystem without hard links
            res.conflicts.append(rel)
            return
        os.rename(src, dst)
        res.placed.append(rel)
        return
    os.unlink(src)
    res.placed.append(rel)


def delete_markers(rule, path):
    if not rule.marker or not is_dir(path):
        return
    for root, _, names in os.walk(path):
        if rule.marker in names:
            os.remove(os.path.join(root, rule.marker))


def remove_empty(path, stop, marker=""):
    """Removes empty folders (or holding only the marker) under and above path, up to stop excluded."""
    if is_dir(path):
        for root, _, _ in os.walk(path, topdown=False):
            remove_if_empty(root, marker)
    path = os.path.dirname(path)
    while path.startswith(stop.rstrip("/") + "/") and remove_if_empty(path, marker):
        path = os.path.dirname(path)


def remove_if_empty(path, marker):
    try:
        names = os.listdir(path)
        if any(n != marker or not marker for n in names):
            return False
        for n in names:
            os.remove(os.path.join(path, n))
        os.rmdir(path)
        return True
    except OSError:
        return False


def init_temp(rule):
    """Temp folder is the mover's: marked by a sentinel, and only then purged (leftovers of a crash)."""
    os.makedirs(rule.temp, exist_ok=True)
    sentinel = os.path.join(rule.temp, TEMP_SENTINEL)
    if not os.path.exists(sentinel):
        if os.listdir(rule.temp):
            raise RuntimeError(f"temp {rule.temp} is not empty and not the mover's, refusing to use it")
        open(sentinel, "w").close()
    for name in os.listdir(rule.temp):
        if name != TEMP_SENTINEL:
            path = os.path.join(rule.temp, name)
            log.info(f"[{rule.name}] purging temp leftover {path}")
            shutil.rmtree(path) if is_dir(path) else os.remove(path)


class CopyMemory:
    """Files a copy rule already delivered, by path+size+mtime, and by content hash."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS copied (rule TEXT, rel TEXT, size INT, mtime_ns INT, "
                        "sha256 TEXT, PRIMARY KEY (rule, rel, size, mtime_ns))")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_hash ON copied (rule, sha256)")

    def known(self, rule):
        def check(f):
            return self.db.execute("SELECT 1 FROM copied WHERE rule=? AND rel=? AND size=? AND mtime_ns=?",
                                   (rule.name, *f)).fetchone() is not None
        return check

    def new_content(self, rule, files):
        """Hashes files; same content already copied (renamed or touched file) is recorded and dropped."""
        keep = {}
        for f in files:
            h = hashlib.sha256()
            with open(os.path.join(rule.source, f[0]), "rb") as fh:
                while chunk := fh.read(1 << 20):
                    h.update(chunk)
            digest = h.hexdigest()
            if self.db.execute("SELECT 1 FROM copied WHERE rule=? AND sha256=?", (rule.name, digest)).fetchone():
                log.info(f"[{rule.name}] already copied (same content): {f[0]}")
                self.record(rule, [f], digest)
            else:
                keep[f] = digest
        return keep

    def record(self, rule, files, digest):
        self.db.executemany("INSERT OR REPLACE INTO copied VALUES (?, ?, ?, ?, ?)",
                            [(rule.name, *f, digest) for f in files])
        self.db.commit()

    def reset(self, rule_name):
        n = self.db.execute("DELETE FROM copied WHERE rule=?", (rule_name,)).rowcount
        self.db.commit()
        return n


def deliver(rule, unit, memory, dry_run):
    """Moves or copies one unit into the target. Returns a Result (empty in dry run)."""
    src = os.path.join(rule.source, unit.rel)
    dst = os.path.join(rule.target, unit.rel)
    direct = rule.direct
    how = "rename" if direct else f"via {rule.temp}"
    if dry_run:
        log.info(f"[{rule.name}] PLAN {rule.mode} {unit.rel} ({len(unit.files)} files) -> {dst} ({how})")
        return Result()
    res = Result()
    if direct:
        delete_markers(rule, src)
        place(rule, src, dst, unit.rel, res)
    else:
        files = {f: None for f in unit.files}
        if rule.mode == "copy":
            files = memory.new_content(rule, unit.files)
        staged = os.path.join(rule.temp, unit.rel)
        for f in files:
            tmp = os.path.join(rule.temp, f[0])
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            shutil.copy2(os.path.join(rule.source, f[0]), tmp)
        if files:
            place(rule, staged, dst, unit.rel, res)
        if os.path.lexists(staged):  # conflicting copies
            shutil.rmtree(staged) if is_dir(staged) else os.remove(staged)
        remove_empty(staged, rule.temp)
        placed = set(res.placed)
        if rule.mode == "move":
            for rel in placed:
                os.remove(os.path.join(rule.source, rel))
            delete_markers(rule, src)
        else:
            for f, digest in files.items():
                if f[0] in placed:
                    memory.record(rule, [f], digest)
    if rule.mode == "move":
        remove_empty(src, rule.source, rule.marker)
    if not res.placed and not res.conflicts:
        return res
    log.info(f"[{rule.name}] {rule.mode} {unit.rel}: {len(res.placed)} files -> {dst} ({how})")
    for rel in res.conflicts:
        log.warning(f"[{rule.name}] CONFLICT {rel}: already in target, left at source")
    return res
