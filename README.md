# Proto-Sync

A self-hosted, FreeFileSync-style compare & sync web app, grown out of `media-backup.sh`.
Compare two folders, review every copy / move / delete in a side-by-side grid, change
anything you disagree with, then synchronize — or let a schedule, a file-change watcher,
a drive being plugged in, or a webhook do it for you. rsync does the transfers.

```
 ┌ Compare ┐ ⚙          ▽              ⚙ [Mirror|Update|Two way|Custom] [ Synchronize ]
 │ /mnt/media            ● ready ext4 ▬▬  →  ⇄  /mnt/media-archive1   ● ready ntfs ▬▬ │
 │ Left only 10  Right only 4  Left newer 2 …    → 12  ← 0  🗑 3  ↪ 1    [ Filter by path ] │
 │ Left                     Size  Modified │ Action │ Right                  Size  Modified │
 │ TV/Pluribus/ S01E01.mkv  1.7 MB …       │  •  →  │                                      │
 │                                         │  •  🗑  │ Movies/Old Rip (2009)/ old.avi       │
 │ TV/Severance/ S02E06 - Attila.mkv …     │  •  ↪  │                                      │
```

## Features

| Area | What you get |
|---|---|
| **Compare** | By time + size (with tolerance and DST hour-shift), by content, or by size. Categories: left/right only, left/right newer, different, conflict, identical. Folder pairs: as many per job as you like. |
| **Review grid** | Virtualized — handles hundreds of thousands of rows. Category and action chips, path search, multi-select (click / Shift / Ctrl / Ctrl+A), per-row action override that cascades into folders, *Exclude via filter*, bulk actions on everything shown. |
| **Sync variants** | Mirror →, Update →, Two way ⇄ (remembers the last sync to tell deletions from new files, with a conflict policy), Custom (pick the action per category). |
| **Moves & renames** | Detected by size + time + a sampled content hash, then applied as a rename instead of copy + delete. Different content with the same name/size is *not* treated as a move. |
| **Deletion handling** | Recycle (`.protosync-trash` on the same drive, auto-purged after N days), Versioning (separate folder; per-run folder, per-file time-stamp, or replace; min/max/age retention), or Permanent. |
| **Safety** | Sentinel files, mountpoint check, empty-source guard, max-delete % and count (override only after review), free-space check, per-path locks, mid-run drive watch with hang detection, re-stat before every delete, post-copy verification, retries. |
| **Automation** | Daily (pick days), every N minutes, cron, once, on startup, **real-time** (inotify or polling, debounced), **when the drive connects**, after another job (success / failure / any). Allowed-hours window, queue-if-running, jitter, dry-run-only triggers. |
| **Integration** | Per-job webhook URL with a rotatable token, pre/post shell hooks with `PROTOSYNC_*` env vars, notifications via ntfy, Discord, Gotify, generic JSON webhook, email. Full REST API + live server-sent events. |
| **History** | Every run with trigger, status, counts and a full log including the CREATED / UPDATED / MOVED / DELETED change trees from the original script. Retention is configurable. |

## Install

Whatever you choose, **bind-mount the drives at the same path inside as outside**
(`/mnt` → `/mnt`) so paths in logs and jobs match what you see on the host.

### Podman compose (quickest)

```fish
git clone <this repo> proto-sync; and cd proto-sync
cp .env.example .env          # edit PROTOSYNC_ROOTS, TZ, seed paths
podman compose up -d --build
```

Open `http://<host>:8475`.

### Podman Quadlet (systemd-native, recommended for a server)

```fish
podman build --format docker -t localhost/proto-sync:latest .
mkdir -p ~/.config/containers/systemd ~/.local/share/proto-sync
cp deploy/proto-sync.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start proto-sync
loginctl enable-linger $USER   # keep running while logged out
journalctl --user -u proto-sync -f
```

### Bare metal

See the header of `deploy/proto-sync.service`. It needs Python 3.11+ and rsync.

### Why the mount options matter

- **`rslave` propagation** on the `/mnt` bind. Without it, a USB or DAS drive mounted *after* the
  container started is invisible inside it — you would see an empty mountpoint, the sentinel
  check would (correctly) block the run, and the *when the drive connects* trigger would never fire.
- **`SecurityLabelDisable` / `label=disable`** on SELinux hosts (Bazzite, Fedora). The usual `:z`
  would relabel every file on your media drives.
- **No `keep-id`.** Under rootless Podman the container's root already *is* your user, so copied
  files stay yours. `keep-id` would map container root to a subordinate uid.

## First run

1. The first start creates **Entertainment Server → Archive**, the same pair and excludes as
   `media-backup.sh`. Its 03:00 trigger starts **disabled**.
2. Make sure both drives carry the sentinel — the same file the script used:
   ```fish
   touch /mnt/media/.mounted /mnt/media-archive1/.mounted
   ```
   Or click the red *no .mounted* badge next to a path. Only do that when the drive really is mounted.
3. Press **Compare** (F5), review, then **Synchronize** (F9). Tick *Dry run* first if you like.
4. When you're happy, open the job's **Schedule** tab, enable the daily trigger and remove the old cron entry.

## Coming from media-backup.sh

