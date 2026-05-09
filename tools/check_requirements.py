from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

try:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name
except Exception:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name


IGNORED_PREFIXES = (
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "--trusted-host",
    "--constraint",
    "-i ",
    "-f ",
    "-c ",
)


def load_requirements(requirements_path: Path, *, visited: set[Path] | None = None) -> list[Requirement]:
    normalized_path = requirements_path.resolve()
    if visited is None:
        visited = set()
    if normalized_path in visited:
        return []
    visited.add(normalized_path)

    requirements: list[Requirement] = []
    for line_number, raw_line in enumerate(normalized_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("-r ") or line.startswith("--requirement "):
            _, nested_path_text = line.split(maxsplit=1)
            nested_path = (normalized_path.parent / nested_path_text.strip()).resolve()
            requirements.extend(load_requirements(nested_path, visited=visited))
            continue

        if line.startswith(IGNORED_PREFIXES):
            continue

        try:
            requirements.append(Requirement(line))
        except Exception as exc:
            raise ValueError(f"{normalized_path}:{line_number}: unable to parse requirement '{line}': {exc}") from exc

    return requirements


def build_installed_version_map() -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        installed[canonicalize_name(name)] = distribution.version
    return installed


def find_unsatisfied_requirements(requirements: list[Requirement]) -> list[str]:
    installed = build_installed_version_map()
    issues: list[str] = []

    for requirement in requirements:
        if requirement.marker and not requirement.marker.evaluate():
            continue

        installed_version = installed.get(canonicalize_name(requirement.name))
        if installed_version is None:
            issues.append(f"{requirement.name}{requirement.specifier} is not installed")
            continue

        if requirement.specifier and installed_version not in requirement.specifier:
            issues.append(
                f"{requirement.name} {installed_version} does not satisfy {requirement.specifier}"
            )

    return issues


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: check_requirements.py <requirements.txt>")
        return 2

    requirements_path = Path(args[0])
    if not requirements_path.exists():
        print(f"requirements.txt was not found: {requirements_path}")
        return 2

    try:
        requirements = load_requirements(requirements_path)
    except Exception as exc:
        print(f"Failed to inspect requirements: {exc}")
        return 2

    issues = find_unsatisfied_requirements(requirements)
    if not issues:
        return 0

    print("Unsatisfied requirements detected:")
    for issue in issues:
        print(f"  - {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())