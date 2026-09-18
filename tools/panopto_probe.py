"""The first thing to run against a real Panopto site, once IT has issued
the API client. Walks the whole path the kiosk will take -- sign in, read
the assignment folder, upload a short synthetic session, wait for Panopto
to process it -- and prints every raw response, so each provisional
assumption in panopto_api.py is confirmed or contradicted in one sitting.

    python tools/panopto_probe.py                      # uses config.json
    python tools/panopto_probe.py --no-upload          # sign-in and folder only
    python tools/panopto_probe.py --config path.json

Steps, in the order they can fail:

  1. Sign in.        Confirms the OAuth endpoints, the redirect URI IT
                     registered, and whether PKCE alone is accepted or the
                     client secret is required.
  2. Read folder.    Confirms /Panopto/api/v1/folders/{id} and that this
                     account can see the assignment folder.
  3. Upload.         Confirms sessionUpload, the S3 target (needs boto3),
                     the UCS manifest, and completion.
  4. Wait.           Polls State until Panopto has processed the session,
                     then prints the viewer URL. Open it and check that
                     both streams play and line up.

Uploads a real (synthetic, two-second) session into the real folder. Run
it as yourself, and delete the test session afterwards from Panopto.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from panopto_api import (
    UPLOAD_STATE_PROCESSED,
    LoginCancelled,
    PanoptoAuthError,
    PanoptoClient,
    PanoptoError,
    UserLogin,
)
from panopto_upload import build_manifest, build_ucs_manifest, upload_session
from recorder import Recorder
from session_reader import Session
from synthetic_camera import SyntheticCamera

POLL_S = 5.0
MAX_WAIT_S = 600.0


def step(number: int, title: str) -> None:
    print(f"\n[{number}] {title}")
    print("-" * (len(title) + 6))


def record_probe_session(root: Path) -> Session:
    instrument = SyntheticCamera(320, 240, name="instrument", fps=30)
    third = SyntheticCamera(160, 120, name="third", fps=30)
    instrument.start()
    third.start()
    try:
        recorder = Recorder(
            instrument, third,
            instrument_key="slit_lamp", instrument_label="Probe (synthetic)",
            third_person_label="third-person camera",
            output_root=str(root), fps=30, preset="ultrafast",
        )
        recorder.start()
        time.sleep(2.0)
        recorder.stop()
    finally:
        instrument.stop()
        third.stop()
    return Session.load(recorder.session_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--no-upload", action="store_true", help="stop after reading the folder")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config: {exc}")
        return 2
    if cfg.panopto is None:
        print(f"{args.config} has no 'panopto' section. Enter the integration in Settings first.")
        return 2
    panopto = cfg.panopto
    print(f"site: {panopto.host}   client: {panopto.client_id}   folder: {panopto.assignment_folder_id}")
    print(f"secret: {'stored' if panopto.client_secret else 'none'}   redirect port: {panopto.redirect_port}")

    login = UserLogin(
        panopto.host, panopto.client_id, panopto.client_secret, redirect_port=panopto.redirect_port
    )
    client = PanoptoClient(panopto.host, login)

    step(1, "Sign in (a private browser window will open)")
    try:
        login.sign_in()
    except LoginCancelled as exc:
        print(f"not completed: {exc}")
        return 1
    except PanoptoAuthError as exc:
        print(f"REFUSED: {exc}")
        print("If PKCE-only was tried, enter the client secret in Settings and rerun.")
        return 1
    print("signed in; token held in memory")

    try:
        step(2, "Read the assignment folder")
        folder = client.folder(panopto.assignment_folder_id)
        print(json.dumps(folder, indent=2)[:2000])

        if args.no_upload:
            print("\n--no-upload: stopping here.")
            return 0

        step(3, "Upload a two-second synthetic session")
        with tempfile.TemporaryDirectory() as work:
            session = record_probe_session(Path(work))
            manifest = build_manifest(session)
            print("manifest that will be sent:")
            print(build_ucs_manifest(manifest))
            url = upload_session(
                session,
                client,
                panopto.assignment_folder_id,
                progress_cb=lambda done, total: print(f"\r  {done}/{total} bytes", end=""),
            )
            print()
        print(f"upload accepted; viewer URL: {url}")

        step(4, "Wait for Panopto to process it")
        upload_id = url.rsplit("=", 1)[-1]
        deadline = time.monotonic() + MAX_WAIT_S
        state = -1
        while time.monotonic() < deadline:
            state = client.upload_state(upload_id)
            print(f"  state {state}")
            if state == UPLOAD_STATE_PROCESSED:
                break
            time.sleep(POLL_S)
        if state != UPLOAD_STATE_PROCESSED:
            print(f"still processing after {MAX_WAIT_S:.0f}s -- check Panopto later")
            return 1
        print(f"\nprocessed. Open and check both streams line up:\n  {url}")
        print("Then delete this test session from Panopto.")
        return 0
    except PanoptoError as exc:
        print(f"\nFAILED: {exc}")
        return 1
    finally:
        login.sign_out()
        print("\nsigned out")


if __name__ == "__main__":
    sys.exit(main())
