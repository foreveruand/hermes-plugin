"""Regression tests for profile-scoped config resolution and briefing delivery.

Run directly: ``python3 tests/test_config_resolution.py`` (also pytest-compatible).
"""

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = PLUGIN_ROOT.parent.parent  # <profile>/plugins/<plugin> -> <profile>
EXPECTED_NAME = PROFILE_DIR.name if PROFILE_DIR.parent.name == "profiles" else "default"

sys.path.insert(0, str(PLUGIN_ROOT))

import config  # noqa: E402
import core  # noqa: E402
import bdpan  # noqa: E402
import tasks  # noqa: E402


def test_profile_resolution_ignores_root_hermes_home():
    """A host pointed at the root home must not steal this plugin's owning profile."""
    saved = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(Path.home() / ".hermes")
    try:
        profile_dir = config.resolve_hermes_profile_dir()
        assert profile_dir.resolve() == PROFILE_DIR.resolve(), profile_dir
        assert config.resolve_hermes_profile_name(PROFILE_DIR) == EXPECTED_NAME
    finally:
        if saved is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = saved


def test_load_config_keeps_default_destinations():
    """Overriding some destinations must not drop the others (e.g. asmr_r)."""
    cfg = config.load_config()
    destinations = cfg["destinations"]
    for key in ("asmr", "asmr_r", "bilibili", "pixiv", "extract"):
        assert key in destinations, f"missing destinations.{key}: {destinations}"
    # Host config must be read from the owning profile, not the root home.
    assert cfg["config_dir"] == str(PROFILE_DIR / "data" / "media-downloader")
    assert cfg["tasks_dir"] == str(PROFILE_DIR / "data" / "media-downloader" / "tasks")


def test_send_command_is_portable_and_profile_scoped():
    """The profile must be explicit and the non-portable flag must be gone."""
    cmd = tasks._build_send_command(EXPECTED_NAME, "telegram:1:2", "hi")
    assert cmd == ["hermes", "--profile", EXPECTED_NAME, "send", "--to", "telegram:1:2", "hi"]
    assert "--no-preview" not in cmd
    assert tasks._resolve_send_profile() == EXPECTED_NAME


def test_normalize_rclone_progress_is_single_line():
    """A raw rclone stats line must collapse into one compact progress string."""
    line = "29.877 MiB / 365.731 MiB, 8%, 9.769 KiB/s, ETA 9h46m46s (xfr#4/13)"
    out = core._normalize_rclone_progress(line)
    assert "\n" not in out
    assert "8%" in out and "9.769 KiB/s" in out and "ETA 9h46m46s" in out
    assert core._normalize_rclone_progress("2024/01/01 ERROR : x: Failed to copy") == ""
    per_file = "* track01.flac:  8% /35.731MiB, 9.808KiB/s, 9h44m"
    assert core._normalize_rclone_progress(per_file).startswith("8%")


