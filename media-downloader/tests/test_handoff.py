"""Tests for the HMD1 handoff code and the /dl import pipeline.

Run directly: python3 tests/test_handoff.py (also pytest-compatible).
"""

import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import config  # noqa: E402
import handoff  # noqa: E402
import tasks  # noqa: E402


def _cfg(tmp: Path) -> dict:
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["destinations"] = {
        "asmr": str(tmp / "ASMR"),
        "asmr_r": str(tmp / "ASMR_R"),
        "bilibili": str(tmp / "bilibili"),
        "pixiv": str(tmp / "pixiv"),
        "extract": str(tmp / "extract"),
    }
    cfg["staging_base"] = str(tmp / "staging")
    cfg["staging_fallback"] = str(tmp / "staging")
    return cfg


def _transfer(**extra) -> dict:
    transfer = {
        "engine": "bdpan",
        "archive_name": "work_ab12cd.7z",
        "archive_size": 2048,
        "password": "Pass1234",
        "remote_rel": "ASMR/work_ab12cd.7z",
        "remote_path": "我的应用数据/bdpan/ASMR/work_ab12cd.7z",
        "saved_path": "我的应用数据/bdpan/ASMR/work_ab12cd.7z",
    }
    transfer.update(extra)
    return transfer


def _manifest(cat: str = "asmr", sub: str = "") -> str:
    return handoff.encode_manifest({
        "v": 1, "engine": "bdpan", "remote": "ASMR/work_ab12cd.7z",
        "name": "work_ab12cd.7z", "password": "Pass1234",
        "cat": cat, "sub": sub, "ts": int(time.time()),
    })


def test_asmr_manifest_carries_category_but_no_duplicate_prefix():
    """ASMR archives already contain circle/RJ, so the receiver extracts at the root."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        destination = str(Path(cfg["destinations"]["asmr_r"]) / "社团" / "RJ123456")
        code = handoff.build_manifest(_transfer(), destination, cfg,
                                     internal_prefix=("社团", "RJ123456"), title="作品")
        assert code.startswith("HMD1:")
        manifest = handoff.decode_manifest(code)
        assert manifest["cat"] == "asmr_r"
        assert manifest["sub"] == ""
        assert manifest["remote"] == "ASMR/work_ab12cd.7z"
        assert manifest["prefix"] == "社团/RJ123456"
        assert manifest["saved"].startswith("我的应用数据/bdpan/")
        assert manifest["size"] == 2048


def test_pixiv_manifest_keeps_author_prefix():
    """Pixiv staging omits the author directory, so the receiver recreates it."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        destination = str(Path(cfg["destinations"]["pixiv"]) / "画师名")
        code = handoff.build_manifest(_transfer(), destination, cfg, title="图集")
        manifest = handoff.decode_manifest(code)
        assert manifest["cat"] == "pixiv"
        assert manifest["sub"] == "画师名"


def test_manifest_requires_a_destination_or_override():
    """A code without a category must prompt for --to instead of guessing a path."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        code = _manifest(cat="")
        try:
            handoff.resolve_import_destination(handoff.decode_manifest(code), cfg)
            assert False, "expected ImportDestinationRequired"
        except handoff.ImportDestinationRequired as exc:
            assert "asmr" in str(exc) and "--to" in str(exc)

        root, label, _ = handoff.resolve_import_destination(
            handoff.decode_manifest(code), cfg, override=str(Path(tmp) / "custom"))
        assert label == "custom"
        assert root == str(Path(tmp) / "custom")

        remote_root, remote_label, _ = handoff.resolve_import_destination(
            handoff.decode_manifest(code), cfg, override="remote:Media/Custom")
        assert remote_label == "custom"
        assert remote_root == "remote:Media/Custom"


def test_import_moves_files_into_category_root_and_is_idempotent():
    """Import extracts under destinations.<cat> and never overwrites existing files."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        code = _manifest(cat="asmr")

        def fake_download(manifest, download_dir, cfg, progress_cb):
            archive = download_dir / "try0" / "work_ab12cd.7z"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"7z")
            return archive, "/apps/bdpan/ASMR/work_ab12cd.7z", ""

        def fake_extract(archive, extract_dir, password, cfg, progress_cb):
            (extract_dir / "track01.flac").write_bytes(b"audio")
            (extract_dir / "cover.jpg").write_bytes(b"image")
            return ""

        original_download, original_extract = handoff._download_archive, handoff._extract_archive
        handoff._download_archive, handoff._extract_archive = fake_download, fake_extract
        try:
            result = handoff.run_import_target(code, cfg)
            assert result["success"] is True
            assert result["category"] == "asmr"
            assert result["count"] == 2
            assert (Path(cfg["destinations"]["asmr"]) / "track01.flac").is_file()

            again = handoff.run_import_target(code, cfg)
            assert again["success"] is True
            assert again["count"] == 0
            assert again["transfer"]["skipped"] == 2
        finally:
            handoff._download_archive, handoff._extract_archive = original_download, original_extract


