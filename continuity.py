"""SQLite chat continuity, separate from the user-visible history."""
import json

# Conservative context-payload budgets in UTF-8 bytes (including JSON markup).
CONTEXT_BUDGET = 24_000
SUMMARY_BATCH = 12
RECENT_TURNS = 8
SUMMARY_MAX = 2_800

SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity (
 id INTEGER PRIMARY KEY CHECK(id=1),
 epoch INTEGER NOT NULL DEFAULT 0,
 floor INTEGER NOT NULL DEFAULT 0,
 covered INTEGER NOT NULL DEFAULT 0,
 summary TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO continuity(id) VALUES(1);
CREATE INDEX IF NOT EXISTS chat_role_id ON messages(role,id);
CREATE TABLE IF NOT EXISTS abandoned_turns(
 message_id INTEGER PRIMARY KEY REFERENCES messages(id)
);
"""

def state(con):
    return dict(con.execute("SELECT epoch,floor,covered,summary FROM continuity WHERE id=1").fetchone())

def retire_context(con, cut=None):
    # Removing passages by matching words misses paraphrases and assistant echoes.
    # Keep the original chat for the user, but start model-visible context cleanly.
    if cut is None:
        cut = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    else:
        cut = max(cut, state(con)["floor"])
    con.execute("UPDATE continuity SET epoch=epoch+1,floor=?,covered=?,summary='' WHERE id=1", (cut,cut))

def reset_summary(con):
    con.execute("UPDATE continuity SET epoch=epoch+1,covered=floor,summary='' WHERE id=1")

def payload_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

def complete_recent(con, floor, covered, user_id):
    pairs = con.execute("""SELECT u.id, u.text AS question, a.text AS answer
        FROM messages u JOIN messages a ON a.reply_to=u.id
        WHERE u.role='user' AND u.id>? AND u.id<? AND a.id>?
        ORDER BY u.id DESC LIMIT ?""", (max(floor,covered),user_id,floor,RECENT_TURNS)).fetchall()
    entries = [(p["id"],[{"role":"user","text":p["question"]},
                          {"role":"assistant","text":p["answer"]}]) for p in pairs]
    # If the user is answering today's proactive question, the model needs
    # to see that question even though it is not paired with a user message.
    openings = con.execute("""SELECT id,text FROM messages
        WHERE role='followup' AND id>? AND id<?
        ORDER BY id DESC LIMIT 2""",(max(floor,covered),user_id))
    entries.extend((p["id"],[{"role":"assistant","text":p["text"]}]) for p in openings)
    return [turn for _,turn in sorted(entries,key=lambda pair:pair[0],reverse=True)[:RECENT_TURNS]]

def assemble(con, snap, user_id, context, budget=CONTEXT_BUDGET):
    # Preserve user input first, then active fact/event candidates. Optional parts
    # cannot expand the request beyond the configured payload budget.
    out = {**context,"facts":[],"events":[],"recent":[]}
    for field in ("facts","events"):
        for entry in context[field]:
            out[field].append(entry)
            if payload_bytes(out)>budget:
                out[field].pop()
    if snap["summary"]:
        out["history_summary"] = snap["summary"]
        if payload_bytes(out)>budget:
            out.pop("history_summary")
    for turn in complete_recent(con,snap["floor"],snap["covered"],user_id):
        out["recent"] = turn + out["recent"]
        if payload_bytes(out)>budget:
            out["recent"] = out["recent"][len(turn):]
            break
    return out

def compaction_input(con):
    snap = state(con)
    # Include unresolved rows so we cannot advance past a delayed/retried turn.
    rows = con.execute("""SELECT u.id,u.text AS question,a.text AS answer
        FROM messages u LEFT JOIN messages a ON a.reply_to=u.id AND a.id>?
        WHERE u.role='user' AND u.id>? AND NOT EXISTS(
            SELECT 1 FROM abandoned_turns d WHERE d.message_id=u.id) ORDER BY u.id LIMIT ?""",
        (snap["floor"],max(snap["floor"],snap["covered"]),SUMMARY_BATCH+RECENT_TURNS)).fetchall()
    if len(rows) < SUMMARY_BATCH+RECENT_TURNS:
        return None
    turns = []
    data = {"type":"summary","previous_summary":snap["summary"],"turns":turns}
    end = snap["covered"]
    for row in rows[:SUMMARY_BATCH]:
        if row["answer"] is None:
            break
        turns.append({"source_id":row["id"],"user":row["question"],"assistant":row["answer"]})
        if payload_bytes(data)>CONTEXT_BUDGET:
            turns.pop()
            break
        end = row["id"]
    if not turns:
        return None
    return snap, end, data

def summary_result(result):
    """Normalize an explicit no-op or a usable summary. None signals invalid output."""
    if not isinstance(result, dict):
        return None
    if set(result) == {"no_summary"} and result["no_summary"] is True:
        return {"no_summary": True}
    if set(result) != {"summary"}:
        return None
    summary = result["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary.encode("utf-8")) > SUMMARY_MAX:
        return None
    return {"summary": summary.strip()}


def commit_summary(con, snap, end, result):
    normalized = summary_result(result)
    if normalized is None:
        return False
    if normalized.get("no_summary") is True:
        # A successful review of trivial turns advances coverage but cannot erase context.
        return bool(con.execute("""UPDATE continuity SET covered=?
            WHERE id=1 AND epoch=? AND covered=?""",
            (end,snap["epoch"],snap["covered"])).rowcount)
    return bool(con.execute("""UPDATE continuity SET covered=?,summary=?
        WHERE id=1 AND epoch=? AND covered=?""",
        (end,normalized["summary"],snap["epoch"],snap["covered"])).rowcount)
