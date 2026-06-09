#!/usr/bin/env bash
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
#
# Reproducible unit-test run for the collector/api code base.
#
# Runs pytest (with coverage) inside the gyanam-collector image so the exact
# runtime dependencies are used. Dev-only test deps (pytest-cov, pytest-httpx)
# are installed into the ephemeral container at run time from requirements-dev.txt.
#
# Usage:
#   ./scripts/run-tests.sh                # full suite + coverage summary
#   ./scripts/run-tests.sh tests/test_config.py -k env   # pass args through to pytest
#
# Behind a TLS-intercepting proxy, set PIP_TRUSTED_HOST=1 (or in .env) so the
# dev-dep install can reach PyPI.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="gyanam-collector:latest"

# Honor PIP_TRUSTED_HOST from env/.env for proxy environments.
if [[ -z "${PIP_TRUSTED_HOST:-}" && -f "${REPO_ROOT}/.env" ]]; then
    PIP_TRUSTED_HOST="$(grep -E '^PIP_TRUSTED_HOST=' "${REPO_ROOT}/.env" | cut -d= -f2- || true)"
fi
PIP_FLAGS=""
if [[ -n "${PIP_TRUSTED_HOST:-}" ]]; then
    PIP_FLAGS="--trusted-host pypi.org --trusted-host files.pythonhosted.org"
fi

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "Image ${IMAGE} not found — run ./gyanam.sh build first." >&2
    exit 1
fi

# Enforce the coverage floor only on a full run; subset runs (args given) skip it.
COV_GATE="--cov-fail-under=50"
if [[ $# -gt 0 ]]; then
    COV_GATE=""
fi
PYTEST_TARGET="${*:-tests/}"

echo "Running test suite in ${IMAGE} ..."
docker run --rm -w /app -e PYTHONPATH=/app \
    -v "${REPO_ROOT}/collector/src:/app/src:ro" \
    -v "${REPO_ROOT}/collector/tests:/app/tests:ro" \
    -v "${REPO_ROOT}/collector/config:/app/config:ro" \
    -v "${REPO_ROOT}/reference_artifacts:/app/reference_artifacts:ro" \
    -v "${REPO_ROOT}/collector/pytest.ini:/app/pytest.ini:ro" \
    -v "${REPO_ROOT}/collector/requirements-dev.txt:/app/requirements-dev.txt:ro" \
    "${IMAGE}" \
    sh -c "pip install --no-cache-dir -q ${PIP_FLAGS} pytest-cov pytest-httpx && python -m pytest --cov=src --cov-report=term-missing:skip-covered ${COV_GATE} ${PYTEST_TARGET}"