def test_fenced_and_forwarded_codes_are_split():
    """Users paste codes with fences or with the whole /dl line; both must work."""
    code = _manifest()
    fenced = "```\n" + code + "\n```"
    assert handoff.split_handoff(fenced)[0] == code
    forwarded = "/dl " + code + " --to asmr"
    split = handoff.split_handoff(forwarded)
    assert split[0] == code
    assert split[1] == "--to asmr"


def test_import_reports_the_work_directory_without_double_nesting():
    """ASMR archives already contain circle/RJ, so report that dir without adding it twice."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        code = handoff.encode_manifest({
            "v": 1, "engine": "bdpan", "remote": "ASMR/work_ab12cd.7z",
            "name": "work_ab12cd.7z", "password": "Pass1234",
            "cat": "asmr", "sub": "", "prefix": "社团/RJ9", "ts": 1,
        })

        def fake_download(manifest, download_dir, cfg, progress_cb):
            archive = download_dir / "try0" / "work_ab12cd.7z"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"7z")
            return archive, "/apps/bdpan/ASMR/work_ab12cd.7z", ""

        def fake_extract(archive, extract_dir, password, cfg, progress_cb):
            work = extract_dir / "社团" / "RJ9"
            work.mkdir(parents=True)
            (work / "a.flac").write_bytes(b"audio")
            return ""

        original_download, original_extract = handoff._download_archive, handoff._extract_archive
        handoff._download_archive, handoff._extract_archive = fake_download, fake_extract
        try:
            result = handoff.run_import_target(code, cfg)
        finally:
            handoff._download_archive, handoff._extract_archive = original_download, original_extract

        expected = Path(cfg["destinations"]["asmr"]) / "社团" / "RJ9"
        assert Path(result["destination"]) == expected
        assert (expected / "a.flac").is_file()
        assert not (expected / "社团").exists()


def test_import_to_rclone_destination_transfers_extracted_tree():
    """A receiver in rclone mode uploads the extracted tree to its remote spec."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        cfg["destinations"]["asmr"] = "remote:Media/ASMR"
        code = _manifest(cat="asmr")
        seen = {}

        def fake_download(manifest, download_dir, cfg, progress_cb):
            archive = download_dir / "try0" / "work_ab12cd.7z"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"7z")
            return archive, "/apps/bdpan/ASMR/work_ab12cd.7z", ""

        def fake_extract(archive, extract_dir, password, cfg, progress_cb):
            (extract_dir / "track01.flac").write_bytes(b"audio")
            (extract_dir / "cover.jpg").write_bytes(b"image")
            return ""

        def fake_rclone(staging_dir, destination, cfg, progress_cb=None):
            seen["destination"] = destination
            return {"ok": True, "moved": 2, "total": 2, "errors": 0,
                    "progress": "", "log": ""}

        originals = (handoff._download_archive, handoff._extract_archive, handoff.rclone_transfer)
        handoff._download_archive, handoff._extract_archive, handoff.rclone_transfer = (
            fake_download, fake_extract, fake_rclone)
        try:
            result = handoff.run_import_target(code, cfg)
        finally:
            (handoff._download_archive, handoff._extract_archive,
             handoff.rclone_transfer) = originals

        assert seen["destination"] == "remote:Media/ASMR"
        assert result["destination"] == "remote:Media/ASMR"
        assert result["count"] == 2
        assert sorted(result["files"]) == [
            "remote:Media/ASMR/cover.jpg",
            "remote:Media/ASMR/track01.flac",
        ]


