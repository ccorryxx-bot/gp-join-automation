"""
Phase 8 (2026-09-23): "leave muted groups" automation, invoked by
.github/workflows/leave.yml. Two modes, selected by MODE:

  MODE=scan  - read-only. Iterates every dialog for this account, finds
               groups/supergroups where Telegram shows this account as
               admin-restricted (banned_rights.send_messages=True but
               view_messages=False -- i.e. "muted", not fully kicked/banned),
               and reports the full candidate list back to the Worker in a
               single POST to /leave-report (mode=scan). The Worker then
               asks the admin a Y/N confirm in Telegram before anything
               actually leaves.

  MODE=leave - destructive. Fetches the admin-confirmed candidate list for
               SCAN_ID from the Worker's /leave-candidates endpoint, then
               actually leaves each one with a short randomized delay
               between calls, and reports the full per-group result list
               back in a single POST to /leave-report (mode=leave).

Env vars (required): TG_API_ID, TG_API_HASH, TG_SESSION_STRING, ACCOUNT,
MODE, SCAN_ID -- see leave.yml for where these come from.

Env vars (optional): REPORT_URL, REPORT_SECRET -- same as join_telegram.py.
If REPORT_URL is unset, the report step is skipped (useful for a local dry
run of scan mode only); MODE=leave still requires REPORT_URL since it also
needs it to fetch the candidate list.

Exit code: 0 only if the mode's Worker round-trip actually succeeded (report
for both modes, plus the initial candidate fetch for leave mode) -- same
"don't go green on a silent failure" rule as join_telegram.py. A per-group
leave failure does NOT fail the run by itself -- with dozens of groups in
one batch, some individual failures are a normal outcome the admin is
already told about in the finish notice, not a CI-red event.
"""

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

from telethon.sync import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError, RPCError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantBanned

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"]
ACCOUNT = os.environ.get("ACCOUNT", "CH")
MODE = os.environ["MODE"]  # "scan" | "leave"
SCAN_ID = os.environ["SCAN_ID"]
REPORT_URL = os.environ.get("REPORT_URL", "").rstrip("/")
REPORT_SECRET = os.environ.get("REPORT_SECRET", "")

# Read-only per-group check during scan -- lighter on Telegram's abuse
# system than a "peer action" like join/leave, so a short flat delay is
# enough to avoid bursting GetParticipantRequest.
SCAN_PER_GROUP_DELAY_SECONDS = (1, 2)

# Leaving IS a peer action Telegram's abuse system watches, but leaving a
# group you're already restricted in is a much weaker signal than joining a
# *new* one -- shorter pacing than join's 1-3min is a deliberate tradeoff to
# fit a whole batch inside the ~10-15min budget this was sized against.
# Tune down further only after watching a few real runs.
LEAVE_PER_GROUP_DELAY_SECONDS = (3, 8)

MAX_FLOODWAIT_AUTO_RETRY_SECONDS = 120

# Same Cloudflare Browser Integrity Check workaround as join_telegram.py --
# see that file's comment for the full root-cause writeup.
REPORT_HEADERS_BASE = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
}


def report(payload: dict) -> bool:
    """Returns True only if the Worker actually received this report."""
    print(f"[report] mode={payload.get('mode')} scan_id={SCAN_ID} account={ACCOUNT}")
    if not REPORT_URL:
        return True  # nothing configured to deliver to -- not a failure

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{REPORT_URL}/leave-report",
            data=data,
            headers={**REPORT_HEADERS_BASE, "X-Report-Secret": REPORT_SECRET},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"[report] callback returned {e.code}: {body}")
        print(f"::error title=Leave report failed::HTTP {e.code} for scan_id={SCAN_ID}: {body[:300]}")
        return False
    except Exception as e:
        print(f"[report] callback failed: {e}")
        print(f"::error title=Leave report failed::{type(e).__name__} for scan_id={SCAN_ID}: {e}")
        return False


