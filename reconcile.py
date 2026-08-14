"""
食い違い検出。台帳の問い合わせの一種（別パイプラインではない）。
名寄せ(LLM) -> SQL突合(LLM不使用) -> benign判定(LLM) の3段。
新旧の決定(newest_claim_id)はSQLが行い、LLMは選べない（claude.md）。
"""

import json
import sys
import uuid
from datetime import date, datetime, timezone

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
                    "kind": {"type": ["string", "null"]},
                    "member_hints": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["canonical_name", "kind", "member_hints"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clusters"],
    "additionalProperties": False,
}

CLUSTER_SYSTEM = """建設プロジェクトの「対象物」表記（工事文書からの抜粋、表記ゆれあり）のリストが与えられる。
同一の物理的対象物・仕様項目を指す表記をグループ化してほしい。

- 正式名称・略称・別表記（例:「基礎配筋」「基礎鉄筋」「F1配筋」「異形棒鋼」）が同じ対象物を
  指していれば1クラスタにまとめる。文脈上明らかに違う対象物（別の部材・別の工区専用部材等）は
  別クラスタにする。
- 入力リストのすべての hint が、必ずどれか1つのクラスタの member_hints に含まれること。
- 判断に迷う場合は、まとめるより分ける方を優先する（誤って統合すると誤検出の原因になる）。
"""

BENIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "is_benign": {"type": "boolean"},
        "benign_reason": {"enum": ["scope_differs", "revision_ordered", "conditional", None]},
        "explanation": {"type": "string"},
    },
    "required": ["is_benign", "benign_reason", "explanation"],
    "additionalProperties": False,
}

BENIGN_SYSTEM = """複数の文書から抽出された、同一対象物・同一属性についての値の食い違い候補を判定する。

以下のいずれかに該当すれば正当な差異（is_benign=true）:
- 適用範囲（scope）が明確に異なる（例: 工区が違う）
- 改訂の順序として自然（新しい文書の値が最新の仕様であり、古い文書は単に更新されていないだけ
  ではなく、明確に「旧仕様として参照」等の文脈がある場合）
- 条件付きの記述（「◯◯の場合はX」）で、条件が異なれば異なる値になるのが自然

該当しなければ is_benign=false（未反映の疑い）。
explanation には、両方の claim の内容を踏まえた判定理由を1〜2文で書く。
"""


