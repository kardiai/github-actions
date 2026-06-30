import os
import sys
import yaml
from datetime import datetime, timezone


def validate():
    slot = os.environ["SLOT"]
    path = f"data-processing/{slot}/manifest.yaml"
    with open(path) as f:
        manifest = yaml.safe_load(f)
    status = manifest.get("package", {}).get("status")
    if status != "in_progress":
        print(f"ERROR: Cannot seal — current status is '{status}'")
        sys.exit(1)
    print(f"OK: {path} is in_progress, proceeding with seal")


def seal():
    slot    = os.environ["SLOT"]
    version = os.environ["VERSION"]
    tag     = os.environ["TAG"]
    actor   = os.environ["ACTOR"]
    now     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    path = f"data-processing/{slot}/manifest.yaml"
    text = open(path).read()

    text = text.replace("version: null",       f'version: "{version}"')
    text = text.replace("status: in_progress", "status: sealed")
    text = text.replace("sealed_at: null",     f'sealed_at: "{now}"')
    text = text.replace("sealed_by: null",     f'sealed_by: "{actor}"')
    text = text.replace("git_tag: null",       f'git_tag: "{tag}"')

    open(path, "w").write(text)
    print(f"Sealed {path} → {version}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "--validate":
        validate()
    elif cmd == "--seal":
        seal()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
