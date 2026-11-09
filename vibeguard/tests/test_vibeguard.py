"""Tests for the slim VibeGuard plugin: fixed codes, random fakes, stability."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine  # noqa: E402
import mapping  # noqa: E402
import patterns  # noqa: E402
import session  # noqa: E402
import topology  # noqa: E402

NOTE = """---
title: 站点与设备对照
---

## S1 测试甲（220级，3 台）

| 代号 | 设备 | 类型 |
| --- | --- | --- |
| `K1` | 测101 | 设备 |
| `K2` | 丙联1 | 设备 |

## S2 测试丙（110级，2 台）

| 代号 | 设备 | 类型 |
| --- | --- | --- |
| `K3` | Ⅰ丙联1 | 设备 |
| `K4` | 测352 | 设备 |

## S3 测试丁（110级，1 台）

| 代号 | 设备 | 类型 |
| --- | --- | --- |
| `K4` | 测352 | 设备 |
"""

#: The same table written in the current __VG_HIDDEN_ format.
NEW_NOTE = """---
title: 站点与设备对照
---

## __VG_HIDDEN_S1__ 测试甲（220级，3 台）

| 代号 | 设备 | 类型 |
| --- | --- | --- |
| `__VG_HIDDEN_K1__` | 测101 | 设备 |
| `__VG_HIDDEN_K2__` | 丙联1 | 设备 |

## __VG_HIDDEN_S2__ 测试丙（110级，2 台）

| 代号 | 设备 | 类型 |
| --- | --- | --- |
| `__VG_HIDDEN_K3__` | Ⅰ丙联1 | 设备 |
| `__VG_HIDDEN_K4__` | 测352 | 设备 |

## __VG_HIDDEN_S3__ 测试丁（110级，1 台）

| 代号 | 设备 | 类型 |
| --- | --- | --- |
| `__VG_HIDDEN_K4__` | 测352 | 设备 |
"""

DOC = """220级站点
测试甲站
测220级东线：测101设备运行、Ⅰ丙联1设备待机
测试丙站
丙110级南线：Ⅰ丙联1设备运行、测352设备运行
测试丁站
测352设备运行
"""


def build_patterns(base: Path, mapping_file: Path) -> patterns.PatternSet:
    cfg = {
        "keywords": [],
        "regex": [{"pattern": r"sk-[A-Za-z0-9]{48}", "category": "OPENAI_KEY"}],
        "builtin": ["china_id", "china_phone", "email"],
        "exclude": ["example.com"],
    }
    return patterns.build_pattern_set(cfg, {"base_dir": str(base), "mapping": str(mapping_file)})


class MappingTests(unittest.TestCase):
    def test_seed_keeps_note_codes(self) -> None:
        entities = mapping.Entities()
        entities.seed_from_note(NOTE)
        self.assertEqual(entities.sites, {"测试甲": "S1", "测试丙": "S2", "测试丁": "S3"})
        self.assertEqual(entities.devices["Ⅰ丙联1"], "K3")
        self.assertEqual(entities.surface_for("K1"), "测101")

    def test_seed_reads_hidden_codes_from_new_note(self) -> None:
        entities = mapping.Entities()
        entities.seed_from_note(NEW_NOTE)
        self.assertEqual(entities.sites, {"测试甲": "S1", "测试丙": "S2", "测试丁": "S3"})
        self.assertEqual(entities.devices["Ⅰ丙联1"], "K3")
        self.assertEqual(entities.surface_for("K1"), "测101")

    def test_code_re_reads_both_forms(self) -> None:
        for text, code in (("__VG_HIDDEN_S12__", "S12"), ("[[S12]]", "S12"),
                           ("__VG_HIDDEN_K3__", "K3"), ("[[K3]]", "K3")):
            match = mapping.CODE_RE.search(text)
            self.assertIsNotNone(match, text)
            self.assertEqual((match.group(1) or match.group(3)) + (match.group(2) or match.group(4)), code)

    def test_seed_strips_site_suffix(self) -> None:
        note = """## S1 测试乙服务站（220级，1 台）

| 代号 | 设备 | 类型 |
| --- | --- | --- |
| `K1` | 测乙001 | 设备 |

