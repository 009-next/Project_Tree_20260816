"""資料の抽出を並列で行う（「▶出力」の待ち時間を縮めるため）。

extractor.main() は資料を1件ずつ順に処理する。1件あたり 100〜165 秒かかる
ため、資料6件で 10 分以上を占めていた（実測。全体 19 分のうち約7割）。

抽出の中身は変えない:
  LLM 呼び出しは extractor.extract_document() を、原文照合は
  extractor.verify_span() を、そのまま呼ぶ。プロンプトも判定規則も
  extractor.py のものを使う。ここがやるのは「順番に待つ」のをやめることだけ。

安全のための約束:
  1. extractor.py / llm.py は変更しない
  2. LLM 呼び出しだけを別スレッドで行う。台帳への書き込みは呼び出し元の
     スレッドへ戻してから順に行う（sqlite3 の接続は作成スレッド専用のため）
  3. 途中で何が起きても、呼び出し側が extractor.main() へ切り替えられるよう
     status を返す。並列側が失敗しても従来の経路で完走できる
"""

from __future__ import annotations

import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
from ledger import Ledger  # noqa: E402

# 同時に投げる本数。増やすほど速いが、API 側の同時実行制限に当たると
# 429 が返って結局遅くなる。実測で無理のない値にしている。
MAX_WORKERS = 4


def run(ledger: Ledger, *, max_workers: int = MAX_WORKERS) -> dict:
    """未抽出の資料を並列で抽出し、台帳へ書き込む。

    戻り値の status:
      ok        … 並列で処理できた
      empty     … 未抽出の資料が無い
      unavailable … 並列経路を使えない。呼び出し側は従来経路へ切り替えること
    """
    try:
        import extractor
    except Exception as e:
        return {"status": "unavailable", "reason": f"extractor を読み込めません: {e}"}

    for name in ("extract_document", "verify_span"):
        if not hasattr(extractor, name):
            return {"status": "unavailable",
                    "reason": f"extractor.{name} がありません（並列経路は使えません）"}

    pending = ledger.documents_without_events()
    if not pending:
        return {"status": "empty", "documents": 0, "events_added": 0, "cost_usd": 0.0}

    # sqlite3.Row は読み取り専用なので、スレッドへ渡す前に素の dict にする
    docs = [{"doc_id": d["doc_id"], "title": d["title"],
             "text": d["text"], "occurred_at": d["occurred_at"]} for d in pending]

    run_id = "run_" + uuid.uuid4().hex[:20]
    ledger.insert_run({
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": "extract",
        "model": llm.MODEL_EXTRACT,
        "prompt_version": llm.PROMPT_VERSION,
    })

    # --- ここだけ並列。LLM を呼ぶ以外のことはしない ---
    def call(doc):
        try:
            events, claims, usage = extractor.extract_document(doc)
            return doc, events, claims, usage, None
        except Exception as e:
            return doc, [], [], {"input_tokens": 0, "output_tokens": 0}, e

    workers = max(1, min(int(max_workers), len(docs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(call, docs))

    # --- 以降は呼び出し元スレッドで、順に書き込む ---
    total_in = total_out = 0
    total_events = total_claims = total_rejected = 0
    errors = []

    for doc, events, claims, usage, err in results:
        if err is not None:
            errors.append({"doc_id": doc["doc_id"], "title": doc["title"],
                           "reason": f"{type(err).__name__}: {str(err)[:120]}"})
            continue

        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)

        for ev in events:
            if extractor.verify_span(doc["text"], ev["span_start_line"],
                                     ev["span_end_line"], ev["span_quote"]):
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
                total_events += 1
            else:
                total_rejected += 1

        for c in claims:
            if extractor.verify_span(doc["text"], c["span_start_line"],
                                     c["span_end_line"], c["span_quote"]):
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
                total_claims += 1
            else:
                total_rejected += 1

    ledger.commit()

    cost = llm.cost_usd({"input_tokens": total_in, "output_tokens": total_out},
                        llm.MODEL_EXTRACT)
    ledger.update_run(
        run_id,
        finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        doc_count=len(docs),
        event_count=total_events,
        rejected_count=total_rejected,
        input_tokens=total_in,
        output_tokens=total_out,
        cost_usd=cost,
    )

    return {"status": "ok", "documents": len(docs), "workers": workers,
            "events_added": total_events, "claims_added": total_claims,
            "rejected": total_rejected, "cost_usd": round(cost, 4),
            "errors": errors}
