"""Validate cross-references across extraction output YAML files."""
import sys
import yaml
from pathlib import Path

output_dir = Path(".extraction/output")
errors = []


def load_ids(filename, key):
    path = output_dir / filename
    if not path.exists():
        return set()
    try:
        data = yaml.safe_load(path.read_text())
        return {item["id"] for item in (data.get(key) or []) if "id" in item}
    except Exception:
        return set()


capability_ids = load_ids("capabilities.yaml", "capabilities")
flow_ids = load_ids("flows.yaml", "flows")
operation_ids = load_ids("operations.yaml", "operations")
dependency_ids = load_ids("dependencies.yaml", "dependencies")
entity_ids = load_ids("entities.yaml", "entities")


def get_refs(item, singular, plural):
    """Return list of ref values — handles both singular string and plural list field names."""
    refs = []
    val = item.get(singular)
    if val:
        refs.append(val) if isinstance(val, str) else refs.extend(val)
    val = item.get(plural)
    if val:
        refs.append(val) if isinstance(val, str) else refs.extend(val)
    return refs


def check(source_file, items_key, ref_singular, ref_plural, valid_ids, target_file):
    path = output_dir / source_file
    if not path.exists():
        return
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as e:
        errors.append(f"{source_file}: YAML parse error — {e}")
        return
    for item in (data.get(items_key) or []):
        item_id = item.get("id", "?")
        for ref in get_refs(item, ref_singular, ref_plural):
            if ref not in valid_ids:
                errors.append(
                    f"{source_file} → {item_id}.{ref_singular}/{ref_plural} = '{ref}' "
                    f"— not found in {target_file}"
                )


# endpoints.yaml
check("endpoints.yaml", "endpoints", "capability_ref", "capability_refs", capability_ids, "capabilities.yaml")
check("endpoints.yaml", "endpoints", "flow_ref", "flow_refs", flow_ids, "flows.yaml")
check("endpoints.yaml", "endpoints", "operation_ref", "operation_refs", operation_ids, "operations.yaml")

# pages.yaml
check("pages.yaml", "pages", "capability_ref", "capability_refs", capability_ids, "capabilities.yaml")
check("pages.yaml", "pages", "flow_ref", "flow_refs", flow_ids, "flows.yaml")

# capabilities.yaml — dependency refs
cap_path = output_dir / "capabilities.yaml"
if cap_path.exists():
    try:
        data = yaml.safe_load(cap_path.read_text())
        for cap in (data.get("capabilities") or []):
            for dep in (cap.get("dependencies") or []):
                if dep not in dependency_ids:
                    errors.append(
                        f"capabilities.yaml → {cap.get('id', '?')}.dependencies = '{dep}' "
                        f"— not found in dependencies.yaml"
                    )
    except Exception as e:
        errors.append(f"capabilities.yaml: YAML parse error — {e}")

# flows.yaml — capability_refs in steps + dependency refs
flow_path = output_dir / "flows.yaml"
if flow_path.exists():
    try:
        data = yaml.safe_load(flow_path.read_text())
        for flow in (data.get("flows") or []):
            fid = flow.get("id", "?")
            for dep in (flow.get("dependencies") or []):
                if dep not in dependency_ids:
                    errors.append(
                        f"flows.yaml → {fid}.dependencies = '{dep}' "
                        f"— not found in dependencies.yaml"
                    )
            for step in (flow.get("steps") or []):
                if not isinstance(step, dict):
                    continue  # plain string steps are valid
                order = step.get("order", "?")
                for ref in get_refs(step, "capability_ref", "capability_refs"):
                    if ref not in capability_ids:
                        errors.append(
                            f"flows.yaml → {fid}.steps[{order}].capability_ref = '{ref}' "
                            f"— not found in capabilities.yaml"
                        )
    except Exception as e:
        errors.append(f"flows.yaml: YAML parse error — {e}")

# entities.yaml — relationship targets
entity_path = output_dir / "entities.yaml"
if entity_path.exists():
    try:
        data = yaml.safe_load(entity_path.read_text())
        for entity in (data.get("entities") or []):
            eid = entity.get("id", "?")
            for rel in (entity.get("relationships") or []):
                target = rel.get("target")
                if target and target not in entity_ids:
                    errors.append(
                        f"entities.yaml → {eid}.relationships.target = '{target}' "
                        f"— not found in entities.yaml"
                    )
    except Exception as e:
        errors.append(f"entities.yaml: YAML parse error — {e}")


if errors:
    print(f"Found {len(errors)} cross-reference error(s):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("All cross-references valid.")
    sys.exit(0)
