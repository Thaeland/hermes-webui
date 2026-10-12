"""Round-8 item 2 (re-gate 2026-10-12, head c67105b8): GET /api/workspaces
regression on SSH/Docker profiles, even with the sync kill switch off.

Every remoteness check re-reads the profile config (``_remote_terminal_cwd``)
and rebuilds the blocked-roots tables (dozens of ``Path.resolve()`` calls)
inside the merge loop: 2-3 candidate calls per row. The merge must do the
profile-level work ONCE per call and reuse it for every row — same call-scoped
ContextVar pattern as ``profile_home_resolve_cache_scope`` (PR #7636 lesson:
never process-lifetime; a retargeted symlink or edited config must be visible
to the next request).
"""

import api.projects_bridge as bridge
import api.workspace as workspace


def test_merge_reuses_remote_cwd_and_blocked_roots_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    # No shared store: the merge takes the pass-through projection, which
    # still stamps remote kind per row (round 7 blocker 3).
    monkeypatch.setattr(bridge, "_projects_db_path", lambda profile_home: None)

    cwd_calls: list[int] = []
    roots_calls: list[int] = []

    def _counted_cwd(profile=None):
        cwd_calls.append(1)
        return "/remote/work"

    orig_roots = workspace._workspace_blocked_roots

    def _counted_roots():
        roots_calls.append(1)
        return orig_roots()

    monkeypatch.setattr(workspace, "_remote_terminal_cwd", _counted_cwd)
    monkeypatch.setattr(workspace, "_workspace_blocked_roots", _counted_roots)

    N = 8
    rows = [{"path": f"/remote/work/p{i}", "name": f"p{i}"} for i in range(N)]
    out = bridge.merge_hermes_projects(rows, profile_home=None, profile="default")

    # Behavior must be unchanged: every remote row keeps its explicit kind.
    assert len(out) == N
    assert all(w.get("workspace_kind") == "remote" for w in out)

    # Profile-level work happens ONCE per merge call, not once per row.
    assert len(cwd_calls) == 1, (
        f"remote terminal cwd re-read {len(cwd_calls)} times for one merge call "
        f"(config read per row — the SSH/Docker latency finding)"
    )
    # Blocked-root builders are also profile/process constants: a handful of
    # first-call builds is fine, a per-row rebuild is the regression.
    assert len(roots_calls) <= 4, (
        f"blocked-roots table rebuilt {len(roots_calls)} times in one merge call "
        f"(each rebuild is ~45 Path.resolve() calls)"
    )


def test_scope_memo_is_call_scoped_not_process_lifetime(monkeypatch):
    """The memo must not leak across merge calls: a config change between two
    requests is visible on the next one (PR #7636 lesson)."""
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    monkeypatch.setattr(bridge, "_projects_db_path", lambda profile_home: None)

    values = iter(["/remote/a", "/remote/b"])
    monkeypatch.setattr(workspace, "_remote_terminal_cwd",
                        lambda profile=None: next(values))

    rows_a = [{"path": "/remote/a/x", "name": "x"}]
    out_a = bridge.merge_hermes_projects(rows_a, profile_home=None, profile="default")
    assert out_a[0].get("workspace_kind") == "remote"

    rows_b = [{"path": "/remote/b/y", "name": "y"}]
    out_b = bridge.merge_hermes_projects(rows_b, profile_home=None, profile="default")
    assert out_b[0].get("workspace_kind") == "remote", (
        "second merge call must see the NEW cwd — a process-lifetime memo "
        "would still answer /remote/a and drop the kind stamp"
    )
