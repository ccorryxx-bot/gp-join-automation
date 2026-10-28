"""
Phase 8 (2026-09-23): "leave muted groups" automation, invoked by
.github/workflows/leave.yml. Two modes, selected by MODE:

  MODE=scan  - read-only. Iterates every dialog for this account and flags
               groups where this account CANNOT send messages: muted
               (admin-restricted: text banned but view_messages=False, i.e.
               not fully kicked/banned), read_only (sending off for everyone),
               paid_messages (Stars per message), and view-only channels
               with fewer than MIN_MEMBERS_THRESHOLD members ("under_50_members")
               -- and, since Phase 18, EVERY other view-only channel too
               ("view_only", any size). A group where
               the account CAN send messages is "active" and is NEVER a
               candidate, whatever its size (owner's rule, 2026-10-17; before
               that, small active groups were offered as "under_50_members").
               Reports the full candidate list (each tagged with its reason)
               back to the Worker in a single POST to /leave-report
               (mode=scan). The Worker then asks the admin a single Y/N
               confirm in Telegram before anything actually leaves.

  MODE=leave - destructive. Fetches the admin-confirmed candidate list for
               SCAN_ID from the Worker's /leave-candidates endpoint, then
               actually leaves each one with a short randomized delay
               between calls, and reports the full per-group result list
               back in a single POST to /leave-report (mode=leave).
               SCOPE (env, default "all") says which button the admin
               tapped; every candidate is re-verified against Telegram's
               CURRENT state and is only left if that state is inside the
               scope (see LEAVE_SCOPES). An active / admin / unverifiable
               group is never left, whatever the candidate row says. The run
               stops starting new leaves after LEAVE_TIME_BUDGET_SECONDS so
               it always reports before the 20-minute job timeout, and stops
               at the first PeerFlood / over-budget FloodWait instead of
               hammering a rate-limited account.

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

  MODE=list   - read-only. Phase 19 (2026-10-29), invoked by the admin's
               /listgroups_py bot command. Walks the same dialogs with the SAME
               classifier (evaluate()) and keeps only the groups where the
               account can send messages ("active"), then drops three kinds the
               owner's production list_groups.py never listed (list_exclusion():
               admin-owned broadcast channels, monoforum inboxes, Stars-
               subscription groups). Reports the group IDs plus a full bucket
               breakdown to /leave-report (mode=list); the Worker turns the IDs
               into a .txt file and sends it to the admin. A run that cannot
               finish (error, or LIST_TIME_BUDGET_SECONDS used up) reports an
               explicit error -- never a partial list that looks complete.

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

import inspect
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
    ChatBannedRights,
    ChatForbidden,
)

ACCOUNT = os.environ.get("ACCOUNT", "CH")
MODE = os.environ["MODE"]  # "scan" | "leave" | "status"
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

# Phase 18 (2026-10-28): what the admin's confirm button is allowed to leave.
# Passed by the Worker as the SCOPE workflow input (MODE=leave only).
#   all - the original "Yes, Leave": muted, read-only, paid-message groups and
#         view-only channels UNDER the member threshold. Big view-only
#         channels are deliberately NOT in here (they were never offered
#         before), so an old habit of tapping "Yes" can't start dropping them.
#   rvp - "read-only + view-only + paid": read_only, paid_messages and EVERY
#         view-only channel (view_only = any size, under_50_members = small).
#         Never muted.
# The scope is checked against the CURRENT, re-verified state of each group,
# not against what the scan row says.
SCOPE = os.environ.get("SCOPE", "all")
LEAVE_SCOPES = {
    "all": frozenset({"muted", "read_only", "paid_messages", "under_50_members"}),
    "rvp": frozenset({"read_only", "paid_messages", "view_only", "under_50_members"}),
}

# The job has timeout-minutes: 20 (leave.yml). A batch of 100+ channels at
# 3-8 s each plus verification can outlast that, and a killed job never
# reports -- the scan row would stay 'leaving' forever and the admin would
# hear nothing. No new leave is STARTED after this budget; the run reports
# what it did and the rest is left for the next /leavescan.
LEAVE_TIME_BUDGET_SECONDS = int(os.environ.get("LEAVE_TIME_BUDGET_SECONDS", str(16 * 60)))

# Phase 19 (2026-10-29): MODE=list. Same 20-minute job cap as leave, so the run
# stops itself first and reports an explicit error instead of being killed
# silently. EXCLUDE_PAID_SUB carries over the owner's production setting
# (Stars-subscription groups count as "paid", not active); set to 0 to keep them.
LIST_TIME_BUDGET_SECONDS = int(os.environ.get("LIST_TIME_BUDGET_SECONDS", str(17 * 60)))
EXCLUDE_PAID_SUB = os.environ.get("EXCLUDE_PAID_SUB", "1") != "0"

# Telegram API calls made through _with_floodwait_retry(). scan/status only
# pace themselves (1-2 s) after a group that actually cost an API call; an
# admin/legacy-chat dialog costs none, and sleeping there only stretched a
# 400-group scan past the 10-minute "leave:running" safety flag.
_TG_CALLS = 0

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


def _require_tl_fields(channel_cls=Channel, rights_cls=ChatBannedRights):
    """Refuse to run on a Telethon whose TL schema lacks the fields the
    classifier relies on. getattr(entity, "send_paid_messages_stars", None)
    would quietly return None on an old schema and every paid group would be
    counted as active; the same goes for send_plain on the rights object."""
    chan = set(inspect.signature(channel_cls.__init__).parameters)
    rights = set(inspect.signature(rights_cls.__init__).parameters)
    missing = sorted(
        ({"send_paid_messages_stars", "default_banned_rights", "gigagroup"} - chan)
        | ({"send_plain", "send_messages", "view_messages", "until_date"} - rights))
    if missing:
        raise RuntimeError("telethon_too_old_missing_fields:" + ",".join(missing))


def _error_text(e):
    return f"{type(e).__name__}: {str(e)[:120]}"


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


def text_banned(rights):
    """True if these ChatBannedRights forbid plain text messages -- the
    Telegram permission literally called "Send messages".

    Telegram has TWO text flags: the legacy `send_messages` and `send_plain`
    (added with the granular media permissions). Telegram's own software
    writes only `send_plain` when it mutes a member or makes a group
    read-only: tdlib (which also runs the Bot API server behind moderator
    bots) builds chatBannedRights with send_messages=false and puts the text
    restriction in send_plain (td/telegram/DialogParticipant.cpp,
    RestrictedRights::get_chat_banned_rights, used by channels.editBanned),
    and the Android permission editor's "Send messages" toggle flips
    send_plain as well (ChatRightsEditActivity). Reading only the legacy flag
    missed those restrictions and counted such groups as active. Either flag
    set = text banned."""
    return bool(rights and (rights.send_messages or rights.send_plain))


def mute_state(rights, now):
    """(muted_now, until). Muted = can't send text but can still view.
    until=None means permanent. A restriction whose until_date has passed is
    expired (tdlib treats it the same way) and is NOT muted."""
    if not (rights and text_banned(rights) and not rights.view_messages):
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
    global _TG_CALLS
    try:
        _TG_CALLS += 1
        return fn()
    except FloodWaitError as e:
        if e.seconds > MAX_FLOODWAIT_AUTO_RETRY_SECONDS:
            print(f"[{tag}] FloodWait {e.seconds}s too long -- giving up on this call")
            raise
        print(f"[{tag}] FloodWait {e.seconds}s -- sleeping, then retrying once")
        time.sleep(e.seconds + 1)
        _TG_CALLS += 1
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
    default_rights = getattr(entity, "default_banned_rights", None)
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
            if default_rights and text_banned(default_rights):
                # Phase 18 safety guard. The group is read-only for everyone by
                # default, but Telegram holds a per-user record for THIS account
                # that does not (any longer) ban text: either an admin-granted
                # exception ("this member may post") or an expired restriction.
                # Telegram's permission exceptions can lift a default, so we
                # cannot prove the account is unable to send -- and a group the
                # account can send in must never be left. Treat it as
                # unverifiable: counted as 'unchecked', never a leave candidate.
                # 🟡 conservative by design; not confirmed live which of the two
                # cases a given group is.
                return {"bucket": "unchecked", "admin": False, "error": "per_user_record_in_read_only_group"}

    stars = getattr(entity, "send_paid_messages_stars", None) if is_channel else None
    if default_rights and text_banned(default_rights):
        info["bucket"] = "read_only"      # group-wide: nobody but admins can send
    elif stars:
        info.update(bucket="paid_messages", stars_price=stars)
    elif is_channel and (getattr(entity, "broadcast", False) or getattr(entity, "gigagroup", False)):
        info["bucket"] = "broadcast"      # only admins post; members just read
    return info


def leave_reason(client, entity, info):
    """Leave reason for an evaluated group, or None. Only groups where this
    account cannot send messages can have one:
      muted | read_only | paid_messages | under_50_members | view_only
    NEVER a candidate: an active group (the account can send), an
    admin/creator group, or an unreadable group ("unchecked") that can't be
    judged."""
    if info["admin"]:
        return None, {}
    bucket = info["bucket"]
    if bucket == "muted":
        return "muted", ({"muted_until": info["muted_until"]} if info.get("muted_until") else {})
    if bucket == "read_only":
        return "read_only", {}
    if bucket == "paid_messages":
        return "paid_messages", {"stars_price": info["stars_price"]}
    if bucket == "broadcast":
        # View-only channel: not active (members can't post), so it can be
        # offered whatever its size. Small ones keep the original reason
        # "under_50_members" (still in the original "Yes, Leave" scope);
        # bigger ones are "view_only", which only the "rvp" button leaves.
        count = get_member_count(client, entity)
        extra = {"member_count": count} if count is not None else {}
        if count is not None and count < MIN_MEMBERS_THRESHOLD:
            return "under_50_members", extra
        return "view_only", extra
    # bucket == "active": the account can send messages here -> never a
    # candidate, however small the group is.
    return None, {}


def _public_url(entity):
    username = getattr(entity, "username", None) if isinstance(entity, Channel) else None
    return f"https://t.me/{username}" if username else None


def scan_leave_candidates(client) -> tuple:
    """Returns (total_groups, candidates, unchecked). total_groups counts only
    groups and channels the account is actually in (no private chats/bots/
    Saved Messages, no migrated/deactivated legacy chats, no groups it was
    removed from). `unchecked` = live groups Telegram wouldn't let us judge;
    they are never candidates and are reported so the admin knows they exist.
    A group is reported under at most ONE reason, priority:
      muted > read_only > paid_messages > under_50_members / view_only
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

        calls_before = _TG_CALLS
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
        if _TG_CALLS != calls_before:  # pace only after a real API call
            time.sleep(random.uniform(*SCAN_PER_GROUP_DELAY_SECONDS))

    print(f"[scan] total={total} candidates={len(candidates)} unchecked={unchecked} skipped_not_live={skipped}")
    return total, candidates, unchecked


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

        calls_before = _TG_CALLS
        info = evaluate(client, entity, me.id, now)
        if info["bucket"] == "unchecked":
            print(f"[status] couldn't check {dialog.id}: {info.get('error')}")
        counts["restricted" if info["bucket"] == "muted" else info["bucket"]] += 1
        if _TG_CALLS != calls_before:  # pace only after a real API call
            time.sleep(random.uniform(*SCAN_PER_GROUP_DELAY_SECONDS))

    parts = sum(v for k, v in counts.items() if k != "total")
    if parts != counts["total"]:
        print(f"::error title=Status buckets don't add up::total={counts['total']} sum={parts}")
    print(f"[status] {counts} (of which archived={archived}) skipped_not_live={skipped}")
    return counts


