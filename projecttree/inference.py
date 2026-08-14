"""LLM 推論層（proposal.md 2-1 / 2-2 / 2-4）。

このアプリの一貫した約束を、ここでも崩さない。

  1. LLM に渡す証拠は SQL が選ぶ。LLM は選ばれた証拠の範囲でしか語れない。
  2. LLM の出力は必ず event_id を引かせ、実在しない ID を引いた主張は「未検証」へ落とす。
  3. 呼ぶ前に必ず原価を見積もり、上限を超えるなら呼ばない。
  4. 送る前に機密情報をマスクする。

既存の extractor.py / insight.py / stages.py は書き換えない。ここは追加層。
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
from ledger import Ledger  # noqa: E402
from projecttree import masking as _mask  # noqa: E402
from projecttree import models as _models  # noqa: E402
from projecttree import provider as _prov  # noqa: E402

# proposal.md 2-1「入出力におけるトークン数の消費が0.5以内」
# 1回の推論が $0.5 を超えないよう、送信前に見積もって止める。
MAX_COST_USD = 0.5

# 証拠として渡す上限。ここを絞ることが原価管理の主軸になる。
MAX_EVENTS = 60
MAX_PATTERNS = 12
MAX_GAPS = 12

STAGE_TITLES = {
    1: "状況確認", 2: "現状の課題", 3: "試行錯誤",
    4: "課題・変化", 5: "解決策、発展策の提案",
}


# --------------------------------------------------------------------------
# 共通ユーティリティ
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _approx_tokens(text: str) -> int:
    """日本語混在テキストの概算トークン数。1トークン≒1.8文字として多めに見る。"""
    return int(len(text) / 1.8) + 1


def preflight(task: str, prompt_text: str, evidence_text: str,
              expected_output_tokens: int) -> dict:
    """呼ぶ前の見積り。allowed=False なら呼んではいけない。"""
    model = _models.model_for_task(task)
    in_tok = _approx_tokens(prompt_text + evidence_text)
    est = _models.estimate(task, in_tok, expected_output_tokens)
    est["allowed"] = est["usd"] <= MAX_COST_USD
    est["limit_usd"] = MAX_COST_USD
    est["model"] = model
    if not est["allowed"]:
        est["reason"] = f"見積り ${est['usd']} が上限 ${MAX_COST_USD} を超えます"
    return est


def _call(model: str, **kw):
    """LLM 呼び出し。失敗は例外ではなく理由を返し、UI が読める形にする。"""
    try:
        return _prov.get_client().messages.create(model=model, **kw), None
    except TypeError as e:
        # SDK がキーを解決できないときは TypeError になる
        if "authentication" in str(e).lower():
            return None, "APIキーが設定されていません。⚙設定 から登録してください。"
        return None, f"{type(e).__name__}: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def _collect_evidence(ledger: Ledger, thread_id: str) -> tuple[str, set[str]]:
    """SQL が証拠を選ぶ。戻り値: (LLM へ渡す本文, 有効な event_id 集合)"""
    ev = ledger.conn.execute(
        "SELECT event_id, occurred_on, kind, summary FROM events "
        "WHERE thread_id = ? ORDER BY occurred_on LIMIT ?",
        (thread_id, MAX_EVENTS),
    ).fetchall()
    valid_ids = {r["event_id"] for r in ev}

    lines = ["## 時系列（この範囲の事実だけを根拠にすること）"]
    for r in ev:
        lines.append(f"- [{r['event_id']}] {r['occurred_on']} ({r['kind']}) {r['summary']}")

    gaps = ledger.conn.execute(
        "SELECT kind, description FROM gaps WHERE thread_id = ? LIMIT ?",
        (thread_id, MAX_GAPS),
    ).fetchall()
    if gaps:
        lines.append("\n## 記録の空白（SQL が検出済み）")
        lines += [f"- ({g['kind']}) {g['description']}" for g in gaps]

    # discrepancies は案件列を持たない。claims の出典文書をたどって案件へ結び付ける。
    disc = ledger.conn.execute(
        "SELECT DISTINCT d.attribute, d.explanation, o.name FROM discrepancies d "
        "JOIN objects o ON o.object_id = d.object_id "
        "JOIN claims c ON c.object_id = d.object_id "
        "JOIN events e ON e.doc_id = c.doc_id "
        "WHERE e.thread_id = ? AND d.status = 'open' LIMIT ?",
        (thread_id, MAX_PATTERNS),
    ).fetchall()
    if disc:
        lines.append("\n## 未解消の食い違い（SQL が検出済み）")
        lines += [f"- {d['name']} / {d['attribute']}: {d['explanation']}" for d in disc]

    return "\n".join(lines), valid_ids


# --------------------------------------------------------------------------
# 2-1: プロンプト入力 → 推論（原因究明・新発見・進捗率向上の提案）
# --------------------------------------------------------------------------

INFER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "問いへの直接の回答。200字以内。"},
        # maxItems は構造化出力スキーマで未対応。件数の上限は description で伝える。
        "findings": {
            "type": "array",
            "description": "最大6件。根拠が引けないものは書かない。",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["原因究明", "新発見", "進捗率向上"]},
                    "statement": {"type": "string", "description": "主張。120字以内。"},
                    "basis_event_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "根拠とした event_id。証拠に無いIDは書かないこと。",
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["type", "statement", "basis_event_ids", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "findings"],
    "additionalProperties": False,
}

INFER_SYSTEM = """あなたは土木工事の記録を読む分析者です。
与えられた時系列・空白・食い違いだけを根拠に、問いへ答えてください。

