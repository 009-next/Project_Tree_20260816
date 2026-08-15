"""
FastAPI app。台帳を読むだけの GET は LLM を呼ばない。
`/api/ask` は既存台帳の参照のみ（llm_calls は常に 0 を返す設計原則の可視化）。
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ledger import Ledger
from paths import resource_dir

app = FastAPI(title="業務確定台帳")

STATIC_DIR = resource_dir() / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def get_ledger() -> Ledger:
    return Ledger()


def _row_to_event(r) -> dict:
    return {
        "event_id": r["event_id"],
        "doc_id": r["doc_id"],
        "occurred_on": r["occurred_on"],
        "date_precision": r["date_precision"],
        "thread_id": r["thread_id"],
        "kind": r["kind"],
        "summary": r["summary"],
        "detail": r["detail"],
        "actors": json.loads(r["actors"] or "[]"),
        "targets": json.loads(r["targets"] or "[]"),
        "magnitude": {"value": r["mag_value"], "unit": r["mag_unit"]} if r["mag_value"] is not None else None,
        "certainty": r["certainty"],
        "source": {"doc_id": r["doc_id"], "start_line": r["span_start"], "end_line": r["span_end"], "quote": r["span_quote"]},
    }


def _row_to_insight(r) -> dict:
    return {
        "insight_id": r["insight_id"],
        "thread_id": r["thread_id"],
        "detection_method": r["detection_method"],
        "type": r["type"],
        "label": r["label"],
        "statement": r["statement"],
        "basis_event_ids": json.loads(r["basis_event_ids"] or "[]"),
        "pattern_type": r["pattern_type"],
        "pattern_evidence": json.loads(r["pattern_evidence"]) if r["pattern_evidence"] else None,
        "basis_rule": r["basis_rule"],
        "horizon": r["horizon"],
    }


def _row_to_gap(r) -> dict:
    return {
        "gap_id": r["gap_id"], "thread_id": r["thread_id"], "kind": r["kind"],
        "period_start": r["period_start"], "period_end": r["period_end"],
        "anchor_event_id": r["anchor_event_id"], "description": r["description"],
    }


@app.get("/")
def index():
    f = STATIC_DIR / "timeline.html"
    if not f.exists():
        raise HTTPException(404, "static/timeline.html がまだありません")
    # 更新後に古い画面が残らないよう、毎回サーバーへ確認させる（/projecttree と同じ理由）
    return FileResponse(str(f), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/threads")
def list_threads():
    l = get_ledger()
    rows = l.conn.execute("SELECT * FROM threads ORDER BY last_seen DESC").fetchall()
    result = [{
        "thread_id": r["thread_id"], "name": r["name"],
        "aliases": json.loads(r["aliases"] or "[]"),
        "first_seen": r["first_seen"], "last_seen": r["last_seen"], "status": r["status"],
    } for r in rows]
    l.close()
    return result


@app.get("/api/timeline")
def timeline(thread_id: str):
    l = get_ledger()
    thread = l.conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if thread is None:
        l.close()
        raise HTTPException(404, "thread not found")

    events = l.conn.execute(
        "SELECT * FROM events WHERE thread_id = ? ORDER BY occurred_on", (thread_id,)
    ).fetchall()
    insights = l.conn.execute(
        "SELECT * FROM insights WHERE thread_id = ? ORDER BY generated_at", (thread_id,)
    ).fetchall()
    gaps = l.conn.execute(
        "SELECT * FROM gaps WHERE thread_id = ? ORDER BY period_start", (thread_id,)
    ).fetchall()
    l.close()

    return {
        "thread": {
            "thread_id": thread["thread_id"], "name": thread["name"],
            "aliases": json.loads(thread["aliases"] or "[]"),
        },
        "events": [_row_to_event(r) for r in events],
        "insights": [_row_to_insight(r) for r in insights],
        "gaps": [_row_to_gap(r) for r in gaps],
    }


@app.get("/api/discrepancies")
def discrepancies(status: str | None = None, object_id: str | None = None):
    l = get_ledger()
    q = "SELECT * FROM discrepancies WHERE 1=1"
    params = []
    if status:
        q += " AND status = ?"
        params.append(status)
    if object_id:
        q += " AND object_id = ?"
        params.append(object_id)
    rows = l.conn.execute(q, params).fetchall()

    result = []
    for r in rows:
        obj = l.conn.execute("SELECT name, aliases FROM objects WHERE object_id = ?", (r["object_id"],)).fetchone()
        claim_ids = json.loads(r["claim_ids"])
        claims = [dict(l.conn.execute("SELECT * FROM claims WHERE claim_id = ?", (cid,)).fetchone()) for cid in claim_ids]
        result.append({
            "discrepancy_id": r["discrepancy_id"],
            "object": {"name": obj["name"], "aliases": json.loads(obj["aliases"] or "[]")} if obj else None,
            "attribute": r["attribute"],
            "detection_method": r["detection_method"],
            "status": r["status"],
            "newest_claim_id": r["newest_claim_id"],
            "stale_claim_ids": json.loads(r["stale_claim_ids"] or "[]"),
            "benign_reason": r["benign_reason"],
            "explanation": r["explanation"],
            "claims": [{
                "claim_id": c["claim_id"], "doc_id": c["doc_id"], "object_surface": c["object_surface"],
                "value_raw": c["value_raw"], "scope": c["scope"], "effective_on": c["effective_on"],
                "source": {"doc_id": c["doc_id"], "start_line": c["span_start"], "end_line": c["span_end"], "quote": c["span_quote"]},
            } for c in claims],
        })
    l.close()
    return result


@app.get("/api/objects/{object_id}")
def get_object(object_id: str):
    l = get_ledger()
    obj = l.conn.execute("SELECT * FROM objects WHERE object_id = ?", (object_id,)).fetchone()
    if obj is None:
        l.close()
        raise HTTPException(404, "object not found")
    claims = l.conn.execute("SELECT * FROM claims WHERE object_id = ?", (object_id,)).fetchall()
    l.close()
    return {
        "object_id": obj["object_id"], "name": obj["name"], "aliases": json.loads(obj["aliases"] or "[]"),
        "claims": [{
            "claim_id": c["claim_id"], "doc_id": c["doc_id"], "object_surface": c["object_surface"],
            "attribute": c["attribute"], "value_raw": c["value_raw"],
            "source": {"doc_id": c["doc_id"], "start_line": c["span_start"], "end_line": c["span_end"], "quote": c["span_quote"]},
        } for c in claims],
    }


@app.get("/api/source/{doc_id}")
def get_source(doc_id: str, start_line: int | None = None, end_line: int | None = None, context: int = 3):
    l = get_ledger()
    doc = l.conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    l.close()
    if doc is None:
        raise HTTPException(404, "document not found")

    lines = doc["text"].split("\n")
    if start_line is None:
        start_line, end_line = 1, len(lines)
    lo = max(1, start_line - context)
    hi = min(len(lines), (end_line or start_line) + context)

    return {
        "doc_id": doc_id, "title": doc["title"], "source_path": doc["source_path"],
        "occurred_at": doc["occurred_at"],
        "lines": [{"line": i, "text": lines[i - 1], "highlighted": start_line <= i <= (end_line or start_line)}
                  for i in range(lo, hi + 1)],
    }


@app.get("/api/ask")
def ask(q: str):
    """台帳参照のみ。LLM 呼び出しは行わない（llm_calls は常に 0）。"""
    l = get_ledger()
    threads = l.conn.execute("SELECT * FROM threads").fetchall()

    matched_thread = None
    for t in threads:
        names = [t["name"]] + json.loads(t["aliases"] or "[]")
        if any(n in q or q in n for n in names if n):
            matched_thread = t
            break

    if matched_thread is None:
        l.close()
        return {
            "interpretation": "質問文から案件を特定できませんでした。案件名（表記ゆれ含む）を含めて質問してください。",
            "answer": None, "events": [], "insights": [], "gaps": [], "llm_calls": 0,
        }

    thread_id = matched_thread["thread_id"]
    events = l.conn.execute("SELECT * FROM events WHERE thread_id = ? ORDER BY occurred_on DESC LIMIT 5", (thread_id,)).fetchall()
    insights = l.conn.execute("SELECT * FROM insights WHERE thread_id = ?", (thread_id,)).fetchall()
    gaps = l.conn.execute("SELECT * FROM gaps WHERE thread_id = ?", (thread_id,)).fetchall()
    l.close()

    return {
        "interpretation": f"「{matched_thread['name']}」に関する直近の状況、と解釈しました。",
        "answer": f"直近 {len(events)} 件の記録があります。" + ("記録に空白があります。" if gaps else ""),
        "events": [_row_to_event(r) for r in events],
        "insights": [_row_to_insight(r) for r in insights],
        "gaps": [_row_to_gap(r) for r in gaps],
        "llm_calls": 0,
    }


@app.get("/api/runs")
def list_runs():
    l = get_ledger()
    rows = l.all_runs()
    l.close()
    return [dict(r) for r in rows]


@app.get("/api/runs/cost_summary")
def cost_summary():
    l = get_ledger()
    rows = l.all_runs()
    l.close()
    total_cost = sum(r["cost_usd"] for r in rows)
    total_in = sum(r["input_tokens"] for r in rows)
    total_out = sum(r["output_tokens"] for r in rows)
    return {
        "runs": len(rows), "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_in, "total_output_tokens": total_out,
        "by_stage": {
            stage: {
                "runs": len([r for r in rows if r["stage"] == stage]),
                "cost_usd": round(sum(r["cost_usd"] for r in rows if r["stage"] == stage), 4),
            } for stage in sorted({r["stage"] for r in rows})
        },
    }


# ===========================================================================
# Project_Tree (Enhancement.md) 用の追加API。
# 既存の9エンドポイントは変更していない。ここも LLM は一切呼ばない。
# ===========================================================================

@app.get("/projecttree")
def projecttree_index():
    f = STATIC_DIR / "projecttree.html"
    if not f.exists():
        raise HTTPException(404, "static/projecttree.html がありません")
    # Cache-Control を付けないと、ブラウザは Last-Modified からの経過時間を元に
    # 独自判断でキャッシュを使い回す（ヒューリスティックキャッシュ）。
    # アプリを更新しても古い画面が出続けるため、毎回サーバーへ確認させる。
    return FileResponse(str(f), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/projecttree/threads")
def pt_threads():
    """モデルまたは段階を持つ案件だけを返す（フォルダツリー表示用）。"""
    l = get_ledger()
    rows = l.conn.execute(
        """SELECT t.thread_id, t.name, t.first_seen, t.last_seen,
                  (SELECT COUNT(*) FROM stages s WHERE s.thread_id = t.thread_id) AS stage_count,
                  (SELECT COUNT(*) FROM model_parts m WHERE m.thread_id = t.thread_id) AS part_count,
                  (SELECT COUNT(*) FROM events e WHERE e.thread_id = t.thread_id) AS event_count
           FROM threads t
           WHERE stage_count > 0 OR part_count > 0
           ORDER BY part_count DESC, event_count DESC"""
    ).fetchall()
    # デモ用に非表示にした案件を一覧から除く。既定では何も隠していないので、
    # この機能を使わない限り従来と同じ結果になる。
    try:
        from projecttree import visibility as _vis
        hidden = _vis.hidden_ids(l)
    except Exception:
        hidden = set()
    l.close()
    return [dict(r) for r in rows if r["thread_id"] not in hidden]


@app.get("/api/projecttree/stages")
def pt_stages(thread_id: str):
    """5段階と、各段階に紐づくイベント（時系列・原文への入口）を返す。"""
    l = get_ledger()
    stages = l.conn.execute(
        "SELECT * FROM stages WHERE thread_id = ? ORDER BY stage_no", (thread_id,)
    ).fetchall()
    if not stages:
        l.close()
        raise HTTPException(404, "この案件はまだ段階分類されていません")

    result = []
    for s in stages:
        evs = l.conn.execute(
            """SELECT e.* FROM events e
               JOIN stage_events se ON se.event_id = e.event_id
               WHERE se.stage_id = ? ORDER BY e.occurred_on""",
            (s["stage_id"],),
        ).fetchall()
        result.append({
            "stage_id": s["stage_id"], "stage_no": s["stage_no"], "title": s["title"],
            "summary": s["summary"], "method": s["method"],
            "events": [_row_to_event(e) for e in evs],
        })

    gaps = l.conn.execute(
        "SELECT * FROM gaps WHERE thread_id = ?", (thread_id,)
    ).fetchall()
    l.close()
    return {"thread_id": thread_id, "stages": result, "gaps": [_row_to_gap(g) for g in gaps]}


@app.get("/api/projecttree/parts")
def pt_parts(thread_id: str):
    """モデル部位と段階の対応（クリック連動の要）。"""
    l = get_ledger()
    rows = l.conn.execute(
        "SELECT * FROM model_parts WHERE thread_id = ? ORDER BY stage_no", (thread_id,)
    ).fetchall()
    l.close()
    return [{
        "part_id": r["part_id"], "part_key": r["part_key"], "name": r["name"],
        "shape": r["shape"], "stage_no": r["stage_no"], "source": r["source"],
        "size": json.loads(r["size_json"]), "pos": json.loads(r["pos_json"]),
    } for r in rows]


def _asset_url(asset_id: str, path: str) -> str:
    """アセットの参照URL。static 配下にあるものは従来のURLを維持する。"""
    name = Path(path).name
    if (resource_dir() / "static" / "models" / name).is_file():
        return "/static/models/" + name
    return f"/api/projecttree/asset/{asset_id}"


@app.get("/api/projecttree/assets")
def pt_assets(thread_id: str, kind: str | None = None, stage_no: int | None = None):
    """生成済みの資料用画像・2D/3Dモデルの一覧。"""
    l = get_ledger()
    q = "SELECT * FROM assets WHERE thread_id = ?"
    params: list = [thread_id]
    if kind:
        q += " AND kind = ?"
        params.append(kind)
    if stage_no is not None:
        q += " AND stage_no = ?"
        params.append(stage_no)
    q += " ORDER BY kind, stage_no"
    rows = l.conn.execute(q, params).fetchall()
    l.close()
    return [{
        "asset_id": r["asset_id"], "kind": r["kind"], "fmt": r["fmt"],
        "stage_no": r["stage_no"], "source": r["source"],
        # ブラウザから参照できる相対URLに変換。
        # static/models/ に実体がある生成物はこれまで通りのURLを返し、
        # それ以外（アップロード物・Blender出力・イメージ図）は
        # asset_id で実体を引く経路へ回す（従来は一律 static を指して404だった）。
        "url": _asset_url(r["asset_id"], r["path"]),
    } for r in rows]


# ---------------------------------------------------------------------------
# 設定・セキュリティ関連API（Enhancement.md 4章）
# APIキーはメモリのみ・末尾4文字だけ返す。値そのものは絶対に返さない。
# ---------------------------------------------------------------------------

from pydantic import BaseModel  # noqa: E402
from projecttree import security as _sec  # noqa: E402


class ApiKeyIn(BaseModel):
    api_key: str


@app.get("/api/config")
def get_config():
    """アプリ内設定画面用。キーの値は返さずマスク表示のみ。"""
    return {
        "api_key_set": _sec.API_KEY.is_set,
        "api_key_masked": _sec.API_KEY.masked(),
        "budget_daily_jpy": _sec.BUDGET.daily_limit_jpy,
        "budget_spent_jpy": round(_sec.BUDGET.spent_today(), 1),
        "budget_remaining_jpy": round(_sec.BUDGET.remaining(), 1),
        "model_gen_modes": [
            {"id": "local", "label": "ローカル生成（trimesh/ezdxf）", "cost_note": "追加費用 0 円"},
            {"id": "meshy", "label": "Meshy API（画像→3D）", "cost_note": "外部有料API・APIキーが必要"},
        ],
    }


@app.post("/api/config/api_key")
def set_api_key(body: ApiKeyIn):
    try:
        _sec.API_KEY.set(body.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"api_key_set": True, "api_key_masked": _sec.API_KEY.masked()}


@app.delete("/api/config/api_key")
def clear_api_key():
    _sec.API_KEY.clear()
    return {"api_key_set": False, "api_key_masked": _sec.API_KEY.masked()}


@app.get("/api/config/estimate")
def estimate(model: str = "claude-opus-5", input_tokens: int = 30000, output_tokens: int = 2000):
    """解析前に推定原価を提示する（Enhancement.md 4-5）。"""
    est = _sec.estimate_cost(model, input_tokens, output_tokens)
    est["within_budget"] = _sec.BUDGET.can_spend(est["jpy"])
    est["budget_remaining_jpy"] = round(_sec.BUDGET.remaining(), 1)
    return est


# ==========================================================================
# Phase 6: 資料ダウンロード（Enhancement.md §2-3-1）と1次情報の入力（§2-1）
# 既存のエンドポイントは変更していない。以下はすべて追加。
# ==========================================================================

import urllib.parse  # noqa: E402

from fastapi import File, Form, UploadFile  # noqa: E402
from fastapi.responses import Response  # noqa: E402

from projecttree import exporters as _exp  # noqa: E402
from projecttree import intake as _intake  # noqa: E402


@app.get("/api/projecttree/export")
def pt_export(thread_id: str, fmt: str, stage_no: int | None = None):
    """md / pptx / xlsx を生成して返す。台帳を読むだけで LLM は呼ばない。"""
    if fmt not in _exp.BUILDERS:
        raise HTTPException(400, f"unsupported format: {fmt}")
    ledger = get_ledger()
    try:
        data, name, media = _exp.build(ledger, thread_id, fmt, stage_no)
    except ValueError as e:
        raise HTTPException(404, str(e))
    finally:
        ledger.close()

    quoted = urllib.parse.quote(name)
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


@app.post("/api/projecttree/intake/prompt")
def pt_intake_prompt(prompt: str = Form(...), thread_id: str | None = Form(None)):
    """プロンプト・メモ書きを1次情報として台帳に取り込む。"""
    ledger = get_ledger()
    try:
        result = _intake.intake_prompt(ledger, prompt, thread_id)
        result["pending"] = _intake.pending_count(ledger)
        return result
    except _intake.IntakeRejected as e:
        raise HTTPException(400, str(e))
    finally:
        ledger.close()


@app.post("/api/projecttree/intake/files")
async def pt_intake_files(files: list[UploadFile] = File(...), thread_id: str | None = Form(None)):
    """資料・画像をアップロードして台帳に取り込む（txt/md/pdf/docx/pptx/jpeg/png）。"""
    ledger = get_ledger()
    results = []
    try:
        for f in files:
            data = await f.read()
            try:
                results.append(_intake.intake_file(ledger, data, f.filename or "upload", thread_id))
            except _intake.IntakeRejected as e:
                results.append({"status": "rejected", "filename": f.filename, "reason": str(e)})
        return {"results": results, "pending": _intake.pending_count(ledger)}
    finally:
        ledger.close()


@app.get("/api/projecttree/intake/status")
def pt_intake_status():
    """取り込み済みだが未抽出の件数（extractor を回すべきか判断する材料）。"""
    ledger = get_ledger()
    try:
        pending = _intake.pending_count(ledger)
        total = ledger.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return {
            "documents": total,
            "pending_extraction": pending,
            "accepted_ext": sorted(_intake.ACCEPTED_EXT),
            "max_upload_mb": _intake.MAX_UPLOAD_BYTES // 1024 // 1024,
        }
    finally:
        ledger.close()


# ==========================================================================
# Phase 7: LLM プロバイダ切替（Anthropic 直 / Orca Router）
# 既存のエンドポイントは変更していない。以下はすべて追加。
# ==========================================================================

import os  # noqa: E402

from projecttree import provider as _prov  # noqa: E402


class OrcaKeyIn(BaseModel):
    api_key: str


@app.get("/api/config/provider")
def get_provider():
    """現在の接続先と、各工程で使うモデル名を返す。キー本体は返さない。"""
    return _prov.status()


@app.post("/api/config/provider/orca_key")
def set_orca_key(body: OrcaKeyIn):
    """Orca Router のキーを設定する。プロセスのメモリ上だけに保持する。"""
    key = (body.api_key or "").strip()
    if not key:
        raise HTTPException(400, "キーが空です")
    os.environ["ORCA_API_KEY"] = key
    os.environ["PT_LLM_PROVIDER"] = "orca"
    return _prov.status()


@app.delete("/api/config/provider/orca_key")
def clear_orca_key():
    os.environ.pop("ORCA_API_KEY", None)
    os.environ.pop("PT_LLM_PROVIDER", None)
    return _prov.status()


@app.post("/api/config/provider/select")
def select_provider(name: str):
    """接続先を明示的に切り替える（orca / anthropic）。"""
    if name not in ("orca", "anthropic"):
        raise HTTPException(400, "provider は orca / anthropic のいずれかです")
    if name == "orca" and not os.environ.get("ORCA_API_KEY"):
        raise HTTPException(400, "先に Orca のキーを設定してください")
    os.environ["PT_LLM_PROVIDER"] = name
    return _prov.status()


@app.get("/api/config/provider/probe")
def probe_provider():
    """実際に1回だけ最小トークンで呼び出し、疎通を確認する。"""
    return _prov.probe()


# ==========================================================================
# Phase 8: proposal.md 対応（フォルダ取込 / マスキング / モデル割当 /
#          LLM推論 / 資料用画像 / 会議用資料 / 3D進捗管理）
# 既存のエンドポイントは変更していない。以下はすべて追加。
# ==========================================================================

from projecttree import docs as _docs          # noqa: E402,F401  (BUILDERS 登録のため)
from projecttree import inference as _inf      # noqa: E402
from projecttree import masking as _mask       # noqa: E402
from projecttree import models as _mdl         # noqa: E402
from projecttree import progress as _prg       # noqa: E402
from projecttree import slides as _sld         # noqa: E402


# ---------------------------------------------------------------- 出力形式

@app.get("/api/projecttree/formats")
def pt_formats():
    """ダウンロードできる形式の一覧。UI の右クリックメニューはこれを引く。"""
    return {"formats": [{"fmt": f, "label": _exp.FORMAT_LABELS.get(f, f),
                         "ext": _exp.FILE_EXT.get(f, f)}
                        for f in ("md", "pdf", "docx", "pptx", "pptx_img", "xlsx")]}


@app.get("/api/projecttree/export2")
def pt_export2(thread_id: str, fmt: str, stage_no: int | None = None):
    """pdf / word / 画像入りpptx を含む出力。既存の /export はそのまま残してある。"""
    if fmt not in _exp.BUILDERS:
        raise HTTPException(400, f"unsupported format: {fmt}")
    ledger = get_ledger()
    try:
        data, name, media = _exp.build_ext(ledger, thread_id, fmt, stage_no)
    except ValueError as e:
        raise HTTPException(404, str(e))
    finally:
        ledger.close()
    quoted = urllib.parse.quote(name)
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quoted})


# ---------------------------------------------------------------- 資料用画像

@app.post("/api/projecttree/slides")
def pt_slides_build(thread_id: str = Form(...)):
    """段階ごとの資料用画像を作り直す。LLM は呼ばないので原価は 0。"""
    ledger = get_ledger()
    try:
        return {"images": _sld.generate(ledger, thread_id), "cost_usd": 0.0}
    except _sld.ThreadNotFound as e:
        raise HTTPException(404, str(e))
    finally:
        ledger.close()


@app.get("/api/projecttree/slides/{thread_id}/{stage_no}.png")
def pt_slide_png(thread_id: str, stage_no: int):
    ledger = get_ledger()
    try:
        imgs = _sld.ensure_images(ledger, thread_id)
    except _sld.ThreadNotFound as e:
        raise HTTPException(404, str(e))
    finally:
        ledger.close()
    if stage_no not in imgs:
        raise HTTPException(404, "画像がありません")
    return Response(content=imgs[stage_no].read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- 機密マスキング

class MaskIn(BaseModel):
    text: str
    allow_sensitive: bool = False


@app.post("/api/security/preflight")
def sec_preflight(body: MaskIn):
    """API へ送る前の確認。何を伏せ、何文字送るかを返す。原文は返さない。"""
    return _mask.preflight(body.text, allow_sensitive=body.allow_sensitive)


@app.post("/api/security/mask_preview")
def sec_mask_preview(body: MaskIn):
    """伏字にした後の本文を見せる（復元表は返さない）。"""
    r = _mask.mask(body.text)
    return {"masked_text": r.text, **r.summary()}


# ---------------------------------------------------------------- モデル割当

@app.get("/api/config/models")
def cfg_models():
    return {"family": _mdl.family(), "provider": _prov.active_provider(),
            "assignments": _mdl.assignment_table()}


@app.post("/api/config/models/family")
def cfg_models_family(name: str = Form(...)):
    if name not in ("claude", "qwen", "codex"):
        raise HTTPException(400, "family は claude / qwen / codex のいずれかです")
    if name != "claude" and _prov.active_provider() != "orca":
        raise HTTPException(400, name + " は Orca Router 経由でのみ使えます")
    os.environ["PT_MODEL_FAMILY"] = name
    return {"family": _mdl.family(), "assignments": _mdl.assignment_table()}


@app.get("/api/config/models/probe_alt")
def cfg_models_probe_alt():
    """Orca 上の Qwen / codex が実際に応答するかを1回ずつ確認する。"""
    return {"results": _mdl.probe_alternatives()}


# ---------------------------------------------------------------- LLM 推論

class InferIn(BaseModel):
    thread_id: str
    question: str
    allow_sensitive: bool = False
    dry_run: bool = False


@app.post("/api/projecttree/infer")
def pt_infer(body: InferIn):
    """プロンプトから、台帳の証拠だけを根拠に推論する（proposal.md 2-1）。"""
    ledger = get_ledger()
    try:
        _inf.ensure_tables(ledger)
        return _inf.infer_from_prompt(ledger, body.thread_id, body.question,
                                      allow_sensitive=body.allow_sensitive,
                                      dry_run=body.dry_run)
    finally:
        ledger.close()


@app.post("/api/projecttree/stages/llm")
def pt_stages_llm(thread_id: str = Form(...), dry_run: bool = Form(False)):
    """ルールで決まらなかった記録だけを LLM が段階分類する（proposal.md 2-2）。"""
    ledger = get_ledger()
    try:
        return _inf.classify_stages_llm(ledger, thread_id, dry_run=dry_run)
    finally:
        ledger.close()


@app.post("/api/projecttree/parts/infer")
def pt_parts_infer(thread_id: str = Form(...), dry_run: bool = Form(False)):
    """モデル部位と段階・時系列を LLM が対応づける（proposal.md 2-4）。"""
    ledger = get_ledger()
    try:
        _inf.ensure_tables(ledger)
        return _inf.infer_parts(ledger, thread_id, dry_run=dry_run)
    finally:
        ledger.close()


@app.get("/api/projecttree/parts/links")
def pt_parts_links(thread_id: str):
    """部位に紐付いた記録。モデルをクリックしたときに引く。"""
    ledger = get_ledger()
    try:
        _inf.ensure_tables(ledger)
        rows = ledger.conn.execute(
            "SELECT pe.part_key, pe.stage_no, pe.reason, e.event_id, e.occurred_on, e.summary "
            "FROM part_events pe JOIN events e ON e.event_id = pe.event_id "
            "WHERE pe.thread_id = ? ORDER BY pe.part_key, e.occurred_on", (thread_id,)).fetchall()
        return {"links": [dict(r) for r in rows]}
    finally:
        ledger.close()


# ---------------------------------------------------------------- 3D進捗管理

@app.get("/api/projecttree/progress/presets")
def pt_presets():
    return {"presets": _prg.presets()}


@app.post("/api/projecttree/progress/structure")
def pt_add_structure(thread_id: str = Form(...), preset: str = Form(...)):
    """基本構造の追加。ローカル生成なので原価は 0。"""
    ledger = get_ledger()
    try:
        return _prg.add_structure(ledger, thread_id, preset)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        ledger.close()


@app.post("/api/projecttree/progress/photo")
async def pt_from_photo(thread_id: str = Form(...), confirm: bool = Form(False),
                        file: UploadFile = File(...)):
    """写真から3Dモデルを作る。confirm=false のときは見積りだけ返し API は呼ばない。"""
    data = await file.read()
    media = file.content_type or "image/jpeg"
    if media not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(400, "jpeg / png / webp のみ対応します")
    ledger = get_ledger()
    try:
        return _prg.from_photo(ledger, thread_id, data, media, confirm=confirm)
    finally:
        ledger.close()


@app.post("/api/projecttree/progress/import")
async def pt_import_model(thread_id: str = Form(...), files: list[UploadFile] = File(...)):
    """図面・モデルの取込（2D: dxf/svg/dwg/pdf、3D: glb/stl/obj/ifc 等）。"""
    ledger = get_ledger()
    try:
        out = []
        for f in files:
            out.append(_prg.import_model(ledger, thread_id, f.filename or "noname", await f.read()))
        return {"results": out}
    finally:
        ledger.close()


@app.get("/api/projecttree/progress/uploads")
def pt_list_uploads(thread_id: str):
    ledger = get_ledger()
    try:
        return {"uploads": _prg.list_uploads(ledger, thread_id)}
    finally:
        ledger.close()


@app.get("/api/projecttree/progress/parts")
def pt_progress_parts(thread_id: str, sync: bool = True):
    """部材の進捗一覧。sync=true なら台帳から進捗を取り直す（手入力は保護される）。"""
    ledger = get_ledger()
    try:
        _prg.ensure_tables(ledger)
        if sync:
            _prg.sync_from_ledger(ledger, thread_id)
        return {"parts": _prg.part_list(ledger, thread_id),
                "overall": _prg.overall(ledger, thread_id)}
    finally:
        ledger.close()


@app.post("/api/projecttree/progress/set")
def pt_progress_set(thread_id: str = Form(...), part_key: str = Form(...),
                    percent: int = Form(...), note: str = Form("")):
    ledger = get_ledger()
    try:
        r = _prg.set_progress(ledger, thread_id, part_key, percent, note)
        r["overall"] = _prg.overall(ledger, thread_id)
        return r
    except _prg.PartNotFound as e:
        raise HTTPException(404, str(e))
    finally:
        ledger.close()


@app.get("/api/projecttree/progress/overall")
def pt_progress_overall(thread_id: str | None = None):
    """thread_id 指定なら構造物の全体進捗、省略なら全案件の進捗。"""
    ledger = get_ledger()
    try:
        _prg.ensure_tables(ledger)
        if thread_id:
            _prg.sync_from_ledger(ledger, thread_id)
            return _prg.overall(ledger, thread_id)
        return _prg.project_overall(ledger)
    finally:
        ledger.close()


# ==========================================================================
# Phase 9: 画像のLLM読取 / プロジェクトCRUD / Blender連携
# 既存のエンドポイントは変更していない。以下はすべて追加。
# ==========================================================================

from projecttree import blender as _bl        # noqa: E402
from projecttree import projects as _proj     # noqa: E402
from projecttree import vision as _vis        # noqa: E402


# ---------------------------------------------------------------- 画像のLLM読取

@app.post("/api/projecttree/intake/image_llm")
async def pt_intake_image_llm(thread_id: str | None = Form(None),
                              confirm: bool = Form(False),
                              files: list[UploadFile] = File(...)):
    """画像をLLMに読ませ、記述文を台帳へ入れる（proposal.md 2-1）。

    confirm=false のときは見積りだけ返し、API は呼ばない。
    """
    ledger = get_ledger()
    try:
        out = []
        for f in files:
            data = await f.read()
            out.append(_vis.intake_image(ledger, data, f.filename or "noname.jpg",
                                         thread_id=thread_id, confirm=confirm))
        total = sum(r.get("cost_usd", 0) or 0 for r in out)
        est = sum((r.get("estimate") or {}).get("usd", 0) for r in out)
        return {"results": out, "cost_usd": round(total, 4),
                "estimate_usd": round(est, 4) if est else None}
    finally:
        ledger.close()


# ---------------------------------------------------------------- プロジェクト管理

class ProjectIn(BaseModel):
    name: str
    aliases: list[str] | None = None


class RenameIn(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None


class MergeIn(BaseModel):
    into_id: str
    from_ids: list[str]


@app.get("/api/projecttree/projects")
def pt_projects():
    """案件一覧。中身の件数付きで返すので、消して良いか画面で判断できる。"""
    ledger = get_ledger()
    try:
        return {"projects": _proj.listing(ledger)}
    finally:
        ledger.close()


@app.post("/api/projecttree/projects")
def pt_project_create(body: ProjectIn):
    ledger = get_ledger()
    try:
        return _proj.create(ledger, body.name, body.aliases)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        ledger.close()


@app.patch("/api/projecttree/projects/{thread_id}")
def pt_project_rename(thread_id: str, body: RenameIn):
    ledger = get_ledger()
    try:
        return _proj.rename(ledger, thread_id, body.name, body.aliases)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        ledger.close()


@app.post("/api/projecttree/projects/merge")
def pt_project_merge(body: MergeIn):
    """割れてしまった案件を1つにまとめる（スレッド過剰分割の直し方）。"""
    ledger = get_ledger()
    try:
        return _proj.merge(ledger, body.into_id, body.from_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        ledger.close()


@app.get("/api/projecttree/projects/{thread_id}/delete_preview")
def pt_project_delete_preview(thread_id: str):
    """消す前に、何がどれだけ消えるかを返す。"""
    ledger = get_ledger()
    try:
        return _proj.delete_preview(ledger, thread_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    finally:
        ledger.close()


@app.delete("/api/projecttree/projects/{thread_id}")
def pt_project_delete(thread_id: str, cascade: bool = False, keep_files: bool = True):
    """案件を削除する。中身がある場合は cascade=true が無いと拒否する。"""
    ledger = get_ledger()
    try:
        r = _proj.delete(ledger, thread_id, cascade=cascade, keep_files=keep_files)
        if r["status"] == "refused":
            raise HTTPException(409, f"中身が {r['total']} 件あります。cascade=true を指定してください。")
        return r
    except ValueError as e:
        raise HTTPException(404, str(e))
    finally:
        ledger.close()


# ---------------------------------------------------------------- Blender 連携

@app.get("/api/projecttree/blender/status")
def pt_blender_status():
    """Blender が繋がるかを確認する。落ちていてもアプリは動く。"""
    return _bl.available()


@app.post("/api/projecttree/blender/build")
def pt_blender_build(thread_id: str = Form(...)):
    """台帳の部材を Blender 側に組み立てる。LLM 不使用なので原価 0。"""
    ledger = get_ledger()
    try:
        return _bl.build(ledger, thread_id)
    finally:
        ledger.close()


@app.post("/api/projecttree/blender/show")
def pt_blender_show(thread_id: str = Form(...), upto_stage: int | None = Form(None)):
    """アプリの表示段階スライダーと同じ絞り込みを Blender 側へ反映する。"""
    return _bl.show_upto(thread_id, upto_stage)


@app.post("/api/projecttree/blender/export")
def pt_blender_export(thread_id: str = Form(...)):
    """Blender 側のモデルを GLB で書き出し、台帳の assets へ登録する。"""
    ledger = get_ledger()
    try:
        return _bl.export_glb(ledger, thread_id)
    finally:
        ledger.close()


# ==========================================================================
# Phase 10: Enhancement02.md 対応
#   1-1 イメージ図（生成・編集・PNG）/ 1-2 出力ボタン（一括実行）
#   1-3 フォルダ事前設定 / 1-4 進捗率（既存 API を UI から使う）
# 既存のエンドポイントは変更していない。以下はすべて追加。
# ==========================================================================

from projecttree import autorun as _auto        # noqa: E402
from projecttree import foldersync as _fsync    # noqa: E402
from projecttree import illustrate as _ill      # noqa: E402


# ---------------------------------------------------------------- イメージ図

@app.get("/api/projecttree/illust")
def pt_illust_list(thread_id: str):
    """この案件のイメージ図の一覧（いつ・いくらで作ったか・編集済みか）。"""
    ledger = get_ledger()
    try:
        return {"illustrations": _ill.listing(ledger, thread_id)}
    finally:
        ledger.close()


@app.get("/api/projecttree/illust/svg")
def pt_illust_svg(thread_id: str, stage_no: int | None = None,
                  event_id: str | None = None, edited: bool = True):
    """イメージ図の SVG を返す。画面はこれをそのまま埋め込む。"""
    ledger = get_ledger()
    try:
        svg = _ill.get_svg(ledger, thread_id, stage_no=stage_no,
                           event_id=event_id, edited=edited)
    finally:
        ledger.close()
    if svg is None:
        raise HTTPException(404, "イメージ図がありません")
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/projecttree/illust/png")
def pt_illust_png(thread_id: str, stage_no: int):
    """イメージ図の PNG。資料へ貼るのと同じ絵を画面でも使えるようにする。"""
    ledger = get_ledger()
    try:
        p = _ill.png_path(ledger, thread_id, stage_no)
    finally:
        ledger.close()
    if p is None:
        raise HTTPException(404, "イメージ図がありません")
    return Response(content=p.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "no-cache"})


@app.post("/api/projecttree/illust/generate")
async def pt_illust_generate(thread_id: str = Form(...),
                             stage_no: int | None = Form(None),
                             event_id: str | None = Form(None),
                             confirm: bool = Form(False),
                             photos: list[UploadFile] | None = File(None)):
    """イメージ図を作る。confirm=false のときは見積りだけ返し API は呼ばない。

    photos を付けると、その写真の形・配置を絵に反映させる（1-1「アップロードした
    画像があれば、イメージ図に活用」）。
    """
    shots: list[tuple[str, bytes, str]] = []
    for f in (photos or []):
        media = f.content_type or ""
        if media not in ("image/jpeg", "image/png", "image/webp"):
            continue
        shots.append((f.filename or "photo", await f.read(), media))

    ledger = get_ledger()
    try:
        return _ill.generate(ledger, thread_id, stage_no=stage_no,
                             event_id=event_id, photos=shots, confirm=confirm)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        ledger.close()


@app.get("/api/projecttree/illust/objects")
def pt_illust_objects(thread_id: str, stage_no: int | None = None,
                      event_id: str | None = None):
    """編集できるオブジェクトの一覧（画面の編集リスト用）。"""
    ledger = get_ledger()
    try:
        return {"objects": _ill.objects_of(ledger, thread_id, stage_no=stage_no,
                                           event_id=event_id)}
    finally:
        ledger.close()


class IllustEditIn(BaseModel):
    thread_id: str
    stage_no: int | None = None
    event_id: str | None = None
    edits: dict


@app.post("/api/projecttree/illust/edits")
def pt_illust_edits(body: IllustEditIn):
    """位置・大きさ・注記の編集を保存する。原本は書き換えないので元に戻せる。"""
    ledger = get_ledger()
    try:
        return _ill.save_edits(ledger, body.thread_id, body.edits,
                               stage_no=body.stage_no, event_id=body.event_id)
    finally:
        ledger.close()


@app.delete("/api/projecttree/illust/edits")
def pt_illust_reset(thread_id: str, stage_no: int | None = None,
                    event_id: str | None = None):
    ledger = get_ledger()
    try:
        return _ill.reset_edits(ledger, thread_id, stage_no=stage_no, event_id=event_id)
    finally:
        ledger.close()


# ---------------------------------------------------------------- 出力ボタン

class AutorunIn(BaseModel):
    thread_id: str | None = None
    project_name: str | None = None
    question: str | None = None
    steps: list[str] | None = None


@app.get("/api/projecttree/autorun/steps")
def pt_autorun_steps():
    return {"steps": [{"step": k, "label": v, "uses_llm": k in _auto.LLM_STEPS}
                      for k, v in _auto.STEPS]}


@app.post("/api/projecttree/autorun/estimate")
def pt_autorun_estimate(body: AutorunIn):
    """押す前に、走る工程と原価の合計を返す。API は呼ばない。"""
    ledger = get_ledger()
    try:
        return _auto.estimate(ledger, body.thread_id, steps=body.steps)
    finally:
        ledger.close()


@app.post("/api/projecttree/autorun")
def pt_autorun(body: AutorunIn):
    """「出力」ボタンの本体。工程を順に走らせ、結果と実測原価を返す。"""
    ledger = get_ledger()
    try:
        return _auto.run(ledger, thread_id=body.thread_id,
                         project_name=body.project_name,
                         question=body.question, steps=body.steps)
    finally:
        ledger.close()


# ---------------------------------------------------------------- フォルダ同期

@app.get("/api/projecttree/folder/spec")
def pt_folder_spec():
    """どのサブフォルダに何を置くかの一覧（Enhancement.md 3 の表）。"""
    return {"spec": _fsync.spec()}


@app.post("/api/projecttree/folder/sync")
async def pt_folder_sync(files: list[UploadFile] = File(...),
                         paths: list[str] = Form(...),
                         thread_id: str | None = Form(None),
                         project_name: str | None = Form(None),
                         create_project: bool = Form(True)):
    """プロジェクトフォルダを丸ごと取り込む。LLM を使わないので原価は 0。

    paths には各ファイルの webkitRelativePath を files と同じ並びで渡す。
    """
    if len(paths) != len(files):
        raise HTTPException(400, "paths と files の件数が一致しません")

    payload: list[tuple[str, bytes]] = []
    for rel, f in zip(paths, files):
        payload.append((rel or (f.filename or "noname"), await f.read()))

    ledger = get_ledger()
    try:
        return _fsync.sync(ledger, payload, thread_id=thread_id,
                           project_name=project_name, create_project=create_project)
    finally:
        ledger.close()


# ==========================================================================
# Phase 12: 不具合修正
#   assets の実体配信。生成物の置き場は static/models/ だけではない
#   （uploads/models/・assets/illust/・assets/blender/ にもある）ため、
#   static 配下に無いものは asset_id で引いて実体を返す。
#   既存の /static/models/... のURLはそのまま残してある。
# ==========================================================================

from paths import app_dir  # noqa: E402


@app.get("/api/projecttree/asset/{asset_id}")
def pt_asset_file(asset_id: str):
    """登録済みアセットの実体を返す。

    パスは DB に登録されたものだけを使い、アプリの作業ディレクトリ配下に
    収まっているかを確認してから返す（外部のファイルは配信しない）。
    """
    ledger = get_ledger()
    try:
        row = ledger.conn.execute(
            "SELECT path, fmt, kind FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    finally:
        ledger.close()
    if row is None:
        raise HTTPException(404, "アセットが見つかりません")

    p = Path(row["path"])
    roots = [app_dir().resolve(), Path(__file__).resolve().parent]
    try:
        rp = p.resolve()
    except OSError:
        raise HTTPException(404, "ファイルを開けません")
    if not any(str(rp).startswith(str(r)) for r in roots):
        raise HTTPException(403, "配信できない場所のファイルです")
    if not rp.is_file():
        raise HTTPException(404, "ファイルが見つかりません")

    media = {
        "glb": "model/gltf-binary", "gltf": "model/gltf+json",
        "stl": "model/stl", "obj": "text/plain", "ply": "text/plain",
        "svg": "image/svg+xml", "dxf": "application/dxf",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "pdf": "application/pdf",
    }.get((row["fmt"] or "").lower(), "application/octet-stream")
    return Response(content=rp.read_bytes(), media_type=media,
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/projecttree/projects/duplicates")
def pt_project_duplicates():
    """同名で割れている案件の一覧と、統合したときの計画を返す（変更しない）。"""
    ledger = get_ledger()
    try:
        return _proj.merge_duplicates(ledger, dry_run=True)
    finally:
        ledger.close()


@app.post("/api/projecttree/projects/dedup")
def pt_project_dedup():
    """同名で割れた案件をまとめる。モデルや段階を持つ案件を残す。"""
    ledger = get_ledger()
    try:
        return _proj.merge_duplicates(ledger, dry_run=False)
    finally:
        ledger.close()


# ==========================================================================
# Phase 13: 案件の表示・非表示（デモ用の絞り込み）
# データは消さず、一覧に出すかどうかだけを切り替える。既定は全表示。
# ==========================================================================

from projecttree import visibility as _vis  # noqa: E402


class VisibilityIn(BaseModel):
    thread_ids: list[str] | None = None
    keywords: list[str] | None = None
    hidden: bool = True


@app.get("/api/projecttree/visibility")
def pt_visibility_status():
    """今どの案件を表示しているか。"""
    ledger = get_ledger()
    try:
        return _vis.status(ledger)
    finally:
        ledger.close()


@app.post("/api/projecttree/visibility/set")
def pt_visibility_set(body: VisibilityIn):
    """指定した案件の表示・非表示を切り替える。"""
    ledger = get_ledger()
    try:
        return _vis.set_hidden(ledger, body.thread_ids or [], body.hidden)
    finally:
        ledger.close()


@app.post("/api/projecttree/visibility/keep_only")
def pt_visibility_keep_only(body: VisibilityIn):
    """指定した案件だけを表示する。keywords を渡すと名前で絞り込む。"""
    ledger = get_ledger()
    try:
        if body.keywords:
            return _vis.keep_by_keywords(ledger, body.keywords)
        return _vis.keep_only(ledger, body.thread_ids or [])
    finally:
        ledger.close()


@app.post("/api/projecttree/visibility/show_all")
def pt_visibility_show_all():
    """絞り込みを解除して全案件を表示に戻す。"""
    ledger = get_ledger()
    try:
        return _vis.show_all(ledger)
    finally:
        ledger.close()


# ==========================================================================
# Phase 14: 記録から 2D/3D モデルを起こす（プリセット非依存）
#   modelgen.py のプリセットは護岸・橋梁にしか当たらず、
#   それ以外の案件ではモデルを作れなかった。
#   写真が無くても台帳の記録だけから起こせる経路を足す。
#   既存の /api/projecttree/model（プリセット）と
#   /api/projecttree/progress/photo（写真）はそのまま残してある。
# ==========================================================================

from projecttree import modelgen_llm as _mgl  # noqa: E402


@app.post("/api/projecttree/model/llm/estimate")
def pt_model_llm_estimate(thread_id: str):
    """記録からモデルを起こす前の見積り。API はまだ呼ばない。"""
    ledger = get_ledger()
    try:
        return _mgl.generate(ledger, thread_id, confirm=False)
    finally:
        ledger.close()


@app.post("/api/projecttree/model/llm")
def pt_model_llm(thread_id: str, confirm: bool = False):
    """記録から 2D/3D モデルを起こす。プリセットへは落とさない。"""
    ledger = get_ledger()
    try:
        return _mgl.generate(ledger, thread_id, confirm=confirm)
    finally:
        ledger.close()
