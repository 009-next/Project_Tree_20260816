"""
L2 イベント・Claim 抽出。1文書につき生涯1回だけ実行する（claude.md 設計原則）。
verify_span は LLM を通さない（コード検証）。
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

from ledger import Ledger
import llm

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "occurred_on": {"type": "string", "description": "YYYY-MM-DD。不明な部分は01で埋める"},
                    "date_precision": {"enum": ["exact", "month", "quarter", "unknown"]},
                    "thread_hint": {"type": ["string", "null"], "description": "この出来事が属する案件名。文書タイトルに含まれる正式な案件名を使う"},
                    "kind": {"enum": ["decision", "issue", "request", "completion", "concern",
                                       "schedule_change", "cost_change", "info"]},
                    "summary": {"type": "string"},
                    "detail": {"type": ["string", "null"]},
                    "actors": {"type": "array", "items": {"type": "string"}},
                    "targets": {"type": "array", "items": {"type": "string"}},
                    "magnitude_value": {"type": ["number", "null"], "description": "変更量の数値（例: 10日後ろ倒し→10）。量が明記されない場合は null"},
                    "magnitude_unit": {"type": ["string", "null"], "description": "変更量の単位（例: days, JPY）"},
                    "span_start_line": {"type": "integer"},
                    "span_end_line": {"type": "integer"},
                    "span_quote": {"type": "string", "description": "原文からの逐語引用。要約禁止"},
                    "certainty": {"enum": ["explicit", "inferred"]},
                },
                "required": ["occurred_on", "date_precision", "thread_hint", "kind", "summary", "detail",
                             "actors", "targets", "magnitude_value", "magnitude_unit",
                             "span_start_line", "span_end_line", "span_quote", "certainty"],
                "additionalProperties": False,
            },
        },
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "object_hint": {"type": ["string", "null"], "description": "対象物の推定名（名寄せ前）"},
                    "object_surface": {"type": "string", "description": "原文での表記そのまま"},
                    "attribute": {"type": "string"},
                    "value_raw": {"type": "string"},
                    "value_norm": {"type": ["string", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "effective_on": {"type": ["string", "null"]},
                    "revision": {"type": ["string", "null"]},
                    "scope": {"type": ["string", "null"], "description": "工区・条件等の適用範囲"},
                    "span_start_line": {"type": "integer"},
                    "span_end_line": {"type": "integer"},
                    "span_quote": {"type": "string"},
                    "certainty": {"enum": ["explicit", "inferred"]},
                },
                "required": ["object_hint", "object_surface", "attribute", "value_raw", "value_norm", "unit",
                             "effective_on", "revision", "scope", "span_start_line", "span_end_line",
                             "span_quote", "certainty"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["events", "claims"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """業務資料から、検証可能な事実だけを抽出する。

# thread_hint（重要・案件の名寄せに直結する）
ユーザーメッセージ冒頭の「文書タイトル」には、その文書が属する案件の正式名称が
含まれている（末尾の文書種別・工種・宛先等は案件名の一部ではない）。
例:「総合体育館新築工事（P07） 施工計画書（基礎工事）」→ 案件名は「総合体育館新築工事（P07）」。

- thread_hint には、この案件名をそのまま使う。
- **同一文書から抽出する全イベントで、thread_hint は一字一句同一の値にする。**
  イベントごとに話題（例:「足場材」「電源停止申請」等）を thread_hint に入れない。
  話題は summary や targets に書く。
- 文書タイトルに案件名が含まれない場合のみ、本文から案件名を推定する。

# Event（出来事）
決定・課題・依頼・完了・懸念・工程変更・費用変更・その他の共有事項を抽出する。
- 日付が曖昧な場合（「来月末までに」「今四半期中に」等）は date_precision を month/quarter/unknown にし、
  occurred_on はユーザーメッセージ冒頭に示す「文書の日付」を基準に推定する
  （例: 文書日付が2026-04-18で「今四半期中に」なら 2026-04-01〜2026-06-01 の範囲で妥当な日を選ぶ）。
  **文書の日付と無関係な年（2001年・2024年等のプレースホルダ）を絶対に使わない。**
  文脈から年が変わると明確に読み取れる場合（「来年◯月」等）のみ文書日付の年からずらしてよい。
