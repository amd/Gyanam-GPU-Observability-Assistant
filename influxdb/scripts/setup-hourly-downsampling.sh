#!/bin/bash
#
# Creates the hourly downsampled bucket and aggregation task in InfluxDB.
# This aggregates 15-minute data into hourly averages for long-term storage.
#

set -e

INFLUX_HOST="${INFLUXDB_URL:-http://localhost:8086}"
INFLUX_TOKEN="${INFLUXDB_TOKEN}"
INFLUX_ORG="${INFLUXDB_ORG:-prometheus}"
SOURCE_BUCKET="gpu_metrics_downsampled"
HOURLY_BUCKET="gpu_metrics_hourly"
HOURLY_RETENTION="${INFLUXDB_HOURLY_RETENTION:-90d}"

echo "Setting up hourly downsampling: ${SOURCE_BUCKET} -> ${HOURLY_BUCKET}"

# Wait for InfluxDB to be ready
for i in $(seq 1 30); do
    if curl -sf "${INFLUX_HOST}/ping" > /dev/null 2>&1; then
        break
    fi
    echo "Waiting for InfluxDB... ($i/30)"
    sleep 2
done

# Create the hourly bucket (idempotent — ignores if exists)
echo "Creating bucket: ${HOURLY_BUCKET} (retention: ${HOURLY_RETENTION})"
influx bucket create \
    --host "${INFLUX_HOST}" \
    --token "${INFLUX_TOKEN}" \
    --name "${HOURLY_BUCKET}" \
    --retention "${HOURLY_RETENTION}" \
    2>/dev/null || echo "  Bucket already exists, skipping."

# Create the hourly downsampling task (delete existing first for idempotency)
TASK_NAME="downsample_gpu_metrics_hourly"
echo "Creating task: ${TASK_NAME}"

# Delete existing task if present
EXISTING_TASK_ID=$(influx task list \
    --host "${INFLUX_HOST}" \
    --token "${INFLUX_TOKEN}" \
    2>/dev/null | grep "${TASK_NAME}" | awk '{print $1}')

if [ -n "${EXISTING_TASK_ID}" ]; then
    echo "  Removing existing task ${EXISTING_TASK_ID}..."
    influx task delete \
        --host "${INFLUX_HOST}" \
        --token "${INFLUX_TOKEN}" \
        --id "${EXISTING_TASK_ID}" 2>/dev/null || true
fi

# Create the Flux task
TASK_FILE="$(pwd)/_gyanam_hourly_task.flux"
cat > "${TASK_FILE}" <<FLUX_EOF
option task = {name: "${TASK_NAME}", every: 1h, offset: 5m}

// Aggregate 15-minute data to hourly
// Process min, avg, and max separately with appropriate aggregation functions
from(bucket: "${SOURCE_BUCKET}")
    |> range(start: -task.every)
    |> filter(fn: (r) => r._measurement == "gpu_metrics_15m")
    |> filter(fn: (r) => r._field =~ /_min\$/)
    |> aggregateWindow(every: 1h, fn: min, createEmpty: false)
    |> map(fn: (r) => ({ r with _measurement: "gpu_metrics_1h" }))
    |> to(bucket: "${HOURLY_BUCKET}", org: "${INFLUX_ORG}")

from(bucket: "${SOURCE_BUCKET}")
    |> range(start: -task.every)
    |> filter(fn: (r) => r._measurement == "gpu_metrics_15m")
    |> filter(fn: (r) => r._field =~ /_avg\$/)
    |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    |> map(fn: (r) => ({ r with _measurement: "gpu_metrics_1h" }))
    |> to(bucket: "${HOURLY_BUCKET}", org: "${INFLUX_ORG}")

from(bucket: "${SOURCE_BUCKET}")
    |> range(start: -task.every)
    |> filter(fn: (r) => r._measurement == "gpu_metrics_15m")
    |> filter(fn: (r) => r._field =~ /_max\$/)
    |> aggregateWindow(every: 1h, fn: max, createEmpty: false)
    |> map(fn: (r) => ({ r with _measurement: "gpu_metrics_1h" }))
    |> to(bucket: "${HOURLY_BUCKET}", org: "${INFLUX_ORG}")
FLUX_EOF

echo "  Flux task written to ${TASK_FILE}"

if influx task create \
    --host "${INFLUX_HOST}" \
    --token "${INFLUX_TOKEN}" \
    --file "${TASK_FILE}"; then
    rm -f "${TASK_FILE}"
else
    rm -f "${TASK_FILE}"
    echo "ERROR: Failed to create hourly downsampling task"
    exit 1
fi

echo "Hourly downsampling setup complete!"
echo "  Source: ${SOURCE_BUCKET} -> Target: ${HOURLY_BUCKET}"
echo "  Interval: 1 hour | Retention: ${HOURLY_RETENTION}"
echo "  Aggregations: min, avg, max (from 15-min data)"
echo ""
echo "Current tiered storage configuration:"
echo "  - Raw data: 7 days"
echo "  - 15-min aggregations: 30 days"
echo "  - Hourly aggregations: ${HOURLY_RETENTION}"
