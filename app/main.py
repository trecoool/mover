#!/usr/bin/env python3
# Container entrypoint and CLI. `watch`: inotify on every enabled rule's source, each changed unit is
# delivered once two signatures one debounce apart match (and its marker exists). `run`: one rule, now.

import argparse
import fcntl
import logging
import logging.handlers
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import config
import scan
import transfer

log = logging.getLogger("mover")
CONFIG_DIR = "/config"
CONFIG_FILE = f"{CONFIG_DIR}/mover.toml"
DEFAULT_CONFIG = "/app/default-mover.toml"
LOG_FORMAT = logging.Formatter("%(asctime)s %(levelname).1s %(message)s", "%y%m%d-%H:%M:%S")


def env(name, default):
    return os.environ.get(name, default)


def setup(log_file):
    """Logging, default config, then drop to PUID:PGID so every written file has that owner."""
    os.umask(int(env("UMASK", "000"), 8))
    os.makedirs(f"{CONFIG_DIR}/logs", exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        shutil.copyfile(DEFAULT_CONFIG, CONFIG_FILE)
    uid, gid = int(env("PUID", "99")), int(env("PGID", "100"))
    if os.getuid() == 0:
        for root, dirs, files in os.walk(CONFIG_DIR):
            for name in [root] + [os.path.join(root, n) for n in dirs + files]:
                os.chown(name, uid, gid)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    log.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout),
                    logging.handlers.RotatingFileHandler(f"{CONFIG_DIR}/logs/{log_file}", maxBytes=5 << 20,
                                                         backupCount=5, encoding="utf-8")):
        handler.setFormatter(LOG_FORMAT)
        log.addHandler(handler)