厳守事項:
- basis_event_ids には、証拠として示された [event_id] のみを書く。存在しないIDを作らない。
- 証拠から言えないことは書かない。推測を事実のように書かない。
- [PERSON_001] のような角括弧付きの識別子は伏字である。そのままの表記で使うこと。
"""


def infer_from_prompt(ledger: Ledger, thread_id: str, question: str,
                      *, allow_sensitive: bool = False, dry_run: bool = False) -> dict:
    """利用者の問いに対し、台帳の証拠だけを根拠に推論する。"""
    evidence, valid_ids = _collect_evidence(ledger, thread_id)

    # 送信前マスキング（proposal.md 1-2）
    m = _mask.mask(question + "\n\n" + evidence)
    if m.has_sensitive_label and not allow_sensitive:
        return {"status": "blocked",
                "reason": f"機密ラベル {'/'.join(m.labels)} を検出しました",
                "mask": m.summary()}

    est = preflight("infer_proposal", INFER_SYSTEM, m.text, 1200)
    if not est["allowed"]:
        return {"status": "blocked", "reason": est["reason"], "estimate": est}
    if dry_run:
        return {"status": "dry_run", "estimate": est, "mask": m.summary(),
                "evidence_events": len(valid_ids)}

    model = _models.model_for_task("infer_proposal")
    resp, err = _call(
        model,
        max_tokens=4000,
        system=INFER_SYSTEM,
        messages=[{"role": "user",
                   "content": f"{m.text}\n\n## 問い\n{question}"}],
        output_config={"format": {"type": "json_schema", "schema": _models.sanitize_schema(INFER_SCHEMA)}},
    )
    if err:
        return {"status": "error", "reason": err, "model": model}
    data, perr = _models.parse_json_response(resp)
    if perr:
        return {"status": "error", "reason": perr, "model": model}
    usage = llm.usage_dict(resp)
    cost = _prov.cost_usd(usage, model) or llm.cost_usd(usage, model)

    # 検証層: 実在しない event_id を引いた主張は「未検証」へ落とす
    kept, unverified = [], []
    for f in data.get("findings", []):
        ids = [i for i in f.get("basis_event_ids", []) if i in valid_ids]
        bad = [i for i in f.get("basis_event_ids", []) if i not in valid_ids]
        f = dict(f, basis_event_ids=ids, dropped_ids=bad)
        f["statement"] = _mask.unmask(f["statement"], m.mapping)
        (kept if ids else unverified).append(f)

    return {
        "status": "ok",
        "answer": _mask.unmask(data.get("answer", ""), m.mapping),
        "findings": kept,            # 根拠が実在する = 台帳で追跡できる
        "unverified": unverified,    # 根拠を引けなかった = 仮説
        "label": "仮説・未検証を含む（llmレーン）",
        "model": model,
        "usage": usage,
        "cost_usd": round(cost, 4),
        "estimate_usd": est["usd"],
        "mask": m.summary(),
        "evidence_events": len(valid_ids),
    }


# --------------------------------------------------------------------------
# 2-2: LLM による段階分類（キーワードで引っ掛からない記録のタグ付け）
# --------------------------------------------------------------------------

STAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "stage_no": {"type": "integer", "minimum": 1, "maximum": 5},
                    "keyword": {"type": "string", "description": "判断の根拠になった語。原文中の語。"},
                },
                "required": ["event_id", "stage_no", "keyword"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}

STAGE_SYSTEM = """記録を、次の5段階のどれかに分類してください。

