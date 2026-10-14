"""
Phase 8 (2026-09-23): "leave muted groups" automation, invoked by
.github/workflows/leave.yml. Two modes, selected by MODE:

  MODE=scan  - read-only. Iterates every dialog for this account and flags
               two kinds of candidates: groups/supergroups where Telegram
               shows this account as admin-restricted (banned_rights.
               send_messages=True but view_messages=False -- i.e. "muted",
               not fully kicked/banned), and (Phase 9, 2026-09-26) any
               group/channel with fewer than MIN_MEMBERS_THRESHOLD total
               members. Reports the full candidate list (each tagged with
               its reason) back to the Worker in a single POST to
               /leave-report (mode=scan). The Worker then asks the admin a
               single Y/N confirm in Telegram covering BOTH reasons at once
               before anything actually leaves.

  MODE=leave - destructive. Fetches the admin-confirmed candidate list for
               SCAN_ID from the Worker's /leave-candidates endpoint, then
               actually leaves each one with a short randomized delay
               between calls, and reports the full per-group result list
               back in a single POST to /leave-report (mode=leave).

  MODE=status - read-only. Phase 11 (2026-09-28), invoked by the admin's
               /account_status bot command. Same dialog walk as MODE=scan's
               muted/read_only/paid_messages checks, but every group lands
               in exactly one bucket (total / active / restricted /
               paid_messages / read_only / broadcast / unchecked) instead of building a leave
               candidate list, and the member-count fallback is skipped
               entirely -- it's the one check in MODE=scan that can cost an
               extra get_participants call per group, and /account_status
               has no use for that number. Reports the counts back in
               a single POST to /leave-report (mode=status). SCAN_ID is
               unused here (no leave_scans row exists for a status run --
               nothing to confirm or leave later) but still required/sent
               for a consistent report payload shape across modes.

Env vars (required): ACCOUNT (credentials resolved by tg_accounts.py),
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
from datetime import datetime, timezone

from tg_accounts import make_client
from telethon.errors import FloodWaitError, PeerFloodError, RPCError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import (
    Channel,
    ChannelParticipantAdmin,
    ChannelParticipantBanned,
    ChannelParticipantCreator,
    ChannelParticipantLeft,
    Chat,
    ChatForbidden,
)

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

# Phase 9 (2026-09-26): scan now also flags groups the account is a member
# of with fewer than this many total members, in addition to muted
# (admin-restricted) groups. Overridable via env var, same convention as
# PEER_FLOOD_PAUSE_HOURS on the Worker side, without needing a redeploy.
MIN_MEMBERS_THRESHOLD = int(os.environ.get("MIN_MEMBERS_THRESHOLD", "50"))

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


# ---------------------------------------------------------------------------
# Classification -- ONE implementation shared by status, scan and the
# leave-time re-check, so the three can never drift apart (before 2026-10-15
# status and scan each carried their own copy of this logic).
#
# Every rule below is tied to a source, not a guess:
#   * Administrators/creators are NOT affected by a group's default
#     restrictions; plain members AND restricted users are
#     (tdlib DialogParticipantStatus::apply_restrictions).
#   * Telethon's iter_dialogs() returns migrated/deactivated legacy chats
#     unless told not to (ignore_migrated defaults to False) and
#     delete_dialog() itself special-cases deactivated chats.
#   * Telethon's TL date reader returns 1970-01-01 (not None) for
#     until_date=0, i.e. "forever".
#   * get_participants(limit=0) is GetFullChannel -- Telethon's own comment:
#     "May not have access to the channel, but getFull can get the .total".
#     limit>=1 is channels.getParticipants, which needs rights a normal
#     member of a broadcast channel doesn't have.
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def is_live_group(entity):
    """(True, "") if this dialog is a group/channel the account is actually IN
    right now. Otherwise (False, why) -- such dialogs are neither counted nor
    ever offered for leaving."""
    if isinstance(entity, ChatForbidden):
        return False, "forbidden"
    if isinstance(entity, Chat):
        if getattr(entity, "deactivated", False) or getattr(entity, "migrated_to", None) is not None:
            return False, "migrated_or_deactivated"
        if getattr(entity, "left", False):
            return False, "left"
    if isinstance(entity, Channel) and getattr(entity, "left", False):
        return False, "left"
    return True, ""


def is_admin(entity):
    return bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None))


def mute_state(rights, now):
    """(muted_now, until). Muted = can't send text but can still view.
    until=None means permanent. A restriction whose until_date has passed is
    expired (tdlib treats it the same way) and is NOT muted."""
    if not (rights and rights.send_messages and not rights.view_messages):
        return False, None
    until = getattr(rights, "until_date", None)
    if until is None or until.timestamp() < 30:  # epoch / <30s == "forever"
        return True, None
    if until <= now:
        return False, None
    return True, until


def _with_floodwait_retry(fn, tag):
    """Run fn(); on a short FloodWait sleep and retry ONCE (the old code just
    skipped the group). Anything else propagates to the caller."""
    try:
        return fn()
    except FloodWaitError as e:
        if e.seconds > MAX_FLOODWAIT_AUTO_RETRY_SECONDS:
            print(f"[{tag}] FloodWait {e.seconds}s too long -- giving up on this call")
            raise
        print(f"[{tag}] FloodWait {e.seconds}s -- sleeping, then retrying once")
        time.sleep(e.seconds + 1)
        return fn()


def get_member_count(client, entity):
    """Total member count as Telegram reports it (includes bots; Telegram
    gives no cheap human-only number). Legacy chats always carry it; channels
    often do, otherwise GetFullChannel (limit=0) -- works without admin rights."""
    count = getattr(entity, "participants_count", None)
    if count is not None:
        return count
    if not isinstance(entity, Channel):
        return None
    try:
        return _with_floodwait_retry(lambda: client.get_participants(entity, limit=0).total, "count")
    except RPCError as e:
        print(f"[count] can't read member count for {entity.id}: {type(e).__name__}")
        return None


def evaluate(client, entity, me_id, now):
    """Classify one live group/channel for this account.
    Returns {"bucket", "admin", ...} with bucket one of:
      muted | read_only | paid_messages | broadcast | active | unchecked
    "unchecked" = Telegram wouldn't tell us (error in "error"); it is counted
    as such instead of silently vanishing from the totals."""
    info = {"bucket": "active", "admin": False}
    if is_admin(entity):
        # Admins/creators can post regardless of default restrictions and are
        # never restricted per-user, so no GetParticipant call is needed.
        info["admin"] = True
        return info

    is_channel = isinstance(entity, Channel)
    if is_channel:
        try:
            participant = _with_floodwait_retry(
                lambda: client(GetParticipantRequest(entity, me_id)).participant, "participant")
        except RPCError as e:
            return {"bucket": "unchecked", "admin": False, "error": type(e).__name__}
        if isinstance(participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
            # Telegram itself says admin/creator -- trust that over a possibly
            # stale/min entity that lacked the admin_rights flag.
            info["admin"] = True
            return info
        if isinstance(participant, ChannelParticipantLeft):
            return {"bucket": "unchecked", "admin": False, "error": "not_a_participant"}
        if isinstance(participant, ChannelParticipantBanned):
            rights = participant.banned_rights
            if rights and rights.view_messages:
                # Fully banned/kicked -- not really "in" the group any more.
                return {"bucket": "unchecked", "admin": False, "error": "banned"}
            muted, until = mute_state(rights, now)
            if muted:
                info.update(bucket="muted", muted_until=until.isoformat() if until else None)
                return info

    default_rights = getattr(entity, "default_banned_rights", None)
    stars = getattr(entity, "send_paid_messages_stars", None) if is_channel else None
    if default_rights and default_rights.send_messages:
        info["bucket"] = "read_only"      # group-wide: nobody but admins can send
    elif stars:
        info.update(bucket="paid_messages", stars_price=stars)
    elif is_channel and (getattr(entity, "broadcast", False) or getattr(entity, "gigagroup", False)):
        info["bucket"] = "broadcast"      # only admins post; members just read
    return info


def leave_reason(client, entity, info):
    """Leave reason for an evaluated group, or None. Admin/creator groups are
    NEVER leave candidates, and an unreadable group ("unchecked") can't be
    judged so it isn't one either."""
    if info["admin"]:
        return None, {}
    bucket = info["bucket"]
    if bucket == "muted":
        return "muted", ({"muted_until": info["muted_until"]} if info.get("muted_until") else {})
    if bucket == "read_only":
        return "read_only", {}
    if bucket == "paid_messages":
        return "paid_messages", {"stars_price": info["stars_price"]}
    if bucket in ("active", "broadcast"):
        count = get_member_count(client, entity)
        if count is not None and count < MIN_MEMBERS_THRESHOLD:
            return "under_50_members", {"member_count": count}
    return None, {}


