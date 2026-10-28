# mover

Watches source folders and moves or copies finished downloads to a target. Generic: one rule per source → target.

## Behaviour

- **Unit of work** per rule: `top` (each top-level entry of the source, whole subtree), `leaf` (folders holding only files; a folder with subfolders waits until they are gone), `file` (each file, relative path kept). Loose files at the source root are always single units.
- **Debounce:** a unit is delivered when two signatures (paths, sizes, mtimes) taken `debounce` seconds apart match. inotify events (re)arm the check; no polling. Full rescan at startup and on inotify queue overflow.
- **Marker** (optional): a unit waits until every file has the marker (e.g. `.done`) in its folder or a parent folder below the source root. Markers are never delivered and are deleted on move.
- **Delivery:** move on one disk = rename. Otherwise copy into `temp` (sibling of the target, same disk, default `<target>_movertemp`), then rename into the target, then delete the source. Existing folders are merged: the first missing level is renamed in one go (a new album arrives whole inside an existing artist folder).
- **Never overwrites:** a file already in the target is a `CONFLICT`, logged, left at the source.
- **Copy mode** remembers delivered files (path + size + mtime, plus content hash: a touched or renamed file with the same content is not copied again). `mover reset <rule>` forgets.
- Emptied source folders are removed (including ones holding only the marker); excluded entries are never touched.

## Configuration

`/config/mover.toml`, example written on first start. Restart after changes.

```toml
debounce = 60

[[rule]]
name = "downloads"
source = "/mnt/cache/downloads/complete"
target = "/mnt/cache/inbox"
unit = "top"
mode = "move"
marker = ".done"
exclude = ["_incomplete"]

[[rule]]
name = "confirm"
source = "/mnt/cache/staging"
target = "/mnt/pool/library"
enabled = false     # manual: docker exec mover mover run confirm
```

Use real disk paths (`/mnt/cache/...`, `/mnt/<pool>/...`), mounted at the same path in the container. Not `/mnt/user`: inotify there misses writes made through disk paths, and its single device id hides cross-disk moves. Mount the target's parent so the temp sibling is reachable.

Startup refuses: overlapping source/target/temp, temp not on the target's disk, non-empty temp without the mover's `.mover_temp` sentinel.

## Commands

`docker exec mover mover <cmd>`

| Command | Effect |
|---|---|
| `check` | validate config, show each rule (rename or via temp) |
| `run <rule> [--dry-run]` | deliver every ready unit now, no debounce, enabled or not |
| `reset <rule>` | forget a copy rule's memory |

`ARMED=0` (default): every rule only logs `PLAN` lines. `ARMED=1`: rules act, except those with `dry_run = true`. Logs in `/config/logs` (`mover.log` daemon, `manual.log` commands).

## Install (Unraid)

Image `ghcr.io/trecoool/mover:latest`, built by CI on every push to `main`. Add the container from `unraid/mover.xml`; updates come through Unraid's Docker tab.
