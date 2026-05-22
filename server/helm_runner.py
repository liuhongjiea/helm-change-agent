"""
Runs `helm template` for a given component+cluster and returns the rendered YAML string.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config_index import ChartSpec, get_component_specs


def render(component: str, cluster: str, run_dir: Path) -> tuple[str, list[dict]]:
    """
    Returns (merged_yaml_string, resource_list).
    resource_list items: {"kind": str, "name": str, "namespace": str}
    """
    specs = get_component_specs(component, cluster)
    all_yaml_parts: list[str] = []

    for spec in specs:
        yaml_text = _helm_template_one(spec, run_dir)
        if yaml_text.strip():
            all_yaml_parts.append(yaml_text)

    merged = "\n---\n".join(all_yaml_parts)

    # Parse resources from merged yaml
    import yaml as pyyaml
    resources = []
    for doc in pyyaml.safe_load_all(merged):
        if not doc or not isinstance(doc, dict):
            continue
        kind = doc.get("kind", "")
        name = doc.get("metadata", {}).get("name", "")
        ns = doc.get("metadata", {}).get("namespace") or specs[0].namespace
        if kind and name:
            resources.append({"kind": kind, "name": name, "namespace": ns})

    return merged, resources


def _helm_template_one(spec: ChartSpec, run_dir: Path) -> str:
    chart_dir = Path(spec.chart_dir)
    if not chart_dir.is_dir():
        raise FileNotFoundError(f"Chart directory not found: {chart_dir}")

    values_file = Path(spec.values_file)
    if not values_file.exists():
        raise FileNotFoundError(f"Values file not found: {values_file}")

    cmd = ["helm", "template", spec.release_name, str(chart_dir), "-n", spec.namespace]

    if spec.default_values_file:
        default_vf = Path(spec.default_values_file)
        if default_vf.exists():
            cmd += ["-f", str(default_vf)]

    cmd += ["-f", str(values_file)]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(run_dir),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"helm template failed for {spec.release_name}:\n{result.stderr}"
        )
    return result.stdout
