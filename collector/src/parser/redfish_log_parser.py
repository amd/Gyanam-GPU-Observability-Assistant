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
"""Parser for redfish-tree.log files from AMD diagnostic data."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class RedfishLogParser:
    """Parses redfish-tree.log to extract JSON data following specific URLs.

    The redfish-tree.log file contains multiple JSON blocks appended together,
    each preceded by a URL line. This parser extracts the JSON block that
    follows a specific URL pattern.
    """

    def __init__(self, target_url: str = "redfish/v1/TelemetryService/MetricReports/All"):
        """Initialize the parser.

        Args:
            target_url: The URL pattern to search for (default is MetricReports/All)
        """
        self.target_url = target_url

    def parse_file(self, log_path: Path) -> dict | None:
        """Parse redfish-tree.log and extract JSON after target URL.

        Args:
            log_path: Path to the redfish-tree.log file

        Returns:
            Parsed JSON dict or None if not found
        """
        if not log_path.exists():
            logger.warning(f"redfish-tree.log not found at {log_path}")
            return None

        try:
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError as e:
            logger.error(f"Failed to read {log_path}: {e}")
            return None

        return self.parse_content(content)

    def parse_content(self, content: str) -> dict | None:
        """Parse redfish log content and extract JSON after target URL.

        The log format typically looks like:
        GET redfish/v1/SomeEndpoint
        {"json": "data", ...}
        GET redfish/v1/TelemetryService/MetricReports/All
        {"MetricReportDefinitions": [...], ...}
        GET redfish/v1/AnotherEndpoint
        {...}

        Args:
            content: Full content of redfish-tree.log

        Returns:
            Parsed JSON dict or None if not found
        """
        # Find the line with our target URL
        lines = content.split("\n")

        for i, line in enumerate(lines):
            if self.target_url in line:
                logger.debug(f"Found target URL at line {i}: {line.strip()}")

                # Extract JSON block starting from the next line
                json_block = self._extract_json_block(lines, i + 1)

                if json_block:
                    try:
                        data = json.loads(json_block)
                        logger.info(f"Successfully parsed JSON block ({len(json_block)} chars)")
                        return data  # type: ignore[no-any-return]
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse JSON block: {e}")
                        logger.debug(f"JSON preview: {json_block[:500]}")
                        return None

        logger.warning(f"Target URL '{self.target_url}' not found in redfish-tree.log")
        return None

    def _extract_json_block(self, lines: list[str], start_idx: int) -> str | None:
        """Extract a JSON block starting from the given line index.

        Uses json.JSONDecoder.raw_decode() to correctly find the end of the
        JSON object, handling braces inside string values.

        Args:
            lines: All lines from the log file
            start_idx: Index to start reading from

        Returns:
            JSON string or None
        """
        if start_idx >= len(lines):
            return None

        # Collect candidate lines until we hit another URI or run out
        candidate_lines = []
        found_brace = False

        for i in range(start_idx, len(lines)):
            line = lines[i]

            # Skip empty lines and separator lines before JSON starts
            if not found_brace:
                stripped = line.strip()
                if not stripped or stripped.startswith("="):
                    continue

            # Stop if we hit another URI line after finding JSON content
            if line.strip().startswith("URI:"):
                if found_brace:
                    break
                else:
                    continue

            if "{" in line:
                found_brace = True

            if found_brace:
                candidate_lines.append(line)

        if not candidate_lines:
            return None

        # Join and use raw_decode to find exact JSON boundary
        text = "\n".join(candidate_lines)

        # Find the first '{' to start decoding from
        brace_pos = text.find("{")
        if brace_pos == -1:
            return None

        decoder = json.JSONDecoder()
        try:
            _, end_idx = decoder.raw_decode(text, brace_pos)
            return text[brace_pos:end_idx]
        except json.JSONDecodeError:
            # Fall back to returning everything we collected
            return text

    def save_extracted_json(self, log_path: Path, output_path: Path) -> bool:
        """Parse log and save extracted JSON to a file.

        Args:
            log_path: Path to redfish-tree.log
            output_path: Where to save the extracted JSON

        Returns:
            True if successful, False otherwise
        """
        data = self.parse_file(log_path)

        if data is None:
            return False

        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            logger.info(f"Saved extracted JSON to {output_path}")
            return True

        except OSError as e:
            logger.error(f"Failed to write JSON to {output_path}: {e}")
            return False
