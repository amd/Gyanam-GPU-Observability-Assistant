#!/bin/bash
# Check alert subscription status

echo "=== Alert Manager Status ===="
echo

# Check via API health endpoint
echo "Fetching status from API..."
docker compose exec api curl -s http://localhost:8080/health/detailed 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    alert_mgr = data.get('collector_service', {}).get('components', {}).get('alert_manager', {})

    print(f\"Alert Manager:\")
    print(f\"  Enabled: {alert_mgr.get('enabled')}\")
    print(f\"  Running: {alert_mgr.get('running')}\")
    print(f\"  Active Subscriptions: {alert_mgr.get('active_subscriptions')}\")
    print(f\"  SSE Subscriptions: {alert_mgr.get('sse_subscriptions', 'N/A')}\")
    print(f\"  Webhook Subscriptions: {alert_mgr.get('webhook_subscriptions', 'N/A')}\")
    print(f\"  Alerts Received: {alert_mgr.get('alerts_received')}\")
    print(f\"  Alerts Written: {alert_mgr.get('alerts_written')}\")
    print(f\"  Alerts Dropped: {alert_mgr.get('alerts_dropped')}\")
    print()
    print(f\"Subscribers ({len(alert_mgr.get('subscribers', []))}):\")
    for sub in alert_mgr.get('subscribers', []):
        print(f\"  - {sub.get('target_name')}:\")
        print(f\"      Type: {sub.get('subscription_type', 'unknown')}\")
        print(f\"      State: {sub.get('state', 'unknown')}\")
        if sub.get('subscription_type') == 'sse':
            print(f\"      Running: {sub.get('is_running')}\")
            print(f\"      Consecutive Failures: {sub.get('consecutive_failures')}\")
            print(f\"      Failure Reason: {sub.get('failure_reason', 'None')}\")
            time_in_state = sub.get('time_in_state_hours')
            if time_in_state is not None:
                print(f\"      Time in State: {time_in_state:.2f}h\")
            print(f\"      Last Event: {sub.get('last_event_time', 'Never')}\")
        elif sub.get('subscription_type') == 'webhook':
            print(f\"      Subscribed: {sub.get('is_subscribed')}\")
            print(f\"      Subscription ID: {sub.get('subscription_id')}\")
except Exception as e:
    print(f\"Error: {e}\")
    print(\"Raw data:\", file=sys.stderr)
    print(sys.stdin.read(), file=sys.stderr)
"

echo
echo "=== Recent SSE/Alert Logs ===="
docker compose logs collector --tail=50 2>&1 | grep -iE "sse|alert.*subscri|degraded|failed" | tail -20

echo
echo "=== Prometheus Metrics ===="
docker compose exec api curl -s http://localhost:8080/metrics 2>/dev/null | grep "gyanam_alert"
