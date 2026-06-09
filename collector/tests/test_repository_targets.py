# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for TargetRepository target CRUD + credential encryption."""


async def _make(repo, **kw):
    kw.setdefault("name", "gpu1")
    kw.setdefault("host", "10.0.0.9")
    kw.setdefault("username", "admin")
    kw.setdefault("password", "pw")
    return await repo.create_target(**kw)


async def test_create_encrypts_password_and_reads_back(repo):
    t = await _make(repo, password="s3cret")
    assert t.id is not None
    # Stored password is encrypted, not plaintext.
    assert t.encrypted_password != "s3cret"
    assert repo.decrypt_password(t) == "s3cret"


async def test_get_by_host_and_id(repo):
    t = await _make(repo, host="10.1.1.1")
    assert (await repo.get_target_by_host("10.1.1.1")).id == t.id
    assert (await repo.get_target(t.id)).host == "10.1.1.1"
    assert await repo.get_target_by_host("10.9.9.9") is None
    assert await repo.get_target(999999) is None


async def test_get_all_targets_enabled_only(repo):
    await _make(repo, name="a", host="10.0.0.1", enabled=True)
    await _make(repo, name="b", host="10.0.0.2", enabled=False)
    assert len(await repo.get_all_targets()) == 2
    enabled = await repo.get_all_targets(enabled_only=True)
    assert [t.host for t in enabled] == ["10.0.0.1"]


async def test_update_target(repo):
    t = await _make(repo)
    updated = await repo.update_target(t.id, name="renamed", enabled=False)
    assert updated.name == "renamed"
    assert updated.enabled is False
    assert await repo.update_target(999999, name="x") is None


async def test_delete_target(repo):
    t = await _make(repo)
    assert await repo.delete_target(t.id) is True
    assert await repo.get_target(t.id) is None
    assert await repo.delete_target(t.id) is False


async def test_ssh_proxy_host_lookup(repo):
    t = await _make(
        repo,
        name="proxy",
        host="10.5.5.5",
        connection_mode="ssh_proxy",
        ssh_proxy_host="proxy.example.com",
        ssh_proxy_username="root",
        ssh_password="k",
    )
    found = await repo.get_target_by_ssh_proxy_host("proxy.example.com")
    assert found is not None and found.id == t.id


async def test_update_poll_status_batch(repo):
    a = await _make(repo, name="a", host="10.0.0.1")
    b = await _make(repo, name="b", host="10.0.0.2")
    await repo.update_poll_status_batch(
        {
            a.id: ("success", None),
            b.id: ("error", "timeout"),
        }
    )
    ra = await repo.get_target(a.id)
    rb = await repo.get_target(b.id)
    assert ra.last_poll_status == "success" and ra.consecutive_failures == 0
    assert rb.last_poll_status == "error" and rb.last_error_message == "timeout"
    assert rb.consecutive_failures == 1
    # A second error increments the failure counter.
    await repo.update_poll_status_batch({b.id: ("error", "again")})
    assert (await repo.get_target(b.id)).consecutive_failures == 2