def test_rclone_failure_message_has_counts_not_progress():
    """The failure reason must carry success/fail counts and drop progress noise."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "RJ01633051"
        staging.mkdir()
        (staging / "a.flac").write_bytes(b"x")
        (staging / "b.flac").write_bytes(b"y")

        cfg = dict(core.DEFAULT_CONFIG)
        cfg["staging_fallback"] = tmp
        cfg["upload_mode"] = "rclone"

        def fake_transfer(staging_dir, destination, cfg, progress_cb=None):
            return {"ok": False, "moved": 1, "total": 2, "errors": 1, "progress":
                    "50% · 9.8 KiB/s · ETA 1h", "log": "ERROR : b.flac: Failed to copy: denied"}

        original = core.rclone_transfer
        core.rclone_transfer = fake_transfer
        try:
            try:
                core.archive_staged_files(staging, "remote:Media/ASMR", cfg)
                assert False, "expected RuntimeError"
            except RuntimeError as e:
                message = str(e)
        finally:
            core.rclone_transfer = original

        assert "成功 1/2" in message and "失败 1" in message
        assert "Failed to copy" in message
        assert "ETA 1h" not in message and "KiB/s" not in message


def test_asmr_briefing_prints_only_rj_directory():
    """ASMR completion must show the RJ directory, never the individual files."""
    _drain_notifications()
    rj_dir = "/mnt/media/MediaServer/ASMRAudioR/社团/RJ01633051"
    task = tasks.Task(
        id="download_test_asmr", type="download", status="completed",
        target="RJ01633051", resolved_path="asmr", output_path=rj_dir,
        files=[f"{rj_dir}/wav/track01.flac", f"{rj_dir}/wav/track02.flac"],
        transfer={"engine": "local", "moved": 2, "failed": 0, "total": 2},
        delivery_target="telegram:1", started_at=time.time(), finished_at=time.time() + 1,
    )
    tasks.queue_task_notification(task)
    msg = _drain_notifications()
    assert rj_dir in msg
    assert "track01.flac" not in msg
    assert "文件数" in msg


def test_rclone_briefing_shows_transfer_counts():
    """A completed rclone task must report success/failure file counts."""
    _drain_notifications()
    task = tasks.Task(
        id="download_test_rclone", type="download", status="completed",
        target="https://example.com/v", resolved_path="bilibili",
        output_path="remote:Media/bilibili",
        files=["remote:Media/bilibili/a.mp4"],
        transfer={"engine": "rclone", "moved": 4, "failed": 9, "total": 13},
        delivery_target="telegram:1", started_at=time.time(), finished_at=time.time() + 1,
    )
    tasks.queue_task_notification(task)
    msg = _drain_notifications()
    assert "成功 <code>4</code>" in msg and "失败 <code>9</code>" in msg


def test_legacy_task_json_loads_without_new_fields():
    """Task records written before progress/transfer existed must still load."""
    with tempfile.TemporaryDirectory() as tmp:
        legacy = {"id": "old_1", "type": "download", "status": "completed",
                  "created_at": 1.0, "target": "RJ1", "resolved_path": "asmr",
                  "params": {}, "files": []}
        (Path(tmp) / "old_1.json").write_text(json.dumps(legacy), encoding="utf-8")
        loaded = tasks.load_task("old_1", tmp)
        assert loaded is not None
        assert loaded.progress == "" and loaded.transfer == {}


def test_upload_mode_and_bdpan_defaults():
    """upload_mode defaults to local, rclone no longer carries an enabled flag."""
    assert config.DEFAULT_CONFIG["upload_mode"] == "local"
    assert "enabled" not in config.DEFAULT_CONFIG["rclone"]
    cfg = config.load_config()
    assert cfg.get("upload_mode") in ("local", "rclone", "bdpan")
    for key in ("bin", "remote_dir", "seven_zip_bin", "password_length", "timeout_seconds"):
        assert key in cfg["bdpan"], f"missing bdpan.{key}"


def test_bdpan_volume_and_obfuscation_defaults():
    """Volumes and neutral archive names are on by default and survive load_config."""
    bd = config.DEFAULT_CONFIG["bdpan"]
    assert bd["volume_size"] == "2g"
    assert bd["archive_name_prefix"] == "pkg"
    assert bd["obfuscate_filename"] is True
    assert bd["upload_retries"] == 3
    loaded = config.load_config()["bdpan"]
    for key in ("volume_size", "archive_name_prefix", "obfuscate_filename", "upload_retries"):
        assert key in loaded, f"missing bdpan.{key}"
    assert loaded["obfuscate_filename"] is True
    assert int(loaded["upload_retries"]) >= 1


def test_obfuscated_archive_name_is_neutral_and_dated():
    """Outer names carry only prefix + date + random hex, never any work metadata."""
    from datetime import datetime

    name = bdpan.build_obfuscated_archive_name("pkg", 6)
    assert re.fullmatch(r"pkg_\d{8}_[0-9a-f]{6}\.7z", name), name
    assert name.isascii()
    assert datetime.now().strftime("%Y%m%d") in name
    assert bdpan.build_obfuscated_archive_name("pkg", 6) != name
    # A CJK title/circle can never leak through as a prefix.
    assert bdpan.build_obfuscated_archive_name("蒼井かすみ", 4).startswith("pkg_")
    assert bdpan.build_obfuscated_archive_name("my pkg", 4).startswith("mypkg_")


def test_volume_size_reaches_the_7z_command():
    """A valid volume_size becomes -v<size>; disabled values keep one archive."""
    import io

    class FakeProc:
        def __init__(self, cmd):
            self.args = cmd
            self.returncode = 0
            self.stdout = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    seen: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        seen.append(list(cmd))
        return FakeProc(cmd)

    original = bdpan.subprocess.Popen
    bdpan.subprocess.Popen = fake_popen
    try:
        with tempfile.TemporaryDirectory() as tmp:
            assembly = Path(tmp) / "asm"
            assembly.mkdir()
            (assembly / "a.flac").write_bytes(b"x")
            bdpan.create_encrypted_archive(assembly, Path(tmp) / "p.7z", "pw", "7z",
                                           volume_size="2g")
            bdpan.create_encrypted_archive(assembly, Path(tmp) / "q.7z", "pw", "7z",
                                           volume_size="0")
            bdpan.create_encrypted_archive(assembly, Path(tmp) / "r.7z", "pw", "7z",
                                           volume_size="disabled")
    finally:
        bdpan.subprocess.Popen = original

    assert "-v2g" in seen[0], seen[0]
    assert not [arg for arg in seen[1] if str(arg).startswith("-v")]
    assert not [arg for arg in seen[2] if str(arg).startswith("-v")]
    assert bdpan.normalize_volume_size(" 1G ") == "1g"
    assert bdpan.normalize_volume_size("") == ""
    assert bdpan.normalize_volume_size("none") == ""


def test_volume_discovery_uses_natural_order():
    """7z writes .7z.001..N (never the unsplit file); discovery sorts numerically."""
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("pkg_1.7z.010", "pkg_1.7z.002", "pkg_1.7z.001"):
            (Path(tmp) / name).write_bytes(b"x")
        volumes = bdpan.find_archive_volumes(Path(tmp) / "pkg_1.7z")
        assert [v.name for v in volumes] == [
            "pkg_1.7z.001", "pkg_1.7z.002", "pkg_1.7z.010"]

        single = Path(tmp) / "single.7z"
        single.write_bytes(b"x")
        assert [v.name for v in bdpan.find_archive_volumes(single)] == ["single.7z"]
        assert bdpan.find_archive_volumes(Path(tmp) / "missing.7z") == []


def test_bdpan_multivolume_upload_uploads_each_volume_and_retries():
    """Volumes go up one by one under <remote_dir>/<stem>/; a bad part retries alone."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "RJ4"
        staging.mkdir()
        (staging / "a.flac").write_bytes(b"x")
        cfg = dict(core.DEFAULT_CONFIG)
        cfg["staging_fallback"] = tmp
        cfg["upload_mode"] = "bdpan"
        cfg["bdpan"] = dict(config.DEFAULT_CONFIG["bdpan"])
        cfg["bdpan"]["remote_dir"] = "ASMR"

        attempts: dict[str, int] = {}

        def fake_archive(assembly_dir, archive_path, password, *args, **kwargs):
            for index in (1, 2, 3):
                Path(f"{archive_path}.{index:03d}").write_bytes(b"v" * 10)
            return ""

        def fake_upload(archive_path, remote_rel, cfg, timeout_seconds=7200):
            attempts[remote_rel] = attempts.get(remote_rel, 0) + 1
            if remote_rel.endswith(".002") and attempts[remote_rel] == 1:
                return {"ok": False, "log": "connection reset"}
            return {"ok": True, "saved_path": f"我的应用数据/bdpan/{remote_rel}",
                    "remote_path": remote_rel}

        orig_archive, orig_upload = bdpan.create_encrypted_archive, bdpan.bdpan_upload
        orig_delay = bdpan._UPLOAD_RETRY_DELAY_SECONDS
        bdpan.create_encrypted_archive = fake_archive
        bdpan.bdpan_upload = fake_upload
        bdpan._UPLOAD_RETRY_DELAY_SECONDS = 0
        try:
            files, log, transfer = bdpan.bdpan_archive_transfer(staging, "蒼井かすみ", cfg)
        finally:
            bdpan.create_encrypted_archive = orig_archive
            bdpan.bdpan_upload = orig_upload
            bdpan._UPLOAD_RETRY_DELAY_SECONDS = orig_delay

        stem = transfer["remote_folder"].rsplit("/", 1)[-1]
        volume_names = [f"{stem}.7z.{i:03d}" for i in (1, 2, 3)]
        assert transfer["remote_folder"] == f"ASMR/{stem}"
        assert transfer["volumes"] == volume_names
        assert transfer["volume_count"] == 3
        assert transfer["archive_name"] == f"{stem}.7z"
        assert transfer["archive_size"] == 30
        assert transfer["saved_path"].endswith(f"ASMR/{stem}/{volume_names[0]}")
        assert files == [transfer["saved_path"]]
        assert "蒼井" not in transfer["archive_name"]
        assert list(attempts) == [f"ASMR/{stem}/{name}" for name in volume_names]
        assert attempts[f"ASMR/{stem}/{volume_names[1]}"] == 2
        assert "3 volume" in log
        # Local volumes are cleaned up after a successful upload.
        assert not list(Path(tmp).glob("*.7z*"))


