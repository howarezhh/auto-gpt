import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
REQUIREMENTS_FILE = ROOT / "requirements.txt"
DATA_DIR = ROOT / "data"
DEFAULT_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"


def run_command(command: list[str], cwd: Path | None = None) -> None:
    print(">>", " ".join(str(item) for item in command))
    subprocess.run(command, cwd=cwd or ROOT, check=True)


def ensure_env_file() -> None:
    if ENV_FILE.exists() or not ENV_EXAMPLE.exists():
        return
    shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
    print(f"Created {ENV_FILE.name} from {ENV_EXAMPLE.name}")


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(exist_ok=True)


def ensure_venv() -> Path:
    python_executable = VENV_DIR / "Scripts" / "python.exe"
    if not python_executable.exists():
        print("Virtual environment not found. Creating .venv ...")
        run_command([sys.executable, "-m", "venv", str(VENV_DIR)])
    return python_executable


def install_dependencies(python_executable: Path, index_url: str) -> None:
    run_command([str(python_executable), "-m", "pip", "install", "--upgrade", "pip", "-i", index_url])
    run_command([str(python_executable), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE), "-i", index_url])


def start_server(python_executable: Path, host: str, port: int, reload_enabled: bool) -> None:
    command = [str(python_executable), "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)]
    if reload_enabled:
        command.append("--reload")
    run_command(command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Single-file launcher for the aotu-gpt project.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for uvicorn. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="Port for uvicorn. Default: 8000")
    parser.add_argument(
        "--index-url",
        default=DEFAULT_INDEX_URL,
        help=f"Python package mirror URL. Default: {DEFAULT_INDEX_URL}",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="Disable uvicorn auto reload.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Only prepare venv, dependencies and config. Do not start the server.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_data_dir()
    ensure_env_file()
    python_executable = ensure_venv()
    install_dependencies(python_executable, args.index_url)

    if args.prepare_only:
        print("Environment preparation completed.")
        return

    start_server(
        python_executable=python_executable,
        host=args.host,
        port=args.port,
        reload_enabled=not args.no_reload,
    )


if __name__ == "__main__":
    main()
