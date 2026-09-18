"""Panopto's REST API for a signed-in student: login, one folder read, and
the session upload.

Knows HTTP, not recordings. Nothing here imports session_reader, Qt, or a
camera; `panopto_upload.py` is this module's only importer in the app, the
same boundary winusb.py has under net2860_winusb_camera.py and that
nothing-outside-a-camera-module-touches-the-IDS-SDK has around ids_camera.

**There is no service account.** The student signs in as themselves and
the upload runs as them, into a Panopto Assignment Folder -- where each
student sees only their own submissions and faculty see all (ROADMAP.md
2026-09-18). That is why this module holds no credential worth stealing:
the client id is public by design, and the client secret Panopto issues
for its "Server-side Web Application" client (its authorization-code type;
the "User Based" type is the password grant, which is *not* this) cannot
mint a token without a live login. The kiosk holds nothing that grants
access on its own.

**Two different APIs live behind one host.** Reading a folder is the
Public API v1 under /Panopto/api/v1. Uploading is a separate, older REST
surface under /Panopto/PublicAPI/REST, and it is not JSON all the way
down: it hands back an S3 target and the files go there.

**What is confirmed and what is not.** The upload flow is written from
Panopto's own published sample (Panopto/upload-python-sample) and the UCS
2.0 schema; the OAuth endpoints and scope from their
panopto-api-python-examples, and Basic-auth client authentication from
their "OAuth2 Access Tokens For Services" article. Those are theirs. The
folder path is the brief's confirmed operation with an unconfirmed shape,
and is marked PROVISIONAL; PKCE is sent in addition to what their sample
sends, and a standard IdentityServer ignores it when unused. The rule is
vendor/ids_peak_api.txt's -- don't invent an API and then build on it as
though it were measured.

**The transport is injectable on purpose.** Every request goes through a
Transport, so tests drive the whole client -- login included -- against an
in-memory double with no site, no credentials and no network. UrllibTransport
is the real one; stdlib urllib keeps this off requirements.txt.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# Confirmed against Panopto/panopto-api-python-examples. Scope is
# "openid api" with no offline_access: that is what would hand us a
# refresh token, and a kiosk must not remember anyone.
AUTHORIZE_PATH = "/Panopto/oauth2/connect/authorize"
TOKEN_PATH = "/Panopto/oauth2/connect/token"
TOKEN_SCOPE = "api"
# PROVISIONAL path shape; the operation is confirmed.
API_ROOT = "/Panopto/api/v1"

# Confirmed against Panopto's published Python sample: the upload API is
# its own surface, and the session id it returns is spelled "ID".
UPLOAD_API_ROOT = "/Panopto/PublicAPI/REST"
UPLOAD_STATE_COMPLETE = 1  # what we PUT to say "all files are there"
UPLOAD_STATE_PROCESSED = 4  # what Panopto reports once it has processed them

# The manifest's name in Panopto's sample. Kept identical rather than
# improved: the server picks the session XML out of the uploaded files,
# and this is the name that is known to work.
MANIFEST_FILENAME = "upload_manifest_generated.xml"

# The loopback redirect IT registers on the API client. Fixed, because a
# registered redirect URI has to match exactly; config.json can override
# it if the port is taken on some machine. 127.0.0.1 rather than
# "localhost", as RFC 8252 recommends: on Windows "localhost" resolves to
# ::1 first and the fallback costs two seconds per request.
REDIRECT_HOST = "127.0.0.1"
DEFAULT_REDIRECT_PORT = 48219
REDIRECT_PATH = "/callback"

# How long a student gets to complete the sign-in before the kiosk gives
# up and says so. Long enough for a password and a 2FA prompt; short
# enough that an abandoned login doesn't hold the viewer open all day.
DEFAULT_LOGIN_TIMEOUT_S = 180.0

DEFAULT_TIMEOUT_S = 30.0

# Refresh a token this long before it actually expires, so a call can't be
# issued with a token that dies in flight.
_TOKEN_SKEW_S = 60.0


class PanoptoError(RuntimeError):
    """Any failure talking to Panopto: transport, auth, or an error status."""


class PanoptoAuthError(PanoptoError):
    """Sign-in was refused, or a call was made without one. Distinct so the
    viewer can say "sign in again" rather than "upload failed"."""


class LoginCancelled(PanoptoError):
    """The student backed out, or the sign-in timed out. Not an error to
    report loudly: the recording is still in the buffer and they can try
    again."""


class UploadAborted(PanoptoError):
    """Raised out of the transfer's progress callback to stop it. boto3
    offers no cancel, so an exception is the only way out of a transfer
    already in flight; panopto_upload turns it back into UploadCancelled."""


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
    an exception -- the client decides what a 401 means. Only a transport
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


class TokenSource(Protocol):
    """What PanoptoClient needs from a login: a bearer token, and a way to
    drop one that a 401 has shown to be dead."""

    def token(self) -> str: ...

    def invalidate(self) -> None: ...


# -- sign-in ---------------------------------------------------------------


def open_in_private_browser(url: str) -> Callable[[], None] | None:
    """Open the sign-in in a browser that shares nothing with the last
    student's, and return a closer that ends it.

    This is the whole privacy property of signing in on a shared machine.
    Two things have to hold, and InPrivate alone gives neither reliably:

    - *Nothing carried in.* A throwaway `--user-data-dir` is a browser
      that has never seen a cookie. InPrivate would do this too, but --
    - *Nothing left behind.* If Edge is already running, a new
      `--inprivate` window belongs to that existing process, so no handle
      we hold can close it, and the window sits there signed into the
      student's Google account until someone notices. A throwaway profile
      directory forces a separate process that is ours to terminate, and
      deleting the directory afterwards removes whatever it wrote.

    The closer is idempotent and never raises: it runs in a `finally`
    after sign-in, whatever happened. The fallback to the default browser
    returns no closer and is logged as a warning, because it is a real
    loss -- the kiosk image must have Edge (DECISIONS.md 2026-09-18).
    """
    edge = _find_edge()
    if edge is None:
        logger.warning(
            "Microsoft Edge not found; sign-in is opening in the default browser, "
            "which may keep the previous student's session"
        )
        webbrowser.open(url)
        return None

    profile = tempfile.mkdtemp(prefix="reflex-signin-")
    process = subprocess.Popen(
        [
            edge,
            f"--user-data-dir={profile}",
            "--inprivate",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            url,
        ]
    )

    def close() -> None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001 -- best effort; the profile delete is what matters
            logger.warning("sign-in browser did not exit cleanly")
        shutil.rmtree(profile, ignore_errors=True)

    return close


def _find_edge() -> str | None:
    on_path = shutil.which("msedge")
    if on_path:
        return on_path
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not root:
            continue
        candidate = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if candidate.exists():
            return str(candidate)
    return None


class UserLogin:
    """Authorization code + PKCE for one student, over a loopback redirect.

    sign_in() opens the browser and blocks until the redirect comes back,
    the timeout passes, or cancel_cb() goes True -- and then closes that
    browser, always, because it is signed into the student's Google
    account. The token lives only in memory and sign_out() drops it: a
    kiosk must never remember who was last here. Nothing is refreshed -- a session upload takes minutes and
    the token lasts an hour; if it does expire, the viewer asks for a fresh
    sign-in rather than this module quietly holding a refresh token.
    """

    def __init__(
        self,
        host: str,
        client_id: str,
        client_secret: str | None = None,
        transport: Transport | None = None,
        *,
        redirect_port: int = DEFAULT_REDIRECT_PORT,
        open_url: Callable[[str], Callable[[], None] | None] = open_in_private_browser,
        timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
        now: Callable[[], float] = time.monotonic,
    ):
        self._host = host
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport or UrllibTransport()
        self._redirect_port = redirect_port
        self._open_url = open_url
        self._timeout_s = timeout_s
        self._now = now
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def redirect_uri(self) -> str:
        return f"http://{REDIRECT_HOST}:{self._redirect_port}{REDIRECT_PATH}"

    @property
    def signed_in(self) -> bool:
        return self._token is not None and self._now() < self._expires_at

    def token(self) -> str:
        if not self.signed_in:
            raise PanoptoAuthError("not signed in to Panopto")
        return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        self.sign_out()

    def sign_out(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def sign_in(self, cancel_cb: Callable[[], bool] | None = None) -> None:
        """Run the browser flow to completion. Raises LoginCancelled on
        timeout or cancel, PanoptoAuthError if Panopto refused."""
        self.sign_out()
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        state = secrets.token_urlsafe(24)

        with _LoopbackReceiver(self._redirect_port, state) as receiver:
            query = urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": self._client_id,
                    "redirect_uri": self.redirect_uri,
                    "scope": f"openid {TOKEN_SCOPE}",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
            close_browser = self._open_url(f"https://{self._host}{AUTHORIZE_PATH}?{query}")
            try:
                code = receiver.wait(self._timeout_s, cancel_cb)
            finally:
                # Whatever happened -- code received, timed out, cancelled --
                # the window signed into the student's Google account must
                # not outlive this call.
                if close_browser is not None:
                    close_browser()

        self._exchange(code, verifier)

    def _exchange(self, code: str, verifier: str) -> None:
        # Client authentication as Panopto documents it ("How to Get OAuth2
        # Access Tokens For Services", 1.2): id and secret as HTTP Basic in
        # the Authorization header, not in the form. code_verifier is ours
        # on top; harmless where PKCE isn't enforced.
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self._client_id,
            "code_verifier": verifier,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self._client_secret:
            basic = base64.b64encode(
                f"{self._client_id}:{self._client_secret}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"
        response = self._transport.request(
            "POST",
            f"https://{self._host}{TOKEN_PATH}",
            headers=headers,
            body=urllib.parse.urlencode(form).encode("ascii"),
        )
        if response.status in (400, 401, 403):
            raise PanoptoAuthError(
                f"Panopto refused the sign-in ({response.status}). If this keeps "
                f"happening, the API client in Settings may be misconfigured."
            )
        if not response.ok:
            raise PanoptoError(f"token exchange failed: HTTP {response.status}")

        payload = response.json() or {}
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise PanoptoError("token response carried no access_token")
        try:
            lifetime = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._token = token
        self._expires_at = self._now() + max(0.0, lifetime - _TOKEN_SKEW_S)


class _LoopbackReceiver:
    """One-shot HTTP listener for the OAuth redirect on 127.0.0.1.

    Loopback only, never 0.0.0.0: the code it receives is single-use, but
    there is no reason to let anything else on the network hand us one.
    The state check is what stops a stray or malicious redirect from
    completing someone else's sign-in.
    """

    _DONE_PAGE = (
        b"<!doctype html><meta charset='utf-8'><title>Reflex</title>"
        b"<p style='font: 18px system-ui; margin: 3em'>Signed in. You can close this window "
        b"and return to Reflex.</p>"
    )

    def __init__(self, port: int, expected_state: str):
        self._expected_state = expected_state
        self._result: dict[str, str] = {}
        self._event = threading.Event()
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler's spelling
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                if parts.path != REDIRECT_PATH:
                    self.send_error(404)
                    return
                state = query.get("state", [""])[0]
                if state != receiver._expected_state:
                    logger.warning("ignored a redirect with the wrong state")
                    self.send_error(400, "state mismatch")
                    return
                if "error" in query:
                    receiver._result["error"] = query["error"][0]
                else:
                    receiver._result["code"] = query.get("code", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(receiver._DONE_PAGE)
                receiver._event.set()

            def log_message(self, *_args):  # quiet; the app's logger is the record
                pass

        try:
            self._server = _ExclusiveServer((REDIRECT_HOST, port), Handler)
        except OSError as exc:
            raise PanoptoError(
                f"could not listen on {REDIRECT_HOST}:{port} for the sign-in redirect: {exc}"
            ) from exc
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def __enter__(self) -> "_LoopbackReceiver":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait(self, timeout_s: float, cancel_cb: Callable[[], bool] | None) -> str:
        deadline = time.monotonic() + timeout_s
        while not self._event.is_set():
            if cancel_cb is not None and cancel_cb():
                raise LoginCancelled("sign-in cancelled")
            if time.monotonic() >= deadline:
                raise LoginCancelled("sign-in timed out")
            self._event.wait(0.1)
        if "error" in self._result:
            raise PanoptoAuthError(f"sign-in refused: {self._result['error']}")
        code = self._result.get("code", "")
        if not code:
            raise PanoptoAuthError("sign-in redirect carried no code")
        return code


class _ExclusiveServer(http.server.HTTPServer):
    """HTTPServer that refuses to share its port.

    socketserver sets SO_REUSEADDR by default, and on Windows that lets a
    second process bind a port that is already listening -- which for an
    OAuth receiver means another process could take the redirect and the
    code with it. SO_EXCLUSIVEADDRUSE is the Windows way to say no; on
    other platforms the attribute doesn't exist and plain binding is
    already exclusive.
    """

    allow_reuse_address = False

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


# -- client ----------------------------------------------------------------


@dataclass
class UploadTarget:
    """Where a session's files are going, and under what id.

    `destination` is Panopto's UploadTarget: an S3-style URL whose last two
    path segments are the bucket and the key prefix. `raw` is the whole
    sessionUpload object, kept because completing the upload PUTs it back
    with its State changed rather than sending a fresh body.
    """

    upload_id: str
    folder_id: str
    destination: str
    raw: dict = field(default_factory=dict)

    def s3_parts(self) -> tuple[str, str, str]:
        """(endpoint, bucket, prefix), split the way Panopto's own sample
        splits it: everything but the last two segments is the endpoint."""
        elements = self.destination.split("/")
        if len(elements) < 3:
            raise PanoptoError(f"upload target is not an S3 URL: {self.destination!r}")
        return "/".join(elements[:-2]), elements[-2], elements[-1]


class PanoptoClient:
    """One folder read, and the upload. Everything runs as whoever signed in."""

    def __init__(self, host: str, auth: TokenSource, transport: Transport | None = None):
        self._host = host
        self._auth = auth
        self._transport = transport or UrllibTransport()

    def folder(self, folder_id: str) -> dict:
        """PROVISIONAL path. What Test connection reads, and what confirms
        the signed-in student can actually see the assignment folder."""
        return self._json("GET", f"/folders/{urllib.parse.quote(folder_id)}")

    # -- upload ----------------------------------------------------------
    #
    # Written from Panopto/upload-python-sample. Three steps: ask for an
    # upload, put the files (videos plus the UCS manifest) on the S3 target
    # it names, then PUT the object back with State=1 to say we are done.

    def begin_upload(self, folder_id: str) -> UploadTarget:
        created = self._json(
            "POST", "/sessionUpload", payload={"FolderId": folder_id}, root=UPLOAD_API_ROOT
        )
        upload_id = created.get("ID")
        destination = created.get("UploadTarget")
        if not upload_id or not destination:
            raise PanoptoError(f"sessionUpload returned no ID/UploadTarget: {created}")
        return UploadTarget(
            upload_id=str(upload_id), folder_id=folder_id, destination=str(destination), raw=created
        )

    def put_upload_file(
        self,
        target: UploadTarget,
        local_path: Path | str,
        remote_name: str,
        progress_cb: Callable[[int, int], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> None:
        """Send one file to the upload target with S3 multipart.

        boto3 is imported here, not at module scope, so the module loads
        without it; it is pinned in requirements.txt because the frozen
        app has to carry it. Configured exactly as Panopto's sample does,
        dummy credentials and all -- the target URL is the authorization.
        """
        try:
            import boto3  # noqa: PLC0415 -- deliberately lazy; see above
        except ImportError as exc:
            raise PanoptoError("uploading needs boto3 (pip install -r requirements.txt)") from exc

        local_path = Path(local_path)
        endpoint, bucket, prefix = target.s3_parts()
        total = local_path.stat().st_size
        sent = 0

        def on_bytes(count: int) -> None:
            nonlocal sent
            if cancel_cb is not None and cancel_cb():
                raise UploadAborted()
            sent += count
            if progress_cb is not None:
                progress_cb(min(sent, total), total)

        s3 = boto3.session.Session().client(
            service_name="s3",
            endpoint_url=endpoint,
            aws_access_key_id="dummy",
            aws_secret_access_key="dummy",
            config=boto3.session.Config(signature_version="s3"),
        )
        s3.upload_file(str(local_path), bucket, f"{prefix}/{remote_name}", Callback=on_bytes)

    def finish_upload(self, target: UploadTarget) -> str:
        """Mark every file uploaded; return the id the viewer URL needs."""
        payload = dict(target.raw)
        payload["State"] = UPLOAD_STATE_COMPLETE
        done = self._json(
            "PUT", f"/sessionUpload/{target.upload_id}", payload=payload, root=UPLOAD_API_ROOT
        )
        # SessionId appears once Panopto has made the session; until then
        # the upload's own ID is what identifies it.
        return str(done.get("SessionId") or done.get("ID") or target.upload_id)

    def upload_state(self, upload_id: str) -> int:
        """Where processing has got to. UPLOAD_STATE_PROCESSED means the
        session is watchable; the probe polls this, the kiosk does not --
        a student should not wait on Panopto's encoder."""
        state = self._json("GET", f"/sessionUpload/{upload_id}", root=UPLOAD_API_ROOT).get("State")
        return int(state) if state is not None else -1

    def viewer_url(self, session_id: str) -> str:
        """PROVISIONAL, but the stable-looking half of the API."""
        return f"https://{self._host}/Panopto/Pages/Viewer.aspx?id={urllib.parse.quote(session_id)}"

    # -- plumbing --------------------------------------------------------

    def _json(self, method: str, path: str, payload: dict | None = None, root: str = API_ROOT) -> dict:
        response = self._request(method, path, payload, root)
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}

    def _request(
        self, method: str, path: str, payload: dict | None = None, root: str = API_ROOT
    ) -> HttpResponse:
        """No retry. A 401 means the student's token is gone -- expired, or
        the sign-in never happened -- and the only fix is signing in again,
        which is the viewer's to ask for. Anything else is a real error and
        is raised: retrying a 500 just delays the message the student needs.
        """
        url = f"https://{self._host}{root}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self._auth.token()}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self._transport.request(method, url, headers=headers, body=body)

        if response.status == 401:
            self._auth.invalidate()
            raise PanoptoAuthError("Panopto no longer accepts this sign-in. Sign in again.")
        if response.status == 403:
            raise PanoptoAuthError(
                f"Panopto refused {method} {path}: this account cannot use that folder."
            )
        if not response.ok:
            raise PanoptoError(f"{method} {path} failed: HTTP {response.status} {_snippet(response.body)}")
        return response


def _snippet(body: bytes, limit: int = 200) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    return text[:limit] if text else ""
