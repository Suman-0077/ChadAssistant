"""Regression suite for every finding in docs/m2-review.md.

Each check below corresponds to a real bug that existed and was fixed. If
one starts failing, a guarantee the approval system depends on has been
lost — most of them silently, which is why they're here.

Runs against a throwaway vault in a temp dir. Stubs the `anthropic` client
so no API key and no network are needed. Takes under a second.

    python tests/test_m2_regressions.py

Exits non-zero on any failure, so it works as a pre-commit / CI step.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

# --- Sandbox setup: must happen BEFORE importing chad -----------------------
# config.py validates env vars at import time and exits if any are missing.

_TMP = Path(tempfile.mkdtemp(prefix="chad-test-"))
_VAULT = _TMP / "vault"
_VAULT.mkdir(parents=True)

os.environ.update(
    ANTHROPIC_API_KEY="sk-test-not-real",
    TELEGRAM_BOT_TOKEN="1:test",
    ALLOWED_TELEGRAM_ID="111",
    VAULT_PATH=str(_VAULT),
    STATE_DIR=str(_TMP / "state"),
    USER_TIMEZONE="Australia/Sydney",
)

# Stub the third-party imports the app makes at import time, so the suite runs
# on a bare `python3` with no venv and no installed dependencies.
#
# `dotenv` is stubbed to a no-op deliberately, not just for convenience: the
# real load_dotenv() would read the project's .env, and a stray VAULT_PATH in
# there could point these destructive-ish tests at the live vault. The stub
# makes that impossible rather than relying on load_dotenv's precedence rules.
_fake_dotenv = types.ModuleType("dotenv")
_fake_dotenv.load_dotenv = lambda *a, **kw: False
sys.modules.setdefault("dotenv", _fake_dotenv)

# brain.py constructs an Anthropic client at import time. We never call it.
_fake_anthropic = types.ModuleType("anthropic")


class _FakeClient:
    def __init__(self, **kwargs):
        self.messages = types.SimpleNamespace(create=lambda **kw: None)


_fake_anthropic.Anthropic = _FakeClient
sys.modules.setdefault("anthropic", _fake_anthropic)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chad import brain, history, proposals, vault  # noqa: E402
from chad import reminders  # noqa: E402,F401  (registers the executor)

CHAT = 999
_results: list[tuple[bool, str, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    _results.append((bool(passed), name, detail))
    print(("  ok   " if passed else "  FAIL ") + name)
    if not passed and detail:
        print(f"         {detail}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def _fresh_pending(**args) -> str:
    """Queue a proposal and mark its buttons as delivered."""
    pid = proposals.STORE.add("add_reminder", args or
                              {"date": "2026-08-07", "text": "test item"}, CHAT)
    proposals.STORE.set_message_id(pid, 1)
    return pid


def _reminders_text() -> str:
    p = _VAULT / "reminders.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# === C1 — approve_pending had no gates at all ===============================
# The model could propose an action, read the pid back from the tool result,
# and approve it in the same turn, before any button was ever sent.

def test_c1_no_model_facing_approval_tools():
    section("C1: the model has no code path to approval")
    names = [t["name"] for t in brain.TOOLS]
    check("approve_pending is not a tool", "approve_pending" not in names, str(names))
    check("reject_pending is not a tool", "reject_pending" not in names, str(names))
    check("_run_tool rejects approve_pending",
          brain._run_tool("approve_pending", {"pid": "x"}, CHAT).startswith("Unknown tool"))


def test_c1_execute_gates():
    section("C1: execute() enforces every gate")

    # Same-turn self-approval: proposal exists but buttons were never sent.
    out = brain._run_tool("propose_action", {
        "kind": "add_reminder",
        "args": {"date": "2026-12-25", "text": "SAME_TURN"},
        "summary": "ignored — derived server-side",
    }, CHAT)
    pid = out.split()[1]
    check("message_id gate blocks unshown proposals",
          _raises(pid, CHAT) and "SAME_TURN" not in _reminders_text(),
          "a proposal the user never saw must never execute")

    proposals.STORE.set_message_id(pid, 1)

    proposals.STORE.set_status(pid, proposals.STATUS_REJECTED)
    check("rejected proposals cannot execute", _raises(pid, CHAT))

    proposals.STORE._data[pid].update(status=proposals.STATUS_PENDING,
                                      expires=time.time() - 1)
    check("expired proposals cannot execute", _raises(pid, CHAT))

    proposals.STORE._data[pid].update(status=proposals.STATUS_PENDING,
                                      expires=time.time() + 3600)
    check("cross-chat approval is refused", _raises(pid, CHAT + 1))

    proposals.STORE.execute(pid, chat_id=CHAT)
    check("the same proposal cannot execute twice",
          _raises(pid, CHAT) and _reminders_text().count("SAME_TURN") == 1,
          f"{_reminders_text().count('SAME_TURN')} lines written")


def _raises(pid: str, chat_id: int) -> bool:
    try:
        proposals.STORE.execute(pid, chat_id=chat_id)
        return False
    except proposals.ProposalError:
        return True


# === C2 — reminders.md was writable by generic tools ========================
# add_reminder isn't exposed as a tool, but append_note reached the file
# directly, making the whole approval flow optional.

def test_c2_protected_notes():
    section("C2: guarded files are unreachable from generic write tools")
    for note, label in [("reminders.md", "reminders.md"), ("memory.md", "memory.md")]:
        for tool, args in [
            ("append_note", {"note_name": note, "text": "DIRECT_WRITE"}),
            ("create_note", {"note_name": note, "text": "DIRECT_WRITE"}),
            ("edit_section", {"note_name": note, "section_heading": "Identity",
                              "new_body": "DIRECT_WRITE"}),
        ]:
            out = brain._run_tool(tool, args, CHAT)
            check(f"{label} refuses {tool}", "protected" in out, out[:100])
    check("no direct write reached reminders.md",
          "DIRECT_WRITE" not in _reminders_text())


# === H1 — preview and payload were independent model-supplied fields ========

def test_h1_summary_is_server_derived():
    section("H1: the preview is derived from the args that execute")
    pid = proposals.STORE.add(
        "add_reminder", {"date": "2026-08-07", "text": "submit assignment"}, CHAT)
    summary = proposals.STORE.get(pid)["summary"]
    check("summary renders the real args",
          "submit assignment" in summary and "2026-08-07" in summary, summary)

    # The model's own `summary` argument must be ignored entirely.
    out = brain._run_tool("propose_action", {
        "kind": "add_reminder",
        "args": {"date": "2030-01-01", "text": "actual payload"},
        "summary": "Add reminder: something completely different",
    }, CHAT)
    shown = proposals.STORE.get(out.split()[1])["summary"]
    check("model-supplied summary is discarded",
          "completely different" not in shown and "actual payload" in shown, shown)


# === H3 / M1 / M3 / M6 — store robustness ===================================

def test_h3_malformed_entry_survives():
    section("H3: one bad entry cannot take the bot down")
    proposals.STORE._data["broken"] = {"pid": "broken", "kind": "add_reminder"}
    try:
        proposals.STORE.visible_for_chat(CHAT)
        ok, detail = True, ""
    except Exception as e:                                     # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {e} in system-prompt assembly"
    finally:
        proposals.STORE._data.pop("broken", None)
    check("visible_for_chat tolerates a malformed entry", ok, detail)


def test_m1_args_validated_before_the_human_sees_them():
    section("M1/L1/L2: bad args are rejected at propose time")
    for args, label in [
        ({"totally": "wrong"}, "unknown keys"),
        ({"date": "9999-99-99", "text": "x"}, "impossible date"),
        ({"date": "not-a-date", "text": "x"}, "malformed date"),
        ({"date": "2026-01-01", "text": ""}, "empty text"),
        ({"date": "2026-01-01", "text": "a | b"}, "field separator in text"),
    ]:
        out = brain._run_tool(
            "propose_action", {"kind": "add_reminder", "args": args, "summary": "x"}, CHAT)
        check(f"{label} rejected before queueing", out.startswith("Error"), out[:100])

    out = brain._run_tool(
        "propose_action", {"kind": "nonexistent", "args": {}, "summary": "x"}, CHAT)
    check("unknown kind rejected", out.startswith("Error"), out[:100])


def test_m3_corrupt_file_is_preserved():
    section("M3: a corrupt store is preserved, not destroyed")
    path = Path(os.environ["STATE_DIR"]) / "corrupt_probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    proposals.ProposalStore(path)
    preserved = list(path.parent.glob("corrupt_probe.json.corrupt.*"))
    check("corrupt file renamed aside", bool(preserved),
          "the next atomic save would otherwise os.replace over the evidence")


def test_m6_get_returns_a_copy():
    section("M6: callers cannot mutate stored proposals")
    pid = _fresh_pending()
    snapshot = proposals.STORE.get(pid)
    snapshot["args"]["text"] = "MUTATED"
    snapshot["status"] = "done"
    stored = proposals.STORE._data[pid]
    check("get() returns a deep copy",
          stored["args"]["text"] != "MUTATED" and stored["status"] != "done",
          "the returned dict defines what executes; it must not be live state")


# === H2 — history structure ==================================================

def test_h2_history_structure():
    section("H2: history stays structurally valid")
    store = history.HistoryStore(Path(os.environ["STATE_DIR"]) / "hist_probe.json")
    cid = 42

    # A trim must never leave a leading orphaned tool_result.
    orphan = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "data"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]
    trimmed = history._trim(orphan)
    check("leading orphaned tool_result is stripped",
          not trimmed or history._is_valid_opener(trimmed[0]),
          "the API rejects a message list opening with a tool_result")

    # Consecutive same-role entries must not be persisted.
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": "[approved proposal ab12]"},
        {"role": "user", "content": "[approved proposal cd34]"},
        {"role": "user", "content": "thanks"},
    ]
    store.set(cid, msgs)
    roles = [m["role"] for m in store.get(cid)]
    runs = [i for i in range(len(roles) - 1) if roles[i] == roles[i + 1]]
    check("no same-role run survives a save", not runs, f"roles={roles}")

    # Every tool_use must keep a matching tool_result after any repair.
    with_tools = [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "read_note", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "data"}]},
        {"role": "user", "content": "[approved proposal xy]"},
    ]
    out = history._trim(with_tools)
    uses = _count_blocks(out, "assistant", "tool_use")
    results = _count_blocks(out, "user", "tool_result")
    check("tool_use blocks keep their tool_result",
          uses <= results,
          f"{uses} tool_use vs {results} tool_result — a flattened batch orphans "
          f"the call and the API rejects the request")


def _count_blocks(messages: list[dict], role: str, block_type: str) -> int:
    n = 0
    for m in messages:
        if m.get("role") == role and isinstance(m.get("content"), list):
            n += sum(1 for b in m["content"]
                     if isinstance(b, dict) and b.get("type") == block_type)
    return n


# === Path jail (pre-M2, but the foundation everything else assumes) =========

def test_path_jail():
    section("Jail: no write escapes the vault")
    for bad in ["../../etc/passwd.md", "/etc/passwd.md", "notes/../../escape.md",
                "secrets.txt", "", "sub/../../../../tmp/x.md"]:
        out = brain._run_tool("read_note", {"note_name": bad}, CHAT)
        check(f"read_note refuses {bad!r}", out.startswith("Error:"), out[:90])


# === Consistency between the tool schema and the executor registry ==========

def test_schema_matches_registry():
    section("Schema: advertised kinds match registered executors")
    schema = next(t for t in brain.TOOLS if t["name"] == "propose_action")
    advertised = set(schema["input_schema"]["properties"]["kind"]["enum"])
    registered = set(proposals.known_kinds())
    check("propose_action enum == known_kinds()", advertised == registered,
          f"advertised={sorted(advertised)} registered={sorted(registered)} — "
          f"a registered kind the model is never told about is unusable")


def main() -> int:
    print("Chad M2 regression suite")
    print(f"sandbox vault: {_VAULT}")

    for fn in [
        test_c1_no_model_facing_approval_tools,
        test_c1_execute_gates,
        test_c2_protected_notes,
        test_h1_summary_is_server_derived,
        test_h3_malformed_entry_survives,
        test_m1_args_validated_before_the_human_sees_them,
        test_m3_corrupt_file_is_preserved,
        test_m6_get_returns_a_copy,
        test_h2_history_structure,
        test_path_jail,
        test_schema_matches_registry,
    ]:
        fn()

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = [name for ok, name, _ in _results if not ok]
    print(f"\n{passed}/{len(_results)} checks passed")
    if failed:
        print("\nFAILED:")
        for name in failed:
            print(f"  - {name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