- span_quote は原文からの逐語引用のみ。要約や言い換えは禁止。装飾記号（**太字**等）は
  含めても含めなくてもよいが、それ以外の文字は原文と完全に一致させること。
- 文書に明記されている内容は certainty=explicit、行間から読み取った内容は inferred。

# kind の判定基準（重要）
- **schedule_change**: 工期・引渡し予定日・完了予定日など、既存の期日を変更する決定。
  「◯日後ろ倒し」「予定日を◯月◯日に変更」のように、変更前後の日付や変更幅が
  明記されている記述は必ず schedule_change にする（decision にしない）。
  単なる将来の作業計画の記述（変更を伴わない当初計画）、提出期限、定例会の開催日程、
  着工日そのものの記述は schedule_change ではなく info または request とする。
- **cost_change**: 費用・金額の変更決定。同様に、既存の期日ではなく金額を変更する場合。
- **decision**: 上記2つに該当しない、その他の合意・決定事項。

# magnitude（変更量。schedule_change / cost_change のみ）
「10日後ろ倒し」「14日延長」のように変更幅が数値で明記されている場合は
magnitude_value / magnitude_unit に入れる（例: value=10, unit="days"）。
変更後の絶対日付・金額のみが書かれ、変更前との差分が原文にない場合は null のままにする
（差分を計算・推測して埋めない）。

# Claim（検証可能な主張＝対象物・属性・値）
仕様・数量・工期・費用など、後で他文書と突き合わせられる値を抽出する。
- object_surface は原文の表記そのまま（正規化しない）。
- **attribute は測定対象の性質を表す安定した一般名にする。**
  「径（変更前）」「径（変更後）」のように、変更の前後や文脈で名前を変えてはいけない。
  同じ性質（例: 鉄筋の径）を指すなら、文書を跨いでも常に同じ attribute 文字列
  （例: 常に「鉄筋径」）を使う。他の文書の claim と付き合わせて初めて意味を持つため、
  ここで表記が揺れると突き合わせが機能しなくなる。
- 決定文が「AからBに変更する」のように新旧両方の値を含む場合、**Claim としては
  新しい値（B）だけを抽出する**（新しい決定が確立した値）。古い値（A）は、それ単体で
  他の文書に独立して書かれている場合にのみ、その文書側の Claim として別途拾う。
  1つの決定文から「変更前」「変更後」のペアを2つの Claim に分けて起こさない。
- 工区や条件などで値の適用範囲が限定される場合は scope に書く（例:「A工区」「地盤改良時」）。
- value_norm は比較用の正規化値（例: "D16"）。正規化できる場合は必ず埋める
  （型式名・規格名なども value_raw と同じ文字列でよいので、なるべく null にしない）。
- **effective_on は、この Claim が「新しい決定として確立した日」の場合にのみ埋める**
  （通常は文書日付と同じ）。他の文書に書かれている既存の値を単に参照・引用しているだけの
  Claim（新しい決定を伴わない）では、effective_on は null のままにする。
  この区別が、後で「どの Claim が最新の決定で、どれが未反映の古い値か」を機械的に
  判定するための唯一の手がかりになる。**null にしすぎない、埋めすぎない、どちらも避け、
  実際に決定文であるかどうかを文脈から正しく判断すること。**

