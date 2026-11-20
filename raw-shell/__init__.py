"""raw-shell — /raw (and /shell) <command> for Hermes Agent.

Registers ``/raw`` and its alias ``/shell``: both run the rest of the message
as a shell command on the host where the Hermes gateway runs and return the
output. No LLM is involved.

Why an alias: Hermes dispatches slash commands as
``quick_commands -> plugin commands -> bundles -> skills``. A ``quick_commands``
entry with the same name always wins, and an ``exec`` quick command never
receives the trailing text (its ``$@`` is empty), so ``/raw ls <dir>`` would
silently lose the argument. ``/shell`` is free of that legacy name so it
reaches this plugin even when ``quick_commands.raw`` still exists.

Why a plugin: plugins use the public register_command() API, so `hermes update`
never touches this code — unlike patching Hermes core or config-only
quick_commands (which take no arguments).

Access model:
There is no per-plugin user allowlist. Any surface that can reach the gateway
(CLI, Telegram, Discord, an unknown platform, ...) may run the command. The
gateway's own pairing / allowed-users settings are therefore the only boundary;
keep them tight. ``platform``, ``session_key`` and the sender id are read for
logging only and never gate execution.

Argument shape:
Hermes' slash-command contract calls ``handler(raw_args: str, **context)``, but
some surfaces and older dispatch paths hand the handler an ``args`` /
``command`` / ``text`` field (a string, dict or option list) instead. Extraction
is defensive on purpose: an empty container must never reach
``Popen(..., shell=True, executable="/bin/bash")``, which prints
``/bin/bash: -c: option requires an argument`` instead of a useful message.

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
from collections.abc import Mapping

log = logging.getLogger(__name__)

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


_COMMAND_KEYS = ("command", "text", "args", "arg", "raw_args", "input", "query", "prompt")
_MAX_UNWRAP = 4


def _coerce_command(value, _depth: int = 0) -> str:
    """Extract the command text from a dispatch payload.

    Accepts Hermes' documented ``raw_args`` string, plus the dict/list forms some
    surfaces use (``{"command": ...}``, ``{"text": ...}``, ``{"args": ...}``,
    ``["df", "-h"]``). Unknown objects yield ``""`` instead of ``repr(value)``:
    arbitrary text must never become a shell command.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace").strip()
    if _depth >= _MAX_UNWRAP:
        return ""
    if isinstance(value, Mapping):
        for key in _COMMAND_KEYS:
            if key in value:
                found = _coerce_command(value[key], _depth + 1)
                if found:
                    return found
        return ""
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, Mapping) and item.get("name") in _COMMAND_KEYS:
                # Discord-style option: {"name": "command", "value": "df -h"}.
                found = _coerce_command(item.get("value"), _depth + 1)
            else:
                found = _coerce_command(item, _depth + 1)
            if found:
                parts.append(found)
        return " ".join(parts).strip()
    return ""


def _command_from(raw_args, kwargs) -> str:
    """Prefer the positional argument, then the same fields carried as keywords."""
    command = _coerce_command(raw_args)
    if command:
        return command
    for key in _COMMAND_KEYS:
        if key in kwargs:
            command = _coerce_command(kwargs[key])
            if command:
                return command
    return ""


def _build_handler():
    def _handle_raw(raw_args=None, **kwargs) -> str:
        command = _command_from(raw_args, kwargs)
        if not command:
            return "Usage: /raw <shell command>   e.g. /raw df -h"

        platform = str(kwargs.get("platform") or "")
        session_key = str(kwargs.get("session_key") or "")
        source = kwargs.get("source")
        user_id = ""
        if source is not None:
            user_id = str(getattr(source, "user_id", "") or "")

        timeout = _timeout()
        limit = _max_chars()
        log.info(
            "raw-shell: exec platform=%s session=%s user=%s cmd=%r",
            platform or "local", session_key or "-", user_id or "-", command[:200],
        )
        try:
            return _run(command, timeout, limit)
        except Exception as exc:  # never raise into the command dispatcher
            log.exception("raw-shell: unexpected failure")
            return f"$ {command}\n❌ failed to run: {exc}"

    return _handle_raw


def register(ctx):
    handler = _build_handler()
    # /shell is the conflict-free name: a legacy ``quick_commands.raw`` (which
    # cannot receive arguments) takes precedence over plugin commands in
    # Hermes, so /raw may never reach this handler on such an install.
    for name, description in (
        ("raw", "Run a shell command on the host (no LLM): /raw df -h"),
        ("shell", "Run a shell command on the host (no LLM): /shell df -h "
                  "(alias for /raw; use it when quick_commands.raw shadows /raw)"),
    ):
        ctx.register_command(
            name,
            handler=handler,
            description=description,
            args_hint="<shell command>",
        )
    log.info("raw-shell: registered /raw and /shell (quick_commands with the same name take precedence)")
