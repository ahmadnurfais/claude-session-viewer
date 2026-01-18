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
