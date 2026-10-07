from __future__ import annotations

import hashlib
import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.validate_linux_update_compat import UpdateHarness


class LinuxValidationContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_is_read_from_transaction_after_normal_restart(self):
        with tempfile.TemporaryDirectory(prefix="rocketcat-validator-contract-") as temporary:
            root = Path(temporary)
            app, candidate = root / "app", root / "candidate"
            app.mkdir()
            candidate.mkdir()
            payload = b"synthetic managed source\n"
            (app / "README.md").write_bytes(payload)
            manifest = {
                "version": "v0.2.4", "managed_directories": [], "managed_files": ["README.md"],
                "image_deployment_files": [],
                "files": [{"path": "README.md", "size": len(payload),
                           "sha256": hashlib.sha256(payload).hexdigest()}],
            }
            (candidate / "update-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            transaction_root = app / "data/update/transactions/synthetic-transaction"
            transaction_root.mkdir(parents=True)
            (transaction_root / "transaction.json").write_text(
                json.dumps({"candidate_root": str(candidate), "target_tag": "v0.2.4"}), encoding="utf-8")
            harness = object.__new__(UpdateHarness)
            harness.name = "rocketcat-v024-validation-synthetic"
            harness.source_tag = "v0.2.3"
            harness.target_manifest = manifest
            harness.target_reference = candidate
            harness.report = {
                "last_health": {"version": "v0.2.4"},
                "transactions": [{"transaction_id": "synthetic-transaction"}], "checks": [],
            }
            harness.immutable_before = {}

            async def immutable_hashes():
                return {}

            def synthetic_exec(*args):
                self.assertEqual(("exec", harness.name, "python", "-c"), args[:4])
                script = args[4].replace("r=Path('/app')", "r=Path(" + repr(str(app)) + ")")
                result = subprocess.run([sys.executable, "-c", script, *args[5:]],
                                        capture_output=True, text=True, check=True)
                return result.stdout

            harness.immutable_hashes = immutable_hashes
            harness.docker = synthetic_exec
            await harness.assert_managed()
            self.assertFalse((app / "update-manifest.json").exists())
            self.assertEqual(1, len(harness.report["checks"]))
            # Health-failure recovery restores source files, but its transaction
            # still retains the original target candidate and manifest.
            old_candidate = root / "old-candidate"
            old_candidate.mkdir()
            harness.source_manifest = dict(manifest, version="v0.2.3")
            harness.source_reference = old_candidate
            (old_candidate / "update-manifest.json").write_text(
                json.dumps(harness.source_manifest), encoding="utf-8")
            await harness.assert_managed(source=True)
            (app / "README.md").write_bytes(b"corrupted source")
            with self.assertRaisesRegex(RuntimeError, "differs from official reference"):
                await harness.assert_managed()

    async def test_container_guard_refuses_production_target(self):
        harness = object.__new__(UpdateHarness)
        harness.name = "rocketcat-v024-validation-synthetic"
        with self.assertRaisesRegex(RuntimeError, "outside the isolated container"):
            harness.docker("restart", "RocketCatShell")

    async def test_live_identity_check_uses_linux_sqlite_view(self):
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="rocketcat-validator-identity-") as temporary:
            root = Path(temporary)
            (root / "data/user_identity").mkdir(parents=True)
            with contextlib.closing(sqlite3.connect(root / "data/user_identity/validation-preserved.sqlite3")) as connection:
                connection.execute("CREATE TABLE validation_state(marker TEXT, payload TEXT)")
                connection.execute("INSERT INTO validation_state VALUES ('upgrade','synthetic-identity-preserved')")
                connection.commit()
            scope = root / "data/bots/synthetic-bot/identity_scope.json"
            scope.parent.mkdir(parents=True)
            container_db = "/app/data/user_identity/active-runtime.sqlite3"
            scope.write_text(json.dumps({"database_path": container_db}), encoding="utf-8")
            harness = object.__new__(UpdateHarness)
            harness.mount_root, harness.bot_id = root, "synthetic-bot"
            harness.name = "rocketcat-v024-validation-synthetic"
            harness.sentinels, harness.report = {}, {}
            calls = []

            def synthetic_exec(*args):
                calls.append(args)
                self.assertEqual(("exec", harness.name, "python", "-c"), args[:4])
                self.assertIn("mode=ro", args[4])
                self.assertEqual(container_db, args[5])
                return "ok"

            harness.docker = synthetic_exec
            await harness.assert_sentinels()
            self.assertEqual(1, len(calls))
            self.assertFalse((root / "data/user_identity/active-runtime.sqlite3").exists())

    async def test_interruption_is_frozen_before_switch_and_restores_clean_helper(self):
        with tempfile.TemporaryDirectory(prefix="rocketcat-validator-interruption-") as temporary:
            root = Path(temporary)
            helper = root / "app/tools/update_helper.py"
            helper.parent.mkdir(parents=True)
            original = (Path(__file__).resolve().parents[2] / "tools/update_helper.py").read_bytes()
            helper.write_bytes(original)
            harness = object.__new__(UpdateHarness)
            harness.name = "rocketcat-v024-validation-synthetic"
            harness.args = SimpleNamespace(target_tag="v0.2.4")

            def synthetic_exec(*args):
                self.assertEqual(("exec", harness.name, "python", "-c"), args[:4])
                script = args[4].replace("Path('/app/data/update')", "Path(" + repr(str(root / "app/data/update")) + ")")
                script = script.replace("Path('/app/tools/update_helper.py')", "Path(" + repr(str(helper)) + ")")
                subprocess.run([sys.executable, "-c", script], check=True)
                return ""

            harness.docker = synthetic_exec
            await harness.inject_failure("interrupt")
            self.assertEqual(original, (root / "app/data/update/validation-helper-original.py").read_bytes())
            patched = helper.read_text(encoding="utf-8")
            compile(patched, str(helper), "exec")
            self.assertLess(patched.index("write_bytes(Path('/app/data/update/validation-helper-original.py')"),
                            patched.index("_backup(source_root, backup_root, runtime_path)"))
            self.assertIn("time.sleep(60)", patched)
