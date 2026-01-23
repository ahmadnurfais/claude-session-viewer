import json
import re
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Claude Session Viewer")

PROJECTS_BASE = Path("/claude/projects")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


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


def _first_user_title(records: List[dict]) -> str:
    for record in records:
        if record.get("type") != "user" or record.get("isSidechain"):
            continue
        content = record.get("message", {}).get("content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()[:120]
    return ""


@app.get("/api/projects")
def list_projects():
    if not PROJECTS_BASE.exists():
        return []
    result = []
    for item in sorted(PROJECTS_BASE.iterdir()):
        if not item.is_dir():
            continue
        sessions = [f for f in item.glob("*.jsonl") if is_uuid(f.stem)]
        result.append({"id": item.name, "label": item.name.lstrip("-").replace("-", "/"), "session_count": len(sessions)})
    return result


@app.get("/api/projects/{project_id}/sessions")
def list_sessions(project_id: str):
    project = PROJECTS_BASE / project_id
    if not project.exists():
        raise HTTPException(404, "Project not found")
    sessions = []
    for path in sorted(project.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True):
        if not is_uuid(path.stem):
            continue
        records = read_jsonl(path)
        sessions.append({
            "id": path.stem,
            "title": _first_user_title(records) or path.stem,
            "record_count": len(records),
            "mtime": path.stat().st_mtime,
            "path": str(path),
        })
    return {"sessions": sessions, "orphaned_agents": []}


@app.get("/api/content")
def get_content(path: str = Query(...)):
    file_path = Path(path)
    if PROJECTS_BASE.resolve() not in file_path.resolve().parents:
        raise HTTPException(403, "Access denied")
    return {"messages": read_jsonl(file_path), "path": str(file_path)}


@app.get("/")
def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
