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

# ---------------------------------------------------------------------------
# Stage 1: build OpenBMC libcper -> cper-convert (CPER binary -> JSON decoder).
# CPER (Common Platform Error Record, UEFI Spec Appendix N) attachments from
# BMC LogEntries are decoded into human-readable alert messages at runtime.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS cper-builder

# Pinned libcper revision (OpenBMC). Bump deliberately.
ARG LIBCPER_REF=b5fa948036741b3ffc68bdbf8904d5030cb41e2e

# Behind a TLS-intercepting corporate proxy the git clone can't verify GitHub's
# cert chain; set PIP_TRUSTED_HOST=1 (same knob as the pip install) to relax TLS
# for the clone only. Otherwise the clone uses full verification.
ARG PIP_TRUSTED_HOST=""

RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential meson ninja-build pkg-config libjson-c-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git ${PIP_TRUSTED_HOST:+-c http.sslVerify=false} \
        clone https://github.com/openbmc/libcper.git /src/libcper \
    && cd /src/libcper \
    && git checkout "${LIBCPER_REF}" \
    && meson setup build \
    && ninja -C build

# ---------------------------------------------------------------------------
# Stage 2: runtime image.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies. libjson-c5 is the only runtime dep of cper-convert.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    libjson-c5 \
    && rm -rf /var/lib/apt/lists/*

# CPER decoder binary + its shared libraries from the builder stage.
COPY --from=cper-builder /src/libcper/build/cper-convert /usr/local/bin/cper-convert
COPY --from=cper-builder /src/libcper/build/*.so* /usr/local/lib/
RUN ldconfig

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies.
# Secure by default (full TLS verification). Behind a TLS-intercepting corporate
# proxy where PyPI's cert chain can't be verified, opt in by building with
# --build-arg PIP_TRUSTED_HOST=1 (docker-compose reads it from the PIP_TRUSTED_HOST
# env var). It only relaxes cert verification for PyPI hosts during the build.
ARG PIP_TRUSTED_HOST=""
RUN pip install --no-cache-dir \
    ${PIP_TRUSTED_HOST:+--trusted-host pypi.org --trusted-host files.pythonhosted.org} \
    -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY config/ ./config/

# Create data directory for SQLite
RUN mkdir -p /app/data

# Create temp directory for blob extraction
RUN mkdir -p /tmp/telemetry

# Set Python path
ENV PYTHONPATH=/app

# Expose port for Config UI
EXPOSE 8080

# No healthcheck for collector service (it's a background process without HTTP endpoints)
# The API service has its own healthcheck

# Default command (overridden by docker-compose for api/collector services)
CMD ["python", "-m", "src.collector_main"]
