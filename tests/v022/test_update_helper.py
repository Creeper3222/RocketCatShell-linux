from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "tools" / "update_helper.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("rocketcat_linux_update_helper_test", HELPER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load_helper()


def write_managed_tree(root: Path, marker: str, *, omit: set[str] | None = None) -> None:
    omitted = omit or set()
    for relative in helper.MANAGED_DIRECTORIES:
        if relative in omitted:
            continue
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "payload.txt").write_text(f"{relative}:{marker}\n", encoding="utf-8")
    for relative in helper.MANAGED_FILES:
        if relative in omitted:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}:{marker}\n", encoding="utf-8")


def candidate_entries(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "update-manifest.json":
            continue
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return entries


def write_image_files(root: Path, marker: str = "image") -> None:
    for relative in helper.IMAGE_DEPLOYMENT_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}:{marker}\n", encoding="utf-8")


class UpdateHelperPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rocketcat-linux-helper-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "install"
        self.candidate = self.root / "candidate"
        self.backup = self.root / "backup"
        self.runtime = self.source / "data" / "update" / "runtime.json"
        self.source.mkdir()
        self.candidate.mkdir()
        write_managed_tree(self.source, "old", omit={"LICENSE"})
        write_managed_tree(self.candidate, "new")
        (self.source / "config").mkdir()
        (self.source / "config" / "shell.json").write_text("protected", encoding="utf-8")
        (self.source / "data" / "plugins" / "user_plugin").mkdir(parents=True)
        (self.source / "data" / "plugins" / "user_plugin" / "main.py").write_text(
            "protected", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_helper_frozen_contract_matches_runtime_validator(self) -> None:
        from rocketcat_shell import update_manifest

        self.assertEqual(helper.MANAGED_DIRECTORIES, update_manifest.MANAGED_DIRECTORIES)
        self.assertEqual(helper.MANAGED_FILES, update_manifest.MANAGED_FILES)
        self.assertEqual(helper.IMAGE_DEPLOYMENT_FILES, update_manifest.IMAGE_DEPLOYMENT_FILES)
        self.assertEqual(helper.CONTAINER_RUNTIME_GENERATION, 1)

    def test_backup_install_restore_preserves_protected_paths_and_absence(self) -> None:
        helper._backup(self.source, self.backup, self.runtime)
        helper._install(self.source, self.candidate)
        self.assertEqual((self.source / "README.md").read_text(encoding="utf-8"), "README.md:new\n")
        self.assertEqual((self.source / "config" / "shell.json").read_text(encoding="utf-8"), "protected")
        self.assertEqual(
            (self.source / "data" / "plugins" / "user_plugin" / "main.py").read_text(encoding="utf-8"),
            "protected",
        )
        helper._restore(self.source, self.backup, self.runtime)
        self.assertFalse((self.source / "LICENSE").exists())
        self.assertEqual((self.source / "README.md").read_text(encoding="utf-8"), "README.md:old\n")
        self.assertEqual((self.source / "config" / "shell.json").read_text(encoding="utf-8"), "protected")

    def test_restore_validates_complete_backup_before_removing_source(self) -> None:
        helper._backup(self.source, self.backup, self.runtime)
        (self.backup / "complete.json").unlink()
        original = (self.source / "README.md").read_bytes()
        with self.assertRaises(helper.UpdateHelperError):
            helper._restore(self.source, self.backup, self.runtime)
        self.assertEqual((self.source / "README.md").read_bytes(), original)

    def test_candidate_hashes_and_image_only_files_are_checked(self) -> None:
        write_image_files(self.candidate)
        payload = {"candidate_files": candidate_entries(self.candidate)}
        helper._validate_candidate(self.candidate, payload)
        (self.candidate / "rocketcat_shell" / "payload.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaises(helper.UpdateHelperError):
            helper._validate_candidate(self.candidate, payload)

    def test_runtime_marker_is_backed_up_and_restored(self) -> None:
        self.runtime.parent.mkdir(parents=True)
        helper._atomic_write_json(self.runtime, {"runtime_version": "v0.2.2", "python": "old"})
        helper._backup(self.source, self.backup, self.runtime)
        helper._atomic_write_json(self.runtime, {"runtime_version": "v0.2.3", "python": "new"})
        helper._restore(self.source, self.backup, self.runtime)
        self.assertEqual(helper._read_json(self.runtime)["runtime_version"], "v0.2.2")


class UpdateHelperRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rocketcat-linux-helper-recovery-")
        self.source = Path(self.temporary.name) / "install"
        self.transaction_id = "a" * 24
        self.transaction_root = (
            self.source / "data" / "update" / "transactions" / self.transaction_id
        )
        self.candidate = self.transaction_root / "candidate" / helper.PACKAGE_ROOT_DIRECTORY
        self.transaction_file = self.transaction_root / "transaction.json"
        self.backup = self.transaction_root / "backup"
        self.runtime = self.source / "data" / "update" / "runtime.json"
        self.source.mkdir()
        self.candidate.mkdir(parents=True)
        write_managed_tree(self.source, "old")
        write_managed_tree(self.candidate, "new")
        write_image_files(self.candidate)
        now = time.time()
        self.payload = {
            "transaction_id": self.transaction_id,
            "status": "prepared",
            "stage": "waiting_for_shutdown",
            "current_version": "v0.2.2",
            "target_version": "v0.2.3",
            "target_tag": "v0.2.3",
            "source_root": str(self.source),
            "state_root": str(self.source),
            "candidate_root": str(self.candidate),
            "candidate_files": candidate_entries(self.candidate),
            "old_python": str(Path(__import__("sys").executable).resolve()),
            "image_version": "v0.2.2",
            "health_urls": ["http://127.0.0.1:5751"],
            "created_at": now,
            "updated_at": now,
        }
        helper._atomic_write_json(self.transaction_file, self.payload)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pre_replacement_restart_marks_transaction_failed(self) -> None:
        self.assertEqual(helper.recover_transactions(self.source), 0)
        transaction = helper._read_json(self.transaction_file)
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(transaction["stage"], "interrupted_before_replacement")
        self.assertEqual((self.source / "README.md").read_text(encoding="utf-8"), "README.md:old\n")

    def test_interrupted_replacement_restores_and_starts_rollback_watch(self) -> None:
        helper._backup(self.source, self.backup, self.runtime)
        helper._install(self.source, self.candidate)
        payload = helper._read_json(self.transaction_file)
        helper._update_transaction(
            self.transaction_file, payload, status="applying", stage="replacing"
        )
        with mock.patch.object(helper, "_spawn_watchdog") as watchdog:
            self.assertEqual(helper.recover_transactions(self.source), 0)
        self.assertEqual((self.source / "README.md").read_text(encoding="utf-8"), "README.md:old\n")
        transaction = helper._read_json(self.transaction_file)
        self.assertEqual(transaction["status"], "rolling_back")
        self.assertEqual(transaction["stage"], "monitoring_rollback")
        watchdog.assert_called_once()

    def test_missing_backup_requires_manual_recovery(self) -> None:
        payload = helper._read_json(self.transaction_file)
        helper._update_transaction(
            self.transaction_file, payload, status="applying", stage="replacing"
        )
        self.assertEqual(helper.recover_transactions(self.source), 1)
        transaction = helper._read_json(self.transaction_file)
        self.assertEqual(transaction["status"], "recovery_required")
        self.assertEqual(transaction["stage"], "backup_incomplete")

    def test_target_watch_success_completes_and_clears_runtime_transaction(self) -> None:
        helper._atomic_write_json(
            self.runtime,
            helper._runtime_payload(
                version="v0.2.3",
                python=str(Path(__import__("sys").executable).resolve()),
                image_version="v0.2.2",
                transaction_id=self.transaction_id,
                mode="target_monitoring",
            ),
        )
        payload = helper._read_json(self.transaction_file)
        helper._update_transaction(
            self.transaction_file, payload, status="monitoring", stage="monitoring_target"
        )
        with mock.patch.object(helper, "_health_matches", return_value=True):
            self.assertEqual(helper.watch_transaction(self.transaction_file, "target"), 0)
        self.assertEqual(helper._read_json(self.transaction_file)["status"], "completed")
        runtime = helper._read_json(self.runtime)
        self.assertEqual(runtime["active_transaction"], "")
        self.assertEqual(runtime["mode"], "active")

    def test_target_watch_failure_requests_container_restart(self) -> None:
        payload = helper._read_json(self.transaction_file)
        helper._update_transaction(
            self.transaction_file, payload, status="monitoring", stage="monitoring_target"
        )
        with (
            mock.patch.object(helper, "_health_matches", return_value=False),
            mock.patch.object(helper.os, "kill") as kill,
            mock.patch.object(helper.time, "sleep"),
        ):
            self.assertEqual(helper.watch_transaction(self.transaction_file, "target"), 1)
        self.assertEqual(helper._read_json(self.transaction_file)["status"], "rollback_pending")
        self.assertEqual(kill.call_count, 2)


if __name__ == "__main__":
    unittest.main()
