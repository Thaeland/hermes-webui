"""Read-only bridge from the Hermes Agent Projects store (projects.db).

Implements the read-path slice of issue #5763: the WebUI workspace list
surfaces the profile's authoritative ``projects.db`` (the same SQLite store
backing Hermes Desktop / CLI ``hermes project list``) instead of requiring a
manually duplicated picker list.

Design (per the maintainer's recommended first slice):
- In-process read only. No writes ever touch ``projects.db``; the WebUI keeps
  owning ``workspaces.json`` for its own additions.
- Fail-safe by contract: a missing DB, missing tables, lock contention, or any
  other error yields an empty list — the workspace picker then behaves exactly
  as it did before this bridge existed (``workspaces.json`` fallback).
- Results are cached per DB path and invalidated on file mtime change, so a
  project created via Desktop/CLI appears on the next ``/api/workspaces``
  poll without a WebUI restart.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# path -> (db_mtime, [(path, name), ...])
_cache: dict[str, tuple[float | None, list[tuple[str, str]]]] = {}
_cache_lock = threading.Lock()

# Env kill-switch: set HERMES_WEBUI_PROJECTS_DB_SYNC=0 to disable the bridge.
_DISABLE_ENV = "HERMES_WEBUI_PROJECTS_DB_SYNC"


def _sync_enabled() -> bool:
    return os.environ.get(_DISABLE_ENV, "1").strip().lower() not in ("0", "false", "off", "no")


def _projects_db_path(profile_home: Path | None) -> Path | None:
    """Resolve ``projects.db`` for a profile home (None = ambient active profile)."""
    try:
        if profile_home is not None:
            home = Path(profile_home)
        else:
            from api.profiles import get_active_hermes_home
            home = get_active_hermes_home()
    except Exception:
        return None
    db = home / "projects.db"
    return db if db.is_file() else None


def _query_projects(db: Path) -> list[tuple[str, str]]:
    """Return (path, name) for each non-archived project with a usable folder.

    A project's workspace path is its primary folder when set, otherwise any
    attached folder (lowest ``added_at`` for determinism). Projects with no
    folders at all are skipped: a workspace entry needs a real directory.
    """
    # uri=ro + busy_timeout: never create a DB, never block on a writer for long.
    uri = f"file:{db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT p.name AS name,
                   COALESCE(
                       (SELECT pf.path FROM project_folders pf
                         WHERE pf.project_id = p.id AND pf.is_primary = 1
                         LIMIT 1),
                       (SELECT pf.path FROM project_folders pf
                         WHERE pf.project_id = p.id
                         ORDER BY pf.added_at ASC LIMIT 1)
                   ) AS path
            FROM projects p
            WHERE COALESCE(p.archived, 0) = 0
            ORDER BY p.created_at ASC
            """
        ).fetchall()
    except sqlite3.Error:
        # Missing tables / older schema — treat as "no opinion".
        return []
    finally:
        conn.close()

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        path = (r["path"] or "").strip()
        name = (r["name"] or "").strip()
        if not path or not name or path in seen:
            continue
        seen.add(path)
        out.append((path, name))
    return out


def load_hermes_project_workspaces(profile_home: Path | None = None) -> list[dict]:
    """Return workspace-shaped entries ``{'path','name','source'}`` from projects.db.

    Never raises: any failure returns ``[]`` so callers fall back to the
    WebUI-local workspace list unchanged.
    """
    if not _sync_enabled():
        return []
    try:
        db = _projects_db_path(profile_home)
        if db is None:
            return []
        key = str(db)
        try:
            mtime = db.stat().st_mtime
        except OSError:
            return []
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None and cached[0] == mtime:
                entries = cached[1]
            else:
                entries = _query_projects(db)
                _cache[key] = (mtime, entries)
        return [{"path": p, "name": n, "source": "hermes_project"} for p, n in entries]
    except Exception:
        logger.debug("projects.db bridge failed; falling back to local workspaces", exc_info=True)
        return []


