"""3D進捗管理（proposal.md Part2 2-2 の5機能）。

  1. 基本構造の追加   … プリセット構造を追加し、段階・時系列へ結び付ける
  2. 写真から作成     … 写真から構造パラメータを推論（LLM。呼ぶ前に必ず原価を提示）
  3. 図面・モデル取込 … 2D/3D ファイルの受け取り
  4. 部材の進捗一覧   … 部材ごとの進捗率
  5. 全体進捗         … 案件・構造物の全体進捗

進捗率は「台帳の記録から機械的に決まる値」を既定にする。
段階Nの記録が台帳にあれば、段階Nの部材は着手～完了とみなす。
手入力で上書きもできるが、その場合は source='manual' として区別する。
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402
from paths import app_dir  # noqa: E402
from projecttree import modelgen as _mg  # noqa: E402

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS part_progress (
  thread_id  TEXT NOT NULL,
  part_key   TEXT NOT NULL,
  percent    INTEGER NOT NULL CHECK(percent BETWEEN 0 AND 100),
  status     TEXT NOT NULL CHECK(status IN ('未着手','進行中','完了')),
  source     TEXT NOT NULL CHECK(source IN ('ledger','manual')),
  note       TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (thread_id, part_key)
);

CREATE TABLE IF NOT EXISTS uploaded_models (
  upload_id   TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK(kind IN ('2d','3d')),
  fmt         TEXT NOT NULL,
  orig_name   TEXT NOT NULL,
  path        TEXT NOT NULL,
  bytes       INTEGER NOT NULL,
  uploaded_at TEXT NOT NULL
);
"""

# 取込を許す図面・モデル形式。実行可能形式は入れない。
MODEL_EXT_2D = {".dxf", ".svg", ".dwg", ".pdf"}
MODEL_EXT_3D = {".glb", ".gltf", ".stl", ".obj", ".ply", ".ifc", ".step", ".stp"}
MAX_MODEL_BYTES = 200 * 1024 * 1024      # 3D側の既存実装と同じ上限


def ensure_tables(ledger: Ledger) -> None:
    ledger.conn.executescript(PROGRESS_DDL)
    ledger.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uploads_dir() -> Path:
    d = app_dir() / "uploads" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# 1. 基本構造の追加
# --------------------------------------------------------------------------

def presets() -> list[dict]:
    return [{"key": k, "type": v["type"], "parts": len(v["parts"]),
             "part_names": [p["name"] for p in v["parts"]]}
            for k, v in _mg.PRESETS.items()]


def add_structure(ledger: Ledger, thread_id: str, preset: str,
                  out_dir: Path | None = None) -> dict:
    """プリセット構造を案件へ追加し、部材と段階の対応まで作る。LLM は呼ばない。"""
    if preset not in _mg.PRESETS:
        raise ValueError(f"未知のプリセット: {preset}")
    out = out_dir or (app_dir() / "assets" / "models")
    out.mkdir(parents=True, exist_ok=True)

    res = _mg.generate_for_thread(ledger, thread_id, out, preset=preset)
    ensure_tables(ledger)
    sync_from_ledger(ledger, thread_id)
    res["progress"] = overall(ledger, thread_id)
    res["cost_usd"] = 0.0
    res["note"] = "プリセットからローカル生成。LLM 不使用のため原価は 0 です。"
    return res


# --------------------------------------------------------------------------
# 2. 写真から作成（LLM。呼ぶ前に必ず原価を提示する）
# --------------------------------------------------------------------------

# modelgen.PARAMS_SCHEMA は minItems / minimum などを使っており、構造化出力では 400 になる。
# modelgen 側は変更せず、送信用に整形したものを使い、要素数は受信後にコードで検証する。
def _photo_schema() -> dict:
    from projecttree import models as _models
    s = _models.sanitize_schema(_mg.PARAMS_SCHEMA)
    for key in ("size", "pos"):
        s["properties"]["parts"]["items"]["properties"][key]["description"] = \
            "[x, y, z] の3要素。必ず3つ書くこと。"
    return s


PHOTO_SCHEMA = _photo_schema()

PHOTO_SYSTEM = """写真に写っている土木構造物を、直方体・円柱・法面の組み合わせとして記述してください。

厳守事項:
- 単位はメートル。写真から読み取れない寸法は、一般的な標準寸法で置く。
- stage は施工順（1が最初に造る部位、数字が大きいほど後）。
- 部位は6個以内。細部は作らない。
- 判断できない場合でも、必ず1つ以上の部位を返す。
"""


