"""
Phase 4: Telethon join logic, invoked by .github/workflows/join.yml

Env vars (required):
  TG_API_ID, TG_API_HASH, TG_SESSION_STRING  - dedicated Telethon account creds
  GROUP_URL, QUEUE_ID                        - passed from workflow_dispatch inputs

Env vars (optional):
  REPORT_URL, REPORT_SECRET - base URL of the Worker (e.g.
               https://gp-join-automation.<sub>.workers.dev) and the shared
               secret it expects on X-Report-Secret. If REPORT_URL is unset,
               the report step is skipped entirely (useful for local testing).

Exit code: 0 only if BOTH the join succeeded/was already a member AND the
/report callback was actually delivered. A failed callback now fails the
job even if the join itself worked -- a "green" run that never told the
user anything is worse than a red one that did.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

# telethon.sync wraps every client method (including __call__) so it runs
# synchronously with no `await` and no running event loop. Plain
# `from telethon import TelegramClient` does NOT do this -- __call__ stays a
# bare coroutine function, so calling it unawaited just builds a coroutine
# object and silently drops it (RuntimeWarning: coroutine
# 'UserMethods.__call__' was never awaited) -- the request never reaches
# Telegram, no exception is raised, and the old code fell straight through
# to `return "joined", ""`. https://docs.telethon.dev/en/stable/basic/quick-start.html
from telethon.sync import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    RPCError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"]
GROUP_URL = os.environ["GROUP_URL"]
QUEUE_ID = os.environ["QUEUE_ID"]
REPORT_URL = os.environ.get("REPORT_URL", "").rstrip("/")
REPORT_SECRET = os.environ.get("REPORT_SECRET", "")

# Short floods get auto-slept through; anything past this budget fails fast
# instead of tying up the Actions runner (worst case: MAX_RETRIES sleeps).
MAX_FLOODWAIT_AUTO_RETRY_SECONDS = 120
MAX_FLOODWAIT_RETRIES = 3

# Cloudflare's Browser Integrity Check flags urllib's default
# "Python-urllib/3.x" User-Agent as a bot signature and blocks it with a 403
# (error 1010) *at the edge* -- before the Worker's own code, and therefore
# before X-Report-Secret is ever checked, gets to run. Confirmed directly
# against production D1 on 2026-09-22: debug_log (written at the very top
# of handleReport(), pre-auth-check) had 0 rows across all 5 real join
# attempts so far, every one of which ended stale_no_report_timeout. This
# header is a stopgap; the durable fix is putting the Worker on a zone we
# control so a WAF rule can skip BIC for /report specifically, since that
# route already has its own auth (X-Report-Secret).
REPORT_HEADERS_BASE = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
}


def extract_identifier(url: str):
    """Mirror of worker.js's normalizeTelegramInviteUrl, taken back apart."""
    url = url.strip()
    if "/+" in url:
        return "invite", url.split("/+", 1)[1]
    if "joinchat/" in url:
        return "invite", url.split("joinchat/", 1)[1]
    return "username", url.rstrip("/").split("/")[-1]


def _write_summary(status: str, detail: str, report_error: str):
    """Best-effort: surface the failure in the GitHub Actions Job Summary
    tab so it's visible without digging through raw logs."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a") as f:
            f.write(
                f"\n### \u26a0\ufe0f /report callback failed \u2014 queue_id={QUEUE_ID}\n"
                f"- Join result: **{status}** ({detail or 'no detail'})\n"
                f"- Report delivery error: `{report_error}`\n"
                f"- The Worker never saw this result \u2014 the user will get a "
                f"generic `stale_no_report_timeout` from the 15-min sweep "
                f"instead of the real reason.\n"
            )
    except OSError:
        pass


def report(status: str, detail: str = "") -> bool:
    """Returns True only if the Worker actually received this report."""
    print(f"[report] queue_id={QUEUE_ID} status={status} detail={detail}")
    if not REPORT_URL:
        return True  # nothing configured to deliver to -- not a failure

    try:
        payload = json.dumps(
            {"queue_id": QUEUE_ID, "status": status, "detail": detail}
        ).encode()
        req = urllib.request.Request(
            f"{REPORT_URL}/report",
            data=payload,
            headers={**REPORT_HEADERS_BASE, "X-Report-Secret": REPORT_SECRET},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"[report] callback returned {e.code}: {body}")
        print(
            f"::error title=Report callback failed::HTTP {e.code} for "
            f"queue_id={QUEUE_ID}: {body[:300]}"
        )
        _write_summary(status, detail, f"HTTP {e.code}: {body[:300]}")
        return False
    except Exception as e:
        print(f"[report] callback skipped: {e}")
        print(
            f"::error title=Report callback failed::{type(e).__name__} for "
            f"queue_id={QUEUE_ID}: {e}"
        )
        _write_summary(status, detail, f"{type(e).__name__}: {e}")
        return False


def join_with_floodwait_handling(client, kind, identifier):
    attempt = 0
    while True:
        try:
            if kind == "invite":
                client(ImportChatInviteRequest(identifier))
            else:
                client(JoinChannelRequest(identifier))
            return "joined", ""
        except UserAlreadyParticipantError:
            return "already_member", ""
        except FloodWaitError as e:
            attempt += 1
            if e.seconds > MAX_FLOODWAIT_AUTO_RETRY_SECONDS or attempt > MAX_FLOODWAIT_RETRIES:
                return "failed", f"floodwait_{e.seconds}s_exceeded_budget"
            print(
                f"FloodWait {e.seconds}s -- sleeping "
                f"(attempt {attempt}/{MAX_FLOODWAIT_RETRIES})"
            )
            time.sleep(e.seconds + 1)
        except (InviteHashExpiredError, InviteHashInvalidError):
            return "failed", "invite_hash_invalid_or_expired"
        except ChannelsTooMuchError:
            return "failed", "account_joined_too_many_channels"
        except ChannelPrivateError:
            return "failed", "channel_private_or_kicked"
        except UserBannedInChannelError:
            return "failed", "user_banned_in_channel"
        except RPCError as e:
            return "failed", f"rpc_error_{type(e).__name__}"


def main():
    kind, identifier = extract_identifier(GROUP_URL)
    print(f"queue_id={QUEUE_ID} kind={kind} identifier={identifier}")

    # join_with_floodwait_handling only anticipates specific Telethon errors.
    # Anything else (revoked session, auth key error, connection drop, a
    # Telethon version quirk we haven't seen) must NOT be allowed to crash
    # this script silently -- that leaves the row stuck at 'triggered' forever
    # with no /report ever sent and the user left wondering what happened.
    try:
        with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
            status, detail = join_with_floodwait_handling(client, kind, identifier)
    except Exception as e:
        status, detail = "failed", f"unexpected_error:{type(e).__name__}:{str(e)[:150]}"
        print(f"[unexpected] {detail}")

    reported = report(status, detail)

    # Two independent failure modes now both surface as a red CI run: the
    # join itself failing, and the join succeeding but the user never being
    # told because /report couldn't get through.
    if status == "failed" or not reported:
        sys.exit(1)


if __name__ == "__main__":
    main()
