"""
Scans ~/work/miks-config and builds an in-memory index of clusters, components,
and their Helm chart specs. Called once at server startup.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MIKS_CONFIG_DIR = Path(os.getenv("MIKS_CONFIG_DIR", "/Users/hongjieliu/work/miks-config"))


@dataclass
class ChartSpec:
    task_name: str           # "helm" or "helm-1.32" for multi-chart components
    chart_dir: str           # absolute path to chart directory
    values_file: str         # template string with {{ .CLUSTER }}
    default_values_file: str | None
    namespace: str
    release_name: str


@dataclass
class ComponentInfo:
    name: str
    clusters: list[str]      # clusters this component is deployed to
    specs: list[ChartSpec]   # one per task (usually just one)


_index: dict = {}


def build_index() -> None:
    global _index

    clusters = _load_clusters()
    all_components = _load_component_names()

    cluster_to_components: dict[str, list[str]] = {c: [] for c in clusters}
    component_infos: dict[str, ComponentInfo] = {}

    for comp in all_components:
        comp_dir = MIKS_CONFIG_DIR / comp
        if not comp_dir.is_dir():
            continue

        mapping_path = comp_dir / "mapping.yaml"
        task_path = comp_dir / "tasks" / "task.yaml"
        if not mapping_path.exists() or not task_path.exists():
            continue

        try:
            comp_clusters = _parse_mapping_clusters(mapping_path, clusters)
            specs = _parse_chart_specs(comp, task_path)
        except Exception:
            continue

        if not specs:
            continue

        info = ComponentInfo(name=comp, clusters=comp_clusters, specs=specs)
        component_infos[comp] = info

        for c in comp_clusters:
            if c in cluster_to_components:
                cluster_to_components[c].append(comp)

    # Only keep clusters that actually have components
    active_clusters = sorted(c for c, comps in cluster_to_components.items() if comps)

    _index = {
        "clusters": active_clusters,
        "all_clusters": clusters,
        "components": sorted(component_infos.keys()),
        "cluster_to_components": {c: sorted(cluster_to_components[c]) for c in active_clusters},
        "component_infos": component_infos,
    }


def get_index() -> dict:
    if not _index:
        build_index()
    return _index


def get_component_specs(component: str, cluster: str) -> list[ChartSpec]:
    """Return resolved ChartSpec list for a component+cluster pair."""
    idx = get_index()
    info: ComponentInfo | None = idx["component_infos"].get(component)
    if not info:
        raise ValueError(f"Unknown component: {component}")

    resolved = []
    for spec in info.specs:
        values_file = _resolve_cluster_template(spec.values_file, cluster, component)
        default_values_file = (
            _resolve_cluster_template(spec.default_values_file, cluster, component)
            if spec.default_values_file
            else None
        )
        resolved.append(ChartSpec(
            task_name=spec.task_name,
            chart_dir=spec.chart_dir,
            values_file=values_file,
            default_values_file=default_values_file,
            namespace=spec.namespace,
            release_name=spec.release_name,
        ))
    return resolved


def _load_clusters() -> list[str]:
    cluster_dir = MIKS_CONFIG_DIR / "cluster-config"
    if not cluster_dir.is_dir():
        return []
    return sorted(p.stem for p in cluster_dir.glob("*.yaml"))


def _load_component_names() -> list[str]:
    comp_file = MIKS_CONFIG_DIR / "components.yaml"
    if not comp_file.exists():
        return []
    with open(comp_file) as f:
        data = yaml.safe_load(f)
    raw = data.get("components", [])
    # May be list of strings or list of dicts with "name"
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("component")
            if name:
                result.append(name)
    return result


def _parse_mapping_clusters(mapping_path: Path, all_clusters: list[str]) -> list[str]:
    with open(mapping_path) as f:
        data = yaml.safe_load(f)

    clusters: set[str] = set()
    for task_entry in data.get("tasks", []):
        if task_entry.get("scope") == "all":
            return all_clusters
        entry_clusters = task_entry.get("clusters", [])
        clusters.update(entry_clusters)
    return sorted(clusters)


def _parse_chart_specs(comp: str, task_path: Path) -> list[ChartSpec]:
    comp_dir = MIKS_CONFIG_DIR / comp
    with open(task_path) as f:
        data = yaml.safe_load(f)

    specs = []
    for task in data.get("tasks", []):
        source = task.get("source", {}).get("helm", {})
        target = task.get("target", {}).get("helm", {})

        chart_dir_raw: str = source.get("chartDir", "")
        values_file_raw: str = source.get("valuesFile", "")
        default_values_raw: str | None = source.get("defaultValuesFile")
        namespace: str = target.get("namespace", "default")
        release_name: str = target.get("releaseName", comp)
        task_name: str = task.get("name", "helm")

        if not chart_dir_raw or not values_file_raw:
            continue

        # Paths in task.yaml are relative to the component dir (leading "/" means comp-root-relative)
        chart_dir = str(comp_dir / chart_dir_raw.lstrip("/"))
        values_file = str(comp_dir / values_file_raw.lstrip("/"))
        default_values_file = (
            str(comp_dir / default_values_raw.lstrip("/")) if default_values_raw else None
        )

        specs.append(ChartSpec(
            task_name=task_name,
            chart_dir=chart_dir,
            values_file=values_file,
            default_values_file=default_values_file,
            namespace=namespace,
            release_name=release_name,
        ))

    return specs


def _resolve_cluster_template(template: str, cluster: str, comp: str) -> str:
    # Handle Go-style {{ .CLUSTER }} and also simple {CLUSTER}
    result = template.replace("{{ .CLUSTER }}", cluster)
    result = result.replace("{{.CLUSTER}}", cluster)
    result = result.replace("{CLUSTER}", cluster)
    return result


def options_response() -> dict:
    idx = get_index()
    return {
        "clusters": idx["clusters"],
        "components": idx["components"],
        "cluster_to_components": idx["cluster_to_components"],
        "component_specs": {
            name: [
                {
                    "task_name": s.task_name,
                    "chart_dir": s.chart_dir,
                    "values_file": s.values_file,
                    "default_values_file": s.default_values_file,
                    "namespace": s.namespace,
                    "release_name": s.release_name,
                }
                for s in info.specs
            ]
            for name, info in idx["component_infos"].items()
        },
    }
