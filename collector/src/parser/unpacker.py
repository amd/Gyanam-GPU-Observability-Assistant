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
"""Blob unpacker for extracting compressed telemetry data."""

import gzip
import io
import logging
import os
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ExtractedFile:
    """Represents an extracted file from a blob."""

    path: Path
    original_name: str
    size: int


class BlobUnpacker:
    """Handles decompression of telemetry blobs.

    Supports zip and gzip compressed files, extracting them to a
    temporary directory for processing.
    """

    def __init__(
        self,
        temp_dir: str = "/tmp/telemetry",
        cleanup_after_parse: bool = True,
        max_blob_size: int = 104857600,  # 100MB
    ):
        """Initialize the unpacker.

        Args:
            temp_dir: Base directory for temporary extraction
            cleanup_after_parse: Whether to cleanup extracted files after parsing
            max_blob_size: Maximum blob size to process in bytes
        """
        self.temp_dir = Path(temp_dir)
        self.cleanup_after_parse = cleanup_after_parse
        self.max_blob_size = max_blob_size

        # Ensure temp directory exists
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def unpack(
        self, content: bytes, content_type: str = "", target_name: str = "unknown"
    ) -> list[ExtractedFile]:
        """Unpack compressed content to temporary files.

        Args:
            content: Raw blob content
            content_type: MIME type hint for the content
            target_name: Name of the target (for temp directory naming)

        Returns:
            List of ExtractedFile objects

        Raises:
            ValueError: If content exceeds max size or is invalid
        """
        if len(content) > self.max_blob_size:
            raise ValueError(
                f"Blob size ({len(content)} bytes) exceeds maximum ({self.max_blob_size} bytes)"
            )

        if len(content) == 0:
            raise ValueError("Empty blob content")

        # Sanitize target_name to be safe for filesystem use
        import re

        safe_target_name = re.sub(r"[^a-zA-Z0-9_-]", "_", target_name)[:50]
        if not safe_target_name:
            safe_target_name = "unknown"

        # Create a unique extraction directory
        extract_dir = Path(tempfile.mkdtemp(prefix=f"{safe_target_name}_", dir=self.temp_dir))

        logger.debug(f"Extracting blob to {extract_dir}")

        try:
            # Detect compression type and extract
            if self._is_zip(content):
                return self._extract_zip(content, extract_dir)
            elif self._is_gzip(content):
                return self._extract_gzip(content, extract_dir, safe_target_name)
            elif self._is_tar(content):
                return self._extract_tar(content, extract_dir)
            else:
                # Try to detect based on content type or treat as raw
                if "zip" in content_type.lower():
                    return self._extract_zip(content, extract_dir)
                elif "gzip" in content_type.lower() or "gz" in content_type.lower():
                    return self._extract_gzip(content, extract_dir, safe_target_name)
                elif "tar" in content_type.lower() or "application/x-tar" in content_type.lower():
                    return self._extract_tar(content, extract_dir)
                else:
                    # Try TAR as fallback for binary data
                    tar_result = self._try_extract_tar_direct(content, extract_dir)
                    if tar_result is not None:
                        return tar_result
                    # Assume raw JSON or uncompressed data
                    return self._save_raw(content, extract_dir, safe_target_name)

        except Exception as e:
            # Cleanup on error
            self._cleanup_dir(extract_dir)
            raise ValueError(f"Failed to extract blob: {e}") from e

    def _is_zip(self, content: bytes) -> bool:
        """Check if content is a ZIP file."""
        return content[:4] == b"PK\x03\x04"

    def _is_gzip(self, content: bytes) -> bool:
        """Check if content is gzip compressed."""
        return content[:2] == b"\x1f\x8b"

    def _is_tar(self, content: bytes) -> bool:
        """Check if content is a TAR file.

        TAR files have 'ustar' magic at offset 257.
        """
        if len(content) < 262:
            return False
        return content[257:262] == b"ustar"

    def _try_extract_tar_direct(
        self, content: bytes, extract_dir: Path
    ) -> list[ExtractedFile] | None:
        """Try to extract as TAR in a single pass. Returns None if not a valid TAR."""
        try:
            return self._extract_tar(content, extract_dir)
        except ValueError:
            return None

    def _extract_zip(self, content: bytes, extract_dir: Path) -> list[ExtractedFile]:
        """Extract a ZIP archive.

        Includes decompressed size tracking to protect against zip bombs.
        """
        # Maximum total decompressed size (500MB) to prevent zip bombs
        max_decompressed_size = 500 * 1024 * 1024
        extracted = []
        total_decompressed = 0

        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            for info in zf.infolist():
                # Skip directories
                if info.is_dir():
                    continue

                # Skip hidden/system files
                if info.filename.startswith(("__MACOSX", ".")):
                    continue

                # Extract file with streaming size check
                # Deduplicate basenames to avoid overwrites
                basename = Path(info.filename).name
                extracted_path = extract_dir / basename
                if extracted_path.exists():
                    stem = extracted_path.stem
                    suffix = extracted_path.suffix
                    counter = 1
                    while extracted_path.exists():
                        extracted_path = extract_dir / f"{stem}_{counter}{suffix}"
                        counter += 1
                file_size = 0

                with zf.open(info) as src, open(extracted_path, "wb") as dst:
                    while True:
                        chunk = src.read(65536)  # 64KB chunks
                        if not chunk:
                            break
                        file_size += len(chunk)
                        total_decompressed += len(chunk)
                        if total_decompressed > max_decompressed_size:
                            # Clean up and abort
                            dst.close()
                            self._cleanup_dir(extract_dir)
                            raise ValueError(
                                f"Total decompressed size exceeds limit "
                                f"({total_decompressed / 1024 / 1024:.1f}MB > "
                                f"{max_decompressed_size / 1024 / 1024}MB)"
                            )
                        dst.write(chunk)

                extracted.append(
                    ExtractedFile(path=extracted_path, original_name=info.filename, size=file_size)
                )

                logger.debug(f"Extracted: {info.filename} ({file_size} bytes)")

        return extracted

    def _extract_gzip(
        self, content: bytes, extract_dir: Path, target_name: str
    ) -> list[ExtractedFile]:
        """Extract gzip compressed data.

        Uses streaming decompression to handle large files safely.
        If the decompressed content is a tar archive, extracts it further.
        """
        # Maximum decompressed size (500MB) to prevent zip bombs
        max_decompressed_size = 500 * 1024 * 1024

        try:
            decompressed = bytearray()
            with gzip.GzipFile(fileobj=io.BytesIO(content), mode="rb") as gz:
                while True:
                    chunk = gz.read(65536)  # 64KB chunks
                    if not chunk:
                        break
                    decompressed.extend(chunk)
                    if len(decompressed) > max_decompressed_size:
                        raise ValueError(
                            f"Decompressed size exceeds limit "
                            f"({len(decompressed) / 1024 / 1024:.1f}MB > "
                            f"{max_decompressed_size / 1024 / 1024}MB)"
                        )
        except gzip.BadGzipFile as e:
            raise ValueError(f"Invalid gzip data: {e}")
        except OSError as e:
            raise ValueError(f"Error decompressing gzip data: {e}")

        decompressed_bytes = bytes(decompressed)
        logger.debug(f"Decompressed gzip: {len(decompressed_bytes)} bytes")

        # Check if the decompressed content is a tar archive
        if self._is_tar(decompressed_bytes):
            logger.debug("Decompressed gzip content is a tar archive, extracting further")
            return self._extract_tar(decompressed_bytes, extract_dir)

        # Otherwise save as a single file
        # Detect whether it looks like text/JSON or binary
        try:
            decompressed_bytes.decode("utf-8")
            extension = ".json"
        except UnicodeDecodeError:
            extension = ".bin"

        output_path = extract_dir / f"{target_name}_telemetry{extension}"
        with open(output_path, "wb") as out_file:
            out_file.write(decompressed_bytes)

        return [
            ExtractedFile(
                path=output_path,
                original_name=f"{target_name}_telemetry{extension}",
                size=len(decompressed_bytes),
            )
        ]

    def _extract_tar(self, content: bytes, extract_dir: Path) -> list[ExtractedFile]:
        """Extract a TAR archive.

        Extracts all files from TAR, including redfish-tree.log.
        """
        max_decompressed_size = 500 * 1024 * 1024  # 500MB limit
        extracted = []
        total_decompressed = 0

        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r") as tar:
                for member in tar.getmembers():
                    # Skip directories
                    if member.isdir():
                        continue

                    # Skip non-regular files
                    if not member.isfile():
                        continue

                    # Check size limits
                    if member.size > max_decompressed_size:
                        raise ValueError(
                            f"File {member.name} exceeds size limit "
                            f"({member.size / 1024 / 1024:.1f}MB)"
                        )

                    total_decompressed += member.size
                    if total_decompressed > max_decompressed_size:
                        raise ValueError(
                            f"Total decompressed size exceeds limit "
                            f"({total_decompressed / 1024 / 1024:.1f}MB)"
                        )

                    # Extract using basename (flatten directory structure)
                    basename = os.path.basename(member.name)
                    if not basename or basename.startswith("."):
                        continue

                    # Deduplicate basenames to avoid overwrites
                    extracted_path = extract_dir / basename
                    if extracted_path.exists():
                        stem = Path(basename).stem
                        suffix = Path(basename).suffix
                        counter = 1
                        while extracted_path.exists():
                            extracted_path = extract_dir / f"{stem}_{counter}{suffix}"
                            counter += 1

                    # Extract file
                    with tar.extractfile(member) as src:
                        if src is not None:
                            with open(extracted_path, "wb") as dst:
                                shutil.copyfileobj(src, dst)

                            extracted.append(
                                ExtractedFile(
                                    path=extracted_path, original_name=basename, size=member.size
                                )
                            )

                            logger.debug(f"Extracted from TAR: {basename} ({member.size} bytes)")

        except tarfile.TarError as e:
            raise ValueError(f"Invalid TAR archive: {e}")
        except OSError as e:
            raise ValueError(f"Error extracting TAR: {e}")

        logger.info(f"Extracted {len(extracted)} files from TAR archive")
        return extracted

    def _save_raw(self, content: bytes, extract_dir: Path, target_name: str) -> list[ExtractedFile]:
        """Save raw (uncompressed) content."""
        # Try to detect if it's JSON
        try:
            content.decode("utf-8")
            extension = ".json"
        except UnicodeDecodeError:
            extension = ".bin"

        output_path = extract_dir / f"{target_name}_telemetry{extension}"
        with open(output_path, "wb") as f:
            f.write(content)

        logger.debug(f"Saved raw content: {len(content)} bytes")

        return [
            ExtractedFile(
                path=output_path,
                original_name=f"{target_name}_telemetry{extension}",
                size=len(content),
            )
        ]

    def cleanup(self, files: list[ExtractedFile]) -> None:
        """Clean up extracted files and their parent directories.

        Args:
            files: List of ExtractedFile objects to clean up
        """
        if not self.cleanup_after_parse:
            return

        cleaned_dirs = set()

        for file in files:
            try:
                if file.path.exists():
                    file.path.unlink()
                    cleaned_dirs.add(file.path.parent)
            except OSError as e:
                logger.warning(f"Failed to delete {file.path}: {e}")

        # Remove empty parent directories
        for dir_path in cleaned_dirs:
            try:
                if dir_path.exists() and not any(dir_path.iterdir()):
                    dir_path.rmdir()
            except OSError:
                # Directory is non-empty or otherwise un-removable — leave it
                # for the next cleanup cycle to retry. Best-effort cleanup.
                pass

    def _cleanup_dir(self, dir_path: Path) -> None:
        """Clean up a directory and all its contents."""
        try:
            shutil.rmtree(dir_path, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to cleanup {dir_path}: {e}")

    def cleanup_old_files(self, max_age_seconds: int = 3600) -> int:
        """Clean up old extracted files.

        Args:
            max_age_seconds: Maximum age of files to keep

        Returns:
            Number of files/directories cleaned up
        """
        import time

        cleaned = 0
        current_time = time.time()

        for item in self.temp_dir.iterdir():
            try:
                if item.is_dir():
                    mtime = item.stat().st_mtime
                    if current_time - mtime > max_age_seconds:
                        shutil.rmtree(item)
                        cleaned += 1
                elif item.is_file():
                    mtime = item.stat().st_mtime
                    if current_time - mtime > max_age_seconds:
                        item.unlink()
                        cleaned += 1
            except OSError as e:
                logger.warning(f"Failed to cleanup {item}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} old files/directories")

        return cleaned
