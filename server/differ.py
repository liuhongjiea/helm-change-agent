"""
Semantic YAML diff: compares repo (helm template) vs cluster (kubectl get) configs.
Groups by (kind, namespace, name), identifies added/removed/changed resources.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import yaml


@dataclass
class ResourceDiff:
    kind: str
    namespace: str
    name: str
    status: str          # "added" | "removed" | "changed" | "unchanged"
    diff_text: str       # human-readable unified diff or summary
    repo_doc: dict | None
    cluster_doc: dict | None


def diff(
    repo_yaml: str,
    cluster_yaml: str,
    default_namespace: str = "default",
    skip_keys: set[tuple] | None = None,
) -> tuple[list[ResourceDiff], dict]:
    """
    Returns (diff_list, summary).
    default_namespace: used when a resource has no explicit namespace in metadata
    skip_keys: set of (kind, ns, name) tuples to exclude from comparison entirely
               (used for resources we couldn't fetch due to RBAC/CRD issues)
    summary = {"added": int, "removed": int, "changed": int, "unchanged": int}
    """
    repo_docs = _parse_docs(repo_yaml, default_namespace)
    cluster_docs = _parse_docs(cluster_yaml, default_namespace)

    all_keys = set(repo_docs.keys()) | set(cluster_docs.keys())
    if skip_keys:
        all_keys -= skip_keys
    results: list[ResourceDiff] = []

    for key in sorted(all_keys):
        kind, ns, name = key
        repo_doc = repo_docs.get(key)
        cluster_doc = cluster_docs.get(key)

        if repo_doc is not None and cluster_doc is None:
            results.append(ResourceDiff(
                kind=kind, namespace=ns, name=name,
                status="added",
                diff_text=f"+ New resource: {kind}/{name} in {ns}",
                repo_doc=repo_doc,
                cluster_doc=None,
            ))
        elif repo_doc is None and cluster_doc is not None:
            results.append(ResourceDiff(
                kind=kind, namespace=ns, name=name,
                status="removed",
                diff_text=f"- Resource not in repo: {kind}/{name} in {ns} (unmanaged or to be deleted)",
                repo_doc=None,
                cluster_doc=cluster_doc,
            ))
        else:
            # Both exist — normalize cluster doc, then field-level diff
            cluster_doc_norm = _normalize_cluster_doc(repo_doc, cluster_doc)
            diff_text = _field_diff(repo_doc, cluster_doc_norm)
            status = "changed" if diff_text else "unchanged"
            results.append(ResourceDiff(
                kind=kind, namespace=ns, name=name,
                status=status,
                diff_text=diff_text,
                repo_doc=repo_doc,
                cluster_doc=cluster_doc,
            ))

    summary = {
        "added": sum(1 for r in results if r.status == "added"),
        "removed": sum(1 for r in results if r.status == "removed"),
        "changed": sum(1 for r in results if r.status == "changed"),
        "unchanged": sum(1 for r in results if r.status == "unchanged"),
    }
    return results, summary


def diff_to_markdown(diffs: list[ResourceDiff]) -> str:
    """Render diff list as a markdown section."""
    if not diffs:
        return "_无差异_"

    lines: list[str] = []
    for r in diffs:
        if r.status == "unchanged":
            continue
        icon = {"added": "🟢 新增", "removed": "🔴 删除", "changed": "🟡 变更"}.get(r.status, r.status)
        lines.append(f"### {icon}: `{r.kind}/{r.name}` (ns: `{r.namespace}`)\n")
        if r.diff_text:
            lines.append("```diff\n" + r.diff_text + "\n```\n")

    return "\n".join(lines) if lines else "_所有资源无差异_"


def diff_for_ai(diffs: list[ResourceDiff], max_bytes: int = 25_000) -> str:
    """
    Returns a compact text summary for the AI prompt.
    Skips unchanged resources, truncates if too large.
    """
    parts: list[str] = []
    for r in diffs:
        if r.status == "unchanged":
            continue
        parts.append(f"[{r.status.upper()}] {r.kind}/{r.name} ns={r.namespace}")
        if r.diff_text:
            # Truncate individual diffs
            text = r.diff_text[:2000]
            parts.append(text)
        parts.append("")

    result = "\n".join(parts)
    if len(result.encode()) > max_bytes:
        result = result.encode()[:max_bytes].decode("utf-8", errors="ignore")
        result += "\n...(truncated)"
    return result


_CLUSTER_SCOPED_KINDS = {
    "ClusterRole", "ClusterRoleBinding", "PersistentVolume", "Namespace",
    "CustomResourceDefinition", "StorageClass", "IngressClass",
    "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration",
    "APIService", "PriorityClass", "RuntimeClass", "NodeClass",
}

# Annotations injected by helm/k8s infrastructure (not written by chart authors)
_INFRA_ANNOTATIONS = frozenset({
    "kubectl.kubernetes.io/last-applied-configuration",
    "meta.helm.sh/release-name",
    "meta.helm.sh/release-namespace",
})

# Labels that embed version numbers and change with every helm upgrade.
# Strip from both sides — they reflect the deployer, not meaningful drift.
_HELM_VERSION_LABELS = frozenset({
    "helm.sh/chart",              # e.g. "botkube-0.1.2" — chart version
    "app.kubernetes.io/version",  # application version tag
    "app.kubernetes.io/managed-by",  # always "Helm" when helm-managed
})


def _parse_docs(yaml_text: str, default_namespace: str = "default") -> dict[tuple, dict]:
    """Parse multi-document YAML, key by (kind, namespace, name)."""
    docs: dict[tuple, dict] = {}
    if not yaml_text or not yaml_text.strip():
        return docs
    try:
        for doc in yaml.safe_load_all(yaml_text):
            if not doc or not isinstance(doc, dict):
                continue
            kind = doc.get("kind", "Unknown")
            meta = doc.get("metadata", {})
            name = meta.get("name", "")
            # Cluster-scoped resources have no namespace; namespace-scoped ones
            # may omit it in helm template output (chart uses .Release.Namespace).
            # Use default_namespace (= release namespace) as fallback so repo and
            # cluster sides get the same key.
            if kind in _CLUSTER_SCOPED_KINDS:
                ns = ""
            else:
                ns = meta.get("namespace") or default_namespace
            if name:
                key = (kind, ns, name)
                docs[key] = _strip_runtime_fields(doc)
    except yaml.YAMLError:
        pass
    return docs


def _strip_runtime_fields(doc: dict) -> dict:
    """Remove fields managed purely by k8s runtime — applied to both repo and cluster docs."""
    d = copy.deepcopy(doc)
    meta = d.get("metadata", {})

    # Pure runtime fields — k8s manages these, chart authors don't set them
    for key in ("resourceVersion", "uid", "creationTimestamp", "generation",
                "selfLink", "managedFields"):
        meta.pop(key, None)

    # namespace: already captured in the diff key (kind, ns, name).
    meta.pop("namespace", None)

    # Strip infra-managed annotations; normalize null → absent
    annotations = meta.get("annotations")
    if isinstance(annotations, dict):
        for a in _INFRA_ANNOTATIONS:
            annotations.pop(a, None)
        if not annotations:
            meta.pop("annotations", None)
    elif annotations is None:
        meta.pop("annotations", None)

    # Strip helm-managed version labels from both sides (they change every version bump)
    labels = meta.get("labels")
    if isinstance(labels, dict):
        for lbl in _HELM_VERSION_LABELS:
            labels.pop(lbl, None)
        if not labels:
            meta.pop("labels", None)
    elif labels is None:
        meta.pop("labels", None)

    # status is managed by the control plane, not by the chart
    d.pop("status", None)
    return d


def _normalize_cluster_doc(repo: dict, cluster: dict) -> dict:
    """Strip k8s-auto-defaulted fields from cluster doc that repo doc doesn't set.

    k8s fills in many default values at creation time that chart authors never write
    in their templates.  Comparing helm-template output (no defaults) against
    kubectl-get output (all defaults present) produces noisy false-positive diffs.

    Strategy: strip a cluster-side field only when:
      - it is a known k8s-defaulted field, AND
      - the corresponding repo doc also lacks that field
    so intentionally-set non-default values are still compared correctly.
    """
    c = copy.deepcopy(cluster)
    kind = c.get("kind", "")
    r_spec = repo.get("spec") or {}
    c_spec = c.get("spec") or {}

    if kind == "Service":
        # Runtime-assigned network identity fields — chart authors never set these
        for f in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy",
                  "sessionAffinity", "sessionAffinityConfig", "internalTrafficPolicy",
                  "type"):  # type: ClusterIP is k8s default
            if f not in r_spec:
                c_spec.pop(f, None)
        # Strip default protocol=TCP from ports when repo port doesn't specify it
        r_ports_by_num = {p.get("port"): p
                         for p in r_spec.get("ports", []) if isinstance(p, dict)}
        for port in c_spec.get("ports", []):
            if isinstance(port, dict):
                r_port = r_ports_by_num.get(port.get("port")) or {}
                if "protocol" not in r_port and port.get("protocol") == "TCP":
                    port.pop("protocol", None)

    elif kind == "ServiceAccount":
        # k8s ≤1.23 injects a token secret reference; strip if chart doesn't declare it
        if "secrets" not in repo:
            c.pop("secrets", None)

    elif kind in ("Deployment", "DaemonSet", "StatefulSet", "ReplicaSet"):
        if kind == "Deployment":
            if "progressDeadlineSeconds" not in r_spec:
                c_spec.pop("progressDeadlineSeconds", None)
            if "revisionHistoryLimit" not in r_spec:
                c_spec.pop("revisionHistoryLimit", None)
            # Default RollingUpdate strategy: strip if chart omits strategy entirely
            if "strategy" not in r_spec:
                c_spec.pop("strategy", None)

        if kind == "DaemonSet":
            if "updateStrategy" not in r_spec:
                c_spec.pop("updateStrategy", None)
            if "revisionHistoryLimit" not in r_spec:
                c_spec.pop("revisionHistoryLimit", None)

        if kind == "StatefulSet":
            if "updateStrategy" not in r_spec:
                c_spec.pop("updateStrategy", None)
            if "revisionHistoryLimit" not in r_spec:
                c_spec.pop("revisionHistoryLimit", None)
            if "podManagementPolicy" not in r_spec:
                c_spec.pop("podManagementPolicy", None)

        _normalize_pod_template(
            r_spec.get("template") or {},
            c_spec.get("template") or {},
        )

    elif kind in ("Job", "CronJob"):
        if kind == "Job":
            r_job = r_spec
            c_job = c_spec
        else:
            r_job = (r_spec.get("jobTemplate") or {}).get("spec") or {}
            c_job = (c_spec.get("jobTemplate") or {}).get("spec") or {}
        for f in ("completionMode", "suspend", "backoffLimitPerIndex"):
            if f not in r_job:
                c_job.pop(f, None)
        _normalize_pod_template(
            r_job.get("template") or {},
            c_job.get("template") or {},
        )

    return c


def _normalize_pod_template(r_tmpl: dict, c_tmpl: dict) -> None:
    """Strip k8s-defaulted fields from a cluster-side pod template in-place."""
    # template.metadata.creationTimestamp is always null from k8s
    r_meta = r_tmpl.get("metadata") or {}
    c_meta = c_tmpl.get("metadata") or {}
    if isinstance(c_meta, dict) and "creationTimestamp" not in r_meta:
        c_meta.pop("creationTimestamp", None)

    r_pod = r_tmpl.get("spec") or {}
    c_pod = c_tmpl.get("spec") or {}
    if not isinstance(c_pod, dict):
        return

    # Pod-level fields k8s fills with well-known defaults
    _POD_DEFAULTS = (
        "dnsPolicy",               # ClusterFirst
        "restartPolicy",           # Always
        "schedulerName",           # default-scheduler
        "serviceAccount",          # deprecated alias for serviceAccountName, k8s copies it
        "terminationGracePeriodSeconds",  # 30
        "enableServiceLinks",      # true
        "preemptionPolicy",        # PreemptLowerPriority
    )
    for f in _POD_DEFAULTS:
        if f not in r_pod:
            c_pod.pop(f, None)

    # Empty securityContext added by k8s when chart omits it
    if "securityContext" not in r_pod and c_pod.get("securityContext") == {}:
        c_pod.pop("securityContext", None)

    # containers / initContainers
    r_containers = {cont.get("name"): cont
                    for cont in r_pod.get("containers", []) if isinstance(cont, dict)}
    r_init = {cont.get("name"): cont
              for cont in r_pod.get("initContainers", []) if isinstance(cont, dict)}

    for cont_list, r_map in (
        (c_pod.get("containers", []), r_containers),
        (c_pod.get("initContainers", []), r_init),
    ):
        for cont in cont_list:
            if not isinstance(cont, dict):
                continue
            r_cont = r_map.get(cont.get("name")) or {}
            for f in ("terminationMessagePath", "terminationMessagePolicy"):
                if f not in r_cont:
                    cont.pop(f, None)
            if "resources" not in r_cont and cont.get("resources") == {}:
                cont.pop("resources", None)
            if "securityContext" not in r_cont and cont.get("securityContext") == {}:
                cont.pop("securityContext", None)

    # container ports: strip default protocol=TCP when repo port doesn't specify it
    for cont in c_pod.get("containers", []) + c_pod.get("initContainers", []):
        if not isinstance(cont, dict):
            continue
        r_cont = r_containers.get(cont.get("name")) or {}
        r_cont_ports = {p.get("containerPort"): p
                        for p in r_cont.get("ports", []) if isinstance(p, dict)}
        for port in cont.get("ports", []):
            if isinstance(port, dict):
                r_port = r_cont_ports.get(port.get("containerPort")) or {}
                if "protocol" not in r_port and port.get("protocol") == "TCP":
                    port.pop("protocol", None)

    # volumes: strip k8s-injected service-account token volumes and defaultMode defaults
    r_vol_names = {v.get("name") for v in r_pod.get("volumes", []) if isinstance(v, dict)}
    r_vols = {v.get("name"): v for v in r_pod.get("volumes", []) if isinstance(v, dict)}
    # Remove in-place: filter out auto-injected SA token volumes not present in repo
    c_volumes = c_pod.get("volumes", [])
    c_pod["volumes"] = [
        v for v in c_volumes
        if not (isinstance(v, dict)
                and v.get("name") not in r_vol_names
                and _is_sa_token_volume(v))
    ]
    for vol in c_pod.get("volumes", []):
        if not isinstance(vol, dict):
            continue
        r_vol = r_vols.get(vol.get("name")) or {}
        for vol_type in ("projected", "secret", "configMap"):
            c_sub = vol.get(vol_type)
            r_sub = r_vol.get(vol_type)
            if isinstance(c_sub, dict):
                if not isinstance(r_sub, dict) or "defaultMode" not in r_sub:
                    c_sub.pop("defaultMode", None)
    # If volumes list is now empty and repo had no volumes, remove the key
    if not c_pod.get("volumes") and "volumes" not in r_pod:
        c_pod.pop("volumes", None)


def _is_sa_token_volume(vol: dict) -> bool:
    """Return True if this volume is the k8s-injected service-account token projected volume."""
    if vol.get("name", "").startswith("kube-api-access"):
        return True
    projected = vol.get("projected")
    if not isinstance(projected, dict):
        return False
    sources = projected.get("sources", [])
    source_types = {list(s.keys())[0] for s in sources if isinstance(s, dict) and s}
    # k8s injects: serviceAccountToken + configMap (kube-root-ca.crt) + downwardAPI
    return source_types <= {"serviceAccountToken", "configMap", "downwardAPI"}


def _field_diff(repo: dict, cluster: dict) -> str:
    """Return a readable text summary of differences."""
    try:
        from deepdiff import DeepDiff
        dd = DeepDiff(cluster, repo, ignore_order=True, verbose_level=1)
        if not dd:
            return ""
        lines: list[str] = []
        for change_type, changes in dd.items():
            if change_type == "values_changed":
                for path, change in changes.items():
                    lines.append(f"- {path}: {change['old_value']!r} → {change['new_value']!r}")
            elif change_type == "dictionary_item_added":
                for path in changes:
                    lines.append(f"+ {path} (新增字段)")
            elif change_type == "dictionary_item_removed":
                for path in changes:
                    lines.append(f"- {path} (删除字段)")
            elif change_type in ("iterable_item_added", "iterable_item_removed"):
                for path in changes:
                    lines.append(f"~ {change_type}: {path}")
            else:
                lines.append(f"~ {change_type}: {list(changes.keys())[:3]}")
        return "\n".join(lines[:50])  # cap output
    except ImportError:
        # Fallback: compare YAML text
        repo_text = yaml.dump(repo, default_flow_style=False, allow_unicode=True)
        cluster_text = yaml.dump(cluster, default_flow_style=False, allow_unicode=True)
        if repo_text == cluster_text:
            return ""
        import difflib
        diff_lines = list(difflib.unified_diff(
            cluster_text.splitlines(), repo_text.splitlines(),
            fromfile="cluster", tofile="repo", lineterm=""
        ))
        return "\n".join(diff_lines[:80])
