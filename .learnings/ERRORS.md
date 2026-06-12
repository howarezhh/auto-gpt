## [ERR-20260609-001] benchmark_content_guard_overhead

**Logged**: 2026-06-09T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Content guard benchmark fixture initially missed provider runtime state fields.

### Error
```
AttributeError: 'BenchmarkProvider' object has no attribute 'circuit_state'
```

### Context
- Command: `.venv\Scripts\python.exe scripts\benchmark_content_guard_overhead.py --iterations 10 --concurrency 2 --scan-bytes 2048`
- The new runtime benchmark path calls `ContentRuntimeGuardService.record_runtime_pass()`, which reads provider and model circuit/content state fields.

### Suggested Fix
Benchmark fixtures that call runtime guard services must include the provider/model state fields touched by pass/failure recording.

### Metadata
- Reproducible: yes
- Related Files: `scripts/benchmark_content_guard_overhead.py`

### Resolution
- **Resolved**: 2026-06-09T00:00:00+08:00
- **Notes**: Added the missing provider/model content integrity and circuit state fields to benchmark fixtures.

---

## [ERR-20260610-003] rg_windows_glob_path

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
在 Windows PowerShell 中把 `stage*_regression_check.py` 作为普通路径参数传给 `rg`，ripgrep 报路径语法错误。

### Error
```
rg: stage*_regression_check.py: 文件名、目录名或卷标语法不正确。 (os error 123)
```

### Context
- Command: `rg ... tests stage*_regression_check.py`
- Windows 路径解析不接受该通配形式作为实际路径参数。

### Suggested Fix
使用 ripgrep 自身的 `-g 'stage*_regression_check.py' .` 做文件过滤，或先只查目录再单独查根目录匹配文件。

### Metadata
- Reproducible: yes
- Related Files: `.learnings/ERRORS.md`

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: 已改用 `rg ... tests; rg ... -g 'stage*_regression_check.py' .`。

---

## [ERR-20260610-002] pwsh_select_string_interpolation

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
在外层双引号 `pwsh -Command` 中拼接 `Select-String | ForEach-Object { "$($_...)" }`，导致 `$_` 与反斜杠被外层解析破坏。

### Error
```
InvalidOperation: You cannot call a method on a null-valued expression.
\: The term '\' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context
- Command: `Select-String ... | ForEach-Object { "$($_.Path):$($_.LineNumber):$($_.Line.Trim())" }`
- 外层 PowerShell 先处理插值和转义，内层脚本收到的表达式已失真。

### Suggested Fix
需要输出匹配行号时优先用 `rg -n --fixed-strings` 或把 PowerShell 脚本放进单引号脚本块；避免在双引号 `-Command` 中嵌套 `$()` 插值。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: 改用 `rg -n` 获取关键实现行号。

---

## [ERR-20260610-002] postgres_test_database_create_privilege

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
尝试为 PostgreSQL 回归脚本创建隔离测试库 `aotu_gpt_test` 时，当前业务账号缺少 `CREATE DATABASE` 权限。

### Error
```
psycopg.errors.InsufficientPrivilege: permission denied to create database
```

### Context
- Command: PowerShell here-string piped to `.venv\Scripts\python.exe -`
- DSN: `postgresql://aotu_gpt:***@127.0.0.1:5432/postgres`
- 组合测试中的 `tests/test_background_guard_regression.py` 需要 `aotu_gpt_test` 已存在。

### Suggested Fix
使用 PostgreSQL 管理员角色预先创建 `aotu_gpt_test`，或为本地测试配置具备建库权限的专用管理账号；不要把回归脚本指向生产库。

### Metadata
- Reproducible: yes
- Related Files: `tests/test_background_guard_regression.py`

---

## [ERR-20260610-002] postgres_test_database_create_privilege

**Logged**: 2026-06-10T01:24:00+08:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
本地 PostgreSQL 角色可连接服务但没有 `CREATE DATABASE` 权限，导致依赖 `aotu_gpt_test` 的回归测试无法自动补齐隔离测试库。

