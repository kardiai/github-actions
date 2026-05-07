#!/usr/bin/env python3
"""
Parse requirements file for git+ssh:// dependencies,
clone them via HTTPS with a token, and build wheels using uv.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Pattern examples:
#   package @ git+ssh://git@github.com/org/repo@ref
#   package @ git+ssh://git@github.com/org/repo.git@ref#subdirectory=packages/pkg
GIT_SSH_PATTERN = re.compile(
    r"^(?P<package>[A-Za-z0-9_.-]+)\s*@\s*"
    r"git\+ssh://git@github\.com/"
    r"(?P<org>[^/]+)/(?P<repo>[^@/#]+?)(?:\.git)?@(?P<ref>[^#\s]+)"
    r"(?:#(?P<fragment>\S+))?$"
)


def parse_git_dependencies(requirements_path: Path) -> list[dict]:
    """Extract all git+ssh:// dependencies from a requirements file."""
    deps = []
    for line in requirements_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = GIT_SSH_PATTERN.match(line)
        if match:
            dep = match.groupdict()
            fragment = dep.pop("fragment")
            dep["subdirectory"] = None

            if fragment:
                for key_value in fragment.split("&"):
                    if not key_value or "=" not in key_value:
                        continue
                    key, value = key_value.split("=", 1)
                    if key == "subdirectory":
                        dep["subdirectory"] = value
                        break

            deps.append(dep)
    return deps


def build_wheel(dep: dict, token: str, output_dir: Path) -> None:
    """Clone a repo and build a wheel from it."""
    package = dep["package"]
    org, repo, ref = dep["org"], dep["repo"], dep["ref"]
    subdirectory = dep.get("subdirectory")
    clone_url = f"https://x-access-token:{token}@github.com/{org}/{repo}.git"

    with tempfile.TemporaryDirectory() as tmp_dir:
        clone_path = Path(tmp_dir) / repo
        print(f"===> Cloning {org}/{repo} @ {ref}")
        subprocess.run(
            ["git", "clone", "--branch", ref, "--depth", "1", clone_url, str(clone_path)],
            check=True,
        )

        # Extract version from ref (e.g. "v0.1.0" -> "0.1.0")
        version = ref.lstrip("v")
        build_path = clone_path / subdirectory if subdirectory else clone_path

        if not build_path.exists():
            raise FileNotFoundError(
                f"Build path not found for {package}: {build_path} (subdirectory={subdirectory})"
            )

        print(
            f"===> Building wheel for {package} from "
            f"{subdirectory or '.'} (version: {version})"
        )
        # Write version directly into pyproject.toml to override setuptools-scm
        pyproject_path = build_path / "pyproject.toml"
        if not pyproject_path.exists():
            raise FileNotFoundError(
                f"pyproject.toml not found for {package} at {pyproject_path}"
            )
        pyproject_content = pyproject_path.read_text()

        # Replace dynamic version with static version
        pyproject_content = pyproject_content.replace(
            'dynamic = ["version"]',
            f'version = "{version}"',
        )
        pyproject_path.write_text(pyproject_content)

        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(output_dir), str(build_path)],
            check=True,
        )
        print(f"===> Done: {package}")


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <requirements-file> [output-dir]")
        sys.exit(1)

    requirements_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("vendor")

    token = os.environ.get("GIT_TOKEN")
    if not token:
        print("Error: GIT_TOKEN environment variable is not set", file=sys.stderr)
        sys.exit(1)

    if not requirements_path.exists():
        print(f"Error: {requirements_path} not found", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    deps = parse_git_dependencies(requirements_path)

    if not deps:
        print("No git+ssh:// dependencies found, skipping.")
        return

    print(f"Found {len(deps)} private git dependencies:")
    for dep in deps:
        subdir = dep.get("subdirectory")
        subdir_suffix = f" (subdirectory={subdir})" if subdir else ""
        print(f"  - {dep['package']}: {dep['org']}/{dep['repo']} @ {dep['ref']}{subdir_suffix}")

    for dep in deps:
        build_wheel(dep, token, output_dir)

    print(f"\n===> Built wheels in {output_dir}/:")
    for whl in sorted(output_dir.glob("*.whl")):
        print(f"  {whl.name}")


if __name__ == "__main__":
    main()
