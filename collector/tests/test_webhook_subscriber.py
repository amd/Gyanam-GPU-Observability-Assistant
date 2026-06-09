# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the webhook subscriber (Redfish EventService subscriptions)."""

from src.redfish.webhook_subscriber import (
    SubscriptionFailureType,
    WebhookSubscriber,
)

SUBS_URL = "https://bmc/redfish/v1/EventService/Subscriptions"


def _sub():
    return WebhookSubscriber(
        target_id=1,
        target_name="n1",
        target_bmc="10.0.0.1",
        base_url="https://bmc",
        username="u",
        password="p",
        webhook_url="http://collector:8081/redfish-webhook/1",
    )


async def test_create_subscription_success(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=SUBS_URL,
        status_code=201,
        json={"Id": "7"},
        headers={"Location": f"{SUBS_URL}/7"},
    )
    res = await _sub().create_subscription()
    assert res.success is True


async def test_create_subscription_permanent_400(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=SUBS_URL,
        status_code=400,
        text="PropertyValueFormatError: Destination invalid",
    )
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.PERMANENT


async def test_create_subscription_not_supported_501(httpx_mock):
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=501, text="Not Implemented")
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.PERMANENT


async def test_create_subscription_temporary_500(httpx_mock):
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=500, text="boom")
    res = await _sub().create_subscription()
    assert res.success is False
    assert res.failure_type == SubscriptionFailureType.TEMPORARY


async def test_create_conflict_finds_existing(httpx_mock):
    s = _sub()
    httpx_mock.add_response(method="POST", url=SUBS_URL, status_code=409)
    httpx_mock.add_response(
        method="GET",
        url=SUBS_URL,
        status_code=200,
        json={"Members": [{"@odata.id": "/redfish/v1/EventService/Subscriptions/9"}]},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://bmc/redfish/v1/EventService/Subscriptions/9",
        status_code=200,
        json={"Destination": s.webhook_url},
    )
    res = await s.create_subscription()
    assert res.success is True
    assert s.subscription_id == "9"


async def test_delete_subscription(httpx_mock):
    s = _sub()
    s._subscription_url = f"{SUBS_URL}/7"
    httpx_mock.add_response(method="DELETE", url=f"{SUBS_URL}/7", status_code=200)
    assert await s.delete_subscription() is True
    assert s.is_subscribed is False


async def test_verify_subscription(httpx_mock):
    s = _sub()
    s._subscription_url = f"{SUBS_URL}/7"
    httpx_mock.add_response(method="GET", url=f"{SUBS_URL}/7", status_code=200, json={})
    assert await s.verify_subscription() is True


def test_parse_webhook_event_filters_and_args():
    s = _sub()
    events = {
        "Events": [
            {
                "MessageSeverity": "Critical",
                "Message": "hot",
                "MessageId": "T.1",
                "MessageArgs": ["GPU0"],
            },
            {"MessageSeverity": "OK", "Message": "fine", "MessageId": "T.2"},  # filtered
        ]
    }
    alerts = s.parse_webhook_event(events)
    assert len(alerts) == 1
    assert alerts[0].severity == "Critical"
