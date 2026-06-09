#!/bin/bash
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# gyanam.sh - GPU Metrics Collection Pipeline Management Script
#
# Usage: ./gyanam.sh <command>
#
# Commands:
#   start       Start all services
#   stop        Stop all services
#   restart     Restart all services
#   status      Show status of all services
#   logs        Show logs (use -f for follow mode)
#   build       Build/rebuild the collector image
#   clean       Stop services and remove volumes (WARNING: deletes data)
#   init        Initialize environment file with required variables
#

set -e

# Script configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
# Derive project name from directory name so multiple deployments are isolated
PROJECT_NAME="$(basename "${SCRIPT_DIR}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/_/g')"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored message
print_msg() {
    local color=$1
    local msg=$2
    echo -e "${color}${msg}${NC}"
}

print_info() {
    print_msg "${BLUE}" "[INFO] $1"
}

print_success() {
    print_msg "${GREEN}" "[OK] $1"
}

print_warning() {
    print_msg "${YELLOW}" "[WARN] $1"
}

print_error() {
    print_msg "${RED}" "[ERROR] $1"
}

# Check if docker is available
check_docker() {
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed or not in PATH"
        exit 1
    fi

    if ! docker info &> /dev/null; then
        print_error "Docker daemon is not running or you don't have permission"
        exit 1
    fi
}

# Check if docker compose is available
check_docker_compose() {
    if docker compose version &> /dev/null; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &> /dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        print_error "Docker Compose is not installed"
        exit 1
    fi
}

# Generate a random encryption key
generate_encryption_key() {
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || \
    openssl rand -base64 32 | tr '+/' '-_'
}

# Generate a random password
generate_password() {
    openssl rand -base64 16 | tr -d '/+=' | head -c 16
}

# Check for existing deployment with the same project name
check_existing_project() {
    local running
    cd "${SCRIPT_DIR}"
    running=$(${COMPOSE_CMD} -p ${PROJECT_NAME} ps -q 2>/dev/null)
    if [[ -n "${running}" ]]; then
        print_error "A deployment with project name '${PROJECT_NAME}' is already running!"
        echo ""
        print_info "Running containers:"
        ${COMPOSE_CMD} -p ${PROJECT_NAME} ps 2>/dev/null
        echo ""
        print_info "Options:"
        echo "  1. Stop it first:  ./gyanam.sh stop"
        echo "  2. Use a different directory name for a separate deployment"
        return 1
    fi
    return 0
}

# Check if a port is already in use
check_port_available() {
    local port=$1
    local service=$2
    local in_use=false

    if command -v ss &> /dev/null; then
        ss -tlnp 2>/dev/null | grep -q ":${port} " && in_use=true
    elif command -v netstat &> /dev/null; then
        netstat -tlnp 2>/dev/null | grep -q ":${port} " && in_use=true
    else
        # No tool available to check — skip silently
        return 0
    fi

    if [[ "${in_use}" == "true" ]]; then
        print_error "Port ${port} (${service}) is already in use!"
        print_info "Set a different port in .env, e.g.: ${service}_PORT=$((port + 1))"
        return 1
    fi
    return 0
}

