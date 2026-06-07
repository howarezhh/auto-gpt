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

## [ERR-20260601-001] powershell_quoted_range_and_pipe_patterns

**Logged**: 2026-06-01T22:04:53+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
PowerShell commands passed through `pwsh -Command` can misparse regex pipe patterns and range expressions when quoting is not explicit enough.

### Error
```text
project_disk: The term 'project_disk' is not recognized as a name of a cmdlet...
Select-Object: Cannot bind parameter 'Index'. Cannot convert value "35..65" to type "System.Int32".
```

### Context
- Command attempted: `rg -n "PROJECT_DISK_CACHE_SECONDS|project_disk|..." ...`
- Command attempted: `Get-Content ... | Select-Object -Index 35..65`
- Command attempted: nested `pwsh -Command` checks containing `$script` and `$_` inside an outer double-quoted command.
- Environment: Windows workspace using PowerShell 7 via `pwsh`.

### Suggested Fix
Use single-quoted regex patterns and single-quoted script blocks inside nested `pwsh -Command` calls, or prefer `rg -C` for context. For `Select-Object -Index`, ensure the range is evaluated by PowerShell rather than passed as a string.

### Metadata
- Reproducible: yes
- Related Files: none

---

## [ERR-20260607-001] powershell-dollar-expansion-in-nested-pwsh

**Logged**: 2026-06-07T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
Nested `pwsh -Command` scripts containing `$lines[...]` failed because the outer PowerShell expanded `$lines` before PowerShell 7 received the command.

### Error
```text
ParserError: Missing type name after '['.
```

### Context
- Command attempted: `pwsh -NoLogo -NoProfile -Command "$lines = Get-Content ...; $lines[7330..7778]"`
- Environment: Codex shell running through PowerShell, with project rule requiring PowerShell 7.

### Suggested Fix
Wrap nested PowerShell scripts in `& { ... }` and escape `$` as `` `$ `` when the outer shell could parse it first.

### Metadata
- Reproducible: yes
- Related Files: app/static/js/app.js
- Tags: powershell, command-quoting

---
## [ERR-20260531-001] powershell_nested_regex_escaping

**Logged**: 2026-05-31T14:45:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Nested `pwsh -Command` with double-quoted regex patterns can leak backslashes to PowerShell parsing and fail before `rg` runs.

### Error
```text
The term '\' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context
- Command attempted: `pwsh -Command "rg -n \"jsonable_encoder|datetime.*isoformat|isoformat\\(\\)\" app"`
- Environment: Codex shell command already runs under PowerShell, then nested `pwsh -Command` adds another quoting layer.

### Suggested Fix
Use a single-quoted outer PowerShell command for regex searches, or avoid nested `pwsh -Command` when the command contains escaped parentheses or quotes.

### Metadata
- Reproducible: yes
- Related Files: none

---
## [ERR-20260530-001] powershell_nested_rg_pipe_pattern

**Logged**: 2026-05-30T23:55:23+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Nested `pwsh -Command` can misparse ripgrep regex patterns containing `|` when quoting is not protected.

### Error
```text
ParserError: Expressions are only allowed as the first element of a pipeline.
```

### Context
- Command attempted: `pwsh -Command "rg -n \"uvicorn|FastAPI|app =|...\" app start_project.py run.ps1 -S"`
- Environment: Windows PowerShell 7 command executed through another PowerShell command layer.

### Suggested Fix
Use PowerShell single quotes around the ripgrep pattern inside the nested command, for example `& rg -n 'uvicorn|FastAPI|app =' app start_project.py run.ps1 -S`.

### Metadata
- Reproducible: yes
- Related Files: none

---
## [ERR-20260502-002] github_push_network_unreachable

**Logged**: 2026-05-02T23:30:00+08:00
**Priority**: medium
**Status**: pending
**Area**: infra

### Summary
`git push -u origin main` failed because the local environment could not connect to GitHub over HTTPS.

### Error
```text
fatal: unable to access 'https://github.com/howarezhh/auto-gpt.git/': Recv failure: Connection was reset
fatal: unable to access 'https://github.com/howarezhh/auto-gpt.git/': Failed to connect to github.com port 443 after 21089 ms: Could not connect to server
```

### Context
- Command attempted twice: `git push -u origin main`
- Branch: `main`
- Remote: `https://github.com/howarezhh/auto-gpt.git`
- Impact: local commits are ready but not synced to GitHub.

### Suggested Fix
Retry after network/proxy access to `github.com:443` is restored; do not run fetch/pull/merge unless the remote rejects with non-fast-forward and the user confirms a recovery plan.

