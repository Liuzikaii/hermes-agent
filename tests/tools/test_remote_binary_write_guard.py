"""Binary overwrite decisions use the execution target's filesystem."""

import json
import sqlite3

import pytest

from tools import file_tools, terminal_tool
from tools.file_operations import ShellFileOperations
from tools.file_tools_write_guards import _check_binary_document_write
from tools.registry import registry


@pytest.fixture(params=["docker", "ssh"])
def peer(tmp_path, monkeypatch, request):
    from tools.environments.local import LocalEnvironment

    host = tmp_path / "host"
    target = tmp_path / "peer"
    host.mkdir()
    target.mkdir()
    shell = LocalEnvironment(cwd=str(target))

    class PeerTransport:
        cwd = str(host)

        def execute(self, command, cwd=None, **kwargs):
            # Only emulate the namespace boundary; the probes and mutations run
            # through the real shell and ShellFileOperations against real files.
            return shell.execute(
                command.replace(str(host), str(target)), cwd=str(target), **kwargs
            )

    env = PeerTransport()
    monkeypatch.setattr(
        file_tools, "_get_file_ops", lambda task_id: ShellFileOperations(env)
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": request.param}
    )
    try:
        yield host, target, env
    finally:
        shell.cleanup()


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize(
    "operation,extension",
    [("write", ".sqlite"), ("write", ".pdf"), ("replace", ".pdf"), ("patch", ".pdf")],
)
def test_registry_preserves_remote_binary_when_host_path_is_missing(
    peer, operation, extension
):
    host, target, _ = peer
    path = host / ("document" + extension)
    actual = target / path.name
    if extension == ".sqlite":
        with sqlite3.connect(actual) as db:
            db.execute("create table records(value text)")
            db.execute("insert into records values('original')")
    else:
        actual.write_bytes(b"%PDF-1.4\noriginal\n%%EOF\n")
    before = actual.read_bytes()
    assert not path.exists()
    if operation == "write":
        result = registry.dispatch(
            "write_file",
            {"path": str(path), "content": "replacement\n"},
            task_id="peer",
        )
    elif operation == "replace":
        result = registry.dispatch(
            "patch",
            {"path": str(path), "old_string": "original", "new_string": "replacement"},
            task_id="peer",
        )
    else:
        patch = f"*** Begin Patch\n*** Update File: {path}\n@@\n-original\n+replacement\n*** End Patch"
        result = registry.dispatch(
            "patch", {"mode": "patch", "patch": patch}, task_id="peer"
        )
    assert isinstance(result, str)
    result = json.loads(result)
    assert "Refusing" in result.get("error", ""), result
    assert actual.read_bytes() == before


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("state", ["missing", "present", "unavailable"])
def test_binary_guard_observes_peer_presence_and_requires_a_successful_probe(
    peer, state, monkeypatch
):
    host, target, env = peer
    path = host / "document.pdf"
    path.write_bytes(b"host-only shadow")
    if state == "present":
        (target / path.name).write_bytes(b"%PDF-1.4\noriginal\n%%EOF\n")
    if state == "unavailable":
        monkeypatch.setattr(
            env,
            "execute",
            lambda *args, **kwargs: {
                "output": "transport unavailable",
                "returncode": 1,
            },
        )
    error = _check_binary_document_write(str(path), "peer")
    if state == "missing":
        assert error is None, error
    else:
        assert error
        if state == "unavailable":
            assert "unavailable" in error.lower()
