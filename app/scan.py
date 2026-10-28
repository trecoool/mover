# Finds the units of work in a source (single file, leaf folder or top-level entry) and their state:
# a signature (paths, sizes, mtimes) for the debounce, and readiness against end-of-download markers.

import fnmatch
import os
from dataclasses import dataclass

from config import TEMP_SENTINEL


@dataclass(frozen=True)
class Unit:
    rel: str  # relative to the source root
    files: tuple  # sorted (rel, size, mtime_ns); markers and excluded entries left out
    ready: bool  # every file has a marker in its folder or a parent folder (always true without marker)


def hidden(rule, name):
    """Entries the rule never sees: excluded names, markers, mover's own files."""
    return name == rule.marker or name == TEMP_SENTINEL or any(fnmatch.fnmatch(name, p) for p in rule.exclude)


def visible(rule, path):
    try:
        with os.scandir(path) as it:
            return [e for e in it if not hidden(rule, e.name)]
    except FileNotFoundError:
        return []


def walk_files(rule, rel):
    """(rel, size, mtime_ns) of every visible file under rel (or rel itself if a file)."""
    path = os.path.join(rule.source, rel)
    if not os.path.isdir(path) or os.path.islink(path):
        try:
            st = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return []
        return [(rel, st.st_size, st.st_mtime_ns)]
    out = []
    for e in visible(rule, path):
        out += walk_files(rule, os.path.join(rel, e.name))
    return out


def top_entries(rule):
    return sorted(e.name for e in visible(rule, rule.source))


class MarkerCheck:
    """A folder is covered when it or a parent below the source root holds the marker."""

    def __init__(self, rule):
        self.rule, self.cache = rule, {}

    def covered(self, rel_dir):
        if not self.rule.marker or rel_dir in ("", "."):
            return not self.rule.marker
        if rel_dir not in self.cache:
            here = os.path.exists(os.path.join(self.rule.source, rel_dir, self.rule.marker))
            self.cache[rel_dir] = here or self.covered(os.path.dirname(rel_dir))
        return self.cache[rel_dir]

    def ready(self, files):
        return all(self.covered(os.path.dirname(f[0])) for f in files)


def leaf_dirs(rule, rel):
    """Folders under rel holding files and no visible subfolder. Files beside subfolders wait."""
    entries = visible(rule, os.path.join(rule.source, rel))
    subdirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    if not subdirs:
        return [rel] if entries else []
    out = []
    for d in subdirs:
        out += leaf_dirs(rule, os.path.join(rel, d.name))
    return out


def units_in(rule, top, known=None):
    """Units inside one top-level entry. known(file) -> True drops files already copied (copy mode)."""
    path = os.path.join(rule.source, top)
    if rule.unit == "top" or not os.path.isdir(path) or os.path.islink(path):
        groups = [(top, walk_files(rule, top))]
    elif rule.unit == "leaf":
        groups = [(d, walk_files(rule, d)) for d in leaf_dirs(rule, top)]
    else:
        groups = [(f[0], [f]) for f in walk_files(rule, top)]
    markers = MarkerCheck(rule)
    units = []
    for rel, files in groups:
        files = sorted(f for f in files if not (known and known(f)))
        if files:
            units.append(Unit(rel, tuple(files), markers.ready(files)))
    return units
