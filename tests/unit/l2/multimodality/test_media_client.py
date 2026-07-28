import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.l2_interfaces.multimodality.client import MultimodalityClient
from src.l2_interfaces.multimodality.media_client import QWBMediaClient
from src.l2_interfaces.multimodality.skills.media import MediaSkills
from src.utils.settings import MultimodalityConfig


def make_client(tmp_path: Path, host: MagicMock) -> QWBMediaClient:
    return QWBMediaClient(
        host_os_client=host,
        config=MultimodalityConfig(
            enabled=True,
            video_understanding_enabled=True,
            media_generation_enabled=True,
        ),
        api_url="http://127.0.0.1:8000/v1/chat/completions",
        api_key="local-test-key",
        state_path=tmp_path / "media_jobs.json",
    )


@pytest.mark.asyncio
async def test_start_image_job_encodes_local_reference(tmp_path):
    host = MagicMock()
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"png-bytes")
    host.validate_path.return_value = reference
    host.sandbox_dir = tmp_path
    client = make_client(tmp_path, host)
    client._request = AsyncMock(
        return_value={
            "id": "media_abcdef",
            "kind": "image",
            "operation": "edit",
            "status": "queued",
        }
    )

    job = await client.start_job(
        kind="image",
        operation="edit",
        prompt="keep the face",
        reference_images=["sandbox/reference.png"],
    )

    assert job["id"] == "media_abcdef"
    request_payload = client._request.await_args.args[2]
    assert request_payload["reference_images"][0].startswith(
        "data:image/png;base64,"
    )
    assert client.api_url == "http://127.0.0.1:8000/v1"
    saved = json.loads((tmp_path / "media_jobs.json").read_text(encoding="utf-8"))
    assert saved["jobs"]["media_abcdef"]["status"] == "queued"


@pytest.mark.asyncio
async def test_media_skills_attach_video_and_return_background_job(tmp_path):
    host = MagicMock()
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    host.validate_path.return_value = video
    vision = MultimodalityClient(host_os_client=host)
    media_client = MagicMock()
    media_client.is_online = True
    media_client.start_job = AsyncMock(
        return_value={
            "id": "media_123abc",
            "kind": "video",
            "status": "queued",
        }
    )
    skills = MediaSkills(vision, media_client, video_understanding_enabled=True)

    attached = await skills.look_at_video("sandbox/clip.mp4")
    generated = await skills.generate_video(
        "slow camera motion",
        first_frame="sandbox/frame.png",
    )

    assert attached.is_success is True
    assert "[SYSTEM_MARKER_VIDEO_ATTACHED:" in attached.message
    assert generated.is_success is True
    assert json.loads(generated.message)["id"] == "media_123abc"
    media_client.start_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_media_job_downloads_completed_artifact():
    vision = MagicMock()
    media_client = MagicMock()
    media_client.is_online = True
    media_client.wait_for_job = AsyncMock(
        return_value={
            "id": "media_abc123",
            "kind": "image",
            "status": "completed",
            "result": {"url": "https://example.invalid/result.png"},
        }
    )
    media_client.download_result = AsyncMock(
        return_value="_system/download/media_abc123.png"
    )
    skills = MediaSkills(vision, media_client, video_understanding_enabled=True)

    result = await skills.check_media_job("media_abc123")

    assert result.is_success is True
    assert json.loads(result.message)["artifact_path"].endswith(".png")
    media_client.download_result.assert_awaited_once()
