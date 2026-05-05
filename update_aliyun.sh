#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements.txt"
REQUIREMENTS_HASH_FILE="$VENV_DIR/.requirements.sha256"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
SERVICE_NAME="${SERVICE_NAME:-aotu-gpt}"
NGINX_SERVICE_NAME="${NGINX_SERVICE_NAME:-nginx}"
BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/ready}"
GIT_FETCH_RETRIES="${GIT_FETCH_RETRIES:-2}"
GIT_FETCH_TIMEOUT_SECONDS="${GIT_FETCH_TIMEOUT_SECONDS:-120}"
GIT_MIRROR_URLS="${GIT_MIRROR_URLS:-}"

log() {
  printf '[aotu-gpt-update] %s\n' "$1"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run this script as root or with sudo."
    exit 1
  fi
}

enter_project_root() {
  cd "$PROJECT_ROOT"
}

require_git_repo() {
  if [[ ! -d "$PROJECT_ROOT/.git" ]]; then
    log "Project root is not a Git repository: ${PROJECT_ROOT}"
    exit 1
  fi
}

ensure_local_runtime_paths() {
  mkdir -p "$PROJECT_ROOT/data"
  if [[ ! -f "$PROJECT_ROOT/.env" && -f "$PROJECT_ROOT/.env.example" ]]; then
    log "Creating .env from .env.example because .env is missing."
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
  fi
}

protect_runtime_state() {
  git update-index --skip-worktree .env 2>/dev/null || true
  git rm -r --cached --ignore-unmatch data >/dev/null 2>&1 || true
}

backup_runtime_state() {
  local backup_dir="$1"
  mkdir -p "$backup_dir"
  if [[ -f "$PROJECT_ROOT/.env" ]]; then
    cp -a "$PROJECT_ROOT/.env" "$backup_dir/.env"
  fi
  if [[ -d "$PROJECT_ROOT/data" ]]; then
    cp -a "$PROJECT_ROOT/data" "$backup_dir/data"
  fi
}

restore_runtime_state() {
  local backup_dir="$1"
  if [[ -f "$backup_dir/.env" ]]; then
    cp -a "$backup_dir/.env" "$PROJECT_ROOT/.env"
  fi
  if [[ -d "$backup_dir/data" ]]; then
    rm -rf "$PROJECT_ROOT/data"
    cp -a "$backup_dir/data" "$PROJECT_ROOT/data"
  fi
}

ensure_clean_code_worktree() {
  local dirty=""
  dirty="$(git status --porcelain --untracked-files=no | grep -vE '^[[:space:]]*D[[:space:]]+data/' || true)"
  if [[ -n "$dirty" ]]; then
    log "Tracked code/config files have local modifications. Commit or discard them before updating:"
    printf '%s\n' "$dirty"
    exit 1
  fi
}

current_requirements_hash() {
  sha256sum "$REQUIREMENTS_FILE" | awk '{print $1}'
}

install_python_dependencies_if_needed() {
  local current_hash=""
  local installed_hash=""

  if [[ ! -x "$PYTHON_BIN" ]]; then
    log "Creating project virtual environment..."
    python3 -m venv "$VENV_DIR"
  fi

  if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
    log "requirements.txt not found, skipping Python dependency installation."
    return
  fi

  current_hash="$(current_requirements_hash)"
  if [[ -f "$REQUIREMENTS_HASH_FILE" ]]; then
    installed_hash="$(cat "$REQUIREMENTS_HASH_FILE")"
  fi

  if [[ -n "$installed_hash" && "$installed_hash" == "$current_hash" ]]; then
    log "Python dependencies already match requirements.txt, skipping."
    return
  fi

  log "Installing Python dependencies with mirror: ${PIP_INDEX_URL}"
  "$PYTHON_BIN" -m pip install --upgrade pip -i "$PIP_INDEX_URL"
  "$PYTHON_BIN" -m pip install -r "$REQUIREMENTS_FILE" -i "$PIP_INDEX_URL"
  printf '%s' "$current_hash" > "$REQUIREMENTS_HASH_FILE"
}

remote_fetch_url() {
  git remote get-url "$REMOTE" 2>/dev/null || printf '%s' "$REMOTE"
}

github_repo_path_from_url() {
  local url="$1"
  local path=""
  case "$url" in
    https://github.com/*)
      path="${url#https://github.com/}"
      ;;
    http://github.com/*)
      path="${url#http://github.com/}"
      ;;
    git@github.com:*)
      path="${url#git@github.com:}"
      ;;
    ssh://git@github.com/*)
      path="${url#ssh://git@github.com/}"
      ;;
    *)
      return 1
      ;;
  esac
  [[ -n "$path" ]] || return 1
  [[ "$path" == *.git ]] || path="${path}.git"
  printf '%s' "$path"
}

append_unique_fetch_url() {
  local url="$1"
  local existing=""
  [[ -n "$url" ]] || return 0
  for existing in "${FETCH_URL_CANDIDATES[@]}"; do
    if [[ "$existing" == "$url" ]]; then
      return 0
    fi
  done
  FETCH_URL_CANDIDATES+=("$url")
}