def list_exclusion(entity):
    """MODE=list only. Why a group the account CAN send in (evaluate() says
    "active") is still not wanted in the ID list -- or None to keep it. These
    are the rules of the owner's production list_groups.py that the shared
    classifier deliberately does not apply, so /account_status and /leavescan
    keep their meaning:
      channel           - a broadcast channel the account administers.
                          evaluate() calls any admin "active" (admins can post),
                          but a channel is not a group.
      monoforum         - a channel's direct-messages inbox, not a group.
      paid_subscription - a Stars-subscription group (EXCLUDE_PAID_SUB)."""
    if not isinstance(entity, Channel):
        return None  # legacy chats are always plain groups
    if getattr(entity, "monoforum", False):
        return "monoforum"
    if not getattr(entity, "megagroup", False):
        return "channel"
    if EXCLUDE_PAID_SUB and getattr(entity, "subscription_until_date", None):
        return "paid_subscription"
    return None


def scan_active_group_ids(client, budget_seconds=None, clock=time.monotonic) -> dict:
    """Read-only snapshot for /listgroups_py. Every live group lands in exactly
    ONE bucket, so   total == active + restricted + paid_messages + read_only
    + broadcast + unchecked + sum(excluded)   always holds (logged if not).
      group_ids - Telegram peer ids (the -100... form) of the active groups, in
                  dialog order, each once
      active    - len(group_ids): the account can send there AND list_exclusion()
                  has nothing against it
      excluded  - active-by-classifier groups list_exclusion() dropped, by reason
      unchecked - Telegram wouldn't let us judge; NOT in group_ids (reported so
                  the admin knows the list may be short -- re-run later)
    Raises RuntimeError if LIST_TIME_BUDGET_SECONDS runs out before every group
    was checked: a partial list would look like a complete one."""
    budget = LIST_TIME_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    started = clock()
    me = client.get_me()
    now = _now()
    counts = {"total": 0, "active": 0, "restricted": 0, "paid_messages": 0,
              "read_only": 0, "broadcast": 0, "unchecked": 0}
    excluded = {"channel": 0, "monoforum": 0, "paid_subscription": 0}
    ids = []
    seen = set()
    skipped = {}

    for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        live, why = is_live_group(entity)
        if not live:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        if dialog.id in seen:  # same group returned twice (e.g. archive + main folder)
            skipped["duplicate_dialog"] = skipped.get("duplicate_dialog", 0) + 1
            continue
        seen.add(dialog.id)
        if budget and clock() - started > budget:
            raise RuntimeError(f"time_budget_exceeded:{counts['total']}_groups_checked_of_more")
        counts["total"] += 1

        calls_before = _TG_CALLS
        info = evaluate(client, entity, me.id, now)
        bucket = info["bucket"]
        if bucket == "unchecked":
            print(f"[list] couldn't check {dialog.id}: {info.get('error')}")
            counts["unchecked"] += 1
        elif bucket == "active":
            reason = list_exclusion(entity)
            if reason:
                excluded[reason] += 1
            else:
                ids.append(dialog.id)
                counts["active"] += 1
        else:
            counts["restricted" if bucket == "muted" else bucket] += 1
        if _TG_CALLS != calls_before:  # pace only after a real API call
            time.sleep(random.uniform(*SCAN_PER_GROUP_DELAY_SECONDS))

    parts = sum(v for k, v in counts.items() if k != "total") + sum(excluded.values())
    if parts != counts["total"]:
        print(f"::error title=List buckets don't add up::total={counts['total']} sum={parts}")
    print(f"[list] {counts} excluded={excluded} listed={len(ids)} skipped_not_live={skipped}")
    return {"group_ids": ids, **counts, "excluded": excluded}


