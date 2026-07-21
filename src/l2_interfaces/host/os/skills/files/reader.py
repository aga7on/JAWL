"""
Skills for reading files.
Includes mechanisms for mass reading and context window protection.
"""

import asyncio
import hashlib
from typing import Literal

from src.utils.logger import main_logger
from src.utils._tools import format_size

from src.l2_interfaces.host.os.client import HostOSClient, HostOSAccessLevel
from src.l2_interfaces.host.os.decorators import require_access

from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents


class HostOSReader:
    """Agent skills for secure file reading."""

    def __init__(self, host_os_client: HostOSClient):
        self.host_os = host_os_client

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def read_file(
        self, filepath: str, read_from: Literal["head", "tail"] = "head"
    ) -> SkillResult:
        """
        Reads file content.
        Note: use framework root relative path (e.g., 'sandbox/file.txt').
        """

        max_chars = self.host_os.config.file_read_max_chars

        try:
            safe_path = self.host_os.validate_path(filepath, is_write=False)

            if not safe_path.is_file():
                return SkillResult.fail(
                    f"Error: Path is not a file or does not exist ({filepath})."
                )

            def _read_fast():
                with open(safe_path, "rb") as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    f.seek(0)
                    digest = hashlib.sha256()
                    while chunk := f.read(1024 * 1024):
                        digest.update(chunk)
                    sha256 = digest.hexdigest()

                    if file_size <= max_chars:
                        f.seek(0)
                        return (
                            f.read().decode("utf-8", errors="replace").replace("\r\n", "\n"),
                            False,
                            file_size,
                            sha256,
                        )

                    if read_from == "tail":
                        f.seek(file_size - max_chars)
                        return (
                            f.read().decode("utf-8", errors="replace").replace("\r\n", "\n"),
                            True,
                            file_size,
                            sha256,
                        )
                    else:
                        f.seek(0)
                        return (
                            f.read(max_chars).decode("utf-8", errors="replace"),
                            True,
                            file_size,
                            sha256,
                        )

            content, is_truncated, file_size, sha256 = await asyncio.to_thread(
                _read_fast
            )

            size_str = format_size(file_size)
            header = (
                f"[File: {safe_path.name} | Read: {len(content)} chars | "
                f"Original size: {size_str} | SHA-256: {sha256}]\n{'='*40}\n"
            )

            if is_truncated:
                if read_from == "tail":
                    content = f"...[File truncated from the beginning. Showing the last {max_chars} bytes]...\n{content}"
                else:
                    content = f"{content}\n...[File truncated from the end. Showing the first {max_chars} bytes]..."

            main_logger.info(
                f"[Host OS] Read file ({read_from}): {safe_path.name} ({size_str})"
            )
            return SkillResult.ok(header + content)

        except PermissionError as e:
            return SkillResult.fail(str(e))

        except Exception as e:
            return SkillResult.fail(f"Error reading file: {e}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def read_file_range(
        self,
        filepath: str,
        start_line: int = 1,
        end_line: int | None = None,
        max_lines: int = 400,
    ) -> SkillResult:
        """Read an exact line range with stable line numbers and file SHA-256.

        Use this instead of loading a whole large file when only a symbol or error
        location is relevant. At most ``max_lines`` (1-1000) and the configured
        file character budget are returned.
        """

        if start_line < 1:
            return SkillResult.fail("start_line must be at least 1.")
        if max_lines < 1 or max_lines > 1000:
            return SkillResult.fail("max_lines must be between 1 and 1000.")
        requested_end = end_line if end_line is not None else start_line + max_lines - 1
        if requested_end < start_line:
            return SkillResult.fail("end_line must be greater than or equal to start_line.")
        effective_end = min(requested_end, start_line + max_lines - 1)

        try:
            safe_path = self.host_os.validate_path(filepath, is_write=False)
            if not safe_path.is_file():
                return SkillResult.fail(
                    f"Error: Path is not a file or does not exist ({filepath})."
                )
            max_chars = self.host_os.config.file_read_max_chars

            def _read_range():
                digest = hashlib.sha256()
                selected = []
                selected_chars = 0
                total_lines = 0
                chars_truncated = False
                with open(safe_path, "rb") as stream:
                    for line_number, raw_line in enumerate(stream, start=1):
                        digest.update(raw_line)
                        total_lines = line_number
                        if line_number < start_line or line_number > effective_end:
                            continue
                        decoded = raw_line.decode("utf-8", errors="replace").rstrip(
                            "\r\n"
                        )
                        rendered = f"{line_number:>6} | {decoded}"
                        if selected_chars + len(rendered) + 1 > max_chars:
                            chars_truncated = True
                            continue
                        selected.append(rendered)
                        selected_chars += len(rendered) + 1
                return selected, total_lines, digest.hexdigest(), chars_truncated

            selected, total_lines, sha256, chars_truncated = await asyncio.to_thread(
                _read_range
            )
            if start_line > total_lines and total_lines > 0:
                return SkillResult.fail(
                    f"start_line {start_line} is beyond the file's {total_lines} lines."
                )
            if total_lines == 0:
                return SkillResult.ok(
                    f"[File: {safe_path.name} | Empty | SHA-256: {sha256}]"
                )

            actual_end = min(effective_end, total_lines)
            header = (
                f"[File: {safe_path.name} | Lines: {start_line}-{actual_end} "
                f"of {total_lines} | SHA-256: {sha256}]"
            )
            notices = []
            if requested_end > effective_end:
                notices.append(
                    f"Requested range capped at max_lines={max_lines}."
                )
            if chars_truncated:
                notices.append(
                    f"Output capped at {max_chars} configured characters."
                )
            body = "\n".join(selected)
            if notices:
                body += "\n... [" + " ".join(notices) + "]"
            main_logger.info(
                f"[Host OS] Read line range {start_line}-{actual_end}: "
                f"{safe_path.name}"
            )
            return SkillResult.ok(header + ("\n" + body if body else ""))
        except PermissionError as e:
            return SkillResult.fail(str(e))
        except Exception as e:
            return SkillResult.fail(f"Error reading file range: {e}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def read_files_in_directory(
        self, path: str = ".", max_files: int = 10, recursive: bool = False
    ) -> SkillResult:
        """
        Reads multiple files in dir.
        Skips binaries.

        recursive: Includes subdirectories if True.
        """

        try:
            safe_path = self.host_os.validate_path(path, is_write=False)

            if not safe_path.is_dir():
                return SkillResult.fail(f"Error: Path is not a directory ({path}).")

            total_max_chars = self.host_os.config.file_read_max_chars * 2

            def _read_all():
                results = []
                total_chars = 0
                files_read = 0

                ignore_dirs = {
                    ".git",
                    "venv",
                    ".venv",
                    "env",
                    "__pycache__",
                    "node_modules",
                    ".pytest_cache",
                }

                iterator = safe_path.rglob("*") if recursive else safe_path.iterdir()
                items = sorted([p for p in iterator if p.is_file()])

                for item in items:
                    rel_path = item.relative_to(safe_path)

                    if recursive and any(part in ignore_dirs for part in rel_path.parts):
                        continue

                    if files_read >= max_files:
                        results.append(
                            f"\n... [Reached the limit of reading {max_files} files. Others hidden]"
                        )
                        break

                    try:
                        with open(item, "r", encoding="utf-8") as f:
                            content = f.read()

                        if not content.strip():
                            continue

                        chars_left = total_max_chars - total_chars
                        if chars_left <= 0:
                            results.append(
                                "\n... [Reached global character reading limit. Operation aborted]"
                            )
                            break

                        if len(content) > chars_left:
                            content = (
                                content[:chars_left]
                                + "\n... [File truncated due to system limits]"
                            )

                        total_chars += len(content)

                        results.append(f"--- File: {rel_path.as_posix()} ---\n{content}\n")
                        files_read += 1

                    except UnicodeDecodeError:
                        continue
                    except Exception as e:
                        results.append(
                            f"--- File: {rel_path.as_posix()} ---\n[Read error: {e}]\n"
                        )
                        files_read += 1

                return results, files_read, total_chars

            results, files_read, total_chars = await asyncio.to_thread(_read_all)

            if not results:
                return SkillResult.ok(
                    f"Directory '{path}' is empty or contains only binary files."
                )

            size_str = format_size(total_chars)
            header = f"[Files read: {files_read} from directory {safe_path.name} | Total size: {size_str}]\n{'='*60}\n\n"

            main_logger.info(
                f"[Host OS] Mass reading of {files_read} files from directory: {safe_path.name}"
            )
            return SkillResult.ok(header + "\n".join(results))

        except PermissionError as e:
            return SkillResult.fail(str(e))
        except Exception as e:
            return SkillResult.fail(f"Error during mass file reading: {e}")
