"""Optional bounded Language Server Protocol navigation for coding agents."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

import psutil

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.skills.files.editor import HostOSEditor
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils.logger import main_logger
from src.utils._tools import redact_sensitive_text


@dataclass
class _LSPSession:
    key: Tuple[str, Tuple[str, ...]]
    command: Tuple[str, ...]
    root: Path
    process: asyncio.subprocess.Process
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    server: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    documents: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    next_request_id: int = 2
    last_used: float = field(default_factory=time.monotonic)
    leases: int = 0
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.idle.set()


class HostOSCodingLanguageServer:
    """Resolve definitions/references through an installed allowlisted LSP server."""

    _MAX_MESSAGE_BYTES = 2 * 1024 * 1024
    _MAX_SOURCE_BYTES = 2 * 1024 * 1024
    _MAX_RENAME_FILES = 100
    _MAX_RENAME_EDITS = 2000
    _MAX_RENAME_TEXT_CHARS = 1_000_000
    _MAX_RENAME_TOTAL_BYTES = 16 * 1024 * 1024
    _SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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
        max_sessions: int = 4,
        idle_timeout_sec: float = 300.0,
        max_open_documents: int = 128,
        workspaces: Optional[HostOSCodingWorkspaces] = None,
        editor: Optional[HostOSEditor] = None,
    ) -> None:
        if max_sessions < 1 or max_sessions > 8:
            raise ValueError("max_sessions must be between 1 and 8.")
        if idle_timeout_sec < 1 or idle_timeout_sec > 3600:
            raise ValueError("idle_timeout_sec must be between 1 and 3600.")
        if max_open_documents < 1 or max_open_documents > 512:
            raise ValueError("max_open_documents must be between 1 and 512.")
        self.host_os = host_os_client
        self.fallback = fallback or HostOSCodingContext(host_os_client)
        self._server_commands = {
            suffix.lower(): tuple(command)
            for suffix, command in (server_commands or {}).items()
        }
        self._max_sessions = max_sessions
        self._idle_timeout_sec = idle_timeout_sec
        self._max_open_documents = max_open_documents
        self._sessions: Dict[Tuple[str, Tuple[str, ...]], _LSPSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._accepting_queries = True
        self._reaper_task: Optional[asyncio.Task[None]] = None
        self.workspaces = workspaces
        self.editor = editor

    async def start(self) -> None:
        """Enable lazy session creation when managed by the system lifecycle."""

        self._accepting_queries = True
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reap_idle_sessions(), name="jawl-lsp-session-reaper"
            )

    async def stop(self) -> None:
        """Close every retained server before shared system resources disappear."""

        reaper = self._reaper_task
        self._reaper_task = None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        async with self._sessions_lock:
            self._accepting_queries = False
            sessions = list(self._sessions.values())
            self._sessions.clear()
        if sessions:
            await asyncio.gather(
                *(self._drain_and_close_session(session) for session in sessions),
                return_exceptions=True,
            )

    async def _reap_idle_sessions(self) -> None:
        interval = min(30.0, max(0.5, self._idle_timeout_sec / 2))
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            async with self._sessions_lock:
                stale = [
                    session
                    for session in self._sessions.values()
                    if session.leases == 0
                    and not session.lock.locked()
                    and (
                        session.process.returncode is not None
                        or now - session.last_used > self._idle_timeout_sec
                        or not session.root.is_dir()
                    )
                ]
                for session in stale:
                    self._sessions.pop(session.key, None)
            if stale:
                await asyncio.gather(
                    *(self._close_session(session) for session in stale),
                    return_exceptions=True,
                )

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

    @staticmethod
    def _session_key(
        command: Tuple[str, ...], root: Path
    ) -> Tuple[str, Tuple[str, ...]]:
        return os.path.normcase(str(root.resolve())), command

    @staticmethod
    def _kill_process_tree_sync(pid: int) -> None:
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        processes = parent.children(recursive=True)
        processes.append(parent)
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(processes, timeout=1)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=1)

    async def _close_session(self, session: _LSPSession) -> None:
        async with session.lock:
            process = session.process
            if process.returncode is not None:
                return
            graceful = False
            try:
                request_id = session.next_request_id
                session.next_request_id += 1
                await self._send(
                    session.writer,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "shutdown",
                        "params": None,
                    },
                )
                await asyncio.wait_for(
                    self._receive_response(
                        session.reader, request_id, session.writer
                    ),
                    timeout=1,
                )
                await self._send(
                    session.writer,
                    {"jsonrpc": "2.0", "method": "exit", "params": None},
                )
                await asyncio.wait_for(process.wait(), timeout=2)
                graceful = True
            except (Exception, asyncio.CancelledError):
                graceful = False
            if not graceful and process.returncode is None:
                await asyncio.to_thread(self._kill_process_tree_sync, process.pid)
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def _drain_and_close_session(self, session: _LSPSession) -> None:
        try:
            await asyncio.wait_for(session.idle.wait(), timeout=2)
        except asyncio.TimeoutError:
            if session.process.returncode is None:
                main_logger.warning(
                    f"[Host OS] Forcing busy LSP shutdown for {session.server}."
                )
                await asyncio.to_thread(
                    self._kill_process_tree_sync, session.process.pid
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(session.process.wait(), timeout=2)
            return
        await self._close_session(session)

    async def _create_session(
        self,
        command: Tuple[str, ...],
        root: Path,
        key: Tuple[str, Tuple[str, ...]],
    ) -> _LSPSession:
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
        session = _LSPSession(
            key=key,
            command=command,
            root=root,
            process=process,
            reader=process.stdout,
            writer=process.stdin,
            server=Path(command[0]).name,
        )
        try:
            await self._send(
                session.writer,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "processId": None,
                        "rootUri": root.as_uri(),
                        "capabilities": {
                            "textDocument": {
                                "rename": {"prepareSupport": True}
                            },
                            "workspace": {
                                "workspaceEdit": {
                                    "documentChanges": True,
                                    "resourceOperations": [],
                                }
                            },
                        },
                        "workspaceFolders": [
                            {"uri": root.as_uri(), "name": root.name}
                        ],
                    },
                },
            )
            initialized = await self._receive_response(
                session.reader, 1, session.writer
            )
            if "error" in initialized:
                raise RuntimeError(f"initialize failed: {initialized['error']}")
            await self._send(
                session.writer,
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            )
            return session
        except BaseException:
            await asyncio.shield(self._close_session(session))
            raise

    async def _get_session(
        self, command: Tuple[str, ...], root: Path
    ) -> Tuple[_LSPSession, bool]:
        key = self._session_key(command, root)
        async with self._sessions_lock:
            if not self._accepting_queries:
                raise RuntimeError("LSP session manager is stopping")
            now = time.monotonic()
            stale = [
                session
                for session in self._sessions.values()
                if session.leases == 0
                and not session.lock.locked()
                and (
                    session.process.returncode is not None
                    or now - session.last_used > self._idle_timeout_sec
                    or not session.root.is_dir()
                )
            ]
            for session in stale:
                self._sessions.pop(session.key, None)
                await self._close_session(session)

            existing = self._sessions.get(key)
            if existing is not None:
                existing.last_used = now
                existing.leases += 1
                existing.idle.clear()
                return existing, True

            if len(self._sessions) >= self._max_sessions:
                candidates = [
                    session
                    for session in self._sessions.values()
                    if session.leases == 0 and not session.lock.locked()
                ]
                if not candidates:
                    raise RuntimeError("All bounded LSP sessions are busy")
                evicted = min(candidates, key=lambda item: item.last_used)
                self._sessions.pop(evicted.key, None)
                await self._close_session(evicted)

            session = await self._create_session(command, root, key)
            self._sessions[key] = session
            session.leases = 1
            session.idle.clear()
            return session, False

    async def _release_session(self, session: _LSPSession) -> None:
        async with self._sessions_lock:
            session.leases = max(0, session.leases - 1)
            if session.leases == 0:
                session.last_used = time.monotonic()
                session.idle.set()

    async def _discard_session(self, session: _LSPSession) -> None:
        async with self._sessions_lock:
            if self._sessions.get(session.key) is session:
                self._sessions.pop(session.key, None)
        await self._drain_and_close_session(session)

    async def _reset_session(
        self, command: Tuple[str, ...], root: Path
    ) -> None:
        key = self._session_key(command, root)
        async with self._sessions_lock:
            session = self._sessions.get(key)
            if session is not None and session.leases:
                raise RuntimeError("Cannot reset a busy LSP session")
            session = self._sessions.pop(key, None)
        if session is not None:
            await self._close_session(session)

    async def _sync_document_locked(
        self, session: _LSPSession, source_file: Path, source: str
    ) -> str:
        """Synchronize one document while the caller holds the session lock."""

        document_key = os.path.normcase(str(source_file.resolve()))
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        document = session.documents.get(document_key)
        if document is None:
            if len(session.documents) >= self._max_open_documents:
                evicted_key, evicted = min(
                    session.documents.items(),
                    key=lambda item: float(item[1]["last_used"]),
                )
                await self._send(
                    session.writer,
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didClose",
                        "params": {"textDocument": {"uri": evicted["uri"]}},
                    },
                )
                session.documents.pop(evicted_key, None)
            version = 1
            sync_state = "opened"
            await self._send(
                session.writer,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": source_file.as_uri(),
                            "languageId": self._LANGUAGE_IDS[
                                source_file.suffix.lower()
                            ],
                            "version": version,
                            "text": source,
                        }
                    },
                },
            )
        elif document["digest"] != digest:
            version = int(document["version"]) + 1
            sync_state = "changed"
            await self._send(
                session.writer,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didChange",
                    "params": {
                        "textDocument": {
                            "uri": source_file.as_uri(),
                            "version": version,
                        },
                        "contentChanges": [{"text": source}],
                    },
                },
            )
        else:
            version = int(document["version"])
            sync_state = "unchanged"
        session.documents[document_key] = {
            "digest": digest,
            "version": version,
            "uri": source_file.as_uri(),
            "last_used": time.monotonic(),
        }
        return sync_state

    async def _request_locked(
        self, session: _LSPSession, method: str, params: Dict[str, Any]
    ) -> Any:
        request_id = session.next_request_id
        session.next_request_id += 1
        await self._send(
            session.writer,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
        )
        response = await self._receive_response(
            session.reader, request_id, session.writer
        )
        session.last_used = time.monotonic()
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise RuntimeError(f"{method} failed ({code}): {error}")
        return response.get("result")

    async def _query_session(
        self,
        session: _LSPSession,
        source_file: Path,
        source: str,
        line: int,
        column: int,
        operation: str,
    ) -> Tuple[Any, str]:
        async with session.lock:
            if session.process.returncode is not None:
                raise RuntimeError("retained LSP process exited unexpectedly")
            sync_state = await self._sync_document_locked(
                session, source_file, source
            )

            method = (
                "textDocument/definition"
                if operation == "definition"
                else "textDocument/references"
            )
            source_lines = source.splitlines()
            lsp_character = self._lsp_character(source_lines[line - 1], column)
            params: Dict[str, Any] = {
                "textDocument": {"uri": source_file.as_uri()},
                "position": {"line": line - 1, "character": lsp_character},
            }
            if operation == "references":
                params["context"] = {"includeDeclaration": True}
            return await self._request_locked(session, method, params), sync_state

    async def _rename_session(
        self,
        session: _LSPSession,
        source_file: Path,
        source: str,
        line: int,
        column: int,
        new_name: str,
    ) -> Tuple[Any, str]:
        async with session.lock:
            if session.process.returncode is not None:
                raise RuntimeError("retained LSP process exited unexpectedly")
            sync_state = await self._sync_document_locked(
                session, source_file, source
            )
            source_lines = source.splitlines()
            lsp_character = self._lsp_character(source_lines[line - 1], column)
            position = {"line": line - 1, "character": lsp_character}
            base_params = {
                "textDocument": {"uri": source_file.as_uri()},
                "position": position,
            }
            try:
                prepared = await self._request_locked(
                    session, "textDocument/prepareRename", base_params
                )
            except RuntimeError as exc:
                # -32601 is the standard MethodNotFound response. Servers that
                # omit prepareRename may still implement rename correctly.
                if "(-32601)" not in str(exc):
                    raise
            else:
                if prepared is None:
                    raise RuntimeError(
                        "textDocument/prepareRename rejected this position"
                    )
            rename_params = {**base_params, "newName": new_name}
            result = await self._request_locked(
                session, "textDocument/rename", rename_params
            )
            return result, sync_state

    async def _query_server(
        self,
        command: Tuple[str, ...],
        root: Path,
        source_file: Path,
        source: str,
        line: int,
        column: int,
        operation: str,
        restart_session: bool = False,
    ) -> Tuple[Any, str, Dict[str, Any]]:
        if restart_session:
            await self._reset_session(command, root)
        for attempt in range(2):
            session, reused = await self._get_session(command, root)
            try:
                raw, sync_state = await self._query_session(
                    session, source_file, source, line, column, operation
                )
                result = raw, session.server, {
                    "session_mode": "incremental",
                    "session_reused": reused,
                    "session_restarted": attempt > 0,
                    "session_reset_requested": restart_session,
                    "document_sync": sync_state,
                }
            except asyncio.CancelledError:
                await self._release_session(session)
                await asyncio.shield(self._discard_session(session))
                raise
            except Exception:
                await self._release_session(session)
                await self._discard_session(session)
                if attempt > 0 or not reused:
                    raise
            else:
                await self._release_session(session)
                return result
        raise RuntimeError("LSP query retry exhausted")

    async def _rename_server(
        self,
        command: Tuple[str, ...],
        root: Path,
        source_file: Path,
        source: str,
        line: int,
        column: int,
        new_name: str,
        restart_session: bool = False,
    ) -> Tuple[Any, str, Dict[str, Any]]:
        if restart_session:
            await self._reset_session(command, root)
        for attempt in range(2):
            session, reused = await self._get_session(command, root)
            try:
                raw, sync_state = await self._rename_session(
                    session,
                    source_file,
                    source,
                    line,
                    column,
                    new_name,
                )
                result = raw, session.server, {
                    "session_mode": "incremental",
                    "session_reused": reused,
                    "session_restarted": attempt > 0,
                    "session_reset_requested": restart_session,
                    "document_sync": sync_state,
                }
            except asyncio.CancelledError:
                await self._release_session(session)
                await asyncio.shield(self._discard_session(session))
                raise
            except Exception:
                await self._release_session(session)
                await self._discard_session(session)
                if attempt > 0 or not reused:
                    raise
            else:
                await self._release_session(session)
                return result
        raise RuntimeError("LSP rename retry exhausted")

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

    @staticmethod
    def _lsp_character(line: str, python_column: int) -> int:
        """Convert a one-based Python string column to a zero-based UTF-16 unit."""

        prefix = line[: max(0, python_column - 1)]
        return len(prefix.encode("utf-16-le")) // 2

    @staticmethod
    def _offset_from_lsp_position(
        source: str,
        position: Any,
        lines: Optional[List[str]] = None,
        line_offsets: Optional[List[int]] = None,
    ) -> int:
        """Convert a zero-based LSP UTF-16 position to a Python offset."""

        if not isinstance(position, dict):
            raise ValueError("LSP edit position must be an object.")
        line = position.get("line")
        character = position.get("character")
        if (
            not isinstance(line, int)
            or isinstance(line, bool)
            or not isinstance(character, int)
            or isinstance(character, bool)
            or line < 0
            or character < 0
        ):
            raise ValueError("LSP edit position must contain non-negative integers.")
        lines = lines if lines is not None else source.splitlines(keepends=True)
        if not lines:
            lines = [""]
        if line_offsets is None:
            line_offsets = []
            offset = 0
            for value in lines:
                line_offsets.append(offset)
                offset += len(value)
        if line == len(lines) and character == 0 and source.endswith(("\n", "\r")):
            return len(source)
        if line >= len(lines):
            raise ValueError("LSP edit line is outside the current file.")
        raw_line = lines[line]
        if raw_line.endswith("\r\n"):
            content = raw_line[:-2]
        elif raw_line.endswith(("\n", "\r")):
            content = raw_line[:-1]
        else:
            content = raw_line
        utf16_units = 0
        for index, value in enumerate(content):
            if utf16_units == character:
                return line_offsets[line] + index
            width = 2 if ord(value) > 0xFFFF else 1
            if utf16_units < character < utf16_units + width:
                raise ValueError("LSP edit splits a UTF-16 surrogate pair.")
            utf16_units += width
        if utf16_units != character:
            raise ValueError("LSP edit character is outside the current line.")
        return line_offsets[line] + len(content)

    @classmethod
    def _workspace_edit_documents(cls, raw: Any) -> Dict[str, List[Dict[str, Any]]]:
        """Extract only textual WorkspaceEdit forms; reject resource operations."""

        if not isinstance(raw, dict):
            raise ValueError("LSP rename did not return a WorkspaceEdit object.")
        changes = raw.get("changes")
        document_changes = raw.get("documentChanges")
        if changes is not None and document_changes is not None:
            raise ValueError("WorkspaceEdit cannot mix changes and documentChanges.")
        documents: Dict[str, List[Dict[str, Any]]] = {}
        if changes is not None:
            if not isinstance(changes, dict):
                raise ValueError("WorkspaceEdit.changes must be an object.")
            for uri, edits in changes.items():
                if not isinstance(uri, str) or not isinstance(edits, list):
                    raise ValueError("WorkspaceEdit.changes contains invalid entries.")
                documents.setdefault(uri, []).extend(edits)
        elif document_changes is not None:
            if not isinstance(document_changes, list):
                raise ValueError("WorkspaceEdit.documentChanges must be a list.")
            for change in document_changes:
                if not isinstance(change, dict) or "textDocument" not in change:
                    raise ValueError(
                        "LSP rename resource operations are not permitted."
                    )
                text_document = change.get("textDocument")
                edits = change.get("edits")
                uri = (
                    text_document.get("uri")
                    if isinstance(text_document, dict)
                    else None
                )
                if not isinstance(uri, str) or not isinstance(edits, list):
                    raise ValueError(
                        "WorkspaceEdit.documentChanges contains invalid text edits."
                    )
                documents.setdefault(uri, []).extend(edits)
        else:
            raise ValueError("LSP rename returned no text changes.")
        if not documents:
            raise ValueError("LSP rename returned an empty WorkspaceEdit.")
        if len(documents) > cls._MAX_RENAME_FILES:
            raise ValueError(
                f"LSP rename exceeds the {cls._MAX_RENAME_FILES}-file limit."
            )
        return documents

    @classmethod
    def _normalize_workspace_edit(
        cls, raw: Any, workspace: Path
    ) -> Tuple[List[Dict[str, Any]], str]:
        documents = cls._workspace_edit_documents(raw)
        workspace_root = workspace.resolve()
        targets: Dict[str, Dict[str, Any]] = {}
        for uri, edits in documents.items():
            path = cls._path_from_uri(uri)
            if path is None or not path.is_relative_to(workspace_root):
                raise ValueError("LSP rename attempted to edit outside the task workspace.")
            relative = path.relative_to(workspace_root)
            if ".git" in relative.parts or not path.is_file():
                raise ValueError("LSP rename target is not a writable workspace file.")
            key = os.path.normcase(str(path))
            target = targets.setdefault(key, {"path": path, "edits": []})
            target["edits"].extend(edits)
        if len(targets) > cls._MAX_RENAME_FILES:
            raise ValueError(
                f"LSP rename exceeds the {cls._MAX_RENAME_FILES}-file limit."
            )
        plans: List[Dict[str, Any]] = []
        total_edits = 0
        total_new_text = 0
        total_source_bytes = 0

        for target in sorted(targets.values(), key=lambda item: str(item["path"])):
            path = target["path"]
            relative = path.relative_to(workspace_root)
            before = path.read_bytes()
            total_source_bytes += len(before)
            if len(before) > cls._MAX_SOURCE_BYTES:
                raise ValueError("LSP rename source exceeds the 2 MiB file limit.")
            if total_source_bytes > cls._MAX_RENAME_TOTAL_BYTES:
                raise ValueError("LSP rename exceeds the 16 MiB transaction limit.")
            try:
                source = before.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("LSP rename requires UTF-8 source files.") from exc

            source_lines = source.splitlines(keepends=True) or [""]
            line_offsets = []
            offset = 0
            for value in source_lines:
                line_offsets.append(offset)
                offset += len(value)
            normalized: List[Dict[str, Any]] = []
            seen = set()
            for edit in target["edits"]:
                if not isinstance(edit, dict):
                    raise ValueError("WorkspaceEdit contains a non-object text edit.")
                edit_range = edit.get("range")
                new_text = edit.get("newText")
                if not isinstance(edit_range, dict) or not isinstance(new_text, str):
                    raise ValueError("WorkspaceEdit text edit requires range and newText.")
                start_position = edit_range.get("start")
                end_position = edit_range.get("end")
                start = cls._offset_from_lsp_position(
                    source, start_position, source_lines, line_offsets
                )
                end = cls._offset_from_lsp_position(
                    source, end_position, source_lines, line_offsets
                )
                if end < start:
                    raise ValueError("WorkspaceEdit text edit range is reversed.")
                key = (start, end, new_text)
                if key in seen:
                    continue
                seen.add(key)
                total_edits += 1
                total_new_text += len(new_text)
                if total_edits > cls._MAX_RENAME_EDITS:
                    raise ValueError(
                        f"LSP rename exceeds the {cls._MAX_RENAME_EDITS}-edit limit."
                    )
                if total_new_text > cls._MAX_RENAME_TEXT_CHARS:
                    raise ValueError("LSP rename replacement text exceeds 1,000,000 chars.")
                normalized.append(
                    {
                        "start_offset": start,
                        "end_offset": end,
                        "range": {
                            "start": start_position,
                            "end": end_position,
                        },
                        "new_text": new_text,
                    }
                )
            normalized.sort(
                key=lambda item: (item["start_offset"], item["end_offset"])
            )
            previous: Optional[Dict[str, Any]] = None
            for edit in normalized:
                if previous is not None and (
                    edit["start_offset"] < previous["end_offset"]
                    or edit["start_offset"] == previous["start_offset"]
                ):
                    raise ValueError("WorkspaceEdit contains overlapping text edits.")
                previous = edit
            updated = source
            for edit in reversed(normalized):
                updated = (
                    updated[: edit["start_offset"]]
                    + edit["new_text"]
                    + updated[edit["end_offset"] :]
                )
            if updated == source:
                continue
            after = updated.encode("utf-8")
            plans.append(
                {
                    "path": path,
                    "relative_path": relative.as_posix(),
                    "before": before,
                    "after": after,
                    "before_sha256": hashlib.sha256(before).hexdigest(),
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                    "source": source,
                    "updated": updated,
                    "edits": normalized,
                }
            )
        if not plans:
            raise ValueError("LSP rename produced no effective source changes.")
        contract = [
            {
                "path": plan["relative_path"],
                "before_sha256": plan["before_sha256"],
                "after_sha256": plan["after_sha256"],
                "edits": [
                    {
                        "range": edit["range"],
                        "new_text": edit["new_text"],
                    }
                    for edit in plan["edits"]
                ],
            }
            for plan in plans
        ]
        preview_sha256 = hashlib.sha256(
            json.dumps(
                contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return plans, preview_sha256

    @staticmethod
    def _rename_diff(plans: List[Dict[str, Any]], max_chars: int) -> Dict[str, Any]:
        chunks = []
        for plan in plans:
            relative = plan["relative_path"]
            chunks.extend(
                difflib.unified_diff(
                    plan["source"].splitlines(keepends=True),
                    plan["updated"].splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                    n=3,
                )
            )
        full_diff = redact_sensitive_text("".join(chunks))
        truncated = len(full_diff) > max_chars
        return {
            "diff": (
                full_diff[:max_chars]
                + "\n... [Rename preview truncated; inspect files after apply.]"
                if truncated
                else full_diff
            ),
            "preview_diff_chars": len(full_diff),
            "preview_truncated": truncated,
        }

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

    @staticmethod
    def _validate_new_name(new_name: str) -> str:
        value = new_name if isinstance(new_name, str) else ""
        if (
            not value
            or value != value.strip()
            or len(value) > 128
            or any(character.isspace() or ord(character) < 32 for character in value)
            or not (
                value.isidentifier()
                or re.fullmatch(r"[A-Za-z_$][\w$]*", value)
            )
        ):
            raise ValueError(
                "new_name must be one identifier of at most 128 characters."
            )
        return value

    async def _compute_coding_rename(
        self,
        task_id: str,
        relative_path: str,
        line: int,
        column: int,
        new_name: str,
        timeout_sec: int,
        restart_session: bool,
        max_chars: int,
    ) -> Tuple[Path, Dict[str, str], List[Dict[str, Any]], Dict[str, Any]]:
        if self.workspaces is None:
            raise RuntimeError(
                "Task-scoped LSP rename requires the shared coding workspace manager."
            )
        if line < 1 or column < 1:
            raise ValueError("line and column must be positive one-based values.")
        if timeout_sec < 2 or timeout_sec > 120:
            raise ValueError("timeout_sec must be between 2 and 120.")
        if max_chars < 1000 or max_chars > 100000:
            raise ValueError("max_chars must be between 1000 and 100000.")
        new_name = self._validate_new_name(new_name)
        workspace = self.workspaces.resolve_workspace_path(
            task_id, ".", is_write=False
        )
        source_file = self.workspaces.resolve_workspace_path(
            task_id, relative_path, is_write=False
        )
        if not source_file.is_file():
            raise FileNotFoundError(f"Source file not found ({relative_path}).")
        if source_file.stat().st_size > self._MAX_SOURCE_BYTES:
            raise ValueError("Source file exceeds the 2 MiB LSP rename limit.")
        source_bytes = source_file.read_bytes()
        try:
            source = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("LSP rename requires UTF-8 source files.") from exc
        lines = source.splitlines()
        if line > len(lines):
            raise ValueError("line is outside the source file.")
        if column > len(lines[line - 1]) + 1:
            raise ValueError("column is outside the source line.")
        old_name = self._symbol_at(source, line, column)
        if not old_name:
            raise ValueError("No identifier found at the requested position.")
        if old_name == new_name:
            raise ValueError("new_name is unchanged from the selected identifier.")
        command = self._server_command(source_file.suffix)
        if command is None:
            raise RuntimeError(
                "No allowlisted LSP server is available; rename refuses lexical fallback."
            )
        query_result = await asyncio.wait_for(
            self._rename_server(
                command,
                workspace,
                source_file,
                source,
                line,
                column,
                new_name,
                restart_session=restart_session,
            ),
            timeout=timeout_sec,
        )
        raw, server = query_result[:2]
        session_metadata = (
            query_result[2]
            if len(query_result) > 2 and isinstance(query_result[2], dict)
            else {}
        )
        plans, preview_sha256 = await asyncio.to_thread(
            self._normalize_workspace_edit, raw, workspace
        )
        if source_file.read_bytes() != source_bytes:
            raise ValueError("Source file changed while LSP rename was prepared.")
        fingerprint = await self.workspaces.workspace_fingerprint(workspace)
        for plan in plans:
            if hashlib.sha256(plan["path"].read_bytes()).hexdigest() != plan[
                "before_sha256"
            ]:
                raise ValueError("Workspace changed while LSP edits were normalized.")
        diff_payload = await asyncio.to_thread(self._rename_diff, plans, max_chars)
        payload: Dict[str, Any] = {
            "task_id": task_id,
            "relative_path": relative_path.replace("\\", "/"),
            "old_name": old_name,
            "new_name": new_name,
            "backend": "lsp",
            "server": server,
            "workspace_fingerprint": fingerprint["fingerprint"],
            "workspace_head": fingerprint["head"],
            "rename_preview_sha256": preview_sha256,
            "file_count": len(plans),
            "edit_count": sum(len(plan["edits"]) for plan in plans),
            "files": [
                {
                    "path": plan["relative_path"],
                    "edit_count": len(plan["edits"]),
                    "before_sha256": plan["before_sha256"],
                    "after_sha256": plan["after_sha256"],
                }
                for plan in plans
            ],
            **diff_payload,
            **session_metadata,
        }
        return workspace, fingerprint, plans, payload

    def _apply_rename_transaction(
        self, plans: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        if self.editor is None:
            raise RuntimeError(
                "Task-scoped LSP rename requires the shared checkpoint editor."
            )
        for plan in plans:
            current = plan["path"].read_bytes()
            if hashlib.sha256(current).hexdigest() != plan["before_sha256"]:
                raise ValueError(
                    f"Rename rejected: {plan['relative_path']} changed after preview."
                )
        checkpoints = {
            plan["relative_path"]: self.editor._create_checkpoint(
                plan["path"], plan["before"], plan["after_sha256"]
            )
            for plan in plans
        }
        written: List[Dict[str, Any]] = []
        try:
            for plan in plans:
                current = plan["path"].read_bytes()
                if hashlib.sha256(current).hexdigest() != plan["before_sha256"]:
                    raise ValueError(
                        "Rename rejected during apply: "
                        f"{plan['relative_path']} changed before its write."
                    )
                self.editor._atomic_write(plan["path"], plan["after"])
                written.append(plan)
        except Exception as exc:
            rollback_errors = []
            for plan in reversed(written):
                try:
                    current = plan["path"].read_bytes()
                    if hashlib.sha256(current).hexdigest() != plan["after_sha256"]:
                        rollback_errors.append(
                            f"{plan['relative_path']}: changed after rename write"
                        )
                        continue
                    self.editor._atomic_write(plan["path"], plan["before"])
                except Exception as rollback_exc:
                    rollback_errors.append(
                        f"{plan['relative_path']}: {type(rollback_exc).__name__}"
                    )
            checkpoint_ids = ",".join(checkpoints.values())
            if rollback_errors:
                raise RuntimeError(
                    "Rename write failed and rollback was incomplete; restore "
                    f"checkpoints {checkpoint_ids}. Errors: {rollback_errors}"
                ) from exc
            raise RuntimeError(
                "Rename write failed; all written files were rolled back. "
                f"Recovery checkpoints: {checkpoint_ids}."
            ) from exc
        return checkpoints

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def preview_coding_symbol_rename(
        self,
        task_id: str,
        relative_path: str,
        line: int,
        column: int,
        new_name: str,
        timeout_sec: int = 30,
        restart_session: bool = False,
        max_chars: int = 30000,
    ) -> SkillResult:
        """Preview one bounded semantic project rename without writing files."""

        try:
            _, _, _, payload = await self._compute_coding_rename(
                task_id,
                relative_path,
                line,
                column,
                new_name,
                timeout_sec,
                restart_session,
                max_chars,
            )
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            return SkillResult.fail(str(exc))
        except asyncio.TimeoutError:
            return SkillResult.fail(
                f"LSP rename timed out after {timeout_sec} seconds."
            )
        except Exception as exc:
            return SkillResult.fail(
                f"LSP rename preview failed: {type(exc).__name__}: {exc}"
            )

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def apply_coding_symbol_rename(
        self,
        task_id: str,
        relative_path: str,
        line: int,
        column: int,
        new_name: str,
        expected_workspace_fingerprint: str,
        expected_rename_preview_sha256: str,
        timeout_sec: int = 30,
        restart_session: bool = False,
    ) -> SkillResult:
        """Recompute and atomically apply an exact previously previewed rename."""

        expected_workspace_fingerprint = str(
            expected_workspace_fingerprint or ""
        ).lower()
        expected_rename_preview_sha256 = str(
            expected_rename_preview_sha256 or ""
        ).lower()
        if not self._SHA256_PATTERN.fullmatch(expected_workspace_fingerprint):
            return SkillResult.fail(
                "expected_workspace_fingerprint must be a SHA-256 value."
            )
        if not self._SHA256_PATTERN.fullmatch(expected_rename_preview_sha256):
            return SkillResult.fail(
                "expected_rename_preview_sha256 must be a SHA-256 value."
            )
        try:
            if self.workspaces is None or self.editor is None:
                raise RuntimeError(
                    "Task-scoped LSP rename dependencies are unavailable."
                )
            workspace, fingerprint, plans, payload = (
                await self._compute_coding_rename(
                    task_id,
                    relative_path,
                    line,
                    column,
                    new_name,
                    timeout_sec,
                    restart_session,
                    1000,
                )
            )
            if fingerprint["fingerprint"] != expected_workspace_fingerprint:
                return SkillResult.fail(
                    "Rename rejected: workspace fingerprint changed since preview."
                )
            if payload["rename_preview_sha256"] != expected_rename_preview_sha256:
                return SkillResult.fail(
                    "Rename rejected: recomputed LSP edits differ from preview."
                )
            async with self.workspaces._lock:
                current_fingerprint = await self.workspaces.workspace_fingerprint(
                    workspace
                )
                if current_fingerprint["fingerprint"] != (
                    expected_workspace_fingerprint
                ):
                    return SkillResult.fail(
                        "Rename rejected: workspace changed while apply was pending."
                    )
                checkpoints = await asyncio.to_thread(
                    self._apply_rename_transaction, plans
                )
                after_fingerprint = await self.workspaces.workspace_fingerprint(
                    workspace
                )
            main_logger.info(
                f"[Host OS] Applied LSP rename {payload['old_name']} -> "
                f"{payload['new_name']} in {len(plans)} file(s)."
            )
            return SkillResult.ok(
                json.dumps(
                    {
                        "task_id": task_id,
                        "old_name": payload["old_name"],
                        "new_name": payload["new_name"],
                        "server": payload["server"],
                        "rename_preview_sha256": expected_rename_preview_sha256,
                        "before_fingerprint": expected_workspace_fingerprint,
                        "after_fingerprint": after_fingerprint["fingerprint"],
                        "file_count": len(plans),
                        "edit_count": payload["edit_count"],
                        "checkpoints": checkpoints,
                    },
                    ensure_ascii=False,
                )
            )
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            return SkillResult.fail(str(exc))
        except asyncio.TimeoutError:
            return SkillResult.fail(
                f"LSP rename timed out after {timeout_sec} seconds."
            )
        except Exception as exc:
            return SkillResult.fail(
                f"LSP rename apply failed: {type(exc).__name__}: {exc}"
            )

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.OBSERVER)
    async def get_lsp_session_status(self) -> SkillResult:
        """Return bounded process-free metadata for retained navigation sessions."""

        async with self._sessions_lock:
            now = time.monotonic()
            sessions = [
                {
                    "root": (
                        session.root.relative_to(self.host_os.framework_dir).as_posix()
                        if session.root.is_relative_to(self.host_os.framework_dir)
                        else session.root.name
                    ),
                    "server": session.server,
                    "alive": session.process.returncode is None,
                    "busy": bool(session.leases) or session.lock.locked(),
                    "open_documents": len(session.documents),
                    "idle_seconds": round(max(0.0, now - session.last_used), 3),
                }
                for session in sorted(
                    self._sessions.values(), key=lambda item: str(item.root)
                )
            ]
            payload = {
                "accepting_queries": self._accepting_queries,
                "session_count": len(sessions),
                "max_sessions": self._max_sessions,
                "idle_timeout_sec": self._idle_timeout_sec,
                "max_open_documents_per_session": self._max_open_documents,
                "sessions": sessions,
            }
        return SkillResult.ok(json.dumps(payload, ensure_ascii=False))

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
        restart_session: bool = False,
    ) -> SkillResult:
        """Resolve a symbol semantically with LSP, retaining syntax fallback.

        Line and column are one-based. Installed servers are auto-detected from a
        fixed allowlist. Server failures, unsupported languages, and empty LSP
        results fall back to bounded syntax-aware occurrence navigation. Set
        ``restart_session=true`` after broad out-of-band project mutations when
        a server's own filesystem watcher cannot be trusted.
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
                query_result = await asyncio.wait_for(
                    self._query_server(
                        command,
                        root,
                        source_file,
                        source,
                        line,
                        column,
                        operation,
                        restart_session=restart_session,
                    ),
                    timeout=timeout_sec,
                )
                raw, server = query_result[:2]
                session_metadata = (
                    query_result[2]
                    if len(query_result) > 2 and isinstance(query_result[2], dict)
                    else {}
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
                        **session_metadata,
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