# Initialize environment file
init_env() {
    # Check for conflicting running deployment
    if ! check_existing_project; then
        exit 1
    fi

    if [[ -f "${ENV_FILE}" ]]; then
        print_warning "Environment file already exists: ${ENV_FILE}"
        read -p "Do you want to overwrite it? (y/N): " confirm
        if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
            print_info "Keeping existing environment file"
            return 0
        fi
    fi

    print_info "Generating environment file..."

    # Generate secrets
    local influx_token
    local influx_admin_pass
    local grafana_admin_pass
    local encryption_key
    influx_token=$(generate_password)$(generate_password)
    influx_admin_pass=$(generate_password)
    grafana_admin_pass=$(generate_password)
    encryption_key=$(generate_encryption_key)

    cat > "${ENV_FILE}" << EOF
# GPU Metrics Collector - Environment Configuration
# Generated on $(date)

# InfluxDB Configuration
INFLUXDB_TOKEN=${influx_token}
INFLUXDB_ADMIN_USER=admin
INFLUXDB_ADMIN_PASSWORD=${influx_admin_pass}
INFLUXDB_ORG=prometheus
INFLUXDB_BUCKET=gpu_metrics

# Retention policies for tiered storage
INFLUXDB_RETENTION=7d                 # Raw data retention (5-min intervals)
INFLUXDB_15M_RETENTION=30d            # 15-minute aggregations retention
INFLUXDB_HOURLY_RETENTION=90d         # Hourly aggregations retention

# InfluxDB write performance tuning (recommended for 300+ targets)
INFLUXDB_BATCH_SIZE=5000              # Batch size for writes (default: 1000)
INFLUXDB_WRITE_TIMEOUT_MS=120000      # Write timeout in ms (default: 60000)
INFLUXDB_FLUSH_INTERVAL_SECONDS=15    # Buffer flush interval (default: 10)

# Alert System Configuration
# Note: Replace 'collector' with actual hostname/IP if BMCs are on different network
ALERT_WEBHOOK_BASE_URL=http://collector:8081/redfish-webhook
ALERT_ENABLE_WEBHOOK_FALLBACK=true    # Fallback to webhooks when SSE is unavailable

# Grafana Configuration
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=${grafana_admin_pass}

# Collector Configuration
ENCRYPTION_KEY=${encryption_key}

# Metrics Backend: "influxdb" (default) or "prometheus"
METRICS_BACKEND=influxdb

# Port Configuration (change for multiple deployments on same host)
API_PORT=8080
INFLUXDB_PORT=8086
GRAFANA_PORT=3000
PROMETHEUS_PORT=9090
EOF

    chmod 600 "${ENV_FILE}"
    print_success "Environment file created: ${ENV_FILE}"
    echo ""
    print_info "Generated credentials:"
    echo "  InfluxDB Admin Password: ${influx_admin_pass}"
    echo "  Grafana Admin Password:  ${grafana_admin_pass}"
    echo ""
    print_warning "Please save these credentials securely!"
}