def _public_url(entity):
    username = getattr(entity, "username", None) if isinstance(entity, Channel) else None
    return f"https://t.me/{username}" if username else None


def scan_leave_candidates(client) -> tuple:
    """Returns (total_groups, candidates). total_groups counts only groups and
    channels the account is actually in (no private chats/bots/Saved
    Messages, no migrated/deactivated legacy chats, no groups it was removed
    from). A group is reported under at most ONE reason, priority:
      muted > read_only > paid_messages > under_50_members
    Admin/creator groups are never candidates. Candidates carry "url" (public
    groups only) and, per reason, member_count / stars_price / muted_until
    (set only for a temporary mute)."""
    me = client.get_me()
    now = _now()
    total = 0
    unchecked = 0
    skipped = {}
    candidates = []

    for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        live, why = is_live_group(entity)
        if not live:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        total += 1

        info = evaluate(client, entity, me.id, now)
        if info["bucket"] == "unchecked":
            unchecked += 1
            print(f"[scan] couldn't check {dialog.id}: {info.get('error')}")
        else:
            reason, extra = leave_reason(client, entity, info)
            if reason:
                candidates.append({
                    "peer_id": str(dialog.id),
                    "peer_type": "channel" if dialog.is_channel else "chat",
                    "title": dialog.title or "",
                    "reason": reason,
                    "url": _public_url(entity),
                    **extra,
                })
        time.sleep(random.uniform(*SCAN_PER_GROUP_DELAY_SECONDS))

    print(f"[scan] total={total} candidates={len(candidates)} unchecked={unchecked} skipped_not_live={skipped}")
    return total, candidates


