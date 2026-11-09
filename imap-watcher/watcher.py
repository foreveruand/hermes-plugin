"""
Hermes IMAP IDLE Email Watcher Plugin.
Real-time push notifications for incoming emails via IMAP IDLE.

Architecture:
- Single connection per account: multiplexes multiple folders over a single
  IMAP TCP/TLS connection. Eliminates connection resets caused by excessive
  concurrent worker threads.
- Primary folder (default INBOX) is monitored in real-time via IMAP IDLE.
- Auxiliary folders (e.g. Junk, Spam, verification codes / 验证码) are polled
  sequentially over the same connection on IDLE heartbeat timeouts or incoming mail events.
- Per-folder watermarks (UIDVALIDITY + UID), account-wide Message-ID dedup,
  age-window filtering, burst protection, strict deduplication, asynchronous
  notification queue, and clean single-worker lifecycle guarantees.
- Telegram notifications use HTML formatting with collapsed <blockquote expandable>
  body, safe truncation, and per-message link-preview suppression.
"""

from __future__ import annotations

import datetime
import email
from email.header import decode_header
import email.utils
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import imapclient

logger = logging.getLogger("imap-watcher")

PLUGIN_PROFILE_DIR = Path(__file__).resolve().parent.parent.parent
PROFILE_NAME = os.environ.get(
    "HERMES_PROFILE",
    PLUGIN_PROFILE_DIR.name if PLUGIN_PROFILE_DIR.parent.name == "profiles" else "default",
)
CONFIG_PATH = PLUGIN_PROFILE_DIR / "email_accounts.json"
if not CONFIG_PATH.exists():
    env_home = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
    env_profile = os.environ.get("HERMES_PROFILE")
    if env_profile and (env_home / "profiles" / env_profile / "email_accounts.json").exists():
        CONFIG_PATH = env_home / "profiles" / env_profile / "email_accounts.json"
    elif (env_home / "email_accounts.json").exists():
        CONFIG_PATH = env_home / "email_accounts.json"
STATE_PATH = PLUGIN_PROFILE_DIR / "cache" / "email_state.json"

# Special-use folder flags that must never be pushed. Junk/Spam is deliberately
# NOT excluded so misclassified mail still reaches the user.
_EXCLUDE_FOLDER_FLAGS: Set[bytes] = {
    b"\\Sent",
    b"\\Drafts",
    b"\\Trash",
    b"\\All",
    b"\\Noselect",
}

# Telegram hard limit is 4096 UTF-16 units; keep a safety margin so the
# tag-unaware `hermes send` chunker never splits our HTML tags mid-way.
_TELEGRAM_SAFE_LIMIT = 4000

# State & In-flight locks
_STATE_LOCK = threading.RLock()
_IN_FLIGHT_KEYS: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()
_TOKEN_LOCK = threading.Lock()

# Single shared durable state
_STATE: dict = {"version": 2, "folders": {}, "accounts": {}}

# Lifecycle management (Single worker & connection per account)
_STOP_EVENT = threading.Event()
_ACTIVE_CLIENTS: dict[str, imapclient.IMAPClient] = {}       # account name -> client
_ACTIVE_WORKERS: dict[str, threading.Thread] = {}            # account name -> worker thread
_WORKER_GENERATIONS: dict[str, int] = {}                     # account name -> generation
_LIFECYCLE_LOCK = threading.Lock()

# Notification Queue & Worker
_NOTIFICATION_QUEUE: queue.Queue[Tuple[str, str]] = queue.Queue()
_NOTIFICATION_WORKER: Optional[threading.Thread] = None

# Runtime statuses for /mailstatus
_ACCOUNT_STATUSES: dict[str, dict[str, Any]] = {}

# In-memory deduplication caches (synced with state file)
_PROCESSED_UIDS: dict[str, set[int]] = {}       # worker_id -> UIDs
_PROCESSED_MSG_IDS: dict[str, set[str]] = {}    # account name -> Message-IDs (account-wide)


def _decode_str(raw: str | bytes | None) -> str:
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        parts = decode_header(raw)
        decoded = []
        for content, encoding in parts:
            if isinstance(content, bytes):
                encoding = encoding or "utf-8"
                if encoding.lower() in ("gb2312", "gbk"):
                    encoding = "gb18030"
                try:
                    decoded.append(content.decode(encoding, errors="ignore"))
                except Exception:
                    decoded.append(content.decode("utf-8", errors="ignore"))
            else:
                decoded.append(str(content))
        return "".join(decoded).strip()
    except Exception:
        return str(raw).strip()


