"""Tests for panopto_api against an in-memory Panopto, plus the double
itself, which test_panopto_upload.py imports.

The double plays both halves: the API (folder read, session upload) and
the identity provider. For sign-in it stands in for the browser too --
given the authorize URL it "logs the student in" by hitting the app's own
loopback redirect, so the whole PKCE flow runs for real on 127.0.0.1 with
no site, no credentials and no browser.

**What these tests do and don't prove.** The double answers the same
paths panopto_api.py sends, so it cannot tell us those paths are right --
only a real site settles that (DECISIONS.md 2026-09-18). What it proves is the
client's own behaviour, which is where the bugs that would cost a student
a recording or a classmate their privacy live: PKCE and state actually
checked, a token that lives only in memory and dies on sign-out, a 401
that demands a fresh sign-in rather than a silent retry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import unittest
import urllib.parse
import urllib.request

from panopto_api import (
    API_ROOT,
    UPLOAD_API_ROOT,
    UPLOAD_STATE_COMPLETE,
    UPLOAD_STATE_PROCESSED,
    HttpResponse,
    LoginCancelled,
    PanoptoAuthError,
    PanoptoClient,
    PanoptoError,
    UploadTarget,
    UserLogin,
)

HOST = "neco.example.panopto.com"
CLIENT_ID = "reflex-kiosk"
CLIENT_SECRET = "s3cret"

# One port per test process, well away from the app's default, so a
# developer running the app and the tests side by side doesn't collide.
TEST_REDIRECT_PORT = 48999


class FakePanoptoSite:
    """An in-memory Panopto behind the Transport seam, and the browser
    that signs a student in.

    Holds folders and uploads, and records every request so a test can
    assert on what was sent. `refuse_login` and `fail_next` force the
    failure paths that are otherwise unreachable without a real outage.
    """

    def __init__(self, client_id: str = CLIENT_ID, client_secret: str | None = None):
        self._client_id = client_id
        self._client_secret = client_secret
        self.folders: dict[str, dict] = {}
        self.uploads: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.authorize_urls: list[str] = []
        self.issued_token = "token-1"
        self.token_lifetime = 3600
        self.token_requests = 0
        self.fail_next: tuple[int, str] | None = None
        self.refuse_login = False
        self.expire_token = False
        self._pending: dict[str, str] = {}  # code -> expected code_challenge

    # -- seeding ---------------------------------------------------------

    def add_folder(self, folder_id: str, name: str) -> dict:
        folder = {"Id": folder_id, "Name": name}
        self.folders[folder_id] = folder
        return folder

    # -- the browser -----------------------------------------------------

    def open_url(self, url: str) -> None:
        """What the app hands the browser. Signs in immediately by calling
        the app's loopback redirect from another thread, the way a browser
        would after the student typed their password."""
        self.authorize_urls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        redirect = query["redirect_uri"][0]
        state = query["state"][0]
        if self.refuse_login:
            params = {"state": state, "error": "access_denied"}
        else:
            code = f"code-{len(self._pending) + 1}"
            self._pending[code] = query["code_challenge"][0]
            params = {"state": state, "code": code}
        threading.Thread(
            target=_hit, args=(f"{redirect}?{urllib.parse.urlencode(params)}",), daemon=True
        ).start()

    # -- transport -------------------------------------------------------

    def request(self, method, url, *, headers=None, body=None, timeout=30.0) -> HttpResponse:
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        self.requests.append((method, path))

        if path.endswith("/oauth2/connect/token"):
            return self._token(body or b"", headers or {})

        if self.fail_next is not None:
            status, message = self.fail_next
            self.fail_next = None
            return _json_response(status, {"Message": message})

        auth = (headers or {}).get("Authorization", "")
        if self.expire_token or auth != f"Bearer {self.issued_token}":
            return _json_response(401, {"Message": "expired"})

        payload = json.loads(body.decode("utf-8")) if body else None
        if path.startswith(UPLOAD_API_ROOT):
            return self._upload_api(method, path[len(UPLOAD_API_ROOT):], payload)
        if not path.startswith(API_ROOT):
            return _json_response(404, {"Message": "no such path"})
        return self._api(method, path[len(API_ROOT):], query)

    def _token(self, body: bytes, headers: dict) -> HttpResponse:
        self.token_requests += 1
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("ascii")).items()}
        if form.get("grant_type") != "authorization_code":
            return _json_response(400, {"error": "unsupported_grant_type"})
        if form.get("client_id") != self._client_id:
            return _json_response(400, {"error": "invalid_client"})
        if self._client_secret is not None:
            # As Panopto documents it: Basic auth, never the form body.
            expected = base64.b64encode(
                f"{self._client_id}:{self._client_secret}".encode("utf-8")
            ).decode("ascii")
            if headers.get("Authorization") != f"Basic {expected}" or "client_secret" in form:
                return _json_response(401, {"error": "invalid_client"})
        challenge = self._pending.pop(form.get("code", ""), None)
        if challenge is None:
            return _json_response(400, {"error": "invalid_grant"})
        verifier = form.get("code_verifier", "").encode("ascii")
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode()
        if expected != challenge:
            return _json_response(400, {"error": "invalid_grant", "detail": "pkce"})
        return _json_response(
            200, {"access_token": self.issued_token, "expires_in": self.token_lifetime}
        )

    def _api(self, method: str, path: str, query: dict) -> HttpResponse:
        if path.startswith("/folders/") and method == "GET":
            folder = self.folders.get(urllib.parse.unquote(path[len("/folders/"):]))
            if folder is None:
                return _json_response(404, {"Message": "no such folder"})
            return _json_response(200, folder)
        return _json_response(404, {"Message": f"unhandled {method} {path}"})

    def _upload_api(self, method: str, path: str, payload: dict | None) -> HttpResponse:
        """Panopto's upload surface: ask, then say when the files are there."""
        if path == "/sessionUpload" and method == "POST":
            upload_id = f"upload-{len(self.uploads) + 1}"
            record = {
                "ID": upload_id,
                "FolderId": payload["FolderId"],
                "UploadTarget": f"https://s3.example.com/panopto-bucket/{upload_id}",
                "State": 0,
                "files": [],
            }
            self.uploads[upload_id] = record
            return _json_response(200, record)

        if path.startswith("/sessionUpload/"):
            upload_id = path[len("/sessionUpload/"):]
            record = self.uploads.get(upload_id)
            if record is None:
                return _json_response(404, {"Message": "no such upload"})
            if method == "PUT":
                record["State"] = payload.get("State", record["State"])
                if record["State"] == UPLOAD_STATE_COMPLETE:
                    record["SessionId"] = f"session-{upload_id}"
                return _json_response(200, record)
            if method == "GET":
                return _json_response(200, record)

        return _json_response(404, {"Message": f"unhandled {method} {path}"})


