"""Idle context compaction patch for Hermes Agent (Issue #79357).

Hermes decides whether a conversation has been idle long enough with
``time.time() - agent._last_activity_ts``.  That clock is reset on every
Gateway / Desktop turn:

* ``GatewayRunner._init_cached_agent_for_turn`` sets ``_last_activity_ts`` to
  the current time;
* ``TurnFacadeLease.start()`` calls ``_touch_activity()``;
* a rebuilt agent after cache eviction starts with a fresh timestamp.

The gap is therefore always ~0 seconds and idle compaction never fires.

This plugin replaces ``agent.turn_context_compaction._idle_compaction`` with a
version that derives the reference time from the conversation history and lets
``compression.idle_compact_min_tokens`` override the native floor
(``threshold_tokens * summary_target_ratio``).
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("idle-compaction")

__all__ = [
    "apply_patch",
    "idle_reference_timestamp",
    "patched_idle_compaction",
    "register",
    "resolve_idle_floor",
    "unpatch",
]

_PATCHED = False
_ORIGINAL_IDLE_COMPACTION = None

_MODULE_CANDIDATES = (
    "agent.turn_context_compaction",
    "hermes.agent.turn_context_compaction",
    "hermes_cli.agent.turn_context_compaction",
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _is_valid_timestamp(value: Any) -> bool:
    """True for a usable epoch timestamp (bools are not numbers here)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _positive_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    if value is None:
        return default
    return bool(value)


def _lookup(container: Any, key: str) -> Any:
    if isinstance(container, Mapping):
        return container.get(key)
    return getattr(container, key, None)


def _as_dict(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None) or getattr(value, "dict", None)
    if callable(dump):
        try:
            dumped = dump()
            if isinstance(dumped, Mapping):
                return dict(dumped)
        except Exception:  # pragma: no cover - defensive
            pass
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _profile_config_path() -> Optional[Path]:
    home = os.environ.get("HERMES_HOME")
    if home:
        candidate = Path(home).expanduser() / "config.yaml"
        if candidate.is_file():
            return candidate
    candidate = Path(__file__).resolve().parents[1] / "config.yaml"
    return candidate if candidate.is_file() else None


def _load_hermes_config() -> Any:
    """Best-effort Hermes config lookup.

    Prefers the public read-only loader, then falls back to parsing the
    profile's ``config.yaml`` directly so the plugin also works in tests and
    standalone tools where ``hermes_cli`` is not importable.
    """
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
    except Exception:
        load_config_readonly = None

    if load_config_readonly is not None:
        for args in ((), (None,)):
            try:
                config = load_config_readonly(*args)
            except Exception:
                continue
            if config is not None and (
                isinstance(config, Mapping) or hasattr(config, "compression")
            ):
                return config

    path = _profile_config_path()
    if path is None:
        return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except Exception:
        logger.debug("failed to read %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _compression_settings(agent: Any) -> dict:
    """Merge global compression config with any agent-local override."""
    settings: dict = {}

    global_config = _load_hermes_config()
    compression = _lookup(global_config, "compression")
    if compression is not None:
        settings.update(_as_dict(compression))

    for attr in ("compression_config", "_compression_config"):
        local = getattr(agent, attr, None)
        if local is None:
            continue
        nested = _lookup(local, "compression")
        settings.update(_as_dict(nested if nested is not None else local))

    agent_config = getattr(agent, "config", None)
    if agent_config is None:
        agent_config = getattr(agent, "_config", None)
    if agent_config is not None:
        nested = _lookup(agent_config, "compression")
        if nested is not None:
            settings.update(_as_dict(nested))

    return settings


def _get_compressor(agent: Any) -> Any:
    for attr in ("context_compressor", "_compressor", "compressor", "_context_compressor"):
        compressor = getattr(agent, attr, None)
        if compressor is not None:
            return compressor
    return None


def _compaction_module() -> Any:
    """Locate the upstream ``turn_context_compaction`` module."""
    for name in _MODULE_CANDIDATES:
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "_idle_compaction"):
            return module

    for name, module in list(sys.modules.items()):
        if (
            module is not None
            and name.endswith("turn_context_compaction")
            and hasattr(module, "_idle_compaction")
        ):
            return module

    for name in _MODULE_CANDIDATES:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _resolve_helper(module: Any, name: str, agent: Any) -> Any:
    for owner in (module, getattr(agent, "turn_context", None), agent):
        if owner is None:
            continue
        candidate = getattr(owner, name, None)
        if callable(candidate):
            return candidate
    return None