@contextmanager
def delivery_lock():
    """One delivery at a time across the daemon and manual runs (they share temp folders and state)."""
    with open(f"{CONFIG_DIR}/.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def dry(rule):
    return rule.dry_run or env("ARMED", "0") != "1"


class Watcher:
    """inotifywait on all sources; queues the touched top-level entry, or None for a full rescan."""

    EVENTS = "create,close_write,moved_to,moved_from,delete,delete_self"

    def __init__(self, rules):
        self.rules = rules
        self.events = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            proc = subprocess.Popen(["inotifywait", "-m", "-r", "-q", "--format", "%e|%w%f", "-e", self.EVENTS,
                                     *[r.source for r in self.rules]],
                                    stdout=subprocess.PIPE, text=True, errors="replace")
            self.events.put(None)  # events may have been missed while (re)starting
            for line in proc.stdout:
                event, _, path = line.rstrip("\n").partition("|")
                self.route(event, path)
            log.error(f"inotifywait exited with {proc.wait()}, restarting in 60s")
            time.sleep(60)

    def route(self, event, path):
        if "Q_OVERFLOW" in event:
            log.warning("inotify queue overflow, rescanning")
            self.events.put(None)
            return
        for rule in self.rules:
            if path.startswith(rule.source + "/"):
                rel = os.path.relpath(path, rule.source)
                parts = rel.split("/")
                if not any(scan.hidden(rule, p) and p != rule.marker for p in parts):
                    self.events.put((rule.name, parts[0]))
                return


class Scheduler:
    """Pending top-level entries per rule, checked every debounce until all their units are stable."""

    def __init__(self, rules, memory):
        self.rules = {r.name: r for r in rules}
        self.memory = memory
        self.due = {}  # (rule, top) -> monotonic time of next check
        self.last_sig = {}  # (rule, unit rel) -> signature seen at the previous check
        self.handled = {}  # (rule, unit rel) -> signature already delivered or planned (conflicts, dry run)

    def touch(self, key):
        self.due[key] = time.monotonic() + self.rules[key[0]].debounce

    def rescan(self):
        for rule in self.rules.values():
            for top in scan.top_entries(rule):
                self.touch((rule.name, top))

    def check(self, key):
        """True if the entry must be checked again (some unit still changing)."""
        rule = self.rules[key[0]]
        known = self.memory.known(rule) if rule.mode == "copy" else None
        again = False
        for unit in scan.units_in(rule, key[1], known):
            ukey = (rule.name, unit.rel)
            if self.handled.get(ukey) == unit.files:
                continue
            if self.last_sig.get(ukey) != unit.files:
                self.last_sig[ukey] = unit.files
                again = True
            elif unit.ready:  # not ready: waits for the marker's own event
                with delivery_lock():
                    transfer.deliver(rule, unit, self.memory, dry(rule))
                self.handled[ukey] = unit.files
                self.last_sig.pop(ukey, None)
        return again

    def apply(self, item):
        self.rescan() if item is None else self.touch(item)

    def run(self, events):
        while True:
            now = time.monotonic()
            wait = min(self.due.values(), default=now + 3600) - now
            try:
                self.apply(events.get(timeout=max(0.0, wait)))
                while True:  # drain the burst, then check what is due
                    self.apply(events.get_nowait())
            except queue.Empty:
                pass
            for key in [k for k, t in self.due.items() if t <= time.monotonic()]:
                del self.due[key]
                try:
                    if self.check(key):
                        self.touch(key)
                except Exception as e:
                    log.error(f"[{key[0]}] {key[1]}: {type(e).__name__}: {e}")


def cmd_watch(rules, memory):
    active = [r for r in rules if r.enabled]
    for r in rules:
        state = "dry run" if dry(r) else "armed"
        log.info(f"[{r.name}] {r.source} -> {r.target}, {r.unit}/{r.mode}, "
                 f"{'marker ' + r.marker if r.marker else 'no marker'}, "
                 f"{state if r.enabled else 'disabled (manual run only)'}")
    with delivery_lock():
        for r in active:
            if not dry(r) and not r.direct:
                transfer.init_temp(r)
    if not active:
        log.info("No enabled rule, idling")
        threading.Event().wait()
    Scheduler(active, memory).run(Watcher(active).events)


def cmd_run(rule, memory, dry_run):
    """Delivers every ready unit now, no debounce. For disabled rules (e.g. confirm after review)."""
    known = memory.known(rule) if rule.mode == "copy" else None
    dry_run = dry_run or dry(rule)
    with delivery_lock():
        if not dry_run and not rule.direct:
            transfer.init_temp(rule)
        for top in scan.top_entries(rule):
            for unit in scan.units_in(rule, top, known):
                if not unit.ready:
                    log.info(f"[{rule.name}] waiting for marker: {unit.rel}")
                    continue
                try:
                    transfer.deliver(rule, unit, memory, dry_run)
                except Exception as e:
                    log.error(f"[{rule.name}] {unit.rel}: {type(e).__name__}: {e}")
                    return 1
    return 0


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 ignores SIGTERM without a handler
    p = argparse.ArgumentParser(prog="mover")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("watch", help="daemon (default)")
    sub.add_parser("check", help="validate config and show rules")
    run = sub.add_parser("run", help="deliver a rule's ready units now, enabled or not")
    run.add_argument("rule")
    run.add_argument("--dry-run", action="store_true")
    reset = sub.add_parser("reset", help="forget what a copy rule already copied")
    reset.add_argument("rule")
    args = p.parse_args()
    cmd = args.cmd or "watch"
    setup("mover.log" if cmd == "watch" else "manual.log")
    try:
        rules = config.load(CONFIG_FILE)
    except config.ConfigError as e:
        log.error(f"config: {e}")
        return 2
    memory = transfer.CopyMemory(f"{CONFIG_DIR}/copied.db")
    by_name = {r.name: r for r in rules}
    if cmd in ("run", "reset") and args.rule not in by_name:
        log.error(f"no rule named '{args.rule}'")
        return 2
    if cmd == "check":
        for r in rules:
            log.info(f"[{r.name}] OK: {r.unit}/{r.mode}, {'rename' if r.direct else 'via ' + r.temp}")
        return 0
    if cmd == "reset":
        log.info(f"[{args.rule}] forgot {memory.reset(args.rule)} copied files")
        return 0
    if cmd == "run":
        return cmd_run(by_name[args.rule], memory, args.dry_run)
    cmd_watch(rules, memory)


if __name__ == "__main__":
    sys.exit(main())
