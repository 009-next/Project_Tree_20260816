"""L3 名寄せ。thread_hint の表記ゆれを1回のLLM呼び出しで統合する。"""

import json
import sys
import uuid
from datetime import datetime, timezone

from ledger import Ledger
import llm

CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {"type": "string"},
                    "member_hints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "この案件を指す thread_hint 表記のすべて（入力リストの部分集合）",
                    },
                },
                "required": ["canonical_name", "member_hints"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clusters"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """建設プロジェクトの案件名（表記ゆれあり）のリストが与えられる。
同一案件を指す表記をグループ化してほしい。

- 正式名称・略称・通称が同じ案件を指していれば、1つのクラスタにまとめる。
- 別案件であれば、1文字でも紛らわしくても別クラスタにする（工区が違う、対象施設が違う等の手がかりを見る）。
- 入力リストのすべての hint が、必ずどれか1つのクラスタの member_hints に含まれること（漏れなく分類する）。
- canonical_name は最も正式名称らしい表記を選ぶ。
"""


def build_threads():
    ledger = Ledger()
    hints = [
        r["thread_hint"]
        for r in ledger.conn.execute(
            "SELECT DISTINCT thread_hint FROM events WHERE thread_hint IS NOT NULL"
        ).fetchall()
    ]

    if not hints:
        print("thread_hint が存在しません", file=sys.stderr)
        return

    with llm.client.messages.stream(
        model=llm.MODEL_THREAD,
        max_tokens=24000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "以下を分類してください:\n" + json.dumps(hints, ensure_ascii=False, indent=2)}],
        output_config={"format": {"type": "json_schema", "schema": CLUSTER_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()
    text = next(b.text for b in response.content if b.type == "text")
    clusters = json.loads(text)["clusters"]
    usage = llm.usage_dict(response)

    run_id = "run_" + uuid.uuid4().hex[:20]
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger.insert_run({
        "run_id": run_id, "started_at": started_at, "stage": "thread",
        "model": llm.MODEL_THREAD, "prompt_version": llm.PROMPT_VERSION,
    })

    hint_to_thread = {}
    thread_rows = []
    for cluster in clusters:
        thread_id = "thr_" + uuid.uuid4().hex[:20]
        for h in cluster["member_hints"]:
            hint_to_thread[h] = thread_id
        aliases = [h for h in cluster["member_hints"] if h != cluster["canonical_name"]]
        thread_rows.append({
            "thread_id": thread_id,
            "name": cluster["canonical_name"],
            "aliases": aliases,
        })

    unmatched = [h for h in hints if h not in hint_to_thread]
    for h in unmatched:
        thread_id = "thr_" + uuid.uuid4().hex[:20]
        hint_to_thread[h] = thread_id
        thread_rows.append({"thread_id": thread_id, "name": h, "aliases": []})

    for t in thread_rows:
        member_hints = [h for h, tid in hint_to_thread.items() if tid == t["thread_id"]]
        placeholders = ",".join("?" for _ in member_hints)
        # first_seen/last_seen は「文書の日付」基準（documents.occurred_at）で計算する。
        # events.occurred_on はイベント内容が指す時期であり、将来予定への言及（例:
        # 4/20付文書内の「5月中の目標」）で日付が先走ることがあるため、
        # 「最後に記録を受け取ったのはいつか」という空白検出の意図には向かない。
        date_row = ledger.conn.execute(
            f"""SELECT MIN(d.occurred_at) AS first_seen, MAX(d.occurred_at) AS last_seen
                FROM events e JOIN documents d ON e.doc_id = d.doc_id
                WHERE e.thread_hint IN ({placeholders})""",
            member_hints,
        ).fetchone()
        ledger.conn.execute(
            "INSERT INTO threads (thread_id, name, aliases, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?, 'active')",
            (t["thread_id"], t["name"], json.dumps(t["aliases"], ensure_ascii=False),
             date_row["first_seen"], date_row["last_seen"]),
        )
        for h in member_hints:
            ledger.conn.execute(
                "UPDATE events SET thread_id = ? WHERE thread_hint = ?", (t["thread_id"], h)
            )

    ledger.conn.commit()

    cost = llm.cost_usd(usage, llm.MODEL_THREAD)
    ledger.update_run(
        run_id,
        finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        doc_count=0, event_count=0,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cost_usd=cost,
    )
    ledger.close()

    print(f"完了: {len(hints)} hints -> {len(thread_rows)} threads (未分類 {len(unmatched)}) cost=${cost:.4f}", file=sys.stderr)


if __name__ == "__main__":
    build_threads()
