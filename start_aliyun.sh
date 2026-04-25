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
METADATA_BASE_URL="http://100.100.100.200/latest/meta-data"
SYSTEM_PACKAGES=(python3 python3-venv python3-pip nginx curl ca-certificates)

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
  local missing_packages=()
  local package=""

  for package in "${SYSTEM_PACKAGES[@]}"; do
    if dpkg -s "$package" >/dev/null 2>&1; then
      continue
    fi
    missing_packages+=("$package")
  done

  if [[ ${#missing_packages[@]} -eq 0 ]]; then
    log "Required system packages already installed, skipping."
    return
  fi

  log "Installing missing system packages: ${missing_packages[*]}"
  apt-get update
  apt-get install -y "${missing_packages[@]}"
}

ensure_venv() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    log "Creating project virtual environment..."
    python3 -m venv "$VENV_DIR"
  else
    log "Project virtual environment already exists, skipping."
  fi
}

ensure_data_dir() {
  mkdir -p "$PROJECT_ROOT/data"
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

get_public_ip() {
  local ip=""

  if command -v curl >/dev/null 2>&1; then
    ip="$(curl -fsS --max-time 2 "${METADATA_BASE_URL}/eipv4" 2>/dev/null || true)"
    if [[ -n "$ip" ]]; then
      printf '%s' "$ip"
      return
    fi

    ip="$(curl -fsS --max-time 2 "${METADATA_BASE_URL}/public-ipv4" 2>/dev/null || true)"
    if [[ -n "$ip" ]]; then
      printf '%s' "$ip"
      return
    fi
  fi
}

ensure_external_base_url() {
  local current_value=""
  local public_ip=""

  current_value="$(get_env_value "EXTERNAL_BASE_URL")"
  if [[ -n "$current_value" ]]; then
    return
  fi

  public_ip="$(get_public_ip)"
  if [[ -z "$public_ip" ]]; then
    log "EXTERNAL_BASE_URL is empty and public IP auto-detection failed, keeping it unchanged."
    return
  fi

  set_env_value "EXTERNAL_BASE_URL" "http://${public_ip}"
  log "Detected public IP and wrote EXTERNAL_BASE_URL=http://${public_ip} into .env"
}

extract_server_name_from_url() {
  local url="$1"
  local without_scheme=""
  local host_port=""
  local host_only=""

  without_scheme="${url#http://}"
  without_scheme="${without_scheme#https://}"
  host_port="${without_scheme%%/*}"
  host_only="${host_port%%:*}"

  if [[ -n "$host_only" ]]; then
    printf '%s' "$host_only"
    return
  fi

  printf '_'
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
  ensure_external_base_url

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
  local external_base_url=""
  local server_name="_"

  external_base_url="$(get_env_value "EXTERNAL_BASE_URL")"
  if [[ -n "$external_base_url" ]]; then
    server_name="$(extract_server_name_from_url "$external_base_url")"
  fi

  log "Writing nginx site: ${site_file}"
  cat > "$site_file" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${server_name};

    client_max_body_size 20m;

    location / {
        proxy_pass http://${APP_HOST}:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
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
  nginx -t
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  systemctl enable nginx
  systemctl restart nginx
}

run_health_checks() {
  local local_url="http://${APP_HOST}:${APP_PORT}/login"
  local external_base_url=""
  local external_url=""

  if curl -fsS --max-time 10 "$local_url" >/dev/null; then
    log "Local health check passed: ${local_url}"
  else
    log "Local health check failed: ${local_url}"
    systemctl --no-pager --full status "$SERVICE_NAME" || true
    exit 1
  fi

  external_base_url="$(get_env_value "EXTERNAL_BASE_URL")"
  if [[ -z "$external_base_url" ]]; then
    return
  fi

  external_url="${external_base_url%/}/login"
  if curl -fsS --max-time 10 "$external_url" >/dev/null; then
    log "External health check passed: ${external_url}"
  else
    log "External health check failed: ${external_url}"
    log "This usually means the ECS security group, firewall, or public network mapping is still blocking port 80."
  fi
}

ensure_firewall_port() {
  if ! command -v ufw >/dev/null 2>&1; then
    return
  fi

  if ! ufw status | grep -q "Status: active"; then
    return
  fi

  if ufw status | grep -qE '(^| )80/tcp( |$)|Nginx HTTP'; then
    log "UFW already allows HTTP traffic, skipping."
    return
  fi

  log "Allowing TCP port 80 through UFW..."
  ufw allow 80/tcp
}

print_summary() {
  local proxy_key
  local external_base_url=""
  proxy_key="$(get_env_value "LOCAL_PROXY_API_KEY")"
  external_base_url="$(get_env_value "EXTERNAL_BASE_URL")"
  cat <<EOF

Deployment completed.

Project root: ${PROJECT_ROOT}
Local app URL: http://${APP_HOST}:${APP_PORT}/
Public app URL: ${external_base_url:-Not set}
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
  ensure_data_dir
  ensure_venv
  install_python_dependencies
  ensure_env_file
  write_systemd_service
  write_nginx_site
  reload_and_start_services
  ensure_firewall_port
  run_health_checks
  print_summary
}

main "$@"
