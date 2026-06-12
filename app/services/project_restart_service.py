from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import HTTPException


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESTART_SCRIPT = PROJECT_ROOT / "run.ps1"
LINUX_SERVICE_NAME = "aotu-gpt.service"


class ProjectRestartService:
    @staticmethod
    def trigger() -> dict[str, Any]:
        if os.name == "nt":
            return ProjectRestartService._trigger_windows()
        return ProjectRestartService._trigger_linux()

    @staticmethod
    def _trigger_windows() -> dict[str, Any]:
        if not RESTART_SCRIPT.exists():
            raise HTTPException(status_code=500, detail="未找到项目启动脚本 run.ps1")

        command_display = 'pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File ".\\run.ps1"'
        command = (
            "Start-Sleep -Milliseconds 800; "
            "Start-Process -FilePath 'pwsh' "
            "-ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File','.\\run.ps1') "
            "-WorkingDirectory (Get-Location).Path "
            "-WindowStyle Hidden"
        )
        try:
            process = subprocess.Popen(
                [
                    "pwsh",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail="未找到 PowerShell 7，请确认 pwsh 已安装并加入 PATH") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"触发项目重启失败：{exc}") from exc
        return {
            "restart_mode": "windows_run_ps1",
            "command": command_display,
            "launcher_pid": int(process.pid),
            "service_name": None,
            "script": str(RESTART_SCRIPT),
            "project_root": str(PROJECT_ROOT),
        }

    @staticmethod
    def _trigger_linux() -> dict[str, Any]:
        if platform.system().lower() != "linux":
            raise HTTPException(status_code=400, detail="一键重启仅支持 Windows 本地开发环境或 Linux systemd 部署环境")
        if shutil.which("systemctl") is None:
            raise HTTPException(status_code=500, detail="当前 Linux 环境未找到 systemctl，无法触发 systemd 服务重启")

        command_display = f"systemctl restart {LINUX_SERVICE_NAME}"
        try:
            process = subprocess.Popen(
                [
                    "sh",
                    "-c",
                    f"sleep 0.8; exec systemctl restart {LINUX_SERVICE_NAME}",
                ],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail="当前 Linux 环境未找到 sh，无法触发项目重启") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"触发项目重启失败：{exc}") from exc
        return {
            "restart_mode": "linux_systemd",
            "command": command_display,
            "launcher_pid": int(process.pid),
            "service_name": LINUX_SERVICE_NAME,
            "script": None,
            "project_root": str(PROJECT_ROOT),
        }
