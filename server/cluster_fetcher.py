"""
Fetches live cluster resources using kubectl.
Supports miks-proxy (auto kubeconfig via miks-iam-tool) and user-uploaded kubeconfig.
Read-only only: only kubectl get/describe/version are permitted.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import yaml

# Whitelist of allowed kubectl subcommands — anything else is blocked at the call site.
_ALLOWED_SUBCOMMANDS = {"get", "describe", "version"}
_WRITE_KEYWORDS = {"apply", "create", "delete", "edit", "patch", "replace", "scale",
                   "rollout", "exec", "port-forward", "proxy", "drain", "cordon", "taint"}


def _safe_kubectl(kubeconfig: str, args: list[str], timeout: int = 30) -> str:
    """Run a kubectl command. Raises if the subcommand is not in the whitelist."""
    subcommand = args[0] if args else ""
    if subcommand not in _ALLOWED_SUBCOMMANDS:
        raise ValueError(f"kubectl subcommand '{subcommand}' is not allowed (read-only only)")
    for kw in _WRITE_KEYWORDS:
        if kw in args:
            raise ValueError(f"Dangerous kubectl argument detected: {kw}")

    cmd = ["kubectl", "--kubeconfig", kubeconfig] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"kubectl {subcommand} failed")
    return result.stdout


def fetch_miks_proxy(
    cluster: str,
    resources: list[dict],
    run_dir: Path,
    creds_file: str | None = None,
) -> tuple[str, list[dict]]:
    """
    Generates a kubeconfig via miks-iam-tool then fetches all given resources.
    Returns (yaml_str, fetch_warnings) where fetch_warnings lists resources that
    couldn't be fetched due to RBAC/CRD issues (not "not found").
    """
    kubeconfig_path = str(run_dir / "miks-kubeconfig")
    _generate_miks_kubeconfig(cluster, kubeconfig_path, creds_file)
    _safe_kubectl(kubeconfig_path, ["get", "namespace", "kube-system", "-o", "name"], timeout=15)
    return _fetch_resources(kubeconfig_path, resources, run_dir)


def fetch_kubeconfig(
    kubeconfig_path: str,
    resources: list[dict],
    run_dir: Path,
) -> tuple[str, list[dict]]:
    """Fetch resources using a user-provided kubeconfig.
    Returns (yaml_str, fetch_warnings).
    """
    _safe_kubectl(kubeconfig_path, ["get", "namespace", "kube-system", "-o", "name"], timeout=15)
    return _fetch_resources(kubeconfig_path, resources, run_dir)


def _generate_miks_kubeconfig(cluster: str, output_path: str, creds_file: str | None) -> None:
    # Check miks-iam-tool is available
    check = subprocess.run(["which", "miks-iam-tool"], capture_output=True, text=True)
    if check.returncode != 0:
        raise RuntimeError(
            "miks-iam-tool not found. Install it from: "
            "https://cnbj1-fds.api.xiaomi.net/miks-iam-tool/v0.1.2/"
        )

    # Load credentials
    env = os.environ.copy()
    if creds_file and Path(creds_file).exists():
        with open(creds_file) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    # Handle `export KEY=value` format
                    if line.startswith("export "):
                        line = line[len("export "):]
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")

    ak = env.get("MIKS_IAM_AK", "")
    sk = env.get("MIKS_IAM_SK", "")
    sa = env.get("MIKS_IAM_SA", "")

    if not ak or not sk or not sa:
        raise RuntimeError(
            f"Missing MIKS_IAM_AK/SK/SA in credentials file: {creds_file}"
        )

    result = subprocess.run(
        [
            "miks-iam-tool", "credential-plugin", "get-kubeconfig",
            f"--cluster-id={cluster}",
            f"--iamAK={ak}",
            f"--iamSK={sk}",
            f"--iamServiceAccount={sa}",
            "--expiration=6h",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"miks-iam-tool failed for cluster '{cluster}':\n{result.stderr.strip()}"
        )

    with open(output_path, "w") as f:
        f.write(result.stdout)


def _classify_fetch_error(err_msg: str) -> str:
    """Classify a kubectl error as 'not-found', 'forbidden', 'no-api', or 'other'."""
    m = err_msg.lower()
    if "not found" in m or "(notfound)" in m:
        return "not-found"
    if "forbidden" in m or "(forbidden)" in m or "cannot get" in m or "cannot list" in m:
        return "forbidden"
    if "doesn't have a resource type" in m or "no matches for kind" in m or "no kind" in m:
        return "no-api"
    return "other"


def _fetch_resources(kubeconfig: str, resources: list[dict], run_dir: Path) -> tuple[str, list[dict]]:
    """Fetch each (kind, name, namespace) via kubectl get -o yaml.

    Returns (yaml_str, fetch_warnings).
    fetch_warnings contains resources that failed for reasons OTHER than "not found"
    (e.g. RBAC permission denied, CRD not registered).  Those resources should NOT
    be shown as "added" in the diff since we cannot determine their cluster state.
    """
    parts: list[str] = []
    warnings: list[dict] = []

    for r in resources:
        kind = r["kind"]
        name = r["name"]
        ns = r["namespace"]
        try:
            text = _safe_kubectl(
                kubeconfig,
                ["get", kind, name, "-n", ns, "-o", "yaml"],
                timeout=20,
            )
            if text.strip():
                parts.append(text)
        except RuntimeError as exc:
            err_class = _classify_fetch_error(str(exc))
            if err_class == "not-found":
                # Resource genuinely doesn't exist yet — diff correctly shows "added"
                pass
            else:
                # Can't determine cluster state (RBAC / CRD resolution failure)
                # Record as warning so diff can skip this resource
                reason = {
                    "forbidden": "RBAC: 只读账号无权读取此资源类型",
                    "no-api": "CRD 未注册或 API group 无法解析",
                    "other": str(exc)[:120],
                }.get(err_class, str(exc)[:120])
                warnings.append({"kind": kind, "name": name, "namespace": ns, "reason": reason})

    return "\n---\n".join(parts), warnings