def test_bdpan_briefing_prints_the_handoff_command():
    """The completion briefing must expose a copy-pasteable /dl command."""
    _drain_notifications()
    task = tasks.Task(
        id="download_1", type="download", status="completed", target="RJ123456",
        resolved_path="asmr", output_path="/mnt/ASMR/社团/RJ123456",
        transfer={"engine": "bdpan", "moved": 1, "password": "Pass1234",
                  "archive_name": "work_ab12cd.7z", "manifest": "HMD1:abc123"},
        delivery_target="telegram:1", started_at=time.time(), finished_at=time.time() + 1,
    )
    tasks.queue_task_notification(task)
    msg = _drain_notifications()
    assert "/dl HMD1:abc123" in msg
    assert "一键入库" in msg


def test_import_briefing_reports_target_and_counts():
    """Import briefings must show the final directory and moved/skipped counts."""
    _drain_notifications()
    task = tasks.Task(
        id="import_1", type="import", status="completed", target="HMD1:abc123",
        resolved_path="asmr", output_path="/mnt/ASMR/社团/RJ123456",
        transfer={"engine": "bdpan-import", "archive_name": "work_ab12cd.7z",
                  "moved": 2, "skipped": 1},
        delivery_target="telegram:1", started_at=time.time(), finished_at=time.time() + 1,
    )
    tasks.queue_task_notification(task)
    msg = _drain_notifications()
    assert "媒体入库完成" in msg
    assert "/mnt/ASMR/社团/RJ123456" in msg
    assert "新增 <code>2</code>" in msg and "跳过 <code>1</code>" in msg


def test_manifest_round_trips_split_volumes():
    """A split transfer hands over the folder + volume list, not just the first part."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        stem = "pkg_20261101_8f3a9b"
        volumes = [f"{stem}.7z.{i:03d}" for i in (1, 2, 3)]
        transfer = {
            "engine": "bdpan", "archive_name": f"{stem}.7z", "archive_size": 3072,
            "password": "Pass1234", "volumes": volumes,
            "remote_folder": f"ASMR/{stem}",
            "remote_rel": f"ASMR/{stem}/{volumes[0]}",
            "saved_path": f"我的应用数据/bdpan/ASMR/{stem}/{volumes[0]}",
        }
        destination = str(Path(cfg["destinations"]["asmr"]) / "社团" / "RJ9")
        code = handoff.build_manifest(transfer, destination, cfg,
                                     internal_prefix=("社团", "RJ9"), title="作品")
        manifest = handoff.decode_manifest(code)
        assert manifest["volumes"] == volumes
        assert manifest["remote_folder"] == f"ASMR/{stem}"
        assert manifest["remote"] == f"ASMR/{stem}"
        assert manifest["name"] == f"{stem}.7z"
        assert manifest["password"] == "Pass1234"
        assert manifest["prefix"] == "社团/RJ9"
        assert manifest["size"] == 3072
        assert manifest["saved"].endswith(volumes[0])


def test_decode_manifest_rejects_unsafe_volume_names():
    """Volume names come from the remote side; traversal or nesting must be rejected."""
    for bad in (["../evil.7z.001"], ["sub/pkg.7z.001"], ["/abs.7z.001"], []):
        code = handoff.encode_manifest({
            "v": 1, "engine": "bdpan", "remote": "ASMR/pkg", "name": "pkg.7z",
            "password": "Pass1234", "cat": "asmr", "volumes": bad, "ts": 1,
        })
        try:
            handoff.decode_manifest(code)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError as exc:
            assert "分卷" in str(exc)


def test_import_downloads_every_volume_and_extracts_the_first():
    """Split packages download each part into one dir and extract from .7z.001."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        stem = "pkg_20261101_8f3a9b"
        volumes = [f"{stem}.7z.{i:03d}" for i in (1, 2, 3)]
        code = handoff.encode_manifest({
            "v": 1, "engine": "bdpan", "remote": f"ASMR/{stem}",
            "remote_folder": f"ASMR/{stem}", "name": f"{stem}.7z",
            "password": "Pass1234", "cat": "asmr", "sub": "",
            "volumes": volumes, "ts": 1,
        })

        calls: list[str] = []
        attempts: dict[str, int] = {}
        progress: list[str] = []

        def fake_run(cmd, timeout_seconds, progress_cb, label):
            remote, local = cmd[2], Path(cmd[3])
            calls.append(remote)
            attempts[local.name] = attempts.get(local.name, 0) + 1
            if local.name.endswith(".001") and attempts[local.name] == 1:
                return {"ok": False, "code": 1, "log": "timeout", "progress": ""}
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(b"7z")
            return {"ok": True, "code": 0, "log": "", "progress": ""}

        extracted = {}

        def fake_extract(archive, extract_dir, password, cfg, progress_cb):
            extracted["archive"] = Path(archive).name
            extracted["siblings"] = sorted(p.name for p in Path(archive).parent.iterdir())
            (extract_dir / "track01.flac").write_bytes(b"audio")
            return ""

        originals = (handoff._run_stream, handoff._extract_archive)
        handoff._run_stream, handoff._extract_archive = fake_run, fake_extract
        try:
            result = handoff.run_import_target(code, cfg, progress_cb=progress.append)
        finally:
            handoff._run_stream, handoff._extract_archive = originals

        prefix = f"ASMR/{stem}"
        assert extracted["archive"] == volumes[0]
        assert extracted["siblings"] == sorted(volumes)
        # 首卷重试一次，其余分卷各下载一次
        assert attempts[volumes[0]] == 2
        assert [attempts[name] for name in volumes[1:]] == [1, 1]
        assert all(remote.startswith(prefix) for remote in calls)
        assert result["count"] == 1
        assert result["transfer"]["volume_count"] == 3
        assert result["transfer"]["volumes"] == volumes
        assert any("下载分卷 (2/3)" in item for item in progress), progress
        assert "Volumes: 3" in result["log"]