def _verify_and_leave(client, me_id, c, scope="all") -> dict:
    """Re-checks ONE candidate against Telegram's CURRENT state right before
    leaving (the admin's tap can come hours after the scan: a group may have
    grown past the member threshold, been un-muted, or the account may have
    been made admin since). Only leaves if the CURRENT state is a leave
    reason that the tapped button's scope covers (LEAVE_SCOPES); anything
    unverifiable is skipped, never left on a guess."""
    peer_id = c["peer_id"]
    entity = client.get_entity(int(peer_id))  # fresh fetch, not the scan-time snapshot

    live, why = is_live_group(entity)
    if not live:
        return {"peer_id": peer_id, "status": "skipped", "detail": f"not_a_member_anymore:{why}"}

    info = evaluate(client, entity, me_id, _now())
    if info["bucket"] == "unchecked":
        return {"peer_id": peer_id, "status": "skipped", "detail": f"verify_failed:{info.get('error')}"}
    if info["admin"] or info["bucket"] == "active":
        # Hard stop, independent of leave_reason(): a group where this account
        # can send messages is never left, whatever the (possibly stale or
        # hand-edited) candidate row says.
        return {"peer_id": peer_id, "status": "skipped", "detail": "active_never_left"}
    reason, _ = leave_reason(client, entity, info)
    if not reason:
        return {"peer_id": peer_id, "status": "skipped", "detail": "no_longer_matches_any_leave_reason"}
    if reason not in LEAVE_SCOPES[scope]:
        # e.g. the "rvp" button was tapped but the account got muted here in the
        # meantime, or an "under_50" channel grew past the threshold under the
        # original "Yes" -- not what the admin confirmed, so not touched.
        return {"peer_id": peer_id, "status": "skipped", "detail": f"outside_selected_scope:{reason}"}

    # delete_dialog picks the right underlying call (LeaveChannel for
    # channels/supergroups, DeleteChatUser for legacy groups).
    client.delete_dialog(entity)
    print(f"[leave] left {peer_id} ({c.get('title', '')}) -- verified reason now: {reason}")
    return {"peer_id": peer_id, "status": "left", "detail": ""}


