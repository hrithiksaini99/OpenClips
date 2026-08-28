"""Instagram Reels publisher using the graph API through a shell-free transport."""

import json
import subprocess
from collections.abc import Sequence

from openclips.providers.platforms.base import (
    PlatformPublisher,
    PublishError,
    PublishRequest,
    PublishResult,
    Transport,
    require_media,
    run_transport,
)

_GRAPH_BASE = "https://graph.facebook.com/v19.0"


class InstagramReelsPublisher(PlatformPublisher):
    """Publishes vertical clips as Instagram Reels for one linked account."""

    def __init__(
        self,
        *,
        account_id: str,
        access_token: str,
        binary: str = "curl",
        transport: Transport | None = None,
    ) -> None:
        self._account_id = account_id
        self._access_token = access_token
        self._binary = binary
        self._transport = transport or self._default_transport

    @staticmethod
    def _default_transport(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False)

    def publish(self, request: PublishRequest) -> PublishResult:
        if not self._account_id or not self._access_token:
            msg = "Instagram account credentials are not configured"
            raise PublishError(msg)
        require_media(request)
        if request.media_url is None:
            msg = "Instagram Reels require a publicly reachable media URL"
            raise PublishError(msg)

        media_url = request.media_url
        container_argv = [
            self._binary,
            "-sS",
            "-X",
            "POST",
            f"{_GRAPH_BASE}/{self._account_id}/media",
            "-d",
            json.dumps(
                {
                    "media_type": "REELS",
                    "video_url": media_url,
                    "caption": f"{request.title}\n\n{request.description}".strip(),
                    "access_token": self._access_token,
                }
            ),
        ]
        container_body = self._parse(
            run_transport(self._transport, container_argv, platform="Instagram")
        )
        container_id = str(container_body.get("id", ""))
        if not container_id:
            msg = "Instagram did not return a media container id"
            raise PublishError(msg)

        publish_argv = [
            self._binary,
            "-sS",
            "-X",
            "POST",
            f"{_GRAPH_BASE}/{self._account_id}/media_publish",
            "-d",
            json.dumps(
                {
                    "creation_id": container_id,
                    "access_token": self._access_token,
                }
            ),
        ]
        publish_body = self._parse(
            run_transport(self._transport, publish_argv, platform="Instagram")
        )
        external_id = str(publish_body.get("id", container_id))
        return PublishResult(
            external_id=external_id,
            external_url=f"https://www.instagram.com/reel/{external_id}/",
        )

    @staticmethod
    def _parse(stdout: str) -> dict[str, object]:
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError as error:
            msg = f"Instagram returned unparsable JSON: {error}"
            raise PublishError(msg) from error
        if not isinstance(payload, dict):
            msg = "Instagram returned unexpected JSON"
            raise PublishError(msg)
        if "error" in payload:
            msg = f"Instagram API error: {payload['error'].get('message', payload['error'])}"
            raise PublishError(msg)
        return payload
