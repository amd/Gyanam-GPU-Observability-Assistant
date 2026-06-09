#!/bin/bash
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# alert_disk_space.sh - Alert when Docker disk usage exceeds threshold
#
# Usage: Add to crontab to run periodically (e.g., every 15 minutes)
#   */15 * * * * /path/to/gyanam/scripts/alert_disk_space.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

# Alert threshold (percentage)
THRESHOLD_PERCENT=${DISK_ALERT_THRESHOLD:-80}

# Get disk usage percentage for Docker partition
DISK_USAGE=$(df --output=pcent /var/lib/docker 2>/dev/null | tail -1 | tr -d ' %')

if [ -z "$DISK_USAGE" ]; then
  echo "ERROR: Could not determine disk usage for /var/lib/docker"
  exit 1
fi

if [ "$DISK_USAGE" -gt "$THRESHOLD_PERCENT" ]; then
  ALERT_MSG="WARNING: Docker disk usage at ${DISK_USAGE}% (threshold: ${THRESHOLD_PERCENT}%)"

  # Log to syslog if available
  if command -v logger &>/dev/null; then
    logger -t gyanam-monitor "$ALERT_MSG"
  fi

  # Print to stdout (will be captured by cron and emailed if configured)
  echo "$(date '+%Y-%m-%d %H:%M:%S') - $ALERT_MSG"

  # Show current volume sizes
  echo ""
  echo "Current volume usage:"
  "$SCRIPT_DIR/monitor_volumes.sh" 2>/dev/null || echo "  (run monitor_volumes.sh manually for details)"

  # Optional: Send to external alerting system
  # Uncomment and configure one of these:

  # Slack webhook
  # SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
  # curl -X POST "$SLACK_WEBHOOK_URL" \
  #   -H 'Content-Type: application/json' \
  #   -d "{\"text\":\"$ALERT_MSG\"}"

  # Email (requires mailx or sendmail configured)
  # echo "$ALERT_MSG" | mail -s "Gyanam Disk Alert" your-email@example.com

  # PagerDuty, Datadog, etc.
  # Add your preferred alerting mechanism here

  exit 1
fi

exit 0
