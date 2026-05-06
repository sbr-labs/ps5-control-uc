#!/usr/bin/env python3
"""Look up your PSN Account ID via Sony's OAuth flow.

Works regardless of profile privacy because you authenticate AS yourself —
no third-party API required. Same flow used by pyremoteplay, chiaki, and
ps5-mqtt under the hood.

Usage (via the Docker wrapper):
    ./get-account-id.sh

Or directly if you have pyremoteplay installed:
    python3 get-account-id.py
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from pyremoteplay.oauth import get_login_url, get_user_account
    except ImportError:
        print(
            "ERROR: pyremoteplay not installed. Run via ./get-account-id.sh "
            "(uses Docker, no Python deps needed locally) — or "
            "`pip install pyremoteplay 'pyee<12'` first.",
            file=sys.stderr,
        )
        return 1

    print()
    print("=" * 64)
    print(" Sony OAuth — PSN Account ID lookup")
    print("=" * 64)
    print()
    print("Step 1: Open this URL in your browser and sign in to PSN:")
    print()
    print(f"    {get_login_url()}")
    print()
    print("Step 2: After sign-in, the browser will redirect to a page that")
    print("        looks blank or like an error. That's expected.")
    print()
    print("Step 3: Copy the FULL URL from the browser's address bar")
    print("        (it starts with https://remoteplay.dl.playstation.net/...).")
    print()

    url = input("Paste redirect URL here: ").strip()
    if not url:
        print("ERROR: no URL provided.", file=sys.stderr)
        return 2

    try:
        info = get_user_account(url)
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: {exc}", file=sys.stderr)
        print("Most common cause: the redirect URL was incomplete or expired.", file=sys.stderr)
        return 3

    if not isinstance(info, dict):
        print(f"\nUnexpected response: {info!r}", file=sys.stderr)
        return 4

    account_id = (
        info.get("account_id")
        or info.get("account_id_base64")
        or info.get("user_id")
    )
    if not account_id:
        print(f"\nCould not extract Account ID from response: {info}", file=sys.stderr)
        return 5

    print()
    print("=" * 64)
    print(f" Your Account ID: {account_id}")
    print("=" * 64)
    print()
    print("Copy the Account ID above. install.sh will ask for it during")
    print("Remote Play pairing.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
