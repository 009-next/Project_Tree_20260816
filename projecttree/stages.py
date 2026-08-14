"""
Phase 1: ストーリーテリング5段階分類（Enhancement.md 2-2）

設計原則:
  ルールベースを主軸にする。LLM は「精度を上げる任意の追加層」であり、
  無くても動くことを既定とする（API 上限中でもデモが成立する）。
  これは台帳側の「rule レーン / llm レーン」2レーン構成と同じ思想。

段階5「解決策・発展策の提案」の材料は、LLM ではなく
  patterns.detect_recurrence / detect_chain（SQLのみ）と gaps（SQLのみ）
から作る。つまり提案の根拠は常に機械的に追跡できる。
"""

import argparse
import sys
import uuid
from datetime import datetime, timezone

# clatest 直下のモジュールを import できるようにする
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402
import patterns  # noqa: E402

STAGE_TITLES = {
    1: "状況確認",
    2: "現状の課題",
    3: "試行錯誤",
    4: "課題・変化",
    5: "解決策、発展策の提案",
}

# Enhancement.md 2-2 の表に沿った、events.kind → 段階 の写像。
#   1.状況確認    : 現状がどのようなものか        → 事実の共有・完了報告
#   2.現状の課題  : なぜそのプロジェクトを始めたか → 懸念・問題の表面化
#   3.試行錯誤    : どのように進めようとしているか → 依頼・決定
#   4.課題・変化  : どのような問題に直面しているか → 工程/費用の変更（実際に起きた変化）
#   5.解決策・発展策                              → patterns / gaps から別途生成
KIND_TO_STAGE = {
    "info": 1,
    "completion": 1,
    "concern": 2,
    "issue": 2,
    "request": 3,
    "decision": 3,
    "schedule_change": 4,
    "cost_change": 4,
}


def classify_thread(ledger: Ledger, thread_id: str) -> dict[int, list[str]]:
    """1案件の events を5段階へ振り分ける。戻り値: {stage_no: [event_id, ...]}"""
    rows = ledger.conn.execute(
        "SELECT event_id, kind, occurred_on, summary FROM events "
        "WHERE thread_id = ? ORDER BY occurred_on",
        (thread_id,),
    ).fetchall()

    buckets: dict[int, list[str]] = {n: [] for n in range(1, 6)}
    for r in rows:
        stage_no = KIND_TO_STAGE.get(r["kind"])
        if stage_no:
            buckets[stage_no].append(r["event_id"])

    # 段階5: 提案の根拠は SQL 由来のみ（LLM を通さない）
    proposal_ids: list[str] = []

    for cand in patterns.detect_recurrence(ledger) + patterns.detect_chain(ledger):
        if cand["thread_id"] == thread_id:
            proposal_ids += cand["basis_event_ids"]

    gap_anchors = ledger.conn.execute(
        "SELECT anchor_event_id FROM gaps WHERE thread_id = ? AND anchor_event_id IS NOT NULL",
        (thread_id,),
    ).fetchall()
    proposal_ids += [g["anchor_event_id"] for g in gap_anchors]

    # 重複除去（順序は保つ）
    seen = set()
    buckets[5] = [e for e in proposal_ids if not (e in seen or seen.add(e))]
    return buckets


def summarize_stage(ledger: Ledger, thread_id: str, stage_no: int, event_ids: list[str]) -> str:
    """LLM を使わずに段階サマリを作る。件数と代表イベントの要約を並べるだけ。"""
    if not event_ids:
        return "該当する記録がありません。"

    placeholders = ",".join("?" for _ in event_ids)
    rows = ledger.conn.execute(
        f"SELECT summary, occurred_on FROM events WHERE event_id IN ({placeholders}) "
        f"ORDER BY occurred_on",
        event_ids,
    ).fetchall()

    if stage_no == 5:
        gaps = ledger.conn.execute(
            "SELECT COUNT(*) c FROM gaps WHERE thread_id = ?", (thread_id,)
        ).fetchone()["c"]
        head = f"検出された論点 {len(rows)} 件、記録の空白 {gaps} 件。"
    else:
        head = f"記録 {len(rows)} 件（{rows[0]['occurred_on']} 〜 {rows[-1]['occurred_on']}）。"

    bullets = "／".join(r["summary"][:40] for r in rows[:3])
    return f"{head} 主な内容: {bullets}"


def build_stages(ledger: Ledger, thread_id: str, method: str = "rule") -> int:
    """1案件分の stages / stage_events を作る。既存分は作り直す。"""
    old = ledger.conn.execute(
        "SELECT stage_id FROM stages WHERE thread_id = ?", (thread_id,)
    ).fetchall()
    for o in old:
        ledger.conn.execute("DELETE FROM stage_events WHERE stage_id = ?", (o["stage_id"],))
    ledger.conn.execute("DELETE FROM stages WHERE thread_id = ?", (thread_id,))

    buckets = classify_thread(ledger, thread_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    made = 0

    for stage_no in range(1, 6):
        event_ids = buckets[stage_no]
        stage_id = "stg_" + uuid.uuid4().hex[:20]
        ledger.conn.execute(
            "INSERT INTO stages (stage_id, thread_id, stage_no, title, summary, method, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stage_id, thread_id, stage_no, STAGE_TITLES[stage_no],
             summarize_stage(ledger, thread_id, stage_no, event_ids), method, now),
        )
        for eid in event_ids:
            ledger.conn.execute(
                "INSERT OR IGNORE INTO stage_events (stage_id, event_id) VALUES (?, ?)",
                (stage_id, eid),
            )
        made += 1

    ledger.commit()
    return made


def main():
    parser = argparse.ArgumentParser(description="5段階ストーリーテリング分類")
    parser.add_argument("--thread", default=None, help="thread_id。省略時は全案件")
    parser.add_argument("--min-events", type=int, default=2,
                        help="この件数未満のスレッドは疑似スレッドとみなしスキップ")
    args = parser.parse_args()

    ledger = Ledger()
    ledger.init_db()

    if args.thread:
        targets = [args.thread]
    else:
        rows = ledger.conn.execute(
            "SELECT t.thread_id FROM threads t "
            "WHERE (SELECT COUNT(*) FROM events e WHERE e.thread_id = t.thread_id) >= ?",
            (args.min_events,),
        ).fetchall()
        targets = [r["thread_id"] for r in rows]

    total = 0
    for tid in targets:
        name = ledger.conn.execute(
            "SELECT name FROM threads WHERE thread_id = ?", (tid,)
        ).fetchone()["name"]
        build_stages(ledger, tid)
        counts = ledger.conn.execute(
            "SELECT s.stage_no, COUNT(se.event_id) c FROM stages s "
            "LEFT JOIN stage_events se ON se.stage_id = s.stage_id "
            "WHERE s.thread_id = ? GROUP BY s.stage_no ORDER BY s.stage_no",
            (tid,),
        ).fetchall()
        dist = "/".join(str(c["c"]) for c in counts)
        print(f"  {name}: 段階別イベント数 {dist}", file=sys.stderr)
        total += 1

    ledger.close()
    print(f"完了: {total} 案件を5段階に分類", file=sys.stderr)


if __name__ == "__main__":
    main()