def leave_confirmed_groups(client, candidates: list, scope: str = "all",
                           budget_seconds=None, clock=time.monotonic) -> tuple:
    """Returns (results, aborted_early). Result statuses: left | skipped
    (re-check says it no longer qualifies / is outside the tapped scope / can't
    be verified) | failed. aborted_early=True means the run STOPPED before
    working through every candidate (account-wide PeerFlood, a FloodWait too
    long to wait out, or the time budget); the untouched rest stays 'pending'
    in the Worker's DB."""
    if scope not in LEAVE_SCOPES:
        raise ValueError(f"unknown_scope:{scope}")
    budget = LEAVE_TIME_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    started = clock()

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
        if budget and clock() - started > budget:
            print(f"[leave] time budget ({budget}s) used up -- stopping; the rest stays pending")
            aborted_early = True
            break

        stop = False
        try:
            res = _verify_and_leave(client, me_id, c, scope)
        except FloodWaitError as e:
            if e.seconds > MAX_FLOODWAIT_AUTO_RETRY_SECONDS:
                # The account is rate-limited for longer than we will wait.
                # Carrying on would just hit the same wall for every remaining
                # group (and ignoring a FloodWait is what escalates it) -- stop.
                res = {"peer_id": peer_id, "status": "failed",
                       "detail": f"floodwait_{e.seconds}s_exceeded_budget"}
                stop = True
            else:
                print(f"[leave] FloodWait {e.seconds}s -- sleeping")
                time.sleep(e.seconds + 1)
                try:
                    res = _verify_and_leave(client, me_id, c, scope)
                except (FloodWaitError, PeerFloodError) as e2:
                    res = {"peer_id": peer_id, "status": "failed",
                           "detail": ("peer_flood_detected" if isinstance(e2, PeerFloodError)
                                      else f"floodwait_{e2.seconds}s_again")}
                    stop = True
                except Exception as e2:
                    res = {"peer_id": peer_id, "status": "failed",
                           "detail": f"unexpected_error:{type(e2).__name__}"}
        except PeerFloodError:
            # Account-wide anti-spam signal, not a per-group problem. Stop
            # leaving MORE groups this run; whatever's left stays 'pending'.
            res = {"peer_id": peer_id, "status": "failed", "detail": "peer_flood_detected"}
            stop = True
        except RPCError as e:
            res = {"peer_id": peer_id, "status": "failed", "detail": f"rpc_error_{type(e).__name__}"}
        except Exception as e:
            res = {"peer_id": peer_id, "status": "failed",
                   "detail": f"unexpected_error:{type(e).__name__}:{str(e)[:150]}"}

        results.append(res)
        if stop:
            aborted_early = True
            break
        # Only a real leave is a "peer action" worth the long pacing delay.
        delay = LEAVE_PER_GROUP_DELAY_SECONDS if res["status"] == "left" else SCAN_PER_GROUP_DELAY_SECONDS
        time.sleep(random.uniform(*delay))

    return results, aborted_early


