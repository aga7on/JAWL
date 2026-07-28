# Multimodality Configuration (Vision and Generation)

The `multimodality` interface lets the agent inspect images/videos and use the
configured QWB endpoint for durable image/video generation jobs.

## Important requirement

Visual analysis works only when the primary LLM supports the corresponding
input modality. Set `llm.is_multimodal: true` in `settings.yaml`. Media
generation uses separate Qwen Web media modes through QWB and does not replace
the primary reasoning/coding model.

## Configuration

```yaml
multimodality:
  enabled: true
  video_understanding_enabled: true
  media_generation_enabled: true
  media_request_timeout_sec: 30
  media_poll_interval_sec: 5.0
  media_max_upload_mb: 50
  media_max_download_mb: 500
```

`media_generation_enabled` expects `LLM_API_URL` to point at QWB's `/v1`
base URL.

## Visual input

`look_at_image` and `look_at_video` create system markers. On the next ReAct
step, the loop validates and reads the local file, encodes it as Base64, and
injects an OpenAI-compatible `image_url` or `video_url` content part. QWB then
uploads the media to Qwen Web as a vision file.

Telegram images and supported videos are downloaded into
`sandbox/telegram_media/` and attached to the wake event. Oversized media is
rejected before prompt construction.

## Durable media jobs

Generation is deliberately asynchronous:

1. `MediaSkills.generate_image`, `edit_image`, or `generate_video` returns a
   `media_*` job ID.
2. `MediaSkills.check_media_job` reads its state. A bounded `wait_seconds` can
   be used for short waits.
3. Once complete, `download_result=true` downloads the expiring upstream URL
   into `sandbox/_system/download/` and returns `artifact_path`.
4. Telegram delivery remains a separate explicit `send_file` action.

QWB persists bounded job metadata. If QWB restarts during generation, the job
becomes `interrupted` instead of remaining permanently `running`.

## References

- Image generation/editing accepts one or more reference image paths or URLs.
- Video generation accepts image/video references and optional first/last
  frame anchors.
- Local references are validated by the Host OS gatekeeper and bounded by
  `media_max_upload_mb`.
- Exact reference-to-video behavior still depends on the capabilities exposed
  by the current Qwen Web media backend.
