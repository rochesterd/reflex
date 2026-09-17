"""Tests for panopto_api against an in-memory Panopto, plus the double
itself, which test_panopto_upload.py imports.

**What these tests do and don't prove.** The double answers the same
provisional paths panopto_api.py sends, so it cannot tell us those paths
are right -- both sides hold the same guess, and only a real site settles
it (PANOPTO_PLAN.md's "Assumptions to verify"). What it does prove is the
client's own behaviour, which is where the bugs that would cost a student
a recording live: token caching, refreshing exactly once on a 401,
exact-name matching so a recording can't be filed in a classmate's folder,
and turning an error status into something a kiosk can say out loud.
"""

from __future__ import annotations

import base64
import json
import unittest
import urllib.parse

from panopto_api import (
    API_ROOT,
    ROLE_VIEWER,
    HttpResponse,
    PanoptoAuth,
    PanoptoAuthError,
    PanoptoClient,
    PanoptoError,
    PanoptoNotVerified,
    UploadTarget,
)

HOST = "neco.example.panopto.com"
CLIENT_ID = "reflex-kiosk"
CLIENT_SECRET = "s3cret"


class FakePanoptoSite:
    """An in-memory Panopto behind the Transport seam.

    Holds folders, users and folder permissions, and records every request
    so a test can assert on what was sent rather than only on what came
    back. `fail_next` and `expire_token` force the failure paths that are
    otherwise unreachable without unplugging a real network.
    """

    def __init__(self, client_id: str = CLIENT_ID, client_secret: str = CLIENT_SECRET):
        self._client_id = client_id
        self._client_secret = client_secret
        self.folders: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.access: dict[str, dict[str, str]] = {}
        self.uploads: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.token_requests = 0
        self.issued_token = "token-1"
        self.token_lifetime = 3600
        self.fail_next: tuple[int, str] | None = None
        self.expire_token = False

    # -- seeding ---------------------------------------------------------

    def add_folder(self, folder_id: str, name: str, parent: str | None = None) -> dict:
        folder = {"Id": folder_id, "Name": name, "Parent": parent}
        self.folders[folder_id] = folder
        return folder

    def add_user(self, user_id: str, username: str) -> dict:
        user = {"Id": user_id, "Username": username}
        self.users[user_id] = user
        return user

    # -- transport -------------------------------------------------------

    def request(self, method, url, *, headers=None, body=None, timeout=30.0) -> HttpResponse:
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        self.requests.append((method, path))

        if path.endswith("/oauth2/connect/token"):
            return self._token(headers or {})

        if self.fail_next is not None:
            status, message = self.fail_next
            self.fail_next = None
            return _json_response(status, {"Message": message})

        auth = (headers or {}).get("Authorization", "")
        if self.expire_token or auth != f"Bearer {self.issued_token}":
            return _json_response(401, {"Message": "expired"})

        if not path.startswith(API_ROOT):
            return _json_response(404, {"Message": "no such path"})
        rest = path[len(API_ROOT):]
        payload = json.loads(body.decode("utf-8")) if body else None
        return self._api(method, rest, query, payload)

    def _token(self, headers: dict) -> HttpResponse:
        self.token_requests += 1
        expected = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode("utf-8")
        ).decode("ascii")
        if headers.get("Authorization") != f"Basic {expected}":
            return _json_response(400, {"error": "invalid_client"})
        # A fresh token ends whatever made the old one stale.
        self.expire_token = False
        return _json_response(
            200, {"access_token": self.issued_token, "expires_in": self.token_lifetime}
        )

    def _api(self, method: str, path: str, query: dict, payload: dict | None) -> HttpResponse:
        if path == "/folders" and method == "POST":
            folder_id = f"folder-{len(self.folders) + 1}"
            return _json_response(
                200, self.add_folder(folder_id, payload["Name"], payload["Parent"])
            )

        if path == "/folders/search" and method == "GET":
            needle = query.get("searchQuery", [""])[0].lower()
            hits = [f for f in self.folders.values() if needle in f["Name"].lower()]
            return _json_response(200, {"Results": hits})

        if path == "/users/search" and method == "GET":
            needle = query.get("searchQuery", [""])[0].lower()
            hits = [u for u in self.users.values() if needle in u["Username"].lower()]
            return _json_response(200, {"Results": hits})

        if path.startswith("/folders/"):
            rest = path[len("/folders/"):]
            if "/access/" in rest:
                folder_id, user_id = rest.split("/access/", 1)
                folder_id = urllib.parse.unquote(folder_id)
                user_id = urllib.parse.unquote(user_id)
                if folder_id not in self.folders:
                    return _json_response(404, {"Message": "no such folder"})
                grants = self.access.setdefault(folder_id, {})
                if method == "PUT":
                    grants[user_id] = query.get("role", [ROLE_VIEWER])[0]
                    return _json_response(200, {})
                if method == "DELETE":
                    grants.pop(user_id, None)
                    return _json_response(200, {})
            if rest.endswith("/access") and method == "GET":
                folder_id = urllib.parse.unquote(rest[: -len("/access")])
                return _json_response(200, {"Grants": self.access.get(folder_id, {})})
            if method == "GET":
                folder = self.folders.get(urllib.parse.unquote(rest))
                if folder is None:
                    return _json_response(404, {"Message": "no such folder"})
                return _json_response(200, folder)

        return _json_response(404, {"Message": f"unhandled {method} {path}"})


