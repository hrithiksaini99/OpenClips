"""Attaching a YouTube account and uploading a clip to it.

OAuth 2.0 for installed apps, done against the standard library: the flow is
three HTTP calls, and pulling in google-auth-oauthlib plus
google-api-python-client would add about a dozen transitive packages to an
install that has to land on a laptop.

Two Google behaviours drive the shape of this module and are worth knowing
before reading it:

* Videos uploaded through `videos.insert` from an API project that has not
  passed an audit are locked to private, permanently and without appeal. That
  is not something this code can work around; it only decides what
  `privacy` is worth defaulting to.
* A refresh token issued while the OAuth consent screen is still in "Testing"
  is revoked after seven days, which shows up as an unattended schedule that
  quietly stops posting. `access_token` turns that into an explicit message.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "youtube-token.json"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"
THUMBNAIL_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"

# youtube.upload is the narrowest scope that can post. youtube.readonly is here
# only so the UI can name the channel a token belongs to, which is worth one
# extra scope to stop someone posting to the wrong account for a week.
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)

# A client shipped with OpenClips, so a user connects in one click without
# visiting the Google Cloud console at all. Empty until this project has its own
# audited OAuth client; when it does, setting these two makes every install
# one-click. Note the quota is then shared across everyone using that client:
# 100 uploads a day between them, which is the same trade rclone makes with its
# bundled Drive client.
BUNDLED_CLIENT_ID = os.environ.get("OPENCLIPS_YT_CLIENT_ID", "")
BUNDLED_CLIENT_SECRET = os.environ.get("OPENCLIPS_YT_CLIENT_SECRET", "")

# Where Chrome and Safari drop the file Google hands you.
DOWNLOADS = Path.home() / "Downloads"
_CLIENT_PATTERNS = ("client_secret*.json", "*googleusercontent*.json", "*oauth*client*.json")

# 8 MiB per PUT: large enough that the per-request overhead is noise, small
# enough that a dropped connection costs little to redo.
CHUNK = 8 << 20


class YouTubeError(RuntimeError):
    """Anything Google refused, phrased for the person reading the UI."""


class NotConnected(YouTubeError):
    """No usable token: the account needs attaching, or re-attaching."""


class UploadLimitReached(YouTubeError):
    """The channel has hit its own daily upload cap.

    Nothing to do with the API quota and not fixable from this end: it is
    tied to the channel, rolls over 24 hours after each upload, and is raised
    separately so the queue can wait instead of spending retries on it.
    """


class SetupRequired(YouTubeError):
    """The Google Cloud OAuth client file has not been provided yet."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib following a 308.

    Mid-upload, Google answers 308 to say how many bytes it kept. That is a
    status, not a redirect, and it carries no Location header; left to its own
    devices urllib treats it as one and the upload dies.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)

# One attach can be in flight at a time on a single-user local app, so the PKCE
# verifier lives in memory rather than on disk: it must not outlive the flow.
_pending: dict[str, str] = {}


def _explain(error: urllib.error.HTTPError, *, context: str = "") -> str:
    """Turn a Google error body into something a person can act on.

    `context` matters: Google's 403 for "this channel may not set custom
    thumbnails" says the user cannot "upload and set custom video thumbnails",
    and matching the word "upload" in it produced a message telling people their
    account could not upload at all — while the video sat happily on YouTube.
    """
    try:
        payload = json.loads(error.read())
    except Exception:
        return f"Google returned HTTP {error.code}"
    detail = payload.get("error")
    if isinstance(detail, str):  # the OAuth endpoints use a flat shape
        description = payload.get("error_description", "")
        return f"{detail}{': ' + description if description else ''}"
    if not isinstance(detail, dict):
        return f"Google returned HTTP {error.code}"
    reasons = {item.get("reason", "") for item in detail.get("errors", [])}
    message = detail.get("message", "")
    if "uploadLimitExceeded" in reasons or "number of videos they may upload" in message:
        return (
            "this channel has hit YouTube's daily upload limit. It is a per-channel "
            "cap that rolls over 24 hours after each upload, separate from the API "
            "quota. Verifying the channel at youtube.com/verify raises it."
        )
    if "youtubeSignupRequired" in reasons:
        return "That Google account has no YouTube channel. Create one, then reconnect."
    if "quotaExceeded" in reasons:
        return "This project's daily upload quota is used up; it resets at midnight Pacific."
    if error.code in (401, 403) and context == "thumbnail":
        return (
            "this channel cannot set custom thumbnails yet. Verify your phone "
            "number at youtube.com/verify to turn them on; on Shorts they also "
            "need YouTube Partner Programme membership."
        )
    if "forbidden" in reasons and "upload" in detail.get("message", "").lower():
        return "This account is not allowed to upload. Check the channel is in good standing."
    return f"{detail.get('message') or 'Request rejected'} (HTTP {error.code})"


