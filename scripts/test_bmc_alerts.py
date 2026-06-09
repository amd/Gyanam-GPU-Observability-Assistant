#!/usr/bin/env python3
"""Test BMC alert capabilities - SSE and alternative methods."""

import asyncio
import sys

import httpx


async def test_sse_support(host: str, username: str, password: str):
    """Test if BMC supports SSE alerts."""
    print(f"\n{'='*60}")
    print(f"Testing BMC: {host}")
    print(f"{'='*60}\n")

    base_url = f"https://{host}"
    auth = httpx.BasicAuth(username, password)

    async with httpx.AsyncClient(auth=auth, verify=False, timeout=10.0) as client:
        # Step 1: Check EventService
        print("1. Checking EventService...")
        try:
            response = await client.get(f"{base_url}/redfish/v1/EventService")
            if response.status_code == 200:
                event_service = response.json()
                print("   ✅ EventService found")
                print(f"   - ServiceEnabled: {event_service.get('ServiceEnabled')}")
                print(f"   - Status: {event_service.get('Status', {}).get('State')}")

                sse_uri = event_service.get("ServerSentEventUri")
                if sse_uri:
                    print(f"   - SSE URI: {sse_uri}")
                else:
                    print("   - SSE URI: Not advertised")

                event_types = event_service.get("EventTypesForSubscription", [])
                print(f"   - Event Types: {', '.join(event_types) if event_types else 'None'}")

                # Check subscriptions support
                subscriptions = event_service.get("Subscriptions", {})
                print(f"   - Subscriptions endpoint: {subscriptions.get('@odata.id', 'N/A')}")

            elif response.status_code == 404:
                print("   ❌ EventService not found (404)")
                return await test_alternative_methods(host, username, password, client)
            else:
                print(f"   ⚠️  EventService returned {response.status_code}")
                return await test_alternative_methods(host, username, password, client)
        except Exception as e:
            print(f"   ❌ Error: {e}")
            return await test_alternative_methods(host, username, password, client)

        # Step 2: Test SSE endpoint
        print("\n2. Testing SSE endpoint...")
        sse_endpoint = event_service.get("ServerSentEventUri", "/redfish/v1/EventService/SSE")
        if not sse_endpoint.startswith("http"):
            sse_endpoint = f"{base_url}{sse_endpoint}"

        try:
            timeout = httpx.Timeout(10.0, read=5.0)
            client_sse = httpx.AsyncClient(auth=auth, verify=False, timeout=timeout)

            print(f"   Connecting to {sse_endpoint}...")
            response = await client_sse.get(sse_endpoint, headers={"Accept": "text/event-stream"})

            if response.status_code == 200:
                print("   ✅ SSE endpoint responded with 200")
                content_type = response.headers.get("content-type", "")
                print(f"   - Content-Type: {content_type}")

                if "text/event-stream" in content_type.lower():
                    print("   ✅ Correct content-type for SSE")
                    print("\n   Testing SSE stream (5 second test)...")

                    # Read stream for 5 seconds
                    try:
                        lines_received = []
                        async for line in response.aiter_lines():
                            lines_received.append(line)
                            if len(lines_received) >= 10:  # First 10 lines
                                break

                        if lines_received:
                            print(f"   ✅ Received {len(lines_received)} lines:")
                            for i, line in enumerate(lines_received[:5], 1):
                                print(f"      Line {i}: {line[:80]}")

                            # Check for keep-alives or events
                            has_keepalive = any(line.startswith(":") for line in lines_received)
                            has_data = any(line.startswith("data:") for line in lines_received)

                            if has_data:
                                print("   ✅ SSE WORKING - Received event data!")
                            elif has_keepalive:
                                print("   ✅ SSE WORKING - Received keep-alive comments")
                            else:
                                print("   ⚠️  Stream active but no standard SSE format detected")
                        else:
                            print("   ❌ No data received from stream")
                    except TimeoutError:
                        print("   ⚠️  Stream timeout - may be working but no data sent")
                else:
                    print("   ❌ Wrong content-type, expected text/event-stream")
            elif response.status_code == 404:
                print("   ❌ SSE endpoint not found (404)")
            elif response.status_code == 501:
                print("   ❌ SSE not implemented (501)")
            else:
                print(f"   ❌ SSE endpoint returned {response.status_code}")

        except httpx.TimeoutException:
            print("   ⚠️  Connection timeout")
        except Exception as e:
            print(f"   ❌ Error: {e}")

        # Step 3: Check alternative methods
        return await test_alternative_methods(host, username, password, client)


