"""YouTube Shorts publisher using the resumable upload endpoint."""

import json
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

from openclips.providers.platforms.base import (
    PlatformPublisher,
    PublishError,
    PublishRequest,
    PublishResult,
    Transport,
    require_media,
    run_transport,
)

_UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubeShortsPublisher(PlatformPublisher):
    """Publishes clips as YouTube videos flagged for the Shorts shelf."""

    def __init__(
        self,
        *,
        access_token: str,
        binary: str = "curl",
        transport: Transport | None = None,
    ) -> None:
        self._access_token = access_token
        self._binary = binary
        self._transport = transport or self._default_transport

    @staticmethod
    def _default_transport(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False)

    def publish(self, request: PublishRequest) -> PublishResult:
        if not self._access_token:
            msg = "YouTube access token is not configured"
            raise PublishError(msg)
        require_media(request)

        boundary = uuid.uuid4().hex
        metadata = {
            "snippet": {
                "title": request.title[:100],
                "description": request.description,
            },
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            "Content-Type: video/mp4\r\n\r\n"
        ).encode()
        upload_file = Path(request.clip_media).with_suffix(".upload-body")
        upload_file.write_bytes(body + Path(request.clip_media).read_bytes())
        try:
            argv = [
                self._binary,
                "-sS",
                "-X",
                "POST",
                f"{_UPLOAD_BASE}?uploadType=multipart&part=snippet,status",
                "-H",
                f"Authorization: Bearer {self._access_token}",
                "-H",
                f"Content-Type: multipart/related; boundary={boundary}",
                "--data-binary",
                f"@{upload_file}",
            ]
            stdout = run_transport(self._transport, argv, platform="YouTube")
        finally:
            upload_file.unlink(missing_ok=True)
        payload = self._parse(stdout)
        external_id = str(payload.get("id", ""))
        if not external_id:
            msg = "YouTube did not return a video id"
            raise PublishError(msg)
        return PublishResult(
            external_id=external_id,
            external_url=f"https://www.youtube.com/shorts/{external_id}",
        )

    @staticmethod
    def _parse(stdout: str) -> dict[str, object]:
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError as error:
            msg = f"YouTube returned unparsable JSON: {error}"
            raise PublishError(msg) from error
        if not isinstance(payload, dict):
            msg = "YouTube returned unexpected JSON"
            raise PublishError(msg)
        if "error" in payload:
            message = payload["error"]
            if isinstance(message, dict):
                message = message.get("message", message)
            msg = f"YouTube API error: {message}"
            raise PublishError(msg)
        return payload
