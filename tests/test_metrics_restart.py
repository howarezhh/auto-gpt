from __future__ import annotations

from app.services.project_restart_service import ProjectRestartService
from app.services import project_restart_service


def test_linux_project_restart_uses_fixed_systemd_service(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeProcess:
        pid = 4321

    def fake_popen(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess()

    monkeypatch.setattr(project_restart_service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(project_restart_service.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(project_restart_service.subprocess, "Popen", fake_popen)

    result = ProjectRestartService._trigger_linux()

    assert result["restart_mode"] == "linux_systemd"
    assert result["command"] == "systemctl restart aotu-gpt.service"
    assert result["launcher_pid"] == 4321
    assert result["service_name"] == "aotu-gpt.service"
    assert result["script"] is None
    assert calls[0]["args"] == [
        "sh",
        "-c",
        "sleep 0.8; exec systemctl restart aotu-gpt.service",
    ]
    assert calls[0]["kwargs"]["start_new_session"] is True
