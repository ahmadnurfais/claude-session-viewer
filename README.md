# Claude Session Viewer

Claude Session Viewer is a local, Docker-run web app for reviewing, managing, and backing up Claude Code session history stored under `~/.claude`.

## Why This Exists

Claude Code keeps useful local history, but the data is not easy to review directly. Sessions are stored as JSONL files, agent runs can live beside or under those sessions, and file-history snapshots are linked through JSON records instead of a single database. That means the conversation, tool use, tool results, subagents, and file versions are all connected, but not especially convenient to inspect by hand.

This project was built to make that history easier to use:

- Browse Claude Code sessions by project instead of raw files.
- Read conversations with user, assistant, tool use, and tool result blocks rendered together.
- Inspect agent and subagent JSONL files from the same UI.
- View file-history snapshots and diffs linked from a session.
- Rename or delete sessions through a UI with explicit confirmation.
- Browse backup copies without touching live Claude state.
- Run startup, scheduled, and manual backups to preserve local history.

The main goal is preservation. Even if Claude cleanup is disabled, local directories can still be removed accidentally or become harder to recover later. This viewer exists so those sessions, agent runs, and file versions can be reviewed and backed up as an independent copy of the local state.

## Runtime

Use Docker Compose. The app runs inside a container and does not require a local Python, venv, or conda environment for normal use.

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8888/
```

## Platform Notes

This project was built and used on Linux. The default `docker-compose.yaml` assumes a Linux host and Linux-style bind mounts.

Windows (WSL) and macOS can still run it through Docker, but you may need to adjust the mounted paths, file-sharing settings, timezone, and backup directory before starting the service.

## What It Reads

- `~/.claude/projects` is mounted read-write as `/claude/projects` so confirmed deletes and renames can update Claude session files.
- `~/.claude/file-history` is mounted as `/claude/file-history` so the viewer can show file snapshots and diffs linked from session JSONL records.
- Project session files, `agent-*.jsonl` files, subagent directories, and `file-history-snapshot` records drive the UI.

All important paths are configurable through Docker Compose environment variables and volume mounts:

- `BACKUP_DIR` controls where backups are written inside the container.
- `BACKUP_RETENTION_DAYS` controls automatic backup pruning. Set it to a number of days, or `0` to keep backups forever.
- `FILE_HISTORY_BASE` controls where the container reads Claude file-history snapshots.
- `AUTO_BACKUP_ENABLED` controls startup and midnight scheduled backups. Set it to `false` to disable automatic backups while keeping manual backups available.
- `TZ` controls the container timezone used for backup date folders and scheduled backup timing.

## Delete Behavior

The viewer can delete live sessions and agents after a confirmation prompt. Deleting a UUID session JSONL also removes its matching session subdirectory when present.

Backup sessions and backup dates can also be deleted from the selected backup copy. This does not affect live Claude state.

## Rename Behavior

Live Claude sessions can be renamed from the UI. Rename appends viewer-owned title records to the selected session JSONL so the display name stays stable in this app without rewriting the original conversation records.

## Backups

Backups are written to the configured backup directory. In the included Linux-oriented Compose file, the host backup directory is mounted into the container as:

```text
/backups
```

Backup folders are grouped by date and can include:

- `projects/` copies of selected or all Claude project session folders.
- `file-history/` copies of file-history directories linked to backed-up sessions.
- `backup-info.json` metadata for the backup trigger and copied domains.

The Backups view lets you choose a backup date, then a project, then a session or agent to inspect.