# Check if environment file exists and has required variables
check_env() {
    if [[ ! -f "${ENV_FILE}" ]]; then
        print_error "Environment file not found: ${ENV_FILE}"
        print_info "Run './gyanam.sh init' to create one"
        exit 1
    fi

    # Source the env file to check variables
    # shellcheck source=/dev/null
    source "${ENV_FILE}"

    local missing=()
    [[ -z "${INFLUXDB_TOKEN}" ]] && missing+=("INFLUXDB_TOKEN")
    [[ -z "${INFLUXDB_ADMIN_PASSWORD}" ]] && missing+=("INFLUXDB_ADMIN_PASSWORD")
    [[ -z "${GRAFANA_ADMIN_PASSWORD}" ]] && missing+=("GRAFANA_ADMIN_PASSWORD")
    [[ -z "${ENCRYPTION_KEY}" ]] && missing+=("ENCRYPTION_KEY")

    if [[ ${#missing[@]} -gt 0 ]]; then
        print_error "Missing required environment variables:"
        for var in "${missing[@]}"; do
            echo "  - ${var}"
        done
        print_info "Run './gyanam.sh init' to generate them"
        exit 1
    fi
}

# Start services
cmd_start() {
    print_info "Starting GPU Metrics Collector services..."
    check_env

    # Check port availability before starting
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
    local api_port="${API_PORT:-8080}"
    local inf_port="${INFLUXDB_PORT:-8086}"
    local gra_port="${GRAFANA_PORT:-3000}"
    local port_conflict=false

    local backend="${METRICS_BACKEND:-influxdb}"

    check_port_available "${api_port}" "API" || port_conflict=true
    check_port_available "${gra_port}" "GRAFANA" || port_conflict=true

    if [[ "${backend}" == "prometheus" ]]; then
        local prom_port="${PROMETHEUS_PORT:-9090}"
        check_port_available "${prom_port}" "PROMETHEUS" || port_conflict=true
    else
        check_port_available "${inf_port}" "INFLUXDB" || port_conflict=true
    fi

    if [[ "${port_conflict}" == "true" ]]; then
        echo ""
        print_error "Cannot start — port conflict detected. Edit .env to change ports."
        exit 1
    fi

    cd "${SCRIPT_DIR}"
    if [[ "${backend}" == "prometheus" ]]; then
        print_info "Using Prometheus backend"
        ${COMPOSE_CMD} -p ${PROJECT_NAME} -f docker-compose.yml -f docker-compose.prometheus.yml up -d
    else
        print_info "Using InfluxDB backend"
        ${COMPOSE_CMD} -p ${PROJECT_NAME} up -d
    fi

    echo ""
    print_success "Services started successfully!"
    echo ""
    print_info "Access the services at:"
    echo "  Web UI:        http://localhost:${api_port}"
    if [[ "${backend}" == "prometheus" ]]; then
        local prom_port="${PROMETHEUS_PORT:-9090}"
        echo "  Prometheus:    http://localhost:${prom_port}"
    else
        echo "  InfluxDB:      http://localhost:${inf_port}"
    fi
    echo "  Grafana:       http://localhost:${gra_port}"
    echo ""
    print_info "Use './gyanam.sh logs -f' to view logs"
}

# Stop services
cmd_stop() {
    print_info "Stopping GPU Metrics Collector services..."

    cd "${SCRIPT_DIR}"
    # shellcheck source=/dev/null
    source "${ENV_FILE}" 2>/dev/null || true
    local backend="${METRICS_BACKEND:-influxdb}"

    if [[ "${backend}" == "prometheus" ]]; then
        ${COMPOSE_CMD} -p ${PROJECT_NAME} -f docker-compose.yml -f docker-compose.prometheus.yml down
    else
        ${COMPOSE_CMD} -p ${PROJECT_NAME} down
    fi

    print_success "Services stopped successfully!"
}

# Restart services
cmd_restart() {
    print_info "Restarting GPU Metrics Collector services..."
    cmd_stop
    echo ""
    cmd_start
}

# Show status
cmd_status() {
    print_info "GPU Metrics Collector service status:"
    echo ""

    cd "${SCRIPT_DIR}"
    ${COMPOSE_CMD} -p ${PROJECT_NAME} ps

    echo ""

    # Check health of each service
    # shellcheck source=/dev/null
    source "${ENV_FILE}" 2>/dev/null || true
    local api_port="${API_PORT:-8080}"
    local inf_port="${INFLUXDB_PORT:-8086}"
    local gra_port="${GRAFANA_PORT:-3000}"
    local backend="${METRICS_BACKEND:-influxdb}"

    print_info "Health checks (backend: ${backend}):"

    # Collector (background service - check if process is running)
    local collector_status
    collector_status=$(${COMPOSE_CMD} -p ${PROJECT_NAME} ps collector --format json 2>/dev/null | grep -o '"Health":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
    if [[ "${collector_status}" == "healthy" ]] || ${COMPOSE_CMD} -p ${PROJECT_NAME} ps collector 2>/dev/null | grep -q "Up"; then
        print_success "Collector:  running"
    else
        print_warning "Collector:  not running or unhealthy"
    fi

    # API
    if curl -sf http://localhost:${api_port}/health > /dev/null 2>&1; then
        print_success "API:        healthy (port ${api_port})"
    else
        print_warning "API:        not responding (port ${api_port})"
    fi

    # Metrics backend
    if [[ "${backend}" == "prometheus" ]]; then
        local prom_port="${PROMETHEUS_PORT:-9090}"
        if curl -sf http://localhost:${prom_port}/-/healthy > /dev/null 2>&1; then
            print_success "Prometheus: healthy (port ${prom_port})"
        else
            print_warning "Prometheus: not responding (port ${prom_port})"
        fi
    else
        if curl -sf http://localhost:${inf_port}/ping > /dev/null 2>&1; then
            print_success "InfluxDB:   healthy (port ${inf_port})"
        else
            print_warning "InfluxDB:   not responding (port ${inf_port})"
        fi
    fi

    # Grafana
    if curl -sf http://localhost:${gra_port}/api/health > /dev/null 2>&1; then
        print_success "Grafana:    healthy (port ${gra_port})"
    else
        print_warning "Grafana:    not responding (port ${gra_port})"
    fi
}

# Show logs
cmd_logs() {
    cd "${SCRIPT_DIR}"
    ${COMPOSE_CMD} -p ${PROJECT_NAME} logs "$@"
}

# Build images
cmd_build() {
    print_info "Building GPU Metrics Collector images (API and Collector)..."
    check_env

    cd "${SCRIPT_DIR}"
    ${COMPOSE_CMD} -p ${PROJECT_NAME} build "$@"

    print_success "Build completed for both API and Collector services!"
}

# Setup 15-minute downsampling (bucket + task)
cmd_setup_15m_downsampling() {
    print_info "Setting up InfluxDB 15-minute downsampling..."
    check_env

    cd "${SCRIPT_DIR}"
    ${COMPOSE_CMD} -p ${PROJECT_NAME} exec influxdb /bin/bash /docker-entrypoint-initdb.d/setup-15m-downsampling.sh

    print_success "15-minute downsampling configured!"
}

# Setup hourly downsampling (bucket + task)
cmd_setup_hourly_downsampling() {
    print_info "Setting up InfluxDB hourly downsampling..."
    check_env

    cd "${SCRIPT_DIR}"
    ${COMPOSE_CMD} -p ${PROJECT_NAME} exec influxdb /bin/bash /docker-entrypoint-initdb.d/setup-hourly-downsampling.sh

    print_success "Hourly downsampling configured!"
}

# Setup all downsampling tiers
cmd_setup_all_downsampling() {
    print_info "Setting up complete downsampling pipeline..."
    cmd_setup_15m_downsampling
    cmd_setup_hourly_downsampling
    print_success "All downsampling tiers configured!"
}

# Check InfluxDB bucket status
cmd_influx_status() {
    check_env
    print_info "Checking InfluxDB bucket status..."

    docker run --rm --network "${PROJECT_NAME}_monitoring" \
        -v "${SCRIPT_DIR}/scripts:/scripts:ro" \
        -e INFLUXDB_TOKEN="${INFLUXDB_TOKEN}" \
        -e INFLUXDB_ORG="${INFLUXDB_ORG:-prometheus}" \
        python:3.11-slim \
        bash -c "pip install -q influxdb-client && python /scripts/export_influxdb_data.py --url http://influxdb:8086 --check-status"
}

# List measurements in a bucket
cmd_influx_list() {
    check_env
    local bucket="${1:-gpu_metrics}"
    print_info "Listing measurements in bucket '${bucket}'..."

    docker run --rm --network "${PROJECT_NAME}_monitoring" \
        -v "${SCRIPT_DIR}/scripts:/scripts:ro" \
        -e INFLUXDB_TOKEN="${INFLUXDB_TOKEN}" \
        -e INFLUXDB_ORG="${INFLUXDB_ORG:-prometheus}" \
        python:3.11-slim \
        bash -c "pip install -q influxdb-client && python /scripts/export_influxdb_data.py --url http://influxdb:8086 --list-measurements --bucket ${bucket}"
}

# Export metrics to CSV
cmd_influx_export() {
    check_env
    # shellcheck source=/dev/null
    source "${ENV_FILE}"

    local bucket="${1:-gpu_metrics}"
    local output="${2:-./exports/metrics_$(date +%Y%m%d_%H%M%S).csv}"
    local start="${3:--24h}"
    local stop="${4:-now()}"

    # ---- sudo env-var advisory ---------------------------------------
    # By default `sudo` strips the caller's environment (env_reset). If
    # the user types `INFLUX_CHUNK_HOURS=6 sudo ./gyanam.sh ...` those
    # vars never reach this script. Warn loudly so they don't silently
    # use the defaults and wonder why a 200M-row export is "single
    # window".
    if [[ -n "${SUDO_USER:-}" ]]; then
        local set_any=""
        for v in INFLUXDB_TIMEOUT_MS INFLUX_CHUNK_HOURS INFLUX_DRY_RUN \
                 INFLUX_AGGREGATE_WINDOW INFLUX_AGGREGATE_FN \
                 INFLUX_COLUMNS INFLUX_PIVOT INFLUX_MAX_ROWS \
                 INFLUX_QUIET INFLUX_SKIP_WARMUP \
                 INFLUX_FIELD_NAME INFLUX_TARGET_TAG INFLUX_DEBUG_QUERY \
                 INFLUX_EXPORT_MAX_RETRIES INFLUX_CONFIRM_LARGE \
                 INFLUX_LARGE_EXPORT_THRESHOLD; do
            if [[ -n "${!v+x}" ]]; then set_any="$set_any $v"; fi
        done
        print_warning "Running under sudo (SUDO_USER=${SUDO_USER})."
        if [[ -z "${set_any}" ]]; then
            print_warning "If you set any INFLUX_* env vars BEFORE 'sudo',"
            print_warning "they were stripped before this script saw them."
            print_warning "Use ONE of:"
            print_warning "  sudo -E ./gyanam.sh influx-export ...       # keeps caller env"
            print_warning "  sudo INFLUX_CHUNK_HOURS=6 ./gyanam.sh ...   # set AFTER sudo"
            print_warning "  add yourself to the docker group + re-login to drop sudo"
        else
            print_info "Tunables visible to this script:${set_any}"
        fi
    fi

    print_info "Exporting data from bucket '${bucket}'..."
    print_info "Time range: ${start} to ${stop}"
    print_info "Output: ${output}"

    # Create exports directory if it doesn't exist
    mkdir -p "$(dirname "${output}")"

    # Tunables (all overridable via env at invocation time):
    #   INFLUXDB_TIMEOUT_MS       — per-HTTP-request timeout (ms). Default 10m.
    #   INFLUX_CHUNK_HOURS        — split into N-hour windows. Default 0 = none.
    #   INFLUX_EXPORT_MAX_RETRIES — per-chunk retry budget. Default 3.
    #   INFLUX_DRY_RUN            — 1 = run only pre-flight count.
    #   INFLUX_AGGREGATE_WINDOW   — server-side downsample (e.g. 5m, 1h).
    #   INFLUX_AGGREGATE_FN       — paired with above (mean/max/min/...).
    #   INFLUX_COLUMNS            — comma-separated columns to keep.
    #   INFLUX_PIVOT              — 1 to emit pivoted (wide) CSV.
    #   INFLUX_MAX_ROWS           — refuse if pre-flight count exceeds.
    #   INFLUX_FIELD_NAME         — Filter to a single _field key.
    #                               DEFAULT: unset = export ALL fields
    #                               in the bucket. Set this only if you
    #                               want to narrow (e.g. to "value" on
    #                               a gyanam bucket, or "mean" on a
    #                               downsampled one).
    #   INFLUX_TARGET_TAG         — tag key holding the target identifier.
    #                               Default 'target_name' (gyanam's collector).
    #                               Older builds used 'target' which matched
    #                               nothing — set this if you see 0 rows
    #                               with --targets / a per-target filter.
    #   INFLUX_DEBUG_QUERY        — 1 = print every Flux query to stderr.
    #   INFLUX_QUIET              — 1 = suppress periodic progress lines.
    #   INFLUX_SKIP_WARMUP        — 1 = skip the DNS/TCP/TLS probe.
    #   INFLUX_CONFIRM_LARGE      — 1 = explicitly acknowledge a large
    #                               export (>10M rows) without chunking
    #                               or aggregation. Default refuses to
    #                               start. Use INFLUX_CHUNK_HOURS or
    #                               INFLUX_AGGREGATE_WINDOW instead.
    #   INFLUX_LARGE_EXPORT_THRESHOLD — rows above which the guard fires.
    #                               Default 10,000,000.
    local timeout_ms="${INFLUXDB_TIMEOUT_MS:-600000}"
    local chunk_hours="${INFLUX_CHUNK_HOURS:-0}"
    local max_retries="${INFLUX_EXPORT_MAX_RETRIES:-3}"
    local extra_args=""
    if [[ "${INFLUX_DRY_RUN:-0}" == "1" ]]; then
        extra_args+=" --dry-run"
        print_info "DRY RUN: will only run pre-flight count, no CSV will be written"
    fi
    if [[ -n "${INFLUX_AGGREGATE_WINDOW:-}" ]]; then
        extra_args+=" --aggregate-window=${INFLUX_AGGREGATE_WINDOW}"
        extra_args+=" --aggregate-fn=${INFLUX_AGGREGATE_FN:-mean}"
        print_info "Aggregating server-side: every ${INFLUX_AGGREGATE_WINDOW} via ${INFLUX_AGGREGATE_FN:-mean}()"
    fi
    if [[ -n "${INFLUX_COLUMNS:-}" ]]; then
        extra_args+=" --columns=${INFLUX_COLUMNS}"
    fi
    if [[ "${INFLUX_PIVOT:-0}" == "1" ]]; then
        extra_args+=" --pivot"
    fi
    if [[ -n "${INFLUX_MAX_ROWS:-}" ]]; then
        extra_args+=" --max-rows=${INFLUX_MAX_ROWS}"
    fi
    if [[ "${INFLUX_QUIET:-0}" == "1" ]]; then
        extra_args+=" --quiet"
    fi
    if [[ "${INFLUX_SKIP_WARMUP:-0}" == "1" ]]; then
        extra_args+=" --skip-warmup"
    fi
    if [[ -n "${INFLUX_FIELD_NAME+x}" ]]; then
        # Explicitly set (including empty string to disable the field filter).
        extra_args+=" --field-name=${INFLUX_FIELD_NAME}"
    fi
    if [[ -n "${INFLUX_TARGET_TAG:-}" ]]; then
        extra_args+=" --target-tag=${INFLUX_TARGET_TAG}"
    fi
    if [[ "${INFLUX_DEBUG_QUERY:-0}" == "1" ]]; then
        extra_args+=" --debug-query"
        print_info "DEBUG: every Flux query will be printed to stderr"
    fi
    # INFLUX_CONFIRM_LARGE and INFLUX_LARGE_EXPORT_THRESHOLD are
    # honoured directly by the Python script via env — pass them
    # through unchanged.

    docker run --rm --network "${PROJECT_NAME}_monitoring" \
        -v "${SCRIPT_DIR}/scripts:/scripts:ro" \
        -v "${SCRIPT_DIR}:/workspace" \
        -w /workspace \
        -e INFLUXDB_TOKEN="${INFLUXDB_TOKEN}" \
        -e INFLUXDB_ORG="${INFLUXDB_ORG:-prometheus}" \
        -e INFLUXDB_TIMEOUT_MS="${timeout_ms}" \
        -e INFLUX_CHUNK_HOURS="${chunk_hours}" \
        -e INFLUX_EXPORT_MAX_RETRIES="${max_retries}" \
        -e INFLUX_CONFIRM_LARGE="${INFLUX_CONFIRM_LARGE:-}" \
        -e INFLUX_LARGE_EXPORT_THRESHOLD="${INFLUX_LARGE_EXPORT_THRESHOLD:-}" \
        -e EXPORT_BUCKET="${bucket}" \
        -e EXPORT_START="${start}" \
        -e EXPORT_STOP="${stop}" \
        -e EXPORT_OUTPUT="${output}" \
        -e EXTRA_ARGS="${extra_args}" \
        -e PYTHONUNBUFFERED=1 \
        python:3.11-slim \
        bash -c 'pip install -q influxdb-client && python -u /scripts/export_influxdb_data.py --url http://influxdb:8086 --export --bucket="${EXPORT_BUCKET}" --start="${EXPORT_START}" --stop="${EXPORT_STOP}" --output="${EXPORT_OUTPUT}" ${EXTRA_ARGS}'

    if [ -f "${output}" ]; then
        print_success "Data exported successfully to ${output}"
        print_info "You can now open this file in Excel, LibreOffice, or any spreadsheet application"
    else
        print_error "Export failed - output file not created"
    fi
}

# Monitor volume usage and disk space
cmd_monitor() {
    print_info "Monitoring Docker volume usage and disk space"
    echo ""

    # Check if containers are running
    cd "${SCRIPT_DIR}"
    local running
    running=$(${COMPOSE_CMD} -p ${PROJECT_NAME} ps -q 2>/dev/null)
    if [[ -z "${running}" ]]; then
        print_warning "No containers are running. Start services with './gyanam.sh start'"
        echo ""
    fi

    # Use the dedicated monitoring script if available
    if [[ -x "${SCRIPT_DIR}/scripts/monitor_volumes.sh" ]]; then
        "${SCRIPT_DIR}/scripts/monitor_volumes.sh"
    else
        # Fallback to inline monitoring
        print_info "Docker Volume Usage:"
        docker volume ls | grep "${PROJECT_NAME}" || echo "  No ${PROJECT_NAME} volumes found"
        echo ""

        print_info "Disk Space on Docker Partition:"
        df -h /var/lib/docker | awk 'NR==1 || /\//'
        echo ""

        print_info "Total Docker Usage:"
        docker system df
    fi

    echo ""
    print_info "For detailed tracking, see: scripts/README.md"
    print_info "Set up automated monitoring:"
    echo "  - Track growth: ./scripts/log_volume_growth.sh"
    echo "  - Disk alerts:  ./scripts/alert_disk_space.sh"
}

# Clean up everything
cmd_clean() {
    print_warning "This will stop all services and DELETE all data!"
    read -p "Are you sure you want to continue? (type 'yes' to confirm): " confirm

    if [[ "${confirm}" != "yes" ]]; then
        print_info "Aborted"
        exit 0
    fi

    print_info "Stopping services and removing volumes..."

    cd "${SCRIPT_DIR}"
    # shellcheck source=/dev/null
    source "${ENV_FILE}" 2>/dev/null || true
    local backend="${METRICS_BACKEND:-influxdb}"

    if [[ "${backend}" == "prometheus" ]]; then
        ${COMPOSE_CMD} -p ${PROJECT_NAME} -f docker-compose.yml -f docker-compose.prometheus.yml down -v
    else
        ${COMPOSE_CMD} -p ${PROJECT_NAME} down -v
    fi

    print_success "Cleanup completed!"
}

# Show help
cmd_help() {
    cat << EOF
GPU Metrics Collector Management Script

Usage: ./gyanam.sh <command> [options]

Commands:
  init        Initialize environment file with required variables
  start       Start all services (api, collector, influxdb, grafana)
  stop        Stop all services
  restart     Restart all services
  status      Show status of all services
  monitor     Monitor Docker volume usage and disk space
  logs        Show logs (add -f to follow, or service name)
  build       Build/rebuild the API and collector images
  setup-15m-downsampling    Create 15-min downsampling (30d retention)
  setup-hourly-downsampling Create hourly downsampling (90d retention)
  setup-all-downsampling    Configure complete downsampling pipeline
  influx-status             Check status of all InfluxDB buckets
  influx-list [bucket]      List measurements in a bucket (default: gpu_metrics)
  influx-export [bucket] [output] [start] [stop]
                            Export metrics to CSV (.csv or .csv.gz).
                            See "Influx Export Reference" below for all
                            env vars (timeout, chunking, aggregation, ...).
  clean       Stop services and remove volumes (WARNING: deletes data)
  help        Show this help message

Examples:
  ./gyanam.sh init              # First-time setup
  ./gyanam.sh start             # Start the pipeline
  ./gyanam.sh monitor           # Check volume usage and disk space
  ./gyanam.sh logs -f           # Follow all logs
  ./gyanam.sh logs api          # View API logs only
  ./gyanam.sh logs collector    # View collector logs only
  ./gyanam.sh build             # Rebuild both services
  ./gyanam.sh influx-status     # Check all InfluxDB buckets
  ./gyanam.sh influx-list gpu_metrics_15m  # List measurements in 15-min bucket
  ./gyanam.sh influx-export gpu_metrics ./data.csv.gz -7d   # gzipped CSV (recommended)
  # Probe first: dry-run shows pre-flight count, first-byte time, size estimate.
  INFLUX_DRY_RUN=1 ./gyanam.sh influx-export gpu_metrics ./out.csv.gz -7d
  # Big export: split into 6h windows, each independently retryable.
  INFLUX_CHUNK_HOURS=6 ./gyanam.sh influx-export gpu_metrics ./30d.csv.gz -30d
  # Analysis export: downsample to 5-min means server-side (~60x fewer rows).
  INFLUX_AGGREGATE_WINDOW=5m INFLUX_AGGREGATE_FN=mean \\
      ./gyanam.sh influx-export gpu_metrics ./30d_5m.csv.gz -30d
  # Narrow columns server-side for a focused analysis (less data over the wire).
  INFLUX_COLUMNS="_time,_measurement,target,value" \\
      ./gyanam.sh influx-export gpu_metrics ./narrow.csv.gz -24h
  # Safety cap to refuse accidentally huge ranges:
  INFLUX_MAX_ROWS=200000000 INFLUX_CHUNK_HOURS=6 \\
      ./gyanam.sh influx-export gpu_metrics ./bounded.csv.gz -30d
  # Per-HTTP-request timeout (default 600000 = 10m), applies per chunk:
  INFLUXDB_TIMEOUT_MS=1800000 INFLUX_CHUNK_HOURS=6 \\
      ./gyanam.sh influx-export gpu_metrics ./30d.csv.gz -30d
  ./gyanam.sh stop              # Stop the pipeline

Influx Export Reference:
  Command:
    ./gyanam.sh influx-export [bucket] [output] [start] [stop]

  Positional arguments (all optional):
    bucket    InfluxDB bucket to export from.        Default: gpu_metrics
    output    Output file path.                      Default: ./exports/metrics_<TS>.csv
              If it ends in .gz (e.g. data.csv.gz), output is gzip-compressed
              transparently — 5-10x smaller files; tools like Excel/pandas/
              libreoffice handle .csv.gz natively.
    start     Flux time expression.                  Default: -24h
              Examples: -7d, -24h, -90m, 2026-01-01T00:00:00Z
    stop      Flux time expression.                  Default: now()

  Environment variables (prepend to invocation):

    -- Data shape & volume --
    INFLUX_AGGREGATE_WINDOW=DUR  Server-side downsample to DUR-wide windows
                                 (e.g. 5m, 1h). Huge row reduction for
                                 analytics — 60x for 5-min on 5-second data.
                                 Default: unset (raw points).
    INFLUX_AGGREGATE_FN=FN       Aggregation function used with the above.
                                 Default: mean.
                                 Choices: mean, max, min, median, sum,
                                          count, first, last.
    INFLUX_COLUMNS=LIST          Server-side keep() — comma-separated column
                                 names. Cuts bytes-over-wire significantly
                                 when you only need a few columns.
                                 Example: "_time,_measurement,target,value"
                                 Default: unset (all columns).
    INFLUX_PIVOT=1               Emit pivoted (wide) CSV. Default: 0
                                 (long format). For gyanam buckets (single
                                 'value' field) pivot is a no-op but
                                 expensive server-side — keep off unless
                                 your bucket has multiple fields per point.

    -- Schema mapping (set if you see "0 rows" results) --
    INFLUX_FIELD_NAME=NAME       Narrow the export and the pre-flight
                                 count to a single _field key (e.g.
                                 'value', 'mean'). DEFAULT: unset —
                                 export ALL fields in the bucket. You
                                 only need this if you want the row
                                 count to reflect the pivoted output
                                 on a multi-field bucket, OR you want
                                 to limit a wide bucket to one field.
    INFLUX_TARGET_TAG=KEY        Tag key holding the target identifier
                                 (used with --targets). Default
                                 'target_name' — matches gyanam's
                                 collector (extra_tags['target_name']).
                                 Older builds used 'target' and silently
                                 matched nothing.
    INFLUX_DEBUG_QUERY=1         Print every Flux query the script sends
                                 to stderr. Use this to diagnose 0-rows
                                 results or any query that doesn't
                                 behave as expected. Default: 0.

    -- Chunking & timing --
    INFLUX_CHUNK_HOURS=N         Split the range into N-hour windows; each
                                 is a separate HTTP request, bounded by its
                                 own timeout, independently retried.
                                 Default: 0 (single window).
                                 Recommended for >1d exports.
    INFLUXDB_TIMEOUT_MS=N        Per-HTTP-request timeout (ms). With
                                 chunking, this is a PER-CHUNK ceiling.
                                 Default: 600000 (10 minutes).
                                 Bump to 1800000 (30m) for very slow
                                 single chunks.
    INFLUX_EXPORT_MAX_RETRIES=N  Per-chunk retry budget on transient
                                 errors (timeout, server-disconnect,
                                 connection-reset). Each retry re-runs the
                                 chunk's query from scratch.
                                 Default: 3.

    -- Safety & probing --
    INFLUX_MAX_ROWS=N            Refuse to start the export if the
                                 pre-flight count exceeds N rows. Guards
                                 against accidental huge ranges
                                 (e.g. typing -10y instead of -10d).
                                 Default: unset (no ceiling).
    INFLUX_DRY_RUN=1             Run only the pre-flight count + warmup
                                 probe; print row-count, first-byte time,
                                 and estimated file size. No CSV written.
                                 Use this BEFORE every long export.

    -- Output behaviour --
    INFLUX_QUIET=1               Suppress periodic progress lines (final
                                 summary still printed).
    INFLUX_SKIP_WARMUP=1         Skip the DNS / TCP / TLS warmup probe.
                                 Default: 0 (probe runs).

  Pre-flight output (shown at start of every export):
    * Warmup timings (DNS / TCP / TLS) — localises network slowness.
    * Pre-flight count() with first-byte time — measures server response.
    * Estimated row count + estimated output file size (uncompressed + gzipped).

  Progress output during streaming:
    * First-byte time per window (gap between query-sent and first row).
    * Adaptive progress lines (every 50K rows OR every 10s) with rate
      (rec/s, EMA-smoothed) and ETA.
    * Per-window timing summary in chunked mode.

  Recommended workflow for any export larger than ~1M rows or remote DB:
    1) Probe first  — INFLUX_DRY_RUN=1 to see size + first-byte latency.
    2) For analytics — INFLUX_AGGREGATE_WINDOW=5m for ~60x fewer rows.
    3) For multi-day — INFLUX_CHUNK_HOURS=6 for bounded per-window
                       timeouts + retries.
    4) Always use   — .csv.gz output extension (5-10x smaller).
    5) Set ceiling  — INFLUX_MAX_ROWS=N to prevent accidental huge runs.

  For the full Python-level help (every flag, including ones not exposed
  via env vars), run:
    docker run --rm -v "\$(pwd)/scripts:/scripts:ro" python:3.11-slim \\
      bash -c 'pip install -q influxdb-client && \\
               python /scripts/export_influxdb_data.py --help'

