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
"""Parse physical placement from a Redfish ``Chassis.Location`` object.

This is the authoritative placement source when a BMC populates it. The common
Redfish ``Location`` object exposes a ``Placement`` sub-object with ``Row``,
``Rack``, ``RackOffset`` (the bottom-most rack unit) and ``RackOffsetUnits``
(``EIA_310`` or ``OpenU``); ``PostalAddress.Room`` gives a coarse hall name.

The inventory collector (``inventory/collector.py``) fetches the Chassis
resource and passes its ``Location`` here; this module just parses it.
"""

from __future__ import annotations

from .models import Placement


def placement_from_location(location: dict | None, default_unit_type: str) -> Placement:
    """Build a Placement from one Redfish ``Location`` object."""
    placement = Placement()
    if not isinstance(location, dict):
        return placement

    postal = location.get("PostalAddress")
    if isinstance(postal, dict):
        room = postal.get("Room") or postal.get("Building")
        if room:
            placement.hall = str(room)

    pl = location.get("Placement")
    if isinstance(pl, dict):
        if pl.get("Row"):
            placement.row = str(pl["Row"])
        if pl.get("Rack"):
            placement.rack = str(pl["Rack"])
        offset = pl.get("RackOffset")
        if isinstance(offset, int) and not isinstance(offset, bool):
            placement.rack_u = offset
        units = pl.get("RackOffsetUnits")
        placement.unit_type = units if units in ("EIA_310", "OpenU") else default_unit_type

    return placement
