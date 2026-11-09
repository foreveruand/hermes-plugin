"""VibeGuard pattern building and builtin definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from . import mapping as mapping_module
except ImportError:
    import mapping as mapping_module


def sanitize_category(input_val: Any) -> str:
    raw = str(input_val or "").strip()
    if not raw:
        return "TEXT"
    upper = raw.upper()
    safe = re.sub(r"[^A-Z0-9_]", "_", upper)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "TEXT"


BUILTIN_PATTERNS: Dict[str, Dict[str, str]] = {
    "email": {
        "pattern": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "flags": "i",
        "category": "EMAIL",
    },
    "china_phone": {
        "pattern": r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "flags": "",
        "category": "CHINA_PHONE",
    },
    "china_id": {
        "pattern": r"(?<!\d)\d{17}[\dXx](?!\d)",
        "flags": "",
        "category": "CHINA_ID",
    },
    "uuid": {
        "pattern": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        "flags": "",
        "category": "UUID",
    },
    "ipv4": {
        "pattern": r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?!\d)",
        "flags": "",
        "category": "IPV4",
    },
    "mac": {
        "pattern": r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}",
        "flags": "i",
        "category": "MAC",
    },
}


@dataclass
class PatternSet:
    keywords: List[Dict[str, str]] = field(default_factory=list)
    regex: List[Tuple[re.Pattern, str]] = field(default_factory=list)
    exclude: Set[str] = field(default_factory=set)
    entities: Optional[Any] = None


def build_pattern_set(patterns_cfg: Dict[str, Any] | None, config: Dict[str, Any] | None = None) -> PatternSet:
    raw = patterns_cfg if isinstance(patterns_cfg, dict) else {}

    keywords_list: List[Dict[str, str]] = []
    regex_list: List[Tuple[re.Pattern, str]] = []
    exclude_set: Set[str] = set()

    for item in raw.get("keywords", []):
        if isinstance(item, dict):
            val = str(item.get("value", ""))
            cat = sanitize_category(item.get("category", "KEYWORD"))
            if val:
                keywords_list.append({"value": val, "category": cat})
        elif isinstance(item, str) and item:
            keywords_list.append({"value": item, "category": "KEYWORD"})

    builtins = raw.get("builtin", ["email", "china_phone", "china_id", "ipv4"])
    if isinstance(builtins, list):
        for b_name in builtins:
            rule = BUILTIN_PATTERNS.get(str(b_name).lower())
            if rule:
                flags = re.IGNORECASE if "i" in rule.get("flags", "") else 0
                compiled = re.compile(rule["pattern"], flags)
                regex_list.append((compiled, rule["category"]))

    for item in raw.get("regex", []):
        if isinstance(item, dict):
            pat = str(item.get("pattern", ""))
            cat = sanitize_category(item.get("category", "REGEX"))
            flag_str = str(item.get("flags", ""))
            flags = re.IGNORECASE if "i" in flag_str else 0
            if pat:
                try:
                    compiled = re.compile(pat, flags)
                    regex_list.append((compiled, cat))
                except re.error:
                    pass

    for ex in raw.get("exclude", []):
        if isinstance(ex, str) and ex:
            exclude_set.add(ex)

    entities = mapping_module.load(config) if config else None
    kws = {item["value"] for item in keywords_list if item.get("value")}
    if entities is not None and kws:
        # A place name beats a device name: drop every surface carrying a keyword.
        entities.set_keywords(kws)
    return PatternSet(keywords=keywords_list, regex=regex_list, exclude=exclude_set,
                      entities=entities)
