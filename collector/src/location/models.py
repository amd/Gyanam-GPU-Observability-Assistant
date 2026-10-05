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
"""Placement value object and provenance precedence."""

from __future__ import annotations

from dataclasses import dataclass

# Placement provenance, lowest-trust to highest-trust. A re-resolution may only
# overwrite an existing placement with one of equal-or-higher rank, so an
# operator's manual correction is never clobbered by an automated re-run.
#
# "pinned" is the highest rank: it marks a system an operator deliberately
# cleared (returned to Unplaced). Carrying it as a source — rather than nulling
# the source — means automated hostname/Redfish resolution cannot silently
# re-place it; only an explicit manual placement (force=True) overrides it.
PINNED_SOURCE = "pinned"
SOURCE_PRECEDENCE: dict[str, int] = {
    "hostname": 1,
    "redfish": 2,
    "manual": 3,
    PINNED_SOURCE: 4,
}


def source_rank(source: str | None) -> int:
    """Return the trust rank of a placement source (0 = unknown/unset)."""
    return SOURCE_PRECEDENCE.get(source or "", 0)


@dataclass
class Placement:
    """Where a system sits in the data hall.

    Every field is optional: a partially-known placement (e.g. rack known but
    rack-unit unknown) is still useful and renders in the data-hall view. Maps
    onto the Redfish ``Location.Placement`` object — ``rack_u`` is the
    bottom-most rack unit (Redfish ``RackOffset``); ``height_u`` has no Redfish
    equivalent and defaults to 1 when a system is placed.
    """

    site: str | None = None
    hall: str | None = None
    row: str | None = None
    rack: str | None = None
    rack_u: int | None = None
    height_u: int | None = None
    unit_type: str | None = None  # "EIA_310" | "OpenU"
    source: str | None = None  # "hostname" | "redfish" | "manual"

    def has_location(self) -> bool:
        """True if any physical coordinate was resolved."""
        return any(v is not None and v != "" for v in (self.hall, self.row, self.rack, self.rack_u))

    def to_columns(self) -> dict:
        """Map to the Target ``loc_*`` column keyword arguments."""
        return {
            "loc_site": self.site,
            "loc_hall": self.hall,
            "loc_row": self.row,
            "loc_rack": self.rack,
            "loc_rack_u": self.rack_u,
            "loc_rack_u_height": self.height_u,
            "loc_unit_type": self.unit_type,
            "loc_source": self.source,
        }

    @classmethod
    def from_target(cls, target) -> Placement:
        """Build a Placement from a Target ORM row's ``loc_*`` columns."""
        return cls(
            site=target.loc_site,
            hall=target.loc_hall,
            row=target.loc_row,
            rack=target.loc_rack,
            rack_u=target.loc_rack_u,
            height_u=target.loc_rack_u_height,
            unit_type=target.loc_unit_type,
            source=target.loc_source,
        )
