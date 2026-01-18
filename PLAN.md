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
