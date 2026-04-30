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
SYSTEM_PACKAGES=(python3 python3-venv python3-pip nginx curl ca-certificates postgresql postgresql-contrib redis-server)
POSTGRES_DB="aotu_gpt"
POSTGRES_USER="aotu_gpt"
POSTGRES_PASSWORD="zhh123456"
POSTGRES_HOST="127.0.0.1"
POSTGRES_PORT="5432"
DEFAULT_DATABASE_URL="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
DEFAULT_REDIS_URL="redis://127.0.0.1:6379/0"
DEFAULT_API_KEY_AUTH_CACHE_TTL_SECONDS="60"
DEFAULT_CONCURRENCY_LEASE_TTL_SECONDS="900"
DEFAULT_GLOBAL_MAX_ACTIVE_REQUESTS="1000"
DEFAULT_GLOBAL_MAX_ACTIVE_STREAMS="300"
DEFAULT_API_KEY_MAX_ACTIVE_REQUESTS="50"
DEFAULT_API_KEY_MAX_ACTIVE_STREAMS="10"
DEFAULT_ACCOUNT_MAX_ACTIVE_REQUESTS="100"
DEFAULT_ACCOUNT_MAX_ACTIVE_STREAMS="20"
DEFAULT_PROVIDER_MAX_ACTIVE_REQUESTS="300"
DEFAULT_PROVIDER_MAX_ACTIVE_STREAMS="150"
DEFAULT_WEB_CONCURRENCY="4"
DEFAULT_GUNICORN_TIMEOUT="120"
DEFAULT_GUNICORN_KEEPALIVE="75"
DEFAULT_GUNICORN_GRACEFUL_TIMEOUT="30"
DEFAULT_REQUEST_TIMEOUT_MS="60000"
DEFAULT_STREAM_CONNECT_TIMEOUT_SECONDS="10"
DEFAULT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS="60"
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS="120"
DEFAULT_STREAM_MAX_DURATION_SECONDS="600"
DEFAULT_UPSTREAM_MAX_CONNECTIONS="1200"
DEFAULT_UPSTREAM_MAX_KEEPALIVE_CONNECTIONS="300"
DEFAULT_UPSTREAM_POOL_TIMEOUT_S="10"
RUN_DB_INIT="${RUN_DB_INIT:-0}"