async def test_alternative_methods(host: str, username: str, password: str, client=None):
    """Test alternative alert collection methods."""
    print("\n3. Checking alternative alert methods...")

    base_url = f"https://{host}"
    if client is None:
        auth = httpx.BasicAuth(username, password)
        client = httpx.AsyncClient(auth=auth, verify=False, timeout=10.0)
        close_client = True
    else:
        close_client = False

    alternatives_found = []

    try:
        # Method 1: Event Log Entries
        print("\n   Method 1: EventLog via LogServices")
        try:
            # Try System log first
            log_url = f"{base_url}/redfish/v1/Systems/1/LogServices/EventLog/Entries"
            response = await client.get(log_url)
            if response.status_code == 200:
                log_data = response.json()
                entries = log_data.get("Members", [])
                print("   ✅ EventLog found via LogServices")
                print(
                    f"   - Entries available: {log_data.get('Members@odata.count', len(entries))}"
                )
                if entries:
                    latest = entries[0]
                    print(f"   - Latest entry: {latest.get('Message', 'N/A')[:60]}...")
                    print(f"   - Severity: {latest.get('Severity', 'N/A')}")
                alternatives_found.append("EventLog via /Systems/1/LogServices/EventLog/Entries")
            elif response.status_code == 404:
                # Try Manager log
                log_url = f"{base_url}/redfish/v1/Managers/1/LogServices/EventLog/Entries"
                response = await client.get(log_url)
                if response.status_code == 200:
                    log_data = response.json()
                    entries = log_data.get("Members", [])
                    print("   ✅ EventLog found via Manager LogServices")
                    print(
                        f"   - Entries available: {log_data.get('Members@odata.count', len(entries))}"
                    )
                    alternatives_found.append(
                        "EventLog via /Managers/1/LogServices/EventLog/Entries"
                    )
                else:
                    print("   ❌ EventLog not found (tried System and Manager)")
            else:
                print(f"   ⚠️  EventLog returned {response.status_code}")
        except Exception as e:
            print(f"   ❌ Error checking EventLog: {e}")

        # Method 2: SEL (System Event Log) - IPMI style
        print("\n   Method 2: SEL (System Event Log)")
        try:
            sel_url = f"{base_url}/redfish/v1/Systems/1/LogServices/SEL/Entries"
            response = await client.get(sel_url)
            if response.status_code == 200:
                sel_data = response.json()
                entries = sel_data.get("Members", [])
                print("   ✅ SEL found")
                print(
                    f"   - Entries available: {sel_data.get('Members@odata.count', len(entries))}"
                )
                alternatives_found.append("SEL via /Systems/1/LogServices/SEL/Entries")
            elif response.status_code == 404:
                print("   ❌ SEL not found")
            else:
                print(f"   ⚠️  SEL returned {response.status_code}")
        except Exception as e:
            print(f"   ❌ Error checking SEL: {e}")

        # Method 3: Task Monitor
        print("\n   Method 3: TaskService/Tasks")
        try:
            task_url = f"{base_url}/redfish/v1/TaskService/Tasks"
            response = await client.get(task_url)
            if response.status_code == 200:
                task_data = response.json()
                print("   ✅ TaskService found")
                print(f"   - Active tasks: {task_data.get('Members@odata.count', 0)}")
                alternatives_found.append("Tasks via /TaskService/Tasks")
            elif response.status_code == 404:
                print("   ❌ TaskService not found")
        except Exception as e:
            print(f"   ❌ Error checking TaskService: {e}")

        # Method 4: Check if we can create subscriptions
        print("\n   Method 4: Event Subscriptions (push model)")
        try:
            sub_url = f"{base_url}/redfish/v1/EventService/Subscriptions"
            response = await client.get(sub_url)
            if response.status_code == 200:
                sub_data = response.json()
                print("   ✅ Subscriptions endpoint found")
                print(f"   - Active subscriptions: {sub_data.get('Members@odata.count', 0)}")
                print(
                    "   Note: Could create webhook subscription (push alerts to external endpoint)"
                )
                alternatives_found.append("Webhook subscriptions via /EventService/Subscriptions")
            elif response.status_code == 404:
                print("   ❌ Subscriptions not supported")
        except Exception as e:
            print(f"   ❌ Error checking Subscriptions: {e}")

    finally:
        if close_client:
            await client.aclose()

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}\n")

    if alternatives_found:
        print("✅ Alternative alert collection methods available:")
        for i, method in enumerate(alternatives_found, 1):
            print(f"   {i}. {method}")

        print("\nRECOMMENDATION:")
        if "EventLog" in alternatives_found[0]:
            print("   Use polling-based collection:")
            print("   - Poll EventLog entries every 1-5 minutes")
            print("   - Track last seen entry ID to get only new alerts")
            print("   - Filter by Severity (Warning, Critical)")
    else:
        print("❌ No alternative alert methods found")
        print("\nRECOMMENDATION:")
        print("   This BMC may not support Redfish alerts at all")
        print("   Check BMC documentation or firmware version")


async def main():
    if len(sys.argv) != 4:
        print("Usage: python test_bmc_alerts.py <host> <username> <password>")
        print("\nExample:")
        print("  python test_bmc_alerts.py 10.190.174.96 ADMIN ADMIN")
        sys.exit(1)

    host = sys.argv[1]
    username = sys.argv[2]
    password = sys.argv[3]

    await test_sse_support(host, username, password)


if __name__ == "__main__":
    asyncio.run(main())