def test_bdpan_multivolume_upload_failure_parks_every_volume():
    """A volume that keeps failing keeps every part + the password for a manual retry."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "RJ5"
        staging.mkdir()
        (staging / "a.flac").write_bytes(b"x")
        cfg = dict(core.DEFAULT_CONFIG)
        cfg["staging_fallback"] = tmp
        cfg["upload_mode"] = "bdpan"
        cfg["bdpan"] = dict(config.DEFAULT_CONFIG["bdpan"])

        def fake_archive(assembly_dir, archive_path, password, *args, **kwargs):
            for index in (1, 2, 3):
                Path(f"{archive_path}.{index:03d}").write_bytes(b"v" * 10)
            return ""

        def fake_upload(archive_path, remote_rel, cfg, timeout_seconds=7200):
            if remote_rel.endswith(".002"):
                return {"ok": False, "log": "bdpan 上传失败: 未登录"}
            return {"ok": True, "saved_path": f"我的应用数据/bdpan/{remote_rel}",
                    "remote_path": remote_rel}

        orig_archive, orig_upload = bdpan.create_encrypted_archive, bdpan.bdpan_upload
        orig_delay = bdpan._UPLOAD_RETRY_DELAY_SECONDS
        bdpan.create_encrypted_archive = fake_archive
        bdpan.bdpan_upload = fake_upload
        bdpan._UPLOAD_RETRY_DELAY_SECONDS = 0
        try:
            try:
                bdpan.bdpan_archive_transfer(staging, "title", cfg)
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                message = str(exc)
        finally:
            bdpan.create_encrypted_archive = orig_archive
            bdpan.bdpan_upload = orig_upload
            bdpan._UPLOAD_RETRY_DELAY_SECONDS = orig_delay

        assert "分卷 2/3" in message
        assert "已重试 3 次" in message
        assert "解压密码" in message
        assert "未登录" in message
        kept = sorted(p.name for p in (Path(tmp) / "RJ5").glob("*.7z.0*"))
        assert len(kept) == 3, kept
        assert list((Path(tmp) / "RJ5").glob("*.password.txt"))
        # Nothing is left behind in the working root.
        assert not list(Path(tmp).glob("*.7z*"))


def test_archive_dispatch_defaults_to_local():
    """Without an explicit upload_mode the local backend archives into the destination dir."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "stage"
        staging.mkdir()
        (staging / "a.txt").write_text("hi", encoding="utf-8")
        dest = Path(tmp) / "dest"
        cfg = dict(core.DEFAULT_CONFIG)
        cfg["staging_fallback"] = tmp
        _, _, transfer = core.archive_staged_files(staging, str(dest), cfg)
        assert transfer["engine"] == "local"
        assert (dest / "a.txt").is_file()


