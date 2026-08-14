"""
L4 洞察生成。2レーン構成。
rule: patterns.py が確定した根拠を言語化するだけ（LLM は根拠を選べない）。
llm : イベントストアを渡して自由探索する仮説レーン。horizon 必須で反証可能性を強制する。
生成後は schema.json 相当の制約で検証し、違反は破棄する（claude.md 設計原則）。
"""

import json
import sys
import uuid
from datetime import datetime, timezone

from ledger import Ledger
import llm
import patterns

RULE_VERBALIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {"type": "string"},
    },
    "required": ["statement"],
    "additionalProperties": False,
}

RULE_VERBALIZE_SYSTEM = """検出済みのパターンを1〜2文の日本語に言語化する。
根拠イベントの選択や解釈の変更は禁止。与えられた pattern_evidence の数値
（occurrences・dates・interval_days_mean 等）を必ず文中に含めること。
一般論・助言は書かない。事実の記述のみ。"""

LLM_EXPLORE_SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "type": {"enum": ["observation", "pattern", "forecast", "advice"]},
                    "label": {"enum": ["fact", "interpretation", "prediction"]},
                    "statement": {"type": "string"},
                    "basis_event_ids": {"type": "array", "items": {"type": "string"}},
                    "basis_rule": {"type": ["string", "null"]},
                    "horizon": {"type": ["string", "null"]},
                },
                "required": ["thread_id", "type", "label", "statement", "basis_event_ids", "basis_rule", "horizon"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["insights"],
    "additionalProperties": False,
}

LLM_EXPLORE_SYSTEM = """業務イベントの時系列から、SQLでは見つけられない意味的・因果的な気づきを探す
（例: 担当者交代と応答遅延の関係、別案件の遅延がこの案件に波及している兆候、
見積の前提が過去の記述と変わっている兆候）。

制約（違反すると生成しても破棄される）:
- label="prediction" にする場合、basis_event_ids は同一 thread_id 内の 2 件以上、
  かつ horizon（いつまでに検証可能か）を必ず書く。根拠を示せない予測は書かない。
- 「連携を密にする」「注意が必要」のような一般論は禁止。具体的な事実の組み合わせから
  導ける主張のみ書く。
- 該当する気づきがなければ、無理に insights を埋めない。空配列を返してよい。
"""


def verbalize_rule_pattern(cand: dict) -> tuple[str, dict]:
    ev = cand["pattern_evidence"]
    prompt = json.dumps({"pattern_type": cand["pattern_type"], "pattern_evidence": ev}, ensure_ascii=False)
    response = llm.client.messages.create(
        model=llm.MODEL_RULE_TEXT,
        max_tokens=512,
        system=RULE_VERBALIZE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": RULE_VERBALIZE_SCHEMA}},
    )
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    return data["statement"], llm.usage_dict(response)


def validate_rule_insight(statement: str, pattern_evidence: dict) -> bool:
    """pattern_evidence の主要数値が statement に含まれているか機械検証する。"""
    occ = str(pattern_evidence.get("occurrences", ""))
    return occ != "" and occ in statement


def run_rule_lane(ledger: Ledger, run_id: str) -> dict:
    recurrence = patterns.detect_recurrence(ledger)
    chain = patterns.detect_chain(ledger)

    total_in = total_out = 0
    kept = rejected = 0

    for cand in recurrence + chain:
        statement, usage = verbalize_rule_pattern(cand)
        total_in += usage["input_tokens"]
        total_out += usage["output_tokens"]

        if not validate_rule_insight(statement, cand["pattern_evidence"]):
            rejected += 1
            continue

        # label='fact' が正しい。ruleレーンは「確定した過去の反復パターン」を報告するもので、
        # 予測ではない。schema.json 上も horizon（予測固有）は rule レーンに存在しない。
        # 「これは予測だ」と主張したいのは llm レーン側の役割。
        ledger.conn.execute(
            """INSERT INTO insights (insight_id, run_id, generated_at, thread_id, detection_method,
               type, label, statement, basis_event_ids, pattern_type, pattern_evidence,
               basis_rule, horizon)
               VALUES (?, ?, ?, ?, 'rule', 'pattern', 'fact', ?, ?, ?, ?, NULL, NULL)""",
            ("ins_" + uuid.uuid4().hex[:20], run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             cand["thread_id"], statement, json.dumps(cand["basis_event_ids"], ensure_ascii=False),
             cand["pattern_type"], json.dumps(cand["pattern_evidence"], ensure_ascii=False)),
        )
        kept += 1

    return {"kept": kept, "rejected": rejected, "input_tokens": total_in, "output_tokens": total_out}


