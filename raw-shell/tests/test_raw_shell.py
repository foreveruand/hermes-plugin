"""Tests for the raw-shell plugin (stdlib only, no Hermes needed)."""

import importlib.util
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("raw_shell_plugin", PLUGIN_DIR / "__init__.py")
raw_shell = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(raw_shell)


class FakeCtx:
    def __init__(self):
        self.commands = {}

    def register_command(self, name, handler=None, description="", args_hint="", **kwargs):
        self.commands[name] = handler


def _handle(raw_args=None, **kwargs):
    return raw_shell._build_handler()(raw_args, **kwargs)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("RAW_SHELL_TIMEOUT", "RAW_SHELL_MAX_CHARS"):
        monkeypatch.delenv(key, raising=False)


def test_local_platform_runs_command():
    out = _handle(raw_args="printf hello", platform="cli", session_key="cli:local")
    assert "hello" in out
    assert "⛔" not in out


@pytest.mark.parametrize(
    "platform,session_key",
    [
        ("telegram", "telegram:1"),
        ("discord", "discord:1"),
        ("some_new_platform", ""),
        ("", ""),
    ],
)
def test_remote_and_unknown_platforms_run_without_authentication(platform, session_key):
    """There is no user allowlist: identity must never be a rejection reason."""
    source = types.SimpleNamespace(user_id="987654321")
    out = _handle(
        raw_args="printf remote-ok",
        platform=platform,
        session_key=session_key,
        source=source,
    )
    assert "remote-ok" in out
    assert "⛔" not in out


def test_telegram_df_h_runs_without_authentication():
    out = _handle(raw_args="df -h", platform="telegram", session_key="telegram:123456789")
    assert "$ df -h" in out
    assert "⛔" not in out
    assert "Filesystem" in out or "文件系统" in out


def test_telegram_ls_directory_runs_without_authentication(tmp_path):
    target = tmp_path / "listing"
    target.mkdir()
    (target / "marker.txt").write_text("ok")

    out = _handle(raw_args=f"ls {target}", platform="telegram", session_key="telegram:1")
    assert f"$ ls {target}" in out
    assert "marker.txt" in out
    assert "⛔" not in out


def test_output_is_truncated_to_max_chars(monkeypatch):
    monkeypatch.setenv("RAW_SHELL_MAX_CHARS", "200")
    out = _handle(raw_args="printf 'a%.0s' {1..600}", platform="cli", session_key="cli:local")
    assert "truncated: showing first 200 of 600 chars" in out
    body = out.split("\n", 1)[1]
    assert body.split("\n", 1)[0] == "a" * 200


def test_timeout_kills_process_group_and_returns_partial_output(monkeypatch):
    monkeypatch.setenv("RAW_SHELL_TIMEOUT", "1")
    out = _handle(
        raw_args="printf partial; sleep 5; printf never",
        platform="cli",
        session_key="cli:local",
    )
    assert "partial" in out
    assert "timed out after 1s" in out
    assert "\nnever" not in out


def test_register_adds_raw_and_shell():
    ctx = FakeCtx()
    raw_shell.register(ctx)
    assert set(ctx.commands) == {"raw", "shell"}


def test_registered_raw_handler_runs_from_telegram():
    ctx = FakeCtx()
    raw_shell.register(ctx)
    out = ctx.commands["raw"](
        "printf bound",
        platform="telegram",
        session_key="telegram:1",
        source=types.SimpleNamespace(user_id="987654321"),
    )
    assert "bound" in out
    assert "⛔" not in out


def test_shell_alias_runs_from_telegram():
    ctx = FakeCtx()
    raw_shell.register(ctx)
    out = ctx.commands["shell"](
        "printf alias-ok",
        platform="telegram",
        session_key="telegram:1",
        source=types.SimpleNamespace(user_id="987654321"),
    )
    assert "alias-ok" in out
    assert "⛔" not in out


