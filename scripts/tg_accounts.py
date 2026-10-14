"""
Account -> credentials routing shared by join_telegram.py and leave_groups.py.

Why this lives in Python and not in a workflow `a && b || c` ternary:
GitHub Actions expressions fall through to the next `||` branch when a secret
is missing/empty. With 4 accounts that means a forgotten HHNC/NAWTHON secret
would silently run with CH's credentials -- joining groups as the WRONG
account. Here a missing secret is a hard, named failure instead.

Workflows (join.yml / leave.yml) pass every account's secrets in as env vars;
this module picks the set that matches ACCOUNT.
"""

import os

# telethon.sync wraps every client method so it runs synchronously with no
# `await` and no running event loop. Plain `from telethon import
# TelegramClient` does NOT -- calls would build a coroutine and silently drop
# it (the Phase 6b root cause, see README "Live Verification Log").
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# account -> (API_ID secret, API_HASH secret, SESSION secret).
# CH and JL keep the names they were created with (already live); HHNC and
# NAWTHON follow <ACCOUNT>_API_ID / <ACCOUNT>_API_HASH / <ACCOUNT>_TG_SESSION.
ACCOUNT_SECRETS = {
    "CH": ("CH_TG_API_ID", "CH_TG_API_HASH", "CH_TG_SESSION_STRING"),
    "JL": ("JL_API_ID", "JL_API_HASH", "JL_STRING_SESSION"),
    "HHNC": ("HHNC_API_ID", "HHNC_API_HASH", "HHNC_TG_SESSION"),
    "NAWTHON": ("NAWTHON_API_ID", "NAWTHON_API_HASH", "NAWTHON_TG_SESSION"),
}


def load_credentials(account: str):
    """Returns (api_id:int, api_hash:str, session:str) or raises RuntimeError.

    Called inside each script's main() try-block on purpose, so a config
    problem is reported to the Worker (user gets a real reason) instead of
    crashing at import time and stranding the row at 'triggered'."""
    names = ACCOUNT_SECRETS.get(account)
    if names is None:
        raise RuntimeError(f"unknown_account:{account}")

    values = [os.environ.get(n, "").strip() for n in names]
    missing = [n for n, v in zip(names, values) if not v]
    if missing:
        raise RuntimeError("missing_secrets:" + ",".join(missing))

    api_id, api_hash, session = values
    try:
        return int(api_id), api_hash, session
    except ValueError:
        raise RuntimeError(f"bad_api_id:{names[0]}_is_not_a_number")


def make_client(account: str):
    api_id, api_hash, session = load_credentials(account)
    return TelegramClient(StringSession(session), api_id, api_hash)