def _validate_llm_insight(ins: dict, valid_event_ids: set, event_thread_map: dict) -> bool:
    if len(ins["basis_event_ids"]) < 2:
        return False
    if not ins.get("basis_rule") or not ins.get("horizon"):
        return False
    if not all(eid in valid_event_ids for eid in ins["basis_event_ids"]):
        return False
    threads = {event_thread_map.get(eid) for eid in ins["basis_event_ids"]}
    if len(threads) != 1 or None in threads:
        return False
    if ins["thread_id"] not in threads:
        return False
    return True


def run_llm_lane(ledger: Ledger, run_id: str) -> dict:
    events = ledger.all_events()
    valid_event_ids = {e["event_id"] for e in events}
    event_thread_map = {e["event_id"]: e["thread_id"] for e in events}

    event_store = [
        {"event_id": e["event_id"], "thread_id": e["thread_id"], "occurred_on": e["occurred_on"],
         "kind": e["kind"], "summary": e["summary"]}
        for e in events if e["thread_id"] is not None
    ]

    with llm.client.messages.stream(
        model=llm.MODEL_EXPLORE,
        max_tokens=16000,
        system=[{"type": "text", "text": LLM_EXPLORE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "イベントストア:\n" + json.dumps(event_store, ensure_ascii=False)}],
        output_config={"format": {"type": "json_schema", "schema": LLM_EXPLORE_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()

    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    usage = llm.usage_dict(response)

    kept = rejected = 0
    for ins in data["insights"]:
        if not _validate_llm_insight(ins, valid_event_ids, event_thread_map):
            rejected += 1
            continue
        ledger.conn.execute(
            """INSERT INTO insights (insight_id, run_id, generated_at, thread_id, detection_method,
               type, label, statement, basis_event_ids, pattern_type, pattern_evidence,
               basis_rule, horizon)
               VALUES (?, ?, ?, ?, 'llm', ?, ?, ?, ?, NULL, NULL, ?, ?)""",
            ("ins_" + uuid.uuid4().hex[:20], run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             ins["thread_id"], ins["type"], ins["label"], ins["statement"],
             json.dumps(ins["basis_event_ids"], ensure_ascii=False), ins["basis_rule"], ins["horizon"]),
        )
        kept += 1

    return {"kept": kept, "rejected": rejected, **usage}


def main():
    ledger = Ledger()
    run_id = "run_" + uuid.uuid4().hex[:20]
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger.insert_run({
        "run_id": run_id, "started_at": started_at, "stage": "insight",
        "model": f"{llm.MODEL_RULE_TEXT}+{llm.MODEL_EXPLORE}", "prompt_version": llm.PROMPT_VERSION,
    })

    rule_stats = run_rule_lane(ledger, run_id)
    print(f"rule レーン: kept={rule_stats['kept']} rejected={rule_stats['rejected']}", file=sys.stderr)

    llm_stats = run_llm_lane(ledger, run_id)
    print(f"llm レーン: kept={llm_stats['kept']} rejected={llm_stats['rejected']}", file=sys.stderr)

    total_in = rule_stats["input_tokens"] + llm_stats["input_tokens"]
    total_out = rule_stats["output_tokens"] + llm_stats["output_tokens"]
    cost = (llm.cost_usd({"input_tokens": rule_stats["input_tokens"], "output_tokens": rule_stats["output_tokens"]}, llm.MODEL_RULE_TEXT)
            + llm.cost_usd({"input_tokens": llm_stats["input_tokens"], "output_tokens": llm_stats["output_tokens"]}, llm.MODEL_EXPLORE))

    ledger.update_run(run_id, finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       rejected_count=rule_stats["rejected"] + llm_stats["rejected"],
                       input_tokens=total_in, output_tokens=total_out, cost_usd=cost)
    ledger.conn.commit()
    ledger.close()
    print(f"完了: cost=${cost:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
