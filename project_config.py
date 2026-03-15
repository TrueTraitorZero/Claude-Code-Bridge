"""Project configuration manager — reads/writes projects.json."""
import json
import os
import re
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "projects.json"

_cache = None


def _load_raw() -> list[dict]:
    if not CONFIG_PATH.exists():
        return []
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_projects() -> list[dict]:
    global _cache
    _cache = _load_raw()
    return _cache


def save_projects(projects: list[dict]):
    global _cache
    _cache = projects
    with open(CONFIG_PATH, "w") as f:
        json.dump(projects, f, indent=2, ensure_ascii=False)


def get_projects() -> list[dict]:
    if _cache is None:
        load_projects()
    return _cache


def get_project(pid: str) -> dict | None:
    for p in get_projects():
        if p["id"] == pid:
            return p
    return None


def get_projects_dict() -> dict:
    """Return {pid: {"name": ..., "workdir": ...}} for backward compat."""
    return {p["id"]: {"name": p["name"], "workdir": p["workdir"]} for p in get_projects()}


def validate_project(data: dict, existing_id: str = None):
    """Validate project data. Raises ValueError on error."""
    pid = data.get("id", "")
    if not existing_id and not re.match(r'^[a-z0-9][a-z0-9_-]*$', pid):
        raise ValueError(f"Invalid project id: '{pid}'. Use lowercase a-z, 0-9, dashes, underscores.")

    if not data.get("name", "").strip():
        raise ValueError("Project name is required")

    workdir = data.get("workdir", "").strip()
    if not workdir or not workdir.startswith("/"):
        raise ValueError("Workdir must be an absolute path")

    color = data.get("color", "")
    if not re.match(r'^#[0-9a-fA-F]{6}$', color):
        raise ValueError(f"Invalid color: '{color}'. Use hex format #RRGGBB.")

    # Check for duplicate id (only for new projects)
    if not existing_id:
        if get_project(pid):
            raise ValueError(f"Project '{pid}' already exists")


def add_project(data: dict) -> dict:
    validate_project(data)
    project = {
        "id": data["id"],
        "name": data["name"].strip(),
        "workdir": data["workdir"].strip(),
        "color": data["color"],
        "always_on": bool(data.get("always_on", False)),
    }
    # Create workdir if it doesn't exist
    os.makedirs(project["workdir"], exist_ok=True)
    projects = get_projects()
    projects.append(project)
    save_projects(projects)
    return project


def update_project(pid: str, data: dict) -> dict:
    validate_project(data, existing_id=pid)
    projects = get_projects()
    for i, p in enumerate(projects):
        if p["id"] == pid:
            p["name"] = data.get("name", p["name"]).strip()
            p["workdir"] = data.get("workdir", p["workdir"]).strip()
            p["color"] = data.get("color", p["color"])
            p["always_on"] = bool(data.get("always_on", p.get("always_on", False)))
            os.makedirs(p["workdir"], exist_ok=True)
            save_projects(projects)
            return p
    raise ValueError(f"Project '{pid}' not found")


def delete_project(pid: str):
    projects = get_projects()
    new_projects = [p for p in projects if p["id"] != pid]
    if len(new_projects) == len(projects):
        raise ValueError(f"Project '{pid}' not found")
    if len(new_projects) == 0:
        raise ValueError("Cannot delete the last project")
    save_projects(new_projects)