1. 状況確認         現状がどのようなものか
2. 現状の課題       なぜそのプロジェクトを始めたか
3. 試行錯誤         どのように業務を進めようとしているか
4. 課題・変化       どのような問題に直面しているか
5. 解決策、発展策の提案  どうすればいいか、どう発展できるか

厳守事項:
- 与えられた event_id 以外を出力しない。
- keyword は必ず、その記録の本文に実際に現れる語を書き写す（要約しない）。
"""


def classify_stages_llm(ledger: Ledger, thread_id: str, *, dry_run: bool = False) -> dict:
    """ルールベースで判定できなかった記録を、LLM が段階へ分類する。

    ルール分類の結果は壊さない。ルールで段階が決まらなかった分だけを対象にする。
    """
    from projecttree import stages as _stages

    rows = ledger.conn.execute(
        "SELECT event_id, occurred_on, kind, summary FROM events WHERE thread_id = ?",
        (thread_id,),
    ).fetchall()
    # KIND_TO_STAGE に無い kind = ルールでは決まらない記録
    targets = [r for r in rows if r["kind"] not in _stages.KIND_TO_STAGE]
    if not targets:
        return {"status": "skipped", "reason": "ルールで全件分類済み。LLM 呼び出しは不要です。",
                "total": len(rows), "targets": 0, "cost_usd": 0.0}

    body = "\n".join(f"- [{r['event_id']}] {r['occurred_on']} {r['summary']}" for r in targets)
    m = _mask.mask(body)
    est = preflight("classify_stage", STAGE_SYSTEM, m.text, len(targets) * 40)
    if not est["allowed"]:
        return {"status": "blocked", "reason": est["reason"], "estimate": est}
    if dry_run:
        return {"status": "dry_run", "targets": len(targets), "estimate": est}

    model = _models.model_for_task("classify_stage")
    resp, err = _call(
        model, max_tokens=6000, system=STAGE_SYSTEM,
        messages=[{"role": "user", "content": m.text}],
        output_config={"format": {"type": "json_schema", "schema": _models.sanitize_schema(STAGE_SCHEMA)}},
    )
    if err:
        return {"status": "error", "reason": err, "model": model}
    data, perr = _models.parse_json_response(resp)
    if perr:
        return {"status": "error", "reason": perr, "model": model}
    usage = llm.usage_dict(resp)
    cost = _prov.cost_usd(usage, model) or llm.cost_usd(usage, model)

    # 検証層: 実在する event_id か / keyword が本文に実在するか
    by_id = {r["event_id"]: r["summary"] for r in targets}
    applied, rejected = 0, []
    now = _now()
    for a in data.get("assignments", []):
        eid, sno = a.get("event_id"), a.get("stage_no")
        kw = _mask.unmask(a.get("keyword", ""), m.mapping)
        if eid not in by_id:
            rejected.append({"event_id": eid, "reason": "存在しない event_id"})
            continue
        if kw and kw not in by_id[eid]:
            rejected.append({"event_id": eid, "reason": f"根拠語『{kw}』が原文に無い"})
            continue
        stage_id = _ensure_stage(ledger, thread_id, sno, now)
        ledger.conn.execute(
            "INSERT OR IGNORE INTO stage_events (stage_id, event_id) VALUES (?, ?)",
            (stage_id, eid))
        applied += 1
    ledger.commit()

    return {"status": "ok", "targets": len(targets), "applied": applied,
            "rejected": rejected, "model": model, "usage": usage,
            "cost_usd": round(cost, 4)}


def _ensure_stage(ledger: Ledger, thread_id: str, stage_no: int, now: str) -> str:
    """該当段階の行が無ければ作る。既存行の method は書き換えない。"""
    row = ledger.conn.execute(
        "SELECT stage_id FROM stages WHERE thread_id = ? AND stage_no = ?",
        (thread_id, stage_no)).fetchone()
    if row:
        return row["stage_id"]
    sid = "stg_" + uuid.uuid4().hex[:20]
    ledger.conn.execute(
        "INSERT INTO stages (stage_id, thread_id, stage_no, title, summary, method, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'llm', ?)",
        (sid, thread_id, stage_no, STAGE_TITLES[stage_no], "LLM が分類した記録の集合。", now))
    return sid


# --------------------------------------------------------------------------
# 2-4: モデル部位 ↔ 段階・時系列の対応推論
# --------------------------------------------------------------------------

PART_SCHEMA = {
    "type": "object",
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "part_key": {"type": "string"},
                    "event_id": {"type": "string"},
                    "stage_no": {"type": "integer", "minimum": 1, "maximum": 5},
                    "reason": {"type": "string", "description": "対応づけの根拠。60字以内。"},
                },
                "required": ["part_key", "event_id", "stage_no", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["links"],
    "additionalProperties": False,
}

PART_SYSTEM = """構造物の部位一覧と、工事の時系列記録が与えられます。
各記録が「どの部位に対する作業か」「どの段階か」を対応づけてください。