抽出しすぎない。文書に実際に書かれている検証可能な事実のみを対象にする。
"""


def number_lines(text: str) -> str:
    return "\n".join(f"{i+1}: {line}" for i, line in enumerate(text.split("\n")))


def _strip_markdown_decoration(s: str) -> str:
    """引用照合を markdown 装飾記号（**太字**等）に対して寛容にする。
    content の一致要求は変えない — 記号だけを両辺から除去して比較する。"""
    for token in ("**", "__", "`"):
        s = s.replace(token, "")
    return s


def verify_span(doc_text: str, start_line: int, end_line: int, quote: str) -> bool:
    lines = doc_text.split("\n")
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        return False
    window = "\n".join(lines[start_line - 1: end_line])
    if quote.strip() == "":
        return False
    if quote in window:
        return True
    return _strip_markdown_decoration(quote) in _strip_markdown_decoration(window)


def extract_document(doc) -> tuple[list[dict], list[dict], dict]:
    numbered = number_lines(doc["text"])
    response = llm.client.messages.create(
        model=llm.MODEL_EXTRACT,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"文書タイトル: {doc['title']}\n文書の日付: {doc['occurred_at']}\n\n文書（行番号付き）:\n\n{numbered}"}],
        output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return data["events"], data["claims"], llm.usage_dict(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ledger = Ledger()
    ledger.init_db()

    if args.doc_id:
        pending = [ledger.get_document(args.doc_id)]
    else:
        pending = ledger.documents_without_events()

    if not pending:
        print("未抽出の文書はありません（LLM 呼び出し 0）", file=sys.stderr)
        return

    run_id = "run_" + uuid.uuid4().hex[:20]
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not args.dry_run:
        ledger.insert_run({
            "run_id": run_id,
            "started_at": started_at,
            "stage": "extract",
            "model": llm.MODEL_EXTRACT,
            "prompt_version": llm.PROMPT_VERSION,
        })

    total_in = total_out = 0
    total_events = total_claims = total_rejected = 0

    for doc in pending:
        try:
            events, claims, usage = extract_document(doc)
        except Exception as e:
            print(f"  [ERROR] {doc['doc_id']} ({doc['title']}): {e}", file=sys.stderr)
            continue

        total_in += usage["input_tokens"]
        total_out += usage["output_tokens"]

        kept_events = kept_claims = 0
        for ev in events:
            if verify_span(doc["text"], ev["span_start_line"], ev["span_end_line"], ev["span_quote"]):
                if not args.dry_run:
                    ledger.insert_event({
                        "event_id": "evt_" + uuid.uuid4().hex[:20],
                        "doc_id": doc["doc_id"],
                        "run_id": run_id,
                        "occurred_on": ev["occurred_on"],
                        "date_precision": ev["date_precision"],
                        "thread_hint": ev["thread_hint"],
                        "kind": ev["kind"],
                        "summary": ev["summary"],
                        "detail": ev["detail"],
                        "actors": ev["actors"],
                        "targets": ev["targets"],
                        "mag_value": ev["magnitude_value"],
                        "mag_unit": ev["magnitude_unit"],
                        "span_start": ev["span_start_line"],
                        "span_end": ev["span_end_line"],
                        "span_quote": ev["span_quote"],
                        "certainty": ev["certainty"],
                    })
                kept_events += 1
                total_events += 1
            else:
                total_rejected += 1

        for c in claims:
            if verify_span(doc["text"], c["span_start_line"], c["span_end_line"], c["span_quote"]):
                if not args.dry_run:
                    ledger.insert_claim({
                        "claim_id": "clm_" + uuid.uuid4().hex[:20],
                        "doc_id": doc["doc_id"],
                        "run_id": run_id,
                        "object_hint": c["object_hint"],
                        "object_surface": c["object_surface"],
                        "attribute": c["attribute"],
                        "value_raw": c["value_raw"],
                        "value_norm": c["value_norm"],
                        "unit": c["unit"],
                        "effective_on": c["effective_on"],
                        "revision": c["revision"],
                        "scope": c["scope"],
                        "span_start": c["span_start_line"],
                        "span_end": c["span_end_line"],
                        "span_quote": c["span_quote"],
                        "certainty": c["certainty"],
                    })
                kept_claims += 1
                total_claims += 1
            else:
                total_rejected += 1

        if not args.dry_run:
            ledger.commit()
        print(f"  [{doc['doc_id']}] {doc['title'][:30]}: events={kept_events} claims={kept_claims}", file=sys.stderr)

    cost = llm.cost_usd(
        {"input_tokens": total_in, "output_tokens": total_out}, llm.MODEL_EXTRACT
    )

    if not args.dry_run:
        ledger.update_run(
            run_id,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            doc_count=len(pending),
            event_count=total_events,
            rejected_count=total_rejected,
            input_tokens=total_in,
            output_tokens=total_out,
            cost_usd=cost,
        )

    ledger.close()
    print(
        f"\n完了: 文書={len(pending)} events={total_events} claims={total_claims} "
        f"破棄={total_rejected} in={total_in} out={total_out} cost=${cost:.4f}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
