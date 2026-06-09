#!/bin/bash
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# log_volume_growth.sh - Track Docker volume growth over time
#
# Usage: Add to crontab to run periodically (e.g., every hour)
#   0 * * * * /path/to/gyanam/scripts/log_volume_growth.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
PROJECT_NAME="$(basename "${PROJECT_DIR}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/_/g')"
LOG_FILE="${PROJECT_DIR}/docker_volume_growth.log"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# Create log file with header if it doesn't exist
if [ ! -f "$LOG_FILE" ]; then
  echo "# Docker Volume Growth Log - ${PROJECT_NAME}" > "$LOG_FILE"
  echo "# Format: timestamp,volume_name,size_bytes" >> "$LOG_FILE"
fi

# Get volume sizes
for vol in ${PROJECT_NAME}_influxdb-data ${PROJECT_NAME}_grafana-data ${PROJECT_NAME}_shared-data; do
  if docker volume inspect "$vol" &>/dev/null; then
    MOUNTPOINT=$(docker volume inspect "$vol" 2>/dev/null | grep -o '"Mountpoint": "[^"]*' | cut -d'"' -f4)
    if [ -n "$MOUNTPOINT" ] && [ "$MOUNTPOINT" != "null" ]; then
      # Use timeout to prevent hanging on large volumes (max 60 seconds)
      SIZE=$(timeout 60 sudo du -sb "$MOUNTPOINT" 2>/dev/null | cut -f1)
      if [ $? -eq 124 ]; then
        # Timeout occurred - log warning
        logger -t gyanam-monitor "WARNING: du timeout for $vol (>60s)" 2>/dev/null || true
      elif [ -n "$SIZE" ]; then
        echo "$TIMESTAMP,$vol,$SIZE" >> "$LOG_FILE"
      fi
    fi
  fi
done

# Get disk space
DISK_AVAIL=$(df --output=avail -B1 /var/lib/docker 2>/dev/null | tail -1)
if [ -n "$DISK_AVAIL" ]; then
  echo "$TIMESTAMP,disk_available,$DISK_AVAIL" >> "$LOG_FILE"
fi

# Optional: Keep only last 90 days of logs (to prevent log file from growing forever)
if [ -f "$LOG_FILE" ]; then
  NINETY_DAYS_AGO=$(date -d '90 days ago' '+%Y-%m-%d' 2>/dev/null || date -v-90d '+%Y-%m-%d' 2>/dev/null)
  if [ -n "$NINETY_DAYS_AGO" ]; then
    grep -v "^${NINETY_DAYS_AGO}" "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null && mv "${LOG_FILE}.tmp" "$LOG_FILE"
  fi
fi
