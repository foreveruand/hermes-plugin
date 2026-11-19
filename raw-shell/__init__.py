"""raw-shell — /raw <command> for Hermes Agent.

Registers a single slash command, ``/raw``, that runs the rest of the message
as a shell command on the host where the Hermes gateway runs and returns the
output. No LLM is involved.

Why a plugin: plugins use the public register_command() API, so `hermes update`
never touches this code — unlike patching Hermes core or config-only
quick_commands (which take no arguments).

Authorization (fail closed):
- Only an explicit local surface (CLI / TUI / Desktop / ACP) is trusted;
  a missing, empty or unknown platform is treated as remote.
- Remote callers require the sender's stable user id to be on the allowlist.
  The allowlist is the union of ``plugins.entries.raw-shell.settings.allowed_users``
  (read through ``ctx.get_config``) and the ``RAW_SHELL_ALLOWED_USERS`` env var
  (comma/space separated, e.g. in ~/.hermes/.env):

      RAW_SHELL_ALLOWED_USERS=123456789

  With no allowlist configured, gateway use is denied.

Optional env tuning:
- RAW_SHELL_TIMEOUT   seconds before the process group is killed (default 120)
- RAW_SHELL_MAX_CHARS output characters kept (default 3900, Telegram-safe)
"""

import codecs
import logging
import os
import signal
import subprocess
import threading

log = logging.getLogger(__name__)

_LOCAL_PLATFORMS = {"local", "cli", "tui", "desktop", "acp"}
_REMOTE_HINTS = (
    "telegram", "discord", "slack", "whatsapp", "signal", "matrix",
    "mattermost", "teams", "google_chat", "dingtalk", "feishu", "wecom",
    "qq", "email", "home_assistant", "irc", "line", "sms",
)
_READ_CHUNK = 65536


def _timeout() -> int:
    try:
        return max(1, int(os.environ.get("RAW_SHELL_TIMEOUT", "120")))
    except ValueError:
        return 120


def _max_chars() -> int:
    try:
        return max(200, int(os.environ.get("RAW_SHELL_MAX_CHARS", "3900")))
    except ValueError:
        return 3900


def _split_users(value) -> set:
    """Normalize int/str/None/list config values into a set of trimmed ids."""
    if value is None:
        return set()
    items = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    users = set()
    for item in items:
        if item is None:
            continue
        for part in str(item).replace(",", " ").split():
            if part:
                users.add(part)
    return users


def _allowed_users(ctx) -> set:
    users = _split_users(os.environ.get("RAW_SHELL_ALLOWED_USERS", ""))
    if ctx is not None:
        try:
            users |= _split_users(ctx.get_config("allowed_users", []))
        except Exception:
            log.warning("raw-shell: could not read allowed_users from plugin config")
    return users


def _is_remote(platform: str, session_key: str) -> bool:
    # Defensive: a messaging-platform hint wins even if the surface claims local.
    key = (session_key or "").lower()
    if any(hint in key for hint in _REMOTE_HINTS):
        return True
    # Fail closed: only the known local terminal surfaces are local.
    return (platform or "").strip().lower() not in _LOCAL_PLATFORMS


def _kill_group(proc) -> None:
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _read_stream(stream, limit: int):
    """Drain *stream*, keeping only the first *limit* characters in memory."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    kept = []
    kept_len = 0
    total = 0
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if not text:
                continue
            total += len(text)
            if kept_len < limit:
                piece = text[: limit - kept_len]
                kept.append(piece)
                kept_len += len(piece)
        tail = decoder.decode(b"", final=True)
        if tail:
            total += len(tail)
            if kept_len < limit:
                kept.append(tail[: limit - kept_len])
    except Exception as exc:
        log.warning("raw-shell: error while reading output: %s", exc)
    finally:
        try:
            stream.close()
        except Exception:
            pass
    return "".join(kept), total


def _run(command: str, timeout: int, limit: int) -> str:
    popen_kwargs = {
        "shell": True,
        "executable": "/bin/bash",
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "cwd": os.path.expanduser("~"),
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except Exception as exc:  # spawn failure etc.
        return f"$ {command}\n❌ failed to run: {exc}"

    result = {"out": "", "total": 0}

    def _worker():
        result["out"], result["total"] = _read_stream(proc.stdout, limit)

    reader = threading.Thread(target=_worker, name="raw-shell-reader", daemon=True)
    reader.start()

    timed_out = False
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        code = None
    except Exception as exc:
        _kill_group(proc)
        return f"$ {command}\n❌ failed to run: {exc}"

    # The pipe closes once the process group is gone; don't hang if it lingers.
    reader.join(timeout=5)

    out, total = result["out"], result["total"]
    body = out.rstrip("\n")
    header = f"$ {command}"
    if not timed_out and code != 0:
        header += f"\n(exit {code})"
    text = f"{header}\n{body}" if body else f"{header}\n(no output)"
    if timed_out:
        text += f"\n⏱ timed out after {timeout}s; the process group was killed."
    if total > limit:
        text += f"\n… [truncated: showing first {limit} of {total} chars]"
    return text


def _build_handler(ctx):
    def _handle_raw(raw_args: str, **kwargs) -> str:
        command = (raw_args or "").strip()
        if not command:
            return "Usage: /raw <shell command>   e.g. /raw df -h"

        platform = str(kwargs.get("platform") or "")
        session_key = str(kwargs.get("session_key") or "")
        source = kwargs.get("source")
        user_id = ""
        if source is not None:
            user_id = str(getattr(source, "user_id", "") or "")

        if _is_remote(platform, session_key):
            allowed = _allowed_users(ctx)
            if not user_id or user_id not in allowed:
                log.warning(
                    "raw-shell: denied /raw from platform=%s user=%s cmd=%r",
                    platform or "?", user_id or "?", command[:120],
                )
                return (
                    "⛔ /raw is not enabled for your account on this gateway.\n"
                    "The owner can allow a user id in "
                    "plugins.entries.raw-shell.settings.allowed_users "
                    "or via RAW_SHELL_ALLOWED_USERS in the gateway environment."
                )

        timeout = _timeout()
        limit = _max_chars()
        log.info(
            "raw-shell: exec platform=%s user=%s cmd=%r",
            platform or "local", user_id or "-", command[:200],
        )
        try:
            return _run(command, timeout, limit)
        except Exception as exc:  # never raise into the command dispatcher
            log.exception("raw-shell: unexpected failure")
            return f"$ {command}\n❌ failed to run: {exc}"

    return _handle_raw


def register(ctx):
    ctx.register_command(
        "raw",
        handler=_build_handler(ctx),
        description="Run a shell command on the host (no LLM): /raw df -h",
        args_hint="<shell command>",
    )
