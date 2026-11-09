"""VibeGuard redaction and restoration engine."""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

try:
    from .mapping import CODE_RE
    from .patterns import PatternSet
    from .session import PlaceholderSession
except ImportError:
    from mapping import CODE_RE
    from patterns import PatternSet
    from session import PlaceholderSession

#: Entity spans win ties against keywords, which win against regex hits.
ENTITY = 0
KEYWORD = 1
REGEX = 2


def _excluded(original: str, patterns: PatternSet) -> bool:
    if original in patterns.exclude:
        return True
    return any(item and item in original for item in patterns.exclude)


def redact_text(input_text: str, patterns: PatternSet, session: PlaceholderSession) -> Tuple[str, int]:
    """Replace device names with codes and secrets with random values.

    Returns (masked_text, count_of_replacements). Results are cached per session so
    the same history is not scanned again on every turn.
    """
    if not input_text or not isinstance(input_text, str):
        return input_text, 0

    cached = session.cache_get(input_text)
    if cached is not None:
        return cached

    found: List[Dict[str, Any]] = []

    # 1. Device names -> fixed codes
    entities = patterns.entities
    if entities is not None:
        for start, end, code in entities.find(input_text):
            found.append({"start": start, "end": end, "code": code, "kind": ENTITY})

    # 2. Configured keywords -> placeholders
    for rule in patterns.keywords:
        needle = rule["value"]
        if not needle:
            continue
        start_pos = 0
        while True:
            pos = input_text.find(needle, start_pos)
            if pos == -1:
                break
            end = pos + len(needle)
            original = input_text[pos:end]
            start_pos = end
            if original in patterns.exclude or session.is_fake(original):
                continue
            found.append({"start": pos, "end": end, "original": original,
                          "category": rule["category"], "kind": KEYWORD})

    # 3. Regular expressions -> random fakes
    for compiled_re, category in patterns.regex:
        for match in compiled_re.finditer(input_text):
            original = match.group(0)
            if not original or session.is_fake(original) or _excluded(original, patterns):
                continue
            found.append({"start": match.start(), "end": match.end(), "original": original,
                          "category": category, "kind": REGEX})

    if not found:
        session.cache_put(input_text, input_text, 0)
        return input_text, 0

    # Sort matches: earlier start first, then longer length first, then by kind
    found.sort(key=lambda item: (item["start"], -(item["end"] - item["start"]), item["kind"]))

    # Select non-overlapping intervals
    selected: List[Dict[str, Any]] = []
    current_end = -1
    for match in found:
        if match["start"] >= current_end:
            selected.append(match)
            current_end = match["end"]

    # Reconstruct string
    parts: List[str] = []
    last_idx = 0
    count = 0
    for match in selected:
        parts.append(input_text[last_idx:match["start"]])
        if match["kind"] == ENTITY:
            parts.append(f'{session.prefix}HIDDEN_{match["code"]}__')
        elif match["kind"] == KEYWORD:
            parts.append(session.get_or_create(match["original"], match["category"]))
        else:
            parts.append(session.fake_for(match["original"], match["category"]))
        last_idx = match["end"]
        count += 1
    parts.append(input_text[last_idx:])

    masked = "".join(parts)
    session.cache_put(input_text, masked, count)
    return masked, count


def restore_text(input_text: str, session: PlaceholderSession) -> Tuple[str, int]:
    """Restore codes, random fakes and placeholders back to their real values."""
    if not input_text or not isinstance(input_text, str):
        return input_text, 0

    count = 0
    entities = session.entities
    if entities is not None and ("__VG_HIDDEN_" in input_text or "[[" in input_text):
        def _code(match: Any) -> str:
            nonlocal count
            code = (match.group(1) or match.group(3)) + (match.group(2) or match.group(4))
            surface = entities.surface_for(code)
            if surface is None:
                return match.group(0)
            count += 1
            return surface
        input_text = CODE_RE.sub(_code, input_text)

    input_text, fake_count = session.restore_fakes(input_text)
    count += fake_count

    if session.prefix in input_text:
        re_ph = session.get_placeholder_regex()

        def _replace(match: Any) -> str:
            nonlocal count
            placeholder = match.group(0)
            orig = session.lookup(placeholder)
            if orig is not None:
                count += 1
                return orig
            return placeholder

        input_text = re_ph.sub(_replace, input_text)

    return input_text, count


def redact_deep(value: Any, patterns: PatternSet, session: PlaceholderSession, seen: Set[int] | None = None) -> Tuple[Any, int]:
    """Recursively redacts strings within nested dictionaries and lists."""
    if seen is None:
        seen = set()

    obj_id = id(value)
    if obj_id in seen:
        return value, 0
    seen.add(obj_id)

    total_count = 0

    if isinstance(value, str):
        return redact_text(value, patterns, session)

    if isinstance(value, dict):
        new_dict = {}
        for k, v in value.items():
            if isinstance(v, (str, dict, list)):
                res_v, c = redact_deep(v, patterns, session, seen)
                new_dict[k] = res_v
                total_count += c
            else:
                new_dict[k] = v
        return new_dict, total_count

    if isinstance(value, list):
        new_list = []
        for v in value:
            if isinstance(v, (str, dict, list)):
                res_v, c = redact_deep(v, patterns, session, seen)
                new_list.append(res_v)
                total_count += c
            else:
                new_list.append(v)
        return new_list, total_count

    return value, 0


def restore_deep(value: Any, session: PlaceholderSession, seen: Set[int] | None = None) -> Tuple[Any, int]:
    """Recursively restores placeholders within nested dictionaries and lists."""
    if seen is None:
        seen = set()

    obj_id = id(value)
    if obj_id in seen:
        return value, 0
    seen.add(obj_id)

    total_count = 0

    if isinstance(value, str):
        return restore_text(value, session)

    if isinstance(value, dict):
        new_dict = {}
        for k, v in value.items():
            if isinstance(v, (str, dict, list)):
                res_v, c = restore_deep(v, session, seen)
                new_dict[k] = res_v
                total_count += c
            else:
                new_dict[k] = v
        return new_dict, total_count

    if isinstance(value, list):
        new_list = []
        for v in value:
            if isinstance(v, (str, dict, list)):
                res_v, c = restore_deep(v, session, seen)
                new_list.append(res_v)
                total_count += c
            else:
                new_list.append(v)
        return new_list, total_count

    return value, 0
