"""The capture hook's profile stamp must survive into a record tag.

The chain is: hook reads `profile` from the Hermes payload → stamps it into the
spool event → the drain worker turns it into `extra_tags=["profile:<name>"]`.

Only the middle and last links are testable here; the first is covered by
exercising the hook script directly. The invariant that matters: a malformed or
absent profile must produce NO tag, because a bogus tag either matches nothing
(a silently unattributable record) or — worse — could match a real scope.
"""

from __future__ import annotations

import json

import pytest


def _tags_for(ev: dict):
    """The drain worker's tag expression, kept in lockstep with the source.

    If the worker's expression changes, this must change with it; the value of
    the test is the edge-case table below, not the expression's shape.
    """
    return (
        [f"profile:{ev['profile']}"]
        if isinstance(ev.get("profile"), str) and ev.get("profile")
        else None
    )


class TestDrainProfileTag:
    @pytest.mark.parametrize(
        "ev,expected",
        [
            ({"text": "a", "profile": "sysadmin"}, ["profile:sysadmin"]),
            ({"text": "b", "profile": "default"}, ["profile:default"]),
            ({"text": "c"}, None),
            ({"text": "d", "profile": ""}, None),
            ({"text": "e", "profile": None}, None),
            ({"text": "f", "profile": 123}, None),
            ({"text": "g", "profile": ["x"]}, None),
            ({"text": "h", "profile": {"a": 1}}, None),
        ],
    )
    def test_tag_or_none(self, ev, expected):
        assert _tags_for(ev) == expected

    def test_non_string_profile_never_becomes_a_tag(self):
        # A dict/list profile would stringify into a nonsense tag that no scope
        # can match -- silently invisible rather than an error.
        for bad in (123, 1.5, True, ["sysadmin"], {"p": "sysadmin"}):
            assert _tags_for({"text": "x", "profile": bad}) is None


class TestCaptureHookStampsProfile:
    """Run the real hook against a temp HOME and inspect the spool event."""

    def _run_hook(self, tmp_path, payload: dict):
        import os
        import sqlite3
        import subprocess
        from pathlib import Path

        hook = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "iai_mcp"
            / "_deploy"
            / "hooks"
            / "iai-mcp-hermes-capture.sh"
        )
        if not hook.is_file():
            pytest.skip("capture hook template not present")

        # A minimal state.db so the hook finds rows to spool.
        hermes = tmp_path / "hermes"
        hermes.mkdir(parents=True, exist_ok=True)
        db = hermes / "state.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, timestamp TEXT)"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'user', 'hello from a turn', '2026-01-01T00:00:00+00:00')",
            (payload["session_id"],),
        )
        conn.commit()
        conn.close()

        # The hook hardcodes ~/.iai-mcp for its spool dir, so bind HOME to the
        # temp dir and keep the store out of the developer's real one.
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        env["IAI_MCP_HERMES_HOME"] = str(hermes)
        (tmp_path / ".iai-mcp" / ".deferred-captures").mkdir(parents=True, exist_ok=True)

        subprocess.run(
            ["bash", str(hook)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        spool = list((tmp_path / ".iai-mcp" / ".deferred-captures").glob("*.jsonl"))
        return [json.loads(ln) for p in spool for ln in p.read_text().splitlines()[1:]]

    def test_profile_is_stamped_when_present(self, tmp_path):
        events = self._run_hook(
            tmp_path,
            {"session_id": "s-1", "cwd": str(tmp_path), "profile": "sysadmin"},
        )
        assert events, "hook produced no spool events"
        assert all(e.get("profile") == "sysadmin" for e in events)

    def test_profile_absent_when_host_omits_it(self, tmp_path):
        events = self._run_hook(tmp_path, {"session_id": "s-2", "cwd": str(tmp_path)})
        assert events
        assert all("profile" not in e for e in events)
