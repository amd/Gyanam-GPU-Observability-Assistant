# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extra coverage for BlobUnpacker: content-type routing, nested gzip-tar,
size caps, error/traversal guards, dedup, raw/binary save, and cleanup."""

import gzip
import io
import tarfile
import zipfile

import pytest
from src.parser.unpacker import BlobUnpacker, ExtractedFile


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


# ---- ExtractedFile report_type derivation ----------------------------------


def test_extracted_file_report_type_explicit_and_derived(tmp_path):
    p = tmp_path / "x"
    explicit = ExtractedFile(path=p, original_name="a.json", size=1, report_type="given")
    assert explicit.report_type == "given"
    derived = ExtractedFile(path=p, original_name="thermal.json", size=1)
    assert derived.report_type == "thermal"
    # A name with no stem falls back to the "telemetry" sentinel.
    fallback = ExtractedFile(path=p, original_name="", size=0)
    assert fallback.report_type == "telemetry"


# ---- content-type-hint routing (magic bytes absent) ------------------------


def test_content_type_hint_zip(unpacker):
    # A body without zip magic but with a "zip" content-type hint routes to
    # _extract_zip, which then fails on the bogus content (wrapped guard).
    body = b"not-an-archive-just-text-hint-zip"
    with pytest.raises(ValueError):
        unpacker.unpack(body, content_type="application/zip", target_name="t")


def test_content_type_hint_gzip(unpacker):
    # gzip hint but body is raw gzip without being auto-detected first is moot;
    # feed real gzip via the hint path by passing content_type explicitly.
    files = unpacker.unpack(
        gzip.compress(b'{"k": 1}'), content_type="application/gzip", target_name="t"
    )
    assert files[0].path.read_bytes() == b'{"k": 1}'


def test_content_type_hint_tar(unpacker):
    files = unpacker.unpack(
        _tar_bytes({"m.json": b"{}"}), content_type="application/x-tar", target_name="t"
    )
    assert any(f.original_name == "m.json" for f in files)


# ---- gzip-wrapped tar (nested extraction) ----------------------------------


def test_gzip_wrapped_tar(unpacker):
    inner = _tar_bytes({"redfish-tree.log": b"tree", "metrics.json": b"{}"})
    files = unpacker.unpack(gzip.compress(inner), target_name="t")
    names = {f.original_name for f in files}
    assert {"redfish-tree.log", "metrics.json"} <= names


def test_gzip_binary_payload_saved_as_bin(unpacker):
    # Non-UTF8 payload -> .bin extension branch in _extract_gzip.
    payload = b"\xff\xfe\x00\x01binarydata"
    files = unpacker.unpack(gzip.compress(payload), target_name="t")
    assert len(files) == 1
    assert files[0].path.suffix == ".bin"
    assert files[0].path.read_bytes() == payload


# ---- raw passthrough: binary (non-JSON) path -------------------------------


def test_raw_binary_passthrough(unpacker):
    payload = b"\xff\xfe\x00\x01rawbinary"
    files = unpacker.unpack(payload, target_name="t")
    assert len(files) == 1
    assert files[0].path.suffix == ".bin"
    assert files[0].path.read_bytes() == payload


def test_tar_fallback_for_binary_without_magic(unpacker):
    # A valid tar whose header the cheap _is_tar check still catches, routed via
    # the no-hint fallback (_try_extract_tar_direct).
    files = unpacker.unpack(_tar_bytes({"a.json": b"{}"}), content_type="", target_name="t")
    assert any(f.original_name == "a.json" for f in files)


# ---- dedup of repeated basenames -------------------------------------------


def test_zip_dedup_basenames(unpacker):
    files = unpacker.unpack(
        _zip_bytes({"dir1/report.json": b"1", "dir2/report.json": b"2"}), target_name="t"
    )
    # Both land under the extract root with distinct names.
    names = sorted(f.path.name for f in files)
    assert names[0] == "report.json"
    assert names[1].startswith("report_1")


def test_tar_dedup_basenames(unpacker):
    files = unpacker.unpack(_tar_bytes({"a/log.txt": b"1", "b/log.txt": b"2"}), target_name="t")
    names = sorted(f.path.name for f in files)
    assert names[0] == "log.txt"
    assert names[1].startswith("log_1")


# ---- skip rules: hidden files, directories ---------------------------------


def test_zip_skips_hidden_and_macosx(unpacker):
    files = unpacker.unpack(
        _zip_bytes({"__MACOSX/x": b"junk", ".hidden": b"junk", "real.json": b"{}"}),
        target_name="t",
    )
    assert {f.path.name for f in files} == {"real.json"}


def test_tar_skips_dot_basename(unpacker):
    # A member whose basename starts with '.' is skipped.
    files = unpacker.unpack(_tar_bytes({"dir/.hidden": b"x", "good.json": b"{}"}), target_name="t")
    assert {f.original_name for f in files} == {"good.json"}


# ---- size caps / error paths -----------------------------------------------

_OVER_500MB = 500 * 1024 * 1024 + 1  # one byte past the hard 500MB cap
_MB = b"\x00" * (1024 * 1024)


def _streamed_zip_over_cap() -> bytes:
    """A zip whose single entry decompresses to >500MB (built without holding
    the full payload in memory)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf, zf.open("big.bin", "w") as f:
        for _ in range(501):  # 501MB of zeros -> trips the 500MB cap
            f.write(_MB)
    return buf.getvalue()