def resolve_objects(ledger: Ledger) -> dict:
    hints = [
        r["object_hint"]
        for r in ledger.conn.execute(
            "SELECT DISTINCT object_hint FROM claims WHERE object_hint IS NOT NULL"
        ).fetchall()
    ]
    if not hints:
        return {"hints": 0, "objects": 0, "cost": 0.0}

    with llm.client.messages.stream(
        model=llm.MODEL_THREAD,
        max_tokens=24000,
        system=CLUSTER_SYSTEM,
        messages=[{"role": "user", "content": "以下を分類してください:\n" + json.dumps(hints, ensure_ascii=False, indent=2)}],
        output_config={"format": {"type": "json_schema", "schema": CLUSTER_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()
    text = next(b.text for b in response.content if b.type == "text")
    clusters = json.loads(text)["clusters"]
    usage = llm.usage_dict(response)

    run_id = "run_" + uuid.uuid4().hex[:20]
    ledger.insert_run({
        "run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": "thread", "model": llm.MODEL_THREAD, "prompt_version": llm.PROMPT_VERSION,
    })

    hint_to_obj = {}
    for cluster in clusters:
        object_id = "obj_" + uuid.uuid4().hex[:20]
        aliases = [h for h in cluster["member_hints"] if h != cluster["canonical_name"]]
        ledger.conn.execute(
            "INSERT INTO objects (object_id, name, aliases, kind) VALUES (?, ?, ?, ?)",
            (object_id, cluster["canonical_name"], json.dumps(aliases, ensure_ascii=False), cluster.get("kind")),
        )
        for h in cluster["member_hints"]:
            hint_to_obj[h] = object_id

    for h in hints:
        if h not in hint_to_obj:
            object_id = "obj_" + uuid.uuid4().hex[:20]
            ledger.conn.execute(
                "INSERT INTO objects (object_id, name, aliases, kind) VALUES (?, ?, '[]', NULL)",
                (object_id, h),
            )
            hint_to_obj[h] = object_id

    for h, oid in hint_to_obj.items():
        ledger.conn.execute("UPDATE claims SET object_id = ? WHERE object_hint = ?", (oid, h))

    cost = llm.cost_usd(usage, llm.MODEL_THREAD)
    ledger.update_run(run_id, finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"], cost_usd=cost)
    ledger.conn.commit()
    return {"hints": len(hints), "objects": len(set(hint_to_obj.values())), "cost": cost}


def _newest(claims: list) -> dict:
    """新旧の決定はコードで行う。LLMは選べない。
    優先順位: effective_on 降順 -> revision 降順 -> 文書日付 降順。"""
    def key(c):
        return (c["effective_on"] or "", c["revision"] or "", c["doc_occurred_at"])
    return sorted(claims, key=key, reverse=True)[0]


def detect_discrepancies_sql(ledger: Ledger) -> list[dict]:
    # value_norm が null の定性的な仕様（型式名など）も比較対象に含めるため、
    # value_raw にフォールバックする。COALESCE の結果を compare_value として扱う。
    rows = ledger.conn.execute(
        """SELECT c.claim_id, c.object_id, c.attribute, COALESCE(c.value_norm, c.value_raw) AS compare_value,
                  c.scope, c.revision, c.effective_on, d.occurred_at AS doc_occurred_at
           FROM claims c JOIN documents d ON c.doc_id = d.doc_id
           WHERE c.object_id IS NOT NULL
           ORDER BY c.object_id, c.attribute"""
    ).fetchall()

    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["object_id"], r["attribute"]), []).append(dict(r))

    results = []
    for (object_id, attribute), claims in groups.items():
        distinct_values = {c["compare_value"] for c in claims}
        if len(distinct_values) < 2:
            continue

        distinct_scopes = {c["scope"] for c in claims if c["scope"]}
        if len(distinct_scopes) >= 2 and len(distinct_scopes) == len(claims):
            # 全件が異なる scope を持つ -> 全件が正当な差異
            results.append({
                "object_id": object_id, "attribute": attribute,
                "claim_ids": [c["claim_id"] for c in claims],
                "newest_claim_id": None, "stale_claim_ids": [],
                "auto_benign": True, "benign_reason": "scope_differs",
            })
            continue

        newest = _newest(claims)
        stale = [c for c in claims if c["claim_id"] != newest["claim_id"] and c["compare_value"] != newest["compare_value"]]
        if not stale:
            continue

        results.append({
            "object_id": object_id, "attribute": attribute,
            "claim_ids": [c["claim_id"] for c in claims],
            "newest_claim_id": newest["claim_id"],
            "stale_claim_ids": [c["claim_id"] for c in stale],
            "auto_benign": False, "benign_reason": None,
        })
    return results


def judge_benign(ledger: Ledger, candidate: dict) -> tuple[bool, str, str]:
    def load(cid):
        return dict(ledger.conn.execute(
            "SELECT object_surface, value_raw, scope, revision, effective_on, span_quote FROM claims WHERE claim_id = ?",
            (cid,)
        ).fetchone())

    newest = load(candidate["newest_claim_id"])
    stale = [load(cid) for cid in candidate["stale_claim_ids"]]

    prompt = json.dumps({"attribute": candidate["attribute"], "newest": newest, "stale_candidates": stale}, ensure_ascii=False, indent=2)
    response = llm.client.messages.create(
        model=llm.MODEL_RULE_TEXT,
        max_tokens=1024,
        system=BENIGN_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": BENIGN_SCHEMA}},
    )
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    return data["is_benign"], data.get("benign_reason"), data["explanation"], llm.usage_dict(response)


def main():
    ledger = Ledger()

    obj_stats = resolve_objects(ledger)
    print(f"名寄せ: {obj_stats['hints']} hints -> {obj_stats['objects']} objects cost=${obj_stats['cost']:.4f}", file=sys.stderr)

    candidates = detect_discrepancies_sql(ledger)
    print(f"SQL突合候補: {len(candidates)} 件", file=sys.stderr)

    run_id = "run_" + uuid.uuid4().hex[:20]
    ledger.insert_run({
        "run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": "reconcile", "model": llm.MODEL_RULE_TEXT, "prompt_version": llm.PROMPT_VERSION,
    })

    total_in = total_out = 0
    open_count = benign_count = 0

    for cand in candidates:
        if cand["auto_benign"]:
            ledger.conn.execute(
                """INSERT INTO discrepancies (discrepancy_id, run_id, object_id, attribute,
                   detection_method, status, claim_ids, newest_claim_id, stale_claim_ids,
                   benign_reason, explanation)
                   VALUES (?, ?, ?, ?, 'rule', 'benign', ?, NULL, '[]', ?, NULL)""",
                ("dsc_" + uuid.uuid4().hex[:20], run_id, cand["object_id"], cand["attribute"],
                 json.dumps(cand["claim_ids"], ensure_ascii=False), cand["benign_reason"]),
            )
            benign_count += 1
            continue

        is_benign, benign_reason, explanation, usage = judge_benign(ledger, cand)
        total_in += usage["input_tokens"]
        total_out += usage["output_tokens"]

        status = "benign" if is_benign else "open"
        if is_benign:
            ledger.conn.execute(
                """INSERT INTO discrepancies (discrepancy_id, run_id, object_id, attribute,
                   detection_method, status, claim_ids, newest_claim_id, stale_claim_ids,
                   benign_reason, explanation)
                   VALUES (?, ?, ?, ?, 'llm', ?, ?, ?, ?, ?, ?)""",
                ("dsc_" + uuid.uuid4().hex[:20], run_id, cand["object_id"], cand["attribute"], status,
                 json.dumps(cand["claim_ids"], ensure_ascii=False), cand["newest_claim_id"],
                 json.dumps(cand["stale_claim_ids"], ensure_ascii=False), benign_reason, explanation),
            )
            benign_count += 1
        else:
            # rule レーン: 新旧確定は SQL。explanation は NULL のまま。
            ledger.conn.execute(
                """INSERT INTO discrepancies (discrepancy_id, run_id, object_id, attribute,
                   detection_method, status, claim_ids, newest_claim_id, stale_claim_ids,
                   benign_reason, explanation)
                   VALUES (?, ?, ?, ?, 'rule', 'open', ?, ?, ?, NULL, NULL)""",
                ("dsc_" + uuid.uuid4().hex[:20], run_id, cand["object_id"], cand["attribute"],
                 json.dumps(cand["claim_ids"], ensure_ascii=False), cand["newest_claim_id"],
                 json.dumps(cand["stale_claim_ids"], ensure_ascii=False)),
            )
            open_count += 1

    cost = llm.cost_usd({"input_tokens": total_in, "output_tokens": total_out}, llm.MODEL_RULE_TEXT)
    ledger.update_run(run_id, finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       input_tokens=total_in, output_tokens=total_out, cost_usd=cost)
    ledger.conn.commit()
    ledger.close()

    print(f"完了: open={open_count} benign={benign_count} cost=${cost:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