厳守事項:
- part_key は与えられた一覧のものだけを使う。
- event_id は与えられた一覧のものだけを使う。
- どの部位とも判断できない記録は、対応づけを出力しない（無理に埋めない）。
"""


def infer_parts(ledger: Ledger, thread_id: str, *, dry_run: bool = False) -> dict:
    """2D/3Dモデルの部位と、台帳の時系列・段階を対応づける（proposal.md 2-4）。"""
    parts = ledger.conn.execute(
        "SELECT part_key, name, stage_no FROM model_parts WHERE thread_id = ? ORDER BY stage_no",
        (thread_id,)).fetchall()
    if not parts:
        return {"status": "skipped", "reason": "この案件にはモデル部位がありません。先にモデルを生成してください。"}

    events = ledger.conn.execute(
        "SELECT event_id, occurred_on, summary FROM events WHERE thread_id = ? "
        "ORDER BY occurred_on LIMIT ?", (thread_id, MAX_EVENTS)).fetchall()
    if not events:
        return {"status": "skipped", "reason": "この案件には記録がありません。"}

    body = (
        "## 部位一覧\n"
        + "\n".join(f"- [{p['part_key']}] {p['name']}（既定段階 {p['stage_no']}）" for p in parts)
        + "\n\n## 時系列記録\n"
        + "\n".join(f"- [{e['event_id']}] {e['occurred_on']} {e['summary']}" for e in events)
    )
    m = _mask.mask(body)
    est = preflight("infer_part", PART_SYSTEM, m.text, len(events) * 45)
    if not est["allowed"]:
        return {"status": "blocked", "reason": est["reason"], "estimate": est}
    if dry_run:
        return {"status": "dry_run", "parts": len(parts), "events": len(events), "estimate": est}

    model = _models.model_for_task("infer_part")
    resp, err = _call(
        model, max_tokens=6000, system=PART_SYSTEM,
        messages=[{"role": "user", "content": m.text}],
        output_config={"format": {"type": "json_schema", "schema": _models.sanitize_schema(PART_SCHEMA)}},
    )
    if err:
        return {"status": "error", "reason": err, "model": model}
    data, perr = _models.parse_json_response(resp)
    if perr:
        return {"status": "error", "reason": perr, "model": model}
    usage = llm.usage_dict(resp)
    cost = _prov.cost_usd(usage, model) or llm.cost_usd(usage, model)

    valid_parts = {p["part_key"] for p in parts}
    valid_events = {e["event_id"] for e in events}
    now = _now()
    applied, rejected = 0, []
    for lk in data.get("links", []):
        if lk.get("part_key") not in valid_parts:
            rejected.append({"link": lk, "reason": "存在しない part_key"}); continue
        if lk.get("event_id") not in valid_events:
            rejected.append({"link": lk, "reason": "存在しない event_id"}); continue
        ledger.conn.execute(
            "INSERT OR IGNORE INTO part_events (part_key, thread_id, event_id, stage_no, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (lk["part_key"], thread_id, lk["event_id"], lk["stage_no"],
             _mask.unmask(lk.get("reason", ""), m.mapping), now))
        applied += 1
    ledger.commit()

    return {"status": "ok", "parts": len(parts), "events": len(events),
            "applied": applied, "rejected": rejected,
            "model": model, "usage": usage, "cost_usd": round(cost, 4)}


# 部位↔記録の対応表。既存テーブルは触らず、追加のみ。
PART_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS part_events (
  part_key   TEXT NOT NULL,
  thread_id  TEXT NOT NULL,
  event_id   TEXT NOT NULL,
  stage_no   INTEGER NOT NULL CHECK(stage_no BETWEEN 1 AND 5),
  reason     TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (part_key, event_id)
);
CREATE INDEX IF NOT EXISTS idx_part_events_thread ON part_events(thread_id, stage_no);
"""


def ensure_tables(ledger: Ledger) -> None:
    ledger.conn.executescript(PART_EVENTS_DDL)
    ledger.commit()
