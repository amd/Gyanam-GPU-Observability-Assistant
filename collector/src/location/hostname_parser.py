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
"""Derive physical placement from a host naming convention.

There is no universal location-encoding hostname standard — real schemes vary
in field order and presence, mix prefix-tagged tokens (a data-hall token, a
rack token) with positional ones, and carry noise (environment/role tokens).
So parsing is driven by operator-editable config, in two complementary passes:

1. ``name_patterns`` — full-hostname regexes with named capture groups
   (``site``/``hall``/``row``/``rack``/``rack_u``/``height``). First match wins.
   Use these for schemes whose location fields are positional, not tagged.

2. ``token_rules`` — ordered (field, single-token-regex) rules. The host is
   split on ``- _ .`` and each token is matched against the rules; the first
   rule that claims a still-unset field assigns it. Unknown tokens (domain
   labels, roles, environments) are simply ignored, and a partial placement is
   accepted. This handles prefix-tagged schemes with variable field order.

The parser never raises: a hostname it cannot decode yields ``None`` and the
system renders in the "Unplaced" tray.
"""

from __future__ import annotations

import logging
import re

from .models import Placement

logger = logging.getLogger(__name__)

# Fields a rule / capture group may populate. "height" maps to Placement.height_u.
_VALID_FIELDS = frozenset({"site", "hall", "row", "rack", "rack_u", "height"})
_INT_FIELDS = frozenset({"rack_u", "height"})

# Split a host into location tokens. Domain dots and the common -, _ delimiters
# all separate fields; the domain labels that result are harmless (they match no
# rule). Applied after the FQDN is reduced to its first label.
_TOKEN_SPLIT = re.compile(r"[-_.]+")


def _assign(placement: Placement, field: str, raw: str) -> None:
    """Set ``field`` on ``placement`` from a captured string, if not already set."""
    attr = "height_u" if field == "height" else field
    if getattr(placement, attr) is not None:
        return
    value: str | int = raw
    if field in _INT_FIELDS:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return
    setattr(placement, attr, value)


def _try_name_patterns(host: str, patterns: list[str]) -> Placement | None:
    """Match the whole host against positional regexes with named groups."""
    for pattern in patterns:
        try:
            match = re.match(pattern, host, re.IGNORECASE)
        except re.error:
            logger.warning("Invalid location name_pattern skipped: %r", pattern)
            continue
        if not match:
            continue
        placement = Placement()
        for field, raw in match.groupdict().items():
            if raw is not None and field in _VALID_FIELDS:
                _assign(placement, field, raw)
        if placement.has_location():
            return placement
    return None


def _try_token_rules(host: str, rules: list[tuple[str, str]]) -> Placement | None:
    """Scan delimiter-split tokens against ordered (field, token-regex) rules."""
    placement = Placement()
    for token in _TOKEN_SPLIT.split(host):
        if not token:
            continue
        for field, pattern in rules:
            if field not in _VALID_FIELDS:
                continue
            try:
                match = re.fullmatch(pattern, token, re.IGNORECASE)
            except re.error:
                logger.warning("Invalid location token rule skipped: %r", pattern)
                continue
            if match:
                # Prefer an explicit capture group; fall back to the whole token.
                raw = match.group(1) if match.groups() else match.group(0)
                _assign(placement, field, raw)
                break
    return placement if placement.has_location() else None


def parse_hostname(host: str, config=None) -> Placement | None:
    """Resolve a :class:`Placement` from a hostname, or ``None`` if undecodable.

    Args:
        host: The target host (FQDN or short name).
        config: A ``LocationConfig``; loaded from the global app config when None.

    Returns:
        A hostname-sourced Placement, or None when no rule matches.
    """
    if not host or not host.strip():
        return None

    if config is None:
        from ..config import get_config

        config = get_config().location

    # Reduce an FQDN to its first label so domain components never masquerade
    # as location fields, then normalise case for matching.
    short = host.strip().split(".", 1)[0].lower() if "." in host else host.strip().lower()

    name_patterns = list(getattr(config, "name_patterns", []) or [])
    token_rules = [(rule.field, rule.pattern) for rule in getattr(config, "token_rules", []) or []]

    placement = _try_name_patterns(short, name_patterns) or _try_token_rules(short, token_rules)
    if placement is None:
        return None

    placement.source = "hostname"
    # Default height + unit type so a parsed system paints a sensible block in a
    # standard rack unless a more authoritative source says otherwise.
    if placement.height_u is None:
        placement.height_u = getattr(config, "default_system_height_u", 4)
    if placement.unit_type is None:
        placement.unit_type = getattr(config, "default_unit_type", "OpenU")
    return placement


def parse_system_location(
    name: str | None, host: str | None = None, config=None
) -> Placement | None:
    """Resolve placement from a target's naming, preferring the system name.

    In practice the location convention lives in the **system name**, while the
    ``host`` is the BMC management address (often a bare IP). So the name is
    tried first; the BMC host is only a fallback for deployments that encode
    location there instead. Returns None when neither decodes.
    """
    placement = parse_hostname(name, config) if name else None
    if placement is None and host:
        placement = parse_hostname(host, config)
    return placement