def photo_estimate(image_bytes: int, parts_expected: int = 6) -> dict:
    """写真からの生成にかかる原価の見積り（proposal.md「LLMでかかるトークンとコストを提示」）。"""
    from projecttree import models as _models
    # 画像は概ね (幅×高さ)/750 トークン。1600x1200 相当を上限として見積る。
    img_tokens = 2600
    in_tokens = img_tokens + len(PHOTO_SYSTEM) // 2
    out_tokens = parts_expected * 90
    est = _models.estimate("modelgen_llm", in_tokens, out_tokens)
    est["image_bytes"] = image_bytes
    est["note"] = "画像1枚あたりの概算。実測は生成後に返します。"
    return est


def from_photo(ledger: Ledger, thread_id: str, image_bytes: bytes, media_type: str,
               *, confirm: bool = False) -> dict:
    """写真から構造パラメータを推論し、ローカルで3Dを組み立てる。

    confirm=False のときは見積りだけ返し、API は呼ばない。
    """
    est = photo_estimate(len(image_bytes))
    if not confirm:
        return {"status": "estimate", "estimate": est,
                "message": "この内容で生成しますか。確認後に実行されます。"}

    import base64
    from projecttree import models as _models
    from projecttree import provider as _prov
    import llm

    model = _models.model_for_task("modelgen_llm")
    try:
        resp = _prov.get_client().messages.create(
            model=model, max_tokens=3000, system=PHOTO_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": "この構造物のパラメータを出してください。"},
            ]}],
            output_config={"format": {"type": "json_schema", "schema": PHOTO_SCHEMA}},
        )
    except TypeError as e:
        if "authentication" in str(e).lower():
            return {"status": "error", "reason": "APIキーが設定されていません。⚙設定 から登録してください。"}
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:200]}"}

    params, perr = _models.parse_json_response(resp)
    if perr:
        return {"status": "error", "reason": perr, "model": model}
    usage = llm.usage_dict(resp)
    cost = _prov.cost_usd(usage, model) or llm.cost_usd(usage, model)

    # スキーマで縛れない長さをここで検証する。3要素でない部位は捨てる。
    good, dropped = [], []
    for p in params.get("parts", []):
        if len(p.get("size", [])) == 3 and len(p.get("pos", [])) == 3:
            good.append(p)
        else:
            dropped.append(p.get("part_key") or p.get("name"))
    if not good:
        return {"status": "error", "reason": "使える部位が返りませんでした（座標の要素数が不正）",
                "model": model, "usage": usage, "cost_usd": round(cost, 4)}
    params["parts"] = good

    # 形状生成はローカル。LLM にはパラメータだけを出させる（insight の rule レーンと同じ思想）
    out = app_dir() / "assets" / "models"
    out.mkdir(parents=True, exist_ok=True)
    _mg.persist_parts(ledger, thread_id, params, source="llm")
    glb = out / f"{thread_id}.glb"
    _mg.export_glb(params, glb)
    _mg.register_asset(ledger, thread_id, "3d", "glb", glb, source="llm")

    ensure_tables(ledger)
    sync_from_ledger(ledger, thread_id)

    return {"status": "ok", "model": model, "usage": usage,
            "cost_usd": round(cost, 4), "estimate_usd": est["usd"],
            "parts": [{"part_key": p["part_key"], "name": p["name"], "stage": p["stage"]}
                      for p in params["parts"]],
            "dropped_parts": dropped,
            "glb": str(glb), "progress": overall(ledger, thread_id)}


# --------------------------------------------------------------------------
# 3. 図面・モデル取込
# --------------------------------------------------------------------------

