#!/usr/bin/env python3
"""Manual smoke test for psn_presence.py — run this BEFORE wiring into the
daemon to confirm the auth flow + presence query work for the user's account.

Usage:
    # First-time:  paste your npsso to bootstrap tokens
    NPSSO="<paste>" ACCOUNT_ID="<your-base64-id>" python3 test_psn_presence.py

    # Subsequent runs: just point at the saved tokens
    ACCOUNT_ID="<your-base64-id>" python3 test_psn_presence.py

What it does:
    1. Loads tokens from /tmp/psn_tokens.json (or bootstraps from $NPSSO).
    2. Calls fetch_presence() once and prints what comes back.
    3. Force-expires the access token, calls fetch_presence() again to
       confirm refresh works.

What success looks like:
    - First call returns {app_name: "Call of Duty: ...", app_id: "PPSA...",
      image_url: "https://image.api.playstation.com/..."} when a PS5 game
      is running.
    - Or {} when nothing is running on the account.
    - Refresh test prints "refreshed" and second call still works.

What failure looks like:
    - "npsso → code exchange failed" → npsso is wrong/expired
    - "token exchange failed" → client_id/secret broken (Sony changed it)
    - {} when a game IS running → presence endpoint shape changed; needs investigation
"""

import asyncio
import json
import os
import sys
import time

# Allow running this from the repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psn_presence import PsnPresence  # noqa: E402

TOKEN_PATH = "/tmp/psn_tokens.json"


async def main() -> None:
    # ACCOUNT_ID is now optional — module auto-resolves the decimal account ID
    # via Sony's /me endpoint after auth, so the user doesn't have to type it.
    account_id = os.environ.get("ACCOUNT_ID") or None
    npsso = os.environ.get("NPSSO")

    psn = PsnPresence(token_path=TOKEN_PATH, account_id=account_id)
    ok = await psn.start(npsso=npsso)
    if not ok:
        print("ERROR: psn.start() failed — see log above. If first run, set NPSSO env.")
        sys.exit(1)

    print("\n=== first presence fetch ===")
    p = await psn.fetch_presence()
    print(json.dumps(p, indent=2))

    print("\n=== forcing access-token expiry to test refresh path ===")
    with open(TOKEN_PATH, "r") as f:
        toks = json.load(f)
    toks["access_expires_at"] = time.time() - 1
    with open(TOKEN_PATH, "w") as f:
        json.dump(toks, f)
    # Need to reload into the existing instance — easiest: new instance
    psn2 = PsnPresence(token_path=TOKEN_PATH, account_id=account_id)
    await psn2.start()
    p2 = await psn2.fetch_presence()
    print(json.dumps(p2, indent=2))

    print("\n=== success — module works end-to-end ===")


if __name__ == "__main__":
    asyncio.run(main())
