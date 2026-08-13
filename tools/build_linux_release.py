from __future__ import annotations

import argparse
import json
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path


IGNORED_BUILD_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
IGNORED_BUILD_SUFFIXES = frozenset({".pyc", ".pyo"})


def _ignore_build_artifacts(_: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in IGNORED_BUILD_NAMES or Path(name).suffix in IGNORED_BUILD_SUFFIXES
    }


def _zip_info(path: Path, archive_name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo.from_file(path, archive_name)
    if path.name in {"launcher.sh", "entrypoint.sh"}:
        info.external_attr = (stat.S_IFREG | 0o755) << 16
    else:
        info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.create_system = 3
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify the RocketCatShell Linux release package"
    )
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    expected_name = f"RocketCatShell-linux-{args.tag}.zip"
    if output.name != expected_name:
        raise SystemExit(f"output filename must be {expected_name}")
    if output.exists():
        raise SystemExit(f"output already exists: {output}")

    import sys

    sys.path.insert(0, str(source))
    from rocketcat_shell.update_manifest import (  # noqa: PLC0415
        IMAGE_DEPLOYMENT_FILES,
        MANAGED_DIRECTORIES,
        MANAGED_FILES,
        MANIFEST_NAME,
        PACKAGE_ROOT_DIRECTORY,
        VERSION,
        audit_release_source_contract,
        inspect_and_extract_zip,
        write_manifest,
    )

    if args.tag != VERSION:
        raise SystemExit(
            f"release tag {args.tag} does not match runtime version {VERSION}"
        )
    tracked_files = audit_release_source_contract(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(f"{output.name}.tmp")
    if temporary_output.exists():
        raise SystemExit(f"temporary output already exists: {temporary_output}")

    with tempfile.TemporaryDirectory(prefix="rocketcat-linux-release-") as temporary:
        temporary_root = Path(temporary)
        staging = temporary_root / PACKAGE_ROOT_DIRECTORY
        staging.mkdir()
        for relative in MANAGED_DIRECTORIES:
            shutil.copytree(
                source / Path(relative),
                staging / Path(relative),
                ignore=_ignore_build_artifacts,
            )
        for relative in (*MANAGED_FILES, *IMAGE_DEPLOYMENT_FILES):
            target = staging / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / Path(relative), target)
        write_manifest(staging, version=args.tag, tag_name=args.tag)

        try:
            with zipfile.ZipFile(
                temporary_output,
                "x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                for path in sorted(staging.rglob("*"), key=lambda item: item.as_posix().casefold()):
                    if not path.is_file():
                        continue
                    archive_name = path.relative_to(staging.parent).as_posix()
                    info = _zip_info(path, archive_name)
                    archive.writestr(info, path.read_bytes())
            verify_root = temporary_root / "verify"
            _, manifest = inspect_and_extract_zip(
                temporary_output,
                verify_root,
                expected_tag=args.tag,
            )
            temporary_output.replace(output)
        finally:
            if temporary_output.exists():
                temporary_output.unlink()

    print(
        json.dumps(
            {
                "output": str(output),
                "tag": args.tag,
                "files": len(manifest["files"]),
                "tracked_source_files": len(tracked_files),
                "manifest": MANIFEST_NAME,
                "root_directory": PACKAGE_ROOT_DIRECTORY,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
