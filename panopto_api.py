"""Panopto's REST API: OAuth, folders, permissions, and the upload seam.

Knows HTTP, not recordings. Nothing here imports session_reader, Qt, or a
camera; `panopto_upload.py` is this module's only importer in the app, the
same boundary winusb.py has under net2860_winusb_camera.py and that
nothing-outside-a-camera-module-touches-the-IDS-SDK has around ids_camera.

Two things to know before changing anything here:

**The endpoint paths are provisional.** The Notion brief ("Recording
Storage & Access Decision Brief") confirms these *operations* exist in
Panopto's Public API v1 -- folders, permissions, user search, session
upload -- but not their request shapes. Every path and payload below is
marked PROVISIONAL and is a guess until checked against the real spec or a
captured exchange. The rule is vendor/ids_peak_api.txt's: don't invent an
API and then build on it as though it were measured.

**The transport is injectable on purpose.** Every request goes through a
Transport, so tests drive the whole client against an in-memory double
with no site, no credentials and no network -- the same reason
SyntheticCamera exists. UrllibTransport is the real one; stdlib urllib
rather than requests keeps this off requirements.txt and out of the
PyInstaller specs until the upload transfer (below) forces a real choice.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# PROVISIONAL -- see the module docstring.
TOKEN_PATH = "/Panopto/oauth2/connect/token"
API_ROOT = "/Panopto/api/v1"
TOKEN_SCOPE = "api"

# Confirmed in the brief: the roles a folder or session grant can carry.
ROLE_VIEWER = "Viewer"
ROLE_VIEWER_WITH_LINK = "ViewerWithLink"
ROLE_CREATOR = "Creator"
ROLE_PUBLISHER = "Publisher"

DEFAULT_TIMEOUT_S = 30.0

# Refresh a token this long before it actually expires, so a call can't be
# issued with a token that dies in flight.
_TOKEN_SKEW_S = 60.0


class PanoptoError(RuntimeError):
    """Any failure talking to Panopto: transport, auth, or an error status."""


class PanoptoAuthError(PanoptoError):
    """Credentials were rejected. Distinct because it is the one failure a
    technician can fix without us -- wrong client id/secret, or a client
    that has not been granted API access."""


class PanoptoNotVerified(PanoptoError):
    """A code path whose wire format has not been confirmed against the
    real API yet. Raised rather than guessed at, so an unfinished
    integration fails immediately and says so instead of half-working."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PanoptoError(f"expected JSON, got {self.body[:120]!r}") from exc


class Transport(Protocol):
    """The whole HTTP surface this module needs. Implemented for real by
    UrllibTransport and in memory by the test double."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse: ...


class UrllibTransport:
    """The real transport. An error *status* comes back as a response, not
    an exception -- the client decides what a 401 means, and it means
    something specific here (refresh once, then give up). Only a transport
    failure with no status raises."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                body=exc.read(),
                headers={k.lower(): v for k, v in exc.headers.items()},
            )
        except urllib.error.URLError as exc:
            # No status at all: DNS, TLS, refused, timed out. The kiosk is
            # offline or the host is wrong, and the student needs to be
            # told that rather than shown a stack trace.
            raise PanoptoError(f"could not reach {url}: {exc.reason}") from exc


