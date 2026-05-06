#!/usr/bin/env python3
"""
Parse requirements file for git+ssh:// dependencies,
clone them via HTTPS with a token, and build wheels using uv.

Supports:
  - Simple repos:        pkg @ git+ssh://git@github.com/org/repo@tag
  - Repos with .git:     pkg @ git+ssh://git@github.com/org/repo.git@tag
  - Subdirectories:      pkg @ git+ssh://git@github.com/org/repo.git@tag#subdirectory=path
  - Deduplicates clones: same repo+ref is cloned only once, multiple subdirs built from it
  - Skips version patching if version is already static in pyproject.toml
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Pattern: package @ git+ssh://git@github.com/org/repo[.git]@ref[#subdirectory=path]
GIT_SSH_PATTERN = re.compile(
    r"^[^#]*@\s*git\+ssh://git@github\.com/"
    r"(?P<org>[^/]+)/"
    r"(?P<repo>[^@]+?)"       # non-greedy to stop before .git or @
    r"(?:\.git)?"              # optional .git suffix
    r"@(?P<ref>[^#\s]+)"      # ref: everything up to # or whitespace
    r"(?:#subdirectory=(?P<subdir>\S+))?"  # optional #subdirectory=...
    r"$"
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
            dep.setdefault("subdir", None)
            deps.append(dep)
    return deps


def clone_repo(org: str, repo: str, ref: str, token: str, clone_root: Path) -> Path:
    """Clone a repo if not already cloned. Returns the clone path."""
    clone_key = f"{org}/{repo}@{ref}"
    clone_path = clone_root / f"{org}__{repo}__{ref}"

    if clone_path.exists():
        print(f"  (reusing existing clone for {clone_key})")
        return clone_path

    clone_url = f"https://x-access-token:{token}@github.com/{org}/{repo}.git"
    print(f"===> Cloning {org}/{repo} @ {ref}")
    subprocess.run(
        ["git", "clone", "--branch", ref, "--depth", "1", clone_url, str(clone_path)],
        check=True,
    )
    return clone_path


def patch_dynamic_version(pyproject_path: Path, version: str) -> bool:
    """Replace dynamic version with static if present. Returns True if patched."""
    if not pyproject_path.exists():
        return False

    content = pyproject_path.read_text()
    if 'dynamic = ["version"]' in content:
        patched = content.replace('dynamic = ["version"]', f'version = "{version}"')
        pyproject_path.write_text(patched)
        print(f"  (patched dynamic version → {version})")
        return True
    return False


def build_wheel(clone_path: Path, subdir: str | None, ref: str, output_dir: Path) -> None:
    """Build a wheel from a (possibly subdirectory of a) cloned repo."""
    build_path = clone_path / subdir if subdir else clone_path
    label = subdir or clone_path.name

    # Extract version from ref (e.g. "v1.0.9" -> "1.0.9") for potential patching
    version = ref.lstrip("v")
    pyproject_path = build_path / "pyproject.toml"
    patch_dynamic_version(pyproject_path, version)

    print(f"===> Building wheel for {label}")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output_dir), str(build_path)],
        check=True,
    )
    print(f"===> Done: {label}")


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
        subdir_info = f" (subdirectory={dep['subdir']})" if dep["subdir"] else ""
        print(f"  - {dep['org']}/{dep['repo']} @ {dep['ref']}{subdir_info}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        clone_root = Path(tmp_dir)

        for dep in deps:
            clone_path = clone_repo(
                dep["org"], dep["repo"], dep["ref"], token, clone_root
            )
            build_wheel(clone_path, dep["subdir"], dep["ref"], output_dir)

    print(f"\n===> Built wheels in {output_dir}/:")
    for whl in sorted(output_dir.glob("*.whl")):
        print(f"  {whl.name}")


if __name__ == "__main__":
    main()

