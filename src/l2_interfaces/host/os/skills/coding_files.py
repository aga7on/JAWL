"""Task-scoped file operations for managed coding workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.skills.files.editor import HostOSEditor
from src.l2_interfaces.host.os.skills.files.reader import HostOSReader
from src.l2_interfaces.host.os.skills.files.search import HostOSSearch
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents


class HostOSCodingFiles:
    """Delegate bounded file tools through a stable task/workspace handle."""

    def __init__(
        self,
        host_os_client: HostOSClient,
        workspaces: HostOSCodingWorkspaces,
        reader: HostOSReader,
        editor: HostOSEditor,
        search: HostOSSearch,
        context: Optional[HostOSCodingContext] = None,
    ) -> None:
        self.host_os = host_os_client
        self.workspaces = workspaces
        self.reader = reader
        self.editor = editor
        self.search = search
        self.context = context or HostOSCodingContext(host_os_client)

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _validated_symbol(symbol: str) -> str:
        normalized = symbol.strip() if isinstance(symbol, str) else ""
        if not normalized or len(normalized) > 128 or any(
            not re.fullmatch(r"[A-Za-z_$][\w$]*", part)
            for part in normalized.split(".")
        ):
            raise ValueError(
                "symbol must be a dotted identifier of at most 128 characters."
            )
        return normalized

    @staticmethod
    def _symbol_text(source: str, span: Dict[str, Any]) -> str:
        lines = source.splitlines(keepends=True)
        start = int(span["start_line"])
        end = int(span["end_line"])
        if start < 1 or end < start or end > len(lines):
            raise ValueError("Parser returned an invalid symbol line span.")
        return "".join(lines[start - 1 : end])

    def _one_definition(
        self, path: Path, source: str, symbol: str
    ) -> Dict[str, Any]:
        spans, error, backend = self.context.definition_spans(path, source, symbol)
        if error:
            raise ValueError(f"Structural edit unavailable: {error}.")
        if len(spans) != 1:
            raise ValueError(
                f"Structural edit requires exactly one parser-backed definition "
                f"for '{symbol}', found {len(spans)} ({backend})."
            )
        return spans[0]

    @staticmethod
    def _normalized_replacement(
        replacement: str, original: str, indentation: str
    ) -> str:
        clean = textwrap.dedent(replacement).strip("\r\n")
        if not clean:
            raise ValueError("replacement cannot be empty.")
        newline = "\r\n" if "\r\n" in original else "\n"
        normalized = clean.replace("\r\n", "\n").replace("\r", "\n")
        indented = newline.join(
            indentation + line if line else "" for line in normalized.split("\n")
        )
        if original.endswith(("\r\n", "\n", "\r")):
            indented += newline
        return indented

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def read_coding_file_range(
        self,
        task_id: str,
        relative_path: str,
        start_line: int = 1,
        end_line: Optional[int] = None,
        max_lines: int = 400,
    ) -> SkillResult:
        """Read a numbered file range from a managed task workspace."""

        try:
            path = self.workspaces.resolve_workspace_path(
                task_id, relative_path, is_write=False
            )
            return await self.reader.read_file_range(
                str(path),
                start_line=start_line,
                end_line=end_line,
                max_lines=max_lines,
            )
        except (PermissionError, ValueError) as exc:
            return SkillResult.fail(str(exc))

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def search_coding_workspace(
        self,
        task_id: str,
        query: str,
        regex: bool = False,
        case_sensitive: bool = False,
        globs: Optional[List[str]] = None,
        context_lines: int = 2,
        max_matches: int = 50,
    ) -> SkillResult:
        """Search only inside a managed task workspace."""

        try:
            workspace = self.workspaces.resolve_workspace_path(
                task_id, ".", is_write=False
            )
            return await self.search.search_repository(
                query=query,
                path=str(workspace),
                regex=regex,
                case_sensitive=case_sensitive,
                globs=globs,
                context_lines=context_lines,
                max_matches=max_matches,
            )
        except (PermissionError, ValueError) as exc:
            return SkillResult.fail(str(exc))

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def inspect_coding_symbol(
        self,
        task_id: str,
        relative_path: str,
        symbol: str,
        max_chars: int = 30000,
    ) -> SkillResult:
        """Inspect one exact parser-backed definition and return two edit guards."""

        if max_chars < 1000 or max_chars > 100000:
            return SkillResult.fail("max_chars must be between 1000 and 100000.")
        try:
            symbol = self._validated_symbol(symbol)
            path = self.workspaces.resolve_workspace_path(
                task_id, relative_path, is_write=False
            )

            def _inspect() -> Dict[str, Any]:
                before = path.read_bytes()
                if len(before) > self.context._MAX_SOURCE_BYTES:
                    raise ValueError("Source file exceeds the 2 MiB structural limit.")
                try:
                    source = before.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("Source file is not valid UTF-8 text.") from exc
                span = self._one_definition(path, source, symbol)
                content = self._symbol_text(source, span)
                truncated = len(content) > max_chars
                return {
                    "task_id": task_id,
                    "relative_path": relative_path.replace("\\", "/"),
                    "symbol": symbol,
                    "qualified_name": span["qualified_name"],
                    "kind": span["kind"],
                    "backend": span["backend"],
                    "start_line": span["start_line"],
                    "end_line": span["end_line"],
                    "file_sha256": hashlib.sha256(before).hexdigest(),
                    "symbol_sha256": self._sha256(content),
                    "content_truncated": truncated,
                    "content": content[:max_chars],
                }

            payload = await asyncio.to_thread(_inspect)
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, OSError, ValueError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error inspecting coding symbol: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def replace_coding_symbol(
        self,
        task_id: str,
        relative_path: str,
        symbol: str,
        replacement: str,
        expected_file_sha256: str,
        expected_symbol_sha256: str,
    ) -> SkillResult:
        """Replace one definition atomically after exact file/symbol hash checks.

        The replacement is re-indented to the definition's current nesting level.
        The entire resulting file must parse, and the same unique definition kind
        and qualified identity must still exist after the edit.
        """

        hash_pattern = re.compile(r"^[0-9a-fA-F]{64}$")
        if not hash_pattern.fullmatch(expected_file_sha256 or ""):
            return SkillResult.fail("expected_file_sha256 must be a SHA-256 value.")
        if not hash_pattern.fullmatch(expected_symbol_sha256 or ""):
            return SkillResult.fail("expected_symbol_sha256 must be a SHA-256 value.")
        if not isinstance(replacement, str) or not replacement.strip():
            return SkillResult.fail("replacement must be a non-empty string.")
        if len(replacement) > 200000:
            return SkillResult.fail("replacement cannot exceed 200000 characters.")
        try:
            symbol = self._validated_symbol(symbol)
            path = self.workspaces.resolve_workspace_path(
                task_id, relative_path, is_write=True
            )

            def _replace() -> Dict[str, Any]:
                before = path.read_bytes()
                if len(before) > self.context._MAX_SOURCE_BYTES:
                    raise ValueError("Source file exceeds the 2 MiB structural limit.")
                before_sha256 = hashlib.sha256(before).hexdigest()
                if before_sha256 != expected_file_sha256.lower():
                    raise ValueError(
                        "Structural edit rejected: file changed since inspection "
                        f"(expected {expected_file_sha256.lower()}, current "
                        f"{before_sha256})."
                    )
                try:
                    source = before.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("Source file is not valid UTF-8 text.") from exc
                span = self._one_definition(path, source, symbol)
                original = self._symbol_text(source, span)
                symbol_sha256 = self._sha256(original)
                if symbol_sha256 != expected_symbol_sha256.lower():
                    raise ValueError(
                        "Structural edit rejected: symbol changed since inspection "
                        f"(expected {expected_symbol_sha256.lower()}, current "
                        f"{symbol_sha256})."
                    )
                source_lines = source.splitlines(keepends=True)
                start_line = int(span["start_line"])
                end_line = int(span["end_line"])
                start_offset = sum(len(item) for item in source_lines[: start_line - 1])
                end_offset = sum(len(item) for item in source_lines[:end_line])
                first_line = source_lines[start_line - 1]
                indentation = re.match(r"[ \t]*", first_line).group(0)
                prepared = self._normalized_replacement(
                    replacement, original, indentation
                )
                updated = source[:start_offset] + prepared + source[end_offset:]
                if updated == source:
                    raise ValueError("Structural edit rejected: replacement is unchanged.")
                new_span = self._one_definition(path, updated, symbol)
                if new_span["node_type"] != span["node_type"]:
                    raise ValueError(
                        "Structural edit rejected: definition kind changed from "
                        f"{span['node_type']} to {new_span['node_type']}."
                    )
                after = updated.encode("utf-8")
                after_sha256 = hashlib.sha256(after).hexdigest()
                new_content = self._symbol_text(updated, new_span)
                checkpoint_id = self.editor._create_checkpoint(
                    path, before, after_sha256
                )
                self.editor._atomic_write(path, after)
                return {
                    "task_id": task_id,
                    "relative_path": relative_path.replace("\\", "/"),
                    "symbol": symbol,
                    "qualified_name": new_span["qualified_name"],
                    "kind": new_span["kind"],
                    "backend": new_span["backend"],
                    "start_line": new_span["start_line"],
                    "end_line": new_span["end_line"],
                    "checkpoint_id": checkpoint_id,
                    "before_sha256": before_sha256,
                    "after_sha256": after_sha256,
                    "before_symbol_sha256": symbol_sha256,
                    "after_symbol_sha256": self._sha256(new_content),
                }

            payload = await asyncio.to_thread(_replace)
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, OSError, ValueError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error replacing coding symbol: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def apply_coding_file_patch(
        self,
        task_id: str,
        relative_path: str,
        edits: List[Dict[str, str]],
        expected_sha256: Optional[str] = None,
    ) -> SkillResult:
        """Atomically patch a task-relative file with the existing SHA guard."""

        try:
            path = self.workspaces.resolve_workspace_path(
                task_id, relative_path, is_write=True
            )
            return await self.editor.apply_file_patch(
                str(path), edits=edits, expected_sha256=expected_sha256
            )
        except (PermissionError, ValueError) as exc:
            return SkillResult.fail(str(exc))
