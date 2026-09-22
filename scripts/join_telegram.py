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

Exit code: 0 on joined/already_member, 1 on any failure (surfaces in the
GitHub Actions run status; /report closes the D1 loop either way).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from telethon import TelegramClient
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


def extract_identifier(url: str):
    """Mirror of worker.js's normalizeTelegramInviteUrl, taken back apart."""
    url = url.strip()
    if "/+" in url:
        return "invite", url.split("/+", 1)[1]
    if "joinchat/" in url:
        return "invite", url.split("joinchat/", 1)[1]
    return "username", url.rstrip("/").split("/")[-1]


def report(status: str, detail: str = ""):
    print(f"[report] queue_id={QUEUE_ID} status={status} detail={detail}")
    if not REPORT_URL:
        return
    try:
        payload = json.dumps(
            {"queue_id": QUEUE_ID, "status": status, "detail": detail}
        ).encode()
        req = urllib.request.Request(
            f"{REPORT_URL}/report",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Report-Secret": REPORT_SECRET,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        print(f"[report] callback returned {e.code}: {e.read().decode(errors='replace')}")
    except Exception as e:
        print(f"[report] callback skipped: {e}")


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
    # this script silently — that leaves the row stuck at 'triggered' forever
    # with no /report ever sent and the user left wondering what happened.
    try:
        with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
            status, detail = join_with_floodwait_handling(client, kind, identifier)
    except Exception as e:
        status, detail = "failed", f"unexpected_error:{type(e).__name__}:{str(e)[:150]}"
        print(f"[unexpected] {detail}")

    report(status, detail)

    if status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
