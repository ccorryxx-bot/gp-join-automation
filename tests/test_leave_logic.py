"""
Offline logic tests for scripts/leave_groups.py (no Telegram connection).

Real Telethon TL types are used for every entity so isinstance() checks in the
script behave exactly like production; only the network layer is faked.
What this CANNOT prove: what Telegram's servers actually return for a given
account -- see README "Verification" for the live-check step.

Run:  python tests/test_leave_logic.py
"""
import os, sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.update(ACCOUNT="CH", MODE="status", SCAN_ID="1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import time, random
time.sleep = lambda s: None  # noqa
random.uniform = lambda a, b: 0  # noqa

from telethon import errors, utils
from telethon.tl import types as T
from telethon.tl.functions.channels import GetParticipantRequest
import leave_groups as lg

NOW = datetime.now(timezone.utc)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)  # what telethon returns for until_date=0
PHOTO = T.ChatPhotoEmpty()
ME = 999
READ_ONLY = T.ChatBannedRights(until_date=EPOCH, send_messages=True)


def megagroup(i, **kw):
    return T.Channel(id=i, title=f"mg{i}", photo=PHOTO, date=NOW, megagroup=True, **kw)

def broadcast(i, **kw):
    return T.Channel(id=i, title=f"bc{i}", photo=PHOTO, date=NOW, broadcast=True, **kw)

def legacy(i, count=30, **kw):
    return T.Chat(id=i, title=f"chat{i}", photo=PHOTO, participants_count=count, date=NOW, version=1, **kw)

def dlg(e):
    return SimpleNamespace(
        entity=e, id=utils.get_peer_id(e), title=getattr(e, "title", ""), archived=False,
        is_group=isinstance(e, (T.Chat, T.ChatForbidden)) or (isinstance(e, T.Channel) and bool(e.megagroup)),
        is_channel=isinstance(e, T.Channel))

def banned(rights):
    return T.ChannelParticipantBanned(peer=T.PeerUser(ME), kicked_by=1, date=NOW, banned_rights=rights)

def restrict(until, view=False):
    return T.ChatBannedRights(until_date=until, send_messages=True, view_messages=view)


class FakeClient:
    """participants: {channel_id: participant | Exception}; full_totals: {channel_id: int}
    Telegram behaviour assumed by the fake: channels.getParticipants (limit>=1) needs
    admin rights on a broadcast channel; GetFullChannel (get_participants limit=0,
    per Telethon's own source comment) works for any member."""
    def __init__(self, entities, participants=None, full_totals=None):
        self.dialogs = [dlg(e) for e in entities]
        self.entities = {utils.get_peer_id(e): e for e in entities}
        self.participants = participants or {}
        self.full_totals = full_totals or {}
        self.calls = []
    def get_me(self): return SimpleNamespace(id=ME)
    def iter_dialogs(self): return iter(self.dialogs)
    def get_entity(self, i): return self.entities[i]
    def delete_dialog(self, entity):
        self.calls.append(("leave", entity.id)); self.left = getattr(self, "left", []) + [entity.id]
    def __call__(self, req):
        assert isinstance(req, GetParticipantRequest), req
        self.calls.append(("participant", req.channel.id))
        p = self.participants.get(req.channel.id, T.ChannelParticipant(user_id=ME, date=NOW))
        if isinstance(p, Exception):
            raise p
        return SimpleNamespace(participant=p)
    def get_participants(self, entity, limit=None):
        self.calls.append(("get_participants", entity.id, limit))
        if limit == 0:
            return SimpleNamespace(total=self.full_totals[entity.id])
        if getattr(entity, "broadcast", False) and not (entity.creator or entity.admin_rights):
            raise errors.ChatAdminRequiredError(None)
        return SimpleNamespace(total=self.full_totals[entity.id])


RESULTS = []
def case(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))

def status(entities, **kw):
    return lg.scan_account_status(FakeClient(entities, **kw))

def scan(entities, **kw):
    total, cands = lg.scan_leave_candidates(FakeClient(entities, **kw))
    return total, {int(c["peer_id"]): c for c in cands}

def buckets_sum(s):
    return sum(s.get(k, 0) for k in ("active", "restricted", "paid_messages", "read_only", "broadcast", "unchecked"))


# ---------- baseline: things that must be right in any version ----------
s = status([megagroup(1), megagroup(2, default_banned_rights=READ_ONLY), megagroup(3, send_paid_messages_stars=5)])
case("baseline: active / read_only / paid classified", (s["active"], s["read_only"], s["paid_messages"]) == (1, 1, 1), str(s))

s = status([megagroup(4)], participants={4: banned(restrict(EPOCH))})
case("baseline: forever-muted (until_date=0 -> epoch) counted restricted", s["restricted"] == 1, str(s))

t, c = scan([legacy(5, count=12), megagroup(6, participants_count=60), megagroup(7, participants_count=10)])
case("baseline: under-50 uses entity count (legacy 12 flagged, 60 not, 10 flagged)",
     set(c) == {-5, -1000000000007} and c[-5]["member_count"] == 12, str({k: v["reason"] for k, v in c.items()}))

# ---------- C1: unreadable group must not silently vanish from the buckets ----------
s = status([megagroup(11), megagroup(12)], participants={12: errors.ChannelPrivateError(None)})
case("C1 status: total == sum(buckets) when one group can't be read", s["total"] == buckets_sum(s),
     f"total={s['total']} buckets_sum={buckets_sum(s)} {s}")

