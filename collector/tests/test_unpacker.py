# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the blob unpacker (tar/gz/zip) and its path-traversal defense."""

import gzip
import io
import tarfile
import zipfile

import pytest
from src.parser.unpacker import BlobUnpacker


@pytest.fixture
def unpacker(tmp_path):
    return BlobUnpacker(temp_dir=str(tmp_path / "extract"), cleanup_after_parse=False)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ---- format detection ----


def test_detects_formats(unpacker):
    assert unpacker._is_zip(_zip_bytes({"a.json": b"{}"})) is True
    assert unpacker._is_gzip(gzip.compress(b"{}")) is True
    assert unpacker._is_tar(_tar_bytes({"a.json": b"{}"})) is True


# ---- extraction ----


def test_unpack_gzip(unpacker):
    files = unpacker.unpack(gzip.compress(b'{"k": 1}'), target_name="t")
    assert len(files) == 1
    assert files[0].path.read_bytes() == b'{"k": 1}'


def test_unpack_zip_multiple(unpacker):
    files = unpacker.unpack(_zip_bytes({"a.json": b"1", "b.json": b"2"}), target_name="t")
    assert {f.path.name for f in files} == {"a.json", "b.json"}


def test_unpack_tar(unpacker):
    files = unpacker.unpack(_tar_bytes({"m.json": b'{"x": 1}'}), target_name="t")
    assert any(f.path.read_bytes() == b'{"x": 1}' for f in files)


# ---- security / limits ----


def test_zip_path_traversal_is_contained(unpacker, tmp_path):
    # A traversal entry name must not escape the extraction directory: the
    # extractor keeps only the basename. (Dot-prefixed names are skipped
    # outright as hidden/system files, which is also safe.)
    files = unpacker.unpack(_zip_bytes({"sub/../evil.txt": b"pwned"}), target_name="t")
    extract_root = (tmp_path / "extract").resolve()
    for f in files:
        assert str(f.path.resolve()).startswith(str(extract_root))
        assert "/../" not in str(f.path)
    # The one extracted file lands as a basename inside the root.
    assert any(f.path.name == "evil.txt" for f in files)


def test_empty_blob_rejected(unpacker):
    with pytest.raises(ValueError):
        unpacker.unpack(b"", target_name="t")


def test_oversized_blob_rejected(tmp_path):
    small = BlobUnpacker(temp_dir=str(tmp_path / "e"), max_blob_size=10)
    with pytest.raises(ValueError):
        small.unpack(b"x" * 11, target_name="t")


def test_unpack_raw_json_passthrough(unpacker):
    files = unpacker.unpack(b'{"raw": true}', target_name="t")
    assert len(files) == 1
    assert files[0].path.suffix == ".json"
    assert files[0].path.read_bytes() == b'{"raw": true}'


def test_cleanup_removes_files(tmp_path):
    up = BlobUnpacker(temp_dir=str(tmp_path / "e"), cleanup_after_parse=True)
    files = up.unpack(gzip.compress(b"{}"), target_name="t")
    p = files[0].path
    assert p.exists()
    up.cleanup(files)
    assert not p.exists()


def test_cleanup_old_files(tmp_path):
    import os
    import time

    up = BlobUnpacker(temp_dir=str(tmp_path / "e"))
    old_dir = up.temp_dir / "stale"
    old_dir.mkdir()
    old = time.time() - 7200
    os.utime(old_dir, (old, old))
    assert up.cleanup_old_files(max_age_seconds=3600) >= 1


def test_extracted_file_report_type_derived_from_name(unpacker):
    import gzip

    files = unpacker.unpack(gzip.compress(b'{"MetricValues": []}'), target_name="sysZ")
    assert files and files[0].report_type == "sysZ_telemetry"  # derived from filename