def _post_form(url: str, fields: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raise YouTubeError(_explain(error)) from None
    except urllib.error.URLError as error:
        raise YouTubeError(f"Could not reach Google: {error.reason}") from None


def read_client(payload: dict) -> dict[str, str]:
    """Pull the client out of a downloaded OAuth JSON file.

    Google exports desktop clients under "installed" and web clients under
    "web"; a user who pastes the inner object alone should also work.
    """
    block = payload.get("installed") or payload.get("web") or payload
    if not isinstance(block, dict) or "client_id" not in block:
        raise SetupRequired("That is not a Google OAuth client file")
    return block


def save_client(payload: dict) -> dict[str, str]:
    """Validate an OAuth client file and keep it, so the console is a one-off."""
    block = read_client(payload)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_SECRET.write_text(json.dumps(payload, indent=2))
    with contextlib.suppress(OSError):
        os.chmod(CLIENT_SECRET, 0o600)
    return block


def detect_clients() -> list[dict]:
    """Look for the file Google just handed the user, in their Downloads folder.

    Saves them finding the file and moving it into the project by hand, which is
    the only genuinely fiddly step left in connecting an account.
    """
    if not DOWNLOADS.is_dir():
        return []
    seen: set[Path] = set()
    found: list[dict] = []
    for pattern in _CLIENT_PATTERNS:
        for path in DOWNLOADS.glob(pattern):
            if path in seen or not path.is_file() or path.stat().st_size > 64_000:
                continue
            seen.add(path)
            try:
                payload = json.loads(path.read_text())
                block = read_client(payload)
            except (json.JSONDecodeError, SetupRequired, OSError, UnicodeDecodeError):
                continue  # some other JSON that happens to match the name
            found.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "client_id": block["client_id"][:24] + "…",
                    "kind": "web" if "web" in payload else "desktop",
                    "modified": path.stat().st_mtime,
                }
            )
    found.sort(key=lambda item: -item["modified"])
    return found[:5]


def adopt_client(path: str) -> dict[str, str]:
    """Copy a detected file into config/ and use it."""
    source = Path(path).expanduser().resolve()
    # Only ever adopt from the Downloads folder we advertised, so a stray path
    # from the browser cannot make the server read somewhere else.
    if DOWNLOADS.resolve() not in source.parents:
        raise SetupRequired("That file is not in your Downloads folder")
    try:
        return save_client(json.loads(source.read_text()))
    except json.JSONDecodeError:
        raise SetupRequired(f"{source.name} is not valid JSON") from None


def _get(url: str) -> dict:
    """Authorised GET against the Data API."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token()}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise YouTubeError(_explain(error)) from None


def client_config() -> dict[str, str]:
    """The OAuth client to authorise against: the user's, or the bundled one."""
    if CLIENT_SECRET.is_file():
        try:
            return read_client(json.loads(CLIENT_SECRET.read_text()))
        except json.JSONDecodeError:
            raise SetupRequired(f"{CLIENT_SECRET.name} is not valid JSON") from None
    if BUNDLED_CLIENT_ID:
        return {"client_id": BUNDLED_CLIENT_ID, "client_secret": BUNDLED_CLIENT_SECRET}
    raise SetupRequired("No Google OAuth client has been added yet")