### Error
```
psycopg.errors.InsufficientPrivilege: permission denied to create database
```

### Context
- Command: 使用 `.venv` 中 `psycopg` 连接 `postgres` 数据库并执行 `CREATE DATABASE aotu_gpt_test`
- Earlier failure: `database "aotu_gpt_test" does not exist`

### Suggested Fix
运行 PostgreSQL 依赖回归测试前，用具备 `CREATEDB` 权限的管理员账户预先创建 `aotu_gpt_test`，或为测试角色授予创建隔离测试库的权限；禁止把这类测试指向生产库。

### Metadata
- Reproducible: yes
- Related Files: `tests/test_background_guard_regression.py`, `stage16_observability_regression_check.py`, `项目问题及解决方法记录.md`

---

## [ERR-20260610-001] pwsh_outer_variable_expansion

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
在默认 shell 外层调用 `pwsh -Command "..."` 时，命令里的 `$lines[...]` 被外层 PowerShell 提前解析，导致传给 PowerShell 7 的脚本块残缺。

### Error
```
ParserError: Missing type name after '['.
```

### Context
- Command: `pwsh -NoLogo -NoProfile -Command "$lines = Get-Content ...; $lines[2180..2725]"`
- 项目要求使用 PowerShell 7，但外层 shell 仍会先解析双引号中的 `$`。

### Suggested Fix
包含 `$`、数组下标或复杂表达式的 PowerShell 7 命令使用单引号包裹 `-Command '& { ... }'` 脚本块，避免外层提前展开变量。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: 后续代码片段读取已改用 `pwsh -Command '& { ... }'`。

---

## [ERR-20260610-001] pwsh_rg_pipe_quote

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: shell

### Summary
在 PowerShell 7 校验代码时，把包含 `|` 的 `rg` 正则放进外层双引号命令，外层 shell 先解析了管道符，导致 `pwsh` 收到损坏命令。

### Error
```
ParserError: You must provide a value expression following the '-' operator.
```

### Suggested Fix
包含 `|`、`$`、双引号或括号的检索命令优先拆成多条 `rg --fixed-strings` 查询，或使用可验证的转义/脚本块，避免外层 shell 与目标命令双重解析。

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: 已改用拆分后的固定字符串查询继续校验。

---

## [ERR-20260610-001] pwsh_variable_expansion

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
在默认外层 PowerShell 中用双引号包裹 `pwsh -Command` 脚本时，`$PSVersionTable` 被外层提前展开，导致 PowerShell 7 版本检测命令损坏。

### Error
```
ParserError: An expression was expected after '('.
```

### Context
- Command: `pwsh -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"`
- 外层 shell 先解析 `$PSVersionTable`，传给 PowerShell 7 的脚本变成无效表达式。

### Suggested Fix
包含 `$` 的 PowerShell 7 脚本必须使用单引号包裹或拆成脚本块，例如 `pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'`。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: 后续命令已改用单引号脚本块。

---

## [ERR-20260610-003] stage36_ip_management_postgres_database_missing

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
IP 管理回归脚本连接本地 PostgreSQL 测试库时，目标数据库 `aotu_gpt_test` 不存在，导致脚本在建表前失败。

### Error
```
psycopg.OperationalError: connection failed: connection to server at "127.0.0.1", port 5432 failed: FATAL: database "aotu_gpt_test" does not exist
```

### Context
- Command: `.venv\Scripts\python.exe stage36_ip_management_regression_check.py`
- 环境：本地 PostgreSQL 服务可连接，但缺少脚本使用的测试数据库。

### Suggested Fix
运行依赖 PostgreSQL 的回归脚本前，先创建 `aotu_gpt_test` 测试库，或把 `DATABASE_URL` 指向已存在的隔离测试库。

### Metadata
- Reproducible: yes
- Related Files: `stage36_ip_management_regression_check.py`

---

## [ERR-20260610-002] pwsh_command_variable_quote

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: shell

