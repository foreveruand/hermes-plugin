"""VibeGuard session state: fixed codes, random fakes, placeholder fallback.

Two kinds of masking live here:

* device names   -> a fixed code from mapping.json (__VG_HIDDEN_S12__), same everywhere
* secrets        -> a realistic random value, kept only in this process so the
                    final answer and local tool arguments can be restored
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

try:
    from .mapping import Automaton
    from .patterns import sanitize_category
except ImportError:
    from mapping import Automaton
    from patterns import sanitize_category

_DIGITS = "0123456789"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = _LOWER.upper()
_ALNUM = _LOWER + _UPPER + _DIGITS
_CJK = "云峰岭川河海江宁安平华新昌泰和顺德明远阳丰源泽清宁"

#: Where a random ID number claims to be from; all are real, non-reserved codes.
_ID_REGIONS = ("110101", "320102", "330106", "440305", "510107", "370102")
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"

_CREDENTIAL_PREFIX = re.compile(r"^(sk-|gh[pousr]_|AKIA)")


def _pick(alphabet: str, count: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(count))


def _fake_id() -> str:
    body = secrets.choice(_ID_REGIONS) + str(secrets.randbelow(45) + 1960)
    body += "%02d%02d" % (secrets.randbelow(12) + 1, secrets.randbelow(28) + 1)
    body += _pick(_DIGITS, 3)
    total = sum(int(ch) * weight for ch, weight in zip(body, _ID_WEIGHTS))
    return body + _ID_CHECK[total % 11]


def _fake_phone() -> str:
    return "1" + secrets.choice("356789") + _pick(_DIGITS, 9)


def _fake_email() -> str:
    return _pick("0123456789abcdef", 8) + "@example.com"


def _fake_credential(original: str) -> str:
    match = _CREDENTIAL_PREFIX.match(original)
    head = match.group(1) if match else ""
    tail = original[len(head):]
    alphabet = _UPPER + _DIGITS if tail and tail.upper() == tail else _ALNUM
    return head + _pick(alphabet, len(tail))


def _fake_generic(original: str) -> str:
    if _CREDENTIAL_PREFIX.match(original):
        return _fake_credential(original)
    out: List[str] = []
    for ch in original:
        if ch.isdigit():
            out.append(secrets.choice(_DIGITS))
        elif "a" <= ch <= "z":
            out.append(secrets.choice(_LOWER))
        elif "A" <= ch <= "Z":
            out.append(secrets.choice(_UPPER))
        elif "\u4e00" <= ch <= "\u9fa5":
            out.append(secrets.choice(_CJK))
        else:
            out.append(ch)
    return "".join(out)


_GENERATORS = {
    "CHINA_ID": lambda original: _fake_id(),
    "CHINA_PHONE": lambda original: _fake_phone(),
    "EMAIL": lambda original: _fake_email(),
    "CREDENTIAL": _fake_credential,
}


class PlaceholderSession:
    """Per-conversation mapping: real <-> fake, plus the legacy placeholder path."""

    CACHE_LIMIT = 512

    def __init__(self, prefix: str = "__VG_", secret: Optional[bytes] = None):
        self.prefix = prefix
        self.secret = secret if secret is not None else os.urandom(32)
        self.forward: Dict[str, str] = {}   # placeholder -> original
        self.reverse: Dict[str, str] = {}   # original -> placeholder
        self.fakes: Dict[str, str] = {}     # original -> fake
        self.real_of: Dict[str, str] = {}   # fake -> original
        self.fake_set: set[str] = set()
        self.entities: Any = None
        self._fake_auto: Optional[Automaton] = None
        self._fakes_dirty = False
        self._cache: Dict[str, Tuple[str, int]] = {}

    # ----------------------------------------------------------- placeholders
    def generate_placeholder(self, original: str, category: str = "SECRET") -> str:
        # The category is deliberately dropped: a placeholder like
        # __VG_COUNTY_... would leak what kind of thing was masked.
        h = hmac.new(self.secret, original.encode("utf-8"), hashlib.sha256)
        base = "%sSECRET_%s" % (self.prefix, h.hexdigest()[:12])
        candidate = base + "__"
        if candidate in self.forward and self.forward[candidate] != original:
            i = 1
            while "%s_%d__" % (base, i) in self.forward and self.forward["%s_%d__" % (base, i)] != original:
                i += 1
            candidate = "%s_%d__" % (base, i)
        return candidate

    def get_or_create(self, original: str, category: str) -> str:
        if original in self.reverse:
            return self.reverse[original]
        ph = self.generate_placeholder(original, category)
        self.forward[ph] = original
        self.reverse[original] = ph
        return ph

    def lookup(self, placeholder: str) -> Optional[str]:
        return self.forward.get(placeholder)

    def lookup_reverse(self, original: str) -> Optional[str]:
        return self.reverse.get(original)

    def get_placeholder_regex(self) -> re.Pattern:
        escaped_prefix = re.escape(self.prefix)
        return re.compile(rf"{escaped_prefix}[A-Z0-9_]+_[0-9a-f]{{12}}(?:_\d+)?__")

    # ------------------------------------------------------------ random fakes
    @property
    def has_secrets(self) -> bool:
        if self.forward or self.fakes:
            return True
        return bool(self.entities is not None and self.entities.count)

    def is_fake(self, token: str) -> bool:
        return token in self.fake_set

    def fake_for(self, original: str, category: str) -> str:
        known = self.fakes.get(original)
        if known is not None:
            return known
        generator = _GENERATORS.get(sanitize_category(category), _fake_generic)
        fake = original
        for _ in range(8):
            candidate = generator(original)
            if not candidate or candidate == original or candidate in self.real_of:
                continue
            if self.entities is not None and self.entities.contains(candidate):
                continue
            fake = candidate
            break
        self.fakes[original] = fake
        self.real_of[fake] = original
        self.fake_set.add(fake)
        self._fakes_dirty = True
        return fake

    def _build_fake_automaton(self) -> None:
        automaton = Automaton()
        for fake, original in self.real_of.items():
            automaton.add(fake, original)
        self._fake_auto = automaton.build()
        self._fakes_dirty = False

    def restore_fakes(self, text: str) -> Tuple[str, int]:
        if not self.real_of:
            return text, 0
        if self._fake_auto is None or self._fakes_dirty:
            self._build_fake_automaton()
        spans = self._fake_auto.find(text) if self._fake_auto else []
        if not spans:
            return text, 0
        out: List[str] = []
        cursor = 0
        count = 0
        for start, end, original in spans:
            if start < cursor:
                continue
            out.append(text[cursor:start])
            out.append(original)
            cursor = end
            count += 1
        out.append(text[cursor:])
        return "".join(out), count

    # ------------------------------------------------------------------ cache
    def cache_key(self, text: str) -> str:
        return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()

    def cache_get(self, text: str) -> Optional[Tuple[str, int]]:
        return self._cache.get(self.cache_key(text))

    def cache_put(self, text: str, masked: str, count: int) -> None:
        if len(self._cache) > self.CACHE_LIMIT:
            self._cache.clear()
        self._cache[self.cache_key(text)] = (masked, count)

    def cache_clear(self) -> None:
        self._cache.clear()