Monitoring:
  For automated volume tracking and alerts, see scripts/README.md

Environment:
  Configuration is stored in .env file.
  Run './gyanam.sh init' to generate default configuration.

EOF
}

# Main entry point
main() {
    # Check prerequisites
    check_docker
    check_docker_compose

    # Parse command
    local command="${1:-help}"
    shift || true

    case "${command}" in
        init)
            init_env
            ;;
        start)
            cmd_start
            ;;
        stop)
            cmd_stop
            ;;
        restart)
            cmd_restart
            ;;
        status)
            cmd_status
            ;;
        monitor)
            cmd_monitor
            ;;
        logs)
            cmd_logs "$@"
            ;;
        build)
            cmd_build "$@"
            ;;
        setup-15m-downsampling)
            cmd_setup_15m_downsampling
            ;;
        setup-hourly-downsampling)
            cmd_setup_hourly_downsampling
            ;;
        setup-all-downsampling)
            cmd_setup_all_downsampling
            ;;
        influx-status)
            cmd_influx_status
            ;;
        influx-list)
            cmd_influx_list "$@"
            ;;
        influx-export)
            cmd_influx_export "$@"
            ;;
        clean)
            cmd_clean
            ;;
        help|--help|-h)
            cmd_help
            ;;
        *)
            print_error "Unknown command: ${command}"
            echo ""
            cmd_help
            exit 1
            ;;
    esac
}

main "$@"
