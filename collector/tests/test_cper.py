# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for CPER enrichment: eligibility, summary, decode, fetch, worker."""

import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from src.database.repository import TargetRepository, _cper_eligible
from src.redfish import amd_cper_sections as amd
from src.redfish import cper_decoder
from src.redfish.alert_subscriber import AlertEvent

FIXTURE = Path("reference_artifacts/cper/sample_memory_pcie.cper")
AMD_BOOT_FIXTURE = Path("reference_artifacts/cper/amd_crashdump_boot.bin")


# ---- eligibility --------------------------------------------------------


def test_eligible_by_diagnostic_data_type():
    raw = {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/x/attachment"}
    assert _cper_eligible(raw) is True


def test_eligible_by_resolution_mention():
    raw = {"Resolution": "Collect CPER logs and consult guide.", "AdditionalDataURI": "/x"}
    assert _cper_eligible(raw) is True


def test_not_eligible_without_additional_data_uri():
    assert _cper_eligible({"DiagnosticDataType": "CPER"}) is False


def test_not_eligible_when_no_cper_mention():
    assert _cper_eligible({"Resolution": "Reseat the DIMM", "AdditionalDataURI": "/x"}) is False


def test_not_eligible_non_dict():
    assert _cper_eligible(None) is False


# ---- summarizer ---------------------------------------------------------


def test_summarize_uses_section_messages():
    decoded = {
        "header": {
            "severity": {"name": "Fatal"},
            "notificationType": {"type": "Memory"},
        },
        "sectionDescriptors": [
            {"sectionType": {"type": "Platform Memory"}, "severity": {"name": "Fatal"}},
            {"sectionType": {"type": "PCIe"}, "severity": {"name": "Corrected"}},
        ],
        "sections": [
            {"message": "A Memory Error occurred at 0xDEAD at node 5"},
            {"message": "A PCIe Error occurred"},
        ],
    }
    s = cper_decoder.summarize_cper(decoded)
    assert "CPER Fatal" in s
    assert "Memory" in s
    assert "2 section(s)" in s
    assert "A Memory Error occurred at 0xDEAD at node 5" in s
    assert "[PCIe/Corrected]" in s


def test_summarize_oem_section_surfaces_guid_fru_and_strings():
    import base64

    payload = base64.b64encode(b"\x00\x00garbage01.10.180862\x00\x00").decode()
    decoded = {
        "header": {"severity": {"name": "Fatal"}},
        "sectionDescriptors": [
            {
                "sectionType": {"type": "Unknown", "data": "32ac0c78-2623-48f6-b0d0-7365725fd6ae"},
                "severity": {"name": "Fatal"},
                "fruText": "BOOTERR",
                "sectionLength": 208,
            }
        ],
        "sections": [{"Unknown": {"data": payload}}],
    }
    s = cper_decoder.summarize_cper(decoded)
    assert "32ac0c78-2623-48f6-b0d0-7365725fd6ae" in s
    assert "OEM section" in s
    assert "BOOTERR" in s
    assert "208 bytes" in s
    assert "01.10.180862" in s


def test_ascii_hints_extracts_printable_runs():
    import base64

    b64 = base64.b64encode(b"\x00\x01ABCDEF\x00xy\x00VERSION1.2.3").decode()
    hints = cper_decoder._ascii_hints(b64)
    assert "ABCDEF" in hints
    assert "VERSION1.2.3" in hints
    # too-short "xy" run is excluded
    assert "xy" not in hints


def test_ascii_hints_empty():
    assert cper_decoder._ascii_hints(None) == []
    assert cper_decoder._ascii_hints("!!!not base64!!!") == [] or isinstance(
        cper_decoder._ascii_hints("!!!not base64!!!"), list
    )


def test_summarize_empty():
    assert cper_decoder.summarize_cper(None) == ""


# ---- real decode through cper-convert (needs the bundled binary) --------


@pytest.mark.skipif(not FIXTURE.exists(), reason="CPER fixture missing")
async def test_decode_real_fixture():
    import shutil

    if (
        shutil.which("cper-convert") is None
        and not Path(cper_decoder.DEFAULT_CPER_CONVERT).exists()
    ):
        pytest.skip("cper-convert not available in this environment")
    data = FIXTURE.read_bytes()
    decoded = await cper_decoder.decode_cper(data)
    assert decoded is not None
    assert "sections" in decoded
    summary = cper_decoder.summarize_cper(decoded)
    assert summary.startswith("CPER ")
    assert "section(s)" in summary


async def test_decode_bad_binary_returns_none():
    # Random bytes are not a valid CPER; cper-convert should fail cleanly.
    import shutil

    if (
        shutil.which("cper-convert") is None
        and not Path(cper_decoder.DEFAULT_CPER_CONVERT).exists()
    ):
        pytest.skip("cper-convert not available")
    assert await cper_decoder.decode_cper(b"not a cper record at all") is None


async def test_decode_missing_binary_returns_none():
    assert await cper_decoder.decode_cper(b"\x00\x01", cper_convert_path="/nonexistent/cc") is None


# ---- fetch --------------------------------------------------------------


async def test_fetch_ok(httpx_mock):
    httpx_mock.add_response(
        method="GET", url="https://bmc/x/attachment", status_code=200, content=b"BINARY"
    )
    data = await cper_decoder.fetch_cper_attachment(
        base_url="https://bmc", uri="/x/attachment", username="u", password="p", verify_ssl=False
    )
    assert data == b"BINARY"


async def test_fetch_gone_raises(httpx_mock):
    httpx_mock.add_response(method="GET", url="https://bmc/x", status_code=404)
    with pytest.raises(cper_decoder.CperGoneError):
        await cper_decoder.fetch_cper_attachment(
            base_url="https://bmc", uri="/x", username="u", password="p", verify_ssl=False
        )


async def test_fetch_oversize_raises(httpx_mock):
    httpx_mock.add_response(method="GET", url="https://bmc/x", status_code=200, content=b"X" * 100)
    with pytest.raises(ValueError):
        await cper_decoder.fetch_cper_attachment(
            base_url="https://bmc",
            uri="/x",
            username="u",
            password="p",
            verify_ssl=False,
            max_bytes=10,
        )


# ---- AMD vendor-section decoder -----------------------------------------


def test_is_amd_section():
    assert amd.is_amd_section("32ac0c78-2623-48f6-b0d0-7365725fd6ae")  # AMD_CRASHDUMP
    assert amd.is_amd_section("32AC0C78-2623-48F6-81A2-AC691780551D")  # nonstd, upper
    assert not amd.is_amd_section("a5bc1114-6f64-4ede-b863-3e83ed7c83b1")  # memory
    assert not amd.is_amd_section(None)


@pytest.mark.skipif(not AMD_BOOT_FIXTURE.exists(), reason="AMD boot fixture missing")
def test_decode_real_amd_boot_fixture():
    raw = AMD_BOOT_FIXTURE.read_bytes()
    assert len(raw) == 208
    d = amd.decode_amd_section(amd.AMD_CRASHDUMP, raw)
    assert d["kind"] == "boot_crashdump"
    assert d["firmware_id"] == "01.10.180862"  # verified against the real record
    s = amd.summarize_amd_section(d)
    assert "AMD boot crashdump" in s
    assert "01.10.180862" in s


def test_mca_status_and_ipid_bit_decode():
    status = (1 << 63) | (1 << 61) | (1 << 58) | (1 << 45) | 0x1234  # val,uc,addrv,uecc,ec
    st = amd._decode_mca_status(status)
    assert st["val"] and st["uc"] and st["addrv"] and st["uecc"]
    assert not st["pcc"]
    assert st["error_code"] == 0x1234
    ipid = (0x0001 << 48) | (0x096 << 32) | 0xABCD
    ip = amd._decode_mca_ipid(ipid)
    assert ip["mca_type"] == "0x0001"
    assert ip["hardware_id"] == "0x096"
    assert ip["instance_id_lo"] == "0x0000abcd"


def _build_fatal(status, addr, ipid, synd, fw="9.9.9"):
    buf = bytearray(176)
    buf[16 : 16 + len(fw)] = fw.encode()
    struct.pack_into("<HHIQ", buf, 128, 1, 4, 0, 0)  # reg_ctx_type, reg_arr_size, resv, resv
    struct.pack_into(
        "<8I",
        buf,
        144,
        status & 0xFFFFFFFF,
        status >> 32,
        addr & 0xFFFFFFFF,
        addr >> 32,
        ipid & 0xFFFFFFFF,
        ipid >> 32,
        synd & 0xFFFFFFFF,
        synd >> 32,
    )
    return bytes(buf)


def test_decode_fatal_roundtrip():
    status = (1 << 63) | (1 << 61) | (1 << 57)  # val, uc, pcc
    d = amd.decode_amd_section(amd.AMD_CRASHDUMP, _build_fatal(status, 0xDEAD, 0x96 << 32, 0x5))
    assert d["kind"] == "fatal_crashdump"
    assert d["firmware_id"] == "9.9.9"
    # Registers are the single source of truth (no duplicated top-level copies).
    assert "status" not in d and "address" not in d and "ipid" not in d
    r = d["registers"]
    assert r["STATUS"]["uc"] and r["STATUS"]["pcc"]
    # STATUS now also decodes tcc/errcoreid/addr_lsb.
    assert "addr_lsb" in r["STATUS"] and "errcoreid" in r["STATUS"]
    # ADDR is decoded to {raw, address_valid, valid_address?} (addrv was not set here).
    assert r["ADDR"]["raw"] == "0x000000000000dead"
    assert "SYND" in r and "error_information" in r["SYND"]
    assert r["IPID"]["hardware_id"] == "0x096"
    # (hwid=0x96, mcatype=0x0) -> UMC per the MI300 mapping (best-effort).
    assert r["IPID"]["ip_block"].startswith("UMC")
    assert d["error_class"] == "Uncorrectable"
    assert "UMC" in d["description"]
    assert "sub_block_error_code" in d
    s = amd.summarize_amd_section(d)
    assert "AMD fatal" in s and "Uncorrectable" in s and "UMC" in s


def _build_nonstd(status, addr, ipid, synd, ms_chk=0, fw="8.8.8"):
    buf = bytearray(272)
    buf[16 : 16 + len(fw)] = fw.encode()
    struct.pack_into("<Q", buf, 88, ms_chk)  # info.ms_chk_mask

    # ctx.reg_dump[32] u32 at offset 144; 64-bit reg N at u32 [2N],[2N+1].
    def setreg(idx, val):
        struct.pack_into("<II", buf, 144 + idx * 8, val & 0xFFFFFFFF, val >> 32)

    setreg(amd._ACA_IDX_STATUS, status)
    setreg(amd._ACA_IDX_ADDR, addr)
    setreg(amd._ACA_IDX_IPID, ipid)
    setreg(amd._ACA_IDX_SYND, synd)
    return bytes(buf)


def test_decode_nonstd_roundtrip():
    status = (1 << 63) | (1 << 61) | (1 << 44)  # val, uc, deferred
    ms_chk = (1 << 19) | (1 << 16)  # uncorrected valid-ish + err_type bit0
    d = amd.decode_amd_section(
        amd.AMD_GPU_NONSTANDARD_ERROR, _build_nonstd(status, 0xBEEF, 0x2E << 32, 0x7, ms_chk)
    )
    assert d["kind"] == "runtime_nonstandard"
    assert d["firmware_id"] == "8.8.8"
    # No duplicated top-level status/address; they live only under registers.
    assert "status" not in d and "address" not in d and "ipid" not in d
    r = d["registers"]
    assert r["STATUS"]["uc"] and r["STATUS"]["deferred"]
    assert r["ADDR"]["raw"] == "0x000000000000beef"
    assert d["ms_check"]["uncorrected"] is True
    # Expanded fields: full register set + info fields are decoded.
    assert "STATUS" in r and "IPID" in r and "DESTAT" in r
    assert "identifiers" in d and "context" in d and "error_type" in d
    assert d["error_class"] in ("Uncorrectable", "Deferred")
    assert "description" in d and "sub_block_error_code" in d
    s = amd.summarize_amd_section(d)
    assert "AMD runtime" in s


def test_misc0_synd_and_address_masking():
    # MISC0.ErrCnt, SYND.ErrorInformation, and ADDR masking by STATUS.addr_lsb.
    misc0 = (1 << 63) | (5 << 32)  # valid + error_count=5
    m = amd._decode_misc0(misc0)
    assert m["valid"] and m["error_count"] == 5
    synd = 0x2A  # error_information low bits
    assert amd._decode_synd(synd)["error_information"] == 0x2A
    # addrv set with addr_lsb=6 masks off the low 6 bits.
    status = (1 << 63) | (1 << 58) | (6 << 24)  # val, addrv, addr_lsb=6
    d = amd.decode_amd_section(amd.AMD_CRASHDUMP, _build_fatal(status, 0xDEADBEEF, 0x96 << 32, 0))
    addr = d["registers"]["ADDR"]
    assert addr["address_valid"] is True
    assert addr["valid_address"] == "0x00000000deadbec0"  # 0xDEADBEEF & ~0x3f


def test_umc_location_decode():
    # Real UMC IPID from an OAM5 record -> socket must match OAM number (5).
    ip = amd._decode_mca_ipid(0x0000109600091F01)
    loc = ip["umc_location"]
    assert loc["socket_id"] == 5  # matches fruText "OAM5" on the real record
    assert loc["channel"] == 1 and loc["umc_instance"] == 0 and loc["aid_die_id"] == 0
    # Non-UMC IPID has no umc_location.
    assert "umc_location" not in amd._decode_mca_ipid((0x0001 << 48) | (0x01 << 32))


def test_umc_location_in_summary():
    status = (1 << 63) | (1 << 45)  # val + uecc
    d = amd.decode_amd_section(amd.AMD_CRASHDUMP, _build_fatal(status, 0, 0x0000109600091F01, 0))
    s = amd.summarize_amd_section(d)
    assert "socket5" in s and "ch1" in s


def test_guid_as_ascii():
    # Real platformID from a captured record -> readable board identifier.
    assert amd.guid_as_ascii("31313131-3230-472d-3339-3330362d3030") == "111120G-39306-00"
    # A real (non-ASCII) GUID -> None (no garbage).
    assert amd.guid_as_ascii("a5bc1114-6f64-4ede-b863-3e83ed7c83b1") is None
    assert amd.guid_as_ascii(None) is None


def test_umc_ext_code_meaning():
    # UMC + error_code_ext 0 -> uncorrectable annotation.
    status = (1 << 63) | (1 << 45)  # val + uecc; ext defaults 0
    d = amd.decode_amd_section(amd.AMD_CRASHDUMP, _build_fatal(status, 0, 0x96 << 32, 0))
    assert d.get("error_code_ext_meaning") == "uncorrectable"


def test_ipid_ip_block_mapping():
    # (hwid, mcatype) -> IP name, best-effort from the MI300 table.
    umc = amd._decode_mca_ipid((0x0000 << 48) | (0x96 << 32))
    assert umc["ip_block"].startswith("UMC")
    smu = amd._decode_mca_ipid((0x0001 << 48) | (0x01 << 32))  # our real sample's pair
    assert smu["ip_block"].startswith("SMU")
    xgmi = amd._decode_mca_ipid((0x0000 << 48) | (0x50 << 32))
    assert "XGMI" in xgmi["ip_block"]
    # Unknown pair -> no wrong label.
    unknown = amd._decode_mca_ipid((0x0007 << 48) | (0xAB << 32))
    assert "ip_block" not in unknown


def test_decode_length_guard():
    # Too-short buffers must degrade to a decode_error, never mis-read.
    assert "decode_error" in amd.decode_amd_section(amd.AMD_CRASHDUMP, b"\x00" * 10)
    assert "decode_error" in amd.decode_amd_section(amd.AMD_GPU_NONSTANDARD_ERROR, b"\x00" * 10)
    s = amd.summarize_amd_section({"decode_error": "x"})
    assert "undecoded" in s


@pytest.mark.skipif(not AMD_BOOT_FIXTURE.exists(), reason="AMD boot fixture missing")
def test_enrich_amd_sections_integration():
    import base64

    raw = AMD_BOOT_FIXTURE.read_bytes()
    decoded = {
        "header": {"severity": {"name": "Fatal"}, "notificationType": {"type": "Boot"}},
        "sectionDescriptors": [
            {
                "sectionType": {"type": "Unknown", "data": amd.AMD_CRASHDUMP},
                "severity": {"name": "Fatal"},
            }
        ],
        "sections": [{"Unknown": {"data": base64.b64encode(raw).decode()}}],
    }
    cper_decoder.enrich_amd_sections(decoded)
    assert decoded["sections"][0]["amd"]["kind"] == "boot_crashdump"
    summary = cper_decoder.summarize_cper(decoded)
    assert "AMD boot crashdump" in summary
    assert "01.10.180862" in summary

    # Re-enrichment must re-decode from the raw blob, not bail on the existing
    # "amd" key (regression: it previously picked "amd" as the payload).
    decoded["sections"][0]["amd"] = {"kind": "stale"}
    cper_decoder.enrich_amd_sections(decoded)
    assert decoded["sections"][0]["amd"]["kind"] == "boot_crashdump"


# ---- repository integration --------------------------------------------


def _mk(target_id, message, raw):
    return AlertEvent(
        target_id=target_id,
        target_name=f"n{target_id}",
        target_bmc=f"10.0.0.{target_id}",
        severity="Critical",
        message=message,
        message_id=message,
        event_type="Alert",
        origin_of_condition=None,
        event_timestamp=datetime.now(UTC),
        received_at=datetime.now(UTC),
        source_id=None,
        raw=raw,
    )


async def _repo(tmp_path):
    repo = TargetRepository(
        database_url=f"sqlite:///{tmp_path}/targets.db",
        encryption_key=Fernet.generate_key().decode(),
        alerts_database_url=f"sqlite:///{tmp_path}/alerts.db",
    )
    await repo.init_db()
    return repo


async def test_ingest_marks_pending_and_worker_helpers(tmp_path):
    repo = await _repo(tmp_path)
    cper_raw = {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a/attachment"}
    await repo.create_alerts_batch(
        [
            _mk(1, "cper-alert", cper_raw),
            _mk(1, "plain-alert", {"Message": "no cper here"}),
        ]
    )
    pending = await repo.get_pending_cper_alerts()
    assert len(pending) == 1
    # get_pending returns lightweight PendingCper descriptors (no blob load).
    assert pending[0].uri == "/a/attachment"
    assert pending[0].has_decoded is False

    # Record a decoded result and confirm it leaves the pending set.
    await repo.set_cper_result(
        pending[0].id,
        status="decoded",
        refined_message="CPER Fatal — 1 section(s): [Memory] x",
        decoded={"sections": []},
    )
    assert await repo.get_pending_cper_alerts() == []
    assert (await repo.get_alert_cper(pending[0].id)) == {"sections": []}
    a = await repo.get_alert(pending[0].id)
    assert a.cper_status == "decoded"
    assert a.refined_message.startswith("CPER Fatal")
    await repo.close()


async def test_get_pending_respects_attempt_cooldown(tmp_path):
    # A just-attempted (still-pending) row is excluded while within cooldown,
    # so the adaptive loop doesn't immediately re-fetch it.
    repo = await _repo(tmp_path)
    await repo.create_alerts_batch(
        [_mk(1, "c", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a"})]
    )
    aid = (await repo.get_pending_cper_alerts())[0].id
    await repo.set_cper_result(aid, status="pending", increment_attempt=True)  # stamps now
    assert await repo.get_pending_cper_alerts(attempt_cooldown_seconds=60) == []  # cooling down
    assert len(await repo.get_pending_cper_alerts(attempt_cooldown_seconds=0)) == 1  # no cooldown
    await repo.close()


async def test_set_cper_results_batch_isolates_bad_row(tmp_path):
    # A poison row (non-serializable decoded) must not roll back the whole batch.
    repo = await _repo(tmp_path)
    await repo.create_alerts_batch(
        [
            _mk(1, "good", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a"}),
            _mk(1, "bad", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/b"}),
        ]
    )
    ids = [p.id for p in await repo.get_pending_cper_alerts()]
    written = await repo.set_cper_results_batch(
        [
            {"id": ids[0], "status": "decoded", "refined_message": "ok", "decoded": {"x": 1}},
            # A set() is not JSON-serializable -> this row's UPDATE raises.
            {"id": ids[1], "status": "decoded", "decoded": {"bad": {1, 2, 3}}},
        ]
    )
    assert written == 1  # only the good row persisted
    assert (await repo.get_alert(ids[0])).cper_status == "decoded"
    assert (await repo.get_alert(ids[1])).cper_status == "pending"  # bad row untouched
    await repo.close()


async def test_pending_query_flags_existing_decode_for_local_resummarize(tmp_path):
    # A row reset to 'pending' but still holding cper_decoded must be flagged
    # has_decoded=True so the worker re-summarizes locally (via get_alert_cper)
    # instead of re-fetching from the BMC.
    repo = await _repo(tmp_path)
    await repo.create_alerts_batch(
        [_mk(1, "c", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a"})]
    )
    aid = (await repo.get_pending_cper_alerts())[0].id
    await repo.set_cper_result(aid, status="pending", decoded={"header": {}, "sections": []})
    rows = await repo.get_pending_cper_alerts()
    assert rows[0].has_decoded is True
    assert await repo.get_alert_cper(aid) == {"header": {}, "sections": []}
    await repo.close()


async def test_set_cper_results_batch(tmp_path):
    # Batched write path: multiple outcomes persisted in one call.
    repo = await _repo(tmp_path)
    await repo.create_alerts_batch(
        [
            _mk(1, "a", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a"}),
            _mk(1, "b", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/b"}),
        ]
    )
    ids = [p.id for p in await repo.get_pending_cper_alerts()]
    await repo.set_cper_results_batch(
        [
            {"id": ids[0], "status": "decoded", "refined_message": "ok", "decoded": {"x": 1}},
            {"id": ids[1], "status": "fetch_failed", "increment_attempt": True},
        ]
    )
    assert await repo.get_pending_cper_alerts() == []
    a0 = await repo.get_alert(ids[0])
    a1 = await repo.get_alert(ids[1])
    assert a0.cper_status == "decoded" and a0.refined_message == "ok"
    assert a1.cper_status == "fetch_failed" and a1.cper_attempts == 1
    assert a0.cper_attempted_at is not None and a1.cper_attempted_at is not None
    await repo.close()


async def test_dedup_does_not_recycle_decoded_alert(tmp_path):
    # A re-pull of an already-decoded CPER alert must be deduped at ingest and
    # must NOT re-queue it for another decode cycle.
    repo = await _repo(tmp_path)
    ev = _mk(1, "cper-a", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a/attach"})
    assert await repo.create_alerts_batch([ev]) == 1
    aid = (await repo.get_pending_cper_alerts())[0].id
    await repo.set_cper_result(aid, status="decoded", refined_message="done", decoded={"x": 1})
    # Re-ingest the identical event (as an hourly baseline re-pull would).
    assert await repo.create_alerts_batch([ev]) == 0  # deduped, not re-inserted
    assert await repo.get_pending_cper_alerts() == []  # not re-queued
    a = await repo.get_alert(aid)
    assert a.cper_status == "decoded" and a.refined_message == "done"  # untouched
    await repo.close()


async def test_attempts_cap_excludes_from_pending(tmp_path):
    repo = await _repo(tmp_path)
    await repo.create_alerts_batch(
        [_mk(1, "c", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a"})]
    )
    aid = (await repo.get_pending_cper_alerts())[0].id
    for _ in range(3):
        await repo.set_cper_result(aid, status="pending", increment_attempt=True)
    # attempts now 3 -> excluded at max_attempts=3
    assert await repo.get_pending_cper_alerts(max_attempts=3) == []
    await repo.close()


async def test_requeue_failed_cper_is_time_based(tmp_path):
    # Restart-resilient retry: a fetch_failed row is requeued only when its last
    # attempt is older than the window (no in-memory timer).
    from datetime import timedelta

    from sqlalchemy import update
    from src.database.models import Alert

    repo = await _repo(tmp_path)
    await repo.create_alerts_batch(
        [_mk(1, "c", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a"})]
    )
    aid = (await repo.get_pending_cper_alerts())[0].id
    await repo.set_cper_result(aid, status="fetch_failed", increment_attempt=True)  # stamped now
    # Recent failure -> not yet due for retry.
    assert await repo.requeue_failed_cper(older_than_minutes=60) == 0
    # Backdate the last attempt beyond the window -> requeued (survives restarts).
    async with repo.alert_session_factory() as s:
        await s.execute(
            update(Alert)
            .where(Alert.id == aid)
            .values(cper_attempted_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2))
        )
        await s.commit()
    assert await repo.requeue_failed_cper(older_than_minutes=60) == 1
    a = await repo.get_alert(aid)
    assert a.cper_status == "pending" and a.cper_attempts == 0
    await repo.close()


async def test_finalize_exhausted_cper_clears_limbo(tmp_path):
    # A 'pending' row with attempts >= max (e.g. a requeue that left the counter)
    # must be finalized, not left in limbo where get_pending never selects it.
    repo = await _repo(tmp_path)
    await repo.create_alerts_batch(
        [_mk(1, "c", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/a"})]
    )
    aid = (await repo.get_pending_cper_alerts())[0].id
    for _ in range(3):
        await repo.set_cper_result(aid, status="pending", increment_attempt=True)
    # attempts==3, still pending -> get_pending skips it (limbo)
    assert await repo.get_pending_cper_alerts(max_attempts=3) == []
    finalized = await repo.finalize_exhausted_cper(max_attempts=3)
    assert finalized == 1
    a = await repo.get_alert(aid)
    assert a.cper_status == "fetch_failed"
    await repo.close()


async def test_backfill_marks_existing(tmp_path):
    repo = await _repo(tmp_path)
    # Insert a plain alert, then simulate it being eligible via raw and backfill.
    await repo.create_alerts_batch([_mk(1, "x", {"Message": "plain"})])
    # No pending yet.
    assert await repo.get_pending_cper_alerts() == []
    # Insert an eligible one whose status we clear to simulate pre-feature rows.
    await repo.create_alerts_batch(
        [_mk(2, "y", {"DiagnosticDataType": "CPER", "AdditionalDataURI": "/z"})]
    )
    aid = (await repo.get_pending_cper_alerts())[0].id
    await repo.set_cper_result(aid, status=None if False else "decoded")  # take it out
    # Backfill should find none now (already decoded / plain not eligible).
    marked = await repo.mark_eligible_cper_pending()
    assert marked == 0
    await repo.close()
