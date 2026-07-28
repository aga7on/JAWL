"""Lifecycle-managed, progressive-discovery MCP client broker."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from jsonschema import exceptions as jsonschema_exceptions
from jsonschema.validators import validator_for
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client

from src import __version__
from src.l2_interfaces.mcp.state import MCPState
from src.utils._tools import redact_sensitive_text, truncate_text
from src.utils.logger import main_logger
from src.utils.settings import MCPConfig, MCPServerConfig


class MCPClientError(RuntimeError):
    """Bounded operator-facing MCP client failure."""


class MCPToolOutcomeUnknown(MCPClientError):
    """A side-effecting request was cancelled or timed out after dispatch."""


@dataclass
class _QueuedRequest:
    operation: str
    parameters: dict[str, Any]
    future: asyncio.Future[Any]
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _tool_contract(tool: types.Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "title": tool.title,
        "description": tool.description,
        "input_schema": tool.inputSchema,
        "output_schema": tool.outputSchema,
        "annotations": (
            tool.annotations.model_dump(exclude_none=True)
            if tool.annotations is not None
            else None
        ),
    }


def _prompt_contract(prompt: types.Prompt) -> dict[str, Any]:
    return {
        "name": prompt.name,
        "title": prompt.title,
        "description": prompt.description,
        "arguments": [
            argument.model_dump(exclude_none=True)
            for argument in (prompt.arguments or [])
        ],
    }


class MCPServerWorker:
    """Own one MCP session and all async context managers in one task."""

    _STOP = "__stop__"

    def __init__(
        self,
        config: MCPServerConfig,
        global_config: MCPConfig,
        root_dir: Path,
        state: MCPState,
    ) -> None:
        self.config = config
        self.global_config = global_config
        self.root_dir = root_dir.resolve()
        self.state = state
        self.queue: asyncio.Queue[_QueuedRequest] = asyncio.Queue(maxsize=100)
        self.task: Optional[asyncio.Task[None]] = None
        self.ready: Optional[asyncio.Future[None]] = None
        self.tools: dict[str, types.Tool] = {}
        self.tool_hashes: dict[str, str] = {}
        self.prompts: dict[str, types.Prompt] = {}
        self.prompt_hashes: dict[str, str] = {}
        self.resources: list[types.Resource] = []
        self.resource_templates: list[types.ResourceTemplate] = []

    def _update_state(self, **values: Any) -> None:
        self.state.update_server(
            self.config.name,
            name=self.config.name,
            transport=self.config.transport,
            **values,
        )

    def _resolve_cwd(self) -> Path:
        cwd = (self.root_dir / self.config.cwd).resolve()
        if not cwd.is_relative_to(self.root_dir):
            raise MCPClientError("Configured MCP cwd escaped the JAWL root.")
        if not cwd.is_dir():
            raise MCPClientError(
                f"Configured MCP cwd does not exist ({self.config.cwd})."
            )
        return cwd

    def _stdio_environment(self) -> dict[str, str]:
        missing = [
            name for name in self.config.env_passthrough if name not in os.environ
        ]
        if missing:
            raise MCPClientError(
                "Missing configured MCP environment variables: "
                + ", ".join(sorted(missing))
            )
        environment = get_default_environment()
        environment.update(
            {name: os.environ[name] for name in self.config.env_passthrough}
        )
        return environment

    def _http_headers(self) -> dict[str, str]:
        missing = [
            env_name
            for env_name in self.config.headers_from_env.values()
            if env_name not in os.environ
        ]
        token_env = self.config.bearer_token_env
        if token_env and token_env not in os.environ:
            missing.append(token_env)
        if missing:
            raise MCPClientError(
                "Missing configured MCP HTTP environment variables: "
                + ", ".join(sorted(set(missing)))
            )
        headers = {
            header: os.environ[env_name]
            for header, env_name in self.config.headers_from_env.items()
        }
        if token_env:
            headers["Authorization"] = f"Bearer {os.environ[token_env]}"
        return headers

    async def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.queue = asyncio.Queue(maxsize=100)
        self.ready = asyncio.get_running_loop().create_future()
        self._update_state(state="starting", last_error=None)
        self.task = asyncio.create_task(
            self._run(), name=f"jawl-mcp-{self.config.name}"
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(self.ready),
                timeout=self.global_config.startup_timeout_sec,
            )
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        task = self.task
        if task is None:
            self._update_state(state="offline")
            return
        if not task.done():
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            request = _QueuedRequest(self._STOP, {}, future)
            try:
                self.queue.put_nowait(request)
            except asyncio.QueueFull:
                task.cancel()
            try:
                await asyncio.wait_for(task, timeout=10)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception:
                pass
        else:
            await asyncio.gather(task, return_exceptions=True)
        self.task = None
        self.ready = None
        self._update_state(state="offline")

    async def request(self, operation: str, **parameters: Any) -> Any:
        if self.task is None or self.task.done():
            raise MCPClientError(
                f"MCP server '{self.config.name}' is not connected."
            )
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        request = _QueuedRequest(operation, parameters, future)
        try:
            await asyncio.wait_for(
                self.queue.put(request),
                timeout=self.global_config.request_timeout_sec,
            )
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self.global_config.request_timeout_sec + 5,
            )
        except asyncio.CancelledError:
            request.cancel_event.set()
            raise
        except asyncio.TimeoutError as exc:
            request.cancel_event.set()
            if operation == "call_tool_guarded":
                raise MCPToolOutcomeUnknown(
                    "MCP tool request timed out after dispatch; its external "
                    "outcome is unknown and it was not retried."
                ) from exc
            raise MCPClientError("MCP request timed out.") from exc

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                if self.config.transport == "stdio":
                    errlog = stack.enter_context(
                        open(os.devnull, "w", encoding="utf-8")
                    )
                    parameters = StdioServerParameters(
                        command=str(self.config.command),
                        args=list(self.config.args),
                        env=self._stdio_environment(),
                        cwd=self._resolve_cwd(),
                    )
                    read, write = await stack.enter_async_context(
                        stdio_client(parameters, errlog=errlog)
                    )
                else:
                    http_client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers=self._http_headers(),
                            timeout=self.global_config.request_timeout_sec,
                            follow_redirects=False,
                        )
                    )
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(
                            str(self.config.url),
                            http_client=http_client,
                            terminate_on_close=True,
                        )
                    )
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(
                            seconds=self.global_config.request_timeout_sec
                        ),
                        client_info=types.Implementation(
                            name="JAWL", version=__version__
                        ),
                    )
                )
                initialized = await asyncio.wait_for(
                    session.initialize(),
                    timeout=self.global_config.startup_timeout_sec,
                )
                await self._refresh_all_catalogs(session, initialized.capabilities)
                self._update_state(
                    state="online",
                    protocol_version=str(initialized.protocolVersion),
                    server_name=initialized.serverInfo.name,
                    server_version=initialized.serverInfo.version,
                    tool_count=len(self.tools),
                    resource_count=len(self.resources)
                    + len(self.resource_templates),
                    prompt_count=len(self.prompts),
                    last_error=None,
                )
                assert self.ready is not None
                if not self.ready.done():
                    self.ready.set_result(None)
                await self._serve_requests(session)
        except asyncio.CancelledError:
            if self.ready is not None and not self.ready.done():
                self.ready.cancel()
            raise
        except Exception as exc:
            message = redact_sensitive_text(str(exc), max_chars=1000)
            self._update_state(state="error", last_error=message)
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(MCPClientError(message))
            main_logger.error(
                f"[MCP] Server '{self.config.name}' stopped: {message}"
            )
        finally:
            self._fail_queued_requests(
                MCPClientError(f"MCP server '{self.config.name}' disconnected.")
            )

    async def _serve_requests(self, session: ClientSession) -> None:
        while True:
            request = await self.queue.get()
            if request.operation == self._STOP:
                if not request.future.done():
                    request.future.set_result(None)
                return
            operation_task = asyncio.create_task(
                self._execute(session, request.operation, request.parameters)
            )
            cancellation_task = asyncio.create_task(request.cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {operation_task, cancellation_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation_task in done and request.cancel_event.is_set():
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                    if not request.future.done():
                        request.future.cancel()
                    continue
                cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
                result = await operation_task
                if not request.future.done():
                    request.future.set_result(result)
            except asyncio.CancelledError:
                operation_task.cancel()
                cancellation_task.cancel()
                await asyncio.gather(
                    operation_task, cancellation_task, return_exceptions=True
                )
                if not request.future.done():
                    request.future.cancel()
                raise
            except Exception as exc:
                cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
                if not request.future.done():
                    request.future.set_exception(exc)

    def _fail_queued_requests(self, error: Exception) -> None:
        while True:
            try:
                request = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not request.future.done():
                request.future.set_exception(error)

    async def _protocol_call(self, awaitable: Any) -> Any:
        return await asyncio.wait_for(
            awaitable, timeout=self.global_config.request_timeout_sec
        )

    async def _list_all(self, method: Any, attribute: str) -> list[Any]:
        items: list[Any] = []
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        while True:
            response = await self._protocol_call(method(cursor=cursor))
            items.extend(list(getattr(response, attribute)))
            if len(items) > self.global_config.max_catalog_items:
                raise MCPClientError(
                    f"MCP server '{self.config.name}' exceeded the configured "
                    "catalog item limit."
                )
            next_cursor = getattr(response, "nextCursor", None)
            if not next_cursor:
                return items
            if next_cursor in seen_cursors:
                raise MCPClientError("MCP server repeated a pagination cursor.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def _refresh_tools(self, session: ClientSession) -> list[types.Tool]:
        tools = await self._list_all(session.list_tools, "tools")
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise MCPClientError("MCP server returned duplicate tool names.")
        self.tools = {tool.name: tool for tool in tools}
        self.tool_hashes = {
            tool.name: _canonical_sha256(_tool_contract(tool)) for tool in tools
        }
        self._update_state(tool_count=len(tools))
        return tools

    async def _refresh_resources(
        self, session: ClientSession
    ) -> tuple[list[types.Resource], list[types.ResourceTemplate]]:
        if not self.config.resources_enabled:
            self.resources = []
            self.resource_templates = []
            return [], []
        resources = await self._list_all(session.list_resources, "resources")
        templates = await self._list_all(
            session.list_resource_templates, "resourceTemplates"
        )
        self.resources = resources
        self.resource_templates = templates
        self._update_state(resource_count=len(resources) + len(templates))
        return resources, templates

    async def _refresh_prompts(self, session: ClientSession) -> list[types.Prompt]:
        if not self.config.prompts_enabled:
            self.prompts = {}
            self.prompt_hashes = {}
            return []
        prompts = await self._list_all(session.list_prompts, "prompts")
        names = [prompt.name for prompt in prompts]
        if len(names) != len(set(names)):
            raise MCPClientError("MCP server returned duplicate prompt names.")
        self.prompts = {prompt.name: prompt for prompt in prompts}
        self.prompt_hashes = {
            prompt.name: _canonical_sha256(_prompt_contract(prompt))
            for prompt in prompts
        }
        self._update_state(prompt_count=len(prompts))
        return prompts

    async def _refresh_all_catalogs(
        self, session: ClientSession, capabilities: types.ServerCapabilities
    ) -> None:
        if capabilities.tools is not None:
            await self._refresh_tools(session)
        else:
            self.tools = {}
            self.tool_hashes = {}
        if capabilities.resources is not None:
            await self._refresh_resources(session)
        if capabilities.prompts is not None:
            await self._refresh_prompts(session)

    @staticmethod
    def _validate_schema_value(
        schema: dict[str, Any], value: Any, *, label: str
    ) -> None:
        try:
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            validator_class(schema).validate(value)
        except jsonschema_exceptions.SchemaError as exc:
            raise MCPClientError(
                f"MCP tool advertised an invalid {label} schema: "
                + truncate_text(exc.message, max_chars=800)
            ) from exc
        except jsonschema_exceptions.ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path) or label
            raise MCPClientError(
                f"MCP {label} failed schema validation at {path}: "
                + truncate_text(exc.message, max_chars=800)
            ) from exc

    async def _execute(
        self, session: ClientSession, operation: str, parameters: dict[str, Any]
    ) -> Any:
        if operation == "refresh_tools":
            return await self._refresh_tools(session)
        if operation == "call_tool_guarded":
            tools = await self._refresh_tools(session)
            name = parameters["name"]
            tool = next(
                (candidate for candidate in tools if candidate.name == name), None
            )
            if tool is None:
                raise MCPClientError(f"MCP tool '{name}' is unavailable.")
            if name not in self.config.allowed_tools:
                raise MCPClientError(
                    f"MCP tool '{name}' is not in this server's exact allowlist."
                )
            current_hash = self.tool_hashes[name]
            if parameters["expected_schema_sha256"] != current_hash:
                raise MCPClientError(
                    "MCP tool schema changed or the supplied schema hash is stale; "
                    "discover the tool again before calling it."
                )
            arguments = parameters["arguments"]
            self._validate_schema_value(
                tool.inputSchema, arguments, label="tool arguments"
            )
            try:
                result = await self._protocol_call(
                    session.call_tool(name, arguments=arguments)
                )
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                raise MCPToolOutcomeUnknown(
                    "MCP tool call ended after dispatch; its external outcome is "
                    "unknown and the call was not retried."
                ) from exc
            if (
                result.structuredContent is not None
                and tool.outputSchema is not None
            ):
                self._validate_schema_value(
                    tool.outputSchema,
                    result.structuredContent,
                    label="structured tool output",
                )
            return tool, result
        if operation == "list_resources":
            return await self._refresh_resources(session)
        if operation == "read_resource":
            if not self.config.resources_enabled:
                raise MCPClientError(
                    f"Resources are disabled for MCP server '{self.config.name}'."
                )
            return await self._protocol_call(
                session.read_resource(types.AnyUrl(parameters["uri"]))
            )
        if operation == "list_prompts":
            return await self._refresh_prompts(session)
        if operation == "get_prompt_guarded":
            if not self.config.prompts_enabled:
                raise MCPClientError(
                    f"Prompts are disabled for MCP server '{self.config.name}'."
                )
            prompts = await self._refresh_prompts(session)
            name = parameters["name"]
            prompt = next(
                (candidate for candidate in prompts if candidate.name == name), None
            )
            if prompt is None:
                raise MCPClientError(f"MCP prompt '{name}' is unavailable.")
            if parameters["expected_schema_sha256"] != self.prompt_hashes[name]:
                raise MCPClientError(
                    "MCP prompt schema changed or the supplied schema hash is stale."
                )
            return await self._protocol_call(
                session.get_prompt(name, arguments=parameters["arguments"])
            )
        raise MCPClientError(f"Unknown MCP broker operation '{operation}'.")


class MCPClientManager:
    """Coordinate configured MCP workers and project untrusted results safely."""

    def __init__(
        self,
        config: MCPConfig,
        root_dir: Path,
        state: MCPState,
    ) -> None:
        self.config = config
        self.root_dir = root_dir.resolve()
        self.state = state
        self.artifacts_dir = (
            self.root_dir / "sandbox" / "_system" / "download" / "mcp"
        )
        self.workers = {
            server.name: MCPServerWorker(server, config, self.root_dir, state)
            for server in config.servers
            if server.enabled
        }

    def _worker(self, server: str) -> MCPServerWorker:
        worker = self.workers.get(server)
        if worker is None:
            raise MCPClientError(f"Unknown or disabled MCP server '{server}'.")
        return worker

    async def start(self) -> None:
        for worker in self.workers.values():
            try:
                await worker.start()
            except Exception as exc:
                main_logger.error(
                    f"[MCP] Failed to start '{worker.config.name}': "
                    f"{redact_sensitive_text(str(exc), max_chars=1000)}"
                )

    async def stop(self) -> None:
        for worker in reversed(list(self.workers.values())):
            await worker.stop()
        self.state.is_online = False

    async def reconnect(self, server: str) -> None:
        worker = self._worker(server)
        await worker.stop()
        await worker.start()

    async def get_context_block(self, **kwargs: Any) -> str:
        snapshot = self.state.snapshot()
        lines = [
            "### MCP [ON]",
            "Description: Progressive, policy-controlled Model Context Protocol client.",
            "Use MCPTools.search_tools before MCPTools.call_tool; exact schema "
            "hashes are mandatory.",
        ]
        if not snapshot:
            lines.append("* No enabled MCP servers are configured.")
        for server in snapshot:
            line = (
                f"* {server.get('name')}: {server.get('state', 'unknown')} "
                f"via {server.get('transport')}; tools={server.get('tool_count', 0)}, "
                f"resources={server.get('resource_count', 0)}, "
                f"prompts={server.get('prompt_count', 0)}"
            )
            if server.get("last_error"):
                line += "; error=" + truncate_text(
                    str(server["last_error"]), max_chars=300
                )
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _ranked_tools(
        query: str, tools: list[types.Tool]
    ) -> list[types.Tool]:
        normalized = " ".join(query.lower().split())
        terms = normalized.split()
        ranked: list[tuple[int, str, types.Tool]] = []
        for tool in tools:
            name = tool.name.lower()
            description = (tool.description or "").lower()
            haystack = f"{name} {description}"
            matched = [term for term in terms if term in haystack]
            if not matched:
                continue
            score = len(matched) * 10
            if len(matched) == len(terms):
                score += 50
            if normalized in name:
                score += 100
            score += sum(20 for term in matched if term in name)
            ranked.append((-score, tool.name, tool))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [tool for _, _, tool in ranked]

    async def search_tools(
        self, query: str, server: Optional[str], limit: int
    ) -> dict[str, Any]:
        normalized = " ".join(query.split())
        if len(normalized) < 2:
            raise MCPClientError("MCP tool search query must contain 2 characters.")
        if limit < 1 or limit > 50:
            raise MCPClientError("MCP tool search limit must be between 1 and 50.")
        selected = (
            [self._worker(server)] if server else list(self.workers.values())
        )
        matches: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for worker in selected:
            try:
                tools = await worker.request("refresh_tools")
            except Exception as exc:
                errors.append(
                    {
                        "server": worker.config.name,
                        "error": redact_sensitive_text(str(exc), max_chars=500),
                    }
                )
                continue
            for relevance_rank, tool in enumerate(
                self._ranked_tools(normalized, tools)
            ):
                contract = _tool_contract(tool)
                matches.append(
                    {
                        "server": worker.config.name,
                        "name": tool.name,
                        "title": tool.title,
                        "description": redact_sensitive_text(
                            tool.description or "", max_chars=800
                        ),
                        "input_schema": contract["input_schema"],
                        "schema_sha256": worker.tool_hashes[tool.name],
                        "allowed": tool.name in worker.config.allowed_tools,
                        "_relevance_rank": relevance_rank,
                    }
                )
        matches.sort(
            key=lambda item: (
                0 if item["server"].lower() in normalized.lower() else 1,
                0 if item["allowed"] else 1,
                item["_relevance_rank"],
                item["server"],
                item["name"],
            )
        )
        for match in matches:
            match.pop("_relevance_rank", None)
        return {
            "query": normalized,
            "count": min(len(matches), limit),
            "matches": matches[:limit],
            "errors": errors,
        }

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        expected_schema_sha256: str,
    ) -> tuple[bool, dict[str, Any]]:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_schema_sha256):
            raise MCPClientError("expected_schema_sha256 must be a SHA-256 hash.")
        worker = self._worker(server)
        _, result = await worker.request(
            "call_tool_guarded",
            name=tool,
            arguments=arguments,
            expected_schema_sha256=expected_schema_sha256,
        )
        payload = await self._project_tool_result(server, tool, result)
        return not bool(result.isError), payload

    async def list_resources(self, server: str) -> dict[str, Any]:
        worker = self._worker(server)
        resources, templates = await worker.request("list_resources")
        return {
            "server": server,
            "resources": [
                {
                    "uri": str(resource.uri),
                    "name": resource.name,
                    "title": resource.title,
                    "description": redact_sensitive_text(
                        resource.description or "", max_chars=500
                    ),
                    "mime_type": resource.mimeType,
                    "size": resource.size,
                }
                for resource in resources
            ],
            "templates": [
                {
                    "uri_template": template.uriTemplate,
                    "name": template.name,
                    "title": template.title,
                    "description": redact_sensitive_text(
                        template.description or "", max_chars=500
                    ),
                    "mime_type": template.mimeType,
                }
                for template in templates
            ],
        }

    async def read_resource(self, server: str, uri: str) -> dict[str, Any]:
        worker = self._worker(server)
        result = await worker.request("read_resource", uri=uri)
        contents = []
        for content in result.contents:
            if isinstance(content, types.TextResourceContents):
                contents.append(
                    {
                        "uri": str(content.uri),
                        "mime_type": content.mimeType,
                        "text": redact_sensitive_text(
                            content.text, max_chars=self.config.max_result_chars
                        ),
                    }
                )
            else:
                artifact = await self._store_base64_artifact(
                    server, content.blob, content.mimeType
                )
                contents.append(
                    {
                        "uri": str(content.uri),
                        "mime_type": content.mimeType,
                        "artifact": artifact,
                    }
                )
        return {"server": server, "contents": contents}

    async def list_prompts(self, server: str) -> dict[str, Any]:
        worker = self._worker(server)
        prompts = await worker.request("list_prompts")
        return {
            "server": server,
            "prompts": [
                {
                    **_prompt_contract(prompt),
                    "description": redact_sensitive_text(
                        prompt.description or "", max_chars=500
                    ),
                    "schema_sha256": worker.prompt_hashes[prompt.name],
                }
                for prompt in prompts
            ],
        }

    async def get_prompt(
        self,
        server: str,
        prompt: str,
        arguments: dict[str, str],
        expected_schema_sha256: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_schema_sha256):
            raise MCPClientError("expected_schema_sha256 must be a SHA-256 hash.")
        worker = self._worker(server)
        result = await worker.request(
            "get_prompt_guarded",
            name=prompt,
            arguments=arguments,
            expected_schema_sha256=expected_schema_sha256,
        )
        messages = []
        for message in result.messages:
            content = message.content
            if isinstance(content, types.TextContent):
                projected: dict[str, Any] = {
                    "type": "text",
                    "text": redact_sensitive_text(
                        content.text, max_chars=self.config.max_result_chars
                    ),
                }
            elif isinstance(content, (types.ImageContent, types.AudioContent)):
                projected = {
                    "type": content.type,
                    "mime_type": content.mimeType,
                    "artifact": await self._store_base64_artifact(
                        server, content.data, content.mimeType
                    ),
                }
            else:
                projected = {
                    "type": content.type,
                    "value": redact_sensitive_text(
                        content.model_dump_json(exclude_none=True), max_chars=2000
                    ),
                }
            messages.append({"role": message.role, "content": projected})
        return {
            "server": server,
            "description": redact_sensitive_text(
                result.description or "", max_chars=1000
            ),
            "messages": messages,
        }

    async def _project_tool_result(
        self, server: str, tool: str, result: types.CallToolResult
    ) -> dict[str, Any]:
        contents = []
        text_reports_error = False
        for content in result.content:
            if isinstance(content, types.TextContent):
                if re.match(
                    r"^\s*(?:http\s+)?error\s*(?:[:\-]\s*)?[45]\d{2}\b",
                    content.text,
                    flags=re.IGNORECASE,
                ):
                    text_reports_error = True
                contents.append(
                    {
                        "type": "text",
                        "text": redact_sensitive_text(
                            content.text, max_chars=self.config.max_result_chars
                        ),
                    }
                )
            elif isinstance(content, (types.ImageContent, types.AudioContent)):
                contents.append(
                    {
                        "type": content.type,
                        "mime_type": content.mimeType,
                        "artifact": await self._store_base64_artifact(
                            server, content.data, content.mimeType
                        ),
                    }
                )
            elif isinstance(content, types.ResourceLink):
                contents.append(
                    {
                        "type": "resource_link",
                        "uri": str(content.uri),
                        "name": content.name,
                        "title": content.title,
                        "description": redact_sensitive_text(
                            content.description or "", max_chars=500
                        ),
                        "mime_type": content.mimeType,
                        "size": content.size,
                    }
                )
            elif isinstance(content, types.EmbeddedResource):
                resource = content.resource
                if isinstance(resource, types.TextResourceContents):
                    value: dict[str, Any] = {
                        "uri": str(resource.uri),
                        "mime_type": resource.mimeType,
                        "text": redact_sensitive_text(
                            resource.text, max_chars=self.config.max_result_chars
                        ),
                    }
                else:
                    value = {
                        "uri": str(resource.uri),
                        "mime_type": resource.mimeType,
                        "artifact": await self._store_base64_artifact(
                            server, resource.blob, resource.mimeType
                        ),
                    }
                contents.append({"type": "resource", "resource": value})
        structured = self._bounded_value(result.structuredContent)
        structured_reports_error = bool(
            isinstance(result.structuredContent, dict)
            and (
                result.structuredContent.get("success") is False
                or bool(result.structuredContent.get("error"))
            )
        )
        return {
            "server": server,
            "tool": tool,
            # A few third-party MCP servers incorrectly return protocol-level
            # success while embedding an HTTP error or {"success": false} in
            # the payload. Preserve the content, but expose a truthful outcome
            # to the agent so it does not reason from a false green result.
            "is_error": bool(
                result.isError or text_reports_error or structured_reports_error
            ),
            "content": contents,
            "structured_content": structured,
        }

    def _bounded_value(self, value: Any) -> Any:
        if value is None:
            return None
        serialized = redact_sensitive_text(
            json.dumps(value, ensure_ascii=False, default=str)
        )
        if len(serialized) <= self.config.max_result_chars:
            try:
                return json.loads(serialized)
            except json.JSONDecodeError:
                return serialized
        return {
            "truncated": True,
            "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "preview": serialized[: self.config.max_result_chars - 200],
        }

    async def _store_base64_artifact(
        self, server: str, data: str, mime_type: Optional[str]
    ) -> dict[str, Any]:
        estimated_bytes = (len(data) * 3) // 4
        if estimated_bytes > self.config.max_binary_bytes:
            raise MCPClientError(
                "MCP binary result exceeds the configured artifact limit."
            )
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            raise MCPClientError("MCP server returned invalid base64 data.") from exc
        if len(decoded) > self.config.max_binary_bytes:
            raise MCPClientError(
                "MCP binary result exceeds the configured artifact limit."
            )
        digest = hashlib.sha256(decoded).hexdigest()
        suffix = mimetypes.guess_extension(mime_type or "") or ".bin"
        safe_server = "".join(
            character if character.isalnum() or character in "_.-" else "-"
            for character in server
        )
        path = self.artifacts_dir / safe_server / f"{digest}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, decoded)
        return {
            "path": path.relative_to(self.root_dir).as_posix(),
            "sha256": digest,
            "bytes": len(decoded),
        }

    def bounded_json(self, payload: dict[str, Any]) -> str:
        serialized = redact_sensitive_text(
            json.dumps(payload, ensure_ascii=False, default=str)
        )
        if len(serialized) <= self.config.max_result_chars:
            return serialized
        compact = {
            "truncated": True,
            "payload_sha256": hashlib.sha256(
                serialized.encode("utf-8")
            ).hexdigest(),
            "preview": serialized[: self.config.max_result_chars - 300],
        }
        return json.dumps(compact, ensure_ascii=False)
