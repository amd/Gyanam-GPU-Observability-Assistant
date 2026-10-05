# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Shared datetime normalization helpers.

The project stores and compares timestamps as *naive UTC* so that tz-aware
values (which serialize with a ``+00:00`` suffix) and naive ones do not break
lexicographic range comparisons or raise ``TypeError`` on comparison. These
helpers centralize that normalization so every ingestion path agrees.
"""

from datetime import UTC, datetime


def to_naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to naive UTC.

    If ``dt`` is tz-aware it is converted to UTC and stripped of tzinfo; naive
    values are returned unchanged. ``None`` maps to ``None``.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def naive_utc_now() -> datetime:
    """Return the current time as naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)
