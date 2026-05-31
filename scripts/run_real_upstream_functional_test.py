from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal
from app.models.api_client_key import ApiClientKey
from app.models.request_log import RequestLog


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start local proxy and run real upstream functional tests.")
    parser.add_argument("--proxy-port", type=int, default=8010)
    parser.add_argument("--upstream-base-url", default="https://aijh.huanmin.top/v1")
    parser.add_argument("--model-name", default="gpt-5.5")
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--total-requests", type=int, default=1)
    parser.add_argument("--client-timeout-s", type=float, default=45.0)
    parser.add_argument("--test-timeout-s", type=float, default=180.0)
    return parser.parse_args()


def wait_for_live(proxy_port: int, *, timeout_s: float = 45.0) -> bool:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{proxy_port}/live"
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def terminate_process(process: subprocess.Popen[bytes], *, timeout_s: float = 10.0) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout_s)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def print_log_summary() -> None:
    db = SessionLocal()
    try:
        api_key = db.query(ApiClientKey).filter(ApiClientKey.name == "real-upstream-functional-key").first()
        if api_key is None:
            print("db_log_summary key_id=None log_count=0", flush=True)
            return
        logs = (
            db.query(RequestLog)
            .filter(RequestLog.api_client_key_id == api_key.id)
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
            .limit(30)
            .all()
        )
        print(f"db_log_summary key_id={api_key.id} log_count={len(logs)}", flush=True)
        for item in logs:
            print(
                "db_log id={id} path={path} stream={stream} success={success} status={status} "
                "code={code} duration_ms={duration} ttfb_ms={ttfb} message={message}".format(
                    id=item.id,
                    path=item.request_path,
                    stream=bool(item.is_stream),
                    success=bool(item.success),
                    status=item.status_code,
                    code=item.error_code or "",
                    duration=item.duration_ms,
                    ttfb=item.ttfb_ms,
                    message=(item.message or "")[:240].replace("\n", " "),
                ),
                flush=True,
            )
    finally:
        db.close()


def main() -> int:
    args = parse_args()
    upstream_api_key = os.environ.get("REAL_UPSTREAM_API_KEY", "").strip()
    if not upstream_api_key:
        print("REAL_UPSTREAM_API_KEY is required", file=sys.stderr, flush=True)
        return 2

    log_dir = PROJECT_ROOT / "data" / "real-upstream-test-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    server_out = (log_dir / "python-runner-server.out.log").open("wb")
    server_err = (log_dir / "python-runner-server.err.log").open("wb")
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT),
            "ENABLE_BACKGROUND_WORKERS": "true",
            "ENABLE_STARTUP_DB_INIT": "false",
            "ENABLE_SCHEDULER": "false",
            "ASYNC_REQUEST_LOG_ENABLED": "false",
            "PROVIDER_MAX_ACTIVE_REQUESTS": "30",
            "PROVIDER_MAX_ACTIVE_STREAMS": "30",
            "GLOBAL_MAX_ACTIVE_REQUESTS": "30",
            "GLOBAL_MAX_ACTIVE_STREAMS": "30",
            "API_KEY_MAX_ACTIVE_REQUESTS": "30",
            "API_KEY_MAX_ACTIVE_STREAMS": "30",
            "ACCOUNT_MAX_ACTIVE_REQUESTS": "30",
            "ACCOUNT_MAX_ACTIVE_STREAMS": "30",
            "UPSTREAM_JSON_CLIENT": "aiohttp",
            "UPSTREAM_STREAM_CLIENT": "aiohttp",
        }
    )
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.proxy_port),
            "--no-access-log",
            "--log-level",
            "info",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=server_out,
        stderr=server_err,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    try:
        if not wait_for_live(args.proxy_port):
            print("local proxy did not become ready", file=sys.stderr, flush=True)
            return 3

        test_command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "real_upstream_functional_test.py"),
            "--proxy-base-url",
            f"http://127.0.0.1:{args.proxy_port}",
            "--upstream-base-url",
            args.upstream_base_url,
            "--model-name",
            args.model_name,
            "--max-concurrency",
            str(max(1, min(args.max_concurrency, 30))),
            "--total-requests",
            str(max(1, min(args.total_requests, 30))),
            "--client-timeout-s",
            str(max(1.0, args.client_timeout_s)),
        ]
        test_process = subprocess.Popen(
            test_command,
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = test_process.communicate(timeout=args.test_timeout_s)
            if stdout:
                print(stdout, end="", flush=True)
            if stderr:
                print(stderr, end="", file=sys.stderr, flush=True)
            return_code = int(test_process.returncode or 0)
        except subprocess.TimeoutExpired:
            terminate_process(test_process)
            stdout, stderr = test_process.communicate()
            if stdout:
                print(stdout, end="", flush=True)
            if stderr:
                print(stderr, end="", file=sys.stderr, flush=True)
            print(f"functional test subprocess timed out after {args.test_timeout_s:.0f}s", file=sys.stderr, flush=True)
            return_code = 124
        print_log_summary()
        return return_code
    finally:
        terminate_process(server)
        server_out.close()
        server_err.close()


if __name__ == "__main__":
    raise SystemExit(main())
