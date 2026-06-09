# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for log-collector filename sanitization and path-traversal defense."""

from src.log_collector import LogCollector, sanitize_filename


def test_sanitize_filename_strips_unsafe_chars():
    # Only [A-Za-z0-9.-] survive; everything else becomes '_' (collapsed).
    assert sanitize_filename("gpu node/01") == "gpu_node_01"
    assert sanitize_filename("!!!") == "target"  # falls back when empty
    # The security invariant: no path separators survive (can't traverse).
    traversal = sanitize_filename("../../etc/passwd")
    assert "/" not in traversal and "\\" not in traversal
    assert "/" not in sanitize_filename("a/b/c")


def _collector(tmp_path):
    return LogCollector(repository=None, storage_dir=str(tmp_path / "logs"))


def test_delete_file_within_storage(tmp_path):
    lc = _collector(tmp_path)
    f = lc.storage_dir / "bundle.tar.gz"
    f.write_bytes(b"data")
    assert lc.delete_file(str(f)) is True
    assert not f.exists()


def test_delete_file_rejects_traversal(tmp_path):
    lc = _collector(tmp_path)
    # A secret file outside the storage dir must never be deletable.
    outside = tmp_path / "secret.txt"
    outside.write_text("keep me")
    assert lc.delete_file(str(outside)) is False
    assert lc.delete_file(str(lc.storage_dir / ".." / "secret.txt")) is False
    assert outside.exists()


def test_delete_missing_file_returns_false(tmp_path):
    lc = _collector(tmp_path)
    assert lc.delete_file(str(lc.storage_dir / "nope.gz")) is False
