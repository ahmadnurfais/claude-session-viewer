# Claude Session Viewer — Plan

---

## Current State (as of 2026-03-26)

### Working Features
- Browse all projects under `~/.claude/projects/`
- View sessions (UUID `.jsonl`) and agents (`agent-*.jsonl`) with human-readable content
- Delete sessions and agents (live and backup)
- Real-time session list updates via WebSocket + watchdog
- Content panel: jump to top/bottom, manual refresh button
- Tool use / tool result blocks rendered as collapsible sections
- Tool result messages rendered as `TOOL RESULT` (not `USER`)
- **Backup system**: daily midnight snapshot + startup trigger, 30-day retention
- Backup viewer: browse backed-up sessions/agents, read content, delete entries
- Manual backup trigger from UI (all projects or selective)

### Known Constraints
- No restore UI — backups are safety nets for manual recovery only
- No pre-delete auto-backup — deletes are final
- Content panel does NOT auto-refresh (intentional — prevents tool blocks from collapsing)
- APScheduler was replaced with a pure asyncio midnight loop (avoids GC issue)

### Tech Stack
- Backend: FastAPI + uvicorn + watchdog
- Frontend: vanilla JS SPA, no external CDN dependencies
- Container: Docker Compose, port **8888**
- Volumes:
  - `/home/ahmadnurfais/.claude/projects` → `/claude/projects:rw`
  - `/mnt/linux_data/backup/claude-backups` → `/backups:rw`

---

## Feature 1: File-History Integration (NOT YET IMPLEMENTED)

### What is file-history?
`~/.claude/file-history/` is Claude Code's automatic file snapshot system.
Every time Claude edits a file during a session, it saves a versioned copy here.

**Structure:**
```
~/.claude/file-history/
  <session-uuid>/
    <hash>@v1       ← full content of a file at first snapshot
    <hash>@v2       ← content after it was edited again
    <hash>@v3       ← ...
```

**How it links to sessions:**
Session JSONL files contain `file-history-snapshot` records with `trackedFileBackups`:
```json
{
  "type": "file-history-snapshot",
  "snapshot": {
    "trackedFileBackups": {
      "CLAUDE.md": {
        "backupFileName": "8b551af1a8b2cd07@v2",
        "version": 2,
        "backupTime": "2026-03-24T14:03:28Z"
      },
      "requirements.txt": {
        "backupFileName": "b51279f9bf10a2ff@v5",
        "version": 5
      }
    }
  }
}
```
`backupFileName` = the actual filename inside `file-history/<session-uuid>/`.

**Important:** Only sessions where Claude actually edited files have file-history entries.
Pure conversation sessions will have no entries.

---

### UI Design: "Files Changed" tab

In the content panel, add a tab switcher when viewing a session:

```
[ Conversation ]  [ Files Changed ]
```

**Conversation tab** — existing message viewer (unchanged).

**Files Changed tab** — shows every file Claude touched in that session:
- List of filenames with version count badge (e.g. `requirements.txt  ×5 versions`)
- Click a file → show version list (`v1`, `v2`, `v3`...)
- Click a version → show file content in a code viewer (syntax highlighted by extension)
- If 2+ versions exist, show a simple before/after diff (v(n-1) → v(n))

**Only show the tab** when the session has at least one `trackedFileBackups` entry.

---

### New Volume Mount Required
```yaml
- /home/ahmadnurfais/.claude/file-history:/claude/file-history:ro
```
Add `FILE_HISTORY_BASE=/claude/file-history` env var.

---

### New API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/sessions/{session_id}/files` | List all files tracked in a session with version counts |
| `GET` | `/api/sessions/{session_id}/files/{hash}/versions` | List all versions of a specific file |
| `GET` | `/api/file-history?path=...` | Return raw content of a file-history snapshot (path-based, like `/api/content`) |

The `_safe_path()` function needs updating to also allow paths under `FILE_HISTORY_BASE`.

---

### Backend Logic (`main.py`)

1. Add `FILE_HISTORY_BASE = Path(os.getenv("FILE_HISTORY_BASE", "/claude/file-history"))`
2. Update `_safe_path()` to allow `FILE_HISTORY_BASE`
3. Add helper `_get_session_files(session_id, projects_base)`:
   - Read the session JSONL
   - Collect all `file-history-snapshot` records
   - Build a map: `filename → [{ hash, version, backupTime }]` (sorted by version)
   - Return only the latest snapshot per file (latest `trackedFileBackups`)