### Summary
通过外层 PowerShell 调用 `pwsh -Command` 时，双引号中的 `$PSVersionTable` 被外层提前展开，传入内层后变成损坏表达式。

### Error
```
ParserError: An expression was expected after '('.
```

### Context
- Command: `pwsh -NoLogo -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"`
- 外层 shell 先处理 `$PSVersionTable`，导致内层 PowerShell 收到错误内容。

### Suggested Fix
涉及 `$` 的 `pwsh -Command` 参数优先用外层单引号包裹，例如 `pwsh -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'`。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: 已改用外层单引号确认当前 PowerShell 版本为 7.6.0。

---

## [ERR-20260610-001] pwsh_rg_regex_pipe_quote

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
在 PowerShell 7 的 `-Command` 双引号字符串里直接放入包含 `|` 和内层双引号的 `rg` 正则，外层解析把正则拆成管道命令，导致校验命令失败。

### Error
```
The term 'admin-audits' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context
- Command: `rg -n 'data-logging-tab=\"(admin-audits|user-operations|alert-events)\"|...' ...`
- 外层 `pwsh -Command "..."` 先处理了内层双引号和管道符，`rg` 没有收到完整正则。

### Suggested Fix
复杂 `rg` 正则在 PowerShell 7 中优先拆成多条 `rg --fixed-strings`；若必须使用正则，应把外层命令改短，避免同一层同时承载双引号、反斜杠和 `|`。

### Metadata
- Reproducible: yes
- Related Files: `app/static/js/app.js`, `app/templates/logs.html`, `app/routers/logging_api.py`
- See Also: ERR-20260609-004

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: 改用拆分后的 `rg --fixed-strings` 查询；后续 `$LASTEXITCODE` 类变量检查改用转义后的脚本块或不依赖外层变量的 `Select-String` 检查。

---

## [ERR-20260609-005] pwsh_outer_variable_expansion

**Logged**: 2026-06-09T13:38:55+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
在 `pwsh -Command "..."` 中直接写 `$missing`、`$_` 等 PowerShell 变量，外层命令字符串会提前解析变量，导致内层脚本被破坏。

### Error
```
=: The term '=' is not recognized as a name of a cmdlet...
-match: The term '-match' is not recognized as a name of a cmdlet...
```

### Context
- Command: `pwsh -Command "$missing = @(); Get-Content ... { if ($_ -match ...) ... }"`
- 目标是统计 `问题.md` 中 451-500 是否均已标记完成。

### Suggested Fix
包含 `$`、`$_`、管道脚本块或多层引号的 PowerShell 检查命令必须改用外层单引号、脚本文件，或 PowerShell here-string 管道到项目虚拟环境，避免外层 shell 抢先展开变量。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-09T13:38:55+08:00
- **Notes**: 已改用 PowerShell here-string 管道到 `.venv\Scripts\python.exe -` 完成统计，输出 `451-500 total=50 missing_completed=0`。

---

## [ERR-20260609-003] powershell_command_variable_expansion

**Logged**: 2026-06-09T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary
使用 `pwsh -Command "..."` 执行含 `$p`、`$lines`、`foreach($i ...)` 的片段时，外层 shell 提前展开变量，导致 PowerShell 语法错误。

### Error
```
ParserError: Missing variable name after foreach.
```

### Context
- Command: `pwsh -NoLogo -NoProfile -Command "$p='...'; $lines=Get-Content ...; foreach($i in ...){ ... }"`
- 项目规范要求复杂 PowerShell 命令优先使用单引号包裹，避免 `$`、管道或嵌套引号被外层解析破坏。

### Suggested Fix
含 `$` 的 PowerShell 片段必须用单引号包裹 `-Command` 内容，或拆成更短命令，必要时改用脚本块。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-09T00:00:00+08:00
- **Notes**: 后续读取命令已改用单引号包裹 `-Command`。

---

## [ERR-20260609-003] powershell_nested_herestring

**Logged**: 2026-06-09T23:35:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: shell

### Summary
通过外层 PowerShell 调用 `pwsh -Command` 时，内层单引号 here-string 被外层提前解析，导致 Python 片段内容被当成 PowerShell 语句执行。

### Error
```
ParserError: The 'from' keyword is not supported in this version of the language.
```

### Suggested Fix
让 `pwsh -Command` 参数整体用单引号传递，并在内层使用双引号 here-string `@"..."@`；这样外层不会抢先处理 `@'...'@`。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-09T23:35:00+08:00
- **Notes**: 已用内层双引号 here-string 复跑 Python schema 校验并通过。

---

## [ERR-20260609-003] logging_api_import

**Logged**: 2026-06-09T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary
类型化日志接口新增过滤参数后，`list_content_guard_events()` 出现重复参数导致模块无法导入。

### Error
```
SyntaxError: duplicate argument 'provider_id' in function definition
```

### Context
- Command: `.venv\Scripts\python.exe stage33_content_guard_regression_check.py`
- 导入 `app.routers.logging_api` 时失败，阻断内容防护回归脚本。

### Suggested Fix
新增接口查询参数时先检查同名参数是否已存在；重复字段只保留一份。

### Metadata
- Reproducible: yes
- Related Files: `app/routers/logging_api.py`

### Resolution
- **Resolved**: 2026-06-09T00:00:00+08:00
- **Notes**: Removed duplicate `provider_id`、`model_name` and `is_stream` declarations.

---

## [ERR-20260609-002] stage33_content_guard_regression_check

**Logged**: 2026-06-09T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
新增回归断言使用 `httpx.TimeoutException` 时漏导入 `httpx`。

### Error
```
NameError: name 'httpx' is not defined
```

### Context
- Command: `.venv\Scripts\python.exe stage33_content_guard_regression_check.py`
- 新增内容预检异常 retryable 断言引用了 `httpx.TimeoutException`。

### Suggested Fix
为测试脚本新增外部异常类型断言时，同步检查测试文件顶部导入。

### Metadata
- Reproducible: yes
- Related Files: `stage33_content_guard_regression_check.py`

### Resolution
- **Resolved**: 2026-06-09T00:00:00+08:00
- **Notes**: Added the missing `httpx` import.

---

## [ERR-20260609-004] pwsh_rg_regex_quote

**Logged**: 2026-06-09T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
在 PowerShell 7 中把包含双引号和反斜杠的多个 `rg` 正则拼进同一个命令，导致 ripgrep 收到损坏的正则。

### Error
```
rg: regex parse error:
error: unclosed group
```

### Context
- Command: `rg -n "precheck/probe|...|data-rule-field=\"reason\"" app ...`
- 外层 PowerShell 字符串处理后，末尾正则变成未闭合分组。

### Suggested Fix
多关键字代码检索优先使用多条 `rg --fixed-strings`，复杂正则拆短执行，避免外层 shell 与 ripgrep 正则同时解析引号。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-09T00:00:00+08:00
- **Notes**: 已改用拆分后的 `rg --fixed-strings` 查询。

---

## [ERR-20260610-001] pwsh_outer_variable_expansion

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
在外层 PowerShell 中用双引号传递 `pwsh -Command` 脚本块时，`$lines` 被外层提前展开，导致内层脚本变成非法索引表达式。

### Error
```
ParserError: Missing type name after '['.
```

### Context
- Command: `pwsh -NoLogo -NoProfile -Command "$lines = Get-Content ...; $lines[0..130] ..."`
- 外层 shell 在启动 PowerShell 7 前先处理了 `$lines`，内层收到的命令缺少变量名。

### Suggested Fix
复杂 PowerShell 7 命令用单引号包裹 `-Command '& { ... }'` 脚本块，或转义 `$`，避免外层 shell 抢先展开变量。

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: 后续读取指定行改用 `pwsh -NoLogo -NoProfile -Command '& { ... }'`。

---
---

## [ERR-20260610-001] content-guard-contract-raw-response-header

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: regression

### Summary
`stage33_content_guard_regression_check.py` failed because the template contract expected the old content guard result table headers without `原始响应`.

### Error
```text
AssertionError: 内容防护探针结果表头必须提供完整排障证据列
```

### Context
- `app/templates/content_guard.html` already exposed the `原始响应` column.
- Current project rules require content/health probe results to provide a raw upstream response summary or sample for troubleshooting.

### Suggested Fix
When adding a required troubleshooting column to a template, update the corresponding DOM contract assertion in the same change.

### Metadata
- Reproducible: yes
- Related Files: app/templates/content_guard.html, stage33_content_guard_regression_check.py

---

## [ERR-20260610-002] postgres-test-database-missing

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: test-environment

### Summary
PostgreSQL-backed stage checks failed locally because the configured test database did not exist.

### Error
```text
FATAL: database "aotu_gpt_test" does not exist
```

### Context
- `stage34_logging_system_regression_check.py`, `stage35_logging_usage_accuracy_regression_check.py`, and `stage36_ip_management_regression_check.py` attempted to connect to `127.0.0.1:5432/aotu_gpt_test`.
- The project is now PostgreSQL-only, so these checks require a prepared PostgreSQL test database.

### Suggested Fix
Create or provision `aotu_gpt_test` before running PostgreSQL-backed stage checks, or document the required local test database bootstrap command.

### Metadata
- Reproducible: yes
- Related Files: stage34_logging_system_regression_check.py, stage35_logging_usage_accuracy_regression_check.py, stage36_ip_management_regression_check.py

---

## [ERR-20260610-003] typed-logging-queue-contract-stale-symbol

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: regression

### Summary
`stage33_content_guard_regression_check.py` failed because the typed logging queue governance assertion still searched for the old `_record_worker_failure` symbol.

### Error
```text
AssertionError: 类型化日志队列批次失败必须有死信和失败计数
```

### Context
- `app/logging/queue.py` already records dead letters through `DEAD_LETTER_KEY`, `FAILURE_COUNT_KEY`, `_prepare_failed_processing_item`, and `_dead_letter_payload`.
- The implementation had been refactored, but the static regression assertion still depended on the old helper name.

### Suggested Fix
Static governance checks should assert the current failure-handling semantics and stable queue artifacts, not obsolete private helper names.

### Metadata
- Reproducible: yes
- Related Files: app/logging/queue.py, stage33_content_guard_regression_check.py

### Resolution
- **Resolved**: 2026-06-10T00:00:00+08:00
- **Notes**: Updated the stage33 assertion to check the current dead-letter preparation and payload helpers.

---

## [ERR-20260610-004] powershell-inline-variable-expanded

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
Inline `pwsh -Command` commands failed because the outer PowerShell parsed `$i` before PowerShell 7 received the script.

### Error
```text
ParserError: Missing expression after unary operator '++'.
```

### Context
- Attempted to run line-number printing commands containing `$i++` inside a double-quoted `pwsh -Command` string.
- The project requires PowerShell 7 and warns that complex commands containing `$` must be shortened, escaped, or moved into a script block.

### Suggested Fix
Wrap PowerShell 7 command bodies in single quotes or escape `$` variables when invoking `pwsh -NoLogo -NoProfile -Command` from an outer PowerShell shell.

### Metadata
- Reproducible: yes
- Related Files: 项目全局规范.md

---

## [ERR-20260610-005] ripgrep-windows-glob-not-expanded

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
`rg` failed on `app/templates/*.html` because the Windows shell did not expand the glob as expected.

### Error
```text
rg: app/templates/*.html: IO error for operation on app/templates/*.html: 文件名、目录名或卷标语法不正确。 (os error 123)
```

### Context
- The command tried to search HTML templates with a Bash-style path glob.
- Searching the directory directly (`rg ... app/templates`) worked.

### Suggested Fix
On Windows PowerShell, prefer passing directories to `rg` or use `Get-ChildItem` to enumerate files when a tool does not support the intended glob form.

### Metadata
- Reproducible: yes
- Related Files: app/templates

---

## [ERR-20260610-006] powershell-json-quotes-stripped

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
Inline Python cache checks failed because JSON double quotes were stripped by the outer PowerShell command string.

### Error
```text
JSONDecodeError: Expecting property name enclosed in double quotes
AssertionError: ('invalid_rules_json', 'invalid_rules_json')
```

### Context
- A Python here-string was embedded inside a double-quoted `pwsh -Command` argument.
- The JSON literal arrived in Python as `{ id:r1,... }` instead of valid JSON with quoted keys.

### Suggested Fix
For inline Python under PowerShell, build JSON inside Python with `json.dumps(...)` or avoid nesting raw JSON in an outer double-quoted command.

### Metadata
- Reproducible: yes
- Related Files: app/services/content_guard_rule_service.py

---

## [ERR-20260610-007] powershell-regex-backtick-parsing

**Logged**: 2026-06-10T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
An `rg` search command failed because a complex regex containing PowerShell backticks was embedded in an outer double-quoted `pwsh -Command` string.

### Error
```text
ParserError: Unexpected token ')' in expression or statement.
```

### Context
- The command attempted to search JavaScript `api.post(...)` patterns containing template-literal backticks.
- The outer PowerShell parser consumed the backtick semantics before `rg` received the regex.

### Suggested Fix
Use shorter searches for literal substrings first, or put complex regex patterns in single-quoted PowerShell command bodies/script blocks so template-literal backticks are not interpreted by the outer shell.

### Metadata
- Reproducible: yes
- Related Files: app/static/js/app.js

---

## [ERR-20260610-008] powershell-variable-expanded-before-pwsh

**Logged**: 2026-06-10T23:25:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
A nested `pwsh -Command` check failed because `$files` and `$f` were expanded by the outer PowerShell command string before PowerShell 7 received the script.

### Error
```text
ParserError: Missing variable name after foreach. The correct form is: foreach ($a in $b) {...}
```

### Context
- The command embedded a `foreach ($f in $files)` script inside an outer double-quoted command string.
- The outer shell stripped the variable names, so the inner command became `foreach ( in )`.

### Suggested Fix
Wrap nested PowerShell 7 script bodies in a single-quoted command body or script block, or move complex checks into a `.ps1` file before execution.

### Metadata
- Reproducible: yes
- Related Files: scripts/build_tencent_package.ps1
## [ERR-20260611-001] js_patch_parenthesis

**Logged**: 2026-06-11T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: frontend

### Summary
手工把 `Promise.all(items.map(...))` 改成 `for...of` 时留下多余闭合括号，导致 `node --check` 失败。

### Error
```text
SyntaxError: Unexpected token ')'
```

### Context
- Command: `node --check app/static/js/app.js`
- Related change: 同提供商模型批量测试由并发改串行。

### Suggested Fix
改复杂异步循环时分段替换，并立即运行 `node --check` 定位闭合结构问题。

### Metadata
- Reproducible: yes
- Related Files: app/static/js/app.js

### Resolution
- **Resolved**: 2026-06-11T00:00:00+08:00
- **Notes**: 后续补丁修正多余闭合括号并重新执行语法检查。

---

## [ERR-20260612-001] pwsh_outer_variable_expansion

**Logged**: 2026-06-12T11:57:54+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary
Nested PowerShell health-check command failed because `$` variables in the inner script were expanded by the outer shell.

### Error
```text
.Exception.Message: The term '.Exception.Message' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context
- Attempted command used `$r` and `$_.Exception.Message` inside a double-quoted `pwsh -Command` string.
- The outer PowerShell parsed those variables before the inner PowerShell received the script.

### Suggested Fix
Prefer short commands without `$` for simple checks, or wrap inner PowerShell scripts in single quotes / script blocks so the outer shell cannot expand variables.

### Metadata
- Reproducible: yes
- Related Files: `项目全局规范.md`

### Resolution
- **Resolved**: 2026-06-12T11:57:54+08:00
- **Notes**: Re-ran the health check with `Invoke-RestMethod ... | ConvertTo-Json -Compress`, avoiding outer-shell variable expansion.

---

## [ERR-20260611-003] pwsh_rg_pattern_pipe_parsing

**Logged**: 2026-06-11T23:58:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
An `rg` verification command failed because a complex pattern containing `|` and escaped quotes was not isolated enough from PowerShell parsing.

### Error
```text
The module 'data-tooltip-name=' could not be loaded.
```

### Context
- Command attempted to search multiple tooltip-related patterns in one `rg -n` invocation.
- The pattern mixed alternation, quotes, and backslashes inside nested `pwsh -Command`.

### Suggested Fix
Use single-quoted PowerShell script blocks with simpler fixed-string `rg -F` searches, or split each suspicious pattern into a separate command.

### Metadata
- Reproducible: yes
- Related Files: app/static/js/app.js, app/static/css/app.css
- See Also: ERR-20260611-002

---

## [ERR-20260611-004] isolated_router_service_import_cycle

**Logged**: 2026-06-11T23:40:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
Running a standalone Python snippet that imports `RouterService` directly can hit a circular import through `LogService -> ApiKeyService -> BillingService -> LogService`.

### Error
```text
ImportError: cannot import name 'LogService' from partially initialized module 'app.services.log_service'
```

### Suggested Fix
For ad hoc route investigations, prefer querying persisted `request_logs` diagnostics first, or import through an application-initialized path instead of isolated service imports.

### Metadata
- Reproducible: yes
- Related Files: app/services/router_service.py, app/services/log_service.py, app/services/billing_service.py

---

## [ERR-20260611-004] pwsh_here_string_nested_command

**Logged**: 2026-06-11T23:20:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary
PowerShell here-string piped to Python failed when embedded directly in a nested `pwsh -Command` string; the outer shell parsed Python lines as PowerShell.

### Error
```text
ParserError: The 'from' keyword is not supported in this version of the language.
```

### Context
- Attempted to run a temporary Python Jinja template parser.
- The command used a here-string inside a nested quoted `pwsh -Command`.

### Suggested Fix
Wrap the here-string pipeline in a PowerShell script block (`& { @' ... '@ | .\.venv\Scripts\python.exe - }`) when using nested `pwsh -Command`.

### Metadata
- Reproducible: yes
- Related Files: app/templates/dashboard.html

### Resolution
- **Resolved**: 2026-06-11T23:20:00+08:00
- **Notes**: Re-ran the parser through a script block and confirmed `dashboard.html` and `base.html` parse successfully.

---

## [ERR-20260611-003] browser_use_cli_missing

**Logged**: 2026-06-11T22:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
Visual verification could not run because the `browser-use` CLI is not installed or not on PATH in the current PowerShell environment.

### Error
```text
browser-use: The term 'browser-use' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context
- Attempted command: `browser-use open http://127.0.0.1:8000/logs`
- Local app was listening on `127.0.0.1:8000`, but browser automation was unavailable.

### Suggested Fix
Run `browser-use doctor` only after confirming the CLI is installed and on PATH, or use an available Playwright/browser tool for local visual verification.

### Metadata
- Reproducible: yes
- Related Files: app/templates/logs.html, app/static/css/app.css, app/static/js/app.js

---

## [ERR-20260611-002] rg_regex_escaped_in_pwsh

**Logged**: 2026-06-11T22:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
An `rg` search failed because a complex alternation regex with escaped quotes was embedded in a PowerShell command string and reached `rg` with invalid escapes.

### Error
```text
rg: regex parse error:
error: unrecognized escape sequence
```

### Context
- Command attempted to search several JavaScript literals in one `rg -n` regex.
- The query mixed regex alternation, escaped parentheses, escaped quotes, and Chinese text inside a nested `pwsh -Command`.

### Suggested Fix
Use multiple `rg -F` fixed-string searches or split complex searches into shorter commands before reaching for a combined regex in PowerShell.

### Metadata
- Reproducible: yes
- Related Files: app/static/js/app.js

---