build_fetch_url_candidates() {
  local primary_url="$1"
  local repo_path=""
  local mirror_url=""
  FETCH_URL_CANDIDATES=()
  append_unique_fetch_url "$primary_url"

  if repo_path="$(github_repo_path_from_url "$primary_url")"; then
    append_unique_fetch_url "https://gitclone.com/github.com/${repo_path}"
    append_unique_fetch_url "https://gh-proxy.com/https://github.com/${repo_path}"
    append_unique_fetch_url "https://mirror.ghproxy.com/https://github.com/${repo_path}"
  fi

  for mirror_url in $GIT_MIRROR_URLS; do
    append_unique_fetch_url "$mirror_url"
  done
}

run_git_fetch_candidate() {
  local fetch_url="$1"
  if command -v timeout >/dev/null 2>&1; then
    timeout "${GIT_FETCH_TIMEOUT_SECONDS}s" git fetch --prune "$fetch_url" "+refs/heads/${BRANCH}:refs/remotes/${REMOTE}/${BRANCH}"
    return $?
  fi
  git fetch --prune "$fetch_url" "+refs/heads/${BRANCH}:refs/remotes/${REMOTE}/${BRANCH}"
}

fetch_latest_code() {
  local primary_url=""
  local fetch_url=""
  local attempt=1
  local max_attempts=1

  primary_url="$(remote_fetch_url)"
  build_fetch_url_candidates "$primary_url"
  max_attempts="$GIT_FETCH_RETRIES"
  if (( max_attempts < 1 )); then
    max_attempts=1
  fi

  for fetch_url in "${FETCH_URL_CANDIDATES[@]}"; do
    attempt=1
    while (( attempt <= max_attempts )); do
      log "Fetching ${BRANCH} via ${fetch_url} (${attempt}/${max_attempts})..."
      if run_git_fetch_candidate "$fetch_url"; then
        log "Fetch succeeded via ${fetch_url}."
        return 0
      fi
      log "Fetch failed via ${fetch_url} (${attempt}/${max_attempts})."
      attempt=$((attempt + 1))
      sleep 2
    done
  done

  log "Unable to fetch ${BRANCH} from ${REMOTE} or configured mirrors."
  log "You can override mirrors, for example:"
  log "  sudo env GIT_MIRROR_URLS='https://gitclone.com/github.com/howarezhh/auto-gpt.git' bash update_aliyun.sh"
  return 1
}

update_code() {
  local backup_dir=""
  log "Fetching latest code for ${REMOTE}/${BRANCH}..."
  fetch_latest_code

  local local_head=""
  local remote_head=""
  local_head="$(git rev-parse HEAD)"
  remote_head="$(git rev-parse "${REMOTE}/${BRANCH}")"

  if [[ "$local_head" == "$remote_head" ]]; then
    log "Code is already up to date."
    return
  fi

  log "Updating code to ${REMOTE}/${BRANCH}."
  backup_dir="$(mktemp -d)"
  backup_runtime_state "$backup_dir"
  git reset --hard "${REMOTE}/${BRANCH}"
  restore_runtime_state "$backup_dir"
  rm -rf "$backup_dir"
  ensure_local_runtime_paths
  protect_runtime_state
}

refresh_service_files() {
  if [[ -f "$PROJECT_ROOT/start_aliyun.sh" ]]; then
    log "Refreshing systemd and nginx configuration without database initialization."
    RUN_DB_INIT=0 bash "$PROJECT_ROOT/start_aliyun.sh"
    return
  fi

  log "start_aliyun.sh not found, restarting existing services only."
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME"
  systemctl reload "$NGINX_SERVICE_NAME" || systemctl restart "$NGINX_SERVICE_NAME"
}

wait_for_health() {
  local attempt=1
  while (( attempt <= 20 )); do
    if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null; then
      log "Health check passed: ${HEALTH_URL}"
      return 0
    fi
    log "Health check pending (${attempt}/20), retrying in 1s..."
    sleep 1
    attempt=$((attempt + 1))
  done

  log "Health check failed: ${HEALTH_URL}"
  systemctl --no-pager --full status "$SERVICE_NAME" || true
  exit 1
}

print_summary() {
  cat <<EOF

Update completed.

Project root: ${PROJECT_ROOT}
Git HEAD: $(git rev-parse --short HEAD)
Service: ${SERVICE_NAME}
Health URL: ${HEALTH_URL}

Runtime data kept local:
  ${PROJECT_ROOT}/.env
  ${PROJECT_ROOT}/data/

EOF
}

main() {
  require_root
  enter_project_root
  require_git_repo
  ensure_local_runtime_paths
  protect_runtime_state
  ensure_clean_code_worktree
  update_code
  install_python_dependencies_if_needed
  refresh_service_files
  wait_for_health
  print_summary
}

main "$@"
