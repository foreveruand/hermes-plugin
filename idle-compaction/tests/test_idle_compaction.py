"""Unit tests for the idle-compaction patch (Issue #79357)."""

from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "idle_compaction_plugin", PLUGIN_ROOT / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["idle_compaction_plugin"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class FakeCompressor:
    threshold_tokens = 100_000
    summary_target_ratio = 0.2


class FakeAgent:
    def __init__(self, *, floor=None, last_activity=None, idle_after=3600, enabled=True):
        self._compressor = FakeCompressor()
        self._last_activity_ts = last_activity
        self.compression_idle_compact_min_tokens = floor
        self.compression_config = {
            "enabled": enabled,
            "idle_compact_after_seconds": idle_after,
        }
        self._awaiting_real_usage_after_compression = False
        self._statuses = []
        self.compress_calls = []

    def _compress_context(
        self,
        messages,
        system_message,
        approx_tokens=None,
        task_id=None,
        trigger=None,
    ):
        self.compress_calls.append(
            {
                "messages": messages,
                "system_message": system_message,
                "approx_tokens": approx_tokens,
                "task_id": task_id,
                "trigger": trigger,
            }
        )
        return list(messages) + [{"role": "system", "content": "summary"}]

    def _emit_status(self, message):
        self._statuses.append(message)


class FakeOut:
    def __init__(self, messages, current_turn_user_idx=None):
        self.messages = messages
        self.current_turn_user_idx = current_turn_user_idx


def make_fake_tc():
    module = types.ModuleType("agent.turn_context_compaction")
    module.calls = {"tokens": 20_000}

    def original(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("upstream _idle_compaction should be patched")

    module._idle_compaction = original

    def preflight(messages, system_message):
        return module.calls["tokens"]

    def should_idle_compact(
        messages=None,
        approx_tokens=0,
        floor_tokens=0,
        idle_gap=0.0,
        idle_after=0.0,
        **kwargs,
    ):
        return approx_tokens >= floor_tokens and idle_gap >= idle_after

    def status(**kwargs):
        return "idle compaction status"

    def history(agent, messages):
        module.calls["history"] = (agent, messages)

    def reanchor(agent, messages, user_message):
        module.calls["reanchor"] = (agent, messages, user_message)

    module._preflight_request_tokens = preflight
    module._should_idle_compact = should_idle_compact
    module.automatic_compaction_status_message = status
    module.conversation_history_after_compression = history
    module._reanchor = reanchor
    return module


@pytest.fixture
def fake_tc():
    name = "agent.turn_context_compaction"
    module = make_fake_tc()
    saved = sys.modules.get(name)
    sys.modules[name] = module
    plugin._PATCHED = False
    plugin._ORIGINAL_IDLE_COMPACTION = None

    yield module

    plugin.unpatch()
    plugin._PATCHED = False
    plugin._ORIGINAL_IDLE_COMPACTION = None
    if saved is not None:
        sys.modules[name] = saved
    else:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# idle_reference_timestamp
# ---------------------------------------------------------------------------


def test_reference_timestamp_uses_latest_history_message():
    now = time.time()
    messages = [
        {"role": "user", "timestamp": now - 7200},
        {"role": "assistant", "timestamp": now - 5000},
        {"role": "user", "timestamp": now},
    ]
    agent = FakeAgent(last_activity=now)

    assert plugin.idle_reference_timestamp(agent, messages) == pytest.approx(now - 5000)


def test_reference_timestamp_respects_current_turn_index():
    now = time.time()
    messages = [
        {"role": "user", "timestamp": now - 7200},
        {"role": "assistant", "timestamp": now - 6000},
        {"role": "user", "timestamp": now},
    ]
    agent = FakeAgent(last_activity=now)

    assert plugin.idle_reference_timestamp(agent, messages, 2) == pytest.approx(now - 6000)


def test_reference_timestamp_falls_back_to_last_activity():
    now = time.time()
    agent = FakeAgent(last_activity=now - 120)
    messages = [{"role": "user"}, {"role": "assistant"}]

    assert plugin.idle_reference_timestamp(agent, messages) == pytest.approx(now - 120)


def test_reference_timestamp_ignores_bool_and_invalid_values():
    now = time.time()
    agent = FakeAgent(last_activity=now - 30)
    messages = [{"timestamp": True}, {"timestamp": -1}, {"role": "assistant"}]

    assert plugin.idle_reference_timestamp(agent, messages) == pytest.approx(now - 30)


def test_reference_timestamp_last_resort_is_now():
    agent = FakeAgent(last_activity=None)
    messages = [{"role": "user"}, {"role": "assistant"}]

    before = time.time()
    reference = plugin.idle_reference_timestamp(agent, messages)
    after = time.time()

    assert before <= reference <= after


# ---------------------------------------------------------------------------
# resolve_idle_floor
# ---------------------------------------------------------------------------


def test_resolve_floor_prefers_agent_attribute(monkeypatch):
    monkeypatch.setattr(
        plugin,
        "_load_hermes_config",
        lambda: {"compression": {"idle_compact_min_tokens": 9_999}},
    )
    agent = FakeAgent(floor=15_000)

    assert plugin.resolve_idle_floor(agent, agent._compressor) == 15_000


def test_resolve_floor_uses_configured_value(monkeypatch):
    monkeypatch.setattr(
        plugin,
        "_load_hermes_config",
        lambda: {"compression": {"idle_compact_min_tokens": 7_777}},
    )
    agent = FakeAgent(floor=None)

    assert plugin.resolve_idle_floor(agent, agent._compressor) == 7_777


def test_resolve_floor_falls_back_to_native_formula(monkeypatch):
    monkeypatch.setattr(plugin, "_load_hermes_config", lambda: {"compression": {}})
    agent = FakeAgent(floor=None)

    assert plugin.resolve_idle_floor(agent, agent._compressor) == 20_000


# ---------------------------------------------------------------------------
# patched_idle_compaction
# ---------------------------------------------------------------------------


def test_patched_compaction_runs_when_idle_and_large_enough(fake_tc):
    now = time.time()
    agent = FakeAgent(floor=15_000, last_activity=now)
    messages = [
        {"role": "user", "timestamp": now - 7200},
        {"role": "assistant", "timestamp": now - 7000},
        {"role": "user", "timestamp": now},
    ]
    out = FakeOut(messages, current_turn_user_idx=2)
    user_message = {"role": "user", "content": "continue"}

    plugin.patched_idle_compaction(agent, out, "system", user_message, "task-1")

    assert len(agent.compress_calls) == 1
    call = agent.compress_calls[0]
    assert call["approx_tokens"] == 20_000
    assert call["task_id"] == "task-1"
    assert call["trigger"] == "idle"
    assert out.messages is not messages
    assert fake_tc.calls["history"] == (agent, out.messages)
    assert fake_tc.calls["reanchor"] == (agent, out.messages, user_message)
    assert agent._statuses == ["idle compaction status"]


def test_patched_compaction_with_tuple_return(fake_tc):
    now = time.time()
    agent = FakeAgent(floor=15_000, last_activity=now)

    def custom_compress(messages, system_message, approx_tokens=None, task_id=None, trigger=None):
        agent.compress_calls.append({"trigger": trigger})
        return ([{"role": "system", "content": "compacted"}], "new-system-prompt")

    agent._compress_context = custom_compress
    messages = [
        {"role": "user", "timestamp": now - 7200},
        {"role": "assistant", "timestamp": now - 7000},
        {"role": "user", "timestamp": now},
    ]
    out = FakeOut(messages, current_turn_user_idx=2)
    out.active_system_prompt = "old-system-prompt"
    user_message = {"role": "user", "content": "continue"}

    plugin.patched_idle_compaction(agent, out, "system", user_message, "task-1")

    assert len(agent.compress_calls) == 1
    assert out.messages == [{"role": "system", "content": "compacted"}]
    assert out.active_system_prompt == "new-system-prompt"


def test_patched_compaction_skips_when_below_floor(fake_tc):
    now = time.time()
    fake_tc.calls["tokens"] = 10_000
    agent = FakeAgent(floor=15_000, last_activity=now)
    messages = [
        {"role": "user", "timestamp": now - 7200},
        {"role": "assistant", "timestamp": now - 7000},
        {"role": "user", "timestamp": now},
    ]
    out = FakeOut(messages, current_turn_user_idx=2)

    plugin.patched_idle_compaction(agent, out, "system", {"role": "user"}, "task-1")

    assert agent.compress_calls == []
    assert out.messages is messages
    assert "history" not in fake_tc.calls


def test_patched_compaction_skips_when_not_idle_enough(fake_tc):
    now = time.time()
    agent = FakeAgent(floor=1, last_activity=now)
    messages = [
        {"role": "user", "timestamp": now - 5},
        {"role": "assistant", "timestamp": now - 2},
        {"role": "user", "timestamp": now},
    ]
    out = FakeOut(messages, current_turn_user_idx=2)

    plugin.patched_idle_compaction(agent, out, "system", {"role": "user"}, "task-1")

    assert agent.compress_calls == []
    assert out.messages is messages


def test_patched_compaction_respects_awaiting_real_usage(fake_tc):
    now = time.time()
    agent = FakeAgent(floor=1, last_activity=now)
    agent._awaiting_real_usage_after_compression = True
    messages = [
        {"role": "user", "timestamp": now - 7200},
        {"role": "assistant", "timestamp": now - 7000},
        {"role": "user", "timestamp": now},
    ]
    out = FakeOut(messages, current_turn_user_idx=2)

    plugin.patched_idle_compaction(agent, out, "system", {"role": "user"}, "task-1")

    assert agent.compress_calls == []


# ---------------------------------------------------------------------------
# patch lifecycle and registration
# ---------------------------------------------------------------------------


def test_apply_patch_is_idempotent_and_unpatch_restores(fake_tc):
    original = fake_tc._idle_compaction

    assert plugin.apply_patch() is True
    assert fake_tc._idle_compaction is plugin.patched_idle_compaction
    assert plugin.apply_patch() is True
    assert fake_tc._idle_compaction is plugin.patched_idle_compaction

    assert plugin.unpatch() is True
    assert fake_tc._idle_compaction is original
    assert plugin.unpatch() is False


def test_register_applies_patch(fake_tc):
    class Ctx:
        pass

    plugin.register(Ctx())

    assert fake_tc._idle_compaction is plugin.patched_idle_compaction