class PanoptoAuth:
    """Client-credentials token, cached until just before it expires.

    The service account's credentials, never a student's: students sign in
    to prove who they are, and the upload runs as the service account so
    they get view-only access to their own recording. See the Notion brief.
    """

    def __init__(
        self,
        host: str,
        client_id: str,
        client_secret: str,
        transport: Transport | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
    ):
        self._host = host
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport or UrllibTransport()
        self._now = now
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token is not None and self._now() < self._expires_at:
            return self._token
        return self._fetch()

    def invalidate(self) -> None:
        """Drop the cached token. The client calls this on a 401 so the
        next attempt re-authenticates rather than replaying a dead token."""
        self._token = None
        self._expires_at = 0.0

    def _fetch(self) -> str:
        # PROVISIONAL: form-encoded client_credentials with HTTP Basic
        # credentials is the usual IdentityServer shape, which is what
        # Panopto runs, but this exact call is unconfirmed.
        url = f"https://{self._host}{TOKEN_PATH}"
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": TOKEN_SCOPE}
        ).encode("ascii")
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode("utf-8")
        ).decode("ascii")
        response = self._transport.request(
            "POST",
            url,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
        )
        if response.status in (400, 401, 403):
            raise PanoptoAuthError(
                f"Panopto rejected the client credentials ({response.status}). "
                f"Check panopto.client_id/client_secret in config.json, and that "
                f"the client is allowed API access."
            )
        if not response.ok:
            raise PanoptoError(f"token request failed: HTTP {response.status}")

        payload = response.json() or {}
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise PanoptoError("token response carried no access_token")
        expires_in = payload.get("expires_in", 3600)
        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._token = token
        self._expires_at = self._now() + max(0.0, lifetime - _TOKEN_SKEW_S)
        return token


@dataclass
class UploadTarget:
    """Where a session's files are being uploaded, and under what id.

    PROVISIONAL in every field: Panopto's upload flow hands back an id and
    somewhere to put bytes, but the names and the mechanism (its own
    endpoint, or an S3-compatible target) are not confirmed. Fixed here as
    an interface so panopto_upload.py and the test double can both be
    written against it before the wire format is known.
    """

    upload_id: str
    folder_id: str
    destination: str


