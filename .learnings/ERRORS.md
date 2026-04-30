# Errors

## [ERR-20260429-001] docker_compose_postgres_engine_unavailable

**Logged**: 2026-04-29T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Docker CLI and Compose were installed, but Docker Desktop Linux Engine was not running, so local PostgreSQL container startup could not be used for verification.

### Error
```text
unable to get image 'postgres:16': error during connect: Get "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.51/images/postgres:16/json": open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

### Context
- Command attempted: `docker compose -f docker-compose.postgres.yml up -d`
- Environment: Windows PowerShell workspace
- Task: Stage 1 PostgreSQL migration verification

### Suggested Fix
Start Docker Desktop Linux Engine before running compose verification, or verify PostgreSQL against an already running local/remote PostgreSQL instance.

### Metadata
- Reproducible: yes
- Related Files: docker-compose.postgres.yml

---
## [ERR-20260430-002] github_https_push_connection_reset

**Logged**: 2026-04-30T23:55:00+08:00
**Priority**: high
**Status**: pending
**Area**: infra

### Summary
Pushing to GitHub over HTTPS failed twice because the connection was reset.

### Error
```text
fatal: unable to access 'https://github.com/howarezhh/auto-gpt.git/': Recv failure: Connection was reset
```

### Context
- Command attempted: `git push -u origin main`
- Remote: `https://github.com/howarezhh/auto-gpt.git`
- Local commit exists and branch is ahead by 1, but remote push did not complete.

### Suggested Fix
Retry from a network path that can reach GitHub HTTPS, or switch the remote to an available SSH/proxy configuration after confirming credentials.

### Metadata
- Reproducible: yes
- Related Files: none

---

## [ERR-20260429-003] gunicorn_not_runnable_on_windows

**Logged**: 2026-04-29T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Gunicorn installed successfully in the project virtual environment, but `python -m gunicorn --version` cannot run on Windows because Gunicorn imports Unix-only `fcntl`.

### Error
```text
ModuleNotFoundError: No module named 'fcntl'
```

### Context
- Command attempted: `.venv\Scripts\python.exe -m gunicorn --version`
- Environment: Windows PowerShell workspace
- Task: Stage 2 multi-worker production startup verification

### Suggested Fix
Verify Gunicorn startup on the target Linux/Alibaba Cloud host. Keep Windows startup on Uvicorn/`run.ps1` for local development only.

### Metadata
- Reproducible: yes
- Related Files: requirements.txt, start_aliyun.sh, README.md, 启动指南.md

---

## [ERR-20260429-002] bash_syntax_check_blocked_by_wsl

**Logged**: 2026-04-29T21:15:14+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Default `bash -n` could not validate `start_aliyun.sh` because the Windows environment routes bash through WSL and WSL virtualization support is unavailable.

### Error
```text
Bash/Service/CreateInstance/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED
```

### Context
- Command attempted: `bash -n start_aliyun.sh`
- Fallback attempted: `C:\Program Files\Git\bin\bash.exe -n start_aliyun.sh`
- Fallback result: Git Bash was not installed at the default path.

### Suggested Fix
Validate shell scripts on the target Ubuntu/Alibaba Cloud host, install Git Bash locally, or enable WSL virtualization support before using `bash -n` on this Windows machine.

### Metadata
- Reproducible: yes
- Related Files: start_aliyun.sh

---
# 2026-04-29 PowerShell 不支持 Bash heredoc 重定向

- Context: 在 Windows PowerShell 中运行 `python - <<'PY'` 做内联 Python 验证。
- Error: PowerShell 报 `Missing file specification after redirection operator`。
- Fix: 使用 PowerShell here-string：`@' ... '@ | .\.venv\Scripts\python.exe -`。
- Prevention: 当前 shell 为 PowerShell 时，不要使用 Bash heredoc；内联 Python 优先用 here-string 管道。

# 2026-04-29 Windows SQLite 临时文件测试需要显式释放句柄

- Context: 使用 SQLAlchemy + SQLite 临时文件做幂等验证。
- Error: `NamedTemporaryFile` 路径无法被 SQLite 打开，改用 `TemporaryDirectory` 后清理时报 `PermissionError: [WinError 32]`。
- Fix: 使用临时目录中的普通 `.db` 文件，并在退出前先关闭 session，再调用 `engine.dispose()`。
- Prevention: Windows 上 SQLite 文件测试不要复用仍打开的 `NamedTemporaryFile`；清理临时目录前必须释放 SQLAlchemy engine 连接池。

## [ERR-20260430-001] powershell_nested_quote_variable_expansion

**Logged**: 2026-04-30T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Nested `pwsh -Command` strings containing PowerShell `$` variables can be expanded by the outer command before the inner command executes.

### Error
```text
Missing expression after unary operator '++'.
```

### Context
- Command attempted: nested `pwsh -Command` with `$i=0` and `$i++` inside an outer double-quoted command string.
- Environment: Windows PowerShell / pwsh nested command invocation.

### Suggested Fix
Avoid nesting PowerShell code containing `$` variables inside outer double quotes. Use single-quoted outer command text, escape `$`, or avoid the nested `pwsh -Command` layer when the current shell is already PowerShell.

### Metadata
- Reproducible: yes
- Related Files: none

---

## [ERR-20260430-002] powershell_nested_here_string_in_pwsh_command

**Logged**: 2026-04-30T17:20:00+08:00
**Priority**: medium
**Status**: pending
**Area**: infra

### Summary
Nested `pwsh -Command` plus PowerShell here-string can be parsed by the outer shell unexpectedly, causing Python source piped to `python -` to be interpreted as PowerShell.

### Error
```text
ParserError: The 'from' keyword is not supported in this version of the language.
```

### Context
- Command attempted: embed `$script = @' ... '@; $script | .\.venv\Scripts\python.exe -` inside another `pwsh -Command` string.
- Environment: tool command already executes under PowerShell, then nested `pwsh -Command` adds another quoting layer.

### Suggested Fix
Avoid nested `pwsh -Command` for multiline Python. Prefer direct current-shell commands, short `python -c`, or create a temporary script file when code is multiline.

### Metadata
- Reproducible: yes
- Related Files: none

---
## [ERR-20260430-001] local_bash_validation_wsl_unavailable

**Logged**: 2026-04-30T23:43:00+08:00
**Priority**: medium
**Status**: pending
**Area**: infra

### Summary
Local `bash -n start_aliyun.sh` cannot be trusted on this Windows workspace because WSL/Hyper-V is unavailable.

### Error
```text
Wsl/Service/CreateInstance/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED
```

### Context
- Command attempted: `bash -n start_aliyun.sh`
- Environment: Windows PowerShell 7 workspace with WSL command present but Hyper-V support unavailable.
- Impact: shell script syntax checks must be run on the target Linux server or another working Bash environment.

### Suggested Fix
Validate `start_aliyun.sh` on the Ubuntu ECS host with `bash -n start_aliyun.sh` before rerunning deployment.

### Metadata
- Reproducible: yes
- Related Files: start_aliyun.sh

---
