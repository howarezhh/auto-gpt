#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements.txt"
REQUIREMENTS_HASH_FILE="$VENV_DIR/.requirements.sha256"
SERVICE_NAME="aotu-gpt"
NGINX_SITE_NAME="aotu-gpt"
PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
APP_HOST="127.0.0.1"
APP_PORT="8000"

log() {
  printf '[aotu-gpt] %s\n' "$1"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run this script as root or with sudo."
    exit 1
  fi
}

install_system_packages() {
  log "Installing system packages..."
  apt-get update
  apt-get install -y python3 python3-venv python3-pip nginx
}

ensure_venv() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    log "Creating project virtual environment..."
    python3 -m venv "$VENV_DIR"
  fi
}

current_requirements_hash() {
  sha256sum "$REQUIREMENTS_FILE" | awk '{print $1}'
}

install_python_dependencies() {
  local current_hash=""
  local installed_hash=""

  if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
    log "requirements.txt not found, skipping Python dependency installation."
    return
  fi

  current_hash="$(current_requirements_hash)"
  if [[ -f "$REQUIREMENTS_HASH_FILE" ]]; then
    installed_hash="$(cat "$REQUIREMENTS_HASH_FILE")"
  fi

  if [[ -n "$installed_hash" && "$installed_hash" == "$current_hash" ]]; then
    log "Python dependencies already installed for current requirements.txt, skipping."
    return
  fi

  log "Installing Python dependencies..."
  "$PYTHON_BIN" -m pip install --upgrade pip -i "$PIP_INDEX_URL"
  "$PYTHON_BIN" -m pip install -r "$REQUIREMENTS_FILE" -i "$PIP_INDEX_URL"
  printf '%s' "$current_hash" > "$REQUIREMENTS_HASH_FILE"
}

generate_local_proxy_api_key() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

generate_runtime_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

get_env_value() {
  local key="$1"
  if grep -q "^${key}=" "$ENV_FILE"; then
    grep "^${key}=" "$ENV_FILE" | head -n1 | cut -d'=' -f2-
  fi
}

ensure_strong_secret() {
  local key="$1"
  local placeholder="$2"
  local current_value=""

  current_value="$(get_env_value "$key")"
  if [[ -n "$current_value" && "$current_value" != "$placeholder" && ${#current_value} -ge 32 ]]; then
    return
  fi

  local generated_secret=""
  generated_secret="$(generate_runtime_secret)"
  set_env_value "$key" "$generated_secret"
  log "Generated ${key} and wrote it into .env"
}

ensure_env_file() {
  if [[ ! -f "$ENV_FILE" && -f "$ENV_EXAMPLE" ]]; then
    log "Creating .env from .env.example..."
    cp "$ENV_EXAMPLE" "$ENV_FILE"
  elif [[ ! -f "$ENV_FILE" ]]; then
    log "Creating new .env..."
    touch "$ENV_FILE"
  fi

  set_env_value "APP_ENV" "prod"
  set_env_value "APP_HOST" "$APP_HOST"
  set_env_value "APP_PORT" "$APP_PORT"
  set_env_value "PIP_INDEX_URL" "$PIP_INDEX_URL"
  ensure_strong_secret "SESSION_SECRET_KEY" "change-this-session-secret"
  ensure_strong_secret "API_KEY_ENCRYPTION_SECRET" "change-this-api-key-encryption-secret"

  local current_key=""
  current_key="$(get_env_value "LOCAL_PROXY_API_KEY")"

  if [[ -z "$current_key" ]]; then
    local generated_key
    generated_key="$(generate_local_proxy_api_key)"
    set_env_value "LOCAL_PROXY_API_KEY" "$generated_key"
    log "Generated LOCAL_PROXY_API_KEY and wrote it into .env"
  fi
}

write_systemd_service() {
  local service_file="/etc/systemd/system/${SERVICE_NAME}.service"
  log "Writing systemd service: ${service_file}"
  cat > "$service_file" <<EOF
[Unit]
Description=aotu-gpt FastAPI Service
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=${PROJECT_ROOT}
EnvironmentFile=${ENV_FILE}
ExecStart=${PYTHON_BIN} -m uvicorn app.main:app --host ${APP_HOST} --port ${APP_PORT}
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
}

write_nginx_site() {
  local site_file="/etc/nginx/sites-available/${NGINX_SITE_NAME}"
  log "Writing nginx site: ${site_file}"
  cat > "$site_file" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name _;

    client_max_body_size 20m;

    location / {
        proxy_pass http://${APP_HOST}:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
EOF

  rm -f /etc/nginx/sites-enabled/default
  ln -sf "$site_file" "/etc/nginx/sites-enabled/${NGINX_SITE_NAME}"
}

reload_and_start_services() {
  log "Reloading systemd and starting services..."
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  nginx -t
  systemctl enable nginx
  systemctl restart nginx
}

print_summary() {
  local proxy_key
  proxy_key="$(get_env_value "LOCAL_PROXY_API_KEY")"
  cat <<EOF

Deployment completed.

Project root: ${PROJECT_ROOT}
Local app URL: http://${APP_HOST}:${APP_PORT}/
systemd service: ${SERVICE_NAME}
nginx site: ${NGINX_SITE_NAME}
LOCAL_PROXY_API_KEY: ${proxy_key}

Remember to open ECS security group TCP port 80 to 0.0.0.0/0.
Useful commands:
  systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
  systemctl status nginx
EOF
}

main() {
  require_root
  install_system_packages
  ensure_venv
  install_python_dependencies
  ensure_env_file
  write_systemd_service
  write_nginx_site
  reload_and_start_services
  print_summary
}

main "$@"
