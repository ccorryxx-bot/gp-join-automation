"""
Offline logic tests for scripts/leave_groups.py (no Telegram connection).

Real Telethon TL types are used for every entity so isinstance() checks in the
script behave exactly like production; only the network layer is faked.
What this CANNOT prove: what Telegram's servers actually return for a given
account -- see README "Verification" for the live-check step.

Run:  python tests/test_leave_logic.py
"""
import json, os, sys
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
        total = self.full_totals.get(entity.id, 1000)  # unknown -> large, never below a threshold
        if limit == 0:
            return SimpleNamespace(total=total)
        if getattr(entity, "broadcast", False) and not (entity.creator or entity.admin_rights):
            raise errors.ChatAdminRequiredError(None)
        return SimpleNamespace(total=total)


RESULTS = []
def case(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))

def status(entities, **kw):
    return lg.scan_account_status(FakeClient(entities, **kw))

def scan(entities, **kw):
    total, cands, _unchecked = lg.scan_leave_candidates(FakeClient(entities, **kw))
    return total, {int(c["peer_id"]): c for c in cands}

def scan_u(entities, **kw):
    total, cands, unchecked = lg.scan_leave_candidates(FakeClient(entities, **kw))
    return total, {int(c["peer_id"]): c for c in cands}, unchecked

def buckets_sum(s):
    return sum(s.get(k, 0) for k in ("active", "restricted", "paid_messages", "read_only", "broadcast", "unchecked"))


# ---------- baseline: things that must be right in any version ----------
s = status([megagroup(1), megagroup(2, default_banned_rights=READ_ONLY), megagroup(3, send_paid_messages_stars=5)])
case("baseline: active / read_only / paid classified", (s["active"], s["read_only"], s["paid_messages"]) == (1, 1, 1), str(s))

s = status([megagroup(4)], participants={4: banned(restrict(EPOCH))})
case("baseline: forever-muted (until_date=0 -> epoch) counted restricted", s["restricted"] == 1, str(s))

