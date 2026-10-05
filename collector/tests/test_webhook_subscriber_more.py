# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Additional coverage for the webhook subscriber error/edge paths."""

import httpx
from src.redfish.webhook_subscriber import (
    SubscriptionFailureType,
    WebhookSubscriber,
)

SUBS_URL = "https://bmc/redfish/v1/EventService/Subscriptions"


def _sub(webhook_url: str = "http://collector:8081/redfish-webhook/1"):
    return WebhookSubscriber(
        target_id=1,
        target_name="n1",
        target_bmc="10.0.0.1",
        base_url="https://bmc",
        username="u",
        password="p",
        webhook_url=webhook_url,
    )


# ---- create_subscription ------------------------------------------------


async def test_create_subscription_success_200_default_location(httpx_mock):
    # 200 (not 201) and no Location header -> URL is synthesized from the Id.
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=200, json={"Id": "42"})
    s = _sub()
    res = await s.create_subscription()
    assert res.success is True
    assert s.subscription_id == "42"
    assert s.is_subscribed is True
    assert s._subscription_url.endswith("/Subscriptions/42")


async def test_create_conflict_no_existing_is_temporary(httpx_mock):
    # 409 then the subscriptions list is empty -> cannot adopt -> TEMPORARY.
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=409)
    httpx_mock.add_response(method="GET", url=SUBS_URL, status_code=200, json={"Members": []})
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.TEMPORARY
    assert "409" in res.error_message


async def test_create_400_localhost_is_permanent(httpx_mock):
    # Generic 400 body, but a localhost destination can never be reached by the
    # BMC -> classified PERMANENT.
    httpx_mock.add_response(
        method="POST", url=SUBS_URL, status_code=400, text="Something went wrong"
    )
    res = await _sub("http://localhost:8081/redfish-webhook/1").create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.PERMANENT


async def test_create_400_generic_is_temporary(httpx_mock):
    # 400 with no known-permanent marker and a routable destination -> TEMPORARY.
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=400, text="transient hiccup")
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.TEMPORARY


async def test_create_405_not_supported_is_permanent(httpx_mock):
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=405, text="Nope")
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.PERMANENT


async def test_create_other_status_is_temporary(httpx_mock):
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=503, text="busy")
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.TEMPORARY


async def test_create_connect_error_is_temporary(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("refused"), method="POST", url=SUBS_URL)
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.TEMPORARY
    assert "ConnectError" in res.error_message


async def test_create_unexpected_error_is_temporary(httpx_mock):
    # A non-network error (e.g. malformed JSON on a 200) is still swallowed and
    # reported as TEMPORARY to be safe.
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=200, content=b"not json")
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.TEMPORARY


# ---- _find_existing_subscription ----------------------------------------


async def test_find_existing_matches_destination(httpx_mock):
    s = _sub()
    httpx_mock.add_response(
        method="GET",
        url=SUBS_URL,
        status_code=200,
        json={
            "Members": [
                {"@odata.id": "/redfish/v1/EventService/Subscriptions/3"},
                {"@odata.id": "/redfish/v1/EventService/Subscriptions/4"},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{SUBS_URL}/3",
        status_code=200,
        json={"Destination": "http://other/endpoint"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{SUBS_URL}/4",
        status_code=200,
        json={"Destination": s.webhook_url},
    )
    await s._find_existing_subscription()
    assert s.subscription_id == "4"
    assert s._subscription_url == f"{SUBS_URL}/4"


async def test_find_existing_swallows_errors(httpx_mock):
    s = _sub()
    httpx_mock.add_exception(httpx.ConnectError("down"), method="GET", url=SUBS_URL)
    await s._find_existing_subscription()  # must not raise
    assert s.subscription_id is None


# ---- delete_subscription ------------------------------------------------


async def test_delete_no_url_returns_false():
    assert await _sub().delete_subscription() is False


async def test_delete_404_treated_as_deleted(httpx_mock):
    s = _sub()
    s._subscription_url = f"{SUBS_URL}/7"
    httpx_mock.add_response(method="DELETE", url=f"{SUBS_URL}/7", status_code=404)
    assert await s.delete_subscription() is True
    assert s.is_subscribed is False


async def test_delete_failure_status_returns_false(httpx_mock):
    s = _sub()
    s._subscription_url = f"{SUBS_URL}/7"
    httpx_mock.add_response(method="DELETE", url=f"{SUBS_URL}/7", status_code=500)
    assert await s.delete_subscription() is False
    # URL retained so a later retry can try again.
    assert s._subscription_url == f"{SUBS_URL}/7"


async def test_delete_exception_returns_false(httpx_mock):
    s = _sub()
    s._subscription_url = f"{SUBS_URL}/7"
    httpx_mock.add_exception(httpx.ConnectError("boom"), method="DELETE", url=f"{SUBS_URL}/7")
    assert await s.delete_subscription() is False


# ---- verify_subscription ------------------------------------------------


async def test_verify_no_url_returns_false():
    assert await _sub().verify_subscription() is False


async def test_verify_non_200_returns_false(httpx_mock):
    s = _sub()
    s._subscription_url = f"{SUBS_URL}/7"
    httpx_mock.add_response(method="GET", url=f"{SUBS_URL}/7", status_code=404)
    assert await s.verify_subscription() is False


async def test_verify_exception_returns_false(httpx_mock):
    s = _sub()
    s._subscription_url = f"{SUBS_URL}/7"
    httpx_mock.add_exception(httpx.ConnectError("boom"), method="GET", url=f"{SUBS_URL}/7")
    assert await s.verify_subscription() is False


# ---- parse_webhook_event ------------------------------------------------


def test_parse_drops_event_type_not_subscribed():
    s = _sub()  # event_types default: Alert, StatusChange
    alerts = s.parse_webhook_event(
        {
            "Events": [
                {
                    "EventType": "ResourceAdded",
                    "Severity": "Critical",
                    "Message": "m",
                }
            ]
        }
    )
    assert alerts == []


def test_parse_skips_non_dict_events_and_sets_default_type():
    s = _sub()
    alerts = s.parse_webhook_event(
        {
            "Events": [
                "not-a-dict",
                {"Severity": "Critical", "Message": "ok", "MessageId": "M1"},
            ]
        }
    )
    assert len(alerts) == 1
    assert alerts[0].event_type == "Alert"  # absent EventType -> default


def test_parse_origin_as_bare_string():
    s = _sub()
    alerts = s.parse_webhook_event(
        {
            "Events": [
                {
                    "EventType": "Alert",
                    "Severity": "Warning",
                    "Message": "m",
                    "OriginOfCondition": "/redfish/v1/Systems/1",
                }
            ]
        }
    )
    assert alerts[0].origin_of_condition == "/redfish/v1/Systems/1"
