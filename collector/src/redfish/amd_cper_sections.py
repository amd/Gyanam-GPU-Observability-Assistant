# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Decode AMD GPU vendor (OEM) CPER sections into human-readable fields.

Standard UEFI CPER sections (memory, PCIe, processor, …) are decoded by
OpenBMC ``libcper``. AMD Instinct/OAM GPUs additionally emit *vendor* CPER
sections that libcper does not decode (it returns them as an opaque
``Unknown`` blob). This module decodes those AMD sections.

--------------------------------------------------------------------------
Basis: publicly available information
--------------------------------------------------------------------------
This decoder is written entirely from AMD's **open-source Linux kernel amdgpu
driver**, which is the authoritative, publicly published definition of these
records. No proprietary or confidential AMD material was used. The relevant
public sources (torvalds/linux, ``drivers/gpu/drm/amd/``):

* ``include/amd_cper.h`` — section GUIDs and the packed section structs
  (``#pragma pack(push, 1)``):
    - ``AMD_CRASHDUMP``           = 32ac0c78-2623-48f6-b0d0-7365725fd6ae
    - ``AMD_GPU_NONSTANDARD_ERROR`` = 32ac0c78-2623-48f6-81a2-ac691780551d
    - ``BOOT_TYPE`` (notify)      = 3d61a466-ab40-409a-a698-f362d464b38f
    - ``CPER_NOTIFY_MCE`` (fatal) = e8f56ffe-919c-4cc5-ba88-65abe14913bb
    - ``CPER_NOTIFY_CMC`` (corr.) = 2dce8bb1-bdd7-450e-b9ad-9cf4ebd4f890
    - ``CPER_ACA_REG_COUNT`` = 32, ``CPER_MAX_OAM_COUNT`` = 8,
      ``CPER_CTX_TYPE_CRASH`` = 1, ``CPER_CTX_TYPE_BOOT`` = 9
    - structs: ``cper_sec_crashdump_hdr`` (128 B), ``..._boot`` (208 B),
      ``..._fatal`` (176 B), ``cper_sec_nonstd_err`` (272 B),
      ``cper_sec_crashdump_reg_data`` (status/addr/ipid/synd lo+hi).
* ``amdgpu/amdgpu_aca.h`` — ACA/MCA bank register indices (``enum aca_reg_idx``:
  CTL=0, STATUS=1, ADDR=2, MISC0=3, CONFIG=4, IPID=5, SYND=6, …) and the
  architectural ``ACA_REG__STATUS__*`` / ``ACA_REG__IPID__*`` bitfields.

Introduced by the amdgpu CPER/ACA patch series (2025), e.g.
"drm/amd/include: Add amd cper header" and "Include ACA error type in aca bank"
on the amd-gfx mailing list.

--------------------------------------------------------------------------
Risk: kernel headers are NOT a stable ABI
--------------------------------------------------------------------------
These structs live in *driver internal* headers (not ``uapi/``) and have
already been refactored once (``amd_cper.h`` → ``ras_cper.h``) while keeping the
same GUID values. Field layout, sizes, or the ACA register count CAN change
between kernel/firmware versions. Therefore this decoder is deliberately
**defensive**:

* it dispatches by section GUID + byte length, and validates the buffer is at
  least the expected packed size before reading any field;
* on any mismatch it returns a ``{"decode_error": ...}`` marker instead of
  guessing, so the caller falls back to the generic (GUID + size + strings)
  summary — never a wrong claim;
* the ACA ``STATUS``/``IPID`` bit decode is architectural (AMD MCA/SMCA) and
  more stable than the wrapper structs, but the SMCA ``HardwareID`` is surfaced
  as a raw value rather than mapped to an IP-block name (that mapping is
  version/SoC specific and is intentionally left out to avoid asserting
  something we cannot verify here).

Validated against a real Instinct ``AMD_CRASHDUMP`` boot record; the fatal and
nonstandard (runtime) paths are implemented from the published layout and
covered by round-trip fixtures, but should be re-validated against a real
runtime record before their fields are relied upon operationally.
"""

from __future__ import annotations

import struct

# Canonical lowercase GUID strings, matching how libcper renders sectionType.
AMD_CRASHDUMP = "32ac0c78-2623-48f6-b0d0-7365725fd6ae"
AMD_GPU_NONSTANDARD_ERROR = "32ac0c78-2623-48f6-81a2-ac691780551d"

_AMD_GUIDS = frozenset({AMD_CRASHDUMP, AMD_GPU_NONSTANDARD_ERROR})

# Packed struct sizes (bytes), from amd_cper.h with #pragma pack(1).
_HDR_LEN = 128  # cper_sec_crashdump_hdr
_BOOT_LEN = 208  # cper_sec_crashdump_boot  (hdr + body_boot[80])
_FATAL_LEN = 176  # cper_sec_crashdump_fatal (hdr + body_fatal[48])
_NONSTD_LEN = 272  # cper_sec_nonstd_err     (hdr[64] + info[64] + ctx[144])

_FW_ID_OFF = 16  # cper_sec_crashdump_hdr.fw_id / nonstd hdr.fw_id
_FW_ID_LEN = 48
_CPER_ACA_REG_COUNT = 32  # u32 words in nonstd ctx.reg_dump (== 16 u64 regs)

# enum aca_reg_idx (amdgpu_aca.h) — index into the 64-bit ACA register array.
_ACA_IDX_STATUS = 1
_ACA_IDX_ADDR = 2
_ACA_IDX_IPID = 5
_ACA_IDX_SYND = 6
# All named 64-bit ACA registers (index -> name), from enum aca_reg_idx. reg_dump
# holds these as consecutive lo/hi u32 pairs (CPER_ACA_REG_COUNT=32 u32 = 16 u64).
_ACA_REG_NAMES = {
    0: "CTL",
    1: "STATUS",
    2: "ADDR",
    3: "MISC0",
    4: "CONFIG",
    5: "IPID",
    6: "SYND",
    8: "DESTAT",  # deferred-error status (same layout as STATUS)
    9: "DEADDR",  # deferred-error address
    10: "CTL_MASK",
}

# Best-effort (HardwareID, McaType) -> IP block name.
#
# Source: AMD Instinct MI300 / smu_v13_0_6 table `smu_v13_0_6_mca_ipid_table`
# (public kernel, drivers/gpu/drm/amd/pm/swsmu/smu13/smu_v13_0_6_ppt.c,
# via MCA_BANK_IPID(ip, hwid, mcatype)). RISK: these values are SoC/ASIC and
# kernel-version specific — other AMD GPUs use different numbering. Unknown
# (hwid, mcatype) pairs fall back to the raw values, never a wrong label. The
# SMU entry aggregates GFX/SDMA/MMHUB/VCN/JPEG (the kernel disambiguates those by
# MCA error code, which we do not attempt here).
_IPID_IP_BLOCK = {
    (0x96, 0x0): "UMC (HBM memory)",
    (0x01, 0x1): "SMU (GFX/SDMA/MMHUB/VCN/JPEG family)",
    (0x01, 0x2): "MP5",
    (0x50, 0x0): "PCS_XGMI (xGMI link)",
}


def is_amd_section(guid: str | None) -> bool:
    """Whether a section-type GUID is one this module decodes."""
    return bool(guid) and guid.lower() in _AMD_GUIDS


# ---- bit helpers (architectural AMD MCA/SMCA, see ACA_REG__* macros) -----


def _field(value: int, hi: int, lo: int) -> int:
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)


def _decode_mca_status(status: int) -> dict:
    """Decode an ACA/MCA_STATUS register (ACA_REG__STATUS__* bit positions)."""
    return {
        "raw": f"0x{status:016x}",
        "val": bool(_field(status, 63, 63)),
        "overflow": bool(_field(status, 62, 62)),
        "uc": bool(_field(status, 61, 61)),  # uncorrected
        "en": bool(_field(status, 60, 60)),
        "miscv": bool(_field(status, 59, 59)),
        "addrv": bool(_field(status, 58, 58)),
        "pcc": bool(_field(status, 57, 57)),  # processor context corrupt
        "errcoreid_valid": bool(_field(status, 56, 56)),
        "tcc": bool(_field(status, 55, 55)),  # task context corrupt
        "syndv": bool(_field(status, 53, 53)),
        "cecc": bool(_field(status, 46, 46)),  # correctable ECC
        "uecc": bool(_field(status, 45, 45)),  # uncorrectable ECC
        "deferred": bool(_field(status, 44, 44)),
        "poison": bool(_field(status, 43, 43)),
        "scrub": bool(_field(status, 40, 40)),
        "errcoreid": _field(status, 37, 32),
        "addr_lsb": _field(status, 29, 24),  # low-order valid bits of MCA_ADDR
        "error_code": _field(status, 15, 0),
        "error_code_ext": _field(status, 21, 16),
    }


def _decode_misc0(misc0: int) -> dict:
    """Decode an ACA/MCA_MISC0 register (ACA_REG__MISC0__* bit positions)."""
    return {
        "raw": f"0x{misc0:016x}",
        "valid": bool(_field(misc0, 63, 63)),
        "overflow": bool(_field(misc0, 48, 48)),
        "error_count": _field(misc0, 43, 32),  # ErrCnt
    }


def _decode_synd(synd: int) -> dict:
    """Decode an ACA/MCA_SYND register (ACA_REG__SYND__* bit positions)."""
    return {
        "raw": f"0x{synd:016x}",
        "error_information": _field(synd, 17, 0),
    }


def _decode_mca_ipid(ipid: int) -> dict:
    """Decode an ACA/MCA_IPID register (ACA_REG__IPID__* bit positions).

    Includes a best-effort IP-block name from (HardwareID, McaType); see
    _IPID_IP_BLOCK for the source and its version/ASIC-specific caveat.
    """
    hwid = _field(ipid, 43, 32)
    mcatype = _field(ipid, 63, 48)
    out: dict = {
        "raw": f"0x{ipid:016x}",
        "mca_type": f"0x{mcatype:04x}",
        "instance_id_hi": f"0x{_field(ipid, 47, 44):x}",
        "hardware_id": f"0x{hwid:03x}",
        "instance_id_lo": f"0x{_field(ipid, 31, 0):08x}",
    }
    ip = _IPID_IP_BLOCK.get((hwid, mcatype))
    if ip:
        out["ip_block"] = ip
        out["ip_block_source"] = "best-effort (MI300/smu_v13_0_6 mapping)"
    # For UMC (HBM), the IPID instance fields encode the physical location.
    if (hwid, mcatype) == (0x96, 0x0):
        out["umc_location"] = _decode_umc_location(ipid)
    return out


def _decode_umc_location(ipid: int) -> dict:
    """Decode HBM/UMC physical location from MCA_IPID (MI300 / umc_v12_0.h macros).

    Source: drivers/gpu/drm/amd/amdgpu/umc_v12_0.h — MCA_IPID_2_{SOCKET_ID,
    DIE_ID,UMC_CH,UMC_INST}. These are pure bit extractions from InstanceIdLo/Hi
    (validated: socket_id matches the record's OAM number). NOTE: bank / row /
    column are NOT derivable from the CPER alone — they require the live SMU
    MCA-address→physical translation (umc_v12_0_convert_error_address), so they
    are intentionally omitted rather than guessed. Values are MI300-specific.
    """
    inst_lo = _field(ipid, 31, 0)
    inst_hi = _field(ipid, 47, 44)
    return {
        "socket_id": ((inst_lo & 0x1) << 2) | (inst_hi & 0x03),
        "aid_die_id": (inst_hi >> 2) & 0x03,
        "channel": (((inst_lo >> 20) & 0x1) * 4) + ((inst_lo >> 12) & 0xF),
        "umc_instance": (inst_lo >> 21) & 0x7,
        "note": "bank/row/column require live SMU address translation (not in CPER)",
    }


def _cstr(buf: bytes) -> str:
    return buf.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def _describe_error(status: dict, ipid: dict) -> dict:
    """Turn decoded STATUS/IPID flags into a human error class + sentence."""
    if not status.get("val"):
        return {"error_class": "Invalid", "description": "No valid error logged."}
    if status.get("uc") or status.get("uecc"):
        klass = "Uncorrectable"
    elif status.get("deferred") or status.get("poison"):
        klass = "Deferred"
    elif status.get("cecc"):
        klass = "Corrected"
    else:
        klass = "Informational"

    # Nature of the error from the ECC/scrub flags. When the ECC flags already
    # convey correctability, use that phrasing directly (avoids "Uncorrectable
    # uncorrectable ECC error"); otherwise prefix the class.
    if status.get("uecc"):
        lead = "Uncorrectable ECC error"
    elif status.get("cecc"):
        lead = "Correctable ECC error"
    elif status.get("scrub"):
        lead = f"{klass} scrub error"
    else:
        lead = f"{klass} error"

    where = ipid.get("ip_block") or f"IP hwid {ipid.get('hardware_id', '?')}"
    loc = ipid.get("umc_location")
    if loc:
        where += (
            f" — socket {loc.get('socket_id')}, AID {loc.get('aid_die_id')}, "
            f"channel {loc.get('channel')}, UMC instance {loc.get('umc_instance')}"
        )
    parts = [f"{lead} on {where}"]
    extra = []
    if status.get("poison"):
        extra.append("poisoned data")
    if status.get("deferred"):
        extra.append("deferred")
    if status.get("pcc"):
        extra.append("processor context corrupt")
    if status.get("overflow"):
        extra.append("error-counter overflow")
    if extra:
        parts.append("(" + ", ".join(extra) + ")")
    return {"error_class": klass, "description": " ".join(parts) + "."}


def _common_error_fields(status_d: dict, ipid_d: dict, synd_hex: str) -> dict:
    """Derived fields shared by fatal and runtime sections (no duplication)."""
    out = dict(_describe_error(status_d, ipid_d))
    # Sub-block error code: what AMD matches against its per-IP CODE_* arrays to
    # name GFX/SDMA/MMHUB/VCN/JPEG. Derivation (public kernel): SYND[17:0] & 0xff
    # when the ACA_SYND capability is present, else STATUS.ErrorCode & 0xff. We
    # can't detect that capability from the CPER, and the code->name table is
    # SoC/version specific and not in the public headers — so we surface the raw
    # code (for lookup against AMD's guide) but do NOT assert a sub-block name.
    code = {"from_status_errorcode": (status_d.get("error_code") or 0) & 0xFF}
    try:
        synd = int(synd_hex, 16)
        code["from_synd_errorinformation"] = (synd & ((1 << 18) - 1)) & 0xFF
    except (ValueError, TypeError):
        pass
    out["sub_block_error_code"] = code
    # UMC ErrorCodeExt semantics (smu_v13_0_6): 0/9 = uncorrectable, 6 = correctable.
    ip = (ipid_d or {}).get("ip_block") or ""
    if ip.startswith("UMC"):
        ext = status_d.get("error_code_ext")
        meaning = {0: "uncorrectable", 9: "uncorrectable", 6: "correctable"}.get(ext)
        if meaning:
            out["error_code_ext_meaning"] = meaning
    return out


def _guid_str(b: bytes) -> str:
    """Render a 16-byte CPER guid_t (mixed-endian) as a canonical GUID string."""
    if len(b) < 16:
        return ""
    d1 = int.from_bytes(b[0:4], "little")
    d2 = int.from_bytes(b[4:6], "little")
    d3 = int.from_bytes(b[6:8], "little")
    d4 = b[8:16]
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{d4[0]:02x}{d4[1]:02x}-" + d4[2:].hex()


def guid_as_ascii(guid_str: str | None) -> str | None:
    """Interpret a GUID string as ASCII when it encodes printable text.

    Several CPER header GUIDs (platformID, creatorID, descriptor fruID) are
    actually ASCII-encoded identifiers (board serial, FRU part number, creator
    string) rather than real GUIDs — e.g. platformID
    "31313131-3230-472d-3339-3330362d3030" -> "111120G-93306-00". Best-effort:
    returns the text only when the bytes are predominantly printable ASCII,
    else None (so we never show garbage).
    """
    if not guid_str:
        return None
    try:
        raw = bytes.fromhex(guid_str.replace("-", ""))
    except ValueError:
        return None
    printable = sum(1 for c in raw if 0x20 <= c < 0x7F)
    if printable < max(4, (len(raw) * 3) // 4):
        return None
    text = "".join(chr(c) for c in raw if 0x20 <= c < 0x7F).strip()
    return text or None


def _decode_registers(regs: tuple) -> dict:
    """Name and decode the 64-bit ACA registers held in ctx.reg_dump.

    ``regs`` is the u32 tuple; register N is regs[2N] | regs[2N+1]<<32.
    """

    def _u64(idx: int) -> int:
        return int(regs[2 * idx]) | (int(regs[2 * idx + 1]) << 32)

    out: dict = {}
    max_idx = len(regs) // 2
    for idx, name in _ACA_REG_NAMES.items():
        if idx >= max_idx:
            continue
        val = _u64(idx)
        if name in ("STATUS", "DESTAT"):
            out[name] = _decode_mca_status(val)
        elif name == "IPID":
            out[name] = _decode_mca_ipid(val)
        elif name == "MISC0":
            out[name] = _decode_misc0(val)
        elif name == "SYND":
            out[name] = _decode_synd(val)
        else:
            out[name] = f"0x{val:016x}"
    _annotate_addresses(out)
    return out


def _annotate_addresses(registers: dict) -> None:
    """Turn raw ADDR/DEADDR hex into {raw, valid_address} using the AddrLsb.

    The low ``addr_lsb`` bits of MCA_ADDR are not part of the address; masking
    them off yields the valid physical address (only meaningful when the paired
    STATUS/DESTAT has addrv set).
    """
    for addr_reg, status_reg in (("ADDR", "STATUS"), ("DEADDR", "DESTAT")):
        raw_hex = registers.get(addr_reg)
        st = registers.get(status_reg)
        if not isinstance(raw_hex, str) or not isinstance(st, dict):
            continue
        try:
            raw = int(raw_hex, 16)
        except ValueError:
            continue
        entry = {"raw": raw_hex, "address_valid": bool(st.get("addrv"))}
        lsb = st.get("addr_lsb") or 0
        if st.get("addrv"):
            entry["valid_address"] = f"0x{(raw & ~((1 << lsb) - 1)):016x}"
        registers[addr_reg] = entry


# ---- section decoders ---------------------------------------------------


def _decode_boot(raw: bytes) -> dict:
    # cper_sec_crashdump_boot: hdr(128) + body_boot(reg_ctx_type u16, reg_arr_size
    # u16, reserved u32, reserved u64, msg[8] u64).
    if len(raw) < _BOOT_LEN:
        return {"decode_error": f"boot section {len(raw)}B < expected {_BOOT_LEN}B"}
    fw = _cstr(raw[_FW_ID_OFF : _FW_ID_OFF + _FW_ID_LEN])
    reg_ctx_type, reg_arr_size = struct.unpack_from("<HH", raw, _HDR_LEN)
    msg = list(struct.unpack_from("<8Q", raw, 144))
    return {
        "kind": "boot_crashdump",
        "firmware_id": fw,
        "reg_ctx_type": reg_ctx_type,
        "reg_arr_size": reg_arr_size,
        "oam_messages": [f"0x{m:016x}" for m in msg if m],
    }


def _decode_fatal(raw: bytes) -> dict:
    # cper_sec_crashdump_fatal: hdr(128) + body_fatal(reg_ctx_type u16,
    # reg_arr_size u16, reserved u32, reserved u64, reg_data[32]).
    if len(raw) < _FATAL_LEN:
        return {"decode_error": f"fatal section {len(raw)}B < expected {_FATAL_LEN}B"}
    fw = _cstr(raw[_FW_ID_OFF : _FW_ID_OFF + _FW_ID_LEN])
    status_lo, status_hi, addr_lo, addr_hi, ipid_lo, ipid_hi, synd_lo, synd_hi = struct.unpack_from(
        "<8I", raw, 144
    )
    status = status_lo | (status_hi << 32)
    addr = addr_lo | (addr_hi << 32)
    ipid = ipid_lo | (ipid_hi << 32)
    synd = synd_lo | (synd_hi << 32)
    status_d = _decode_mca_status(status)
    ipid_d = _decode_mca_ipid(ipid)
    synd_hex = f"0x{synd:016x}"
    # Single source of truth: the ACA registers live under "registers" only (no
    # duplicated top-level status/address/ipid/syndrome).
    registers = {
        "STATUS": status_d,
        "ADDR": f"0x{addr:016x}",
        "IPID": ipid_d,
        "SYND": _decode_synd(synd),
    }
    _annotate_addresses(registers)
    return {
        "kind": "fatal_crashdump",
        "firmware_id": fw,
        **_common_error_fields(status_d, ipid_d, synd_hex),
        "registers": registers,
    }


def _decode_nonstd(raw: bytes) -> dict:
    # cper_sec_nonstd_err: hdr(64: valid_mask u64, apic_id u64, fw_id[48]) +
    # info(64: error_type guid[16], valid_mask u64, ms_chk_mask u64,
    # target_addr_id/req_id/resp_id/instr_ptr u64) + ctx(reg_ctx_type u16,
    # reg_arr_size u16, msr_addr u32, mm_reg_addr u64, reg_dump[32] u32).
    if len(raw) < _NONSTD_LEN:
        return {"decode_error": f"nonstd section {len(raw)}B < expected {_NONSTD_LEN}B"}
    # hdr(64): valid_mask u64, apic_id u64, fw_id[48]
    hdr_valid, apic_id = struct.unpack_from("<QQ", raw, 0)
    fw = _cstr(raw[_FW_ID_OFF : _FW_ID_OFF + _FW_ID_LEN])
    # info(64)@64: error_type guid[16], valid_mask u64, ms_chk_mask u64,
    #   target_addr_id u64, req_id u64, resp_id u64, instr_ptr u64
    error_type = _guid_str(raw[64:80])
    info_valid, ms_chk, target_addr_id, req_id, resp_id, instr_ptr = struct.unpack_from(
        "<6Q", raw, 80
    )
    ms_chk_bits = {
        "err_type_valid": bool(_field(ms_chk, 0, 0)),
        "pcc_valid": bool(_field(ms_chk, 1, 1)),
        "uncorr_valid": bool(_field(ms_chk, 2, 2)),
        "precise_ip_valid": bool(_field(ms_chk, 3, 3)),
        "restartable_ip_valid": bool(_field(ms_chk, 4, 4)),
        "overflow_valid": bool(_field(ms_chk, 5, 5)),
        "err_type": _field(ms_chk, 17, 16),
        "pcc": bool(_field(ms_chk, 18, 18)),
        "uncorrected": bool(_field(ms_chk, 19, 19)),
        "precise_ip": bool(_field(ms_chk, 20, 20)),
        "restartable_ip": bool(_field(ms_chk, 21, 21)),
        "overflow": bool(_field(ms_chk, 22, 22)),
    }
    # ctx@128: reg_ctx_type u16, reg_arr_size u16, msr_addr u32, mm_reg_addr u64,
    #   reg_dump[CPER_ACA_REG_COUNT] u32
    reg_ctx_type, reg_arr_size, msr_addr = struct.unpack_from("<HHI", raw, 128)
    mm_reg_addr = struct.unpack_from("<Q", raw, 136)[0]
    regs = struct.unpack_from(f"<{_CPER_ACA_REG_COUNT}I", raw, 144)
    registers = _decode_registers(regs)

    status = registers.get("STATUS") or {}
    ipid = registers.get("IPID") or {}
    synd_reg = registers.get("SYND")
    synd_hex = synd_reg.get("raw") if isinstance(synd_reg, dict) else "0x0000000000000000"
    # Registers are the single source of truth — no duplicated top-level
    # status/address/ipid/syndrome (previously shown twice in the UI table).
    return {
        "kind": "runtime_nonstandard",
        "firmware_id": fw,
        **_common_error_fields(status, ipid, synd_hex),
        "error_type": error_type,
        "apic_id": f"0x{apic_id:016x}",
        "ms_check": ms_chk_bits,
        "registers": registers,
        "context": {
            "reg_ctx_type": reg_ctx_type,
            "reg_arr_size": reg_arr_size,
            "msr_addr": f"0x{msr_addr:08x}",
            "mm_reg_addr": f"0x{mm_reg_addr:016x}",
        },
        "identifiers": {
            "target_addr_id": f"0x{target_addr_id:016x}",
            "req_id": f"0x{req_id:016x}",
            "resp_id": f"0x{resp_id:016x}",
            "instr_ptr": f"0x{instr_ptr:016x}",
        },
        "valid_bits": {"hdr": f"0x{hdr_valid:016x}", "info": f"0x{info_valid:016x}"},
    }


def decode_amd_section(guid: str, raw: bytes) -> dict | None:
    """Decode an AMD vendor CPER section payload.

    ``guid`` is the section-type GUID (as libcper renders it); ``raw`` is the
    section body bytes. Returns a dict of decoded fields, a ``{"decode_error":…}``
    marker on layout mismatch, or None if the GUID isn't an AMD section.
    """
    if not is_amd_section(guid) or not raw:
        return None
    g = guid.lower()
    if g == AMD_GPU_NONSTANDARD_ERROR:
        return _decode_nonstd(raw)
    # AMD_CRASHDUMP covers both boot and fatal; disambiguate by packed length.
    if len(raw) == _FATAL_LEN:
        return _decode_fatal(raw)
    if len(raw) >= _BOOT_LEN:
        return _decode_boot(raw)
    return {"decode_error": f"crashdump section {len(raw)}B matches no known layout"}


def summarize_amd_section(decoded: dict | None) -> str:
    """One-line human summary of a decoded AMD section (for refined_message)."""
    if not isinstance(decoded, dict):
        return ""
    if "decode_error" in decoded:
        return f"AMD section (undecoded: {decoded['decode_error']})"
    fw = decoded.get("firmware_id") or ""
    fw_bit = f" fw {fw}" if fw else ""
    kind = decoded.get("kind")

    if kind == "boot_crashdump":
        msgs = decoded.get("oam_messages") or []
        detail = ("OAM msgs " + ", ".join(msgs)) if msgs else "no OAM messages"
        return f"AMD boot crashdump{fw_bit}: {detail}"

    if kind in ("fatal_crashdump", "runtime_nonstandard"):
        regs = decoded.get("registers") or {}
        st = regs.get("STATUS") or {}
        addr_reg = regs.get("ADDR")
        if isinstance(addr_reg, dict):
            addr = addr_reg.get("valid_address") or addr_reg.get("raw")
        else:
            addr = addr_reg
        origin = "fatal crashdump" if kind == "fatal_crashdump" else "runtime"
        # Lead with the human description; append the key technical values.
        desc = decoded.get("description") or "AMD error"
        bits = [f"MCA_STATUS={st.get('raw', '?')}"]
        ec = st.get("error_code")
        ece = st.get("error_code_ext")
        if ec:
            bits.append(f"errcode=0x{ec:x}")
        if ece:
            bits.append(f"ext=0x{ece:x}")
        if addr and addr != "0x0000000000000000":
            bits.append(f"addr={addr}")
        # HBM/UMC physical location (socket/AID/channel/UMC instance).
        loc = ((regs.get("IPID") or {}).get("umc_location")) or {}
        if loc:
            bits.append(
                f"loc=socket{loc.get('socket_id')}/AID{loc.get('aid_die_id')}"
                f"/ch{loc.get('channel')}/umc{loc.get('umc_instance')}"
            )
        cnt = (regs.get("MISC0") or {}).get("error_count")
        if cnt:
            bits.append(f"errcnt={cnt}")
        return f"AMD {origin}{fw_bit}: {desc} [" + " ".join(bits) + "]"

    return f"AMD section{fw_bit}"