## S2 测试乙站（110级，1 台）

## S3 测试乙站点（35级，1 台）
"""
        entities = mapping.Entities()
        entities.seed_from_note(note)
        self.assertEqual(entities.sites, {"测试乙": "S1"})

    def test_legacy_site_key_keeps_its_code(self) -> None:
        entities = mapping.Entities()
        entities.sites = {"测试乙站点": "S9"}
        added = entities.assign({"sites": [{"name": "测试乙", "device_names": []}]})
        self.assertEqual(added["S"], 0)
        self.assertEqual(entities.sites, {"测试乙": "S9"})
        self.assertEqual(entities.surface_for("S9"), "测试乙")

    def test_load_migrates_legacy_site_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping.json"
            path.write_text(json.dumps({
                "sites": {"测试乙站点": "S1", "测试丁站": "S2", "测试丙": "S3"},
                "devices": {},
            }, ensure_ascii=False), encoding="utf-8")
            entities = mapping.Entities.load(path)
            self.assertEqual(entities.sites, {"测试丙": "S3", "测试乙": "S1", "测试丁": "S2"})
            self.assertEqual(entities.find("测试乙站"), [(0, 3, "S1")])

    def test_bare_name_matches_every_suffix_form(self) -> None:
        entities = mapping.Entities()
        entities.sites = {"测试乙": "S1"}
        entities.index()
        for text in ("测试乙", "测试乙站点", "测试乙站", "测试乙服务站"):
            self.assertEqual(entities.find(text)[0][2], "S1", text)

    def test_longest_match_wins(self) -> None:
        entities = mapping.Entities()
        entities.seed_from_note(NOTE)
        spans = entities.find("巡检Ⅰ丙联1线路")
        self.assertEqual(spans, [(2, 6, "K3")])

    def test_duplicate_device_shares_one_code(self) -> None:
        entities = mapping.Entities()
        entities.seed_from_note(NOTE)
        self.assertEqual(entities.devices["测352"], "K4")
        self.assertEqual(len({code for code in entities.devices.values()}), len(entities.devices))

    def test_assign_is_stable_and_allocates_max_plus_one(self) -> None:
        entities = mapping.Entities()
        entities.seed_from_note(NOTE)
        parsed = topology.parse(DOC)
        added = entities.assign(parsed)
        self.assertEqual(added, {"S": 0, "K": 0})
        entities.assign(parsed)
        self.assertEqual(entities.sites["测试甲"], "S1")
        entities.sites["新区"] = "S" + str(int(max(entities.sites.values())[1:]) + 1)
        self.assertEqual(entities.sites["新区"], "S4")

    def test_ascii_alias_matches_roman_numeral(self) -> None:
        entities = mapping.Entities()
        entities.seed_from_note(NOTE)
        self.assertEqual(entities.find("I丙联1")[0][2], "K3")

    def test_roundtrip_through_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping.json"
            entities = mapping.Entities(path)
            entities.seed_from_note(NOTE)
            entities.save()
            again = mapping.Entities.load(path)
            self.assertEqual(again.sites, entities.sites)
            self.assertEqual(again.find("测101"), [(0, 4, "K1")])


class KeywordPriorityTests(unittest.TestCase):
    """A surface carrying a configured keyword yields to the keyword rule."""

    SITES = {"甲县": "S283", "乙市": "S133", "丙县": "S5"}
    DEVICES = {"甲县1": "K9"}

    def _entities(self) -> mapping.Entities:
        entities = mapping.Entities()
        entities.sites = dict(self.SITES)
        entities.devices = dict(self.DEVICES)
        entities.set_keywords({"甲县"})
        return entities

    def test_surface_containing_keyword_is_not_matched(self) -> None:
        entities = self._entities()
        self.assertEqual(entities.find("甲县"), [])
        self.assertEqual(entities.find("甲县1"), [])

    def test_plain_surface_still_matches(self) -> None:
        entities = self._entities()
        self.assertEqual(entities.find("乙市"), [(0, 2, "S133")])

    def test_excluded_surface_still_restores(self) -> None:
        entities = self._entities()
        self.assertEqual(entities.surface_for("S283"), "甲县")
        self.assertEqual(entities.surface_for("K9"), "甲县1")

    def test_load_accepts_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping.json"
            path.write_text(json.dumps({
                "sites": self.SITES,
                "devices": self.DEVICES,
            }, ensure_ascii=False), encoding="utf-8")
            entities = mapping.Entities.load(path, keywords=["甲县"])
            self.assertEqual(entities.find("甲县"), [])
            self.assertEqual(entities.find("乙市"), [(0, 2, "S133")])

    def test_keyword_beats_same_named_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / "mapping.json"
            path.write_text(json.dumps({
                "sites": self.SITES,
                "devices": self.DEVICES,
            }, ensure_ascii=False), encoding="utf-8")
            pattern_set = patterns.build_pattern_set(
                {"keywords": [{"value": "甲县", "category": "COUNTY"}], "builtin": []},
                {"base_dir": str(base), "mapping": str(path)},
            )
            sess = session.PlaceholderSession()
            sess.entities = pattern_set.entities

            text = "甲县 与 乙市 维护"
            masked, count = engine.redact_text(text, pattern_set, sess)
            self.assertNotIn("__VG_HIDDEN_S283__", masked)
            self.assertIn("__VG_SECRET_", masked)
            self.assertIn("__VG_HIDDEN_S133__", masked)
            self.assertEqual(count, 2)
            restored, _ = engine.restore_text(masked, sess)
            self.assertEqual(restored, text)


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        entities = mapping.Entities(base / "mapping.json")
        entities.seed_from_note(NOTE)
        entities.save()
        self.patterns = build_patterns(base, base / "mapping.json")
        self.session = session.PlaceholderSession()
        self.session.entities = self.patterns.entities

    def test_mask_and_restore_roundtrip(self) -> None:
        text = "测101 与 Ⅰ丙联1 维护，身份证 110101199001011234，手机 13800138000"
        masked, count = engine.redact_text(text, self.patterns, self.session)
        self.assertEqual(count, 4)
        self.assertIn("__VG_HIDDEN_K1__", masked)
        self.assertIn("__VG_HIDDEN_K3__", masked)
        self.assertNotIn("110101199001011234", masked)
        restored, _ = engine.restore_text(masked, self.session)
        self.assertEqual(restored, text)

    def test_restore_accepts_legacy_wikilink_codes(self) -> None:
        text = "测试甲 与 测101 维护"
        masked, _ = engine.redact_text(text, self.patterns, self.session)
        self.assertEqual(masked, "__VG_HIDDEN_S1__ 与 __VG_HIDDEN_K1__ 维护")
        legacy = masked.replace("__VG_HIDDEN_S1__", "[[S1]]").replace("__VG_HIDDEN_K1__", "[[K1]]")
        restored, count = engine.restore_text(legacy, self.session)
        self.assertEqual(restored, text)
        self.assertEqual(count, 2)

    def test_keyword_placeholder_never_names_its_category(self) -> None:
        for category in ("PROVINCE", "CITY", "COUNTY", "KEYWORD", ""):
            sess = session.PlaceholderSession()
            placeholder = sess.generate_placeholder("某省", category)
            self.assertRegex(placeholder, r"^__VG_SECRET_[0-9a-f]{12}(?:_\d+)?__$")
            self.assertTrue(sess.get_placeholder_regex().fullmatch(placeholder), placeholder)

    def test_mask_is_idempotent(self) -> None:
        text = "测101 身份证 110101199001011234"
        masked, _ = engine.redact_text(text, self.patterns, self.session)
        twice, count = engine.redact_text(masked, self.patterns, self.session)
        self.assertEqual(twice, masked)
        self.assertEqual(count, 0)

    def test_fake_shapes(self) -> None:
        cases = {
            ("110101199001011234", "CHINA_ID"): r"\d{17}[\dX]",
            ("13800138000", "CHINA_PHONE"): r"1[3-9]\d{9}",
            ("user@corp.cn", "EMAIL"): r"[0-9a-f]{8}@example\.com",
            ("sk-" + "a" * 48, "OPENAI_KEY"): r"sk-[A-Za-z0-9]{48}",
        }
        for (original, category), shape in cases.items():
            fake = self.session.fake_for(original, category)
            self.assertRegex(fake, "^" + shape + "$")
            self.assertNotEqual(fake, original)
            self.assertEqual(self.session.restore_fakes(fake)[0], original)

    def test_excluded_domains_are_kept(self) -> None:
        text = "联系 ops@example.com 即可"
        masked, count = engine.redact_text(text, self.patterns, self.session)
        self.assertEqual(count, 0)
        self.assertEqual(masked, text)

    def test_cache_returns_same_result(self) -> None:
        text = "测101 维护"
        first, _ = engine.redact_text(text, self.patterns, self.session)
        second, _ = engine.redact_text(text, self.patterns, self.session)
        self.assertEqual(first, second)

    def test_scan_speed(self) -> None:
        text = "测101 与 Ⅰ丙联1 维护，测352 设备。 " * 500
        start = __import__("time").perf_counter()
        engine.redact_text(text, self.patterns, self.session)
        elapsed = __import__("time").perf_counter() - start
        self.assertLess(elapsed, 0.25, "masking %d chars took %.3fs" % (len(text), elapsed))


class TopologyTests(unittest.TestCase):
    def test_parse_reads_sites_and_devices(self) -> None:
        parsed = topology.parse(DOC)
        self.assertEqual(parsed["site_count"], 3)
        names = [site["name"] for site in parsed["sites"]]
        self.assertEqual(names, ["测试甲", "测试丙", "测试丁"])
        self.assertEqual(parsed["sites"][0]["device_names"], ["测101", "Ⅰ丙联1"])
        self.assertEqual(parsed["device_count"], 5)

    def test_parse_strips_site_suffix(self) -> None:
        text = "测试乙服务站\n测乙001设备运行\n测试乙站\n测乙002设备运行\n测试乙站点\n测乙003设备运行\n"
        parsed = topology.parse(text)
        self.assertEqual([site["name"] for site in parsed["sites"]], ["测试乙"])
        self.assertEqual(parsed["site_count"], 1)
        self.assertEqual(parsed["sites"][0]["device_names"], ["测乙001", "测乙002", "测乙003"])

    def test_level_label_is_not_a_site(self) -> None:
        parsed = topology.parse("220级站点\n测试甲站\n测101设备运行\n")
        self.assertEqual([site["name"] for site in parsed["sites"]], ["测试甲"])

    def test_render_titles_use_the_bare_name(self) -> None:
        parsed = topology.parse("测试乙服务站\n测乙001设备运行\n")
        entities = mapping.Entities()
        entities.assign(parsed)
        body = topology.render(parsed, entities, "doc.doc", "2026-10-23")
        self.assertIn("## __VG_HIDDEN_S1__ 测试乙（未知，1 台）", body)
        self.assertNotIn("## __VG_HIDDEN_S1__ 测试乙服务站", body)

    def test_render_uses_hidden_codes(self) -> None:
        parsed = topology.parse(DOC)
        entities = mapping.Entities()
        entities.assign(parsed)
        body = topology.render(parsed, entities, "doc.doc", "2026-10-23")
        self.assertIn("## __VG_HIDDEN_S1__ 测试甲（未知，2 台）", body)
        self.assertIn("| `__VG_HIDDEN_K1__` | 测101 | 设备 |", body)
        self.assertIn("`S12` 在对话里显示为 `__VG_HIDDEN_S12__`", body)
        self.assertNotIn("[[", body)

    def test_same_device_on_two_groups_in_one_site_is_kept(self) -> None:
        text = "测试甲站\n测220级东线：测101设备运行\n测220级西线：测101设备运行\n"
        parsed = topology.parse(text)
        self.assertEqual(len(parsed["sites"][0]["devices"]), 2)

    def test_two_sites_may_share_a_device_name(self) -> None:
        text = "测试丁站\n测352设备运行\n测试戊站\n测352设备运行\n"
        parsed = topology.parse(text)
        self.assertEqual(parsed["site_count"], 2)
        self.assertEqual([len(s["devices"]) for s in parsed["sites"]], [1, 1])


if __name__ == "__main__":
    unittest.main()