def import_model(ledger: Ledger, thread_id: str, filename: str, data: bytes) -> dict:
    """2D図面・3Dモデルを受け取って保存する。名前はこちらで付け直す。"""
    ensure_tables(ledger)
    ext = Path(filename).suffix.lower()
    if ext in MODEL_EXT_2D:
        kind = "2d"
    elif ext in MODEL_EXT_3D:
        kind = "3d"
    else:
        return {"status": "rejected", "filename": filename,
                "reason": f"未対応の形式です（{ext or '拡張子なし'}）"}
    if len(data) > MAX_MODEL_BYTES:
        return {"status": "rejected", "filename": filename,
                "reason": f"{len(data)/1048576:.1f}MB は上限 {MAX_MODEL_BYTES//1048576}MB を超えます"}

    uid = "upl_" + uuid.uuid4().hex[:20]
    # 元のファイル名はDBにだけ残し、ディスク上はランダムIDにする（パス経由の細工を断つ）
    path = uploads_dir() / f"{uid}{ext}"
    path.write_bytes(data)

    ledger.conn.execute(
        "INSERT INTO uploaded_models (upload_id, thread_id, kind, fmt, orig_name, path, bytes, uploaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, thread_id, kind, ext.lstrip("."), Path(filename).name, str(path), len(data), _now()))
    ledger.conn.execute(
        "INSERT INTO assets (asset_id, thread_id, stage_no, kind, fmt, path, generated_at, source) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?, 'local')",
        ("ast_" + uuid.uuid4().hex[:20], thread_id, kind, ext.lstrip("."), str(path), _now()))
    ledger.commit()
    return {"status": "ok", "upload_id": uid, "kind": kind, "fmt": ext.lstrip("."),
            "filename": Path(filename).name, "bytes": len(data)}


def list_uploads(ledger: Ledger, thread_id: str) -> list[dict]:
    ensure_tables(ledger)
    rows = ledger.conn.execute(
        "SELECT upload_id, kind, fmt, orig_name, bytes, uploaded_at FROM uploaded_models "
        "WHERE thread_id = ? ORDER BY uploaded_at DESC", (thread_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# 4. 部材の進捗一覧
# --------------------------------------------------------------------------

def sync_from_ledger(ledger: Ledger, thread_id: str) -> int:
    """台帳の記録から部材進捗を機械的に決める。手入力(manual)は上書きしない。

    判定は単純に保つ。段階Nに記録があれば段階Nの部材は着手済み、
    より後の段階に記録があれば、その部材は完了とみなす。

    ただし段階5は「解決策・発展策の提案」であって施工の記録ではない。
    提案が出ていることを施工の進捗と読むと全部材が完了扱いになるので、
    到達段階の判定には段階1〜4だけを使う。
    """
    ensure_tables(ledger)
    parts = ledger.conn.execute(
        "SELECT part_key, name, stage_no FROM model_parts WHERE thread_id = ? ORDER BY stage_no",
        (thread_id,)).fetchall()
    if not parts:
        return 0

    counts = {n: 0 for n in range(1, 6)}
    for r in ledger.conn.execute(
            "SELECT s.stage_no, COUNT(*) c FROM stages s "
            "JOIN stage_events se ON se.stage_id = s.stage_id "
            "WHERE s.thread_id = ? GROUP BY s.stage_no", (thread_id,)).fetchall():
        counts[r["stage_no"]] = r["c"]

    reached = max([n for n in range(1, 5) if counts[n] > 0], default=0)
    now = _now()
    n_upd = 0
    for p in parts:
        manual = ledger.conn.execute(
            "SELECT 1 FROM part_progress WHERE thread_id = ? AND part_key = ? AND source = 'manual'",
            (thread_id, p["part_key"])).fetchone()
        if manual:
            continue
        s = p["stage_no"]
        if reached > s:
            pct, status = 100, "完了"
        elif reached == s:
            pct, status = 50, "進行中"
        else:
            pct, status = 0, "未着手"
        note = f"段階{s}の部材／台帳は段階{reached}まで記録あり" if reached else "台帳に記録なし"
        ledger.conn.execute(
            "INSERT INTO part_progress (thread_id, part_key, percent, status, source, note, updated_at) "
            "VALUES (?, ?, ?, ?, 'ledger', ?, ?) "
            "ON CONFLICT(thread_id, part_key) DO UPDATE SET "
            "percent=excluded.percent, status=excluded.status, note=excluded.note, "
            "updated_at=excluded.updated_at WHERE part_progress.source = 'ledger'",
            (thread_id, p["part_key"], pct, status, note, now))
        n_upd += 1
    ledger.commit()
    return n_upd


class PartNotFound(Exception):
    """指定された案件、またはその部材が台帳に無い。"""


def set_progress(ledger: Ledger, thread_id: str, part_key: str,
                 percent: int, note: str = "") -> dict:
    """手入力で進捗を上書きする。以後この部材は台帳同期の対象外になる。"""
    ensure_tables(ledger)
    # SQLite は既定で外部キーを強制しないので、存在しない案件・部材でも
    # part_progress に行が入ってしまう。書き込む前に確かめる。
    if ledger.conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone() is None:
        raise PartNotFound(f"案件が見つかりません: {thread_id}")
    if ledger.conn.execute(
            "SELECT 1 FROM model_parts WHERE thread_id = ? AND part_key = ?",
            (thread_id, part_key)).fetchone() is None:
        raise PartNotFound(f"この案件に部材「{part_key}」がありません")
    percent = max(0, min(100, int(percent)))
    status = "完了" if percent >= 100 else ("未着手" if percent <= 0 else "進行中")
    ledger.conn.execute(
        "INSERT INTO part_progress (thread_id, part_key, percent, status, source, note, updated_at) "
        "VALUES (?, ?, ?, ?, 'manual', ?, ?) "
        "ON CONFLICT(thread_id, part_key) DO UPDATE SET "
        "percent=excluded.percent, status=excluded.status, source='manual', "
        "note=excluded.note, updated_at=excluded.updated_at",
        (thread_id, part_key, percent, status, note, _now()))
    ledger.commit()
    return {"part_key": part_key, "percent": percent, "status": status, "source": "manual"}


def part_list(ledger: Ledger, thread_id: str) -> list[dict]:
    """部材一覧。進捗と、その部材に紐付く記録の件数を併せて返す。"""
    ensure_tables(ledger)
    rows = ledger.conn.execute(
        "SELECT mp.part_key, mp.name, mp.shape, mp.stage_no, mp.size_json, mp.source AS model_source, "
        "       COALESCE(pp.percent, 0) AS percent, "
        "       COALESCE(pp.status, '未着手') AS status, "
        "       COALESCE(pp.source, 'ledger') AS progress_source, "
        "       pp.note, pp.updated_at "
        "FROM model_parts mp "
        "LEFT JOIN part_progress pp ON pp.thread_id = mp.thread_id AND pp.part_key = mp.part_key "
        "WHERE mp.thread_id = ? ORDER BY mp.stage_no, mp.part_key",
        (thread_id,)).fetchall()

    linked = {}
    try:
        for r in ledger.conn.execute(
                "SELECT part_key, COUNT(*) c FROM part_events WHERE thread_id = ? GROUP BY part_key",
                (thread_id,)).fetchall():
            linked[r["part_key"]] = r["c"]
    except Exception:
        pass   # part_events はLLM部位推論を一度も実行していなければ存在しない

    out = []
    for r in rows:
        d = dict(r)
        d["size"] = json.loads(d.pop("size_json"))
        d["linked_events"] = linked.get(d["part_key"], 0)
        out.append(d)
    return out


# --------------------------------------------------------------------------
# 5. 全体進捗
# --------------------------------------------------------------------------

def overall(ledger: Ledger, thread_id: str) -> dict:
    """構造物の全体進捗。部材の体積で重み付けする（大きい部材ほど寄与が大きい）。"""
    parts = part_list(ledger, thread_id)
    if not parts:
        return {"percent": 0, "parts": 0, "by_stage": {}, "note": "この案件にはモデル部材がありません。"}

    total_w = 0.0
    done_w = 0.0
    by_stage: dict[int, dict] = {}
    for p in parts:
        sx, sy, sz = p["size"]
        w = max(abs(sx * sy * sz), 0.001)
        total_w += w
        done_w += w * p["percent"] / 100
        b = by_stage.setdefault(p["stage_no"], {"parts": 0, "percent_sum": 0})
        b["parts"] += 1
        b["percent_sum"] += p["percent"]

    for b in by_stage.values():
        b["percent"] = round(b["percent_sum"] / b["parts"])
        b.pop("percent_sum")

    return {
        "percent": round(done_w / total_w * 100) if total_w else 0,
        "parts": len(parts),
        "completed": sum(1 for p in parts if p["percent"] >= 100),
        "in_progress": sum(1 for p in parts if 0 < p["percent"] < 100),
        "not_started": sum(1 for p in parts if p["percent"] <= 0),
        "by_stage": {str(k): v for k, v in sorted(by_stage.items())},
        "weighting": "部材の体積による加重平均",
    }


def project_overall(ledger: Ledger) -> dict:
    """全案件の進捗。モデルを持つ案件だけが対象。"""
    rows = ledger.conn.execute(
        "SELECT DISTINCT mp.thread_id, t.name FROM model_parts mp "
        "JOIN threads t ON t.thread_id = mp.thread_id ORDER BY t.name").fetchall()
    items = []
    for r in rows:
        o = overall(ledger, r["thread_id"])
        items.append({"thread_id": r["thread_id"], "name": r["name"],
                      "percent": o["percent"], "parts": o["parts"]})
    avg = round(sum(i["percent"] for i in items) / len(items)) if items else 0
    return {"projects": items, "percent": avg, "count": len(items)}
