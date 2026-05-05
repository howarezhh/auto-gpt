# aotu-gpt

基于 FastAPI 的个人多中转站统一管理与代理系统。

## 虚拟环境与国内镜像

```powershell
cd D:\AI知识学习方面\AI大模型智能体全套教程\aotu-gpt
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 启动

Windows 本地开发可继续使用 Uvicorn 或 `run.ps1`，该方式只用于本地开发调试，不作为生产并发启动方式。

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## 环境变量说明

- `EXTERNAL_BASE_URL`：外部接入文档使用的统一地址，例如 `https://api.example.com`；若为空，文档页会回退到当前访问地址。
- `APP_ENV`：当值为 `prod` 或 `production` 时，`SESSION_SECRET_KEY` 与 `API_KEY_ENCRYPTION_SECRET` 必须替换为至少 32 位的非默认高强度随机值，否则应用会拒绝启动。
- `ENABLE_STARTUP_DB_INIT`：仅控制非生产环境 Web worker 是否自动初始化数据库；生产环境 Web worker 永不执行建表或迁移，部署时应先独立执行 `python scripts/run_startup_db_init.py`，再启动 Gunicorn 服务。
- 当前对外提供的是 OpenAI 兼容入口，而不是 OpenAI 全量官方产品面；正式开放的路径为 `/v1/chat/completions`、`/v1/responses`、`/v1/responses/{response_id}`、`/v1/responses/{response_id}/cancel`、`/v1/models`。
- 对外代理支持把 `model_reasoning_effort` 作为统一别名接入：`/v1/chat/completions` 会归一化为 `reasoning_effort`，`/v1/responses` 会归一化为 `reasoning.effort`，并在请求日志的 `model_reasoning_effort` 字段中留痕。
- `/v1/responses` 请求会优先按原始 Responses 协议透传到命中的上游；若上游明确返回端点或模型兼容性错误，平台仅对简单文本、普通图片和基础流式请求尝试一次安全互转 fallback。只有进入 fallback 的请求才会收窄到适配子集；包含 `tools`、状态上下文、`reasoning`、结构化输出选项或复杂多模态内容的请求不会做有损互转。

## 单文件启动

```powershell
cd D:\AI知识学习方面\AI大模型智能体全套教程\aotu-gpt
python start_project.py
```

可选参数：

```powershell
python start_project.py --host 0.0.0.0 --port 9000
python start_project.py --prepare-only
python start_project.py --no-reload
```

## 阿里云 Ubuntu 一键部署

适用于已将项目放到 Linux 服务器后的部署场景，例如 `/opt/auto-gpt`。

```bash
cd /opt/auto-gpt
chmod +x start_aliyun.sh
sudo ./start_aliyun.sh
```

脚本会自动完成以下操作：

- 检查并安装缺失的系统依赖：`python3`、`python3-venv`、`python3-pip`、`nginx`、`curl`、`ca-certificates`、`postgresql`、`postgresql-contrib`
- 在项目根目录创建或复用 `.venv`
- 使用清华源安装 Python 依赖
- 自动创建 `data/` 目录
- 创建或更新 `.env`
- 自动写入 PostgreSQL 连接配置和 Gunicorn 多 worker 配置
- 自动创建 PostgreSQL 数据库 `aotu_gpt`、用户 `aotu_gpt`，默认密码为 `zhh123456`
- 自动生成生产环境所需的 `SESSION_SECRET_KEY` 与 `API_KEY_ENCRYPTION_SECRET`（若为空、仍为默认占位值或长度不足）
- 当 `EXTERNAL_BASE_URL` 为空时，尝试根据阿里云元数据自动写入公网地址
- 自动生成 `LOCAL_PROXY_API_KEY`（若当前为空）
- 写入 `systemd` 服务 `aotu-gpt.service`
- 写入 `nginx` 站点配置并启用
- 若服务器启用了 `ufw`，自动放行 `80/TCP`
- 设置后端与 `nginx` 开机自启并立即启动
- 执行本机和公网健康检查；已完成的依赖安装、虚拟环境创建、Python 依赖安装会自动跳过

部署完成后，仍需在阿里云安全组放行 `80/TCP` 到 `0.0.0.0/0`。

生产环境由 `start_aliyun.sh` 写入 `systemd` 并使用 `Gunicorn + UvicornWorker` 多 worker 启动，默认 `WEB_CONCURRENCY=4`、`GUNICORN_TIMEOUT=120`、`GUNICORN_KEEPALIVE=75`、`GUNICORN_GRACEFUL_TIMEOUT=30`。Nginx 继续反向代理到 `127.0.0.1:8000`，并保持 `proxy_buffering off`、`proxy_request_buffering off`、`proxy_read_timeout 600s`、`proxy_send_timeout 600s`，避免 SSE 流式响应被缓冲。

