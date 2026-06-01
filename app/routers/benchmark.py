from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_ROOT = PROJECT_ROOT / "data" / "benchmark-reports"
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "real_concurrency_benchmark.py"
MAX_LOG_LINES = 600

router = APIRouter(prefix="/api/benchmark", tags=["benchmark"])


class RealConcurrencyBenchmarkRequest(BaseModel):
    proxy_base_url: str = Field(default="http://127.0.0.1:8000", min_length=1, max_length=500)
    endpoint: str = Field(default="chat")
    modes: list[str] = Field(default_factory=lambda: ["json", "stream"])
    raw_api_key: str = Field(default="", max_length=4096)
    model_name: str = Field(default="", max_length=200)
    concurrency: int = Field(default=100, ge=1, le=5000)
    probe_min_concurrency: int = Field(default=0, ge=0, le=5000)
    probe_max_concurrency: int = Field(default=0, ge=0, le=20000)
    max_probe_rounds: int = Field(default=10, ge=1, le=30)
    probe_scale: float = Field(default=1.5, ge=1.1, le=5)
    sample_requests: int = Field(default=300, ge=1, le=200000)
    requests_per_concurrency: int = Field(default=2, ge=1, le=100)
    warmup_requests: int = Field(default=12, ge=0, le=10000)
    client_timeout_s: float = Field(default=180, ge=1, le=3600)
    request_timeout_s: float = Field(default=180, ge=1, le=3600)
    boundary_max_rounds: int = Field(default=8, ge=0, le=20)
    progress_interval_s: float = Field(default=0.5, ge=0.1, le=10)
    success_rate_threshold: float = Field(default=0.99, ge=0, le=1)
    timeout_error_threshold: int = Field(default=0, ge=0, le=100000)
    busy_error_threshold: int = Field(default=0, ge=0, le=100000)
    prompt: str = Field(default="请用两句话简洁回答：并发压测探针。", min_length=1, max_length=2000)

    @field_validator("endpoint")
    @classmethod
    def normalize_endpoint(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"chat", "responses"}:
            raise ValueError("endpoint 仅支持 chat 或 responses")
        return normalized

    @field_validator("modes")
    @classmethod
    def normalize_modes(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            current = (item or "").strip().lower()
            if current in {"json", "non-stream", "nonstream"}:
                current = "json"
            if current not in {"json", "stream"}:
                continue
            if current not in normalized:
                normalized.append(current)
        if not normalized:
            raise ValueError("至少选择一种测试模式")
        return normalized

    @field_validator("proxy_base_url", "model_name", "raw_api_key", "prompt")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


@dataclass
class BenchmarkJob:
    job_id: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    return_code: int | None
    config: dict[str, Any]
    logs: list[str] = field(default_factory=list)
    current_line: str = ""
    progress: dict[str, Any] = field(default_factory=dict)
    stage_results: list[dict[str, Any]] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    json_report_path: str | None = None
    html_report_path: str | None = None
    error_message: str | None = None
    process: subprocess.Popen[str] | None = None


_jobs: dict[str, BenchmarkJob] = {}
_jobs_lock = threading.Lock()

_PROGRESS_RE = re.compile(
    r"\[(json|stream) c=(\d+)\]\s+(\d+)/(\d+)\s+\(([\d.]+)%\).*?rps=\s*([\d.]+)"
)
_STAGE_RE = re.compile(
    r"\[(json|stream) c=(\d+)\]\s+(\S+)\s+success=(\d+)/(\d+)\s+"
    r"success_rate=([\d.]+)%\s+rps=([\d.]+)\s+p95=([\d.]+)ms\s+max=([\d.]+)ms"
)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sanitize_config(payload: RealConcurrencyBenchmarkRequest) -> dict[str, Any]:
    data = payload.model_dump()
    data["raw_api_key_provided"] = bool(data.pop("raw_api_key", ""))
    return data


def _build_command(payload: RealConcurrencyBenchmarkRequest) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(SCRIPT_PATH),
        "--proxy-base-url",
        payload.proxy_base_url,
        "--endpoint",
        payload.endpoint,
        "--modes",
        ",".join(payload.modes),
        "--concurrency",
        str(payload.concurrency),
        "--probe-min-concurrency",
        str(payload.probe_min_concurrency),
        "--probe-max-concurrency",
        str(payload.probe_max_concurrency),
        "--max-probe-rounds",
        str(payload.max_probe_rounds),
        "--probe-scale",
        str(payload.probe_scale),
        "--sample-requests",
        str(payload.sample_requests),
        "--requests-per-concurrency",
        str(payload.requests_per_concurrency),
        "--warmup-requests",
        str(payload.warmup_requests),
        "--client-timeout-s",
        str(payload.client_timeout_s),
        "--request-timeout-s",
        str(payload.request_timeout_s),
        "--boundary-max-rounds",
        str(payload.boundary_max_rounds),
        "--progress-interval-s",
        str(payload.progress_interval_s),
        "--success-rate-threshold",
        str(payload.success_rate_threshold),
        "--timeout-error-threshold",
        str(payload.timeout_error_threshold),
        "--busy-error-threshold",
        str(payload.busy_error_threshold),
        "--prompt",
        payload.prompt,
    ]
    if payload.model_name:
        command.extend(["--model-name", payload.model_name])
    return command


