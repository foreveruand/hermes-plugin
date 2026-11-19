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
    def __init__(self, config=None):
        self.config = config or {}
        self.commands = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_command(self, name, handler=None, description="", args_hint="", **kwargs):
        self.commands[name] = handler


def _handle(ctx=None, **kwargs):
    return raw_shell._build_handler(ctx)(**kwargs)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("RAW_SHELL_ALLOWED_USERS", "RAW_SHELL_TIMEOUT", "RAW_SHELL_MAX_CHARS"):
        monkeypatch.delenv(key, raising=False)


def test_local_platform_runs_command():
    out = _handle(raw_args="printf hello", platform="cli", session_key="cli:local")
    assert "hello" in out
    assert "⛔" not in out


def test_empty_platform_is_denied():
    out = _handle(raw_args="printf nope", platform="", session_key="")
    assert "⛔" in out


def test_unknown_platform_is_denied():
    out = _handle(raw_args="printf nope", platform="some_new_platform", session_key="")
    assert "⛔" in out


def test_remote_without_allowlist_is_denied():
    source = types.SimpleNamespace(user_id="123456789")
    out = _handle(raw_args="printf nope", platform="telegram", session_key="telegram:1", source=source)
    assert "⛔" in out


def test_remote_allowlisted_user_runs_command():
    ctx = FakeCtx({"allowed_users": ["123456789"]})
    source = types.SimpleNamespace(user_id="123456789")
    out = _handle(ctx, raw_args="printf allowed", platform="telegram", session_key="telegram:1", source=source)
    assert "allowed" in out
    assert "⛔" not in out


def test_remote_allowlisted_but_wrong_user_is_denied():
    ctx = FakeCtx({"allowed_users": [123456789]})
    source = types.SimpleNamespace(user_id="987654321")
    out = _handle(ctx, raw_args="printf nope", platform="telegram", session_key="telegram:1", source=source)
    assert "⛔" in out


def test_env_and_config_allowlists_are_merged(monkeypatch):
    monkeypatch.setenv("RAW_SHELL_ALLOWED_USERS", "111 , 222")
    ctx = FakeCtx({"allowed_users": 333})
    for user_id in ("111", "222", "333"):
        source = types.SimpleNamespace(user_id=user_id)
        out = _handle(ctx, raw_args="printf ok", platform="discord", session_key="discord:1", source=source)
        assert "ok" in out and "⛔" not in out


def test_remote_hint_in_session_key_wins_over_local_platform():
    out = _handle(raw_args="printf nope", platform="cli", session_key="telegram:123456789")
    assert "⛔" in out


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


def test_register_binds_ctx_for_allowlist():
    ctx = FakeCtx({"allowed_users": ["123456789"]})
    raw_shell.register(ctx)
    handler = ctx.commands["raw"]
    source = types.SimpleNamespace(user_id="123456789")
    out = handler("printf bound", platform="telegram", session_key="telegram:1", source=source)
    assert "bound" in out


def test_handler_never_raises_on_spawn_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(raw_shell.subprocess, "Popen", boom)
    out = _handle(raw_args="echo hi", platform="cli", session_key="cli:local")
    assert "failed to run" in out