4. Add the 3 new endpoints above
5. Works for both live and backup session paths — the `session_id` + `projects_base` combination determines which JSONL to read; file-history is always read from `FILE_HISTORY_BASE`

---

### Implementation Order (Feature 1)

1. `docker-compose.yaml` — add file-history volume + env var
2. `main.py` — `_safe_path` update + helper + 3 new endpoints
3. `index.html` — tab switcher UI + Files Changed panel
4. Rebuild and test

---

## Feature 2: File-History in Backup (NOT YET IMPLEMENTED)

### Why back up file-history?
File-history snapshots are the actual code files Claude wrote/edited. They are tightly
coupled to sessions — if a session is lost, its file-history entries become unrecoverable.
This fits the existing domain-based backup structure.

### Storage Structure Addition
```
/backups/
  2026-03-26/
    backup-info.json
    projects/                    ← already implemented
      ...
    file-history/                ← NEW domain
      <session-uuid>/
        <hash>@v1
        <hash>@v2
        ...
```

### Selective Backup Behaviour
| Backup scope | file-history scope |
|---|---|
| All projects | Copy entire `file-history/` dir |
| Specific projects | Copy only session UUIDs that belong to selected projects (extracted from their JSONL files) |

**How to find session UUIDs for a project:**
Read each `*.jsonl` file in the project dir whose stem is a UUID — those are the session IDs.
Match them against subdirectories in `file-history/`.

### `backup-info.json` update
```json
{
  "timestamp": "...",
  "trigger": "scheduled",
  "domains": {
    "projects": { "projects": ["all"], "status": "success", "count": 5 },
    "file-history": { "sessions": ["all"], "status": "success", "count": 27 }
  }
}
```

### Backup Viewer for File-History
When viewing a backed-up session's "Files Changed" tab, the file content is served from
`/backups/<date>/file-history/<session-uuid>/<hash>@v<n>` instead of the live path.
No additional UI work needed — the existing path-based content endpoint handles this.

### Changes Required (Feature 2)

**`main.py`:**
1. Update `_run_backup()` to also copy `FILE_HISTORY_BASE` into `backup_date_dir / "file-history"`
2. For selective backups: extract session UUIDs from target project JSONL files, copy only matching subdirs
3. Update `_safe_path()` to allow backup file-history paths too
4. Update `backup-info.json` writer to include `file-history` domain stats

**`docker-compose.yaml`:**
- File-history volume already added in Feature 1 (read-only is fine for backup source)

**`index.html`:**
- No UI changes needed — backup viewer reuses the same Files Changed tab
- The API returns file content from the correct path automatically

### Implementation Order (Feature 2)
1. Complete Feature 1 first (file-history viewer must work live before backing it up)
2. Update `_run_backup()` in `main.py`
3. Update `_safe_path()` for backup file-history paths
4. Rebuild and test

---

## Backup Storage Structure (Final/Complete)

```
/mnt/linux_data/backup/claude-backups/
  2026-03-26/
    backup-info.json
    projects/                         ← DONE
      -mnt-linux-data-workspace-rag/
        *.jsonl
        agent-*.jsonl
        <uuid>/subagents/...
    file-history/                     ← Feature 2
      <session-uuid>/
        <hash>@v1
        <hash>@v2
        ...
    settings/                         ← future
    memory/                           ← future
    keybindings/                      ← future
```

---

## Key Decisions

- **Plain directory copy** (not tar.gz) — viewer reads backup content directly, no extraction
- **No restore UI** — backups are safety nets for manual recovery only
- **Domain-namespaced storage** — extensible for future Claude data types
- **Startup trigger** — handles PC-off-at-midnight case
- **Pure asyncio midnight scheduler** — replaced APScheduler (was silently GC'd as local var)
- **Content panel no auto-refresh** — prevents open tool blocks from collapsing mid-read
- **file-history backed up per session UUID** — selective backup matches project → session → file-history
- **Feature 1 must precede Feature 2** — viewer integration before backup integration
