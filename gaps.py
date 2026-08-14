"""
空白検出。LLM を通さない（claude.md 設計原則）。
「答えられない」を信頼できる形で返せるのは、この層が決定的だから。
"""

import sys
import uuid
from datetime import date, datetime, timezone

from ledger import Ledger

NO_RECORD_DAYS = 30
UNRESOLVED_ISSUE_DAYS = 30
MIN_EVENTS_FOR_THREAD = 2

# 「今日」は date.today()（実時計）ではなく固定値にする。
# 実時計を使うと、同じ台帳に対して実行日によって結果が変わり、
# 「一度だけ生成し、以後は参照する」「再現性」の原則に反する。
# 分析の基準日はデータと同様に固定・記録される値であるべき。
AS_OF_DATE = date(2026, 8, 1)
# thread_hint が案件名ではなくトピック名になった単発イベントが、名寄せ後に
# 1イベントだけの疑似スレッドとして残ることがある（reference.md 既知の制限）。
# 空白検出はこの種のノイズに弱いため、最低イベント数で足切りする。


def detect_no_record_after(ledger: Ledger, today: date) -> list[dict]:
    rows = ledger.conn.execute(
        """SELECT t.thread_id, t.name, t.last_seen, t.status FROM threads t
           WHERE t.status != 'closed'
             AND (SELECT COUNT(*) FROM events e WHERE e.thread_id = t.thread_id) >= ?""",
        (MIN_EVENTS_FOR_THREAD,),
    ).fetchall()
    results = []
    for r in rows:
        last_seen = datetime.strptime(r["last_seen"][:10], "%Y-%m-%d").date()
        if (today - last_seen).days >= NO_RECORD_DAYS:
            results.append({
                "thread_id": r["thread_id"],
                "kind": "no_record_after",
                "period_start": r["last_seen"][:10],
                "period_end": None,
                "anchor_event_id": None,
                "description": f"{r['last_seen'][:10]}以降、この案件に関する記録がありません（{(today - last_seen).days}日間）。",
            })
    return results


def detect_unanswered_request(ledger: Ledger) -> list[dict]:
    rows = ledger.conn.execute(
        """SELECT r.event_id, r.thread_id, r.occurred_on, r.summary
           FROM events r
           WHERE r.kind = 'request' AND r.thread_id IS NOT NULL
             AND (SELECT COUNT(*) FROM events e WHERE e.thread_id = r.thread_id) >= ?
             AND NOT EXISTS (
               SELECT 1 FROM events c
               WHERE c.thread_id = r.thread_id
                 AND c.kind IN ('completion', 'decision')
                 AND c.occurred_on >= r.occurred_on
             )""",
        (MIN_EVENTS_FOR_THREAD,),
    ).fetchall()
    return [{
        "thread_id": r["thread_id"],
        "kind": "unanswered_request",
        "period_start": r["occurred_on"],
        "period_end": None,
        "anchor_event_id": r["event_id"],
        "description": f"{r['occurred_on']}の依頼「{r['summary']}」に対する完了・決定の記録がありません。",
    } for r in rows]


def detect_unresolved_issue(ledger: Ledger, today: date) -> list[dict]:
    rows = ledger.conn.execute(
        "SELECT event_id, thread_id, occurred_on, summary FROM events "
        "WHERE kind = 'issue' AND status = 'open' AND thread_id IS NOT NULL"
    ).fetchall()
    results = []
    for r in rows:
        occurred = datetime.strptime(r["occurred_on"][:10], "%Y-%m-%d").date()
        days = (today - occurred).days
        if days >= UNRESOLVED_ISSUE_DAYS:
            results.append({
                "thread_id": r["thread_id"],
                "kind": "unresolved_issue",
                "period_start": r["occurred_on"],
                "period_end": None,
                "anchor_event_id": r["event_id"],
                "description": f"{r['occurred_on']}の課題「{r['summary']}」が未解決のまま{days}日経過しています。",
            })
    return results


def main():
    ledger = Ledger()

    no_record = detect_no_record_after(ledger, AS_OF_DATE)
    unanswered = detect_unanswered_request(ledger)
    unresolved = detect_unresolved_issue(ledger, AS_OF_DATE)

    ledger.conn.execute("DELETE FROM gaps")
    for g in no_record + unanswered + unresolved:
        ledger.conn.execute(
            "INSERT INTO gaps (gap_id, thread_id, kind, period_start, period_end, anchor_event_id, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("gap_" + uuid.uuid4().hex[:20], g["thread_id"], g["kind"], g["period_start"],
             g["period_end"], g["anchor_event_id"], g["description"]),
        )
    ledger.conn.commit()
    ledger.close()

    print(f"no_record_after: {len(no_record)} / unanswered_request: {len(unanswered)} "
          f"/ unresolved_issue: {len(unresolved)}", file=sys.stderr)
    return no_record, unanswered, unresolved


if __name__ == "__main__":
    main()