def main():
    print(f"scan_id={SCAN_ID} account={ACCOUNT} mode={MODE}" + (f" scope={SCOPE}" if MODE == "leave" else ""))

    if MODE == "scan":
        try:
            _require_tl_fields()
            with make_client(ACCOUNT) as client:
                total, candidates, unchecked = scan_leave_candidates(client)
        except Exception as e:
            print(f"[unexpected] scan failed: {type(e).__name__}: {e}")
            # An explicit error, NOT "0 groups / 0 candidates": the Worker would
            # present that as a real, clean result.
            report({"mode": "scan", "account": ACCOUNT, "scan_id": SCAN_ID,
                     "error": _error_text(e)})
            sys.exit(1)

        if not report({"mode": "scan", "account": ACCOUNT, "scan_id": SCAN_ID,
                        "total_dialogs": total, "candidates": candidates,
                        "unchecked": unchecked}):
            sys.exit(1)
        return

    if MODE == "leave":
        if SCOPE not in LEAVE_SCOPES:
            # Fail BEFORE touching Telegram, but still report so the Worker
            # clears its leave:running flag and the admin hears about it.
            print(f"::error::Unknown SCOPE={SCOPE!r} (expected one of {sorted(LEAVE_SCOPES)})")
            report({"mode": "leave", "account": ACCOUNT, "scan_id": SCAN_ID,
                     "results": [], "aborted_early": True})
            sys.exit(1)
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
            _require_tl_fields()
            with make_client(ACCOUNT) as client:
                results, aborted_early = leave_confirmed_groups(client, candidates, SCOPE)
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
            _require_tl_fields()
            with make_client(ACCOUNT) as client:
                counts = scan_account_status(client)
        except Exception as e:
            print(f"[unexpected] status scan failed: {type(e).__name__}: {e}")
            # An explicit error, NOT zero counts: the Worker would show
            # "Total groups - 0 / active groups - 0" as if it were the result.
            report({"mode": "status", "account": ACCOUNT, "scan_id": SCAN_ID,
                     "error": _error_text(e)})
            sys.exit(1)

        if not report({"mode": "status", "account": ACCOUNT, "scan_id": SCAN_ID, **counts}):
            sys.exit(1)
        return

    if MODE == "list":
        try:
            _require_tl_fields()
            with make_client(ACCOUNT) as client:
                result = scan_active_group_ids(client)
        except Exception as e:
            print(f"[unexpected] list failed: {type(e).__name__}: {e}")
            # An explicit error, NOT an empty list: the Worker would send a
            # "[]" file / "0 active groups" as if that were the real answer.
            report({"mode": "list", "account": ACCOUNT, "scan_id": SCAN_ID,
                     "error": _error_text(e)})
            sys.exit(1)

        if not report({"mode": "list", "account": ACCOUNT, "scan_id": SCAN_ID, **result}):
            sys.exit(1)
        return

    print(f"::error::Unknown MODE={MODE}")
    sys.exit(1)


if __name__ == "__main__":
    main()
