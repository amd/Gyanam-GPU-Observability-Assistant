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
"""Reconcile name-derived placement against the BMC-reported placement."""

from __future__ import annotations


def _norm(value) -> str | None:
    s = ("" if value is None else str(value)).strip().lower()
    return s or None


def check_location(name_placement, redfish_placement) -> str | None:
    """Compare two placements, returning "match", "mismatch", or None.

    Only the hall and rack are compared (the fields a hostname typically
    encodes), case-insensitively. Returns None when either side lacks the data
    to make a meaningful comparison.
    """
    if name_placement is None or redfish_placement is None:
        return None

    pairs = []
    for attr in ("hall", "rack"):
        n = _norm(getattr(name_placement, attr, None))
        r = _norm(getattr(redfish_placement, attr, None))
        if n is not None and r is not None:
            pairs.append((n, r))

    if not pairs:
        return None  # nothing comparable on both sides
    return "match" if all(n == r for n, r in pairs) else "mismatch"