class PanoptoClient:
    """Folders, permissions, users, and the upload seam."""

    def __init__(self, host: str, auth: PanoptoAuth, transport: Transport | None = None):
        self._host = host
        self._auth = auth
        self._transport = transport or UrllibTransport()

    @classmethod
    def from_config(cls, panopto_config, transport: Transport | None = None) -> "PanoptoClient":
        """Build from config.PanoptoConfig. Typed loosely so this module
        stays importable without config.py, which matters for the
        standalone probe in tools/."""
        transport = transport or UrllibTransport()
        auth = PanoptoAuth(
            panopto_config.host,
            panopto_config.client_id,
            panopto_config.client_secret,
            transport,
        )
        return cls(panopto_config.host, auth, transport)

    # -- folders ---------------------------------------------------------

    def folder(self, folder_id: str) -> dict:
        """PROVISIONAL path."""
        return self._json("GET", f"/folders/{urllib.parse.quote(folder_id)}")

    def create_folder(self, parent_id: str, name: str) -> dict:
        """PROVISIONAL path and payload."""
        return self._json(
            "POST",
            "/folders",
            payload={"Name": name, "Parent": parent_id},
        )

    def find_folder(self, parent_id: str, name: str) -> dict | None:
        """The exact-name child of `parent_id`, or None.

        Exact match, not the first search hit: a student folder named for
        someone else because their names share a prefix is the filing
        mistake the brief calls out, and a recording in the wrong student's
        folder discloses a classmate's face to a stranger.
        """
        query = urllib.parse.urlencode({"searchQuery": name, "parentId": parent_id})
        results = self._json("GET", f"/folders/search?{query}")  # PROVISIONAL
        for candidate in _results_of(results):
            if candidate.get("Name") == name and _parent_id_of(candidate) == parent_id:
                return candidate
        return None

    def ensure_folder(self, parent_id: str, name: str) -> dict:
        """Find-or-create. The first-visit step in the brief's workflow."""
        existing = self.find_folder(parent_id, name)
        if existing is not None:
            return existing
        return self.create_folder(parent_id, name)

    # -- permissions -----------------------------------------------------

    def folder_access(self, folder_id: str) -> dict:
        """PROVISIONAL path."""
        return self._json("GET", f"/folders/{urllib.parse.quote(folder_id)}/access")

    def grant_folder_access(self, folder_id: str, user_id: str, role: str = ROLE_VIEWER) -> None:
        """PROVISIONAL path and payload."""
        self._json(
            "PUT",
            f"/folders/{urllib.parse.quote(folder_id)}/access/{urllib.parse.quote(user_id)}"
            f"?role={urllib.parse.quote(role)}",
        )

    def revoke_folder_access(self, folder_id: str, user_id: str, role: str = ROLE_VIEWER) -> None:
        """PROVISIONAL path. Present because the probe in tools/ must be
        able to undo what it did to a real site."""
        self._json(
            "DELETE",
            f"/folders/{urllib.parse.quote(folder_id)}/access/{urllib.parse.quote(user_id)}"
            f"?role={urllib.parse.quote(role)}",
        )

    # -- users -----------------------------------------------------------

    def find_user(self, username: str) -> dict | None:
        """PROVISIONAL path. Exact match on the username, for the same
        reason find_folder is exact."""
        query = urllib.parse.urlencode({"searchQuery": username})
        results = self._json("GET", f"/users/search?{query}")
        for candidate in _results_of(results):
            if candidate.get("Username") == username:
                return candidate
        return None

    # -- upload ----------------------------------------------------------
    #
    # The seam. The interface is fixed so panopto_upload.py and the test
    # double are written now; the wire format lands here, in one place,
    # when it can be checked against the real thing.

    def begin_upload(self, folder_id: str) -> UploadTarget:
        raise PanoptoNotVerified(
            "Panopto's upload-session call is not confirmed yet. Run "
            "tools/panopto_probe.py against a real site, then implement this "
            "one method -- see PANOPTO_PLAN.md's 'Assumptions to verify'."
        )

    def put_upload_file(
        self,
        target: UploadTarget,
        local_path: Path,
        remote_name: str,
        progress_cb: Callable[[int, int], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> None:
        raise PanoptoNotVerified(
            "Panopto's upload transfer is not confirmed yet (its own endpoint "
            "or an S3-compatible target). See PANOPTO_PLAN.md."
        )

    def finish_upload(self, target: UploadTarget) -> str:
        raise PanoptoNotVerified(
            "Panopto's upload-completion call is not confirmed yet. It is what "
            "returns the session id the viewer URL is built from."
        )

    def viewer_url(self, session_id: str) -> str:
        """PROVISIONAL, but the stable-looking half of the API."""
        return f"https://{self._host}/Panopto/Pages/Viewer.aspx?id={urllib.parse.quote(session_id)}"

    # -- plumbing --------------------------------------------------------

    def _json(self, method: str, path: str, payload: dict | None = None) -> dict:
        response = self._request(method, path, payload)
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def _request(self, method: str, path: str, payload: dict | None = None) -> HttpResponse:
        """One retry, and only on a 401.

        A 401 usually means the cached token expired mid-session, which is
        ordinary on a kiosk left open all afternoon. Anything else is a
        real error and is raised: retrying a 500 just delays the message
        the student needs to see.
        """
        url = f"https://{self._host}{API_ROOT}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None

        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._auth.token()}", "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            response = self._transport.request(method, url, headers=headers, body=body)
            if response.status == 401 and attempt == 1:
                logger.info("Panopto returned 401 for %s %s; refreshing token", method, path)
                self._auth.invalidate()
                continue
            break

        if response.status in (401, 403):
            raise PanoptoAuthError(
                f"Panopto refused {method} {path} ({response.status}). The service "
                f"account may not have rights on this folder."
            )
        if not response.ok:
            raise PanoptoError(f"{method} {path} failed: HTTP {response.status} {_snippet(response.body)}")
        return response


def _results_of(payload: dict) -> list[dict]:
    """Panopto's search responses wrap their rows; which key is PROVISIONAL,
    so accept the plausible ones rather than crashing on the wrong guess."""
    for key in ("Results", "results", "Items", "items"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _parent_id_of(folder: dict) -> str | None:
    parent = folder.get("Parent") or folder.get("ParentFolder")
    if isinstance(parent, dict):
        return parent.get("Id")
    return parent if isinstance(parent, str) else None


def _snippet(body: bytes, limit: int = 200) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    return text[:limit] if text else ""