### Metadata
- Reproducible: yes
- Related Files: none

---
## [ERR-20260502-001] powershell_python_heredoc_syntax

**Logged**: 2026-05-02T23:24:49+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
PowerShell does not support Bash-style `python - <<'PY'` heredoc syntax.

### Error
```text
ParserError: Missing file specification after redirection operator.
```

### Context
- Command attempted: `.venv\Scripts\python.exe - <<'PY' ... PY`
- Environment: Windows PowerShell workspace.
- Impact: quick Python validation snippets fail before Python starts.

### Suggested Fix
Use a PowerShell here-string piped into Python: `@' ... '@ | .venv\Scripts\python.exe -`.

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-05-02T23:24:49+08:00
- **Notes**: Re-ran the same validation using a PowerShell here-string and it passed.

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
- Recurrence-Count: 2
- Last-Seen: 2026-06-07

---
## [ERR-20260502-001] ripgrep_pattern_starting_with_dash

**Logged**: 2026-05-02T11:56:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Searching CSS custom property names with `rg` failed because the pattern started with `--` and was parsed as a flag.

### Error
```text
rg: unrecognized flag --green|--red
```

### Context
- Command attempted: `rg -n "--green|--red" app\static\css\app.css`
- Environment: PowerShell workspace using ripgrep.

### Suggested Fix
Use `rg -n -- "--green|--red" app\static\css\app.css` when the search pattern starts with `-`.

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
## [ERR-20260603-001] nested_pwsh_rg_pipe_pattern

**Logged**: 2026-06-03T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Nested `pwsh -Command` with a double-quoted `rg` alternation pattern can let `|` be parsed by PowerShell instead of ripgrep.

### Error
```text
The term 'stream' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context
- Command attempted: `pwsh -Command "rg -n \"def forward_|stream|retry\" app/services/proxy_service.py"`
- Environment: Codex shell command already runs under PowerShell, then invokes nested PowerShell 7 per project rule.

### Suggested Fix
Use single quotes inside the nested command for ripgrep patterns, for example `pwsh -Command "rg -n 'def forward_|stream|retry' app/services/proxy_service.py"`.

### Metadata
- Reproducible: yes
- Related Files: none

---

## [ERR-20260603-001] powershell-and-apply-patch-in-chinese-path

**Logged**: 2026-06-03T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: infra

### Summary
apply_patch failed in the Chinese-path workspace, and PowerShell commands with variables or embedded quotes failed when not quoted carefully.

### Error
`	ext
The system cannot find the path specified.
ParserError from unescaped PowerShell variables or quote-heavy commands.
`

### Context
- Workspace path contains Chinese characters.
- apply_patch --help failed before reading patch input.
- pwsh -Command snippets using variables or HTML/CSS quote-heavy strings failed when the outer shell expanded variables or parsed quotes.

### Suggested Fix
Use pwsh -NoLogo -Command with single-quoted command bodies for PowerShell variables, escape dollar signs when using double-quoted outer commands, and prefer small line-based replacements over nested here-strings in quote-heavy HTML/CSS edits.

### Metadata
- Reproducible: yes
- Related Files: app/static/js/app.js, app/templates/user_api_keys.html, app/static/css/app.css

---

## [ERR-20260603-002] powershell_heredoc_not_bash

**Logged**: 2026-06-03T10:50:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
PowerShell 7 does not support Bash-style python - <<'PY' heredoc redirection.

### Error
`	ext
ParserError: Missing file specification after redirection operator.
`

### Context
- Command attempted: inline Python smoke test using & '.\.venv\Scripts\python.exe' - <<'PY' in pwsh -Command.
- Environment: Windows workspace where project requires PowerShell 7 commands.

### Suggested Fix
Use a PowerShell here-string piped into Python: @' ... '@ | & '.\.venv\Scripts\python.exe' -.

### Metadata
- Reproducible: yes
- Related Files: none
- See Also: ERR-20260603-001 nested_pwsh_rg_pipe_pattern

---

## [ERR-20260603-003] nested_pwsh_last_exitcode_expansion

**Logged**: 2026-06-03T11:05:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Nested `pwsh -Command` using double-quoted command text can let the outer shell expand `$LASTEXITCODE`, leaving `if ( -ne 0)` in the inner command.

### Error
```text
-ne: The term '-ne' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context
- Command attempted: run multiple Python regression scripts with an inline `$LASTEXITCODE` check.
- Environment: Codex shell command runs under PowerShell and invokes PowerShell 7 per project rule.