def _hit(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except Exception as exc:  # noqa: BLE001 -- surfaced by the test's own timeout
        print(f"loopback redirect failed: {exc}")


def _json_response(status: int, payload: dict) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


def make_login(site: FakePanoptoSite, secret: str | None = None, **kwargs) -> UserLogin:
    return UserLogin(
        HOST, CLIENT_ID, secret, site,
        redirect_port=TEST_REDIRECT_PORT, open_url=site.open_url, timeout_s=5.0, **kwargs,
    )


def make_client(site: FakePanoptoSite) -> PanoptoClient:
    """A client whose student has already signed in."""
    login = make_login(site)
    login.sign_in()
    return PanoptoClient(HOST, login, site)


class SignInTest(unittest.TestCase):
    def setUp(self):
        self.site = FakePanoptoSite()

    def test_signs_in_through_the_loopback_redirect_with_pkce(self):
        login = make_login(self.site)
        self.assertFalse(login.signed_in)

        login.sign_in()

        self.assertTrue(login.signed_in)
        self.assertEqual(login.token(), "token-1")
        self.assertEqual(self.site.token_requests, 1)
        authorize = urllib.parse.parse_qs(urllib.parse.urlsplit(self.site.authorize_urls[0]).query)
        self.assertEqual(authorize["code_challenge_method"], ["S256"])
        self.assertEqual(authorize["redirect_uri"], [login.redirect_uri])

    def test_the_client_secret_is_sent_only_when_there_is_one(self):
        strict = FakePanoptoSite(client_secret=CLIENT_SECRET)
        with self.assertRaises(PanoptoAuthError):
            make_login(strict).sign_in()  # none given, site demands one
        login = make_login(strict, secret=CLIENT_SECRET)
        login.sign_in()
        self.assertTrue(login.signed_in)

    def test_not_signed_in_is_an_auth_error_not_a_crash(self):
        with self.assertRaises(PanoptoAuthError):
            make_login(self.site).token()

    def test_sign_out_forgets_the_token(self):
        # A kiosk must never remember who was last here.
        login = make_login(self.site)
        login.sign_in()
        login.sign_out()
        self.assertFalse(login.signed_in)
        with self.assertRaises(PanoptoAuthError):
            login.token()

    def test_a_refused_login_is_reported_as_such(self):
        self.site.refuse_login = True
        with self.assertRaises(PanoptoAuthError) as caught:
            make_login(self.site).sign_in()
        self.assertIn("access_denied", str(caught.exception))

    def test_a_cancelled_login_stops_waiting(self):
        login = make_login(self.site)
        login._open_url = lambda _url: None  # the browser never comes back
        with self.assertRaises(LoginCancelled):
            login.sign_in(cancel_cb=lambda: True)

    def test_a_login_nobody_completes_times_out(self):
        login = UserLogin(
            HOST, CLIENT_ID, None, self.site,
            redirect_port=TEST_REDIRECT_PORT, open_url=lambda _url: None, timeout_s=0.3,
        )
        with self.assertRaises(LoginCancelled) as caught:
            login.sign_in()
        self.assertIn("timed out", str(caught.exception))

    def test_a_redirect_with_the_wrong_state_is_ignored(self):
        # Someone hitting the loopback with a guessed or replayed code must
        # not complete the sign-in.
        def hostile_then_honest(url: str) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            redirect = query["redirect_uri"][0]
            _hit(f"{redirect}?state=forged&code=stolen")
            self.site.open_url(url)

        login = make_login(self.site)
        login._open_url = hostile_then_honest
        login.sign_in()
        self.assertEqual(login.token(), "token-1")

    def test_an_expiry_inside_the_skew_is_not_treated_as_signed_in(self):
        self.site.token_lifetime = 30
        login = make_login(self.site)
        login.sign_in()
        self.assertFalse(login.signed_in)

    def test_the_browser_is_closed_however_the_sign_in_ends(self):
        # The window is signed into the student's Google account. It must
        # not outlive sign_in() on any path -- success, refusal, or a
        # student who walked away.
        closed: list[str] = []

        def opener_that_signs_in(url: str):
            self.site.open_url(url)
            return lambda: closed.append("success")

        login = make_login(self.site)
        login._open_url = opener_that_signs_in
        login.sign_in()
        self.assertEqual(closed, ["success"])

        self.site.refuse_login = True
        login._open_url = lambda url: (self.site.open_url(url), lambda: closed.append("refused"))[1]
        with self.assertRaises(PanoptoAuthError):
            login.sign_in()
        self.assertEqual(closed, ["success", "refused"])

        abandoned = UserLogin(
            HOST, CLIENT_ID, None, self.site,
            redirect_port=TEST_REDIRECT_PORT,
            open_url=lambda _url: (lambda: closed.append("abandoned")),
            timeout_s=0.2,
        )
        with self.assertRaises(LoginCancelled):
            abandoned.sign_in()
        self.assertEqual(closed, ["success", "refused", "abandoned"])

    def test_an_opener_with_nothing_to_close_is_fine(self):
        login = make_login(self.site)  # site.open_url returns None
        login.sign_in()
        self.assertTrue(login.signed_in)

    def test_the_port_being_taken_is_a_clear_error(self):
        import http.server

        blocker = http.server.HTTPServer(
            ("127.0.0.1", TEST_REDIRECT_PORT), http.server.BaseHTTPRequestHandler
        )
        self.addCleanup(blocker.server_close)
        with self.assertRaises(PanoptoError) as caught:
            make_login(self.site).sign_in()
        self.assertIn(str(TEST_REDIRECT_PORT), str(caught.exception))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.site = FakePanoptoSite()
        self.site.add_folder("assign-1", "Slit lamp practice")
        self.client = make_client(self.site)

    def test_reads_the_assignment_folder(self):
        self.assertEqual(self.client.folder("assign-1")["Name"], "Slit lamp practice")

    def test_a_401_drops_the_sign_in_and_asks_for_a_fresh_one(self):
        # No silent retry: the student has to sign in again, and the
        # viewer is what asks. Retrying here would hide the expiry.
        self.site.expire_token = True
        with self.assertRaises(PanoptoAuthError) as caught:
            self.client.folder("assign-1")
        self.assertIn("Sign in again", str(caught.exception))
        self.assertFalse(self.client._auth.signed_in)

    def test_a_403_says_the_folder_is_the_problem(self):
        self.site.fail_next = (403, "forbidden")
        with self.assertRaises(PanoptoAuthError) as caught:
            self.client.folder("assign-1")
        self.assertIn("folder", str(caught.exception))

    def test_other_error_statuses_are_not_retried(self):
        self.site.fail_next = (500, "boom")
        with self.assertRaises(PanoptoError) as caught:
            self.client.folder("assign-1")
        self.assertIn("500", str(caught.exception))
        self.assertEqual(sum(1 for m, p in self.site.requests if p.endswith("/assign-1")), 1)

    def test_viewer_url_carries_the_session_id(self):
        self.assertIn("id=abc-123", self.client.viewer_url("abc-123"))


class UploadFlowTest(unittest.TestCase):
    """The three calls written from Panopto's published sample. The double
    answers them the way the sample says the real thing does; only the
    probe can confirm that it really does."""

    def setUp(self):
        self.site = FakePanoptoSite()
        self.site.add_folder("assign-1", "Slit lamp practice")
        self.client = make_client(self.site)

    def test_begin_upload_carries_the_folder_and_returns_a_target(self):
        target = self.client.begin_upload("assign-1")
        self.assertEqual(target.folder_id, "assign-1")
        self.assertTrue(target.upload_id)
        self.assertIn("panopto-bucket", target.destination)
        self.assertEqual(self.site.uploads[target.upload_id]["FolderId"], "assign-1")

    def test_an_upload_target_splits_into_endpoint_bucket_and_prefix(self):
        target = self.client.begin_upload("assign-1")
        endpoint, bucket, prefix = target.s3_parts()
        self.assertEqual(endpoint, "https://s3.example.com")
        self.assertEqual(bucket, "panopto-bucket")
        self.assertEqual(prefix, target.upload_id)

    def test_a_target_that_is_not_an_s3_url_is_refused(self):
        target = UploadTarget(upload_id="u", folder_id="f", destination="nonsense")
        with self.assertRaises(PanoptoError):
            target.s3_parts()

    def test_finishing_sets_the_completed_state_and_returns_the_session_id(self):
        target = self.client.begin_upload("assign-1")
        session_id = self.client.finish_upload(target)
        self.assertEqual(self.site.uploads[target.upload_id]["State"], UPLOAD_STATE_COMPLETE)
        self.assertEqual(session_id, f"session-{target.upload_id}")

    def test_a_sessionUpload_without_a_target_is_an_error_not_a_crash(self):
        original = self.site.request

        def no_target(method, url, **kwargs):
            if url.endswith("/sessionUpload"):
                return _json_response(200, {"ID": "u1"})
            return original(method, url, **kwargs)

        self.site.request = no_target
        with self.assertRaises(PanoptoError):
            self.client.begin_upload("assign-1")

    def test_upload_state_reports_processing_progress(self):
        target = self.client.begin_upload("assign-1")
        self.assertEqual(self.client.upload_state(target.upload_id), 0)
        self.site.uploads[target.upload_id]["State"] = UPLOAD_STATE_PROCESSED
        self.assertEqual(self.client.upload_state(target.upload_id), UPLOAD_STATE_PROCESSED)

    def test_the_transfer_says_what_is_missing_when_boto3_is_absent(self):
        # boto3 is deliberately not a base requirement, so the machine
        # running these tests usually has none. The message has to name the
        # fix rather than surfacing an ImportError from inside a thread.
        target = self.client.begin_upload("assign-1")
        try:
            import boto3  # noqa: F401
        except ImportError:
            with self.assertRaises(PanoptoError) as caught:
                self.client.put_upload_file(target, __file__, "x.mp4")
            self.assertIn("boto3", str(caught.exception))
        else:
            self.skipTest("boto3 is installed here, so the missing-dependency path can't run")


if __name__ == "__main__":
    unittest.main()