def _append_log(job: BenchmarkJob, line: str) -> None:
    current = line.strip()
    if not current:
        return
    with _jobs_lock:
        job.current_line = current
        job.logs.append(current)
        if len(job.logs) > MAX_LOG_LINES:
            job.logs = job.logs[-MAX_LOG_LINES:]
        _parse_job_line(job, current)


def _parse_job_line(job: BenchmarkJob, line: str) -> None:
    progress_match = _PROGRESS_RE.search(line)
    if progress_match:
        mode, concurrency, done, total, percent, rps = progress_match.groups()
        job.progress = {
            "mode": mode,
            "concurrency": int(concurrency),
            "done": int(done),
            "total": int(total),
            "percent": float(percent),
            "rps": float(rps),
        }
    stage_match = _STAGE_RE.search(line)
    if stage_match:
        mode, concurrency, stability, success, total, success_rate, rps, p95, max_latency = stage_match.groups()
        stage = {
            "mode": mode,
            "concurrency": int(concurrency),
            "stable": stability == "稳定",
            "success_requests": int(success),
            "total_requests": int(total),
            "success_rate": float(success_rate),
            "throughput_rps": float(rps),
            "latency_p95_ms": float(p95),
            "latency_max_ms": float(max_latency),
        }
        job.stage_results = [
            item for item in job.stage_results
            if not (item["mode"] == stage["mode"] and item["concurrency"] == stage["concurrency"])
        ]
        job.stage_results.append(stage)
        job.stage_results.sort(key=lambda item: (item["mode"], item["concurrency"]))
    if line.startswith("- json:") or line.startswith("- stream:"):
        job.summaries.append(line[2:].strip())
    if line.startswith("json_report="):
        job.json_report_path = line.split("=", 1)[1].strip()
    if line.startswith("html_report="):
        job.html_report_path = line.split("=", 1)[1].strip()


def _read_process_output(job_id: str, process: subprocess.Popen[str]) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            for line in re.split(r"[\r\n]+", raw_line):
                _append_log(job, line)
        return_code = process.wait()
        with _jobs_lock:
            job.return_code = return_code
            job.finished_at = _now_text()
            if job.status == "stopped":
                return
            if return_code == 0:
                job.status = "completed"
                _load_json_report_summary(job)
            else:
                job.status = "failed"
                job.error_message = job.current_line or f"进程退出码 {return_code}"
    except Exception as exc:
        with _jobs_lock:
            job.status = "failed"
            job.finished_at = _now_text()
            job.error_message = str(exc)