def test_bdpan_slug_password_and_archive_name():
    """Slugs keep CJK, passwords are random alphanumerics, names carry a random suffix."""
    assert bdpan.make_archive_slug("蒼井かすみ 薔薇園シアン!!") == "蒼井かすみ_薔薇園シアン"
    assert bdpan.make_archive_slug("") == "media"
    pw1, pw2 = bdpan.random_password(20), bdpan.random_password(20)
    assert len(pw1) == 20 and pw1 != pw2
    assert all(c.isalnum() for c in pw1)
    name = bdpan.build_archive_name("aoi_kasumi", 6)
    assert re.fullmatch(r"aoi_kasumi_[0-9a-f]{6}\.7z", name), name


def test_bdpan_upload_parses_nested_and_toplevel_json():
    """saved_path/return_url come from data.*; a flat object falls back to remote_path."""
    import types

    def fake_run_nested(cmd, **kwargs):
        return types.SimpleNamespace(
            returncode=0,
            stdout='progress\n{"code":0,"data":{"saved_path":"我的应用数据/bdpan/x.7z",'
                   '"return_url":"https://pan.baidu.com/union/spirit/launch?x=1","fsid":"123"}}\n',
        )

    def fake_run_flat(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout='{"code":0,"remote_path":"r/x.7z"}')

    def fake_run_not_logged_in(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="please login first")

    original = bdpan.subprocess.run
    cfg = dict(core.DEFAULT_CONFIG)
    try:
        bdpan.subprocess.run = fake_run_nested
        res = bdpan.bdpan_upload(Path("/tmp/x.7z"), "x.7z", cfg)
        assert res["ok"] and res["saved_path"] == "我的应用数据/bdpan/x.7z"
        assert res["return_url"].startswith("https://") and res["fsid"] == "123"

        bdpan.subprocess.run = fake_run_flat
        res = bdpan.bdpan_upload(Path("/tmp/x.7z"), "x.7z", cfg)
        assert res["ok"] and res["saved_path"] == "r/x.7z"

        bdpan.subprocess.run = fake_run_not_logged_in
        res = bdpan.bdpan_upload(Path("/tmp/x.7z"), "x.7z", cfg)
        assert not res["ok"] and "登录" in res["log"]
    finally:
        bdpan.subprocess.run = original