def scan_account_status(client) -> dict:
    """Read-only group-count snapshot for /account_status. Every live group
    lands in exactly ONE bucket (same classifier as the scan), so
    total == active + restricted + paid_messages + read_only + broadcast +
    unchecked always holds (checked below and logged if it ever doesn't).
      active        - the account can send normally (includes groups it admins)
      restricted    - admin-muted for THIS account
      read_only     - sending off for everyone (group default)
      paid_messages - Stars per message
      broadcast     - channel/broadcast group: members can't post
      unchecked     - Telegram refused to tell us (see Actions log)"""
    me = client.get_me()
    now = _now()
    counts = {"total": 0, "active": 0, "restricted": 0, "paid_messages": 0,
              "read_only": 0, "broadcast": 0, "unchecked": 0}
    skipped = {}
    archived = 0

    for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        live, why = is_live_group(entity)
        if not live:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        counts["total"] += 1
        archived += 1 if getattr(dialog, "archived", False) else 0

        info = evaluate(client, entity, me.id, now)
        if info["bucket"] == "unchecked":
            print(f"[status] couldn't check {dialog.id}: {info.get('error')}")
        counts["restricted" if info["bucket"] == "muted" else info["bucket"]] += 1
        time.sleep(random.uniform(*SCAN_PER_GROUP_DELAY_SECONDS))

    parts = sum(v for k, v in counts.items() if k != "total")
    if parts != counts["total"]:
        print(f"::error title=Status buckets don't add up::total={counts['total']} sum={parts}")
    print(f"[status] {counts} (of which archived={archived}) skipped_not_live={skipped}")
    return counts


