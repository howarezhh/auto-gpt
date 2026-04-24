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
