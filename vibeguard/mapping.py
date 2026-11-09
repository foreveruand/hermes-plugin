"""Fixed code mapping for sites and devices.

Real device names never reach the model: they are replaced by a code such as
__VG_HIDDEN_S12__ / __VG_HIDDEN_K3__ read from a host-local JSON file. Codes are
allocated once and never reused, so the same name keeps the same code across
restarts and across monthly rebuilds of the inventory.

The matcher is a plain Aho-Corasick automaton: one pass over the text, longest
match wins, no regex backtracking and no per-keyword scanning.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from .config import mapping_path
except ImportError:
    from config import mapping_path

#: New __VG_HIDDEN_S12__ form, plus the legacy [[S12]] wikilink form so old
#: chats and notes still restore.
CODE_RE = re.compile(r"__VG_HIDDEN_([SK])(\d+)__|(?:\[\[([SK])(\d+)\]\])")

#: "## __VG_HIDDEN_S12__ 测试甲（220级，30 台）", legacy "## S12 测试甲（..."
_NOTE_SITE = re.compile(r"^## (?:__VG_HIDDEN_)?(S\d+)(?:__)? (.+?)（", re.M)
#: "| `__VG_HIDDEN_K3__` | 测甲1 | 设备", legacy "| `K3` | ..."
_NOTE_DEVICE = re.compile(r"^\| `(?:__VG_HIDDEN_)?(K\d+)(?:__)?` \| (.+?) \|", re.M)

#: Suffixes a site name may carry; codes are keyed on the bare body.
_SITE_SUFFIXES = ("服务站", "站点", "站")


def site_base(name: str) -> str:
    """Bare site body: "测试乙服务站" / "测试乙站点" / "测试乙站" -> "测试乙".

    Only the suffix is dropped; a name that is nothing but the suffix is kept
    so a heading never degrades into an empty key.
    """
    name = (name or "").strip()
    for suffix in _SITE_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


class Automaton:
    """Aho-Corasick over device surfaces; each state remembers the longest word ending there."""

    __slots__ = ("_children", "_fail", "_best")

    def __init__(self) -> None:
        self._children: List[Dict[str, int]] = [{}]
        self._fail: List[int] = [0]
        self._best: List[Optional[Tuple[str, int, str]]] = [None]

    def add(self, surface: str, code: str) -> None:
        if not surface:
            return
        node = 0
        for ch in surface:
            nxt = self._children[node].get(ch)
            if nxt is None:
                nxt = len(self._children)
                self._children.append({})
                self._fail.append(0)
                self._best.append(None)
                self._children[node][ch] = nxt
            node = nxt
        entry = (code, len(surface), surface)
        best = self._best[node]
        if best is None or entry[1] > best[1]:
            self._best[node] = entry

    def build(self) -> "Automaton":
        queue = list(self._children[0].values())
        for child in queue:
            self._fail[child] = 0
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for ch, child in self._children[node].items():
                f = self._fail[node]
                while f and ch not in self._children[f]:
                    f = self._fail[f]
                self._fail[child] = self._children[f].get(ch, 0) if f or ch in self._children[0] else 0
                if self._fail[child] == child:
                    self._fail[child] = 0
                inherited = self._best[self._fail[child]]
                best = self._best[child]
                if inherited is not None and (best is None or inherited[1] > best[1]):
                    self._best[child] = inherited
                queue.append(child)
        return self

    def find(self, text: str) -> List[Tuple[int, int, str]]:
        """(start, end, code) for every longest match, in text order."""
        spans: List[Tuple[int, int, str]] = []
        node = 0
        children = self._children
        fail = self._fail
        best = self._best
        for i, ch in enumerate(text):
            while node and ch not in children[node]:
                node = fail[node]
            node = children[node].get(ch, 0)
            hit = best[node]
            if hit is not None:
                spans.append((i + 1 - hit[1], i + 1, hit[0]))
        return spans


class Entities:
    """Code table plus the matcher built from it."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self.sites: Dict[str, str] = {}
        self.devices: Dict[str, str] = {}
        self.source = ""
        self.stamp: Dict[str, Any] = {}
        self.generated = ""
        self._file_stamp: Optional[Tuple[float, int]] = None
        self._surfaces: Dict[str, str] = {}
        self._surface_set: set[str] = set()
        self._counters: Dict[str, int] = {"S": 1, "K": 1}
        self._automaton: Optional[Automaton] = None
        self._lock = threading.RLock()
        #: Keyword surfaces own the text; a device name containing one must not
        #: shadow it, or a place name like 甲县 would be read as a site.
        self.keywords: set[str] = set()

    # ---------------------------------------------------------------- building
    def _blocked(self, surface: str) -> bool:
        return any(kw and kw in surface for kw in self.keywords)

    def set_keywords(self, keywords: Iterable[str]) -> None:
        """Rebuild the matcher when the keyword set changes."""
        fresh = {str(kw) for kw in keywords if kw}
        if fresh == self.keywords:
            return
        self.index(fresh)

    def index(self, keywords: Optional[Iterable[str]] = None) -> None:
        if keywords is not None:
            self.keywords = {str(kw) for kw in keywords if kw}
        surfaces: Dict[str, str] = {}
        for table in (self.sites, self.devices):
            for surface, code in table.items():
                surfaces.setdefault(surface, code)
        automaton = Automaton()
        for surface, code in surfaces.items():
            # A surface carrying a keyword is left to the keyword rule.
            if self._blocked(surface):
                continue
            automaton.add(surface, code)
            # "Ⅰ丙联1" typed with an ASCII I is the same device.
            alias = unicodedata.normalize("NFKC", surface)
            if alias != surface and alias not in surfaces and not self._blocked(alias):
                automaton.add(alias, code)
        with self._lock:
            self._surfaces = {code: surface for surface, code in surfaces.items()}
            self._surface_set = set(surfaces)
            self._counters = {"S": self._top("S"), "K": self._top("K")}
            self._automaton = automaton.build()

    def find(self, text: str) -> List[Tuple[int, int, str]]:
        automaton = self._automaton
        return automaton.find(text) if automaton is not None else []

    def surface_for(self, code: str) -> Optional[str]:
        return self._surfaces.get(code)

    def contains(self, surface: str) -> bool:
        return surface in self._surface_set

    @property
    def count(self) -> int:
        return len(set(self.sites) | set(self.devices))

    def _top(self, prefix: str) -> int:
        """First free number: max allocated + 1."""
        table = self.sites if prefix == "S" else self.devices
        top = 0
        for code in table.values():
            if code[:1] == prefix and code[1:].isdigit():
                top = max(top, int(code[1:]))
        return top + 1

    def _next(self, prefix: str) -> int:
        value = self._counters.get(prefix, 1)
        self._counters[prefix] = value + 1
        return value

    @staticmethod
    def _migrate_sites(table: Dict[str, str]) -> Dict[str, str]:
        """Fold legacy keys that still end in 站点 / 站 onto the bare name, keeping codes.

        A name stored both ways keeps the code of the already-bare key; the
        legacy spelling is dropped so one site owns exactly one code.
        """
        migrated: Dict[str, str] = {}
        legacy: Dict[str, str] = {}
        for name, code in table.items():
            base = site_base(name)
            if base == name:
                migrated[name] = code
            else:
                legacy.setdefault(base, code)
        for base, code in legacy.items():
            migrated.setdefault(base, code)
        return migrated

    def assign(self, parsed: Dict[str, Any]) -> Dict[str, int]:
        """Give every site and device a code, reusing the ones already allocated."""
        added = {"S": 0, "K": 0}
        self.sites = self._migrate_sites(self.sites)
        for site in parsed.get("sites") or []:
            name = site_base(str(site.get("name") or ""))
            if not name:
                continue
            if name not in self.sites:
                self.sites[name] = "S" + str(self._next("S"))
                added["S"] += 1
            for device in site.get("device_names") or []:
                if device and device not in self.devices:
                    self.devices[device] = "K" + str(self._next("K"))
                    added["K"] += 1
        self.index()
        return added

    # ----------------------------------------------------------------- storage
    @classmethod
    def load(cls, path: Path, keywords: Optional[Iterable[str]] = None) -> "Entities":
        entities = cls(path)
        if keywords is not None:
            entities.keywords = {str(kw) for kw in keywords if kw}
        try:
            stat = Path(path).stat()
            entities._file_stamp = (stat.st_mtime, stat.st_size)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            entities._automaton = Automaton().build()
            return entities
        if isinstance(data, dict):
            entities.sites = Entities._migrate_sites(
                {str(k): str(v) for k, v in (data.get("sites") or {}).items()})
            entities.devices = {str(k): str(v) for k, v in (data.get("devices") or {}).items()}
            entities.source = str(data.get("source") or "")
            entities.generated = str(data.get("generated") or "")
            entities.stamp = dict(data.get("stamp") or {})
        entities.index()
        return entities

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.generated = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload = {
            "version": 1,
            "generated": self.generated,
            "source": self.source,
            "stamp": self.stamp,
            "sites": self.sites,
            "devices": self.devices,
        }
        tmp = target.with_name("." + target.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        stat = target.stat()
        self._file_stamp = (stat.st_mtime, stat.st_size)
        self.path = target
        return target

    def refresh(self) -> bool:
        """Reload when the file changed underneath us (the monthly cron rewrites it)."""
        if self.path is None:
            return False
        try:
            stat = self.path.stat()
        except OSError:
            return False
        if self._file_stamp == (stat.st_mtime, stat.st_size):
            return False
        fresh = Entities.load(self.path, keywords=self.keywords)
        with self._lock:
            self.keywords = fresh.keywords
            self.sites = fresh.sites
            self.devices = fresh.devices
            self.source = fresh.source
            self.stamp = fresh.stamp
            self.generated = fresh.generated
            self._file_stamp = fresh._file_stamp
            self._surfaces = fresh._surfaces
            self._surface_set = fresh._surface_set
            self._counters = fresh._counters
            self._automaton = fresh._automaton
        return True

    # -------------------------------------------------------------- seed / md
    def seed_from_note(self, text: str) -> int:
        """Adopt the codes an existing note already shows, so nothing shifts."""
        count = 0
        self.sites = self._migrate_sites(self.sites)
        for code, name in _NOTE_SITE.findall(text):
            self.sites.setdefault(site_base(name), code)
            count += 1
        for code, name in _NOTE_DEVICE.findall(text):
            self.devices.setdefault(name.strip(), code)
            count += 1
        self.index()
        return count


_LOCK = threading.RLock()
_CURRENT: Optional[Entities] = None
_CURRENT_PATH: Optional[str] = None


def configured_keywords(config: Dict[str, Any]) -> set[str]:
    """Every non-empty keyword from ``config["patterns"]["keywords"]``."""
    patterns_cfg = config.get("patterns") if isinstance(config, dict) else None
    raw = patterns_cfg.get("keywords") if isinstance(patterns_cfg, dict) else None
    found: set[str] = set()
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(item, dict):
            value = str(item.get("value") or "")
        else:
            value = str(item or "")
        if value:
            found.add(value)
    return found


def load(config: Dict[str, Any]) -> Entities:
    global _CURRENT, _CURRENT_PATH
    path = str(mapping_path(config))
    keywords = configured_keywords(config)
    with _LOCK:
        if _CURRENT is None or _CURRENT_PATH != path:
            _CURRENT = Entities.load(Path(path), keywords=keywords)
            _CURRENT_PATH = path
        else:
            _CURRENT.set_keywords(keywords)
        return _CURRENT


def reload_if_changed(config: Dict[str, Any]) -> bool:
    with _LOCK:
        # load() already re-applies the configured keywords on the cached table.
        return load(config).refresh()
