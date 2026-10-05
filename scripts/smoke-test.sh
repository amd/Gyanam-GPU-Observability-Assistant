#!/usr/bin/env bash
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
#
# Live smoke tests against a RUNNING docker-compose stack.
#
# Unlike ./scripts/run-tests.sh (fast, fully-mocked, run anywhere), this hits the
# actual running services to catch regressions mocks can't: image/dependency
# drift, cross-service wiring, template/route 500s, and real DB/InfluxDB
# connectivity. Run it after `./gyanam.sh start`.
#
# Usage:
#   ./scripts/smoke-test.sh                     # read-only checks (safe on a live fleet)
#   GYANAM_LIVE_MUTATE=1 ./scripts/smoke-test.sh # also run the create/delete round-trip
#   GYANAM_SMOKE_USER=admin GYANAM_SMOKE_PASS=... ./scripts/smoke-test.sh  # non-default creds
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="gyanam-collector:latest"

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "Image ${IMAGE} not found — run ./gyanam.sh build first." >&2
    exit 1
fi

# Discover the compose network (project-prefixed, e.g. gyanam_monitoring).
NET="$(docker network ls --format '{{.Name}}' | grep -E '(^|_)monitoring$' | head -1)"
if [[ -z "${NET}" ]]; then
    echo "Compose 'monitoring' network not found — is the stack running? (./gyanam.sh start)" >&2
    exit 1
fi

# Load secrets from .env (for the gated datastore round-trips + pip proxy flag).
if [[ -f "${REPO_ROOT}/.env" ]]; then
    # shellcheck source=/dev/null
    set -a; source "${REPO_ROOT}/.env"; set +a
fi
PIP_FLAGS=""
if [[ -n "${PIP_TRUSTED_HOST:-}" ]]; then
    PIP_FLAGS="--trusted-host pypi.org --trusted-host files.pythonhosted.org"
fi

# Datastore round-trips (only used when GYANAM_LIVE_MUTATE=1) talk to the real
# services over the compose network, so use the internal service hostnames.
_PG_USER="${POSTGRES_USER:-gyanam}"
_PG_DB="${POSTGRES_DB:-gyanam_alerts}"
ALERTS_DB_URL="postgresql+asyncpg://${_PG_USER}:${POSTGRES_PASSWORD:-}@postgres:5432/${_PG_DB}"

echo "Running live smoke tests against the running stack (network: ${NET}) ..."
docker run --rm --network "${NET}" -w /app -e PYTHONPATH=/app \
    -e GYANAM_API_URL="${GYANAM_API_URL:-http://api:8080}" \
    -e GYANAM_COLLECTOR_URL="${GYANAM_COLLECTOR_URL:-http://collector:8081}" \
    -e GYANAM_SMOKE_USER="${GYANAM_SMOKE_USER:-admin}" \
    -e GYANAM_SMOKE_PASS="${GYANAM_SMOKE_PASS:-changeme}" \
    -e GYANAM_LIVE_MUTATE="${GYANAM_LIVE_MUTATE:-}" \
    -e GYANAM_ALERTS_DB_URL="${ALERTS_DB_URL}" \
    -e GYANAM_ENCRYPTION_KEY="${ENCRYPTION_KEY:-}" \
    -e GYANAM_INFLUXDB_URL="http://influxdb:8086" \
    -e GYANAM_INFLUXDB_TOKEN="${INFLUXDB_TOKEN:-}" \
    -e GYANAM_INFLUXDB_ORG="${INFLUXDB_ORG:-prometheus}" \
    -e GYANAM_INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-gpu_metrics}" \
    -v "${REPO_ROOT}/collector/tests:/app/tests:ro" \
    -v "${REPO_ROOT}/collector/pytest.ini:/app/pytest.ini:ro" \
    "${IMAGE}" \
    sh -c "pip install --no-cache-dir -q ${PIP_FLAGS} pytest && python -m pytest -q -o addopts='' tests/live"