log() {
  printf '[aotu-gpt] %s\n' "$1"
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

validate_project_files() {
  local missing_files=()
  local file=""
  local required_files=(
    "$REQUIREMENTS_FILE"
    "$PROJECT_ROOT/app/main.py"
    "$PROJECT_ROOT/scripts/run_startup_db_init.py"
  )

  for file in "${required_files[@]}"; do
    if [[ ! -f "$file" ]]; then
      missing_files+=("$file")
    fi
  done

  if [[ ${#missing_files[@]} -gt 0 ]]; then
    log "Required project files are missing: ${missing_files[*]}"
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
  "$PYTHON_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

generate_runtime_secret() {
  "$PYTHON_BIN" - <<'PY'
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

set_env_default() {
  local key="$1"
  local value="$2"
  local current_value=""

  current_value="$(get_env_value "$key")"
  if [[ -z "$current_value" ]]; then
    set_env_value "$key" "$value"
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

ensure_database_url() {
  local current_value=""
  current_value="$(get_env_value "DATABASE_URL")"

  if [[ -z "$current_value" || "$current_value" == sqlite* || "$current_value" == SQLITE* ]]; then
    set_env_value "DATABASE_URL" "$DEFAULT_DATABASE_URL"
    log "Wrote PostgreSQL DATABASE_URL into .env"
  fi

  set_env_default "DB_POOL_SIZE" "30"
  set_env_default "DB_MAX_OVERFLOW" "70"
  set_env_default "DB_POOL_TIMEOUT" "5"
  set_env_default "DB_POOL_RECYCLE" "1800"
}

ensure_redis_settings() {
  local redis_url=""
  redis_url="$(get_env_value "REDIS_URL")"

  if [[ -z "$redis_url" ]]; then
    set_env_value "REDIS_URL" "$DEFAULT_REDIS_URL"
  fi

  set_env_default "API_KEY_AUTH_CACHE_TTL_SECONDS" "$DEFAULT_API_KEY_AUTH_CACHE_TTL_SECONDS"
  set_env_default "CONCURRENCY_LEASE_TTL_SECONDS" "$DEFAULT_CONCURRENCY_LEASE_TTL_SECONDS"
  set_env_default "GLOBAL_MAX_ACTIVE_REQUESTS" "$DEFAULT_GLOBAL_MAX_ACTIVE_REQUESTS"
  set_env_default "GLOBAL_MAX_ACTIVE_STREAMS" "$DEFAULT_GLOBAL_MAX_ACTIVE_STREAMS"
  set_env_default "API_KEY_MAX_ACTIVE_REQUESTS" "$DEFAULT_API_KEY_MAX_ACTIVE_REQUESTS"
  set_env_default "API_KEY_MAX_ACTIVE_STREAMS" "$DEFAULT_API_KEY_MAX_ACTIVE_STREAMS"
  set_env_default "ACCOUNT_MAX_ACTIVE_REQUESTS" "$DEFAULT_ACCOUNT_MAX_ACTIVE_REQUESTS"
  set_env_default "ACCOUNT_MAX_ACTIVE_STREAMS" "$DEFAULT_ACCOUNT_MAX_ACTIVE_STREAMS"
  set_env_default "PROVIDER_MAX_ACTIVE_REQUESTS" "$DEFAULT_PROVIDER_MAX_ACTIVE_REQUESTS"
  set_env_default "PROVIDER_MAX_ACTIVE_STREAMS" "$DEFAULT_PROVIDER_MAX_ACTIVE_STREAMS"
}

ensure_gunicorn_settings() {
  local web_concurrency=""
  local gunicorn_timeout=""
  local gunicorn_keepalive=""
  local gunicorn_graceful_timeout=""

  web_concurrency="$(get_env_value "WEB_CONCURRENCY")"
  gunicorn_timeout="$(get_env_value "GUNICORN_TIMEOUT")"
  gunicorn_keepalive="$(get_env_value "GUNICORN_KEEPALIVE")"
  gunicorn_graceful_timeout="$(get_env_value "GUNICORN_GRACEFUL_TIMEOUT")"

  if [[ -z "$web_concurrency" ]]; then
    set_env_value "WEB_CONCURRENCY" "$DEFAULT_WEB_CONCURRENCY"
  fi
  if [[ -z "$gunicorn_timeout" ]]; then
    set_env_value "GUNICORN_TIMEOUT" "$DEFAULT_GUNICORN_TIMEOUT"
  fi
  if [[ -z "$gunicorn_keepalive" ]]; then
    set_env_value "GUNICORN_KEEPALIVE" "$DEFAULT_GUNICORN_KEEPALIVE"
  fi
  if [[ -z "$gunicorn_graceful_timeout" ]]; then
    set_env_value "GUNICORN_GRACEFUL_TIMEOUT" "$DEFAULT_GUNICORN_GRACEFUL_TIMEOUT"
  fi
}

ensure_upstream_pool_settings() {
  local request_timeout_ms=""
  local stream_connect_timeout_seconds=""
  local stream_first_token_timeout_seconds=""
  local stream_idle_timeout_seconds=""
  local stream_max_duration_seconds=""
  local upstream_max_connections=""
  local upstream_max_keepalive_connections=""
  local upstream_pool_timeout_s=""

  request_timeout_ms="$(get_env_value "REQUEST_TIMEOUT_MS")"
  stream_connect_timeout_seconds="$(get_env_value "STREAM_CONNECT_TIMEOUT_SECONDS")"
  stream_first_token_timeout_seconds="$(get_env_value "STREAM_FIRST_TOKEN_TIMEOUT_SECONDS")"
  stream_idle_timeout_seconds="$(get_env_value "STREAM_IDLE_TIMEOUT_SECONDS")"
  stream_max_duration_seconds="$(get_env_value "STREAM_MAX_DURATION_SECONDS")"
  upstream_max_connections="$(get_env_value "UPSTREAM_MAX_CONNECTIONS")"
  upstream_max_keepalive_connections="$(get_env_value "UPSTREAM_MAX_KEEPALIVE_CONNECTIONS")"
  upstream_pool_timeout_s="$(get_env_value "UPSTREAM_POOL_TIMEOUT_S")"

  if [[ -z "$request_timeout_ms" ]]; then
    set_env_value "REQUEST_TIMEOUT_MS" "$DEFAULT_REQUEST_TIMEOUT_MS"
  fi
  if [[ -z "$stream_connect_timeout_seconds" ]]; then
    set_env_value "STREAM_CONNECT_TIMEOUT_SECONDS" "$DEFAULT_STREAM_CONNECT_TIMEOUT_SECONDS"
  fi
  if [[ -z "$stream_first_token_timeout_seconds" ]]; then
    set_env_value "STREAM_FIRST_TOKEN_TIMEOUT_SECONDS" "$DEFAULT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS"
  fi
  if [[ -z "$stream_idle_timeout_seconds" ]]; then
    set_env_value "STREAM_IDLE_TIMEOUT_SECONDS" "$DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS"
  fi
  if [[ -z "$stream_max_duration_seconds" ]]; then
    set_env_value "STREAM_MAX_DURATION_SECONDS" "$DEFAULT_STREAM_MAX_DURATION_SECONDS"
  fi
  if [[ -z "$upstream_max_connections" ]]; then
    set_env_value "UPSTREAM_MAX_CONNECTIONS" "$DEFAULT_UPSTREAM_MAX_CONNECTIONS"
  fi
  if [[ -z "$upstream_max_keepalive_connections" ]]; then
    set_env_value "UPSTREAM_MAX_KEEPALIVE_CONNECTIONS" "$DEFAULT_UPSTREAM_MAX_KEEPALIVE_CONNECTIONS"
  fi
  if [[ -z "$upstream_pool_timeout_s" ]]; then
    set_env_value "UPSTREAM_POOL_TIMEOUT_S" "$DEFAULT_UPSTREAM_POOL_TIMEOUT_S"
  fi
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
  set_env_default "PIP_INDEX_URL" "$PIP_INDEX_URL"
  set_env_value "ENABLE_STARTUP_DB_INIT" "false"
  ensure_database_url
  ensure_redis_settings
  ensure_gunicorn_settings
  ensure_upstream_pool_settings
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

ensure_postgresql_database() {
  local database_url=""
  database_url="$(get_env_value "DATABASE_URL")"

  if [[ "$database_url" != postgresql* ]]; then
    log "DATABASE_URL is not PostgreSQL, skipping PostgreSQL initialization."
    return
  fi

  if [[ "$database_url" != "$DEFAULT_DATABASE_URL" ]]; then
    log "DATABASE_URL is a custom PostgreSQL connection, skipping local PostgreSQL role/database initialization."
    return
  fi

  log "Starting PostgreSQL service..."
  systemctl enable postgresql >/dev/null 2>&1 || true
  systemctl start postgresql

  log "Ensuring PostgreSQL role and database exist..."
  if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${POSTGRES_USER}'" | grep -q 1; then
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE ROLE ${POSTGRES_USER} LOGIN PASSWORD '${POSTGRES_PASSWORD}';"
  fi

  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "ALTER ROLE ${POSTGRES_USER} WITH LOGIN PASSWORD '${POSTGRES_PASSWORD}';"

  if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='${POSTGRES_DB}'" | grep -q 1; then
    runuser -u postgres -- createdb -O "$POSTGRES_USER" "$POSTGRES_DB"
  fi

  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d "$POSTGRES_DB" -c "ALTER DATABASE ${POSTGRES_DB} OWNER TO ${POSTGRES_USER};"
}

ensure_redis_service() {
  log "Starting Redis service..."
  systemctl enable redis-server >/dev/null 2>&1 || true
  systemctl start redis-server
}

postgres_dump_url() {
  local database_url="$1"
  printf '%s' "$database_url" | sed -E 's|^postgresql\+[^:]+://|postgresql://|'
}

backup_database_before_initialization() {
  local database_url=""
  local backup_dir=""
  local backup_file=""
  local dump_url=""

  database_url="$(get_env_value "DATABASE_URL")"
  if [[ "$database_url" != postgresql* ]]; then
    log "DATABASE_URL is not PostgreSQL, skipping database backup before initialization."
    return
  fi

  if ! command -v pg_dump >/dev/null 2>&1; then
    log "pg_dump is not available, cannot safely run database initialization."
    exit 1
  fi

  backup_dir="$PROJECT_ROOT/data/db-backups"
  mkdir -p "$backup_dir"
  backup_file="$backup_dir/aotu-gpt-before-db-init-$(date +%Y%m%d-%H%M%S).dump"
  dump_url="$(postgres_dump_url "$database_url")"

  log "Backing up PostgreSQL database before initialization: ${backup_file}"
  pg_dump -Fc -f "$backup_file" "$dump_url"
  chmod 600 "$backup_file"
}

run_database_initialization() {
  if [[ "$RUN_DB_INIT" != "1" ]]; then
    log "Skipping database initialization. Set RUN_DB_INIT=1 to run it explicitly."
    return
  fi

  backup_database_before_initialization
  log "Running one-shot database initialization..."
  ENABLE_STARTUP_DB_INIT=false "$PYTHON_BIN" "$PROJECT_ROOT/scripts/run_startup_db_init.py"
}

write_systemd_service() {
  local service_file="/etc/systemd/system/${SERVICE_NAME}.service"
  log "Writing systemd service: ${service_file}"
  cat > "$service_file" <<EOF
[Unit]
Description=aotu-gpt FastAPI Service
After=network.target postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
User=root
Group=root
WorkingDirectory=${PROJECT_ROOT}
EnvironmentFile=${ENV_FILE}
ExecStart=/bin/bash -lc 'exec ${PYTHON_BIN} -m gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w \${WEB_CONCURRENCY:-4} -b ${APP_HOST}:${APP_PORT} --timeout \${GUNICORN_TIMEOUT:-120} --keep-alive \${GUNICORN_KEEPALIVE:-75} --graceful-timeout \${GUNICORN_GRACEFUL_TIMEOUT:-30}'
Restart=always
RestartSec=5
TimeoutStopSec=30
LimitNOFILE=1048576

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
        proxy_request_buffering off;
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

wait_for_http_success() {
  local url="$1"
  local attempts="$2"
  local sleep_seconds="$3"
  local label="$4"
  local attempt=1

  while (( attempt <= attempts )); do
    if curl -fsS --max-time 10 "$url" >/dev/null; then
      log "${label} passed: ${url}"
      return 0
    fi

    if (( attempt < attempts )); then
      log "${label} pending (${attempt}/${attempts}), retrying in ${sleep_seconds}s..."
      sleep "$sleep_seconds"
    fi
    attempt=$((attempt + 1))
  done

  return 1
}

run_health_checks() {
  local local_url="http://${APP_HOST}:${APP_PORT}/ready"
  local external_base_url=""
  local external_url=""

  if wait_for_http_success "$local_url" 15 1 "Local health check"; then
    :
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
  if wait_for_http_success "$external_url" 10 1 "External health check"; then
    :
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
Gunicorn workers: $(get_env_value "WEB_CONCURRENCY")
Database initialization: $([[ "$RUN_DB_INIT" == "1" ]] && printf 'executed with backup' || printf 'skipped')
LOCAL_PROXY_API_KEY: ${proxy_key}

Remember to open ECS security group TCP port 80 to 0.0.0.0/0.
Useful commands:
  RUN_DB_INIT=1 sudo bash ${PROJECT_ROOT}/start_aliyun.sh
  systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
  ps -ef | grep '[g]unicorn'
  systemctl status nginx
EOF
}

main() {
  require_root
  enter_project_root
  validate_project_files
  install_system_packages
  ensure_data_dir
  ensure_venv
  install_python_dependencies
  ensure_env_file
  ensure_postgresql_database
  ensure_redis_service
  run_database_initialization
  write_systemd_service
  write_nginx_site
  reload_and_start_services
  ensure_firewall_port
  run_health_checks
  print_summary
}

main "$@"