上游连接池按 1000 活跃请求目标预留初始值：`REQUEST_TIMEOUT_MS=60000`、`UPSTREAM_MAX_CONNECTIONS=1200`、`UPSTREAM_MAX_KEEPALIVE_CONNECTIONS=300`、`UPSTREAM_POOL_TIMEOUT_S=10`。后台“中转站”配置支持按 provider 设置最大活跃请求、最大流式请求、QPS、失败率上限和首 Token 超时；provider 活跃容量与 QPS 计数已优先使用 Redis 共享状态，避免 Gunicorn 多 worker 下只按进程内存计数。

Redis 用于生产实时并发计数、租约释放和短窗口 QPS/RPM 限流。默认 `REDIS_URL=redis://127.0.0.1:6379/0`，并发初始值为 `GLOBAL_MAX_ACTIVE_REQUESTS=1000`、`GLOBAL_MAX_ACTIVE_STREAMS=1000`、`API_KEY_MAX_ACTIVE_REQUESTS=1000`、`API_KEY_MAX_ACTIVE_STREAMS=1000`、`ACCOUNT_MAX_ACTIVE_REQUESTS=1000`、`ACCOUNT_MAX_ACTIVE_STREAMS=1000`、`PROVIDER_MAX_ACTIVE_REQUESTS=1000`、`PROVIDER_MAX_ACTIVE_STREAMS=1000`。这些值可通过 `.env` 初始化，并可在后台“系统设置”中调整；已有数据库中的存量系统设置和 provider 配置不会被启动脚本强制覆盖，需要在后台按实际资源容量调整。

API Key 鉴权结果会按 Bearer Key 的 SHA256 哈希写入 Redis 短 TTL 缓存，默认 `API_KEY_AUTH_CACHE_TTL_SECONDS=60`，用于减少入口重复数据库读取。缓存不保存原始明文 Key；API Key 编辑、启停、轮换、删除、批量授权更新，以及用户账户启停、额度和余额调整会主动清理受影响缓存。

日志、Token 和计费采用后台化处理：主请求链路同步写入 `request_logs` 核心字段，Token 回填、费用计算、余额扣减和账单记录由后台任务补齐。生产默认建议关闭完整 payload 与流式响应持久化，仅保留核心日志字段和必要的失败样本。

SSE 流式请求使用独立流式并发租约，默认 `GLOBAL_MAX_ACTIVE_STREAMS=1000`，并通过 `STREAM_CONNECT_TIMEOUT_SECONDS=10`、`STREAM_FIRST_TOKEN_TIMEOUT_SECONDS=60`、`STREAM_IDLE_TIMEOUT_SECONDS=120`、`STREAM_MAX_DURATION_SECONDS=600` 控制连接上游、首 Token、空闲 chunk 和最长持续时间。流式日志只在结束、取消或异常时写一条摘要，客户端取消记录 499 且不计 provider 失败。

监控与告警入口包括 `/live`、`/ready`、`/health`、`/metrics` 和后台 `/api/metrics/system`。`/ready` 会检查数据库、Redis、全局活跃请求、全局流式请求、数据库连接池和后台 Token/计费任务积压；后台“告警中心”会展示核心健康指标，并把 Redis 不可用、数据库不可用、5xx/429 异常、provider 失败率过高、后台任务积压、Token/计费失败写入告警事件流。

若需要让“使用文档”页面固定展示公网地址，部署完成后再把 `.env` 中的 `EXTERNAL_BASE_URL` 改成你的公网地址，例如：

```bash
EXTERNAL_BASE_URL=http://114.55.144.46
```

部署完成后可手动复查：

```bash
sudo systemctl status aotu-gpt
sudo systemctl status nginx
ps -ef | grep '[g]unicorn'
curl http://127.0.0.1:8000/ready
curl http://114.55.144.46/login
```

如果阿里云服务器直连 GitHub 不稳定，`update_aliyun.sh` 会先尝试原 `origin`，失败后自动尝试 GitHub 加速镜像。也可以手动指定镜像列表：

```bash
cd /opt/auto-gpt
sudo env GIT_MIRROR_URLS="https://gitclone.com/github.com/howarezhh/auto-gpt.git" bash update_aliyun.sh
```
