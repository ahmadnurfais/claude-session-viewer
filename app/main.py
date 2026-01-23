import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Claude Session Viewer")

PROJECTS_BASE = Path("/claude/projects")
BACKUPS_BASE = Path(os.getenv("BACKUP_DIR", "/backups"))
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_backup_running = False


def is_uuid(value: str) -> bool:
    return bool(UUID_RE.match(value))


def read_jsonl(path: Path) -> List[dict]:
    records = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _build_sessions(project: Path) -> dict:
    sessions = []
    for path in sorted(project.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True):
        if not is_uuid(path.stem):
            continue
        records = read_jsonl(path)
        sessions.append({"id": path.stem, "title": path.stem, "record_count": len(records), "path": str(path)})
    return {"sessions": sessions, "orphaned_agents": []}


async def _run_backup(projects: List[str], trigger: str):
    global _backup_running
    if _backup_running:
        return
    _backup_running = True
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        backup_dir = BACKUPS_BASE / today / "projects"
        targets = [p for p in sorted(PROJECTS_BASE.iterdir()) if p.is_dir()]
        if "all" not in projects:
            targets = [p for p in targets if p.name in projects]
        for project in targets:
            dest = backup_dir / project.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(project, dest)
        info = {"timestamp": datetime.now().isoformat(), "trigger": trigger, "domains": {"projects": {"projects": [p.name for p in targets], "count": len(targets), "status": "success"}}}
        (BACKUPS_BASE / today / "backup-info.json").write_text(json.dumps(info, indent=2))
    finally:
        _backup_running = False


@app.on_event("startup")
async def _startup():
    BACKUPS_BASE.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(_run_backup(["all"], "startup"))


@app.get("/api/projects")
def list_projects():
    if not PROJECTS_BASE.exists():
        return []
    return [{"id": p.name, "label": p.name.lstrip("-").replace("-", "/"), "session_count": len(list(p.glob("*.jsonl")))} for p in sorted(PROJECTS_BASE.iterdir()) if p.is_dir()]


@app.get("/api/projects/{project_id}/sessions")
def list_sessions(project_id: str):
    project = PROJECTS_BASE / project_id
    if not project.exists():
        raise HTTPException(404, "Project not found")
    return _build_sessions(project)


@app.get("/api/backups")
def list_backups():
    if not BACKUPS_BASE.exists():
        return []
    return [{"date": p.name} for p in sorted(BACKUPS_BASE.iterdir(), reverse=True) if p.is_dir()]


@app.get("/api/content")
def get_content(path: str = Query(...)):
    return {"messages": read_jsonl(Path(path)), "path": path}


@app.get("/")
def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
