"""QWB media generation and durable job client."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import httpx

from src.l2_interfaces.host.os.client import HostOSClient
from src.utils.logger import main_logger
from src.utils.settings import MultimodalityConfig


_TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
_SAFE_JOB_ID = re.compile(r"^media_[a-f0-9]+$", re.IGNORECASE)


class QWBMediaClient:
    """Calls the bridge media-job API without coupling it to ReAct internals."""

    def __init__(
        self,
        host_os_client: HostOSClient,
        config: MultimodalityConfig,
        api_url: str,
        api_key: str,
        state_path: Path,
    ) -> None:
        self.host_os = host_os_client
        self.config = config
        self.api_url = self._normalize_api_url(api_url)
        self.api_key = api_key
        self.state_path = state_path
        self._jobs: dict[str, dict[str, Any]] = {}
        self.is_online = bool(self.api_url and self.api_key)
        self._load_state()

    @staticmethod
    def _normalize_api_url(api_url: str) -> str:
        value = str(api_url or "").strip().rstrip("/")
        for suffix in ("/chat/completions", "/responses"):
            if value.endswith(suffix):
                value = value[: -len(suffix)]
        return value

    def _endpoint(self, suffix: str) -> str:
        return f"{self.api_url}/{suffix.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "JAWL-Media/1.0",
        }

    def _load_state(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        jobs = payload.get("jobs", {}) if isinstance(payload, dict) else {}
        if isinstance(jobs, dict):
            self._jobs = {
                str(job_id): value
                for job_id, value in jobs.items()
                if _SAFE_JOB_ID.fullmatch(str(job_id))
                and isinstance(value, dict)
            }

    def _save_state_sync(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        payload = {
            "version": 1,
            "jobs": self._jobs,
        }
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, self.state_path)

    async def _remember(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id", ""))
        if not _SAFE_JOB_ID.fullmatch(job_id):
            return
        # The bridge response contains no account token. Keep only bounded,
        # operational metadata in the local durable ledger.
        self._jobs[job_id] = {
            "id": job_id,
            "kind": job.get("kind"),
            "operation": job.get("operation"),
            "status": job.get("status"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "progress": job.get("progress", 0),
            "result": job.get("result"),
            "error": job.get("error"),
        }
        if len(self._jobs) > 200:
            ordered = sorted(
                self._jobs.values(),
                key=lambda item: int(item.get("updated_at") or 0),
            )
            for stale in ordered[: len(self._jobs) - 200]:
                self._jobs.pop(str(stale.get("id")), None)
        await asyncio.to_thread(self._save_state_sync)

    async def _request(
        self,
        method: str,
        suffix: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.is_online:
            raise RuntimeError("QWB media API is not configured")
        timeout = httpx.Timeout(float(self.config.media_request_timeout_sec))
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.request(
                method,
                self._endpoint(suffix),
                headers=self._headers(),
                json=payload,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"QWB returned non-JSON response (HTTP {response.status_code})"
            ) from exc
        if response.status_code >= 400:
            message = (
                body.get("error", {}).get("message")
                if isinstance(body, dict)
                else None
            )
            raise RuntimeError(
                f"QWB media HTTP {response.status_code}: {message or body}"
            )
        if not isinstance(body, dict):
            raise RuntimeError("QWB returned an invalid media response")
        return body

    def _encode_reference_sync(self, value: str, expected: str) -> str:
        if value.startswith(("http://", "https://", "data:")):
            return value
        safe_path = self.host_os.validate_path(value, is_write=False)
        if not safe_path.is_file():
            raise FileNotFoundError(f"Media reference not found: {value}")
        mime_type = mimetypes.guess_type(safe_path.name)[0] or ""
        if not mime_type.startswith(f"{expected}/"):
            raise ValueError(
                f"Expected a {expected} reference, got {mime_type or safe_path.suffix}"
            )
        size = safe_path.stat().st_size
        limit = int(self.config.media_max_upload_mb) * 1024 * 1024
        if size > limit:
            raise ValueError(
                f"Media reference is {size} bytes; configured limit is {limit} bytes"
            )
        encoded = base64.b64encode(safe_path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    async def _encode_references(
        self,
        values: Optional[Iterable[str]],
        expected: str,
    ) -> list[str]:
        result = []
        for value in values or []:
            if not isinstance(value, str) or not value.strip():
                continue
            result.append(
                await asyncio.to_thread(
                    self._encode_reference_sync,
                    value.strip(),
                    expected,
                )
            )
        return result

    async def start_job(
        self,
        *,
        kind: str,
        prompt: str,
        operation: str = "generate",
        size: Optional[str] = None,
        model: Optional[str] = None,
        reference_images: Optional[Iterable[str]] = None,
        reference_videos: Optional[Iterable[str]] = None,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> dict[str, Any]:
        if kind not in {"image", "video"}:
            raise ValueError("kind must be image or video")
        if operation not in {"generate", "edit"}:
            raise ValueError("operation must be generate or edit")
        if not prompt.strip():
            raise ValueError("Media prompt cannot be empty")
        payload: dict[str, Any] = {
            "kind": kind,
            "operation": operation,
            "prompt": prompt.strip(),
            "async": True,
        }
        if size:
            payload["size"] = size
        if model:
            payload["model"] = model
        images = await self._encode_references(reference_images, "image")
        videos = await self._encode_references(reference_videos, "video")
        if images:
            payload["reference_images"] = images
        if videos:
            payload["reference_videos"] = videos
        if first_frame:
            payload["first_frame"] = await asyncio.to_thread(
                self._encode_reference_sync, first_frame, "image"
            )
        if last_frame:
            payload["last_frame"] = await asyncio.to_thread(
                self._encode_reference_sync, last_frame, "image"
            )
        if duration is not None:
            payload["duration"] = max(1, min(60, int(duration)))
        job = await self._request("POST", "media/jobs", payload)
        await self._remember(job)
        return job

    async def get_job(self, job_id: str) -> dict[str, Any]:
        if not _SAFE_JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid media job ID")
        job = await self._request("GET", f"media/jobs/{job_id}")
        await self._remember(job)
        return job

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        if not _SAFE_JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid media job ID")
        job = await self._request("DELETE", f"media/jobs/{job_id}")
        await self._remember(job)
        return job

    def list_local_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return sorted(
            self._jobs.values(),
            key=lambda item: int(item.get("updated_at") or 0),
            reverse=True,
        )[: max(1, min(100, int(limit)))]

    async def wait_for_job(
        self,
        job_id: str,
        timeout_seconds: int = 0,
    ) -> dict[str, Any]:
        deadline = (
            asyncio.get_running_loop().time() + timeout_seconds
            if timeout_seconds > 0
            else None
        )
        while True:
            job = await self.get_job(job_id)
            if job.get("status") in _TERMINAL_STATES:
                return job
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return job
            await asyncio.sleep(float(self.config.media_poll_interval_sec))

    async def download_result(
        self,
        job: dict[str, Any],
        output_filename: Optional[str] = None,
    ) -> str:
        if job.get("status") != "completed":
            raise RuntimeError(f"Media job is not completed: {job.get('status')}")
        result = job.get("result") or {}
        url = str(result.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError("QWB media result has no safe HTTP(S) URL")
        kind = "video" if job.get("kind") == "video" else "image"
        default_extension = ".mp4" if kind == "video" else ".png"
        remote_extension = Path(parsed.path).suffix.lower()
        allowed_extensions = (
            {".mp4", ".webm", ".mov", ".mkv"}
            if kind == "video"
            else {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        )
        extension = (
            remote_extension
            if remote_extension in allowed_extensions
            else default_extension
        )
        filename = output_filename or f"{job['id']}{extension}"
        if "/" not in filename and "\\" not in filename:
            filename = f"sandbox/_system/download/{filename}"
        safe_path = self.host_os.validate_path(filename, is_write=True)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = safe_path.with_suffix(safe_path.suffix + ".part")
        max_bytes = int(self.config.media_max_download_mb) * 1024 * 1024
        downloaded = 0
        timeout = httpx.Timeout(
            connect=float(self.config.media_request_timeout_sec),
            read=300.0,
            write=float(self.config.media_request_timeout_sec),
            pool=float(self.config.media_request_timeout_sec),
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type and content_type != "application/octet-stream":
                        if not content_type.startswith(f"{kind}/"):
                            raise RuntimeError(
                                f"Generated media has unexpected content type "
                                f"{content_type}"
                            )
                    content_length = int(response.headers.get("content-length") or 0)
                    if content_length > max_bytes:
                        raise RuntimeError("Generated media exceeds the download limit")
                    with open(temp_path, "wb") as output:
                        async for chunk in response.aiter_bytes():
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise RuntimeError(
                                    "Generated media exceeds the download limit"
                                )
                            output.write(chunk)
            os.replace(temp_path, safe_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        main_logger.info(
            f"[Multimodality] Downloaded {kind} result: {safe_path.name} "
            f"({downloaded} bytes)"
        )
        return safe_path.relative_to(self.host_os.sandbox_dir).as_posix()

    async def get_context_block(self, **kwargs: Any) -> str:
        state = "ON" if self.is_online else "OFF"
        active = sum(
            1
            for job in self._jobs.values()
            if job.get("status") in {"queued", "running"}
        )
        return (
            f"### QWB MEDIA [{state}]\n"
            "Description: Durable image/video generation, image editing, "
            "reference workflows, and artifact downloads.\n"
            f"Known jobs: {len(self._jobs)}; active: {active}."
        )