| media-backup.sh | Proto-Sync |
|---|---|
| `SRC` / `DST` | Left / right of the folder pair |
| rsync `--delete-after` | Mirror variant, deletion timing *After copying* (default) |
| `.mounted` sentinel checks | Safety → Sentinel file, required on both sides (default) |
| Empty-source guard | Safety → Empty source guard (default on) |
| `flock` lock file | Per-path locks: two jobs touching the same drive never overlap |
| `--no-perms --no-owner --no-group` | Automatic on NTFS / exFAT / FAT / CIFS (Sync → Permissions: *Auto*) |
| `--modify-window=2` | Compare → Time tolerance: 2 s |
| `EXCLUDES=(…)` | Filter → Exclude (*Reset to media-backup defaults* restores them) |
| `--dry-run` | *Dry run* checkbox, or a trigger set to *Dry run only* |
| `--quiet` for cron | Scheduled triggers; notifications instead of terminal output |
| `--verify` | Sync → Verify copies: *Copied files* or *Full* |
| progress2 live line | Run panel: %, bytes, items, rate, ETA, current file — pause / resume / stop |
| change tree in the log | Same trees in every run log (History → open a run) |
| 30-day log retention | Settings → Keep run history (30 days by default) |

**One deliberate difference:** the script deleted permanently, but new jobs default to
**Recycle**. Files removed from the archive go to `.protosync-trash/<time>` on the archive drive
for 14 days. That is your undo for a bad mirror. If you want the old behavior, set Sync →
*Delete permanently*. Recycling costs no copy time, since it is a rename on the same drive,
but it does use space until the purge.

## Safety model

A run that trips a guard is **blocked**: nothing changes, and the log and notification say why.

- **Sentinel / mountpoint** — refuses to run against an empty mountpoint (drive not attached).
- **Empty source** — refuses to mirror an empty left side onto a full right side.
- **Max deletions** — 50% by default. When a reviewed sync exceeds it, the confirm dialog offers an explicit override
  for that one run. A blocked manual run offers the same thing afterwards. Scheduled runs are never overridden.
- **Free space** — checks the target can hold what's about to be copied.
- **During the run** — watches that both drives stay mounted and responsive, and re-checks each
  file right before deleting it. If it changed since the comparison, it's skipped.
- **Afterwards** — size and time are verified for every copy (checksums optionally), then flushed to disk.

## Webhooks & API

Each job has a URL in *Edit job → Schedule*:

```fish
curl -fsS -X POST 'http://vega-cachy:8475/api/hooks/<job-id>?token=<token>'
curl -fsS -X POST 'http://vega-cachy:8475/api/hooks/<job-id>?token=<token>&dry_run=true'
```

These work even with basic auth enabled. *Rotate* invalidates the old URL. Everything else
(`/api/jobs`, `/api/runs`, `/api/events` …) is listed under Settings.

## Configuration

All settings are environment variables; see `.env.example`.

| Variable | Default | |
|---|---|---|
| `PROTOSYNC_ROOTS` | `/mnt,/media,/srv,/run/media` | Only these trees can be browsed or synced |
| `PROTOSYNC_DATA` | `/data` | Database, logs, locks |
| `PROTOSYNC_PORT` | `8475` | |
| `PROTOSYNC_USER` / `PROTOSYNC_PASSWORD` | – | Basic auth when both are set |
| `PROTOSYNC_MAX_RUNS` | `1` | Parallel runs; others queue |
| `PROTOSYNC_SEED_JOB` / `_LEFT` / `_RIGHT` | `1`, `/mnt/media`, `/mnt/media-archive1` | First-start job |
| `TZ` | `UTC` | Schedules fire in this zone |

Size units are chosen in **Settings**. Decimal is the default (1 GB = 1,000,000,000 bytes), the same as
FreeFileSync and drive labels. Binary is also available (1 GiB = 1,073,741,824 bytes). Only the display
changes, not the bytes counted. That's why 52.4 GB and 48.8 GiB are the same amount.

## Keyboard

F5 compare · F9 synchronize · `/` search · arrows, PgUp/PgDn, Home/End to move (Shift extends
the selection) · Ctrl+A / Esc select all / none · Alt+→ copy right · Alt+← copy left ·
Alt+0 do nothing · Alt+D default · Shift+F10 context menu.

## Troubleshooting

- **"folder is missing or not mounted" although it is mounted on the host.** The bind lacks
  `rslave`, or the drive was mounted under a path outside the bound directory.
- **Real-time trigger says the watch failed.** Large trees exceed the inotify limit. Raise it with
  `sysctl fs.inotify.max_user_watches=524288` (persist it in `/etc/sysctl.d/`), or tick
  *Poll instead of inotify*. Polling is also required on NFS, SMB and FUSE mounts.
- **Every file shows as different on an NTFS or exFAT drive.** Keep the time tolerance at 2 s. If the drive
  moved between time zones or DST, add `1` to *Ignore time shift*.
- **Fonts look plain.** The UI loads Space Grotesk and JetBrains Mono from Google Fonts in your
  browser. Offline it falls back to system fonts; nothing else depends on the internet.
- **Permission denied on copies.** Under rootless Podman the files must be writable by your user.
  Under Docker, see the `user:` note in `compose.yaml`.

## Layout

```
app/          FastAPI backend — compare, syncer (rsync), scheduler, runner, notify, API
static/       Vanilla ES-module frontend, no build step
Containerfile compose.yaml .env.example
deploy/       Podman Quadlet unit, bare-metal systemd unit
docs/         Screenshots of every screen
```