def test_single_volume_manifests_keep_the_legacy_download_path():
    """Without a volumes list the import still downloads one named archive."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(Path(tmp))
        code = _manifest(cat="asmr")
        calls: list[list[str]] = []

        def fake_run(cmd, timeout_seconds, progress_cb, label):
            calls.append(cmd)
            local = Path(cmd[3])
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(b"7z")
            return {"ok": True, "code": 0, "log": "", "progress": ""}

        def fake_extract(archive, extract_dir, password, cfg, progress_cb):
            (extract_dir / "track01.flac").write_bytes(b"audio")
            return ""

        originals = (handoff._run_stream, handoff._extract_archive)
        handoff._run_stream, handoff._extract_archive = fake_run, fake_extract
        try:
            result = handoff.run_import_target(code, cfg)
        finally:
            handoff._run_stream, handoff._extract_archive = originals

        assert len(calls) == 1
        assert calls[0][2] == "/apps/bdpan/ASMR/work_ab12cd.7z"
        assert result["count"] == 1
        assert result["transfer"]["volume_count"] == 0


def test_bdpan_briefing_lists_volume_count_and_total_size():
    """A split package must report how many parts were uploaded and the total size."""
    _drain_notifications()
    task = tasks.Task(
        id="download_split", type="download", status="completed", target="RJ9",
        resolved_path="asmr", output_path="我的应用数据/bdpan/ASMR/pkg_20261101_8f3a9b",
        transfer={
            "engine": "bdpan", "moved": 5, "password": "Pass1234",
            "archive_name": "pkg_20261101_8f3a9b.7z",
            "volumes": [f"pkg_20261101_8f3a9b.7z.{i:03d}" for i in (1, 2, 3)],
            "volume_count": 3, "archive_size": 3 * 1024 * 1024 * 1024,
            "saved_path": "我的应用数据/bdpan/ASMR/pkg_20261101_8f3a9b",
        },
        delivery_target="telegram:1", started_at=time.time(), finished_at=time.time() + 1,
    )
    tasks.queue_task_notification(task)
    msg = _drain_notifications()
    assert "分卷数量</b>: 3 卷" in msg
    assert "3.0 GB" in msg


def _drain_notifications() -> str:
    msg = ""
    while True:
        try:
            _, msg = tasks._NOTIFICATION_QUEUE.get_nowait()
            tasks._NOTIFICATION_QUEUE.task_done()
        except Exception:
            break
    return msg


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
