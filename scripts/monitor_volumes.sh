#!/bin/bash
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# monitor_volumes.sh - Display current Docker volume usage and disk space

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
PROJECT_NAME="$(basename "${PROJECT_DIR}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/_/g')"

# Colors
_GREEN='\033[0;32m'
_YELLOW='\033[1;33m'
_RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== Docker Volume Usage ===${NC}"
docker volume inspect ${PROJECT_NAME}_influxdb-data ${PROJECT_NAME}_grafana-data ${PROJECT_NAME}_shared-data 2>/dev/null | \
  jq -r '.[] | "\(.Name): \(.Mountpoint)"' 2>/dev/null | \
  while IFS=: read -r name path; do
    if [ -n "$path" ]; then
      # Remove leading/trailing whitespace from path
      path=$(echo "$path" | xargs)
      # Use timeout to prevent hanging on large volumes (max 60 seconds)
      size=$(timeout 60 sudo du -sh "$path" 2>/dev/null | cut -f1)
      if [ $? -eq 124 ]; then
        size="timeout"
      elif [ -z "$size" ]; then
        size="N/A"
      fi
      echo "  $name: $size"
    fi
  done || {
    # Fallback if jq not available
    for vol in ${PROJECT_NAME}_influxdb-data ${PROJECT_NAME}_grafana-data ${PROJECT_NAME}_shared-data; do
      if docker volume inspect "$vol" &>/dev/null; then
        echo "  $vol: (install jq for size details)"
      fi
    done
  }

echo ""
echo -e "${BLUE}=== Disk Space on Docker Partition ===${NC}"
df -h /var/lib/docker | awk 'NR==1 {print "  " $0} NR>1 {
  use = int($5)
  if (use >= 90) color = "\033[0;31m"      # Red
  else if (use >= 80) color = "\033[1;33m" # Yellow
  else color = "\033[0;32m"                # Green
  printf "  " color "%s\033[0m\n", $0
}'

echo ""
echo -e "${BLUE}=== Total Docker Usage ===${NC}"
docker system df

echo ""
echo -e "${BLUE}=== Docker System Info ===${NC}"
docker system df -v | grep -A 20 "Local Volumes" | grep "${PROJECT_NAME}" || echo "  No ${PROJECT_NAME} volumes found in verbose output"