def merge_hermes_projects(workspaces: list[dict], profile_home: Path | None = None) -> list[dict]:
    """Merge projects.db entries into a WebUI workspace list.

    - projects.db is authoritative for a path it owns: a local entry with the
      same path takes the project's display name.
    - Local-only entries keep their order and names (WebUI additions still work).
    - DB projects not present locally are appended.
    Never mutates the input list.
    """
    db_entries = load_hermes_project_workspaces(profile_home=profile_home)
    if not db_entries:
        return list(workspaces)
    db_by_path = {e["path"]: e for e in db_entries}
    merged: list[dict] = []
    used: set[str] = set()
    for w in workspaces:
        entry = dict(w)
        hit = db_by_path.get(entry.get("path", ""))
        if hit is not None:
            entry["name"] = hit["name"]
            entry["source"] = "hermes_project"
            used.add(hit["path"])
        merged.append(entry)
    for e in db_entries:
        if e["path"] not in used:
            merged.append(dict(e))
    return merged


# ── Write path: register a WebUI workspace as a Hermes Project ─────────────
#
# #5763 Phase-1 slice, write side: creating a project from the WebUI registers
# it in the profile's authoritative projects.db so Desktop/CLI see it too.
# Preferred implementation is the upstream hermes_cli.projects_db module itself
# (same code path as `hermes project create`); a schema-compatible direct
# insert is the fallback when hermes_cli is not importable in this process.


def _projects_db_module():
    try:
        import hermes_cli.projects_db as pdb
        return pdb
    except Exception:
        return None


def _invalidate_cache(db: Path) -> None:
    with _cache_lock:
        _cache.pop(str(db), None)


def _direct_create_project(conn, *, name: str, primary_path: str) -> str:
    """Fallback insert mirroring hermes_cli.projects_db.create_project."""
    import re as _re
    import secrets as _secrets
    slug = _re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-_")[:64].strip("-_") or "project"
    base = slug
    n = 1
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone() is not None:
        n += 1
        slug = base[:60] + f"-{n}"
    pid = "p_" + _secrets.token_hex(4)
    now = int(time.time())
    conn.execute(
        "INSERT INTO projects (id, slug, name, primary_path, created_at, archived)"
        " VALUES (?, ?, ?, ?, ?, 0)",
        (pid, slug, name, primary_path, now),
    )
    conn.execute(
        "INSERT INTO project_folders (project_id, path, label, is_primary, added_at)"
        " VALUES (?, ?, NULL, 1, ?)",
        (pid, primary_path, now),
    )
    return pid


def create_hermes_project(path: str, name: str, profile_home: Path | None = None) -> dict:
    """Create a Hermes Project for ``path`` in the profile's projects.db.

    Returns ``{'id', 'slug', 'name', 'path', 'created': True}``.
    Raises ValueError with a user-facing message when the path already belongs
    to another project or the name is empty; RuntimeError when projects.db is
    unavailable. Never creates the DB file if it is missing (mode=rw, not rwc).
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("project name must not be empty")
    db = _projects_db_path(profile_home)
    if db is None:
        raise RuntimeError("projects.db not found for this profile — cannot register project")
    resolved = os.path.abspath(os.path.expanduser(str(path).strip())).rstrip("/\\")
    pdb = _projects_db_module()
    if pdb is not None:
        conn = pdb.connect(db_path=db)
        try:
            pid = pdb.create_project(conn, name=name, primary_path=resolved)
            row = conn.execute("SELECT slug FROM projects WHERE id = ?", (pid,)).fetchone()
            slug = row["slug"] if row else None
            conn.commit()
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    else:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT p.slug, p.id FROM projects p"
                " JOIN project_folders pf ON pf.project_id = p.id"
                " WHERE pf.path = ? LIMIT 1",
                (resolved,),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"folder already belongs to project '{existing['slug']}' ({existing['id']}); "
                    "switch to it instead of creating a duplicate"
                )
            slug = _direct_create_project(conn, name=name, primary_path=resolved)
            conn.commit()
        finally:
            conn.close()
    _invalidate_cache(db)
    return {"id": pid, "slug": slug, "name": name, "path": resolved, "created": True}
