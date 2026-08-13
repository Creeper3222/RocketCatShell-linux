from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from rocketcat_shell.update_manifest import (
    CONTAINER_RUNTIME_GENERATION,
    IMAGE_DEPLOYMENT_FILES,
    MANAGED_DIRECTORIES,
    MANAGED_FILES,
    MANIFEST_NAME,
    PACKAGE_ROOT_DIRECTORY,
    UpdatePackageError,
    audit_release_source_contract,
    build_manifest,
    compare_tags,
    inspect_and_extract_zip,
    parse_tag,
    write_manifest,
)


def create_release_tree(root: Path, *, version: str = "v0.2.2", marker: str = "base") -> None:
    for relative in MANAGED_DIRECTORIES:
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        payload = directory / "payload.txt"
        payload.write_text(f"{relative}:{marker}\n", encoding="utf-8")
    (root / "rocketcat_shell" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    for relative in (*MANAGED_FILES, *IMAGE_DEPLOYMENT_FILES):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(f"{relative}:{marker}\n", encoding="utf-8")


def write_release_zip(tree: Path, output: Path, *, extra: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(tree.parent).as_posix())
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)


class SemanticVersionTests(unittest.TestCase):
    def test_semver_orders_stable_and_prereleases(self) -> None:
        self.assertGreater(compare_tags("v0.2.3", "v0.2.3-rc.1"), 0)
        self.assertGreater(compare_tags("v0.2.3-rc.2", "v0.2.3-rc.1"), 0)
        self.assertLess(compare_tags("v0.2.2", "v0.2.3"), 0)

    def test_semver_rejects_noncanonical_prereleases(self) -> None:
        for value in ("0.2.2", "v0.2", "v0.2.3-01", "v00.2.3"):
            with self.subTest(value=value), self.assertRaises(UpdatePackageError):
                parse_tag(value)


class UpdateManifestArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rocketcat-linux-manifest-")
        self.root = Path(self.temporary.name)
        self.tree = self.root / PACKAGE_ROOT_DIRECTORY
        self.tree.mkdir()
        create_release_tree(self.tree)
        write_manifest(self.tree, version="v0.2.2", tag_name="v0.2.2")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _archive(self, name: str = "release.zip", *, extra: dict[str, bytes] | None = None) -> Path:
        output = self.root / name
        write_release_zip(self.tree, output, extra=extra)
        return output

    def test_valid_archive_round_trip(self) -> None:
        candidate, manifest = inspect_and_extract_zip(
            self._archive(), self.root / "extract", expected_tag="v0.2.2"
        )
        self.assertEqual(candidate.name, PACKAGE_ROOT_DIRECTORY)
        self.assertEqual(manifest["platform"], "linux")
        self.assertEqual(manifest["container_runtime_generation"], 1)
        self.assertEqual(tuple(manifest["image_deployment_files"]), IMAGE_DEPLOYMENT_FILES)

    def test_rejects_versions_below_transaction_floor(self) -> None:
        with self.assertRaises(UpdatePackageError):
            build_manifest(self.tree, version="v0.2.1", tag_name="v0.2.1")

    def test_rejects_hash_internal_version_and_runtime_generation_mismatch(self) -> None:
        cases = ("hash", "version", "generation")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                case_root = self.root / f"case-{index}"
                shutil.copytree(self.tree, case_root)
                manifest_path = case_root / MANIFEST_NAME
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if case == "hash":
                    manifest["files"][0]["sha256"] = "0" * 64
                elif case == "version":
                    (case_root / "rocketcat_shell" / "__init__.py").write_text(
                        '__version__ = "v9.9.9"\n', encoding="utf-8"
                    )
                else:
                    manifest["container_runtime_generation"] = CONTAINER_RUNTIME_GENERATION + 1
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                archive = self.root / f"invalid-{index}.zip"
                write_release_zip(case_root, archive)
                with self.assertRaises(UpdatePackageError):
                    inspect_and_extract_zip(
                        archive, self.root / f"extract-{index}", expected_tag="v0.2.2"
                    )

    def test_rejects_missing_image_or_managed_file(self) -> None:
        for index, relative in enumerate(("Dockerfile", "requirements.txt")):
            case_root = self.root / f"missing-{index}"
            shutil.copytree(self.tree, case_root)
            (case_root / relative).unlink()
            manifest = json.loads((case_root / MANIFEST_NAME).read_text(encoding="utf-8"))
            manifest["files"] = [item for item in manifest["files"] if item["path"] != relative]
            (case_root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
            archive = self.root / f"missing-{index}.zip"
            write_release_zip(case_root, archive)
            with self.subTest(relative=relative), self.assertRaises(UpdatePackageError):
                inspect_and_extract_zip(
                    archive, self.root / f"missing-extract-{index}", expected_tag="v0.2.2"
                )

    def test_rejects_traversal_absolute_backslash_colon_and_multiple_roots(self) -> None:
        cases = (
            {f"{PACKAGE_ROOT_DIRECTORY}/../escape.txt": b"x"},
            {"/absolute.txt": b"x"},
            {f"{PACKAGE_ROOT_DIRECTORY}\\evil.txt": b"x"},
            {f"{PACKAGE_ROOT_DIRECTORY}/bad:name.txt": b"x"},
            {"OtherRoot/file.txt": b"x"},
        )
        for index, extra in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(UpdatePackageError):
                inspect_and_extract_zip(
                    self._archive(f"unsafe-{index}.zip", extra=extra),
                    self.root / f"unsafe-extract-{index}",
                    expected_tag="v0.2.2",
                )

    def test_rejects_case_collision_unknown_and_protected_paths(self) -> None:
        cases = (
            {f"{PACKAGE_ROOT_DIRECTORY}/README.MD": b"collision"},
            {f"{PACKAGE_ROOT_DIRECTORY}/unknown.txt": b"unknown"},
            {f"{PACKAGE_ROOT_DIRECTORY}/config/shell.json": b"protected"},
            {f"{PACKAGE_ROOT_DIRECTORY}/data/bots/state.bin": b"protected"},
        )
        for index, extra in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(UpdatePackageError):
                inspect_and_extract_zip(
                    self._archive(f"contract-{index}.zip", extra=extra),
                    self.root / f"contract-extract-{index}",
                    expected_tag="v0.2.2",
                )

    def test_rejects_symbolic_link_entry(self) -> None:
        archive = self._archive("symlink.zip")
        with zipfile.ZipFile(archive, "a") as output:
            info = zipfile.ZipInfo(f"{PACKAGE_ROOT_DIRECTORY}/rocketcat_shell/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(info, "target")
        with self.assertRaises(UpdatePackageError):
            inspect_and_extract_zip(archive, self.root / "symlink-extract", expected_tag="v0.2.2")


class ReleaseSourceAuditTests(unittest.TestCase):
    def _repository(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory(prefix="rocketcat-linux-source-audit-")
        root = Path(temporary.name)
        create_release_tree(root)
        source_only = root / "tools" / "build_linux_release.py"
        source_only.write_text("# build tool\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
        return temporary, root

    def test_clean_contract_and_source_only_brand_pass(self) -> None:
        temporary, root = self._repository()
        try:
            asset = root / "assets" / "logo.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"source-only")
            subprocess.run(["git", "-C", str(root), "add", "assets/logo.png"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "asset"], check=True)
            tracked = audit_release_source_contract(root)
            self.assertIn("assets/logo.png", tracked)
            self.assertIn("docker/entrypoint.sh", tracked)
        finally:
            temporary.cleanup()

    def test_untracked_runtime_and_unclassified_tool_are_rejected(self) -> None:
        for index, relative in enumerate(("rocketcat_shell/untracked.py", "tools/new_runtime_tool.py")):
            temporary, root = self._repository()
            try:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x = 1\n", encoding="utf-8")
                if index == 1:
                    subprocess.run(["git", "-C", str(root), "add", relative], check=True)
                    subprocess.run(["git", "-C", str(root), "commit", "-qm", "tool"], check=True)
                with self.subTest(relative=relative), self.assertRaises(UpdatePackageError):
                    audit_release_source_contract(root)
            finally:
                temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
