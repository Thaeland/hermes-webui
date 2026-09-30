"""Tests for issue #5763 read slice — projects.db -> workspace list bridge.

The bridge must be read-only and fail-safe: a present projects.db surfaces
Hermes Projects in the workspace list; a missing/corrupt DB leaves the
workspaces.json behavior exactly as before.
"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from api.projects_bridge import (
    load_hermes_project_workspaces,
    merge_hermes_projects,
    _cache,
)


def _make_projects_db(home: Path, projects: list[dict]) -> Path:
    """Create a minimal projects.db matching the upstream schema."""
    db = home / "projects.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            description TEXT, icon TEXT, color TEXT, board_slug TEXT,
            primary_path TEXT, created_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE project_folders (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL, label TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
            added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path)
        );
        """
    )
    for i, p in enumerate(projects):
        conn.execute(
            "INSERT INTO projects (id, slug, name, created_at, archived) VALUES (?,?,?,?,?)",
            (p["id"], p["slug"], p["name"], 1000 + i, p.get("archived", 0)),
        )
        for j, folder in enumerate(p.get("folders", [])):
            conn.execute(
                "INSERT INTO project_folders (project_id, path, is_primary, added_at) VALUES (?,?,?,?)",
                (p["id"], folder, 1 if j == 0 else 0, 2000 + j),
            )
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    _cache.clear()
    yield
    _cache.clear()


def test_no_db_returns_empty(tmp_path):
    assert load_hermes_project_workspaces(profile_home=tmp_path) == []


def test_projects_surface_with_names(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "gufo", "name": "Gufo", "folders": ["/srv/gufo"]},
        {"id": "p2", "slug": "omp", "name": "Oh My Pi", "folders": ["/srv/omp"]},
    ])
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert [(e["path"], e["name"]) for e in entries] == [
        ("/srv/gufo", "Gufo"), ("/srv/omp", "Oh My Pi"),
    ]
    assert all(e["source"] == "hermes_project" for e in entries)


def test_archived_and_folderless_projects_skipped(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Archived", "folders": ["/srv/a"], "archived": 1},
        {"id": "p2", "slug": "b", "name": "Folderless", "folders": []},
        {"id": "p3", "slug": "c", "name": "Real", "folders": ["/srv/c"]},
    ])
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert [(e["path"], e["name"]) for e in entries] == [("/srv/c", "Real")]


def test_non_primary_folder_used_when_no_primary(tmp_path):
    db = _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "x", "name": "X", "folders": ["/srv/first", "/srv/second"]},
    ])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE project_folders SET is_primary = 0")
    conn.commit()
    conn.close()
    _cache.clear()
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert entries[0]["path"] == "/srv/first"


def test_corrupt_db_fails_safe(tmp_path):
    (tmp_path / "projects.db").write_text("not a database")
    assert load_hermes_project_workspaces(profile_home=tmp_path) == []


def test_cache_invalidates_on_mtime(tmp_path):
    db = _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    assert len(load_hermes_project_workspaces(profile_home=tmp_path)) == 1
    # Add a project without clearing the cache; bump mtime explicitly since
    # same-second writes can leave st_mtime unchanged on coarse filesystems.
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO projects (id, slug, name, created_at) VALUES ('p2','b','B',1001)")
    conn.execute("INSERT INTO project_folders VALUES ('p2','/srv/b',NULL,1,2001)")
    conn.commit()
    conn.close()
    future = time.time() + 5
    import os
    os.utime(db, (future, future))
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert {(e["path"], e["name"]) for e in entries} == {
        ("/srv/a", "A"), ("/srv/b", "B"),
    }


def test_merge_renames_local_entry_and_appends_new(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "gufo", "name": "Gufo", "folders": ["/srv/gufo"]},
        {"id": "p2", "slug": "omp", "name": "Oh My Pi", "folders": ["/srv/omp"]},
    ])
    local = [
        {"path": "/home/user/workspace", "name": "Home"},
        {"path": "/srv/gufo", "name": "gufo-dir"},  # local label loses to project name
    ]
    merged = merge_hermes_projects(local, profile_home=tmp_path)
    assert [(e["path"], e["name"]) for e in merged] == [
        ("/home/user/workspace", "Home"),
        ("/srv/gufo", "Gufo"),
        ("/srv/omp", "Oh My Pi"),
    ]
    # Input list is never mutated
    assert local[1]["name"] == "gufo-dir"


def test_merge_without_db_is_identity(tmp_path):
    local = [{"path": "/srv/x", "name": "X"}]
    assert merge_hermes_projects(local, profile_home=tmp_path) == local


def test_env_kill_switch(tmp_path, monkeypatch):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "0")
    assert load_hermes_project_workspaces(profile_home=tmp_path) == []
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    assert len(load_hermes_project_workspaces(profile_home=tmp_path)) == 1
