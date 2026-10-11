"""An unstartable stdio element is left out of goose and deepseek sessions, and named.

goose drops such an element silently and deepseek fails the whole ``session/new``
over it (the two captures under ``test/fixtures/acp_frames``). Crew prunes the
element before the request and records it as a failed server on the session's MCP
report, so goose's session says what it lost and deepseek's session starts.
"""

from __future__ import annotations

import inspect
import os
import stat
import sys

import pytest

import kiro_crew.acp.client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_startable import prune_unstartable, unstartable_reason
from kiro_crew.acp_backends import (
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP,
)

_MISSING = os.path.join(os.sep, "nonexistent-g19", "no-such-mcp-server")


def _stdio(name: str, command: str, env: list | None = None) -> dict:
    return {"name": name, "command": command, "args": [], "env": env or [], "type": "stdio"}


# ── The check ────────────────────────────────────────────────────────────────


def test_an_absolute_command_that_does_not_exist_is_unstartable():
    assert unstartable_reason(_stdio("bad", _MISSING)) == f"command not found: {_MISSING}"


def test_the_running_interpreter_is_startable():
    assert unstartable_reason(_stdio("ok", sys.executable)) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit")
def test_an_absolute_file_without_the_execute_bit_is_unstartable(tmp_path):
    script = tmp_path / "server"
    script.write_text("#!/bin/sh\n")
    script.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert unstartable_reason(_stdio("bad", str(script))).startswith("command not found")


def test_a_bare_name_on_no_path_is_unstartable(monkeypatch):
    monkeypatch.setattr("kiro_crew.acp.mcp_startable.augmented_path", lambda _base: "")
    reason = unstartable_reason(_stdio("bad", "no-such-mcp-server-g19"))
    assert reason == "command not found on PATH: no-such-mcp-server-g19"


def test_a_bare_name_on_the_elements_own_path_is_startable(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.acp.mcp_startable.augmented_path", lambda _base: "")
    name = "g19-server.exe" if sys.platform == "win32" else "g19-server"
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    element = _stdio("ok", name, env=[{"name": "PATH", "value": str(tmp_path)}])
    assert unstartable_reason(element) == ""


def test_a_relative_path_and_a_remote_element_are_kept():
    assert unstartable_reason(_stdio("rel", os.path.join(".", "bin", "server"))) == ""
    assert unstartable_reason({"name": "remote", "type": "http", "url": "https://x"}) == ""
    assert unstartable_reason({"name": "no-command"}) == ""


def test_prune_keeps_order_and_names_each_pruned_server():
    good_a = _stdio("a", sys.executable)
    good_b = {"name": "b", "type": "http", "url": "https://x"}
    bad = _stdio("bad", _MISSING)
    kept, pruned = prune_unstartable([good_a, bad, good_b])
    assert kept == [good_a, good_b]
    assert [(p.name, p.reason) for p in pruned] == [("bad", f"command not found: {_MISSING}")]


# ── The session/new path ─────────────────────────────────────────────────────


def test_goose_and_deepseek_are_the_pruned_backends():
    assert ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP == {ACP_BACKEND_GOOSE, ACP_BACKEND_DEEPSEEK}


def _drive(tmp_path, monkeypatch, backend: str, *, rolls_back: bool):
    """A client on *backend* whose array holds one good and one unstartable element.

    The fake harness answers the way the captures say the real one does: with
    *rolls_back* (deepseek) an unstartable element in ``mcpServers`` fails the whole
    ``session/new``; without it (goose) the session is created regardless.
    """
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=backend)
    good = _stdio("good", sys.executable)
    bad = _stdio("bad", _MISSING)
    sent: list[dict] = []

    async def _work_dir():
        return str(tmp_path)

    async def _send(_method, params):
        sent.append(params)
        return len(sent)

    async def _wait(_rid, **_kw):
        names = {e.get("name") for e in sent[-1]["mcpServers"]}
        if rolls_back and "bad" in names:
            return {}
        return {"sessionId": "s-1"}

    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    # goose carries the bad element in its spec half; deepseek, with no mirror,
    # receives it as a pooled broker stub.
    spec_half = [good, bad] if client._is_goose else []
    monkeypatch.setattr(client, "_goose_session_mcp_servers", lambda: list(spec_half))
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [] if spec_half else [good, bad])
    return client, sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "rolls_back"), [(ACP_BACKEND_GOOSE, False), (ACP_BACKEND_DEEPSEEK, True)]
)
async def test_the_session_starts_without_the_bad_element_and_names_it(
    tmp_path, monkeypatch, backend, rolls_back
):
    client, sent = _drive(tmp_path, monkeypatch, backend, rolls_back=rolls_back)

    resp = await client._new_session_following_substitution()

    assert resp["sessionId"] == "s-1"
    assert [e["name"] for e in sent[-1]["mcpServers"]] == ["good"]
    payload = client.mcp_session_report().payload()
    assert payload["configured"] == ["good"]
    assert payload["failed"] == ["bad"]
    assert payload["failures"]["bad"] == f"command not found: {_MISSING}"
    assert (
        "failed to start: bad (command not found" in client.mcp_session_report().problem_summary()
    )


@pytest.mark.asyncio
async def test_a_backend_outside_the_set_sends_the_array_untouched(tmp_path, monkeypatch):
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_OPENCODE)
    bad = _stdio("bad", _MISSING)
    sent: list[dict] = []

    async def _work_dir():
        return str(tmp_path)

    async def _send(_method, params):
        sent.append(params)
        return 1

    async def _wait(_rid, **_kw):
        return {"sessionId": "s-1"}

    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    monkeypatch.setattr(client, "_opencode_session_mcp_servers", lambda: [bad])
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [])

    await client._new_session_following_substitution()

    assert [e["name"] for e in sent[-1]["mcpServers"]] == ["bad"]
    assert client.mcp_session_report().payload()["failed"] == []


def test_every_array_build_is_pruned_before_its_roster_handoff():
    """All three builds -- session/new, its substitution retry, session/load -- prune.

    Held by structure, the way the ref guard's pairing is: each roster hand-off to
    the report must be preceded by a prune of the same params' array, so a fourth
    session-establishment path that forgets it fails here.
    """
    lines = inspect.getsource(client_mod).splitlines()
    handoffs = [i for i, ln in enumerate(lines) if "self._begin_session_report(" in ln]
    prunes = [i for i, ln in enumerate(lines) if "await self._prune_unstartable_mcp(" in ln]
    assert len(handoffs) == 3
    assert len(prunes) == 3
    for prune, handoff in zip(prunes, handoffs):
        assert prune < handoff
        target = lines[prune].split("=", 1)[0].strip()
        assert target.endswith('["mcpServers"]'), lines[prune]
        params = target.split("[", 1)[0]
        assert f'{params}.get("mcpServers")' in lines[handoff], (lines[prune], lines[handoff])
