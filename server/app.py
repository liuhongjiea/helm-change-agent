"""
FastAPI server. Bind to 127.0.0.1:8080 only.
Serves static web/ files and provides:
  GET  /api/options
  POST /api/analyze   → SSE stream
  GET  /api/reports
  GET  /api/reports/{report_id}
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv

# Load .env from the web/ project root (two levels up from this file)
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

from . import config_index, helm_runner, cluster_fetcher, differ, ai_analyzer, report_writer

app = FastAPI(title="Helm Change Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8080", "http://localhost:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)

REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/Users/hongjieliu/work/helm-change-agent"))
SKILL_CREDS_FILE = os.getenv(
    "SKILL_CREDS_FILE",
    "/Users/hongjieliu/.claude/skills/helm-change-review/creds.env",
)

# ─── Build index at startup ───────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, config_index.build_index)


# ─── Static files ─────────────────────────────────────────────────────────────

_web_dir = Path(__file__).parent.parent / "web"

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(_web_dir / "index.html")

@app.get("/report.html", response_class=HTMLResponse)
async def report_page():
    return FileResponse(_web_dir / "report.html")

app.mount("/static", StaticFiles(directory=str(_web_dir)), name="static")


# ─── API ──────────────────────────────────────────────────────────────────────

@app.get("/api/options")
async def get_options():
    return JSONResponse(config_index.options_response())


@app.post("/api/analyze")
async def analyze(
    cluster: str = Form(...),
    component: str = Form(...),
    source_type: str = Form(...),  # "miks-proxy" | "kubeconfig-upload"
    kubeconfig_file: UploadFile | None = File(None),
):
    run_id = str(uuid.uuid4())[:8]

    async def stream():
        run_dir = Path(tempfile.mkdtemp(prefix=f"helm-change-{run_id}-"))
        kubeconfig_path: str | None = None

        try:
            # Save uploaded kubeconfig if provided
            if source_type == "kubeconfig-upload" and kubeconfig_file:
                kc_path = run_dir / "uploaded-kubeconfig"
                content = await kubeconfig_file.read()
                kc_path.write_bytes(content)
                kubeconfig_path = str(kc_path)

            # Get index info
            idx = config_index.get_index()
            comp_infos = idx.get("component_infos", {})
            comp_info = comp_infos.get(component)
            namespace = comp_info.specs[0].namespace if comp_info and comp_info.specs else "default"
            release_name = comp_info.specs[0].release_name if comp_info and comp_info.specs else component

            # Step 1: Render helm template
            yield _sse({"step": "render", "status": "running"})
            t0 = time.time()
            try:
                rendered_yaml, resources = await _run_sync(
                    helm_runner.render, component, cluster, run_dir
                )
                ms = int((time.time() - t0) * 1000)
                yield _sse({"step": "render", "status": "done", "ms": ms,
                            "resource_count": len(resources)})
            except Exception as e:
                yield _sse({"step": "render", "status": "error", "msg": str(e)})
                return

            # Step 2: Fetch cluster state
            via = "miks-proxy" if source_type == "miks-proxy" else "kubeconfig"
            yield _sse({"step": "fetch", "status": "running", "via": via})
            t0 = time.time()
            fetch_warnings: list[dict] = []
            try:
                if source_type == "miks-proxy":
                    cluster_yaml, fetch_warnings = await _run_sync(
                        cluster_fetcher.fetch_miks_proxy,
                        cluster, resources, run_dir, SKILL_CREDS_FILE,
                    )
                else:
                    if not kubeconfig_path:
                        yield _sse({"step": "fetch", "status": "error",
                                    "msg": "请上传 kubeconfig 文件"})
                        return
                    cluster_yaml, fetch_warnings = await _run_sync(
                        cluster_fetcher.fetch_kubeconfig,
                        kubeconfig_path, resources, run_dir,
                    )
                ms = int((time.time() - t0) * 1000)
                event: dict = {"step": "fetch", "status": "done", "ms": ms}
                if fetch_warnings:
                    event["warnings"] = fetch_warnings
                yield _sse(event)
            except Exception as e:
                yield _sse({
                    "step": "fetch", "status": "error", "msg": str(e),
                    "fallback_hint": "请切换为「上传 kubeconfig」模式",
                })
                return

            # Step 3: Diff
            # Resources that failed to fetch due to RBAC/CRD issues are excluded from
            # the diff entirely so they don't falsely appear as "added".
            skip_keys = {
                (w["kind"], w["namespace"], w["name"]) for w in fetch_warnings
            }
            yield _sse({"step": "diff", "status": "running"})
            diff_list, summary = differ.diff(
                rendered_yaml, cluster_yaml,
                default_namespace=namespace,
                skip_keys=skip_keys or None,
            )
            yield _sse({
                "step": "diff", "status": "done",
                "added": summary["added"], "removed": summary["removed"],
                "changed": summary["changed"], "unchanged": summary["unchanged"],
            })

            # Step 4: AI analysis
            yield _sse({"step": "ai", "status": "running"})
            t0 = time.time()
            try:
                diff_text = differ.diff_for_ai(diff_list)
                ai_result = await _run_sync(
                    ai_analyzer.analyze,
                    component, cluster, namespace, diff_text, summary,
                )
                ms = int((time.time() - t0) * 1000)
                risk = _extract_risk(ai_result)
                yield _sse({"step": "ai", "status": "done", "ms": ms, "risk": risk})
            except Exception as e:
                yield _sse({"step": "ai", "status": "error", "msg": str(e)})
                ai_result = f"_AI 分析失败: {e}_"

            # Step 5: Write report
            connection_label = (
                "只读服务号 miks-kubectl-view (miks-proxy)"
                if source_type == "miks-proxy"
                else "用户上传 kubeconfig"
            )
            report_id, _ = report_writer.write_report(
                component=component,
                cluster=cluster,
                namespace=namespace,
                release_name=release_name,
                connection_method=connection_label,
                diff_list=diff_list,
                summary=summary,
                ai_analysis=ai_result,
                run_dir=run_dir,
                fetch_warnings=fetch_warnings,
            )

            yield _sse({"step": "done", "report_id": report_id})

        finally:
            # Cleanup temp dir after 1 hour (background, not blocking)
            pass  # Files are small; OS /tmp cleanup handles it

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/reports")
async def list_reports():
    reports = []
    for p in sorted(REPORTS_DIR.glob("helm-change-report-*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        parts = p.stem.split("-")  # helm-change-report-<comp>-<cluster>
        # Parse: helm(0) change(1) report(2) <comp>(3) <cluster>(4+)
        if len(parts) >= 5:
            comp = parts[3]
            cluster_name = "-".join(parts[4:])
        else:
            comp = cluster_name = p.stem
        reports.append({
            "id": p.stem,
            "component": comp,
            "cluster": cluster_name,
            "modified": p.stat().st_mtime,
            "filename": p.name,
        })
    return JSONResponse(reports)


@app.get("/api/reports/{report_id:path}")
async def get_report(report_id: str):
    # Sanitize to prevent path traversal
    safe_id = report_id.replace("/", "").replace("..", "")
    path = REPORTS_DIR / f"{safe_id}.md"
    if not path.exists():
        return JSONResponse({"error": "Report not found"}, status_code=404)
    return JSONResponse({"content": path.read_text(encoding="utf-8")})


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _run_sync(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _extract_risk(text: str) -> str:
    if "🔴" in text or "高风险" in text:
        return "high"
    if "🟡" in text or "中风险" in text:
        return "medium"
    if "🟢" in text or "低风险" in text:
        return "low"
    return "unknown"
