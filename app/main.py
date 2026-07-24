import asyncio
import difflib
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(title="Claude Session Viewer")

PROJECTS_BASE = Path("/claude/projects")
BACKUPS_BASE = Path(os.getenv("BACKUP_DIR", "/backups"))
FILE_HISTORY_BASE = Path(os.getenv("FILE_HISTORY_BASE", "/claude/file-history"))
AUTO_BACKUP_ENABLED = os.getenv("AUTO_BACKUP_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def _parse_backup_retention_days(raw: str) -> Optional[int]:
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError("BACKUP_RETENTION_DAYS must be an integer") from exc
    if days < 0:
        raise ValueError("BACKUP_RETENTION_DAYS must be >= 0")
    return None if days == 0 else days


BACKUP_RETENTION_DAYS = _parse_backup_retention_days(os.getenv("BACKUP_RETENTION_DAYS", "30"))

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# ── WebSocket broadcast ───────────────────────────────────────────────────────

_connections: List[WebSocket] = []
_loop: Optional[asyncio.AbstractEventLoop] = None
_pending_broadcast: Optional[asyncio.TimerHandle] = None


async def _broadcast(data: dict):
    dead = []
    for ws in _connections:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.remove(ws)


async def _debounced_broadcast():
    global _pending_broadcast
    _pending_broadcast = None
    await _broadcast({"type": "refresh"})


class _FSHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        global _pending_broadcast
        if not _loop or _loop.is_closed():
            return
        if _pending_broadcast is not None:
            _pending_broadcast.cancel()
        _pending_broadcast = _loop.call_later(
            1.0,
            lambda: asyncio.run_coroutine_threadsafe(_debounced_broadcast(), _loop),
        )


@app.on_event("startup")
async def _startup():
    global _loop
    _loop = asyncio.get_running_loop()

    # Watch live projects dir
    if PROJECTS_BASE.exists():
        obs = Observer()
        obs.schedule(_FSHandler(), str(PROJECTS_BASE), recursive=True)
        obs.start()

    # Ensure backup dir exists
    BACKUPS_BASE.mkdir(parents=True, exist_ok=True)

    if AUTO_BACKUP_ENABLED:
        # Startup backup: run immediately if today has no backup yet
        today = datetime.now().strftime("%Y-%m-%d")
        today_dir = BACKUPS_BASE / today / "projects"
        if not today_dir.exists():
            logging.info("BACKUP: No backup found for today (%s) — running startup backup", today)
            asyncio.create_task(_run_backup(projects=["all"], trigger="startup"))
        else:
            logging.info("BACKUP: Today's backup already exists (%s), skipping startup backup", today)

        # Start the midnight scheduler as a long-lived asyncio task
        asyncio.create_task(_midnight_scheduler())
    else:
        logging.info("BACKUP: Automatic startup and scheduled backups are disabled")


@app.websocket("/ws")
async def _ws(ws: WebSocket):
    await ws.accept()
    _connections.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in _connections:
            _connections.remove(ws)


# ── Backup logic ──────────────────────────────────────────────────────────────

_backup_running = False


async def _midnight_scheduler():
    """Asyncio task that lives for the container lifetime and fires at every local midnight."""
    logging.info("BACKUP: Midnight scheduler started")
    while True:
        now = datetime.now()
        # Next midnight
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (tomorrow - now).total_seconds()
        logging.info(
            "BACKUP: Next scheduled backup in %.0f seconds (at %s)",
            wait_seconds,
            tomorrow.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await asyncio.sleep(wait_seconds)
        logging.info("BACKUP: Midnight reached — starting scheduled backup")
        await _run_backup(projects=["all"], trigger="scheduled")


async def _run_backup(projects: List[str], trigger: str):
    global _backup_running
    if _backup_running:
        return
    _backup_running = True

    today = datetime.now().strftime("%Y-%m-%d")
    backup_date_dir = BACKUPS_BASE / today
    projects_backup_dir = backup_date_dir / "projects"
    fh_backup_dir = backup_date_dir / "file-history"

    backed_up = []
    fh_sessions_backed_up = 0
    status = "success"

    try:
        if not PROJECTS_BASE.exists():
            logging.warning("BACKUP: Projects base dir not found: %s", PROJECTS_BASE)
            return

        all_projects = [p for p in sorted(PROJECTS_BASE.iterdir()) if p.is_dir()]
        targets = (
            all_projects
            if "all" in projects
            else [p for p in all_projects if p.name in projects]
        )

        logging.info("BACKUP: Starting %s backup — %d project(s) to copy", trigger, len(targets))
        for proj in targets:
            dest = projects_backup_dir / proj.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(str(proj), str(dest))
            backed_up.append(proj.name)
            logging.info("BACKUP: Copied %s", proj.name)

        # Back up file-history sessions that correspond to backed-up projects
        if FILE_HISTORY_BASE.exists():
            for proj in targets:
                for session_file in proj.glob("*.jsonl"):
                    session_id = session_file.stem
                    fh_src = FILE_HISTORY_BASE / session_id
                    if fh_src.is_dir():
                        fh_dest = fh_backup_dir / session_id
                        if fh_dest.exists():
                            shutil.rmtree(fh_dest)
                        shutil.copytree(str(fh_src), str(fh_dest))
                        fh_sessions_backed_up += 1
            logging.info("BACKUP: Copied file-history for %d session(s)", fh_sessions_backed_up)

        # Write / update backup-info.json
        info_path = backup_date_dir / "backup-info.json"
        existing_info = {}
        if info_path.exists():
            try:
                existing_info = json.loads(info_path.read_text())
            except Exception:
                pass

        existing_projects = existing_info.get("domains", {}).get("projects", {}).get("projects", [])
        all_projects_list = list(set(existing_projects + backed_up))

        info = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "domains": {
                "projects": {
                    "projects": all_projects_list,
                    "status": status,
                    "count": len(all_projects_list),
                },
                "file_history": {
                    "sessions": fh_sessions_backed_up,
                    "status": status,
                },
            },
        }
        info_path.write_text(json.dumps(info, indent=2))

        logging.info("BACKUP: Backup complete — %d project(s), %d file-history session(s) backed up to %s",
                     len(backed_up), fh_sessions_backed_up, today)

        # Prune old backups
        _prune_backups()

        # Notify UI
        await _broadcast({"type": "backup_done", "date": today, "trigger": trigger})

    except Exception as e:
        status = f"error: {e}"
        logging.error("BACKUP: Backup failed — %s", e, exc_info=True)
    finally:
        _backup_running = False


def _prune_backups():
    """Delete backup date-dirs older than BACKUP_RETENTION_DAYS."""
    if BACKUP_RETENTION_DAYS is None:
        logging.info("BACKUP: Retention disabled; skipping automatic pruning")
        return
    if not BACKUPS_BASE.exists():
        return
    cutoff = datetime.now().timestamp() - BACKUP_RETENTION_DAYS * 86400
    for item in BACKUPS_BASE.iterdir():
        if not item.is_dir():
            continue
        try:
            # Parse YYYY-MM-DD dir name
            dt = datetime.strptime(item.name, "%Y-%m-%d")
            if dt.timestamp() < cutoff:
                shutil.rmtree(item)
        except ValueError:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_uuid(s: str) -> bool:
    return bool(UUID_RE.match(s))


def read_jsonl(path: Path) -> List[dict]:
    records = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return records


def _first_user_title(records: List[dict]) -> str:
    # Custom title set via /rename takes priority — use the last one found
    custom_title = None
    for r in records:
        if r.get("type") == "custom-title" and r.get("customTitle"):
            custom_title = r["customTitle"]
    if custom_title:
        return custom_title[:120]

    for r in records:
        if r.get("type") == "user" and not r.get("isSidechain"):
            content = r.get("message", {}).get("content", "")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text = ""
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text = c.get("text", "").strip()
                        break
            else:
                text = ""
            text = re.sub(r"<[^>]+>", " ", text).strip()
            if text:
                return text[:120]
    return ""


def _agent_info(path: Path) -> dict:
    records = read_jsonl(path)
    session_id = None
    task = ""
    for r in records:
        if not session_id:
            session_id = r.get("sessionId")
        if r.get("type") == "user" and not task:
            content = r.get("message", {}).get("content", "")
            if isinstance(content, str):
                task = re.sub(r"<[^>]+>", " ", content).strip()[:120]
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        task = re.sub(r"<[^>]+>", " ", c.get("text", "")).strip()[:120]
                        break
        if session_id and task:
            break
    return {"session_id": session_id, "task": task or path.stem}


def _safe_path(path: Path) -> Path:
    resolved = path.resolve()
    for allowed_base in (PROJECTS_BASE.resolve(), BACKUPS_BASE.resolve(), FILE_HISTORY_BASE.resolve()):
        try:
            resolved.relative_to(allowed_base)
            return resolved
        except ValueError:
            pass
    raise HTTPException(403, "Access denied")


def _build_sessions(proj: Path) -> dict:
    """Shared logic for listing sessions — works for both live and backup dirs."""
    session_files = sorted(
        [f for f in proj.glob("*.jsonl") if is_uuid(f.stem)],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    session_ids = {f.stem for f in session_files}

    root_agents = list(proj.glob("agent-*.jsonl"))
    sid_to_agents: Dict[str, List[dict]] = {}
    orphaned_agents = []

    for af in root_agents:
        info = _agent_info(af)
        sid = info["session_id"]
        entry = {
            "id": af.stem,
            "name": af.name,
            "path": str(af),
            "task": info["task"],
            "location": "root",
            "agent_type": "",
            "description": "",
            "record_count": 0,
        }
        try:
            entry["record_count"] = sum(1 for _ in open(af, "r", errors="replace") if _.strip())
        except Exception:
            pass

        if sid and sid in session_ids:
            sid_to_agents.setdefault(sid, []).append(entry)
        else:
            entry["orphaned_session_id"] = sid
            orphaned_agents.append(entry)

    sessions = []
    for sf in session_files:
        sid = sf.stem
        records = read_jsonl(sf)
        user_msgs = [r for r in records if r.get("type") == "user" and not r.get("isSidechain")]
        title = _first_user_title(records) or sid

        subagent_dir = proj / sid / "subagents"
        subagents = []
        if subagent_dir.exists():
            for af in sorted(subagent_dir.glob("agent-*.jsonl")):
                meta = {}
                meta_path = af.parent / (af.stem + ".meta.json")
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                    except Exception:
                        pass
                info = _agent_info(af)
                rc = 0
                try:
                    rc = sum(1 for _ in open(af, "r", errors="replace") if _.strip())
                except Exception:
                    pass
                subagents.append({
                    "id": af.stem,
                    "name": af.name,
                    "path": str(af),
                    "task": info["task"],
                    "location": "subagents",
                    "agent_type": meta.get("agentType", ""),
                    "description": meta.get("description", ""),
                    "record_count": rc,
                })

        all_agents = sid_to_agents.get(sid, []) + subagents
        sessions.append({
            "id": sid,
            "title": title,
            "message_count": len(user_msgs),
            "record_count": len(records),
            "mtime": sf.stat().st_mtime,
            "size_bytes": sf.stat().st_size,
            "agents": all_agents,
            "path": str(sf),
        })

    return {"sessions": sessions, "orphaned_agents": orphaned_agents}


_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _extract_file_path(tool_name: str, inp: dict) -> str:
    """Extract the target file path from a tool_use input dict."""
    if not isinstance(inp, dict):
        return ""
    return inp.get("file_path") or inp.get("path") or ""


def _get_session_files(session_path: Path) -> dict:
    """Parse session JSONL and extract file-history snapshot metadata with context."""
    records = read_jsonl(session_path)
    session_id = session_path.stem

    files: Dict[str, Dict[int, dict]] = {}   # filename -> {version -> version_info}
    last_user_text: Optional[dict] = None     # {uuid, text} of last real user message
    # Maps absolute file_path -> {tool_name, user_snapshot} for most recent write/edit per file
    file_tool_map: Dict[str, dict] = {}

    for r in records:
        rtype = r.get("type")

        # Track the last user message that contains real human text (skip XML system injections)
        if rtype == "user":
            msg = r.get("message", {})
            content = msg.get("content", "")
            candidate = ""
            if isinstance(content, str):
                candidate = content.strip()
            elif isinstance(content, list):
                parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                candidate = " ".join(parts).strip()
            # Skip task notifications, context injections, and pure tool_result turns
            if candidate and not candidate.lstrip().startswith("<task-notification") \
                    and not candidate.lstrip().startswith("<context"):
                last_user_text = {
                    "uuid": r.get("uuid"),
                    "text": candidate[:200].replace("\n", " "),
                }

        # Track Write/Edit tool_use blocks so we can link them to file versions
        if rtype == "assistant":
            msg = r.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_name = block.get("name", "")
                    if tool_name not in _FILE_WRITE_TOOLS:
                        continue
                    fp = _extract_file_path(tool_name, block.get("input", {}))
                    if fp:
                        file_tool_map[fp] = {
                            "tool": tool_name,
                            "user": last_user_text,
                        }

        if rtype != "file-history-snapshot":
            continue

        tracked = r.get("snapshot", {}).get("trackedFileBackups", {})
        if not isinstance(tracked, dict):
            continue

        for filename, info in tracked.items():
            if not isinstance(info, dict):
                continue
            version = info.get("version", 1)
            backup_filename = info.get("backupFileName")  # None for v1 (no backup yet)
            backup_time = info.get("backupTime")

            if filename not in files:
                files[filename] = {}
            # Only record first occurrence of each version — that's when it was created
            if version not in files[filename]:
                # Match absolute tool file_path to relative snapshot filename
                # e.g. "/proj/src/foo.py" ends with "/src/foo.py" (snapshot key)
                tool_info = None
                suffix = "/" + filename
                for fp, tinfo in file_tool_map.items():
                    if fp == filename or fp.endswith(suffix):
                        tool_info = tinfo
                        break

                if tool_info:
                    triggered_by = {
                        "tool": tool_info["tool"],
                        "user": tool_info["user"],
                    }
                else:
                    triggered_by = {
                        "tool": None,
                        "user": last_user_text,
                    }

                files[filename][version] = {
                    "version": version,
                    "filename": backup_filename,
                    "backup_time": backup_time,
                    "has_content": backup_filename is not None,
                    "triggered_by": triggered_by,
                }

    result = []
    for fname in sorted(files):
        versions = sorted(files[fname].values(), key=lambda v: v["version"])
        result.append({
            "name": fname,
            "version_count": len(versions),
            "versions": versions,
        })

    return {
        "session_id": session_id,
        "files": result,
        "has_file_history": bool(result),
    }


# ── API: live projects ─────────────────────────────────────────────────────────

@app.get("/api/projects")
def list_projects():
    if not PROJECTS_BASE.exists():
        return []
    result = []
    for item in sorted(PROJECTS_BASE.iterdir()):
        if not item.is_dir():
            continue
        sessions = [f for f in item.glob("*.jsonl") if is_uuid(f.stem)]
        result.append({
            "id": item.name,
            "label": item.name.lstrip("-").replace("-", "/"),
            "session_count": len(sessions),
        })
    return result


@app.get("/api/projects/{project_id}/sessions")
def list_sessions(project_id: str):
    proj = PROJECTS_BASE / project_id
    if not proj.exists():
        raise HTTPException(404, "Project not found")
    return _build_sessions(proj)


# ── API: backups ───────────────────────────────────────────────────────────────

@app.get("/api/backups")
def list_backups():
    if not BACKUPS_BASE.exists():
        return []
    result = []
    for item in sorted(BACKUPS_BASE.iterdir(), reverse=True):
        if not item.is_dir():
            continue
        try:
            datetime.strptime(item.name, "%Y-%m-%d")
        except ValueError:
            continue

        info = {}
        info_path = item / "backup-info.json"
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text())
            except Exception:
                pass

        proj_dir = item / "projects"
        project_count = len([p for p in proj_dir.iterdir() if p.is_dir()]) if proj_dir.exists() else 0

        fh_dir = item / "file-history"
        fh_session_count = info.get("domains", {}).get("file_history", {}).get("sessions", 0)
        if not fh_session_count and fh_dir.exists():
            fh_session_count = len([d for d in fh_dir.iterdir() if d.is_dir()])

        result.append({
            "date": item.name,
            "timestamp": info.get("timestamp"),
            "trigger": info.get("trigger", "unknown"),
            "project_count": project_count,
            "fh_session_count": fh_session_count,
        })
    return result


@app.get("/api/backups/{date}/projects")
def list_backup_projects(date: str):
    proj_dir = BACKUPS_BASE / date / "projects"
    if not proj_dir.exists():
        raise HTTPException(404, "Backup not found")
    result = []
    for item in sorted(proj_dir.iterdir()):
        if not item.is_dir():
            continue
        sessions = [f for f in item.glob("*.jsonl") if is_uuid(f.stem)]
        result.append({
            "id": item.name,
            "label": item.name.lstrip("-").replace("-", "/"),
            "session_count": len(sessions),
        })
    return result


@app.get("/api/backups/{date}/projects/{project_id}/sessions")
def list_backup_sessions(date: str, project_id: str):
    proj = BACKUPS_BASE / date / "projects" / project_id
    if not proj.exists():
        raise HTTPException(404, "Project not found in backup")
    return _build_sessions(proj)


@app.delete("/api/backups/{date}")
def delete_backup(date: str):
    """Permanently delete an entire backup date directory."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Invalid date format")
    backup_dir = BACKUPS_BASE / date
    if not backup_dir.exists():
        raise HTTPException(404, "Backup not found")
    # Safety: must be directly inside BACKUPS_BASE
    if backup_dir.resolve().parent != BACKUPS_BASE.resolve():
        raise HTTPException(403, "Access denied")
    shutil.rmtree(str(backup_dir))
    logging.info("BACKUP DELETE: Removed backup %s", date)
    return {"deleted": date}


@app.post("/api/backups/run")
async def run_backup(body: dict):
    projects = body.get("projects", ["all"])
    if not isinstance(projects, list) or not projects:
        raise HTTPException(400, "projects must be a non-empty list")
    if _backup_running:
        raise HTTPException(409, "A backup is already running")
    asyncio.create_task(_run_backup(projects=projects, trigger="manual"))
    return {"status": "started"}


@app.get("/api/backups/status")
def backup_status():
    return {"running": _backup_running}


# ── API: session rename ────────────────────────────────────────────────────────

@app.post("/api/session/rename")
async def rename_session(body: dict):
    path = body.get("path", "").strip()
    title = body.get("title", "").strip()
    if not path:
        raise HTTPException(400, "path is required")
    if not title:
        raise HTTPException(400, "title cannot be empty")
    if len(title) > 200:
        raise HTTPException(400, "title too long (max 200 chars)")

    fp = _safe_path(Path(path))
    if not fp.exists():
        raise HTTPException(404, "Session not found")
    if not is_uuid(fp.stem):
        raise HTTPException(400, "Not a UUID session")

    session_id = fp.stem
    records = [
        {"type": "custom-title", "customTitle": title, "sessionId": session_id},
        {"type": "agent-name",   "agentName": title,   "sessionId": session_id},
    ]
    with open(fp, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    logging.info("RENAME: Session %s → %r", session_id, title)
    return {"status": "ok", "title": title}


# ── API: content ──────────────────────────────────────────────────────────────

def _parse_blocks(content) -> List[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    blocks = []
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type", "")
        if t == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif t == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "input": item.get("input", {}),
            })
        elif t == "tool_result":
            rc = item.get("content", "")
            if isinstance(rc, list):
                parts = [c.get("text", "") for c in rc if isinstance(c, dict) and c.get("type") == "text"]
                rc = "\n".join(parts)
            blocks.append({
                "type": "tool_result",
                "tool_use_id": item.get("tool_use_id", ""),
                "content": rc,
                "is_error": item.get("is_error", False),
            })
        elif t == "image":
            blocks.append({"type": "image"})
    return blocks


@app.get("/api/content")
def get_content(path: str = Query(...)):
    file_path = _safe_path(Path(path))
    records = read_jsonl(file_path)
    messages = []
    for r in records:
        if r.get("type") not in ("user", "assistant"):
            continue
        role = r.get("message", {}).get("role", r.get("type"))
        content = r.get("message", {}).get("content", "")
        blocks = _parse_blocks(content)
        if not blocks:
            continue
        messages.append({
            "uuid": r.get("uuid"),
            "role": role,
            "timestamp": r.get("timestamp"),
            "is_sidechain": r.get("isSidechain", False),
            "blocks": blocks,
        })
    return {"messages": messages, "path": str(file_path)}


# ── API: delete (live only) ───────────────────────────────────────────────────

@app.delete("/api/file")
def delete_file(path: str = Query(...)):
    file_path = _safe_path(Path(path))  # allows both live and backup paths
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    if is_uuid(file_path.stem):
        subdir = file_path.parent / file_path.stem
        if subdir.exists() and subdir.is_dir():
            shutil.rmtree(subdir)
    file_path.unlink()
    return {"status": "deleted", "path": str(file_path)}


# ── API: file-history ─────────────────────────────────────────────────────────

@app.get("/api/file-history/content")
def get_file_history_content(path: str = Query(...)):
    fp = _safe_path(Path(path))
    if not fp.exists():
        raise HTTPException(404, "Snapshot not found")
    try:
        return {"content": fp.read_text(encoding="utf-8", errors="replace")}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/file-history/diff")
def get_file_history_diff(path_b: str = Query(...), path_a: str = Query(default="")):
    """Return unified diff between two file-history snapshots. path_a optional (defaults to empty)."""
    fp_b = _safe_path(Path(path_b))
    if not fp_b.exists():
        raise HTTPException(404, "Snapshot not found")

    text_a = ""
    name_a = "(empty)"
    if path_a:
        fp_a = _safe_path(Path(path_a))
        if fp_a.exists():
            text_a = fp_a.read_text(encoding="utf-8", errors="replace")
            name_a = fp_a.name

    text_b = fp_b.read_text(encoding="utf-8", errors="replace")

    diff_lines = list(difflib.unified_diff(
        text_a.splitlines(keepends=True),
        text_b.splitlines(keepends=True),
        fromfile=name_a,
        tofile=fp_b.name,
        n=3,
    ))
    return {"diff": "".join(diff_lines), "unchanged": not bool(diff_lines)}


@app.get("/api/file-history")
def get_file_history(session_path: str = Query(...)):
    fp = _safe_path(Path(session_path))
    if not fp.exists():
        raise HTTPException(404, "Session not found")
    if not is_uuid(fp.stem):
        raise HTTPException(400, "Not a UUID session")
    return _get_session_files(fp)


@app.get("/api/file-history/orphans")
def get_file_history_orphans():
    """Return file-history session dirs that have no matching session JSONL in any live project."""
    if not FILE_HISTORY_BASE.exists():
        return {"orphans": [], "count": 0}

    # Collect all known session UUIDs from live projects
    known_sessions: set = set()
    if PROJECTS_BASE.exists():
        for proj in PROJECTS_BASE.iterdir():
            if proj.is_dir():
                for session_file in proj.glob("*.jsonl"):
                    known_sessions.add(session_file.stem)

    orphans = []
    for fh_dir in sorted(FILE_HISTORY_BASE.iterdir()):
        if not fh_dir.is_dir() or not is_uuid(fh_dir.name):
            continue
        if fh_dir.name in known_sessions:
            continue

        files = [f for f in fh_dir.iterdir() if f.is_file()]
        unique_bases = set()
        for f in files:
            name = f.name
            unique_bases.add(name[: name.rfind("@v")] if "@v" in name else name)

        total_size = sum(f.stat().st_size for f in files)
        mtimes = [f.stat().st_mtime for f in files]

        orphans.append({
            "session_id": fh_dir.name,
            "snapshot_count": len(files),
            "unique_file_count": len(unique_bases),
            "total_size_bytes": total_size,
            "last_modified": datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat() if mtimes else None,
        })

    return {"orphans": orphans, "count": len(orphans)}


@app.get("/api/file-history/orphans/{session_id}/files")
def get_orphan_session_files(session_id: str):
    """List snapshot files inside an orphaned file-history session, grouped by file."""
    if not is_uuid(session_id):
        raise HTTPException(400, "Not a valid session ID")
    fh_dir = _safe_path(FILE_HISTORY_BASE / session_id)
    if not fh_dir.exists():
        raise HTTPException(404, "Session not found")

    groups: Dict[str, list] = {}
    for f in fh_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name
        base = name[: name.rfind("@v")] if "@v" in name else name
        try:
            version = int(name[name.rfind("@v") + 2:]) if "@v" in name else 1
        except ValueError:
            version = 1
        stat = f.stat()
        groups.setdefault(base, []).append({
            "filename": name,
            "version": version,
            "size": stat.st_size,
            "path": str(fh_dir / name),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })

    files = []
    for base in sorted(groups):
        versions = sorted(groups[base], key=lambda v: v["version"])
        files.append({"base": base, "version_count": len(versions), "versions": versions})

    return {"session_id": session_id, "files": files}


@app.delete("/api/file-history/orphans/{session_id}")
def delete_orphan_session(session_id: str):
    """Permanently delete an orphaned file-history session directory."""
    if not is_uuid(session_id):
        raise HTTPException(400, "Not a valid session ID")

    # Re-verify it's actually orphaned before deleting
    known_sessions: set = set()
    if PROJECTS_BASE.exists():
        for proj in PROJECTS_BASE.iterdir():
            if proj.is_dir():
                for session_file in proj.glob("*.jsonl"):
                    known_sessions.add(session_file.stem)
    if session_id in known_sessions:
        raise HTTPException(400, "Session is not orphaned — refusing to delete")

    fh_dir = _safe_path(FILE_HISTORY_BASE / session_id)
    if not fh_dir.exists():
        raise HTTPException(404, "Session not found")

    shutil.rmtree(str(fh_dir))
    logging.info("ORPHAN DELETE: Removed file-history session %s", session_id)
    return {"deleted": session_id}


# ── Static ────────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("static/index.html")