def _pkce() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def begin(redirect_uri: str) -> str:
    """Build the consent URL for the user to open."""
    config = client_config()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    _pending.clear()  # drop any flow the user abandoned
    _pending[state] = verifier
    query = urllib.parse.urlencode(
        {
            "client_id": config["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            # Without this, re-attaching an account returns no refresh token and
            # posting stops as soon as the first access token expires.
            "prompt": "consent",
        }
    )
    return f"{AUTH_ENDPOINT}?{query}"


def complete(*, code: str, state: str, redirect_uri: str) -> None:
    """Exchange the callback's code for a refresh token and store it."""
    verifier = _pending.pop(state, None)
    if verifier is None:
        raise YouTubeError("This sign-in did not start here. Press Connect again.")
    config = client_config()
    tokens = _post_form(
        TOKEN_ENDPOINT,
        {
            "client_id": config["client_id"],
            "client_secret": config.get("client_secret", ""),
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    if not tokens.get("refresh_token"):
        raise YouTubeError(
            "Google returned no refresh token. Remove OpenClips from your account's "
            "third-party access list, then connect again."
        )
    _save(
        {
            "refresh_token": tokens["refresh_token"],
            "access_token": tokens.get("access_token", ""),
            "expires_at": time.time() + float(tokens.get("expires_in", 0)) - 60,
            "attached_at": time.time(),
        }
    )


def _save(token: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token, indent=2))
    # The refresh token is a standing key to the channel; keep it away from
    # other accounts on the machine.
    # Windows ACLs do not map onto POSIX modes.
    with contextlib.suppress(OSError):
        os.chmod(TOKEN_FILE, 0o600)


def _load() -> dict | None:
    if not TOKEN_FILE.is_file():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError:
        return None


def connected() -> bool:
    token = _load()
    return bool(token and token.get("refresh_token"))


def disconnect() -> None:
    """Forget the stored token, and tell Google to drop the grant as well."""
    token = _load()
    TOKEN_FILE.unlink(missing_ok=True)
    if token and token.get("refresh_token"):
        # The local token is gone either way, which is what matters.
        with contextlib.suppress(YouTubeError):
            _post_form(REVOKE_ENDPOINT, {"token": token["refresh_token"]})


def access_token() -> str:
    """Return a live access token, refreshing it when the current one is stale."""
    token = _load()
    if not token or not token.get("refresh_token"):
        raise NotConnected("No YouTube account is attached")
    if token.get("access_token") and time.time() < token.get("expires_at", 0):
        return token["access_token"]
    config = client_config()
    try:
        fresh = _post_form(
            TOKEN_ENDPOINT,
            {
                "client_id": config["client_id"],
                "client_secret": config.get("client_secret", ""),
                "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
    except YouTubeError as error:
        raise NotConnected(
            f"{error} — reconnect the account. If this happens every week, set the "
            "OAuth consent screen to 'In production': tokens issued in 'Testing' "
            "are revoked after seven days."
        ) from None
    token["access_token"] = fresh["access_token"]
    token["expires_at"] = time.time() + float(fresh.get("expires_in", 3600)) - 60
    _save(token)
    return token["access_token"]


@dataclass(frozen=True)
class Channel:
    id: str
    title: str
    thumbnail: str


def channel() -> Channel:
    """The channel the stored token posts to."""
    query = urllib.parse.urlencode({"part": "snippet", "mine": "true"})
    request = urllib.request.Request(
        f"{CHANNELS_ENDPOINT}?{query}",
        headers={"Authorization": f"Bearer {access_token()}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise YouTubeError(_explain(error)) from None
    items = payload.get("items") or []
    if not items:
        raise YouTubeError("That Google account has no YouTube channel.")
    snippet = items[0].get("snippet", {})
    thumbnails = snippet.get("thumbnails", {})
    return Channel(
        id=items[0].get("id", ""),
        title=snippet.get("title", "Unknown channel"),
        thumbnail=(thumbnails.get("default") or {}).get("url", ""),
    )


def status() -> dict:
    """Describe the attachment state for the UI. Never raises."""
    configured = CLIENT_SECRET.is_file() or bool(BUNDLED_CLIENT_ID)
    state: dict = {
        "connected": False,
        "client_configured": configured,
        "client_bundled": not CLIENT_SECRET.is_file() and bool(BUNDLED_CLIENT_ID),
        "client_secret_path": str(CLIENT_SECRET),
        "detected": [] if configured else detect_clients(),
        "channel": None,
        "error": "",
    }
    if not configured or not connected():
        return state
    state["connected"] = True
    try:
        found = channel()
        state["channel"] = {
            "id": found.id,
            "title": found.title,
            "thumbnail": found.thumbnail,
        }
    except Exception as error:
        state["error"] = str(error)
    return state


def upload(
    path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    privacy: str = "private",
    category_id: str = "22",
    made_for_kids: bool = False,
) -> str:
    """Upload one clip, returning its YouTube video id.

    Resumable rather than a single POST: the session survives a dropped
    connection and the bytes that already landed are not sent twice.
    """
    size = path.stat().st_size
    body = json.dumps(
        {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy,
                # Required by the API; leaving it out fails the whole upload.
                "selfDeclaredMadeForKids": made_for_kids,
            },
        }
    ).encode()
    query = urllib.parse.urlencode({"uploadType": "resumable", "part": "snippet,status"})
    start = urllib.request.Request(
        f"{UPLOAD_ENDPOINT}?{query}",
        data=body,
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
    )
    try:
        with urllib.request.urlopen(start, timeout=60) as response:
            session = response.headers.get("Location", "")
    except urllib.error.HTTPError as error:
        explained = _explain(error)
        if "daily upload limit" in explained:
            raise UploadLimitReached(explained) from None
        raise YouTubeError(explained) from None
    except urllib.error.URLError as error:
        raise YouTubeError(f"Could not reach YouTube: {error.reason}") from None
    if not session:
        raise YouTubeError("YouTube did not open an upload session")
    return _send(session, path, size)


def _resume_offset(error: urllib.error.HTTPError, *, fallback: int) -> int:
    """How many bytes YouTube kept, from the Range header it sends with a 308."""
    header = error.headers.get("Range", "")
    if "-" in header:
        try:
            return int(header.rsplit("-", 1)[1]) + 1
        except ValueError:
            pass
    return fallback


def _send(session: str, path: Path, size: int) -> str:
    """PUT the file into an open upload session, resuming where it left off."""
    offset = 0
    attempts = 0
    with path.open("rb") as handle:
        while offset < size:
            handle.seek(offset)
            chunk = handle.read(CHUNK)
            end = offset + len(chunk) - 1
            request = urllib.request.Request(
                session,
                data=chunk,
                method="PUT",
                headers={
                    # Re-read per chunk: a slow upload can outlive the token.
                    "Authorization": f"Bearer {access_token()}",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                },
            )
            try:
                with _opener.open(request, timeout=600) as response:
                    payload = json.loads(response.read() or b"{}")
                    video_id = payload.get("id")
                    if not video_id:
                        raise YouTubeError("Upload finished but YouTube returned no video id")
                    return video_id
            except urllib.error.HTTPError as error:
                if error.code == 308:  # accepted so far; send the next chunk
                    offset = _resume_offset(error, fallback=end + 1)
                    attempts = 0
                    continue
                if error.code in (500, 502, 503, 504) and attempts < 3:
                    attempts += 1
                    time.sleep(2**attempts)  # Google asks for backoff on 5xx
                    continue
                explained = _explain(error)
                if "daily upload limit" in explained:
                    raise UploadLimitReached(explained) from None
                raise YouTubeError(explained) from None
            except urllib.error.URLError as error:
                if attempts < 3:
                    attempts += 1
                    time.sleep(2**attempts)
                    continue
                raise YouTubeError(f"Upload connection failed: {error.reason}") from None
    raise YouTubeError("Upload ended without a video id")


def set_thumbnail(video_id: str, image: Path) -> None:
    """Attach a custom thumbnail to a video.

    Expected to be refused on plenty of channels, and the caller should treat
    that as cosmetic: custom thumbnails need a phone-verified channel, Shorts
    thumbnails additionally need Partner Programme membership, and YouTube's
    July 2026 rollout describes Shorts thumbnails as a Studio feature, so the
    API may decline them outright. The video is already up either way.
    """
    query = urllib.parse.urlencode({"videoId": video_id})
    request = urllib.request.Request(
        f"{THUMBNAIL_ENDPOINT}?{query}",
        data=image.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "image/jpeg",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120):
            return
    except urllib.error.HTTPError as error:
        raise YouTubeError(_explain(error, context="thumbnail")) from None
    except urllib.error.URLError as error:
        raise YouTubeError(f"Could not reach YouTube: {error.reason}") from None


def recent_uploads(limit: int = 50) -> dict[str, str]:
    """Map recent video titles on the channel to their ids.

    Used to work out whether an interrupted upload actually landed. Costs two
    quota units against the 10,000-a-day pool.
    """
    channels = urllib.parse.urlencode({"part": "contentDetails", "mine": "true"})
    payload = _get(f"{CHANNELS_ENDPOINT}?{channels}")
    items = payload.get("items") or []
    if not items:
        return {}
    playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    query = urllib.parse.urlencode(
        {"part": "snippet", "maxResults": min(limit, 50), "playlistId": playlist}
    )
    listing = _get(f"https://www.googleapis.com/youtube/v3/playlistItems?{query}")
    stamped: dict[str, tuple[str, str]] = {}
    for item in listing.get("items", []):
        snippet = item.get("snippet", {})
        title = snippet.get("title", "")
        if not title:
            continue
        published = snippet.get("publishedAt", "")
        # The listing arrives newest-first, so keep the oldest id for a title:
        # if a clip was somehow posted twice, reconciling should point at the
        # original rather than adopt the accidental copy.
        if title not in stamped or published < stamped[title][0]:
            stamped[title] = (published, snippet.get("resourceId", {}).get("videoId", ""))
    return {title: video_id for title, (_at, video_id) in stamped.items()}