def test_bdpan_briefing_reports_path_password_encryption_and_structure():
    """The bdpan briefing must surface save path, link, password, encryption and layout."""
    _drain_notifications()
    task = tasks.Task(
        id="download_test_bdpan", type="download", status="completed",
        target="RJ01633051", resolved_path="asmr",
        output_path="我的应用数据/bdpan/aoi_kasumi_a3f9c2.7z",
        files=["我的应用数据/bdpan/aoi_kasumi_a3f9c2.7z"],
        transfer={
            "engine": "bdpan", "moved": 5, "failed": 0, "total": 5,
            "archive_name": "aoi_kasumi_a3f9c2.7z", "password": "S3cretPass123456",
            "saved_path": "我的应用数据/bdpan/aoi_kasumi_a3f9c2.7z",
            "return_url": "https://pan.baidu.com/union/spirit/launch?x=1",
            "encryption": "AES-256 + 文件名加密(-mhe=on)",
            "internal_structure": "蒼井かすみ/[蒼井かすみ×薔薇園シアン]",
        },
        delivery_target="telegram:1", started_at=time.time(), finished_at=time.time() + 1,
    )
    tasks.queue_task_notification(task)
    msg = _drain_notifications()
    assert "我的应用数据/bdpan/aoi_kasumi_a3f9c2.7z" in msg
    assert 'href="https://pan.baidu.com' in msg
    assert "S3cretPass123456" in msg
    assert "-mhe=on" in msg
    assert "蒼井かすみ" in msg


