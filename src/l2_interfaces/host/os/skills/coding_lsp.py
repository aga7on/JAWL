"""Optional bounded Language Server Protocol navigation for coding agents."""

from __future__ import annotations

import asyncio
import contextlib
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
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils.logger import main_logger


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
                        "capabilities": {},
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
                            "params": {
                                "textDocument": {"uri": evicted["uri"]}
                            },
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
                raise RuntimeError(f"{method} failed: {response['error']}")
            return response.get("result"), sync_state

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