### Suggested Fix
Wrap nested PowerShell script blocks in single quotes, for example `pwsh -Command '& { ... if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } ... }'`.

### Metadata
- Reproducible: yes
- Related Files: none
- See Also: ERR-20260603-001 nested_pwsh_rg_pipe_pattern

---

## [ERR-20260606-003] powershell-double-quoted-variable-loss

**Logged**: 2026-06-06T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tooling

### Summary
PowerShell snippets containing `$i++` were wrapped in an outer double-quoted `pwsh -Command` string, so the outer shell consumed `$i` before PowerShell 7 executed the script.

### Error
```text
ParserError: Missing expression after unary operator '++'.
```

### Context
- Command attempted: line-numbered `Get-Content | ForEach-Object { $i++; ... }`
- Files involved: code inspection commands

### Suggested Fix
Wrap complex PowerShell 7 scripts in an outer single-quoted `-Command '...'` string, or use a script block, whenever the command contains `$`, pipes, regex, JSON, or nested quotes.

### Metadata
- Reproducible: yes
- Related Files: 项目全局规范.md
- Tags: powershell, shell-quoting

---

## [ERR-20260606-003] changelog-anchor-drift

**Logged**: 2026-06-06T21:43:25+08:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary
Attempted to patch the project change log using an outdated anchor near the top of the file after the day section had already changed shape.

### Error
```text
apply_patch verification failed: Failed to find expected lines in 项目全局规范-变更记录.md
```

### Context
- Command attempted: patching `项目全局规范-变更记录.md`
- Files involved: `项目全局规范-变更记录.md`
- The file's top section had been reordered by earlier edits, so the expected header block no longer matched the patch anchor.

### Suggested Fix
Re-read the current file head before patching dated changelog sections, and anchor on the exact current entries rather than remembered line order.

### Metadata
- Reproducible: yes
- Related Files: 项目全局规范-变更记录.md
- Tags: docs, patching

---

## [ERR-20260603-004] httpx_stream_response_text_before_read

**Logged**: 2026-06-03T11:35:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
`TestClient.stream(...)` 返回的流式响应在消费前不能读取 `.text`，否则会触发 `httpx.ResponseNotRead`。

### Error
```text
httpx.ResponseNotRead: Attempted to access streaming response content, without having called `read()`.
```

### Context
- Command attempted: run `stage19_completions_regression_check.py`.
- Environment: FastAPI `TestClient` / httpx streaming response test.

### Suggested Fix
流式断言先检查 `status_code`，正文通过 `iter_text()`、`iter_bytes()` 或显式 `read()` 后再断言；失败信息不要直接引用未读取流的 `.text`。

### Metadata
- Reproducible: yes
- Related Files: stage19_completions_regression_check.py

### Resolution
- **Resolved**: 2026-06-03T11:35:00+08:00
- **Notes**: 将失败提示从 `stream_response.text` 改为 `stream_response.status_code`，随后回归通过。

---

## 2026-06-03 PowerShell does not support Bash heredoc redirection

- Context: Tried to run inline Python with `python - <<'PY'` inside PowerShell.
- Error: `ParserError: Missing file specification after redirection operator.`
- Fix: Use a PowerShell here-string piped into Python, e.g. `@' ... '@ | & $py -`.

---

## [ERR-20260605-001] nested_pwsh_rg_regex_pipe_quoting

**Logged**: 2026-06-05T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
Nested `pwsh -Command` with a double-quoted `rg` regex containing `|` can be split by PowerShell before reaching `rg`.

