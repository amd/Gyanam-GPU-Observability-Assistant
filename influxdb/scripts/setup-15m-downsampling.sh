#!/bin/bash
#
# Creates the 15-minute downsampled bucket and aggregation task in InfluxDB.
# Run once after InfluxDB is initialized, or via docker-compose entrypoint.
#

set -e

INFLUX_HOST="${INFLUXDB_URL:-http://localhost:8086}"
INFLUX_TOKEN="${INFLUXDB_TOKEN}"
INFLUX_ORG="${INFLUXDB_ORG:-prometheus}"
SOURCE_BUCKET="${INFLUXDB_BUCKET:-gpu_metrics}"
DOWNSAMPLE_BUCKET="gpu_metrics_downsampled"
DOWNSAMPLE_RETENTION="${INFLUXDB_15M_RETENTION:-30d}"

echo "Setting up 15-minute downsampling: ${SOURCE_BUCKET} -> ${DOWNSAMPLE_BUCKET}"

# Wait for InfluxDB to be ready
for i in $(seq 1 30); do
    if curl -sf "${INFLUX_HOST}/ping" > /dev/null 2>&1; then
        break
    fi
    echo "Waiting for InfluxDB... ($i/30)"
    sleep 2
done

# Create the downsampled bucket (idempotent — ignores if exists)
echo "Creating bucket: ${DOWNSAMPLE_BUCKET} (retention: ${DOWNSAMPLE_RETENTION})"
influx bucket create \
    --host "${INFLUX_HOST}" \
    --token "${INFLUX_TOKEN}" \
    --org "${INFLUX_ORG}" \
    --name "${DOWNSAMPLE_BUCKET}" \
    --retention "${DOWNSAMPLE_RETENTION}" \
    2>/dev/null || echo "  Bucket already exists, skipping."

# Create the downsampling task (delete existing first for idempotency)
TASK_NAME="downsample_gpu_metrics_15m"
echo "Creating task: ${TASK_NAME}"

# Delete existing task if present
EXISTING_TASK_ID=$(influx task list \
    --host "${INFLUX_HOST}" \
    --token "${INFLUX_TOKEN}" \
    --org "${INFLUX_ORG}" \
    2>/dev/null | grep "${TASK_NAME}" | awk '{print $1}')

if [ -n "${EXISTING_TASK_ID}" ]; then
    echo "  Removing existing task ${EXISTING_TASK_ID}..."
    influx task delete \
        --host "${INFLUX_HOST}" \
        --token "${INFLUX_TOKEN}" \
        --id "${EXISTING_TASK_ID}" 2>/dev/null || true
fi

# Create the Flux task via temp file (influx CLI reads Flux from file, not --flux flag)
TASK_FILE="$(pwd)/_gyanam_downsample_task.flux"
cat > "${TASK_FILE}" <<FLUX_EOF
option task = {name: "${TASK_NAME}", every: 15m, offset: 1m}

// Source data from the last 15 minutes
// Note: In this schema, _measurement is the metric name (e.g., "gpu_die_temp_celsius")
// and _field is always "value"
data = from(bucket: "${SOURCE_BUCKET}")
    |> range(start: -task.every)
    |> filter(fn: (r) =>
        r._field == "value" and
        (r._measurement == "gpu_die_temp_celsius" or
         r._measurement == "gpu_memory_temp_celsius" or
         r._measurement == "hbm_vr_temp_celsius" or
         r._measurement == "consolidated_temp_celsius" or
         r._measurement == "vr_temp_celsius" or
         r._measurement == "hsc_temp_celsius" or
         r._measurement == "ibc_temp_celsius" or
         r._measurement == "board_temp_celsius" or
         r._measurement == "gpu_warmest_temp_celsius" or
         r._measurement == "gpu_power_watts" or
         r._measurement == "gpu_total_power_watts" or
         r._measurement == "board_power_watts")
    )

// Transform to match dashboard expectations:
// - Set _measurement to "gpu_metrics_15m"
// - Set _field to original_measurement + "_min/_max/_avg"
// This matches what the Grafana dashboard queries expect

// Compute min
data
    |> aggregateWindow(every: task.every, fn: min, createEmpty: false)
    |> map(fn: (r) => ({
        r with
        _field: r._measurement + "_min",
        _measurement: "gpu_metrics_15m"
    }))
    |> to(bucket: "${DOWNSAMPLE_BUCKET}", org: "${INFLUX_ORG}")

// Compute max
data
    |> aggregateWindow(every: task.every, fn: max, createEmpty: false)
    |> map(fn: (r) => ({
        r with
        _field: r._measurement + "_max",
        _measurement: "gpu_metrics_15m"
    }))
    |> to(bucket: "${DOWNSAMPLE_BUCKET}", org: "${INFLUX_ORG}")

// Compute mean (avg)
data
    |> aggregateWindow(every: task.every, fn: mean, createEmpty: false)
    |> map(fn: (r) => ({
        r with
        _field: r._measurement + "_avg",
        _measurement: "gpu_metrics_15m"
    }))
    |> to(bucket: "${DOWNSAMPLE_BUCKET}", org: "${INFLUX_ORG}")
FLUX_EOF

echo "  Flux task written to ${TASK_FILE}"

if influx task create \
    --host "${INFLUX_HOST}" \
    --token "${INFLUX_TOKEN}" \
    --org "${INFLUX_ORG}" \
    --file "${TASK_FILE}"; then
    rm -f "${TASK_FILE}"
else
    rm -f "${TASK_FILE}"
    echo "ERROR: Failed to create downsampling task"
    exit 1
fi

echo "Downsampling setup complete!"
echo "  Source: ${SOURCE_BUCKET} -> Target: ${DOWNSAMPLE_BUCKET}"
echo "  Interval: 15 minutes | Retention: ${DOWNSAMPLE_RETENTION}"
echo "  Metrics: temperature (die, memory, VR, HSC, IBC, board), power"
echo "  Aggregations: min, max, avg"
