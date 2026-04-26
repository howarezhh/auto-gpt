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

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## 环境变量说明

- `EXTERNAL_BASE_URL`：外部接入文档使用的统一地址，例如 `https://api.example.com`；若为空，文档页会回退到当前访问地址。
- `APP_ENV`：当值为 `prod` 或 `production` 时，`SESSION_SECRET_KEY` 与 `API_KEY_ENCRYPTION_SECRET` 必须替换为至少 32 位的非默认高强度随机值，否则应用会拒绝启动。
- 当前对外提供的是 OpenAI 兼容子集，而不是完整官方能力面；正式支持的路径仅为 `/v1/chat/completions`、`/v1/responses`、`/v1/models`。
- `/v1/responses` 当前对图片输入走适配子集。支持透传字段：`model`、`instructions`、`input`、`temperature`、`top_p`、`presence_penalty`、`frequency_penalty`、`tools`、`tool_choice`、`response_format`、`stream`、`user`、`metadata`、`seed`、`max_output_tokens`、`max_tokens`。当前不适配并会直接返回 `400` 的字段包括：`previous_response_id`、`parallel_tool_calls`、`reasoning`、`reasoning_effort`、`store`、`text`、`include`、`max_tool_calls`、`truncation`、`background`。

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

- 检查并安装缺失的系统依赖：`python3`、`python3-venv`、`python3-pip`、`nginx`、`curl`、`ca-certificates`
- 在项目根目录创建或复用 `.venv`
- 使用清华源安装 Python 依赖
- 自动创建 `data/` 目录
- 创建或更新 `.env`
- 自动生成生产环境所需的 `SESSION_SECRET_KEY` 与 `API_KEY_ENCRYPTION_SECRET`（若为空、仍为默认占位值或长度不足）
- 当 `EXTERNAL_BASE_URL` 为空时，尝试根据阿里云元数据自动写入公网地址
- 自动生成 `LOCAL_PROXY_API_KEY`（若当前为空）
- 写入 `systemd` 服务 `aotu-gpt.service`
- 写入 `nginx` 站点配置并启用
- 若服务器启用了 `ufw`，自动放行 `80/TCP`
- 设置后端与 `nginx` 开机自启并立即启动
- 执行本机和公网健康检查；已完成的依赖安装、虚拟环境创建、Python 依赖安装会自动跳过

部署完成后，仍需在阿里云安全组放行 `80/TCP` 到 `0.0.0.0/0`。

若需要让“使用文档”页面固定展示公网地址，部署完成后再把 `.env` 中的 `EXTERNAL_BASE_URL` 改成你的公网地址，例如：

```bash
EXTERNAL_BASE_URL=http://114.55.144.46
```

部署完成后可手动复查：

```bash
sudo systemctl status aotu-gpt
sudo systemctl status nginx
curl http://127.0.0.1:8000/login
curl http://114.55.144.46/login
```