def _clean_html_text(raw_html: str) -> str:
    """Convert email HTML to readable plain text.

    Non-content blocks (script/style/head/title/meta/link/noscript) are removed
    wholesale so their raw source can never leak into the notification as text.
    """
    if not raw_html:
        return ""
    text = raw_html
    # HTML comments
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    # Whole non-content blocks (paired tags)
    text = re.sub(
        r"<(script|style|head|title|noscript)\b[^>]*>.*?</\1\s*>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Any leftover (unclosed) non-content / void tags
    text = re.sub(
        r"</?(script|style|head|title|meta|link|base|noscript|iframe|svg)\b[^>]*>",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    # Block-level tags -> newline (opening and closing forms)
    text = re.sub(
        r"</?\s*(br|p|div|tr|li|h[1-6]|table|thead|tbody|section|article|blockquote|ul|ol|hr)\b[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    # Remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    result = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _clean_plain_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    result = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _extract_full_body(msg_bytes: bytes, max_len: int = 20000) -> str:
    """Extract a cleaned, readable body. Truncation to fit Telegram happens later
    in ``_build_notification`` so HTML tags stay balanced."""
    try:
        msg = email.message_from_bytes(msg_bytes)
        body = ""
        html_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                if "attachment" in disp.lower():
                    continue
                if ctype == "text/plain" and not body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="ignore")
                elif ctype == "text/html" and not html_body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html_body = payload.decode(charset, errors="ignore")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                if msg.get_content_type() == "text/html":
                    html_body = payload.decode(charset, errors="ignore")
                else:
                    body = payload.decode(charset, errors="ignore")

        cleaned_plain = _clean_plain_text(body)
        cleaned_html = _clean_html_text(html_body)
        final_text = cleaned_plain or cleaned_html
        return final_text[:max_len]
    except Exception as exc:
        logger.warning(f"Failed to parse body: {exc}")
        return ""


def _parse_email_datetime(raw_date: Any) -> Optional[datetime.datetime]:
    if not raw_date:
        return None
    if isinstance(raw_date, datetime.datetime):
        if raw_date.tzinfo is None:
            raw_date = raw_date.replace(tzinfo=datetime.timezone.utc)
        return raw_date
    if isinstance(raw_date, (bytes, str)):
        date_str = _decode_str(raw_date)
        try:
            dt = email.utils.parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except Exception:
            pass
    return None


def _format_datetime(dt: Optional[datetime.datetime]) -> str:
    if not dt:
        return time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        # User local timezone is CST (UTC+8)
        cst = datetime.timezone(datetime.timedelta(hours=8))
        return dt.astimezone(cst).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def _is_email_too_old(msg_dt: Optional[datetime.datetime], max_age_hours: float) -> bool:
    if not msg_dt:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    age_seconds = (now - msg_dt).total_seconds()
    # Allow small future clock skew (up to 10 minutes)
    if age_seconds < -600:
        return False
    return age_seconds > (max_age_hours * 3600)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units (the unit Telegram uses for its limits)."""
    return len(text.encode("utf-16-le")) // 2


def _truncate_utf16(text: str, max_units: int) -> str:
    """Truncate to at most ``max_units`` UTF-16 units, preferring a line boundary."""
    if max_units <= 0:
        return ""
    if _utf16_len(text) <= max_units:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _utf16_len(text[:mid]) <= max_units:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    newline = cut.rfind("\n")
    if newline > lo * 0.6:
        cut = cut[:newline]
    return cut


def _build_notification(
    name: str,
    folder: str,
    sender: str,
    subject: str,
    date_display: str,
    body: str,
    limit: int = _TELEGRAM_SAFE_LIMIT,
) -> str:
    """Render the Telegram HTML notification, keeping the whole message under the
    Telegram limit and the body inside a collapsed expandable blockquote."""
    esc = html.escape
    head = (
        f"📬 <b>新邮件提醒【{esc(name)}】</b>\n\n"
        f"• <b>发件人</b>: <code>{esc(sender)}</code>\n"
        f"• <b>主题</b>: <b>{esc(subject)}</b>\n"
        f"• <b>时间</b>: <code>{esc(date_display)}</code>\n"
        f"• <b>文件夹</b>: <code>{esc(folder)}</code>\n\n"
    )
    if not body:
        return head + "<i>（无正文内容）</i>"

    open_tag = "<blockquote expandable>"
    close_tag = "</blockquote>"
    note = "\n…（正文过长，已截断，完整内容请查看邮箱）"

    esc_body = esc(body)
    budget = limit - _utf16_len(head) - _utf16_len(open_tag) - _utf16_len(close_tag)
    if _utf16_len(esc_body) <= budget:
        return head + open_tag + esc_body + close_tag

    body_budget = budget - _utf16_len(note)
    if body_budget < 50:
        body_budget = max(0, budget // 2)
    return head + open_tag + _truncate_utf16(esc_body, body_budget) + note + close_tag


# --------------------------------------------------------------------------- #
# Durable state
# --------------------------------------------------------------------------- #

def _empty_state() -> dict:
    return {"version": 2, "folders": {}, "accounts": {}}


def _migrate_state(raw: dict) -> dict:
    """Normalise any historical on-disk state into the v2 schema."""
    if not isinstance(raw, dict):
        return _empty_state()
    if raw.get("version") == 2 and "folders" in raw:
        state = _empty_state()
        state["folders"] = dict(raw.get("folders") or {})
        state["accounts"] = dict(raw.get("accounts") or {})
        return state

    # Legacy: {account_or_user: {last_seen_uid, uidvalidity, recent_uids, recent_msg_ids}}
    state = _empty_state()
    for key, val in raw.items():
        if isinstance(val, int):
            state["folders"][f"{key}|INBOX"] = {
                "last_seen_uid": val,
                "uidvalidity": None,
                "recent_uids": [val] if val else [],
            }
        elif isinstance(val, dict):
            state["folders"][f"{key}|INBOX"] = {
                "last_seen_uid": val.get("last_seen_uid"),
                "uidvalidity": val.get("uidvalidity"),
                "recent_uids": list(val.get("recent_uids") or []),
            }
            if val.get("recent_msg_ids"):
                state["accounts"][key] = {"recent_msg_ids": list(val.get("recent_msg_ids"))}
    logger.info("Migrated legacy email state to schema v2")
    return state


def _load_state() -> dict:
    """Populate the shared ``_STATE`` (and in-memory caches) from disk."""
    global _STATE
    with _STATE_LOCK:
        loaded = _empty_state()
        if STATE_PATH.exists():
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    loaded = _migrate_state(json.load(f))
            except Exception as exc:
                logger.warning(f"Error loading state from {STATE_PATH}: {exc}")
        _STATE = loaded

        _PROCESSED_UIDS.clear()
        _PROCESSED_MSG_IDS.clear()
        for worker_id, folder_data in _STATE.get("folders", {}).items():
            if not isinstance(folder_data, dict):
                continue
            uids = list(folder_data.get("recent_uids") or [])
            last_uid = folder_data.get("last_seen_uid")
            if last_uid and last_uid not in uids:
                uids.append(last_uid)
            _PROCESSED_UIDS[worker_id] = set(uids)
        for account_name, acc_data in _STATE.get("accounts", {}).items():
            if isinstance(acc_data, dict):
                _PROCESSED_MSG_IDS[account_name] = set(acc_data.get("recent_msg_ids") or [])
        return _STATE


def _save_state():
    with _STATE_LOCK:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(_STATE, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"Failed to save state to {STATE_PATH}: {exc}")


def _record_processed_item(
    worker_id: str,
    account_name: str,
    uid: int,
    msg_id: Optional[str],
    uidvalidity: Optional[int],
) -> bool:
    """Atomically record a processed item.

    UIDs are tracked per folder (worker_id); Message-IDs are tracked per account
    so the same email appearing in multiple folders/labels is only pushed once.
    Returns ``True`` when the Message-ID is new (i.e. the caller should notify).
    """
    with _STATE_LOCK:
        is_new_id = True
        if msg_id:
            ids = _PROCESSED_MSG_IDS.setdefault(account_name, set())
            if msg_id in ids:
                is_new_id = False
            else:
                ids.add(msg_id)

        uids = _PROCESSED_UIDS.setdefault(worker_id, set())
        uids.add(uid)
        recent_uids = sorted(uids)[-200:]
        _PROCESSED_UIDS[worker_id] = set(recent_uids)

        recent_msg_ids = list(_PROCESSED_MSG_IDS.get(account_name, set()))[-200:]
        _PROCESSED_MSG_IDS[account_name] = set(recent_msg_ids)

        folder_data = _STATE.setdefault("folders", {}).get(worker_id, {})
        last_seen = max(folder_data.get("last_seen_uid", 0) or 0, uid)
        _STATE["folders"][worker_id] = {
            "last_seen_uid": last_seen,
            "uidvalidity": uidvalidity or folder_data.get("uidvalidity"),
            "recent_uids": recent_uids,
        }
        _STATE.setdefault("accounts", {})[account_name] = {"recent_msg_ids": recent_msg_ids}
        _save_state()

        status = _ACCOUNT_STATUSES.get(worker_id)
        if status is not None:
            status["last_seen_uid"] = last_seen
        return is_new_id


def _get_folder_watermark(state: dict, worker_id: str) -> tuple[int, Optional[int]]:
    folder_data = (state.get("folders") or {}).get(worker_id)
    if isinstance(folder_data, dict):
        last_uid = folder_data.get("last_seen_uid")
        if last_uid is not None:
            return int(last_uid), folder_data.get("uidvalidity")
        known = folder_data.get("recent_uids") or []
        if known:
            return int(max(known)), folder_data.get("uidvalidity")
    return 0, None


def _get_outlook_access_token() -> Optional[str]:
    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
    try:
        import sys
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import outlook_oauth
        token = outlook_oauth.get_valid_access_token()
        if token:
            return token
    except Exception as exc:
        logger.debug(f"Direct import of outlook_oauth failed ({exc}), falling back to CLI")

    script = scripts_dir / "outlook_oauth.py"
    if script.exists():
        res = subprocess.run(
            ["python3", str(script), "refresh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25,
        )
        if res.returncode == 0 and "SUCCESS" in res.stdout:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                for acc in cfg.get("accounts", []):
                    if acc.get("name") == "Outlook":
                        return acc.get("access_token")
    return None


def _connect_account(acc: dict) -> imapclient.IMAPClient:
    """Open and authenticate one IMAP connection for the given account."""
    user = acc.get("user", "")
    host = acc.get("host", "")
    port = acc.get("port", 993)
    ssl_on = acc.get("ssl", True)
    auth_type = acc.get("auth_type", "password")

    client = imapclient.IMAPClient(host, port=port, ssl=ssl_on, timeout=30)
    try:
        if auth_type == "oauth2":
            token = acc.get("access_token")
            exp = acc.get("token_expires_at", 0)
            if not token or time.time() > (exp - 180):
                with _TOKEN_LOCK:
                    token = acc.get("access_token")
                    exp = acc.get("token_expires_at", 0)
                    if not token or time.time() > (exp - 180):
                        token = _get_outlook_access_token()
                        if token:
                            acc["access_token"] = token
                            acc["token_expires_at"] = time.time() + 2700
            if not token:
                raise RuntimeError("No valid OAuth2 access token for Outlook")
            client.oauth2_login(user, token)
        else:
            client.login(user, acc.get("password", ""))
    except Exception:
        try:
            client.logout()
        except Exception:
            pass
        raise
    return client


def _list_monitor_folders(client: imapclient.IMAPClient, extra_excludes: List[str]) -> List[str]:
    """Return selectable folder names, excluding Sent/Drafts/Trash/All and any
    user-configured substrings. Junk/Spam and label-style folders are kept."""
    folders: List[str] = []
    try:
        listing = client.list_folders()
    except Exception as exc:
        logger.warning(f"list_folders failed: {exc}")
        return folders

    lowered_excludes = [e.strip().lower() for e in extra_excludes if e and e.strip()]
    for flags, _delimiter, name in listing:
        flag_set = set(flags or ())
        if flag_set & _EXCLUDE_FOLDER_FLAGS:
            continue
        low = name.lower()
        if any(pat in low for pat in lowered_excludes):
            continue
        folders.append(name)
    return folders


def _notification_worker_loop():
    """Single consumer thread processing notification queue to prevent process storms."""
    logger.info("Notification worker loop started")
    while True:
        try:
            item = _NOTIFICATION_QUEUE.get(timeout=1.0)
        except queue.Empty:
            if _STOP_EVENT.is_set():
                break
            continue

        target, msg_text = item
        cmd = ["hermes", "-p", PROFILE_NAME, "send", "--to", target, msg_text]
        env = os.environ.copy()
        env["HERMES_PROFILE"] = PROFILE_NAME
        env["HERMES_HOME"] = str(PLUGIN_PROFILE_DIR)

        for attempt in range(1, 4):
            try:
                res = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    env=env,
                )
                if res.returncode == 0:
                    logger.info("Email notification delivered successfully via hermes send")
                    break
                else:
                    logger.warning(
                        f"hermes send attempt {attempt}/3 failed (exit {res.returncode}): {res.stderr.strip()}"
                    )
                    if attempt < 3:
                        time.sleep(2)
            except Exception as exc:
                logger.error(f"Exception executing hermes send attempt {attempt}/3: {exc}")
                if attempt < 3:
                    time.sleep(2)
        else:
            logger.error(f"Failed to deliver notification to {target} after 3 attempts")
        _NOTIFICATION_QUEUE.task_done()
    logger.info("Notification worker loop stopped")


def _check_and_process_folder(
    c: imapclient.IMAPClient,
    acc: dict,
    folder: str,
    target: str,
    max_age_hours: float,
    max_batch_push: int,
    generation: int,
    is_primary: bool = False,
) -> int:
    """Inspect one folder over the provided IMAP connection, update watermarks,
    and queue notifications for newly detected emails."""
    name = acc.get("name", "Unknown")
    user = acc.get("user", "")
    worker_id = f"{name}|{folder}"

    with _LIFECYCLE_LOCK:
        if _WORKER_GENERATIONS.get(name) != generation or _STOP_EVENT.is_set():
            return 0

    c.select_folder(folder)

    folder_status = c.folder_status(folder, ["UIDVALIDITY"])
    curr_uidvalidity = folder_status.get(b"UIDVALIDITY")

    state = _load_state()
    last_seen_uid, saved_uidvalidity = _get_folder_watermark(state, worker_id)

    # Handle UIDVALIDITY change or uninitialized watermark
    if saved_uidvalidity and curr_uidvalidity and saved_uidvalidity != curr_uidvalidity:
        logger.warning(
            f"[{worker_id}] UIDVALIDITY changed ({saved_uidvalidity} -> {curr_uidvalidity}). "
            f"Resetting watermark to current max UID to prevent flooding."
        )
        all_uids = c.search(["ALL"])
        last_seen_uid = max(all_uids) if all_uids else 0
        _record_processed_item(worker_id, name, last_seen_uid, None, curr_uidvalidity)
    elif last_seen_uid <= 0:
        all_uids = c.search(["ALL"])
        last_seen_uid = max(all_uids) if all_uids else 0
        _record_processed_item(worker_id, name, last_seen_uid, None, curr_uidvalidity)
        logger.info(f"[{worker_id}] Initialized baseline watermark last_seen_uid={last_seen_uid}")

    folder_mode = "IDLE (Primary)" if is_primary else "Polling (Multiplexed)"
    _ACCOUNT_STATUSES[worker_id] = {
        "status": folder_mode,
        "account": name,
        "folder": folder,
        "user": user,
        "last_seen_uid": last_seen_uid,
        "last_event": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": None,
    }

    # Search for UIDs strictly greater than last_seen_uid
    search_res = c.search(["UID", f"{last_seen_uid + 1}:*"])
    seen_set = _PROCESSED_UIDS.get(worker_id, set())
    new_uids = sorted([u for u in search_res if u > last_seen_uid and u not in seen_set])

    if not new_uids:
        return 0

    logger.info(f"[{worker_id}] Detected {len(new_uids)} new UID(s) > {last_seen_uid}: {new_uids}")

    # Burst/storm protection
    if len(new_uids) > max_batch_push:
        logger.warning(
            f"[{worker_id}] Burst of {len(new_uids)} emails detected (> max_batch_push={max_batch_push}). "
            f"Activating burst protection."
        )
        alert_msg = (
            f"⚠️ <b>邮件提醒【{html.escape(name)}】</b>\n\n"
            f"文件夹 <code>{html.escape(folder)}</code> 检测到突发变动 {len(new_uids)} 封邮件，"
            f"已触发防误风暴保护。\n"
            f"为避免刷屏，已自动跳过历史积压推送并将水位线同步至最新"
            f"（UID: <code>{max(new_uids)}</code>）。"
        )
        for u in new_uids:
            _record_processed_item(worker_id, name, u, None, curr_uidvalidity)
        last_seen_uid = max(new_uids)
        _ACCOUNT_STATUSES[worker_id]["last_seen_uid"] = last_seen_uid
        if target:
            _NOTIFICATION_QUEUE.put((target, alert_msg))
        return len(new_uids)

    envelope_data = c.fetch(new_uids, ["ENVELOPE", "INTERNALDATE"])

    for uid in new_uids:
        with _LIFECYCLE_LOCK:
            if _WORKER_GENERATIONS.get(name) != generation or _STOP_EVENT.is_set():
                break

        in_flight_key = f"{worker_id}:{uid}"
        with _IN_FLIGHT_LOCK:
            if in_flight_key in _IN_FLIGHT_KEYS or uid in _PROCESSED_UIDS.get(worker_id, set()):
                continue
            _IN_FLIGHT_KEYS.add(in_flight_key)

        try:
            item_meta = envelope_data.get(uid, {})
            envelope = item_meta.get(b"ENVELOPE")
            internal_date = item_meta.get(b"INTERNALDATE")

            msg_dt = _parse_email_datetime(internal_date) or (
                _parse_email_datetime(envelope.date) if envelope else None
            )

            # Age filter check
            if _is_email_too_old(msg_dt, max_age_hours):
                date_display = _format_datetime(msg_dt)
                logger.warning(
                    f"[{worker_id}] 忽略历史陈旧邮件 UID {uid}（时间: {date_display}，"
                    f"超出时间窗口 {max_age_hours}h）"
                )
                _record_processed_item(worker_id, name, uid, None, curr_uidvalidity)
                last_seen_uid = max(last_seen_uid, uid)
                _ACCOUNT_STATUSES[worker_id]["last_seen_uid"] = last_seen_uid
                continue

            raw_msg_id = ""
            if envelope and envelope.message_id:
                raw_msg_id = _decode_str(envelope.message_id)
            if not raw_msg_id:
                raw_sub = envelope.subject.decode(errors="ignore") if (envelope and envelope.subject) else ""
                fp_raw = f"{name}:{folder}:{raw_sub}:{internal_date}:{uid}"
                raw_msg_id = f"fp:{hashlib.sha256(fp_raw.encode()).hexdigest()[:24]}"

            # Atomically persist watermark + dedup
            is_new = _record_processed_item(worker_id, name, uid, raw_msg_id, curr_uidvalidity)
            last_seen_uid = max(last_seen_uid, uid)
            _ACCOUNT_STATUSES[worker_id]["last_seen_uid"] = last_seen_uid

            if not is_new:
                logger.info(f"[{worker_id}] 检测到已处理邮件指纹 {raw_msg_id}，跳过 UID {uid}")
                continue

            body_fetch = c.fetch([uid], ["BODY.PEEK[]"])
            raw_body = b""
            if uid in body_fetch:
                raw_body = body_fetch[uid].get(b"BODY[]") or body_fetch[uid].get(b"BODY.PEEK[]") or b""

            subject = "无主题"
            sender = "未知发件人"
            date_display = _format_datetime(msg_dt)

            if envelope:
                if envelope.subject:
                    subject = _decode_str(envelope.subject)
                if envelope.from_ and len(envelope.from_) > 0:
                    f_obj = envelope.from_[0]
                    name_part = _decode_str(f_obj.name) if f_obj.name else ""
                    mbox = _decode_str(f_obj.mailbox) if f_obj.mailbox else ""
                    host_p = _decode_str(f_obj.host) if f_obj.host else ""
                    mail_addr = f"{mbox}@{host_p}" if host_p else mbox
                    sender = f"{name_part} <{mail_addr}>" if name_part else mail_addr

            full_body = _extract_full_body(raw_body)
            msg_text = _build_notification(
                name=name,
                folder=folder,
                sender=sender,
                subject=subject,
                date_display=date_display,
                body=full_body,
            )

            if target:
                _NOTIFICATION_QUEUE.put((target, msg_text))
                logger.info(f"[{worker_id}] 成功入队新邮件提醒: UID {uid} - {subject}")
            else:
                logger.warning(f"[{worker_id}] 检测到新邮件但未配置推送目标 (target 为空)，跳过推送")

        finally:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT_KEYS.discard(in_flight_key)

    return len(new_uids)


def _run_account_worker(
    acc: dict,
    target: str,
    global_max_age: float,
    global_max_batch: int,
    generation: int,
):
    """Single-connection worker for an IMAP account.

    Reuses one authenticated TCP/TLS IMAP connection across all monitored folders:
    - Primary folder (default INBOX) is monitored via IMAP IDLE.
    - Auxiliary folders are polled over the SAME connection on IDLE heartbeat
      returns or when new messages arrive.
    """
    name = acc.get("name", "Unknown")
    user = acc.get("user", "")
    max_age_hours = float(acc.get("max_age_hours", global_max_age))
    max_batch_push = int(acc.get("max_batch_push", global_max_batch))
    resync_seconds = float(acc.get("folder_resync_seconds", 300))
    idle_heartbeat = float(acc.get("idle_heartbeat_seconds", 30))
    extra_excludes = list(acc.get("exclude_folders", []) or [])
    configured_folders = [f for f in acc.get("folders", []) if f]
    summary_key = f"{name}|__account__"

    _ACCOUNT_STATUSES[summary_key] = {
        "status": "Starting",
        "account": name,
        "folder": "*",
        "user": user,
        "last_seen_uid": 0,
        "last_event": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": None,
    }

    logger.info(
        f"[{name}] Starting single-connection IMAP worker (gen={generation}, max_age={max_age_hours}h, "
        f"max_batch={max_batch_push}, heartbeat={idle_heartbeat}s)"
    )

    while not _STOP_EVENT.is_set():
        with _LIFECYCLE_LOCK:
            if _WORKER_GENERATIONS.get(name) != generation:
                logger.info(f"[{name}] Obsolete worker generation {generation}, exiting thread")
                return

        client: Optional[imapclient.IMAPClient] = None
        try:
            _ACCOUNT_STATUSES[summary_key]["status"] = "Connecting"
            _ACCOUNT_STATUSES[summary_key]["last_event"] = time.strftime("%Y-%m-%d %H:%M:%S")
            c = _connect_account(acc)
            client = c

            with _LIFECYCLE_LOCK:
                if _WORKER_GENERATIONS.get(name) != generation or _STOP_EVENT.is_set():
                    try:
                        c.logout()
                    except Exception:
                        pass
                    return
                _ACTIVE_CLIENTS[name] = c

            has_idle = c.has_capability("IDLE")

            # Resolve monitored folder list
            if configured_folders:
                monitored_folders = list(configured_folders)
            else:
                monitored_folders = _list_monitor_folders(c, extra_excludes)
            if not monitored_folders:
                monitored_folders = ["INBOX"]

            # Choose primary folder (prefer case-insensitive INBOX)
            primary_folder = "INBOX"
            for f in monitored_folders:
                if f.upper() == "INBOX":
                    primary_folder = f
                    break
            else:
                primary_folder = monitored_folders[0]

            auxiliary_folders = [f for f in monitored_folders if f != primary_folder]

            logger.info(
                f"[{name}] Single connection ready. Primary IDLE folder: '{primary_folder}', "
                f"auxiliary folders ({len(auxiliary_folders)}): {auxiliary_folders}, has_idle={has_idle}"
            )

            # Initial baseline sync (Catch-up) for all folders
            _ACCOUNT_STATUSES[summary_key]["status"] = "Syncing baseline"
            _ACCOUNT_STATUSES[summary_key]["last_event"] = time.strftime("%Y-%m-%d %H:%M:%S")

            _check_and_process_folder(
                c, acc, primary_folder, target, max_age_hours, max_batch_push, generation, is_primary=True
            )
            for aux in auxiliary_folders:
                if _STOP_EVENT.is_set():
                    break
                _check_and_process_folder(
                    c, acc, aux, target, max_age_hours, max_batch_push, generation, is_primary=False
                )

            # Switch back to primary folder for IDLE loop
            c.select_folder(primary_folder)
            last_resync_time = time.time()

            # Main multiplexed listening loop
            while not _STOP_EVENT.is_set():
                with _LIFECYCLE_LOCK:
                    if _WORKER_GENERATIONS.get(name) != generation:
                        break

                _ACCOUNT_STATUSES[summary_key]["status"] = (
                    f"IDLE on {primary_folder} (Multiplexing {len(monitored_folders)} folders)"
                )
                _ACCOUNT_STATUSES[summary_key]["last_event"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _ACCOUNT_STATUSES[summary_key]["error"] = None

                if has_idle:
                    c.idle()
                    # Heartbeat unblocks periodically (default 30s) or on new mail events
                    responses = c.idle_check(timeout=idle_heartbeat)
                    c.idle_done()
                else:
                    for _ in range(int(idle_heartbeat)):
                        if _STOP_EVENT.is_set():
                            break
                        time.sleep(1)

                with _LIFECYCLE_LOCK:
                    if _WORKER_GENERATIONS.get(name) != generation or _STOP_EVENT.is_set():
                        break

                # 1. Process primary folder new messages
                _check_and_process_folder(
                    c, acc, primary_folder, target, max_age_hours, max_batch_push, generation, is_primary=True
                )

                # 2. Sequentially poll auxiliary folders over the SAME connection
                if auxiliary_folders:
                    for aux in auxiliary_folders:
                        if _STOP_EVENT.is_set():
                            break
                        with _LIFECYCLE_LOCK:
                            if _WORKER_GENERATIONS.get(name) != generation:
                                break
                        _check_and_process_folder(
                            c, acc, aux, target, max_age_hours, max_batch_push, generation, is_primary=False
                        )
                    # Switch connection back to primary folder
                    c.select_folder(primary_folder)

                # 3. Periodically resync dynamic folder list if not explicitly configured
                if not configured_folders and (time.time() - last_resync_time > resync_seconds):
                    latest_folders = _list_monitor_folders(c, extra_excludes)
                    if latest_folders and set(latest_folders) != set(monitored_folders):
                        logger.info(f"[{name}] Detected folder list change: {monitored_folders} -> {latest_folders}")
                        monitored_folders = latest_folders
                        if primary_folder not in monitored_folders:
                            for f in monitored_folders:
                                if f.upper() == "INBOX":
                                    primary_folder = f
                                    break
                            else:
                                primary_folder = monitored_folders[0]
                        auxiliary_folders = [f for f in monitored_folders if f != primary_folder]
                        c.select_folder(primary_folder)
                    last_resync_time = time.time()

        except Exception as exc:
            with _LIFECYCLE_LOCK:
                if _WORKER_GENERATIONS.get(name) != generation or _STOP_EVENT.is_set():
                    return
            _ACCOUNT_STATUSES[summary_key]["status"] = "Error"
            _ACCOUNT_STATUSES[summary_key]["error"] = str(exc)
            _ACCOUNT_STATUSES[summary_key]["last_event"] = time.strftime("%Y-%m-%d %H:%M:%S")
            logger.warning(f"[{name}] IMAP connection error: {exc}")

            if client:
                try:
                    client.logout()
                except Exception:
                    pass

            # Backoff 5s before reconnecting
            for _ in range(5):
                if _STOP_EVENT.is_set():
                    return
                with _LIFECYCLE_LOCK:
                    if _WORKER_GENERATIONS.get(name) != generation:
                        return
                time.sleep(1)

        finally:
            with _LIFECYCLE_LOCK:
                _ACTIVE_CLIENTS.pop(name, None)
            if client:
                try:
                    client.logout()
                except Exception:
                    pass

    _ACCOUNT_STATUSES[summary_key]["status"] = "Stopped"
    logger.info(f"[{name}] IMAP worker stopped cleanly")


def start_watcher():
    global _NOTIFICATION_WORKER
    stop_watcher()

    _STOP_EVENT.clear()

    if not CONFIG_PATH.exists():
        logger.warning(f"Config file not found: {CONFIG_PATH}")
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    target = cfg.get("target", "")
    if not target:
        logger.warning(
            "No notification target configured in email_accounts.json ('target' field missing). Notifications will be skipped."
        )
    global_max_age = float(cfg.get("max_age_hours", 24))
    global_max_batch = int(cfg.get("max_batch_push", 5))

    _load_state()

    if _NOTIFICATION_WORKER is None or not _NOTIFICATION_WORKER.is_alive():
        _NOTIFICATION_WORKER = threading.Thread(
            target=_notification_worker_loop,
            name="imap-notification-worker",
            daemon=True,
        )
        _NOTIFICATION_WORKER.start()

    accounts = [a for a in cfg.get("accounts", []) if a.get("enabled", False)]

    with _LIFECYCLE_LOCK:
        for acc in accounts:
            name = acc.get("name", "Unknown")
            gen = _WORKER_GENERATIONS.get(name, 0) + 1
            _WORKER_GENERATIONS[name] = gen

            t = threading.Thread(
                target=_run_account_worker,
                args=(acc, target, global_max_age, global_max_batch, gen),
                name=f"imap-watcher-{name}",
                daemon=True,
            )
            t.start()
            _ACTIVE_WORKERS[name] = t

    logger.info(f"Spawned {len(_ACTIVE_WORKERS)} IMAP account worker thread(s) (single-connection multi-folder)")


def stop_watcher():
    global _NOTIFICATION_WORKER
    _STOP_EVENT.set()

    with _LIFECYCLE_LOCK:
        # Forcefully terminate socket connections to unblock idle_check immediately
        for name, client in list(_ACTIVE_CLIENTS.items()):
            try:
                if client and hasattr(client, "_imap") and client._imap and client._imap.sock:
                    try:
                        client._imap.sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    client._imap.sock.close()
            except Exception:
                pass
        _ACTIVE_CLIENTS.clear()

        # Join account worker threads with brief timeout
        for name, t in list(_ACTIVE_WORKERS.items()):
            if t.is_alive():
                t.join(timeout=2.0)
        _ACTIVE_WORKERS.clear()
        _WORKER_GENERATIONS.clear()

    if _NOTIFICATION_WORKER and _NOTIFICATION_WORKER.is_alive():
        _NOTIFICATION_WORKER.join(timeout=2.0)
    _NOTIFICATION_WORKER = None

    logger.info("IMAP watcher threads stopped cleanly")


def get_status_report() -> str:
    if not _ACCOUNT_STATUSES:
        return "ℹ️ 当前未启动任何邮箱长连接监听。"

    groups: dict[str, list[tuple[str, dict]]] = {}
    for worker_id, st in _ACCOUNT_STATUSES.items():
        groups.setdefault(st.get("account", "Unknown"), []).append((worker_id, st))

    lines = ["📊 **IMAP 单连接多文件夹监听状态**\n"]
    for account in sorted(groups):
        lines.append(f"\n**📮 {account}**")
        entries = sorted(groups[account], key=lambda x: str(x[1].get("folder", "")))
        for worker_id, st in entries:
            folder = st.get("folder", "")
            status = st.get("status", "Unknown")
            last_event = st.get("last_event", "")
            watermark = st.get("last_seen_uid", 0)
            cached_uids = len(_PROCESSED_UIDS.get(worker_id, set()))
            err = st.get("error")

            if folder == "*":
                lines.append(f"  - 运行: **{status}**（最近活动: `{last_event}`）")
            else:
                lines.append(f"  - [{folder}] **{status}** | 水位线 UID: `{watermark}` (去重 {cached_uids} 条)")
                if last_event:
                    lines.append(f"    · 最近活动: `{last_event}`")
            if err:
                lines.append(f"    · ⚠️ 异常: `{err}`")
    return "\n".join(lines)