### Error
```text
mapping: The term 'mapping' is not recognized as a name of a cmdlet, function, script file, or executable program.
/v1/responses: The term '/v1/responses' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context
- Command attempted: `pwsh -NoLogo -Command "rg -n \"model_mapping|mapping|...\" app"`.
- Environment: Codex shell command runs under PowerShell and invokes PowerShell 7 per project rule.

### Suggested Fix
Use single quotes for the regex inside the nested PowerShell command, or wrap the inner script in a single-quoted script block: `pwsh -Command '& { rg -n ''pattern1|pattern2'' app }'`.

### Metadata
- Reproducible: yes
- Related Files: none
- See Also: ERR-20260603-001 nested_pwsh_rg_pipe_pattern

---

## [ERR-20260605-002] powershell_rg_glob_path_argument

**Logged**: 2026-06-05T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
On Windows, passing `stage*.py` as an `rg` path argument can be treated as an invalid literal path instead of a glob.

### Error
```text
rg: stage*.py: 文件名、目录名或卷标语法不正确。 (os error 123)
```

### Context
- Command attempted: `rg -n 'pattern' stage*.py test_data app/tests tests`.
- Environment: PowerShell 7 wrapper on Windows.

### Suggested Fix
Use ripgrep's glob option instead of a wildcard path argument, for example `rg -n --glob 'stage*.py' 'pattern' .`.

### Metadata
- Reproducible: yes
- Related Files: none
- See Also: ERR-20260605-001 nested_pwsh_rg_regex_pipe_quoting

---

## [ERR-20260605-003] cache_service_no_clear_method

**Logged**: 2026-06-05T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: verification

### Summary
`CacheService` does not expose a `clear()` helper, so temporary verification scripts must not call it.

### Error
```text
AttributeError: type object 'CacheService' has no attribute 'clear'
```

### Context
- Command attempted: an inline Python regression check for model mapping capability filtering.
- Environment: project `.venv` Python on Windows.

### Suggested Fix
Use `CacheService.invalidate_prefix(...)` for the relevant cache prefixes, or run the verification in a fresh Python process with isolated test data.

### Metadata
- Reproducible: yes
- Related Files: app/services/cache_service.py
## [ERR-20260605-001] regression-test-expectation-drift

**Logged**: 2026-06-05T00:00:00Z
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
Endpoint-independence refactor invalidated old fallback-oriented regression expectations.

### Error
`stage22_model_family_compat_regression_check.py` and `stage14_health_routing_regression_check.py` failed because their assertions still expected endpoint fallback or older parallel probe caps.

### Context
- `stage22_model_family_compat_regression_check.py`
- `stage14_health_routing_regression_check.py`

### Suggested Fix
When removing or disabling a compatibility path, rewrite regression assertions to validate the new primary behavior instead of the old fallback path.

### Metadata
- Reproducible: no
- Related Files: app/services/proxy_service.py, app/services/health_service.py
- Tags: tests, backend

---

## [ERR-20260605-002] provider-out-protocol-fields

**Logged**: 2026-06-05T00:00:00Z
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Provider creation regression still reports missing `protocol_type` and `protocol_label` in `ProviderOut`.

### Error
`stage10_tools_regression_check.py` failed while creating a provider because `ProviderOut` validation reported missing `protocol_type` and `protocol_label`.

### Context
- `stage10_tools_regression_check.py`
- `app/routers/providers.py`
- `app/services/provider_service.py`

### Suggested Fix
Verify the create-provider response payload always includes the protocol fields expected by `ProviderOut`, and reconcile any schema drift between the service dict and the response model.

### Metadata
- Reproducible: unknown
- Related Files: app/schemas/provider.py, app/services/provider_service.py
- Tags: backend, tests

### Resolution
- **Resolved**: 2026-06-05T00:00:00Z
- **Notes**: Verified `ProviderService.provider_to_dict()` includes both fields and reran `stage10_tools_regression_check.py` successfully.

---

## [ERR-20260606-001] powershell-nested-command-quoting

**Logged**: 2026-06-06T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Nested `pwsh -Command "..."` calls can let the outer shell expand `$variables` or break regex quoting before PowerShell 7 receives the command.

### Error
```text
The term 'POST' is not recognized as a name of a cmdlet...
Missing type name after '['.
```

### Context
- Commands attempted: `rg -n "...POST /v1/responses..."` and `$lines[120..260]` inside a nested `pwsh -Command "..."`.
- Environment: Windows shell command wrapper with project-required PowerShell 7.

### Suggested Fix
Use `pwsh -NoLogo -NoProfile -Command '& { ... }'` for scripts containing `$`, `[]`, pipes, regex alternation, or nested quotes.

### Metadata
- Reproducible: yes
- Related Files: 项目全局规范.md
- Tags: powershell, tests
- Recurrence-Count: 7
- Last-Seen: 2026-06-07

---

## [ERR-20260606-002] migration-patch-context-drift

**Logged**: 2026-06-06T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary
Broad `apply_patch` context inserted app_settings migration backfill code into unrelated migration functions with similar loop shapes.

### Error
```text
NameError: name 'runtime_settings' is not defined
```

### Context
- Command attempted: `stage10_tools_regression_check.py`
- Files involved: `app/main.py`
- The same `changed = False` / `for column, ddl in additions.items()` pattern appears in multiple migration helpers.

### Suggested Fix
When editing migration helpers, anchor patches on function-specific names or unique target columns, then run `rg` for newly introduced identifiers to confirm they appear only in intended scopes.

### Metadata
- Reproducible: yes
- Related Files: app/main.py
- Tags: migrations, tests

---