def _json_response(status: int, payload: dict) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


def make_client(site: FakePanoptoSite) -> PanoptoClient:
    auth = PanoptoAuth(HOST, CLIENT_ID, CLIENT_SECRET, site)
    return PanoptoClient(HOST, auth, site)


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.site = FakePanoptoSite()

    def test_token_is_fetched_once_and_reused(self):
        auth = PanoptoAuth(HOST, CLIENT_ID, CLIENT_SECRET, self.site)
        self.assertEqual(auth.token(), "token-1")
        self.assertEqual(auth.token(), "token-1")
        self.assertEqual(self.site.token_requests, 1)

    def test_expiry_is_shortened_so_a_token_cannot_die_in_flight(self):
        # A 30s lifetime is entirely inside the 60s skew: it must never be
        # treated as usable, or a call goes out with a token already dead.
        self.site.token_lifetime = 30
        auth = PanoptoAuth(HOST, CLIENT_ID, CLIENT_SECRET, self.site)
        auth.token()
        auth.token()
        self.assertEqual(self.site.token_requests, 2)

    def test_bad_credentials_raise_something_a_technician_can_act_on(self):
        auth = PanoptoAuth(HOST, CLIENT_ID, "wrong", self.site)
        with self.assertRaises(PanoptoAuthError) as caught:
            auth.token()
        self.assertIn("config.json", str(caught.exception))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.site = FakePanoptoSite()
        self.site.add_folder("parent", "Practice Recordings")
        self.client = make_client(self.site)

    def test_expired_token_is_refreshed_once_and_the_call_succeeds(self):
        self.client.folder("parent")  # prime the token
        self.site.expire_token = True
        self.site.issued_token = "token-2"

        self.assertEqual(self.client.folder("parent")["Name"], "Practice Recordings")
        self.assertEqual(self.site.token_requests, 2)

    def test_a_401_that_survives_the_refresh_is_an_auth_error(self):
        def always_401(method, url, *, headers=None, body=None, timeout=30.0):
            if url.endswith("/oauth2/connect/token"):
                return _json_response(200, {"access_token": "t", "expires_in": 3600})
            return _json_response(401, {"Message": "nope"})

        self.site.request = always_401
        with self.assertRaises(PanoptoAuthError):
            self.client.folder("parent")

    def test_other_error_statuses_are_not_retried(self):
        self.site.fail_next = (500, "boom")
        with self.assertRaises(PanoptoError) as caught:
            self.client.folder("parent")
        self.assertIn("500", str(caught.exception))

    def test_find_folder_matches_the_whole_name_not_a_prefix(self):
        # The filing mistake that matters: "Jo Smith" must never resolve to
        # "Jo Smithson", because that puts a recording of two students in a
        # stranger's folder.
        self.site.add_folder("f1", "Jo Smithson", parent="parent")
        self.assertIsNone(self.client.find_folder("parent", "Jo Smith"))

        self.site.add_folder("f2", "Jo Smith", parent="parent")
        self.assertEqual(self.client.find_folder("parent", "Jo Smith")["Id"], "f2")

    def test_find_folder_ignores_a_same_named_folder_under_another_parent(self):
        self.site.add_folder("elsewhere", "Jo Smith", parent="some-other-parent")
        self.assertIsNone(self.client.find_folder("parent", "Jo Smith"))

    def test_ensure_folder_creates_once_then_finds(self):
        created = self.client.ensure_folder("parent", "Jo Smith")
        again = self.client.ensure_folder("parent", "Jo Smith")
        self.assertEqual(created["Id"], again["Id"])
        self.assertEqual(sum(1 for f in self.site.folders.values() if f["Name"] == "Jo Smith"), 1)

    def test_grant_and_revoke_folder_access(self):
        folder = self.client.ensure_folder("parent", "Jo Smith")
        self.client.grant_folder_access(folder["Id"], "user-1", ROLE_VIEWER)
        self.assertEqual(self.site.access[folder["Id"]], {"user-1": ROLE_VIEWER})

        self.client.revoke_folder_access(folder["Id"], "user-1", ROLE_VIEWER)
        self.assertEqual(self.site.access[folder["Id"]], {})

    def test_find_user_is_exact(self):
        self.site.add_user("u1", "jsmithson@neco.edu")
        self.assertIsNone(self.client.find_user("jsmith@neco.edu"))
        self.site.add_user("u2", "jsmith@neco.edu")
        self.assertEqual(self.client.find_user("jsmith@neco.edu")["Id"], "u2")

    def test_viewer_url_carries_the_session_id(self):
        self.assertIn("id=abc-123", self.client.viewer_url("abc-123"))


class UnverifiedSeamTest(unittest.TestCase):
    """The upload calls must fail loudly until they are written against the
    real API -- a silently-half-built uploader is the black pane again."""

    def setUp(self):
        self.client = make_client(FakePanoptoSite())

    def test_begin_upload_refuses_to_guess(self):
        with self.assertRaises(PanoptoNotVerified):
            self.client.begin_upload("parent")

    def test_transfer_and_finish_refuse_to_guess(self):
        target = UploadTarget(upload_id="u", folder_id="parent", destination="nowhere")
        with self.assertRaises(PanoptoNotVerified):
            self.client.put_upload_file(target, __file__, "x.mp4")
        with self.assertRaises(PanoptoNotVerified):
            self.client.finish_upload(target)


if __name__ == "__main__":
    unittest.main()