def fetch_candidates() -> list:
    """MODE=leave only. Pulls the admin-confirmed candidate list back from
    the Worker rather than taking it as a workflow input, so an arbitrarily
    long muted-group list never has to fit in a GitHub Actions input."""
    req = urllib.request.Request(
        f"{REPORT_URL}/leave-candidates?scan_id={SCAN_ID}",
        headers={**REPORT_HEADERS_BASE, "X-Report-Secret": REPORT_SECRET},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("candidates", [])


def scan_muted_groups(client) -> tuple:
    """Returns (total_dialogs, candidates)."""
    me = client.get_me()
    total = 0
    candidates = []

    for dialog in client.iter_dialogs():
        total += 1
        # Basic (legacy) groups don't expose per-user banned_rights the same
        # way channels/supergroups do -- dialog.is_channel is Telethon's
        # flag for "this is a Channel-type entity" and covers supergroups
        # too (megagroup=True), which is exactly what admin-restriction
        # applies to.
        if not dialog.is_channel:
            continue
        try:
            participant = client(GetParticipantRequest(dialog.entity, me.id)).participant
        except FloodWaitError as e:
            if e.seconds > MAX_FLOODWAIT_AUTO_RETRY_SECONDS:
                print(f"[scan] FloodWait {e.seconds}s too long, skipping {dialog.id}")
                continue
            print(f"[scan] FloodWait {e.seconds}s -- sleeping")
            time.sleep(e.seconds + 1)
            continue
        except RPCError as e:
            print(f"[scan] can't read participant status for {dialog.id}: {type(e).__name__}")
            continue

        if isinstance(participant, ChannelParticipantBanned):
            rights = participant.banned_rights
            # send_messages=True + view_messages=False is Telegram's shape
            # for "restricted/read-only" (muted). view_messages=True means
            # fully banned/kicked -- a different case, not handled here,
            # since the account isn't meaningfully "in" that group anymore.
            if rights and rights.send_messages and not rights.view_messages:
                candidates.append({
                    "peer_id": str(dialog.id),
                    "peer_type": "channel",
                    "title": dialog.title or "",
                })

        time.sleep(random.uniform(*SCAN_PER_GROUP_DELAY_SECONDS))

    return total, candidates


def leave_confirmed_groups(client, candidates: list) -> tuple:
    """Returns (results, aborted_early)."""
    # get_entity(int_id) alone can fail to resolve a channel in a *fresh*
    # session ("Could not find the input entity") because Telethon needs an
    # access_hash it only learns by having seen the entity via a dialogs
    # call first. iter_dialogs() here warms that cache for every group this
    # account is currently in, which covers all of `candidates` since they
    # were all found via a scan on this same account.
    list(client.iter_dialogs())

    results = []
    aborted_early = False

    for c in candidates:
        peer_id = c["peer_id"]
        try:
            entity = client.get_entity(int(peer_id))
            # delete_dialog picks the right underlying leave/delete call for
            # whatever entity type this is (Channel vs legacy Chat) -- no
            # need to hand-pick LeaveChannelRequest vs DeleteChatUserRequest.
            client.delete_dialog(entity)
            results.append({"peer_id": peer_id, "status": "left", "detail": ""})
            print(f"[leave] left {peer_id} ({c.get('title', '')})")
        except FloodWaitError as e:
            if e.seconds > MAX_FLOODWAIT_AUTO_RETRY_SECONDS:
                results.append({"peer_id": peer_id, "status": "failed",
                                 "detail": f"floodwait_{e.seconds}s_exceeded_budget"})
                continue
            print(f"[leave] FloodWait {e.seconds}s -- sleeping")
            time.sleep(e.seconds + 1)
            try:
                entity = client.get_entity(int(peer_id))
                client.delete_dialog(entity)
                results.append({"peer_id": peer_id, "status": "left", "detail": ""})
            except Exception as e2:
                results.append({"peer_id": peer_id, "status": "failed",
                                 "detail": f"unexpected_error:{type(e2).__name__}"})
        except PeerFloodError:
            # Same reasoning as join_telegram.py's PeerFloodError handling --
            # an account-wide anti-spam signal, not a per-group problem. Stop
            # leaving MORE groups this run; whatever's left stays 'pending'
            # and gets picked up by the next 3-day scan cycle.
            results.append({"peer_id": peer_id, "status": "failed", "detail": "peer_flood_detected"})
            aborted_early = True
            break
        except RPCError as e:
            results.append({"peer_id": peer_id, "status": "failed",
                             "detail": f"rpc_error_{type(e).__name__}"})
        except Exception as e:
            results.append({"peer_id": peer_id, "status": "failed",
                             "detail": f"unexpected_error:{type(e).__name__}:{str(e)[:150]}"})

        time.sleep(random.uniform(*LEAVE_PER_GROUP_DELAY_SECONDS))

    return results, aborted_early


def main():
    print(f"scan_id={SCAN_ID} account={ACCOUNT} mode={MODE}")

    if MODE == "scan":
        try:
            with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
                total, candidates = scan_muted_groups(client)
        except Exception as e:
            print(f"[unexpected] scan failed: {type(e).__name__}: {e}")
            report({"mode": "scan", "account": ACCOUNT, "scan_id": SCAN_ID,
                     "total_dialogs": 0, "candidates": []})
            sys.exit(1)

        if not report({"mode": "scan", "account": ACCOUNT, "scan_id": SCAN_ID,
                        "total_dialogs": total, "candidates": candidates}):
            sys.exit(1)
        return

    if MODE == "leave":
        try:
            candidates = fetch_candidates()
        except Exception as e:
            print(f"[fetch_candidates] failed: {type(e).__name__}: {e}")
            sys.exit(1)

        if not candidates:
            print("[leave] no pending candidates -- nothing to do")
            report({"mode": "leave", "account": ACCOUNT, "scan_id": SCAN_ID,
                     "results": [], "aborted_early": False})
            return

        try:
            with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
                results, aborted_early = leave_confirmed_groups(client, candidates)
        except Exception as e:
            print(f"[unexpected] leave failed: {type(e).__name__}: {e}")
            report({"mode": "leave", "account": ACCOUNT, "scan_id": SCAN_ID,
                     "results": [], "aborted_early": True})
            sys.exit(1)

        if not report({"mode": "leave", "account": ACCOUNT, "scan_id": SCAN_ID,
                        "results": results, "aborted_early": aborted_early}):
            sys.exit(1)
        return

    print(f"::error::Unknown MODE={MODE}")
    sys.exit(1)


if __name__ == "__main__":
    main()
