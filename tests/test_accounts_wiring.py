"""Drift guard: every Telethon account must be wired in ALL four places.

Phase 18 adds a second drift guard at the bottom: the leave SCOPES (which reasons the
"Yes, Leave" and "read-only + view-only + paid" buttons may leave) live in BOTH
src/worker.js and scripts/leave_groups.py and must stay identical.

Adding an account means touching:
  1. scripts/tg_accounts.py   ACCOUNT_SECRETS   (secret-name routing)
  2. src/worker.js            ACCOUNTS          (pickers, validation, loops)
  3. .github/workflows/join.yml   choice options + env block
  4. .github/workflows/leave.yml  choice options + env block

Forgetting any one of them fails in a confusing way at run time (a GitHub
dispatch 422 for an unknown choice, an empty secret, a picker button that does
nothing). This test fails in CI instead. It parses the files as text -- no
telethon, no network -- so it runs anywhere:

    python3 tests/test_accounts_wiring.py
"""

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILURES = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def load_account_secrets():
    """ACCOUNT_SECRETS dict literal from tg_accounts.py, without importing it."""
    tree = ast.parse(read("scripts/tg_accounts.py"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ACCOUNT_SECRETS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("ACCOUNT_SECRETS not found in scripts/tg_accounts.py")


def load_worker_accounts():
    m = re.search(r"const ACCOUNTS = \[([^\]]*)\];", read("src/worker.js"))
    assert m, "const ACCOUNTS = [...] not found in src/worker.js"
    return re.findall(r'"([^"]+)"', m.group(1))


def load_workflow(rel):
    """(choice options for the `account` input, set of env names that map to secrets)."""
    text = read(rel)
    m = re.search(r"\n      account:\n(?:        .*\n)*?        options:\n((?:          - .*\n)+)", text)
    assert m, f"account choice options not found in {rel}"
    options = [line.strip()[2:].strip() for line in m.group(1).splitlines()]
    env = dict(re.findall(r"^\s+(\w+): \$\{\{ secrets\.(\w+) \}\}\s*$", text, flags=re.M))
    return options, env


secrets_map = load_account_secrets()
accounts = list(secrets_map)

# --- tg_accounts.py is the source of truth -------------------------------
check("at least the 5 known accounts are routed",
      {"CH", "JL", "HHNC", "NAWTHON", "NKST_NADI"} <= set(accounts), str(accounts))

all_names = [n for names in secrets_map.values() for n in names]
check("no secret name is shared between two accounts (no silent cross-use)",
      len(all_names) == len(set(all_names)),
      str([n for n in set(all_names) if all_names.count(n) > 1]))

check("every account has exactly 3 secrets (api id, api hash, session)",
      all(len(v) == 3 for v in secrets_map.values()))

check("every secret name is a valid GitHub secret name ([A-Za-z0-9_], no GITHUB_ prefix)",
      all(re.fullmatch(r"[A-Za-z0-9_]+", n) and not n.upper().startswith("GITHUB_") for n in all_names))

check("account names contain no ':' (the Worker splits callback_data on ':')",
      all(":" not in a for a in accounts))

check("NKST_NADI uses the secret names the owner created",
      secrets_map.get("NKST_NADI") == ("NKST_NADI_API_ID", "NKST_NADI_API_HASH", "NKST_NADI_STRING_SESSION"),
      str(secrets_map.get("NKST_NADI")))

# --- Worker ----------------------------------------------------------------
check("worker.js ACCOUNTS == tg_accounts.py accounts (same set, same order)",
      load_worker_accounts() == accounts, f"worker={load_worker_accounts()} py={accounts}")

# worst case callback_data: leaveconfirm:<ACCOUNT>:<scan_id>:yes -- Telegram limit is 64 bytes
longest = max(len(f"leaveconfirm:{a}:9999999999:yes".encode()) for a in accounts)
check("longest callback_data stays under Telegram's 64-byte limit", longest <= 64, str(longest))

# --- Workflows ---------------------------------------------------------------
for wf in (".github/workflows/join.yml", ".github/workflows/leave.yml"):
    options, env = load_workflow(wf)
    check(f"{wf}: account choice options == accounts", options == accounts, f"{options} vs {accounts}")
    wired = {n for n in all_names if env.get(n) == n}
    missing = [n for n in all_names if n not in wired]
    check(f"{wf}: every account secret is passed to the job (env name == secret name)",
          not missing, f"missing/mismatched: {missing}")


# --- Phase 18: leave scopes must agree between the Worker and the Python script ----------------------
def py_scopes():
    tree = ast.parse(read("scripts/leave_groups.py"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LEAVE_SCOPES" for t in node.targets):
            out = {}
            for k, v in zip(node.value.keys, node.value.values):
                # frozenset({...}) -> the set literal inside the call
                out[k.value] = set(ast.literal_eval(v.args[0]))
            return out
    raise AssertionError("LEAVE_SCOPES not found in scripts/leave_groups.py")


def js_scopes():
    text = read("src/worker.js")
    block = re.search(r"const LEAVE_SCOPES = \{(.*?)\n\};", text, flags=re.S)
    assert block, "const LEAVE_SCOPES = {...} not found in src/worker.js"
    return {name: set(re.findall(r'"([a-z_0-9]+)"', arr))
            for name, arr in re.findall(r"(\w+): \[([^\]]*)\]", block.group(1))}


def js_reasons():
    m = re.search(r"const LEAVE_REASONS = \[([^\]]*)\];", read("src/worker.js"))
    return set(re.findall(r'"([a-z_0-9]+)"', m.group(1)))


py, js = py_scopes(), js_scopes()
# worker "yes" (the original button) is "all" in the script; "rvp" is the same name on both sides
check("Worker 'yes' scope == script 'all' scope (same reasons)", js.get("yes") == py.get("all"), f"js={js.get('yes')} py={py.get('all')}")
check("Worker 'rvp' scope == script 'rvp' scope (same reasons)", js.get("rvp") == py.get("rvp"), f"js={js.get('rvp')} py={py.get('rvp')}")
check("rvp scope never contains 'muted'", "muted" not in py["rvp"] and "muted" not in js["rvp"])
check("original scope never contains 'view_only' (big view-only channels are rvp-only)", "view_only" not in py["all"] and "view_only" not in js["yes"])
check("every reason a scope mentions is a known reason", (py["all"] | py["rvp"]) <= js_reasons() and js_reasons() == (py["all"] | py["rvp"]) | {"muted"} | {"view_only"}, str(js_reasons()))

leave_yml = read(".github/workflows/leave.yml")
m = re.search(r"\n      scope:\n(?:        .*\n)*?        options:\n((?:          - .*\n)+)", leave_yml)
scope_options = [l.strip()[2:].strip() for l in m.group(1).splitlines()] if m else []
check("leave.yml `scope` choice options == script scope names", scope_options == list(py), f"{scope_options} vs {list(py)}")
check("leave.yml passes SCOPE to the script", re.search(r"^\s+SCOPE: \$\{\{ inputs\.scope \}\}\s*$", leave_yml, flags=re.M) is not None)
dispatched = set(re.findall(r'dispatchLeaveWorkflow\(env, account, "leave", scanId, [^?]*\? "(\w+)" : "(\w+)"\)', read("src/worker.js"))[0]) if re.search(r'dispatchLeaveWorkflow\(env, account, "leave"', read("src/worker.js")) else set()
check("every scope value the Worker can dispatch is a valid leave.yml option", dispatched and dispatched <= set(scope_options), f"{dispatched} vs {scope_options}")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("all passed")
