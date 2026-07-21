"""Optional bounded Language Server Protocol navigation for coding agents."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils.logger import main_logger


class HostOSCodingLanguageServer:
    """Resolve definitions/references through an installed allowlisted LSP server."""

    _MAX_MESSAGE_BYTES = 2 * 1024 * 1024
    _MAX_SOURCE_BYTES = 2 * 1024 * 1024
    _SERVER_CANDIDATES: Dict[str, Tuple[Tuple[str, ...], ...]] = {
        ".py": (
            ("basedpyright-langserver", "--stdio"),
            ("pyright-langserver", "--stdio"),
        ),
        ".js": (("typescript-language-server", "--stdio"),),
        ".jsx": (("typescript-language-server", "--stdio"),),
        ".ts": (("typescript-language-server", "--stdio"),),
        ".tsx": (("typescript-language-server", "--stdio"),),
        ".rs": (("rust-analyzer",),),
        ".go": (("gopls",),),
        ".c": (("clangd",),),
        ".cc": (("clangd",),),
        ".cpp": (("clangd",),),
        ".cxx": (("clangd",),),
        ".h": (("clangd",),),
        ".hpp": (("clangd",),),
    }
    _LANGUAGE_IDS = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascriptreact",
        ".ts": "typescript",
        ".tsx": "typescriptreact",
        ".rs": "rust",
        ".go": "go",
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".h": "c",
        ".hpp": "cpp",
    }

    def __init__(
        self,
        host_os_client: HostOSClient,
        fallback: Optional[HostOSCodingContext] = None,
        server_commands: Optional[Dict[str, Sequence[str]]] = None,
    ) -> None:
        self.host_os = host_os_client
        self.fallback = fallback or HostOSCodingContext(host_os_client)
        self._server_commands = {
            suffix.lower(): tuple(command)
            for suffix, command in (server_commands or {}).items()
        }

    def _server_command(self, suffix: str) -> Optional[Tuple[str, ...]]:
        suffix = suffix.lower()
        injected = self._server_commands.get(suffix)
        if injected:
            return injected
        for candidate in self._SERVER_CANDIDATES.get(suffix, ()):
            executable = shutil.which(candidate[0])
            if executable:
                return (executable, *candidate[1:])
        return None

    @staticmethod
    def _symbol_at(source: str, line: int, column: int) -> str:
        lines = source.splitlines()
        if line < 1 or line > len(lines):
            return ""
        text = lines[line - 1]
        cursor = min(max(column - 1, 0), len(text))
        for match in re.finditer(r"[A-Za-z_$][\w$]*", text):
            if match.start() <= cursor <= match.end():
                return match.group(0)
        return ""

    @classmethod
    async def _send(cls, writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        await writer.drain()

    @classmethod
    async def _receive(cls, reader: asyncio.StreamReader) -> Dict[str, Any]:
        header = await reader.readuntil(b"\r\n\r\n")
        if len(header) > 8192:
            raise ValueError("LSP header exceeded 8192 bytes")
        match = re.search(br"(?im)^Content-Length:\s*(\d+)\s*$", header)
        if not match:
            raise ValueError("LSP response omitted Content-Length")
        length = int(match.group(1))
        if length < 0 or length > cls._MAX_MESSAGE_BYTES:
            raise ValueError("LSP response exceeded the bounded message size")
        return json.loads((await reader.readexactly(length)).decode("utf-8"))

    @classmethod
    async def _receive_response(
        cls,
        reader: asyncio.StreamReader,
        request_id: int,
        writer: Optional[asyncio.StreamWriter] = None,
    ) -> Dict[str, Any]:
        while True:
            message = await cls._receive(reader)
            if message.get("id") == request_id:
                return message
            # Language servers may issue capability/progress requests while the
            # client is awaiting its own response. A bounded one-shot client has
            # no dynamic capabilities to register, but it must acknowledge these
            # requests or some servers will wait forever.
            if writer is not None and "id" in message and message.get("method"):
                result: Any = None
                if message["method"] == "workspace/configuration":
                    items = (message.get("params") or {}).get("items", [])
                    result = [None] * len(items) if isinstance(items, list) else []
                await cls._send(
                    writer,
                    {"jsonrpc": "2.0", "id": message["id"], "result": result},
                )

    @staticmethod
    def _creation_kwargs() -> Dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
        return {}

    async def _query_server(
        self,
        command: Tuple[str, ...],
        root: Path,
        source_file: Path,
        source: str,
        line: int,
        column: int,
        operation: str,
    ) -> Tuple[Any, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **self._creation_kwargs(),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        try:
            await self._send(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "processId": None,
                        "rootUri": root.as_uri(),
                        "capabilities": {},
                        "workspaceFolders": [
                            {"uri": root.as_uri(), "name": root.name}
                        ],
                    },
                },
            )
            initialized = await self._receive_response(
                process.stdout, 1, process.stdin
            )
            if "error" in initialized:
                raise RuntimeError(f"initialize failed: {initialized['error']}")
            await self._send(
                process.stdin,
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            )
            await self._send(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": source_file.as_uri(),
                            "languageId": self._LANGUAGE_IDS[
                                source_file.suffix.lower()
                            ],
                            "version": 1,
                            "text": source,
                        }
                    },
                },
            )
            method = (
                "textDocument/definition"
                if operation == "definition"
                else "textDocument/references"
            )
            params: Dict[str, Any] = {
                "textDocument": {"uri": source_file.as_uri()},
                "position": {"line": line - 1, "character": column - 1},
            }
            if operation == "references":
                params["context"] = {"includeDeclaration": True}
            await self._send(
                process.stdin,
                {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
            )
            response = await self._receive_response(process.stdout, 2, process.stdin)
            if "error" in response:
                raise RuntimeError(f"{method} failed: {response['error']}")
            return response.get("result"), Path(command[0]).name
        finally:
            if process.returncode is None:
                try:
                    await self._send(
                        process.stdin,
                        {
                            "jsonrpc": "2.0",
                            "id": 99,
                            "method": "shutdown",
                            "params": None,
                        },
                    )
                    await asyncio.wait_for(
                        self._receive_response(process.stdout, 99, process.stdin),
                        timeout=1,
                    )
                    await self._send(
                        process.stdin,
                        {"jsonrpc": "2.0", "method": "exit", "params": None},
                    )
                except Exception:
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    @staticmethod
    def _path_from_uri(uri: str) -> Optional[Path]:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        raw_path = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", raw_path):
            raw_path = raw_path[1:]
        return Path(raw_path).resolve()

    @staticmethod
    def _python_column(line: str, utf16_character: int) -> int:
        units = 0
        for index, character in enumerate(line):
            if units >= utf16_character:
                return index + 1
            units += 2 if ord(character) > 0xFFFF else 1
        return len(line) + 1

    def _normalize_locations(
        self, raw: Any, root: Path, max_results: int, max_output_chars: int
    ) -> Dict[str, Any]:
        entries = raw if isinstance(raw, list) else ([] if raw is None else [raw])
        locations: List[Dict[str, Any]] = []
        seen = set()
        external_omitted = 0
        invalid_omitted = 0
        for entry in entries:
            if not isinstance(entry, dict):
                invalid_omitted += 1
                continue
            uri = entry.get("uri") or entry.get("targetUri")
            location_range = entry.get("range") or entry.get("targetSelectionRange")
            path = self._path_from_uri(uri) if isinstance(uri, str) else None
            if path is None or not isinstance(location_range, dict):
                invalid_omitted += 1
                continue
            if not path.is_relative_to(root):
                external_omitted += 1
                continue
            start = location_range.get("start", {})
            if not isinstance(start, dict):
                invalid_omitted += 1
                continue
            line_zero = start.get("line")
            character = start.get("character")
            if not isinstance(line_zero, int) or not isinstance(character, int):
                invalid_omitted += 1
                continue
            key = (str(path), line_zero, character)
            if key in seen:
                continue
            seen.add(key)
            preview = ""
            column = character + 1
            try:
                if path.stat().st_size <= self._MAX_SOURCE_BYTES:
                    lines = path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if 0 <= line_zero < len(lines):
                        preview = lines[line_zero].strip()[:240]
                        column = self._python_column(lines[line_zero], character)
            except OSError:
                pass
            locations.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "line": line_zero + 1,
                    "column": column,
                    "lsp_character": character,
                    "preview": preview,
                    "backend": "lsp",
                    "confidence": "semantic",
                }
            )
            if len(locations) >= max_results:
                break
        payload = {
            "results": locations,
            "result_count": len(locations),
            "results_truncated": len(entries) > len(locations),
            "external_locations_omitted": external_omitted,
            "invalid_locations_omitted": invalid_omitted,
        }
        while (
            locations
            and len(json.dumps(payload, ensure_ascii=False)) > max_output_chars
        ):
            locations.pop()
            payload["result_count"] = len(locations)
            payload["results_truncated"] = True
        return payload

    async def _fallback_result(
        self,
        symbol: str,
        root: Path,
        operation: str,
        max_results: int,
        reason: str,
    ) -> SkillResult:
        fallback = await self.fallback.locate_code_symbol(
            symbol=symbol,
            path=str(root),
            include_references=operation == "references",
            max_results=max_results,
        )
        if not fallback.is_success:
            return fallback
        payload = {
            "backend": "syntax_fallback",
            "lsp_available": False,
            "fallback_reason": reason[:500],
            "symbol": symbol,
            "fallback": json.loads(fallback.message),
        }
        return SkillResult.ok(json.dumps(payload, ensure_ascii=False))

    def _discover_project_root(self, source_file: Path) -> Path:
        markers = (
            ".git",
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "CMakeLists.txt",
        )
        for candidate in (source_file.parent, *source_file.parents):
            try:
                self.host_os.validate_path(candidate, is_write=False)
            except PermissionError:
                break
            if any((candidate / marker).exists() for marker in markers):
                return candidate
        return source_file.parent

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def resolve_code_symbol(
        self,
        filepath: str,
        line: int,
        column: int,
        operation: Literal["definition", "references"] = "definition",
        project_root: Optional[str] = None,
        max_results: int = 100,
        timeout_sec: int = 30,
    ) -> SkillResult:
        """Resolve a symbol semantically with LSP, retaining syntax fallback.

        Line and column are one-based. Installed servers are auto-detected from a
        fixed allowlist. Server failures, unsupported languages, and empty LSP
        results fall back to bounded syntax-aware occurrence navigation.
        """

        if line < 1 or column < 1:
            return SkillResult.fail(
                "line and column must be positive one-based values."
            )
        if max_results < 1 or max_results > 500:
            return SkillResult.fail("max_results must be between 1 and 500.")
        if timeout_sec < 2 or timeout_sec > 120:
            return SkillResult.fail("timeout_sec must be between 2 and 120.")
        try:
            source_file = self.host_os.validate_path(filepath, is_write=False)
            root = (
                self.host_os.validate_path(project_root, is_write=False)
                if project_root
                else self._discover_project_root(source_file)
            )
            if not source_file.is_file():
                return SkillResult.fail(f"Error: File not found ({filepath}).")
            if not root.is_dir() or not source_file.is_relative_to(root):
                return SkillResult.fail("filepath must be inside project_root.")
            if source_file.stat().st_size > self._MAX_SOURCE_BYTES:
                return SkillResult.fail(
                    "Source file exceeds the 2 MiB LSP input limit."
                )
            source = source_file.read_text(encoding="utf-8", errors="replace")
            symbol = self._symbol_at(source, line, column)
            if not symbol:
                return SkillResult.fail(
                    "No identifier found at the requested position."
                )
            command = self._server_command(source_file.suffix)
            if command is None:
                return await self._fallback_result(
                    symbol,
                    root,
                    operation,
                    max_results,
                    f"no allowlisted LSP server installed for {source_file.suffix}",
                )
            try:
                raw, server = await asyncio.wait_for(
                    self._query_server(
                        command,
                        root,
                        source_file,
                        source,
                        line,
                        column,
                        operation,
                    ),
                    timeout=timeout_sec,
                )
                normalized = self._normalize_locations(
                    raw,
                    root,
                    max_results,
                    self.host_os.config.file_read_max_chars * 2,
                )
                if normalized["result_count"]:
                    normalized.update(
                        backend="lsp",
                        lsp_available=True,
                        server=server,
                        operation=operation,
                        symbol=symbol,
                    )
                    main_logger.info(
                        f"[Host OS] LSP {operation} resolved {symbol}: "
                        f"{normalized['result_count']} result(s)."
                    )
                    return SkillResult.ok(json.dumps(normalized, ensure_ascii=False))
                reason = f"{server} returned no in-project {operation} locations"
            except asyncio.TimeoutError:
                reason = f"LSP request timed out after {timeout_sec} seconds"
            except Exception as exc:
                reason = f"LSP request failed: {type(exc).__name__}: {exc}"
            return await self._fallback_result(
                symbol, root, operation, max_results, reason
            )
        except PermissionError as exc:
            return SkillResult.fail(str(exc))
        except OSError as exc:
            return SkillResult.fail(f"Error preparing LSP request: {exc}")
