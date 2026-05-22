"""
Assembles the final 5-section markdown report and writes it to disk.
Follows the same naming convention as the skill:
  helm-change-report-<component>-<cluster>.md
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from .differ import ResourceDiff, diff_to_markdown


REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/Users/hongjieliu/work/helm-change-agent"))


def write_report(
    component: str,
    cluster: str,
    namespace: str,
    release_name: str,
    connection_method: str,
    diff_list: list[ResourceDiff],
    summary: dict,
    ai_analysis: str,
    run_dir: Path,
    fetch_warnings: list[dict] | None = None,
) -> tuple[str, str]:
    """
    Writes the report to both /tmp/<run_dir>/report.md and the persistent reports dir.
    Returns (report_id, persistent_path).
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_id = f"helm-change-report-{component}-{cluster}"
    filename = f"{report_id}.md"

    risk_level = _extract_risk_level(ai_analysis)

    content = _render(
        component=component,
        cluster=cluster,
        namespace=namespace,
        release_name=release_name,
        connection_method=connection_method,
        generated_at=now,
        risk_level=risk_level,
        ai_analysis=ai_analysis,
        diff_list=diff_list,
        summary=summary,
        fetch_warnings=fetch_warnings or [],
    )

    # Write to temp dir (for immediate serving)
    tmp_path = run_dir / "report.md"
    tmp_path.write_text(content, encoding="utf-8")

    # Write to persistent reports dir
    persistent_path = REPORTS_DIR / filename
    persistent_path.write_text(content, encoding="utf-8")

    return report_id, str(persistent_path)


def _render(
    component: str,
    cluster: str,
    namespace: str,
    release_name: str,
    connection_method: str,
    generated_at: str,
    risk_level: str,
    ai_analysis: str,
    diff_list: list[ResourceDiff],
    summary: dict,
    fetch_warnings: list[dict] | None = None,
) -> str:
    diff_md = diff_to_markdown(diff_list)

    stats = (
        f"新增 **{summary.get('added', 0)}** 个资源，"
        f"删除 **{summary.get('removed', 0)}** 个资源，"
        f"修改 **{summary.get('changed', 0)}** 个资源，"
        f"无变化 **{summary.get('unchanged', 0)}** 个资源"
    )
    if fetch_warnings:
        stats += f"，**{len(fetch_warnings)}** 个资源无法读取（已跳过）"

    return f"""# Helm 组件变更分析报告

**生成时间**: {generated_at}
**分析组件**: {component}
**目标集群**: {cluster}
**命名空间**: {namespace}
**Release 名称**: {release_name}
**集群连接方式**: {connection_method}
**变更统计**: {stats}
**风险等级**: {risk_level}

---

## 一、🤖 AI 智能变更分析

{ai_analysis}

---

## 二、📊 配置变更详情

{diff_md}

---

## 三、📋 资源清单变更

| 状态 | Kind | Name | Namespace |
|------|------|------|-----------|
{_resource_table(diff_list)}

---

## 四、⚠️ 操作检查清单

- [ ] 已在测试集群验证变更
- [ ] 已通知相关团队
- [ ] 已确认监控告警配置
- [ ] 已准备回滚方案

---

## 五、📝 备注

_（可在此处添加人工分析备注）_
{_fetch_warnings_section(fetch_warnings)}"""


def _fetch_warnings_section(warnings: list[dict] | None) -> str:
    if not warnings:
        return ""
    lines = ["\n\n> **⚠️ 以下资源因权限或 CRD 原因无法读取，已从 diff 中跳过（实际状态未知）：**\n"]
    for w in warnings:
        lines.append(f"> - `{w['kind']}/{w['name']}` ns=`{w['namespace']}` — {w['reason']}")
    return "\n".join(lines)


def _resource_table(diff_list: list[ResourceDiff]) -> str:
    icons = {"added": "🟢 新增", "removed": "🔴 删除", "changed": "🟡 变更", "unchanged": "✅ 无变化"}
    rows = []
    for r in diff_list:
        icon = icons.get(r.status, r.status)
        rows.append(f"| {icon} | `{r.kind}` | `{r.name}` | `{r.namespace}` |")
    return "\n".join(rows) if rows else "| - | - | - | - |"


def _extract_risk_level(ai_text: str) -> str:
    """Extract risk level from AI output."""
    if "🔴" in ai_text or "高风险" in ai_text:
        return "🔴 高风险"
    if "🟡" in ai_text or "中风险" in ai_text:
        return "🟡 中风险"
    if "🟢" in ai_text or "低风险" in ai_text:
        return "🟢 低风险"
    return "⚪ 未知"