def test_shell_alias_passes_directory_argument():
    """Regression: `/shell ls <dir>` must keep the directory in the command.

    A legacy ``quick_commands.raw`` wins over the plugin and drops the argument,
    so ``/shell`` is the route that must reach ``ls`` with its path intact.
    """
    target = PLUGIN_DIR / "tests"
    ctx = FakeCtx()
    raw_shell.register(ctx)
    out = ctx.commands["shell"](f"ls {target}", platform="cli", session_key="cli:local")
    assert f"$ ls {target}" in out
    assert "test_raw_shell.py" in out


def test_handler_never_raises_on_spawn_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(raw_shell.subprocess, "Popen", boom)
    out = _handle(raw_args="echo hi", platform="cli", session_key="cli:local")
    assert "failed to run" in out


@pytest.mark.parametrize(
    "payload",
    [
        "df -h",
        {"command": "df -h"},
        {"text": "df -h"},
        {"args": "df -h"},
        {"args": {"command": "df -h"}},
        {"args": ["df", "-h"]},
        ["df", "-h"],
        ("df", "-h"),
        b"df -h",
    ],
)
def test_df_arguments_survive_every_dispatch_shape(payload):
    """Regression: Hermes may hand the handler a dict/args payload, not just str.

    The old ``(raw_args or "").strip()`` either lost ``df -h`` or let an empty
    container reach ``Popen([], shell=True)``, which prints
    ``/bin/bash: -c: option requires an argument``.
    """
    out = _handle(raw_args=payload, platform="cli", session_key="cli:local")
    assert "$ df -h" in out
    assert "option requires an argument" not in out


@pytest.mark.parametrize(
    "kwargs",
    [
        {"args": {"text": "df -h"}},
        {"args": {"args": ["df", "-h"]}},
        {"command": "df -h"},
        {"text": "df -h"},
        {"args": "df -h"},
    ],
)
def test_command_fields_passed_as_keywords_are_used(kwargs):
    """Some callers omit the positional argument and send the same fields as kwargs."""
    out = _handle(platform="cli", session_key="cli:local", **kwargs)
    assert "$ df -h" in out
    assert "option requires an argument" not in out


def test_ls_directory_survives_dict_dispatch(tmp_path):
    target = tmp_path / "listing"
    target.mkdir()
    (target / "marker.txt").write_text("ok")

    out = _handle(
        raw_args={"args": {"text": f"ls {target}"}},
        platform="cli",
        session_key="cli:local",
    )
    assert f"$ ls {target}" in out
    assert "marker.txt" in out


def test_ls_directory_survives_keyword_dispatch(tmp_path):
    target = tmp_path / "listing"
    target.mkdir()
    (target / "marker.txt").write_text("ok")

    ctx = FakeCtx()
    raw_shell.register(ctx)
    out = ctx.commands["raw"](
        args={"command": f"ls {target}"},
        platform="cli",
        session_key="cli:local",
    )
    assert f"$ ls {target}" in out
    assert "marker.txt" in out


@pytest.mark.parametrize(
    "payload",
    [None, "", "   ", {}, [], (), {"args": ""}, {"args": []}, {"text": {}}],
)
def test_empty_payload_returns_usage_without_invoking_bash(monkeypatch, payload):
    """An empty command must produce a hint, never ``bash -c`` with no argument."""

    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not start for an empty command")

    monkeypatch.setattr(raw_shell.subprocess, "Popen", boom)
    out = _handle(raw_args=payload, platform="cli", session_key="cli:local")
    assert "Usage: /raw" in out


def test_remote_dict_payload_runs_without_authentication():
    ctx = FakeCtx()
    raw_shell.register(ctx)
    out = ctx.commands["raw"](
        {"command": "printf remote-ok"},
        platform="telegram",
        session_key="telegram:1",
        source=types.SimpleNamespace(user_id="987654321"),
    )
    assert "remote-ok" in out
    assert "⛔" not in out