def _load_json_report_summary(job: BenchmarkJob) -> None:
    if not job.json_report_path:
        return
    path = Path(job.json_report_path)
    try:
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    stage_results: list[dict[str, Any]] = []
    summaries: list[str] = []
    for report in data.get("mode_reports", []):
        summary = report.get("summary")
        if summary:
            summaries.append(f"{report.get('mode', '-')}: {summary}")
        for stage in report.get("stage_results", []):
            stage_results.append({
                "mode": stage.get("mode"),
                "concurrency": stage.get("concurrency"),
                "stable": stage.get("stable"),
                "success_requests": stage.get("success_requests"),
                "total_requests": stage.get("total_requests"),
                "success_rate": round(float(stage.get("success_rate") or 0) * 100, 2),
                "throughput_rps": stage.get("throughput_rps"),
                "latency_p95_ms": stage.get("latency_p95_ms"),
                "latency_max_ms": stage.get("latency_max_ms"),
                "first_byte_p95_ms": stage.get("first_byte_p95_ms"),
            })
    if stage_results:
        job.stage_results = sorted(stage_results, key=lambda item: (item.get("mode") or "", item.get("concurrency") or 0))
    if summaries:
        job.summaries = summaries


def _job_to_response(job: BenchmarkJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "return_code": job.return_code,
        "config": job.config,
        "logs": list(job.logs),
        "current_line": job.current_line,
        "progress": dict(job.progress),
        "stage_results": list(job.stage_results),
        "summaries": list(job.summaries),
        "json_report_available": bool(job.json_report_path),
        "html_report_available": bool(job.html_report_path),
        "error_message": job.error_message,
    }


def _resolve_report_path(job: BenchmarkJob, report_type: str) -> Path:
    raw_path = job.html_report_path if report_type == "html" else job.json_report_path
    if not raw_path:
        raise HTTPException(status_code=404, detail="报告尚未生成")
    candidate = Path(raw_path)
    path = candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()
    report_root = REPORT_ROOT.resolve()
    if report_root not in path.parents:
        raise HTTPException(status_code=403, detail="报告路径不在允许范围内")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="报告文件不存在")
    return path


@router.post("/real-concurrency/start")
def start_real_concurrency_benchmark(payload: RealConcurrencyBenchmarkRequest) -> dict[str, Any]:
    if not SCRIPT_PATH.is_file():
        raise HTTPException(status_code=500, detail="真实并发测试脚本不存在")
    with _jobs_lock:
        active_job = next((item for item in _jobs.values() if item.status == "running"), None)
        if active_job is not None:
            raise HTTPException(status_code=409, detail=f"已有压测任务运行中：{active_job.job_id}")

    job_id = uuid4().hex
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if payload.raw_api_key:
        env["REAL_BENCHMARK_RAW_API_KEY"] = payload.raw_api_key
    process = subprocess.Popen(
        _build_command(payload),
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    job = BenchmarkJob(
        job_id=job_id,
        status="running",
        created_at=_now_text(),
        started_at=_now_text(),
        finished_at=None,
        return_code=None,
        config=_sanitize_config(payload),
        process=process,
    )
    with _jobs_lock:
        _jobs[job_id] = job
    threading.Thread(target=_read_process_output, args=(job_id, process), daemon=True).start()
    return _job_to_response(job)


@router.get("/real-concurrency/jobs")
def list_real_concurrency_jobs() -> list[dict[str, Any]]:
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda item: item.created_at, reverse=True)
        return [_job_to_response(item) for item in jobs[:10]]


@router.get("/real-concurrency/jobs/{job_id}")
def get_real_concurrency_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="压测任务不存在")
        return _job_to_response(job)


@router.post("/real-concurrency/jobs/{job_id}/stop")
def stop_real_concurrency_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="压测任务不存在")
        process = job.process
        if job.status != "running" or process is None or process.poll() is not None:
            return _job_to_response(job)
        job.status = "stopped"
        job.finished_at = _now_text()
    process.terminate()
    return _job_to_response(job)


@router.get("/real-concurrency/jobs/{job_id}/report/{report_type}")
def get_real_concurrency_report(job_id: str, report_type: str) -> FileResponse:
    if report_type not in {"html", "json"}:
        raise HTTPException(status_code=404, detail="报告类型不存在")
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="压测任务不存在")
        path = _resolve_report_path(job, report_type)
    media_type = "text/html" if report_type == "html" else "application/json"
    return FileResponse(path, media_type=media_type, filename=path.name)
