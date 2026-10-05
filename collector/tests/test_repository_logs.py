# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for CollectedLog CRUD."""


async def _log(repo, **kw):
    kw.setdefault("target_id", 1)
    kw.setdefault("target_name", "n1")
    kw.setdefault("target_host", "10.0.0.1")
    kw.setdefault("filename", "bundle.tar.gz")
    kw.setdefault("file_path", "/data/logs/bundle.tar.gz")
    return await repo.create_collected_log(**kw)


async def test_create_and_get_log(repo):
    log = await _log(repo)
    assert log.id is not None
    assert log.status == "pending"
    fetched = await repo.get_collected_log(log.id)
    assert fetched.filename == "bundle.tar.gz"
    assert await repo.get_collected_log(999999) is None


async def test_list_logs(repo):
    await _log(repo, filename="a.gz", file_path="/d/a.gz")
    await _log(repo, filename="b.gz", file_path="/d/b.gz")
    logs = await repo.get_all_collected_logs()
    assert {log.filename for log in logs} == {"a.gz", "b.gz"}


async def test_update_log(repo):
    log = await _log(repo)
    updated = await repo.update_collected_log(log.id, status="completed", file_size_bytes=1234)
    assert updated.status == "completed"
    assert updated.file_size_bytes == 1234
    # Non-whitelisted field is ignored.
    same = await repo.update_collected_log(log.id, target_name="hacked")
    assert same.target_name == "n1"


async def test_delete_log(repo):
    log = await _log(repo)
    deleted = await repo.delete_collected_log(log.id)
    assert deleted is not None
    assert await repo.get_collected_log(log.id) is None


async def test_delete_prior_target_logs_keeps_latest(repo):
    # Two completed bundles for node 1, one for node 2, plus a failed node-1 row.
    old1 = await _log(repo, target_id=1, filename="n1-old.gz", file_path="/d/n1-old.gz")
    await repo.update_collected_log(old1.id, status="completed", file_size_bytes=10)
    new1 = await _log(repo, target_id=1, filename="n1-new.gz", file_path="/d/n1-new.gz")
    await repo.update_collected_log(new1.id, status="completed", file_size_bytes=20)
    failed1 = await _log(repo, target_id=1, filename="n1-fail.gz", file_path="/d/n1-fail.gz")
    await repo.update_collected_log(failed1.id, status="failed")
    other = await _log(repo, target_id=2, filename="n2.gz", file_path="/d/n2.gz")
    await repo.update_collected_log(other.id, status="completed", file_size_bytes=30)

    removed = await repo.delete_prior_target_logs(target_id=1, keep_log_id=new1.id)

    # Only node-1's older completed bundle is removed (returned for file cleanup).
    assert {r.filename for r in removed} == {"n1-old.gz"}
    remaining = {log.filename for log in await repo.get_all_collected_logs()}
    assert "n1-old.gz" not in remaining
    assert {"n1-new.gz", "n1-fail.gz", "n2.gz"} <= remaining  # latest + failed + other node


async def test_delete_prior_target_logs_noop_when_only_one(repo):
    only = await _log(repo, target_id=7, filename="solo.gz", file_path="/d/solo.gz")
    await repo.update_collected_log(only.id, status="completed")
    assert await repo.delete_prior_target_logs(target_id=7, keep_log_id=only.id) == []