def _verify_and_leave(client, me_id, c) -> dict:
    """Re-checks ONE candidate against Telegram's CURRENT state right before
    leaving (the admin's Yes can come hours after the scan: a group may have
    grown past the member threshold, been un-muted, or the account may have
    been made admin since). Only leaves if it still qualifies under ANY leave
    reason; anything unverifiable is skipped, never left on a guess."""
    peer_id = c["peer_id"]
    entity = client.get_entity(int(peer_id))  # fresh fetch, not the scan-time snapshot

    live, why = is_live_group(entity)
    if not live:
        return {"peer_id": peer_id, "status": "skipped", "detail": f"not_a_member_anymore:{why}"}

    info = evaluate(client, entity, me_id, _now())
    if info["bucket"] == "unchecked":
        return {"peer_id": peer_id, "status": "skipped", "detail": f"verify_failed:{info.get('error')}"}
    reason, _ = leave_reason(client, entity, info)
    if not reason:
        return {"peer_id": peer_id, "status": "skipped", "detail": "no_longer_matches_any_leave_reason"}

    # delete_dialog picks the right underlying call (LeaveChannel for
    # channels/supergroups, DeleteChatUser for legacy groups).
    client.delete_dialog(entity)
    print(f"[leave] left {peer_id} ({c.get('title', '')}) -- verified reason now: {reason}")
    return {"peer_id": peer_id, "status": "left", "detail": ""}


def leave_confirmed_groups(client, candidates: list) -> tuple:
    """Returns (results, aborted_early). Result statuses: left | skipped
    (re-check says it no longer qualifies / can't be verified) | failed."""
    # get_entity(int_id) alone can fail to resolve a channel in a *fresh*
    # session because Telethon needs an access_hash it only learns from a
    # dialogs call first; iter_dialogs() warms that cache for every group
    # this account is in.
    list(client.iter_dialogs())
    me_id = client.get_me().id

    results = []
    aborted_early = False

    for c in candidates:
        peer_id = c["peer_id"]
        try:
            res = _verify_and_leave(client, me_id, c)
        except FloodWaitError as e:
            if e.seconds > MAX_FLOODWAIT_AUTO_RETRY_SECONDS:
                results.append({"peer_id": peer_id, "status": "failed",
                                 "detail": f"floodwait_{e.seconds}s_exceeded_budget"})
                continue
            print(f"[leave] FloodWait {e.seconds}s -- sleeping")
            time.sleep(e.seconds + 1)
            try:
                res = _verify_and_leave(client, me_id, c)
            except Exception as e2:
                res = {"peer_id": peer_id, "status": "failed",
                       "detail": f"unexpected_error:{type(e2).__name__}"}
        except PeerFloodError:
            # Account-wide anti-spam signal, not a per-group problem. Stop
            # leaving MORE groups this run; whatever's left stays 'pending'.
            results.append({"peer_id": peer_id, "status": "failed", "detail": "peer_flood_detected"})
            aborted_early = True
            break
        except RPCError as e:
            res = {"peer_id": peer_id, "status": "failed", "detail": f"rpc_error_{type(e).__name__}"}
        except Exception as e:
            res = {"peer_id": peer_id, "status": "failed",
                   "detail": f"unexpected_error:{type(e).__name__}:{str(e)[:150]}"}

        results.append(res)
        # Only a real leave is a "peer action" worth the long pacing delay.
        delay = LEAVE_PER_GROUP_DELAY_SECONDS if res["status"] == "left" else SCAN_PER_GROUP_DELAY_SECONDS
        time.sleep(random.uniform(*delay))

    return results, aborted_early


def main():
    print(f"scan_id={SCAN_ID} account={ACCOUNT} mode={MODE}")

    if MODE == "scan":
        try:
            with make_client(ACCOUNT) as client:
                total, candidates = scan_leave_candidates(client)
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
            with make_client(ACCOUNT) as client:
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

    if MODE == "status":
        try:
            with make_client(ACCOUNT) as client:
                counts = scan_account_status(client)
        except Exception as e:
            print(f"[unexpected] status scan failed: {type(e).__name__}: {e}")
            report({"mode": "status", "account": ACCOUNT, "scan_id": SCAN_ID,
                     "total": 0, "active": 0, "restricted": 0,
                     "paid_messages": 0, "read_only": 0})
            sys.exit(1)

        if not report({"mode": "status", "account": ACCOUNT, "scan_id": SCAN_ID, **counts}):
            sys.exit(1)
        return

    print(f"::error::Unknown MODE={MODE}")
    sys.exit(1)


if __name__ == "__main__":
    main()
