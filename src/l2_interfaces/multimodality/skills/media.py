"""Agent skills for video understanding and QWB media jobs."""

from __future__ import annotations

import json
from typing import List, Optional

from src.l2_interfaces.multimodality.client import MultimodalityClient
from src.l2_interfaces.multimodality.media_client import QWBMediaClient
from src.l3_agent.skills.registry import SkillResult, skill


def _job_message(job: dict) -> str:
    return json.dumps(job, ensure_ascii=False, separators=(",", ":"))


class MediaSkills:
    """Media inspection, generation, editing, status and cancellation tools."""

    def __init__(
        self,
        vision_client: MultimodalityClient,
        media_client: Optional[QWBMediaClient],
        video_understanding_enabled: bool,
    ) -> None:
        self.vision_client = vision_client
        self.media_client = media_client
        self.video_understanding_enabled = video_understanding_enabled

    def _require_media_client(self) -> QWBMediaClient:
        if self.media_client is None or not self.media_client.is_online:
            raise RuntimeError("QWB media generation is disabled or not configured")
        return self.media_client

    @skill()
    async def look_at_video(self, filepath: str) -> SkillResult:
        """
        Attaches a local video to the next model request for visual analysis.

        filepath: Video path inside sandbox/.
        """
        if not self.video_understanding_enabled:
            return SkillResult.fail("Video understanding is disabled in interfaces.yaml.")
        try:
            safe_path = self.vision_client.host_os.validate_path(
                filepath, is_write=False
            )
            if not safe_path.is_file():
                return SkillResult.fail(f"Error: File not found ({filepath}).")
            if safe_path.suffix.lower() not in {
                ".mp4",
                ".webm",
                ".mov",
                ".mkv",
                ".avi",
                ".mpeg",
                ".mpg",
            }:
                return SkillResult.fail(
                    f"Error: Format {safe_path.suffix.lower()} is not a supported video."
                )
            return SkillResult.ok(
                f"[SYSTEM_MARKER_VIDEO_ATTACHED: {safe_path.resolve()}]: True. "
                "Video is queued for the next multimodal model request."
            )
        except PermissionError as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error trying to view video: {exc}")

    @skill()
    async def generate_image(
        self,
        prompt: str,
        size: Optional[str] = None,
        reference_images: Optional[List[str]] = None,
        model: Optional[str] = None,
    ) -> SkillResult:
        """
        Starts a background text/reference-to-image job and returns its job ID.

        reference_images: Optional sandbox paths or HTTP(S) URLs.
        """
        try:
            job = await self._require_media_client().start_job(
                kind="image",
                operation="generate",
                prompt=prompt,
                size=size,
                model=model,
                reference_images=reference_images,
            )
            return SkillResult.ok(_job_message(job))
        except Exception as exc:
            return SkillResult.fail(f"Image generation error: {exc}")

    @skill()
    async def edit_image(
        self,
        prompt: str,
        reference_images: List[str],
        size: Optional[str] = None,
        model: Optional[str] = None,
    ) -> SkillResult:
        """
        Starts a background image-edit job using one or more reference images.

        reference_images: Required sandbox paths or HTTP(S) URLs.
        """
        if not reference_images:
            return SkillResult.fail("At least one reference image is required.")
        try:
            job = await self._require_media_client().start_job(
                kind="image",
                operation="edit",
                prompt=prompt,
                size=size,
                model=model,
                reference_images=reference_images,
            )
            return SkillResult.ok(_job_message(job))
        except Exception as exc:
            return SkillResult.fail(f"Image editing error: {exc}")

    @skill()
    async def generate_video(
        self,
        prompt: str,
        size: Optional[str] = None,
        reference_images: Optional[List[str]] = None,
        reference_videos: Optional[List[str]] = None,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        duration: Optional[int] = None,
        model: Optional[str] = None,
    ) -> SkillResult:
        """
        Starts a background text/image/reference-to-video job and returns its job ID.

        References may be sandbox paths or HTTP(S) URLs. first_frame and
        last_frame provide explicit transition anchors when supported upstream.
        """
        try:
            job = await self._require_media_client().start_job(
                kind="video",
                operation="generate",
                prompt=prompt,
                size=size,
                model=model,
                reference_images=reference_images,
                reference_videos=reference_videos,
                first_frame=first_frame,
                last_frame=last_frame,
                duration=duration,
            )
            return SkillResult.ok(_job_message(job))
        except Exception as exc:
            return SkillResult.fail(f"Video generation error: {exc}")

    @skill()
    async def check_media_job(
        self,
        job_id: str,
        wait_seconds: int = 0,
        download_result: bool = True,
        output_filename: Optional[str] = None,
    ) -> SkillResult:
        """
        Checks a media job, optionally waits briefly, and downloads completed output.

        wait_seconds: Bounded wait for short polling; use 0 for an immediate check.
        output_filename: Optional destination inside sandbox/.
        """
        try:
            client = self._require_media_client()
            job = await client.wait_for_job(
                job_id,
                timeout_seconds=max(0, min(300, int(wait_seconds))),
            )
            if job.get("status") == "completed" and download_result:
                job["artifact_path"] = await client.download_result(
                    job,
                    output_filename=output_filename,
                )
            return SkillResult.ok(_job_message(job))
        except Exception as exc:
            return SkillResult.fail(f"Media job check error: {exc}")

    @skill()
    async def cancel_media_job(self, job_id: str) -> SkillResult:
        """Cancels a queued or running QWB media job."""
        try:
            job = await self._require_media_client().cancel_job(job_id)
            return SkillResult.ok(_job_message(job))
        except Exception as exc:
            return SkillResult.fail(f"Media job cancellation error: {exc}")

    @skill()
    async def list_media_jobs(self, limit: int = 20) -> SkillResult:
        """Lists locally remembered media jobs without contacting Qwen."""
        try:
            jobs = self._require_media_client().list_local_jobs(limit=limit)
            return SkillResult.ok(
                json.dumps(jobs, ensure_ascii=False, separators=(",", ":"))
            )
        except Exception as exc:
            return SkillResult.fail(f"Media job listing error: {exc}")