def _call_filtered(function: Any, values: Mapping[str, Any]) -> Any:
    """Call ``function`` with only the keyword arguments it accepts."""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(**values)

    parameters = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return function(**values)

    kwargs = {key: value for key, value in values.items() if key in parameters}
    missing = [
        name
        for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name not in kwargs
    ]
    if missing:
        raise TypeError(f"missing arguments: {', '.join(missing)}")
    return function(**kwargs)


def _estimate_tokens(messages: Sequence[Any], system_message: Any) -> int:
    chars = len(str(system_message or ""))
    for message in messages or []:
        if isinstance(message, Mapping):
            chars += len(str(message.get("content") or ""))
            chars += len(str(message.get("tool_calls") or ""))
    return max(1, chars // 4)


# ---------------------------------------------------------------------------
# public building blocks
# ---------------------------------------------------------------------------


def idle_reference_timestamp(
    agent: Any,
    messages: Sequence[Any],
    current_turn_user_idx: Optional[int] = None,
) -> float:
    """Reference time for the idle gap, based on real conversation history.

    The current turn's user message is excluded: ``messages[:idx]`` when a
    valid index is available, otherwise ``messages[:-1]``.  If the history
    carries no usable timestamp we fall back to ``agent._last_activity_ts`` and
    finally to ``time.time()``.
    """
    history: Sequence[Any] = ()
    if isinstance(messages, (list, tuple)):
        count = len(messages)
        valid_idx = (
            isinstance(current_turn_user_idx, int)
            and not isinstance(current_turn_user_idx, bool)
            and 0 <= current_turn_user_idx < count
        )
        end = current_turn_user_idx if valid_idx else max(count - 1, 0)
        history = messages[:end]

    latest: Optional[float] = None
    for message in history:
        if not isinstance(message, Mapping):
            continue
        stamp = message.get("timestamp")
        if not _is_valid_timestamp(stamp):
            continue
        value = float(stamp)
        if latest is None or value > latest:
            latest = value
    if latest is not None:
        return latest

    activity = getattr(agent, "_last_activity_ts", None)
    if _is_valid_timestamp(activity):
        return float(activity)

    return time.time()


def resolve_idle_floor(agent: Any, compressor: Any) -> int:
    """Minimum token count required before idle compaction may start."""
    direct = _positive_int(
        getattr(agent, "compression_idle_compact_min_tokens", None)
    )
    if direct is not None:
        return direct

    configured = _positive_int(
        _compression_settings(agent).get("idle_compact_min_tokens")
    )
    if configured is not None:
        return configured

    threshold = getattr(compressor, "threshold_tokens", 0) if compressor is not None else 0
    ratio = (
        getattr(compressor, "summary_target_ratio", None)
        if compressor is not None
        else None
    )
    if ratio is None and compressor is not None:
        ratio = getattr(compressor, "target_ratio", 0.2)
    try:
        return int(float(threshold or 0) * float(ratio if ratio is not None else 0.2))
    except (TypeError, ValueError):
        return 0


def _preflight_tokens(module: Any, agent: Any, messages: Any, system_message: Any) -> int:
    function = getattr(module, "_preflight_request_tokens", None)
    if not callable(function):
        try:
            from agent import turn_context as _tc
            function = getattr(_tc, "_preflight_request_tokens", None)
        except Exception:
            pass
    if not callable(function):
        return _estimate_tokens(messages, system_message)

    variants = (
        lambda: function(agent, messages, system_message or ""),
        lambda: function(messages, system_message),
        lambda: function(system_message, messages),
        lambda: function(agent=agent, messages=messages, active_system_prompt=system_message or ""),
        lambda: function(messages=messages, system_message=system_message),
    )
    for call in variants:
        try:
            result = call()
            if isinstance(result, tuple):
                result = result[0]
            return int(result)
        except TypeError:
            continue
        except Exception:
            logger.debug("preflight token estimate failed", exc_info=True)
            break
    return _estimate_tokens(messages, system_message)


def _should_compact(
    module: Any,
    *,
    messages: Any,
    system_message: Any,
    user_message: Any,
    approx_tokens: int,
    floor: int,
    idle_gap: float,
    idle_after: float,
    last_compaction_tokens: int,
    last_compaction_at: float,
    cooldown: float,
    awaiting_real_usage: bool,
    enabled: bool,
) -> bool:
    values = {
        "messages": messages,
        "system_message": system_message,
        "user_message": user_message,
        "approx_tokens": approx_tokens,
        "tokens": approx_tokens,
        "estimated_tokens": approx_tokens,
        "request_tokens": approx_tokens,
        "token_count": approx_tokens,
        "floor_tokens": floor,
        "idle_floor": floor,
        "min_tokens": floor,
        "idle_gap": idle_gap,
        "idle_gap_seconds": idle_gap,
        "idle_after": idle_after,
        "idle_after_seconds": int(idle_after),
        "last_compaction_tokens": last_compaction_tokens,
        "last_compaction_at": last_compaction_at,
        "last_compaction_ts": last_compaction_at,
        "cooldown": cooldown,
        "cooldown_seconds": cooldown,
        "cooldown_active": bool(cooldown),
        "awaiting_real_usage": awaiting_real_usage,
        "enabled": enabled,
        "message_count": len(messages or []),
        "n_messages": len(messages or []),
    }

    function = getattr(module, "_should_idle_compact", None)
    if not callable(function):
        try:
            from agent import turn_context as _tc
            function = getattr(_tc, "_should_idle_compact", None)
        except Exception:
            pass

    if callable(function):
        try:
            decision = _call_filtered(function, values)
            if hasattr(decision, "should_compact"):
                return bool(decision.should_compact)
            if isinstance(decision, tuple):
                return bool(decision[0])
            return bool(decision)
        except Exception:
            logger.debug("upstream _should_idle_compact failed", exc_info=True)

    if approx_tokens < floor:
        return False
    if last_compaction_tokens and approx_tokens < last_compaction_tokens:
        return False
    if (
        last_compaction_at
        and cooldown
        and (time.time() - last_compaction_at) < cooldown
    ):
        return False
    return True


def _status_message(
    module: Any,
    agent: Any,
    *,
    compressor: Any,
    approx_tokens: int,
    floor: int,
    idle_gap: float,
    idle_after: float,
) -> str:
    try:
        from agent.context_engine import automatic_compaction_status_message
        from agent.conversation_compression import IDLE_COMPACTION_STATUS_TEMPLATE
        if compressor is not None:
            text = automatic_compaction_status_message(
                compressor,
                phase="idle",
                default_message=IDLE_COMPACTION_STATUS_TEMPLATE.format(
                    idle_seconds=int(idle_gap), tokens=approx_tokens
                ),
                approx_tokens=approx_tokens,
                idle_seconds=int(idle_gap),
                model=getattr(agent, "model", None),
            )
            if text:
                return str(text)
    except Exception:
        pass

    function = _resolve_helper(module, "automatic_compaction_status_message", agent)
    if callable(function):
        values = {
            "trigger": "idle",
            "phase": "idle",
            "tokens": approx_tokens,
            "approx_tokens": approx_tokens,
            "floor_tokens": floor,
            "idle_gap": idle_gap,
            "idle_after": idle_after,
            "model": getattr(agent, "model", None),
        }
        try:
            text = _call_filtered(function, values)
        except Exception:
            logger.debug("status message helper failed", exc_info=True)
        else:
            if text:
                return str(text)

    return (
        f"Idle compaction: ~{approx_tokens} tokens after {int(idle_gap)}s idle "
        f"(floor {floor})"
    )


def _unwrap_agent(candidate: Any) -> Any:
    """Accept either the agent itself or a bound compactor holding one."""
    if hasattr(candidate, "_compress_context"):
        return candidate
    for attr in ("agent", "_agent"):
        inner = getattr(candidate, attr, None)
        if inner is not None and inner is not candidate:
            return inner
    return candidate


def patched_idle_compaction(
    agent: Any,
    out: Any,
    system_message: Any,
    user_message: Any,
    effective_task_id: Any = None,
) -> None:
    """Drop-in replacement for upstream ``_idle_compaction``."""
    agent = _unwrap_agent(agent)
    module = _compaction_module()
    if module is None:
        logger.warning("idle-compaction: turn_context_compaction is not importable")
        return

    settings = _compression_settings(agent)
    enabled = getattr(agent, "compression_enabled", None)
    if enabled is None:
        enabled = _as_bool(settings.get("enabled"), default=True)
    if not enabled:
        return

    idle_after = getattr(agent, "compression_idle_compact_after_seconds", None)
    if idle_after is None or (isinstance(idle_after, (int, float)) and idle_after <= 0):
        idle_after = _positive_float(settings.get("idle_compact_after_seconds"))
    if idle_after is None or idle_after <= 0:
        return

    messages = getattr(out, "messages", None)
    if not isinstance(messages, (list, tuple)) or not messages:
        return

    compressor = _get_compressor(agent)
    if getattr(agent, "_awaiting_real_usage_after_compression", False) or bool(
        getattr(compressor, "awaiting_real_usage_after_compression", False)
    ):
        return

    current_turn_user_idx = getattr(out, "current_turn_user_idx", None)
    if current_turn_user_idx is None:
        current_turn_user_idx = getattr(agent, "_current_turn_user_idx", None)

    reference = idle_reference_timestamp(agent, messages, current_turn_user_idx)
    idle_gap = max(0.0, time.time() - reference)
    if idle_gap < idle_after:
        return

    approx_tokens = _preflight_tokens(module, agent, messages, system_message)
    floor = resolve_idle_floor(agent, compressor)

    last_compaction_tokens = 0
    if compressor is not None:
        comp_last = getattr(compressor, "last_compression_rough_tokens", 0)
        if isinstance(comp_last, int) and not isinstance(comp_last, bool) and comp_last > 0:
            last_compaction_tokens = comp_last
    if last_compaction_tokens <= 0:
        agent_last = getattr(agent, "_last_compaction_tokens", 0)
        if isinstance(agent_last, int) and not isinstance(agent_last, bool) and agent_last > 0:
            last_compaction_tokens = agent_last

    last_compaction_at = float(
        getattr(agent, "_last_compaction_at", 0.0)
        or getattr(agent, "_last_compaction_ts", 0.0)
        or 0.0
    )

    cooldown = None
    if compressor is not None:
        cooldown_fn = getattr(compressor, "get_active_compression_failure_cooldown", None)
        if callable(cooldown_fn):
            try:
                cooldown = cooldown_fn()
            except Exception:
                pass
    if cooldown is None:
        cooldown = _positive_float(getattr(agent, "_compaction_cooldown_seconds", None))
    if cooldown is None:
        cooldown = _positive_float(settings.get("idle_compact_cooldown_seconds")) or 0.0

    if not _should_compact(
        module,
        messages=messages,
        system_message=system_message,
        user_message=user_message,
        approx_tokens=approx_tokens,
        floor=floor,
        idle_gap=idle_gap,
        idle_after=idle_after,
        last_compaction_tokens=last_compaction_tokens,
        last_compaction_at=last_compaction_at,
        cooldown=cooldown,
        awaiting_real_usage=bool(
            getattr(agent, "_awaiting_real_usage_after_compression", False)
            or getattr(compressor, "awaiting_real_usage_after_compression", False)
        ),
        enabled=True,
    ):
        return

    logger.info(
        "idle compaction triggered: gap=%.0fs tokens=%d floor=%d",
        idle_gap,
        approx_tokens,
        floor,
    )

    message = _status_message(
        module,
        agent,
        compressor=compressor,
        approx_tokens=approx_tokens,
        floor=floor,
        idle_gap=idle_gap,
        idle_after=idle_after,
    )
    emit_status = getattr(agent, "_emit_status", None)
    if message and callable(emit_status):
        try:
            emit_status(message)
        except Exception:
            logger.debug("failed to emit compaction status", exc_info=True)

    try:
        new_result = agent._compress_context(
            messages,
            system_message,
            approx_tokens=approx_tokens,
            task_id=effective_task_id,
            trigger="idle",
        )
    except Exception:
        logger.exception("idle-compaction: _compress_context failed")
        return

    if new_result is None:
        return

    new_prompt = None
    if isinstance(new_result, tuple) and len(new_result) == 2:
        new_messages, new_prompt = new_result
    else:
        new_messages = new_result

    if new_messages is None or new_messages is messages:
        return

    out.messages = new_messages
    if new_prompt is not None and hasattr(out, "active_system_prompt"):
        out.active_system_prompt = new_prompt

    history_hook = _resolve_helper(module, "conversation_history_after_compression", agent)
    if not callable(history_hook):
        try:
            from agent.conversation_compression import conversation_history_after_compression
            history_hook = conversation_history_after_compression
        except Exception:
            pass
    if callable(history_hook):
        try:
            res_history = history_hook(agent, new_messages, getattr(out, "conversation_history", None))
            if res_history is not None and hasattr(out, "conversation_history"):
                out.conversation_history = res_history
        except TypeError:
            try:
                res_history = history_hook(agent, new_messages)
                if res_history is not None and hasattr(out, "conversation_history"):
                    out.conversation_history = res_history
            except TypeError:
                try:
                    res_history = history_hook(new_messages)
                    if res_history is not None and hasattr(out, "conversation_history"):
                        out.conversation_history = res_history
                except Exception:
                    logger.debug("history hook failed", exc_info=True)
            except Exception:
                logger.debug("history hook failed", exc_info=True)
        except Exception:
            logger.debug("history hook failed", exc_info=True)

    reanchor = _resolve_helper(module, "_reanchor", agent)
    if not callable(reanchor):
        try:
            from agent.turn_context_compaction import _reanchor
            reanchor = _reanchor
        except Exception:
            pass
    if callable(reanchor):
        try:
            new_idx = reanchor(agent, new_messages, user_message)
            if isinstance(new_idx, int) and not isinstance(new_idx, bool) and hasattr(out, "current_turn_user_idx"):
                out.current_turn_user_idx = new_idx
        except TypeError:
            try:
                new_idx = reanchor(new_messages, user_message)
                if isinstance(new_idx, int) and not isinstance(new_idx, bool) and hasattr(out, "current_turn_user_idx"):
                    out.current_turn_user_idx = new_idx
            except Exception:
                logger.debug("reanchor failed", exc_info=True)
        except Exception:
            logger.debug("reanchor failed", exc_info=True)
            reanchor(agent, new_messages, user_message)
        except TypeError:
            try:
                reanchor(new_messages, user_message)
            except Exception:
                logger.debug("reanchor failed", exc_info=True)
        except Exception:
            logger.debug("reanchor failed", exc_info=True)


# ---------------------------------------------------------------------------
# patch lifecycle
# ---------------------------------------------------------------------------


def apply_patch() -> bool:
    """Install :func:`patched_idle_compaction`, idempotently."""
    global _PATCHED, _ORIGINAL_IDLE_COMPACTION

    module = _compaction_module()
    if module is None:
        logger.warning("idle-compaction: cannot patch, module not found")
        return False

    current = getattr(module, "_idle_compaction", None)
    if current is None:
        logger.warning("idle-compaction: _idle_compaction is missing")
        return False

    if current is patched_idle_compaction:
        _PATCHED = True
        return True

    if not _PATCHED or _ORIGINAL_IDLE_COMPACTION is None:
        _ORIGINAL_IDLE_COMPACTION = current

    module._idle_compaction = patched_idle_compaction
    _PATCHED = True
    logger.info("idle-compaction: patched turn_context_compaction._idle_compaction")
    return True


def unpatch() -> bool:
    """Restore the upstream implementation."""
    global _PATCHED, _ORIGINAL_IDLE_COMPACTION

    if not _PATCHED or _ORIGINAL_IDLE_COMPACTION is None:
        _PATCHED = False
        return False

    module = _compaction_module()
    if module is None:
        return False

    module._idle_compaction = _ORIGINAL_IDLE_COMPACTION
    _PATCHED = False
    _ORIGINAL_IDLE_COMPACTION = None
    logger.info("idle-compaction: upstream _idle_compaction restored")
    return True


def register(ctx: Any) -> None:
    """Plugin entry point."""
    applied = apply_patch()
    if not applied:
        logger.warning("idle-compaction: plugin loaded but patch was not applied")
