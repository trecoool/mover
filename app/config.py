# Loads and validates mover.toml: one rule per source -> target. Fails fast on bad or dangerous config.

import os
import tomllib
from dataclasses import dataclass

UNITS = ("file", "leaf", "top")
MODES = ("move", "copy")
TEMP_SENTINEL = ".mover_temp"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    name: str
    source: str
    target: str
    temp: str  # sibling of target, same filesystem: cross-disk deliveries land here first
    unit: str
    mode: str
    marker: str  # "" = no marker required
    exclude: tuple  # fnmatch patterns on entry names, any depth
    debounce: int
    enabled: bool
    dry_run: bool

    @property
    def same_disk(self):
        return os.stat(self.source).st_dev == os.stat(self.target).st_dev

    @property
    def direct(self):
        """Moves within one disk are renames; everything else goes through temp."""
        return self.mode == "move" and self.same_disk


def inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def overlap(a, b):
    return inside(a, b) or inside(b, a)


def parse_rule(data, default_debounce):
    name = data.get("name", "")
    where = f"rule '{name}'"
    if not name:
        raise ConfigError("every rule needs a name")
    for key in ("source", "target"):
        if not os.path.isabs(data.get(key, "")):
            raise ConfigError(f"{where}: {key} must be an absolute path")
    source = os.path.realpath(data["source"])
    target = os.path.realpath(data["target"])
    temp = os.path.realpath(data.get("temp", target.rstrip("/") + "_movertemp"))
    unit, mode = data.get("unit", "top"), data.get("mode", "move")
    if unit not in UNITS:
        raise ConfigError(f"{where}: unit must be one of {UNITS}")
    if mode not in MODES:
        raise ConfigError(f"{where}: mode must be one of {MODES}")
    for key, path in (("source", source), ("target", target), ("temp parent", os.path.dirname(temp))):
        if not os.path.isdir(path):
            raise ConfigError(f"{where}: {key} {path} is not a folder")
    if overlap(source, target):
        raise ConfigError(f"{where}: source and target overlap")
    if overlap(temp, source) or overlap(temp, target):
        raise ConfigError(f"{where}: temp {temp} overlaps source or target")
    if os.stat(os.path.dirname(temp)).st_dev != os.stat(target).st_dev:
        raise ConfigError(f"{where}: temp {temp} is not on the target's filesystem, deliveries could not be atomic")
    return Rule(name, source, target, temp, unit, mode, data.get("marker", ""),
                tuple(data.get("exclude", ())), int(data.get("debounce", default_debounce)),
                bool(data.get("enabled", True)), bool(data.get("dry_run", False)))


def load(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}")
    debounce = int(data.get("debounce", 60))
    rules = [parse_rule(r, debounce) for r in data.get("rule", [])]
    names = [r.name for r in rules]
    if len(set(names)) != len(names):
        raise ConfigError("rule names must be unique")
    return rules