def test_bdpan_7z_failure_parks_plaintext_without_password():
    """A failed 7z run must preserve the plaintext download and never leak a password."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "RJ1"
        staging.mkdir()
        (staging / "a.flac").write_bytes(b"x")
        cfg = dict(core.DEFAULT_CONFIG)
        cfg["staging_fallback"] = tmp
        cfg["upload_mode"] = "bdpan"

        def boom(*args, **kwargs):
            raise RuntimeError("7z 压缩失败（退出码 2）")

        original = bdpan.create_encrypted_archive
        bdpan.create_encrypted_archive = boom
        try:
            try:
                bdpan.bdpan_archive_transfer(staging, "title", cfg, internal_prefix=("circle", "RJ1"))
                assert False, "expected RuntimeError"
            except RuntimeError as e:
                message = str(e)
        finally:
            bdpan.create_encrypted_archive = original

        assert "明文已暂存" in message
        assert "解压密码" not in message
        assert (Path(tmp) / "RJ1" / "circle" / "RJ1" / "a.flac").is_file()


def test_bdpan_upload_failure_keeps_archive_and_password():
    """A failed upload must keep the encrypted archive plus its password recoverable."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "RJ2"
        staging.mkdir()
        (staging / "a.flac").write_bytes(b"x")
        cfg = dict(core.DEFAULT_CONFIG)
        cfg["staging_fallback"] = tmp
        cfg["upload_mode"] = "bdpan"

        def fake_archive(assembly_dir, archive_path, password, *args, **kwargs):
            Path(archive_path).write_bytes(b"7zdata")
            return ""

        def fake_upload(archive_path, remote_rel, cfg, timeout_seconds=7200):
            return {"ok": False, "log": "bdpan 上传失败: 未登录"}

        orig_archive, orig_upload = bdpan.create_encrypted_archive, bdpan.bdpan_upload
        orig_delay = bdpan._UPLOAD_RETRY_DELAY_SECONDS
        bdpan.create_encrypted_archive = fake_archive
        bdpan.bdpan_upload = fake_upload
        bdpan._UPLOAD_RETRY_DELAY_SECONDS = 0
        try:
            try:
                bdpan.bdpan_archive_transfer(staging, "title", cfg)
                assert False, "expected RuntimeError"
            except RuntimeError as e:
                message = str(e)
        finally:
            bdpan.create_encrypted_archive = orig_archive
            bdpan.bdpan_upload = orig_upload
            bdpan._UPLOAD_RETRY_DELAY_SECONDS = orig_delay

        assert "bdpan 上传失败" in message and "解压密码" in message
        kept = list((Path(tmp) / "RJ2").glob("*.7z"))
        assert kept and kept[0].is_file()
        assert list((Path(tmp) / "RJ2").glob("*.password.txt"))


def test_bdpan_success_builds_transfer_and_cleans_up():
    """A successful run returns a full bdpan transfer and removes the local archive."""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "RJ3"
        staging.mkdir()
        (staging / "a.flac").write_bytes(b"x")
        cfg = dict(core.DEFAULT_CONFIG)
        cfg["staging_fallback"] = tmp
        cfg["upload_mode"] = "bdpan"

        seen = {}

        def fake_archive(assembly_dir, archive_path, password, *args, **kwargs):
            seen["structure"] = (Path(assembly_dir) / "circle" / "RJ3" / "a.flac").is_file()
            Path(archive_path).write_bytes(b"7zdata")
            return ""

        def fake_upload(archive_path, remote_rel, cfg, timeout_seconds=7200):
            seen["remote_rel"] = remote_rel
            return {
                "ok": True, "saved_path": f"我的应用数据/bdpan/{remote_rel}",
                "remote_path": remote_rel, "return_url": "https://pan.baidu.com/x",
                "return_markdown": "[点击查看](https://pan.baidu.com/x)", "fsid": "9",
            }

        orig_archive, orig_upload = bdpan.create_encrypted_archive, bdpan.bdpan_upload
        bdpan.create_encrypted_archive = fake_archive
        bdpan.bdpan_upload = fake_upload
        try:
            files, _, transfer = bdpan.bdpan_archive_transfer(
                staging, "蒼井かすみ", cfg, internal_prefix=("circle", "RJ3"))
        finally:
            bdpan.create_encrypted_archive = orig_archive
            bdpan.bdpan_upload = orig_upload

        assert seen["structure"] is True
        assert seen["remote_rel"].endswith(".7z")
        assert transfer["engine"] == "bdpan"
        assert transfer["saved_path"].startswith("我的应用数据/bdpan/")
        assert transfer["password"] and transfer["internal_structure"] == "circle/RJ3"
        assert transfer["encryption"].startswith("AES-256")
        assert files == [transfer["saved_path"]]
        assert not list(Path(tmp).glob("*.7z"))
        assert not list(Path(tmp).glob(".bdpan-asm-*"))


def _drain_notifications() -> str:
    """Return the single queued briefing text, clearing the queue."""
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
