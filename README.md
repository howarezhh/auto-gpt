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
- 当前正式对外仅支持 `/v1/chat/completions`、`/v1/responses`、`/v1/models`。

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

- 安装 `python3`、`python3-venv`、`python3-pip`、`nginx`
- 在项目根目录创建或复用 `.venv`
- 使用清华源安装 Python 依赖
- 创建或更新 `.env`
- 自动生成 `LOCAL_PROXY_API_KEY`（若当前为空）
- 写入 `systemd` 服务 `aotu-gpt.service`
- 写入 `nginx` 站点配置并启用
- 设置后端与 `nginx` 开机自启并立即启动

部署完成后，仍需在阿里云安全组放行 `80/TCP` 到 `0.0.0.0/0`。