# ---------- C2: migrated / deactivated legacy chat is not a live group ----------
migrated = legacy(21, deactivated=True, migrated_to=T.InputChannel(channel_id=22, access_hash=0))
s = status([migrated, megagroup(22)])
case("C2 status: migrated legacy chat + its supergroup count as ONE group", s["total"] == 1, f"total={s['total']}")
t, c = scan([migrated, megagroup(22, participants_count=80)])
case("C2 scan: deactivated legacy chat not offered for leaving", -21 not in c and t == 1, f"total={t} cands={list(c)}")

# ---------- C3/C4: admins & creators ----------
s = status([megagroup(31, admin_rights=T.ChatAdminRights(), default_banned_rights=READ_ONLY)])
case("C3 status: admin in a read-only group is NOT read_only (admins ignore default restrictions)",
     s["read_only"] == 0 and s["active"] == 1, str(s))
s = status([megagroup(32, creator=True, default_banned_rights=READ_ONLY)])
case("C3 status: creator in a read-only group is NOT read_only", s["read_only"] == 0 and s["active"] == 1, str(s))
t, c = scan([megagroup(33, creator=True, participants_count=5), megagroup(34, admin_rights=T.ChatAdminRights(), participants_count=5)])
case("C4 scan: never offers groups the account owns/administers for leaving", c == {}, str({k: v["reason"] for k, v in c.items()}))

# ---------- C5: broadcast channels ----------
s = status([broadcast(41, participants_count=500)])
case("C5 status: broadcast channel (can't post) not counted as 'active groups'",
     s.get("active", 0) == 0 and s.get("broadcast", 0) == 1, str(s))

# ---------- C6: member count when the dialog list omits it ----------
t, c = scan([broadcast(51)], full_totals={51: 12})
case("C6 scan: channel count missing from dialog -> real count via GetFullChannel -> flagged (12)",
     -1000000000051 in c and c[-1000000000051].get("member_count") == 12, str(c))
t, c = scan([megagroup(52)], full_totals={52: 70})
case("C6 scan: megagroup with 70 members via fallback is NOT flagged", c == {}, str(c))

# ---------- C7: temporary / expired restrictions ----------
s = status([megagroup(61)], participants={61: banned(restrict(NOW - timedelta(days=1)))})
case("C7 status: EXPIRED restriction is not 'restricted'", s["restricted"] == 0 and s["active"] == 1, str(s))
t, c = scan([megagroup(62)], participants={62: banned(restrict(NOW + timedelta(days=3)))})
cand = c.get(-1000000000062, {})
case("C7 scan: future-dated mute is flagged WITH a muted_until marker", cand.get("reason") == "muted" and cand.get("muted_until"), str(cand))
t, c = scan([megagroup(63)], participants={63: banned(restrict(EPOCH))})
case("C7 scan: permanent mute has no muted_until", c.get(-1000000000063, {}).get("reason") == "muted" and not c[-1000000000063].get("muted_until"), str(c))

# ---------- C8: leave-time re-verification ----------
if hasattr(lg, "leave_confirmed_groups"):
    ent = megagroup(71, participants_count=80)
    fc = FakeClient([ent])
    res, aborted = lg.leave_confirmed_groups(fc, [{"peer_id": str(utils.get_peer_id(ent)), "title": "x", "reason": "under_50_members"}])
    case("C8 leave: group that grew past the threshold since the scan is NOT left",
         res and res[0]["status"] == "skipped", str(res))


# ---------- more coverage: participant types, FloodWait retry, leave happy/unhappy paths ----------
s = status([megagroup(81)], participants={81: T.ChannelParticipantAdmin(user_id=ME, promoted_by=1, date=NOW, admin_rights=T.ChatAdminRights())})
case("C9 status: Telegram says participant is ADMIN (entity flag missing) -> active, not read_only/restricted", s["active"] == 1, str(s))
s = status([megagroup(82)], participants={82: T.ChannelParticipantLeft(peer=T.PeerUser(ME))})
case("C9 status: participant says LEFT -> unchecked, not counted active", s["unchecked"] == 1 and s["active"] == 0, str(s))

class FloodOnce(FakeClient):
    def __call__(self, req):
        if not getattr(self, "_flooded", False):
            self._flooded = True
            raise errors.FloodWaitError(None, 5)
        return super().__call__(req)
fc = FloodOnce([megagroup(83)], participants={83: banned(restrict(EPOCH))})
s = lg.scan_account_status(fc)
case("C10 status: short FloodWait is retried (group not lost) -> restricted=1", s["restricted"] == 1 and s["unchecked"] == 0, str(s))

ent = megagroup(91, participants_count=20)
fc = FakeClient([ent])
res, ab = lg.leave_confirmed_groups(fc, [{"peer_id": str(utils.get_peer_id(ent)), "title": "x", "reason": "under_50_members"}])
case("C11 leave: group that STILL qualifies is left", res[0]["status"] == "left" and getattr(fc, "left", None) == [91], str(res))

ent = megagroup(92, admin_rights=T.ChatAdminRights(), participants_count=5)
fc = FakeClient([ent])
res, ab = lg.leave_confirmed_groups(fc, [{"peer_id": str(utils.get_peer_id(ent)), "title": "x", "reason": "under_50_members"}])
case("C11 leave: account became ADMIN since scan -> skipped, not left", res[0]["status"] == "skipped" and not getattr(fc, "left", None), str(res))

fc = FakeClient([megagroup(93)], participants={93: errors.ChannelPrivateError(None)})
res, ab = lg.leave_confirmed_groups(fc, [{"peer_id": str(utils.get_peer_id(fc.dialogs[0].entity)), "title": "x", "reason": "muted"}])
case("C11 leave: can't verify -> skipped, never left on a guess", res[0]["status"] == "skipped" and not getattr(fc, "left", None), str(res))

bad = [n for n, ok, _ in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
sys.exit(1 if bad else 0)
