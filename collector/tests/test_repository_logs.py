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
