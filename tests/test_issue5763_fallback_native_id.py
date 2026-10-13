"""Greptile P1 on b6972954 / 4e3f8052: fallback projection drops native id.

Saved mirror rows carry only path/name/project_mirror today — ``_clean_workspace_list``
strips ``project_id``. When the store is unreadable or the kill switch is off,
merge pass-through therefore hands the picker an identity-free row. A later
path-only remove/rename can change a successor project that appeared at the
same path.

Keep the persisted native id through load + fallback merge.
"""

import api.projects_bridge as bridge
from api.workspace import load_workspaces, save_workspaces


def test_fallback_merge_keeps_saved_project_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "0")
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    bridge._cache.clear()

    host = tmp_path / "proj"
    host.mkdir()
    save_workspaces(
        [{"path": str(host), "name": "A", "project_mirror": True, "project_id": "proj-a"}],
        profile="default",
    )
    loaded = load_workspaces(profile="default")
    assert loaded[0].get("project_id") == "proj-a", (
        f"load_workspaces dropped native id: {loaded!r}"
    )

    out = bridge.merge_hermes_projects(loaded, profile_home=tmp_path, profile="default")
    assert len(out) == 1
    assert out[0].get("project_mirror") is True
    assert out[0].get("project_id") == "proj-a", (
        f"fallback merge dropped native id: {out!r}"
    )