def _streamed_gzip_over_cap() -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as g:
        for _ in range(501):
            g.write(_MB)
    return buf.getvalue()


def test_zip_total_size_cap(tmp_path):
    up = BlobUnpacker(temp_dir=str(tmp_path / "e"), cleanup_after_parse=False)
    with pytest.raises(ValueError, match="Failed to extract blob"):
        up.unpack(_streamed_zip_over_cap(), target_name="t")


def test_gzip_total_size_cap(tmp_path):
    up = BlobUnpacker(temp_dir=str(tmp_path / "e"))
    with pytest.raises(ValueError, match="Failed to extract blob"):
        up.unpack(_streamed_gzip_over_cap(), target_name="t")


def test_tar_member_too_large(tmp_path):
    # Raise the outer blob-size gate so the payload reaches _extract_tar, where
    # the per-member 500MB cap trips.
    up = BlobUnpacker(temp_dir=str(tmp_path / "e"), max_blob_size=1024 * 1024 * 1024)
    big = b"\x00" * _OVER_500MB
    with pytest.raises(ValueError, match="Failed to extract blob"):
        up.unpack(_tar_bytes({"big.bin": big}), target_name="t")


def test_corrupt_gzip_raises(unpacker):
    with pytest.raises(ValueError, match="Failed to extract blob"):
        # Valid gzip magic but garbage body.
        unpacker.unpack(b"\x1f\x8b\x08\x00corruptnonsense", target_name="t")


def test_gzip_hint_on_non_gzip_body(unpacker):
    # content_type routes to the gzip extractor; a non-gzip body trips the
    # BadGzipFile -> ValueError branch (wrapped by the outer guard).
    with pytest.raises(ValueError, match="Failed to extract blob"):
        unpacker.unpack(b"definitely not gzip", content_type="application/gzip", target_name="t")


def test_corrupt_zip_raises(unpacker):
    with pytest.raises(ValueError, match="Failed to extract blob"):
        unpacker.unpack(b"PK\x03\x04corruptzipheaderonly", target_name="t")


def test_target_name_sanitized_to_unknown(unpacker):
    # All-unsafe target name collapses to "unknown" prefix without error.
    files = unpacker.unpack(gzip.compress(b"{}"), target_name="///")
    assert files and files[0].path.exists()


# ---- cleanup paths ----------------------------------------------------------


def test_cleanup_noop_when_disabled(tmp_path):
    up = BlobUnpacker(temp_dir=str(tmp_path / "e"), cleanup_after_parse=False)
    files = up.unpack(gzip.compress(b"{}"), target_name="t")
    up.cleanup(files)  # disabled -> file remains
    assert files[0].path.exists()


def test_cleanup_handles_missing_file(tmp_path):
    up = BlobUnpacker(temp_dir=str(tmp_path / "e"), cleanup_after_parse=True)
    files = up.unpack(gzip.compress(b"{}"), target_name="t")
    files[0].path.unlink()  # already gone
    up.cleanup(files)  # must not raise
    assert not files[0].path.exists()


def test_cleanup_old_files_counts_files_and_dirs(tmp_path):
    import os
    import time

    up = BlobUnpacker(temp_dir=str(tmp_path / "e"))
    old_file = up.temp_dir / "stale.json"
    old_file.write_text("{}")
    old_dir = up.temp_dir / "staledir"
    old_dir.mkdir()
    old = time.time() - 7200
    os.utime(old_file, (old, old))
    os.utime(old_dir, (old, old))
    # A fresh item is kept.
    (up.temp_dir / "fresh.json").write_text("{}")
    cleaned = up.cleanup_old_files(max_age_seconds=3600)
    assert cleaned == 2
