"""VibeGuard Privacy Protection Plugin for Hermes Agent.

Sensitive names (sites, devices) are replaced by fixed codes from
data/vibeguard/mapping.json; credentials, ID numbers and phone numbers are
replaced by realistic random values that are restored on the way back.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

try:
    from . import mapping
    from .config import load_config
    from .engine import redact_deep, redact_text, restore_deep, restore_text
    from .notice import build_placeholder_notice
    from .patterns import build_pattern_set
    from .session import PlaceholderSession
except ImportError:
    import mapping
    from config import load_config
    from engine import redact_deep, redact_text, restore_deep, restore_text
    from notice import build_placeholder_notice
    from patterns import build_pattern_set
    from session import PlaceholderSession

logger = logging.getLogger("vibeguard")

_SESSIONS: Dict[str, PlaceholderSession] = {}
_LOCK = threading.RLock()
_MAX_SESSIONS = 64


def get_session(session_id: str, prefix: str = "__VG_", entities: Any = None) -> PlaceholderSession:
    sess_key = str(session_id or "default")
    with _LOCK:
        sess = _SESSIONS.get(sess_key)
        if sess is None:
            if len(_SESSIONS) >= _MAX_SESSIONS:
                _SESSIONS.pop(next(iter(_SESSIONS)), None)
            sess = PlaceholderSession(prefix=prefix)
            _SESSIONS[sess_key] = sess
        sess.entities = entities
        return sess


def register(ctx: Any) -> None:
    config = load_config()
    if not config.get("enabled", True):
        logger.info("[vibeguard] Plugin disabled by configuration.")
        return

    prefix = config.get("placeholder_prefix", "__VG_")
    patterns = build_pattern_set(config.get("patterns"), config)
    entities = patterns.entities

    logger.info(
        "[vibeguard] Initialized with %d regex rules, %d keywords, %d mapped devices.",
        len(patterns.regex),
        len(patterns.keywords),
        entities.count if entities is not None else 0,
    )

    def on_llm_request(**kwargs: Any) -> Optional[Dict[str, Any]]:
        """Intercept provider kwargs before sending to LLM. Redact sensitive patterns."""
        req = kwargs.get("request")
        if not isinstance(req, dict):
            return None

        # The monthly sync rewrites mapping.json while the gateway keeps running.
        if mapping.reload_if_changed(config):
            with _LOCK:
                for sess in _SESSIONS.values():
                    sess.cache_clear()

        session_id = kwargs.get("session_id") or "default"
        sess = get_session(session_id, prefix=prefix, entities=entities)
        changed = False

        # Redact messages
        messages = req.get("messages")
        if isinstance(messages, list):
            redacted_messages = []
            for msg in messages:
                if isinstance(msg, dict):
                    msg_copy = dict(msg)
                    content = msg_copy.get("content")
                    if isinstance(content, str):
                        new_content, count = redact_text(content, patterns, sess)
                        if count > 0:
                            msg_copy["content"] = new_content
                            changed = True
                    elif isinstance(content, list):
                        new_parts = []
                        for part in content:
                            if (
                                isinstance(part, dict)
                                and part.get("type") == "text"
                                and isinstance(part.get("text"), str)
                            ):
                                new_text, count = redact_text(part["text"], patterns, sess)
                                if count > 0:
                                    p_copy = dict(part)
                                    p_copy["text"] = new_text
                                    new_parts.append(p_copy)
                                    changed = True
                                    continue
                            new_parts.append(part)
                        msg_copy["content"] = new_parts

                    # Redact arguments in past tool_calls
                    tool_calls = msg_copy.get("tool_calls")
                    if isinstance(tool_calls, list):
                        new_tool_calls = []
                        for tc in tool_calls:
                            if isinstance(tc, dict) and "function" in tc and isinstance(tc["function"], dict):
                                tc_copy = dict(tc)
                                fn_copy = dict(tc["function"])
                                args_str = fn_copy.get("arguments")
                                if isinstance(args_str, str):
                                    new_args_str, count = redact_text(args_str, patterns, sess)
                                    if count > 0:
                                        fn_copy["arguments"] = new_args_str
                                        tc_copy["function"] = fn_copy
                                        changed = True
                                tc_copy["function"] = fn_copy
                                new_tool_calls.append(tc_copy)
                            else:
                                new_tool_calls.append(tc)
                        msg_copy["tool_calls"] = new_tool_calls

                    redacted_messages.append(msg_copy)
                else:
                    redacted_messages.append(msg)
            req["messages"] = redacted_messages

        # If anything was masked, make sure the model knows what the codes mean
        if sess.has_secrets and isinstance(req.get("messages"), list):
            notice = build_placeholder_notice(sess.prefix)
            has_notice = False
            for m in req["messages"]:
                if isinstance(m, dict) and m.get("role") == "system":
                    c = m.get("content", "")
                    if isinstance(c, str) and "[VibeGuard]" in c:
                        has_notice = True
                        break

            if not has_notice:
                injected = False
                for m in req["messages"]:
                    if isinstance(m, dict) and m.get("role") == "system":
                        m["content"] = f"{notice}\n\n{m.get('content', '')}"
                        injected = True
                        changed = True
                        break
                if not injected:
                    req["messages"].insert(0, {"role": "system", "content": notice})
                    changed = True

        if changed:
            return {"request": req, "source": "vibeguard"}
        return None

    def on_tool_request(**kwargs: Any) -> Optional[Dict[str, Any]]:
        """Intercept tool arguments before execution. Restore placeholders to real values."""
        args = kwargs.get("args")
        if not isinstance(args, dict):
            return None

        session_id = kwargs.get("session_id") or "default"
        sess = get_session(session_id, prefix=prefix, entities=entities)
        if not sess.has_secrets:
            return None

        restored_args, count = restore_deep(args, sess)
        if count > 0:
            return {"args": restored_args, "source": "vibeguard"}
        return None

    def on_transform_llm_output(response_text: str = "", **kwargs: Any) -> Optional[str]:
        """Restore placeholders in LLM's final response before delivering to user."""
        if not response_text or not isinstance(response_text, str):
            return None

        session_id = kwargs.get("session_id") or "default"
        sess = get_session(session_id, prefix=prefix, entities=entities)
        if not sess.has_secrets:
            return None

        restored, count = restore_text(response_text, sess)
        if count > 0:
            return restored
        return None

    def on_transform_tool_result(result: Any = "", **kwargs: Any) -> Optional[str]:
        """Redact sensitive plaintext in tool output before it enters message context."""
        if not isinstance(result, str) or not result:
            return None

        session_id = kwargs.get("session_id") or "default"
        sess = get_session(session_id, prefix=prefix, entities=entities)

        redacted, count = redact_text(result, patterns, sess)
        if count > 0:
            return redacted
        return None

    # Register middleware & hooks
    ctx.register_middleware("llm_request", on_llm_request)
    ctx.register_middleware("tool_request", on_tool_request)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