t, c = scan([broadcast(5, participants_count=12), broadcast(6, participants_count=60), broadcast(7, participants_count=10)])
case("baseline (Phase 18): view-only channels: 12 / 10 members -> under_50_members, 60 -> view_only (not dropped silently)",
     {k: v["reason"] for k, v in c.items()} == {-1000000000005: "under_50_members", -1000000000006: "view_only", -1000000000007: "under_50_members"}
     and c[-1000000000005]["member_count"] == 12 and c[-1000000000006]["member_count"] == 60,
     str({k: v["reason"] for k, v in c.items()}))

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
    ent = broadcast(71, participants_count=80)
    fc = FakeClient([ent])
    res, aborted = lg.leave_confirmed_groups(fc, [{"peer_id": str(utils.get_peer_id(ent)), "title": "x", "reason": "under_50_members"}])
    case("C8 leave: view-only channel that grew past the threshold since the scan is NOT left",
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

ent = broadcast(91, participants_count=20)
fc = FakeClient([ent])
res, ab = lg.leave_confirmed_groups(fc, [{"peer_id": str(utils.get_peer_id(ent)), "title": "x", "reason": "under_50_members"}])
case("C11 leave: view-only channel under the threshold STILL qualifies -> left", res[0]["status"] == "left" and getattr(fc, "left", None) == [91], str(res))

ent = megagroup(92, admin_rights=T.ChatAdminRights(), participants_count=5)
fc = FakeClient([ent])
res, ab = lg.leave_confirmed_groups(fc, [{"peer_id": str(utils.get_peer_id(ent)), "title": "x", "reason": "under_50_members"}])
case("C11 leave: account became ADMIN since scan -> skipped, not left", res[0]["status"] == "skipped" and not getattr(fc, "left", None), str(res))

fc = FakeClient([megagroup(93)], participants={93: errors.ChannelPrivateError(None)})
res, ab = lg.leave_confirmed_groups(fc, [{"peer_id": str(utils.get_peer_id(fc.dialogs[0].entity)), "title": "x", "reason": "muted"}])
case("C11 leave: can't verify -> skipped, never left on a guess", res[0]["status"] == "skipped" and not getattr(fc, "left", None), str(res))

# =====================================================================
# 2026-10-17 audit. Owner's rule: "active" = the account can SEND messages.
#   NOT active: muted, read-only, paid messages (and view-only channels).
#   An active group must never be a leave candidate, whatever its size.
# =====================================================================
import contextlib, io, itertools

def pid(e): return utils.get_peer_id(e)

# How Telegram's own software writes a text restriction: `send_plain`, with the legacy
# `send_messages` left False (tdlib DialogParticipant.cpp get_chat_banned_rights();
# Android ChatRightsEditActivity: the "Send messages" toggle flips send_plain).
def plain_only(until=EPOCH, view=False):
    return T.ChatBannedRights(until_date=until, send_plain=True, view_messages=view)
READ_ONLY_PLAIN = plain_only()
BOT_MUTE = T.ChatBannedRights(  # what a Bot-API (TDLib-based) moderator bot writes for a full mute
    until_date=EPOCH, send_plain=True, send_photos=True, send_videos=True, send_roundvideos=True,
    send_audios=True, send_voices=True, send_docs=True, send_stickers=True, send_gifs=True,
    send_games=True, send_inline=True, embed_links=True, send_polls=True)

# ---------- S: send_plain restrictions are real restrictions ----------
s = status([megagroup(101)], participants={101: banned(plain_only())})
case("S1 status: member muted via send_plain (what Telegram clients write) -> restricted, not active",
     s["restricted"] == 1 and s["active"] == 0, str(s))
s = status([megagroup(102)], participants={102: banned(BOT_MUTE)})
case("S2 status: moderator-bot style full mute (send_plain + media flags) -> restricted", s["restricted"] == 1, str(s))
s = status([megagroup(103, default_banned_rights=READ_ONLY_PLAIN)])
case("S3 status: group made read-only via send_plain -> read_only, not active", s["read_only"] == 1 and s["active"] == 0, str(s))
e = megagroup(104)
t, c = scan([e], participants={104: banned(plain_only())})
case("S4 scan: send_plain mute is flagged as muted", c.get(pid(e), {}).get("reason") == "muted", str(c))
s = status([megagroup(105)], participants={105: banned(plain_only(view=True))})
case("S5 status: send_plain + view_messages = kicked/banned -> unchecked (not 'restricted')",
     s["unchecked"] == 1 and s["restricted"] == 0, str(s))
s = status([megagroup(106, admin_rights=T.ChatAdminRights(), default_banned_rights=READ_ONLY_PLAIN)])
case("S6 status: admin in a send_plain read-only group -> active", s["active"] == 1 and s["read_only"] == 0, str(s))
s = status([megagroup(107)], participants={107: banned(T.ChatBannedRights(until_date=EPOCH, send_stickers=True, embed_links=True))})
case("S7 status: only stickers/links restricted, text allowed -> still active (can send messages)", s["active"] == 1, str(s))
e = megagroup(108)
t, c = scan([e], participants={108: banned(plain_only(until=NOW + timedelta(days=2)))})
case("S8 scan: temporary send_plain mute -> muted WITH muted_until", c.get(pid(e), {}).get("muted_until"), str(c))
s = status([megagroup(109)], participants={109: banned(plain_only(until=NOW - timedelta(days=1)))})
case("S8 status: EXPIRED send_plain restriction -> active", s["active"] == 1 and s["restricted"] == 0, str(s))
e = megagroup(110)
fc = FakeClient([e], participants={110: banned(plain_only())})
res, ab = lg.leave_confirmed_groups(fc, [{"peer_id": str(pid(e)), "title": "x", "reason": "muted"}])
case("S9 leave: send_plain-muted group still qualifies at leave time -> left", res[0]["status"] == "left", str(res))

# ---------- A: active groups are never leave candidates ----------
t, c = scan([legacy(111, count=12), megagroup(112, participants_count=60), megagroup(113, participants_count=10)])
case("A1 scan: SMALL but active groups (12 / 60 / 10 members) are NOT leave candidates", c == {},
     str({k: v["reason"] for k, v in c.items()}))
e1, e2 = broadcast(114, participants_count=10), broadcast(115, participants_count=500)
t, c = scan([e1, e2])
case("A2 scan (Phase 18): view-only channels are flagged by size: <50 -> under_50_members, 500 -> view_only",
     {k: v["reason"] for k, v in c.items()} == {pid(e1): "under_50_members", pid(e2): "view_only"},
     str({k: v["reason"] for k, v in c.items()}))
e = megagroup(116, participants_count=20)
fc = FakeClient([e])
res, ab = lg.leave_confirmed_groups(fc, [{"peer_id": str(pid(e)), "title": "x", "reason": "under_50_members"}])
case("A3 leave: stale/forged 'under_50' candidate that is ACTIVE is skipped, never left",
     res[0]["status"] == "skipped" and not getattr(fc, "left", None), str(res))

def fuzz():
    def make(kind, i, n, stars, dflt, adm):
        kw = dict(adm)
        if dflt is not None: kw["default_banned_rights"] = dflt
        if kind == "legacy": return legacy(i, count=n, **kw)
        if stars: kw["send_paid_messages_stars"] = stars
        kw["participants_count"] = n
        if kind == "mega": return megagroup(i, **kw)
        if kind == "bcast": return broadcast(i, **kw)
        return T.Channel(id=i, title=f"gg{i}", photo=PHOTO, date=NOW, gigagroup=True, **kw)
    adms = [{}, {"creator": True}, {"admin_rights": T.ChatAdminRights()}]
    parts = [None, banned(restrict(EPOCH)), banned(plain_only()), banned(BOT_MUTE), banned(plain_only(view=True)),
             T.ChannelParticipantLeft(peer=T.PeerUser(ME)), errors.ChannelPrivateError(None),
             T.ChannelParticipantAdmin(user_id=ME, promoted_by=1, date=NOW, admin_rights=T.ChatAdminRights()),
             banned(T.ChatBannedRights(until_date=EPOCH, send_stickers=True)),   # per-user record, text allowed
             banned(restrict(NOW - timedelta(days=1)))]                        # expired restriction record
    n_cases, problems, i = 0, [], 1000
    for kind, dflt, adm, stars, n, p in itertools.product(
            ["mega", "bcast", "legacy", "giga"], [None, READ_ONLY, READ_ONLY_PLAIN], adms, [None, 5], [5, 500], parts):
        if kind == "legacy" and (stars or p is not None):
            continue  # legacy chats have no stars and no per-member restrictions
        i += 1
        ent = make(kind, i, n, stars, dflt, adm)
        pm = {i: p} if p is not None else {}
        label = f"{kind} dflt={'-' if dflt is None else 'x'} adm={list(adm)} stars={stars} n={n} part={type(p).__name__}"
        with contextlib.redirect_stdout(io.StringIO()):
            st = lg.scan_account_status(FakeClient([ent], participants=pm))
            _, cands, _u = lg.scan_leave_candidates(FakeClient([ent], participants=pm, full_totals={i: n}))
            fc = FakeClient([ent], participants=pm, full_totals={i: n})
            lg.leave_confirmed_groups(fc, [{"peer_id": str(pid(ent)), "title": "x", "reason": "under_50_members"}], "all")
            fc2 = FakeClient([ent], participants=pm, full_totals={i: n})
            lg.leave_confirmed_groups(fc2, [{"peer_id": str(pid(ent)), "title": "x", "reason": "view_only"}], "rvp")
        n_cases += 1
        if st["total"] != buckets_sum(st): problems.append("sum!=total " + label)
        if st["active"] == 1 and cands: problems.append("ACTIVE group offered for leaving: " + label)
        if st["active"] == 1 and getattr(fc, "left", None): problems.append("ACTIVE group was LEFT: " + label)
        if st["active"] == 1 and getattr(fc2, "left", None): problems.append("ACTIVE group was LEFT by rvp: " + label)
        if st["unchecked"] == 1 and (cands or getattr(fc, "left", None) or getattr(fc2, "left", None)):
            problems.append("UNCHECKED group offered/left: " + label)
        if st["restricted"] == 1 and getattr(fc2, "left", None): problems.append("MUTED group left by rvp: " + label)
        if st["broadcast"] == 1 and n >= 50 and getattr(fc, "left", None): problems.append("big view-only left by 'all': " + label)
    return n_cases, problems
n_cases, problems = fuzz()
case(f"A4 invariant over {n_cases} generated situations: active is never offered/left, total == sum(buckets)",
     not problems, f"{len(problems)} problems; first: {problems[:3]}")

# =====================================================================
# Phase 18 (2026-10-28): scopes ("all" = original Yes, "rvp" = read-only +
# view-only + paid), every view-only channel offered, per-user-exception
# guard, time budget, stop-on-rate-limit, pacing, unchecked reported.
# =====================================================================
def leave(entities, cands, scope, participants=None, full_totals=None, client_cls=None, **kw):
    fc = (client_cls or FakeClient)(entities, participants=participants, full_totals=full_totals)
    res, ab = lg.leave_confirmed_groups(fc, [dict(peer_id=str(pid(e)), title="x", reason=r) for e, r in cands], scope, **kw)
    return fc, res, ab

# --- P1/P2: which channels become candidates
e_big, e_small, e_unk = broadcast(201, participants_count=500), broadcast(202, participants_count=7), broadcast(203)
t, c = scan([e_big, e_small, e_unk], full_totals={203: 90})
case("P1 scan: 500 -> view_only, 7 -> under_50_members, count resolved via GetFullChannel (90) -> view_only",
     {k: v["reason"] for k, v in c.items()} == {pid(e_big): "view_only", pid(e_small): "under_50_members", pid(e_unk): "view_only"}
     and c[pid(e_unk)]["member_count"] == 90, str(c))
t, c = scan([megagroup(204, participants_count=500), megagroup(205, participants_count=3), legacy(206, count=2)])
case("P1b scan: ACTIVE groups are still never candidates, at any size (owner's rule)", c == {}, str(c))

# --- P3..P6: scopes decide what a tap may leave
e_ro, e_paid, e_mute, e_vo, e_u50 = (megagroup(211, default_banned_rights=READ_ONLY), megagroup(212, send_paid_messages_stars=5),
                                     megagroup(213), broadcast(214, participants_count=900), broadcast(215, participants_count=5))
ents = [e_ro, e_paid, e_mute, e_vo, e_u50]
parts = {213: banned(restrict(EPOCH))}
cands = [(e_ro, "read_only"), (e_paid, "paid_messages"), (e_mute, "muted"), (e_vo, "view_only"), (e_u50, "under_50_members")]
fc, res, ab = leave(ents, cands, "rvp", participants=parts)
by = {r["peer_id"]: r for r in res}
case("P3 rvp: read-only + paid + big view-only + small view-only are LEFT",
     all(by[str(pid(e))]["status"] == "left" for e in (e_ro, e_paid, e_vo, e_u50)), str(res))
case("P4 rvp: a MUTED group is never left by the rvp button (skipped, outside_selected_scope:muted)",
     by[str(pid(e_mute))]["status"] == "skipped" and by[str(pid(e_mute))]["detail"] == "outside_selected_scope:muted"
     and e_mute.id not in getattr(fc, "left", []), str(by[str(pid(e_mute))]))
fc, res, ab = leave(ents, cands, "all", participants=parts)
by = {r["peer_id"]: r for r in res}
case("P5 all (original Yes): muted + read-only + paid + under-50 are left, exactly as before",
     all(by[str(pid(e))]["status"] == "left" for e in (e_ro, e_paid, e_mute, e_u50)), str(res))
case("P6 all (original Yes): a big view-only channel is NOT left (it was never part of the original Yes)",
     by[str(pid(e_vo))]["status"] == "skipped" and by[str(pid(e_vo))]["detail"] == "outside_selected_scope:view_only"
     and e_vo.id not in getattr(fc, "left", []), str(by[str(pid(e_vo))]))
e_grew = broadcast(216, participants_count=40)
e_grew.participants_count = 400  # grew since the scan
fc, res, ab = leave([e_grew], [(e_grew, "under_50_members")], "all")
case("P6b all: an under_50 channel that grew past the threshold since the scan is skipped (as before)", res[0]["status"] == "skipped", str(res))

# --- P7: nothing the account can send in is ever left, in any scope
e_act, e_adm = megagroup(221, participants_count=900), megagroup(222, admin_rights=T.ChatAdminRights())
for sc in ("all", "rvp"):
    fc, res, ab = leave([e_act, e_adm], [(e_act, "read_only"), (e_adm, "view_only")], sc)
    case(f"P7 {sc}: active group / admin group with a forged candidate row -> skipped, never left",
         [r["status"] for r in res] == ["skipped", "skipped"] and not getattr(fc, "left", None), str(res))

# --- P8: per-user record inside a read-only group is ambiguous -> never touched
EXC = T.ChatBannedRights(until_date=EPOCH, send_stickers=True)           # text allowed for THIS user
e_x = megagroup(231, default_banned_rights=READ_ONLY)
s = status([e_x], participants={231: banned(EXC)})
t, c, unc = scan_u([e_x], participants={231: banned(EXC)})
case("P8 read-only group + a per-user record that allows text -> 'unchecked' (not read_only), not a candidate, counted in scan",
     s["unchecked"] == 1 and s["read_only"] == 0 and c == {} and unc == 1, f"{s} cands={c} unchecked={unc}")
s = status([e_x], participants={231: banned(restrict(NOW - timedelta(days=1)))})
case("P8b read-only group + EXPIRED restriction record -> also unchecked (can't prove the account can't send)", s["unchecked"] == 1, str(s))
fc, res, ab = leave([e_x], [(e_x, "read_only")], "rvp", participants={231: banned(EXC)})
case("P8c leave: such a group is skipped even if a stale row says read_only", res[0]["status"] == "skipped" and not getattr(fc, "left", None), str(res))
e_plain = megagroup(232, default_banned_rights=READ_ONLY)
s = status([e_plain])
case("P8d read-only group where the account is a plain member (no record) is still read_only", s["read_only"] == 1, str(s))

# --- P9: bad scope
try:
    lg.leave_confirmed_groups(FakeClient([]), [], "everything"); bad_scope = False
except ValueError:
    bad_scope = True
case("P9 unknown scope -> ValueError, nothing runs", bad_scope)

# --- P10: time budget stops NEW leaves; the report still happens
class Tick:
    def __init__(self): self.t = 0
    def __call__(self): self.t += 10; return self.t
chans = [broadcast(240 + k, participants_count=300) for k in range(6)]
fc, res, ab = leave(chans, [(e, "view_only") for e in chans], "rvp", budget_seconds=35, clock=Tick())
case("P10 time budget: run stops starting new leaves, aborted_early=True, the untouched rest is not attempted",
     ab is True and 0 < len(res) < 6 and len(getattr(fc, "left", [])) == len(res), f"results={len(res)} aborted={ab}")
fc, res, ab = leave(chans, [(e, "view_only") for e in chans], "rvp", budget_seconds=0)
case("P10b budget 0 = unlimited (all 6 processed)", len(res) == 6 and ab is False, f"{len(res)} {ab}")

# --- P11..P13: rate limits
class FloodOnDelete(FakeClient):
    def __init__(self, *a, seconds=500, times=99, **k):
        super().__init__(*a, **k); self.seconds, self.times, self.n = seconds, times, 0
    def delete_dialog(self, entity):
        self.n += 1
        if self.n <= self.times: raise errors.FloodWaitError(None, self.seconds)
        return super().delete_dialog(entity)
fcx = FloodOnDelete(chans[:3], seconds=500)
res, ab = lg.leave_confirmed_groups(fcx, [dict(peer_id=str(pid(e)), title="x", reason="view_only") for e in chans[:3]], "rvp")
case("P11 a FloodWait too long to wait out STOPS the run (1 attempt, not 3 hammering calls), aborted_early=True",
     len(res) == 1 and fcx.n == 1 and ab is True and res[0]["detail"].startswith("floodwait_500s"), f"n={fcx.n} {res} {ab}")
fcx = FloodOnDelete(chans[:2], seconds=5, times=1)
res, ab = lg.leave_confirmed_groups(fcx, [dict(peer_id=str(pid(e)), title="x", reason="view_only") for e in chans[:2]], "rvp")
case("P12 a short FloodWait is waited out and the group is still left", [r["status"] for r in res] == ["left", "left"] and not ab, str(res))
fcx = FloodOnDelete(chans[:3], seconds=5, times=99)
res, ab = lg.leave_confirmed_groups(fcx, [dict(peer_id=str(pid(e)), title="x", reason="view_only") for e in chans[:3]], "rvp")
case("P12b FloodWait that keeps coming back after one retry also stops the run", len(res) == 1 and ab is True, f"{res} {ab}")
class PeerFloodOnDelete(FakeClient):
    def delete_dialog(self, entity): raise errors.PeerFloodError(None)
fcx = PeerFloodOnDelete(chans[:3])
res, ab = lg.leave_confirmed_groups(fcx, [dict(peer_id=str(pid(e)), title="x", reason="view_only") for e in chans[:3]], "rvp")
case("P13 PeerFlood still stops the run after the first group", len(res) == 1 and ab is True and res[0]["detail"] == "peer_flood_detected", str(res))

# --- P14: pacing sleeps only after a real API call
slept = []
_orig_sleep = time.sleep
time.sleep = lambda s: slept.append(s)
lg.scan_leave_candidates(FakeClient([megagroup(251, admin_rights=T.ChatAdminRights()), megagroup(252, creator=True), legacy(253), legacy(254)]))
no_api = len(slept)
slept.clear()
lg.scan_leave_candidates(FakeClient([megagroup(255), megagroup(256)]))
with_api = len(slept)
time.sleep = _orig_sleep
case("P14 scan: no pacing sleep for admin/legacy groups (0 API calls), one per group that cost a call (2)", (no_api, with_api) == (0, 2), f"{no_api}, {with_api}")

# --- P15: scan report carries the unchecked count; leave with a bad SCOPE exits 1 before touching Telegram
class CM(FakeClient):
    def __enter__(self): return self
    def __exit__(self, *a): return False
def run_main(mode, client, scope=None):
    sent, saved = [], (lg.MODE, lg.make_client, lg.report, lg.SCOPE, lg.fetch_candidates)
    made = []
    lg.MODE, lg.make_client = mode, (lambda account: made.append(1) or client)
    lg.report = lambda payload: sent.append(payload) or True
    lg.fetch_candidates = lambda: [dict(peer_id="1", title="x", reason="view_only")]
    if scope is not None: lg.SCOPE = scope
    code = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            lg.main()
    except SystemExit as ex:
        code = ex.code
    finally:
        lg.MODE, lg.make_client, lg.report, lg.SCOPE, lg.fetch_candidates = saved
    return code, sent, made
code, sent, made = run_main("scan", CM([megagroup(261), megagroup(262)], participants={262: errors.ChannelPrivateError(None)}))
case("P15 scan report includes unchecked=1 (a group Telegram wouldn't let us judge is not silent)",
     code is None and sent and sent[0].get("unchecked") == 1 and sent[0]["total_dialogs"] == 2, str(sent))
code, sent, made = run_main("leave", CM([]), scope="bogus")
case("P15b leave with an unknown SCOPE: exit 1, reports aborted (flags get cleared), Telegram never contacted",
     code == 1 and sent and sent[0].get("aborted_early") is True and not made, f"code={code} sent={sent} made={made}")

# ---------- F: a failed run must not report fake zeros as a result ----------
def run_main_failing(mode):
    sent, saved = [], (lg.MODE, lg.make_client, lg.report)
    def boom(account): raise RuntimeError("missing_secrets:CH_TG_API_ID")
    lg.MODE, lg.make_client, lg.report = mode, boom, (lambda p: sent.append(p) or True)
    code = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            lg.main()
    except SystemExit as ex:
        code = ex.code
    finally:
        lg.MODE, lg.make_client, lg.report = saved
    return code, sent
for mode in ("status", "scan"):
    code, sent = run_main_failing(mode)
    p = sent[0] if sent else {}
    case(f"F1 {mode}: failure exits 1 and reports an explicit error, no fake zero counts",
         code == 1 and "error" in p and "total" not in p and "total_dialogs" not in p and "candidates" not in p, str(p))

# ---------- G: refuse to run on a Telethon that lacks the fields the classifier needs ----------
class OldChannel:  # stands in for a Telethon whose TL schema predates paid messages
    def __init__(self, id, default_banned_rights=None, gigagroup=False): pass
try:
    lg._require_tl_fields(channel_cls=OldChannel); raised = False
except RuntimeError as ex:
    raised = "send_paid_messages_stars" in str(ex)
case("G1 outdated Telethon (no send_paid_messages_stars) -> loud failure, not silent 'active'", raised)
try:
    lg._require_tl_fields(); ok_now = True
except Exception as ex:
    ok_now = False
case("G2 installed Telethon passes the field check", ok_now)


# ---------- L: Phase 19 -- MODE=list (/listgroups_py) ----------
def lst(entities, **kw):
    return lg.scan_active_group_ids(FakeClient(entities, **kw))

def list_sum(r):
    return (r["active"] + r["restricted"] + r["paid_messages"] + r["read_only"] + r["broadcast"]
            + r["unchecked"] + sum(r["excluded"].values()))

r = lst([megagroup(301), megagroup(302, default_banned_rights=READ_ONLY), megagroup(303, send_paid_messages_stars=5),
         broadcast(304, participants_count=100), megagroup(305), legacy(306), legacy(307, default_banned_rights=READ_ONLY)],
        participants={305: banned(restrict(EPOCH))})
case("L1 only groups the account can send in are listed (read-only / paid / view-only channel / muted are not)",
     r["group_ids"] == [-1000000000301, -306] and (r["active"], r["read_only"], r["paid_messages"], r["broadcast"], r["restricted"]) == (2, 2, 1, 1, 1),
     str(r))
case("L1b ids are Telegram peer ids in the -100... form (legacy chats: -<id>), plain ints", all(isinstance(i, int) for i in r["group_ids"]) and r["group_ids"][0] == utils.get_peer_id(megagroup(301)), str(r["group_ids"]))

# the three rules of the owner's list_groups.py that the shared classifier does not apply
st = status([broadcast(311, creator=True)])
r = lst([broadcast(311, creator=True), megagroup(312, creator=True)])
case("L2 admin-owned broadcast CHANNEL is not a group: excluded from the list, although /account_status still counts it active",
     r["group_ids"] == [-1000000000312] and r["excluded"]["channel"] == 1 and st["active"] == 1, f"{r} status={st}")
r = lst([megagroup(313, monoforum=True)])
case("L3 monoforum (channel direct-messages inbox) is excluded", r["group_ids"] == [] and r["excluded"]["monoforum"] == 1, str(r))
SUB = NOW + timedelta(days=5)
r = lst([megagroup(314, subscription_until_date=SUB), megagroup(315)])
case("L4 Stars-subscription group is excluded (EXCLUDE_PAID_SUB on by default)", r["group_ids"] == [-1000000000315] and r["excluded"]["paid_subscription"] == 1, str(r))
lg.EXCLUDE_PAID_SUB = False
r = lst([megagroup(314, subscription_until_date=SUB)])
lg.EXCLUDE_PAID_SUB = True
case("L4b EXCLUDE_PAID_SUB=0 keeps subscription groups", r["group_ids"] == [-1000000000314] and r["excluded"]["paid_subscription"] == 0, str(r))

# honesty about groups we could not judge, and the invariant
r = lst([megagroup(321), megagroup(322), broadcast(323, creator=True), megagroup(324, monoforum=True), megagroup(325, default_banned_rights=READ_ONLY)],
        participants={322: errors.ChannelPrivateError(None)})
case("L5 a group Telegram won't let us read is counted unchecked and NEVER listed as active",
     r["unchecked"] == 1 and -1000000000322 not in r["group_ids"] and r["group_ids"] == [-1000000000321], str(r))
case("L6 total == active + every other bucket + excluded (nothing vanishes)", r["total"] == list_sum(r) == 5, f"total={r['total']} sum={list_sum(r)}")

# same group delivered twice (archive + main folder) is listed and counted once
e = megagroup(331)
fc = FakeClient([e]); fc.dialogs = fc.dialogs * 2
r = lg.scan_active_group_ids(fc)
case("L7 a dialog returned twice is listed and counted once", r["group_ids"] == [-1000000000331] and r["total"] == 1, str(r))

# time budget: a partial list must never look complete
ticks = iter(range(0, 1000, 10))
try:
    lg.scan_active_group_ids(FakeClient([megagroup(341), megagroup(342)]), budget_seconds=5, clock=lambda: next(ticks)); raised = False
except RuntimeError as ex:
    raised = str(ex).startswith("time_budget_exceeded")
case("L8 out of time -> RuntimeError (reported as an error), not a partial list", raised)

# pacing: only after a group that cost a real API call (admin groups cost none)
_orig_sleep, slept = time.sleep, []
time.sleep = lambda s_: slept.append(s_)
lg.scan_active_group_ids(FakeClient([megagroup(351, creator=True), legacy(352)])); no_api = len(slept)
slept.clear()
lg.scan_active_group_ids(FakeClient([megagroup(353), megagroup(354)])); with_api = len(slept)
time.sleep = _orig_sleep
case("L9 list: no pacing sleep for admin/legacy groups, one per group that cost a call", (no_api, with_api) == (0, 2), f"{no_api}, {with_api}")

# main(): the report the Worker receives
code, sent, made = run_main("list", CM([megagroup(361), broadcast(362, participants_count=9)]))
p_ = sent[0] if sent else {}
case("L10 list main: one report, mode=list, ids + breakdown, JSON-serializable, exit 0",
     code is None and len(sent) == 1 and p_.get("mode") == "list" and p_.get("group_ids") == [-1000000000361]
     and p_.get("total") == 2 and p_.get("broadcast") == 1 and "error" not in p_ and json.loads(json.dumps(p_)) == p_, str(sent))
code, sent = run_main_failing("list")
p_ = sent[0] if sent else {}
case("L11 list failure: exits 1 and reports an explicit error -- no group_ids, no fake empty list",
     code == 1 and "error" in p_ and "group_ids" not in p_ and "total" not in p_, str(p_))


bad = [n for n, ok, _ in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
sys.exit(1 if bad else 0)
