"""イメージ図（漫画絵風の二次画像）の生成と編集（Enhancement02.md 1-1）。

これまでの資料用画像（slides.py）は、台帳の文字をそのまま図表に並べたものだった。
読めばわかるが、会議で一目で伝わる絵ではない。ここでは LLM に状況を絵にさせる。

なぜ SVG か:
  - Claude に画像生成機能は無いが、SVG なら「描ける」。
  - ベクターなので拡大しても荒れない（ホイール拡大の要件）。
  - 要素に id を振らせておけば、位置・大きさをあとから数値で編集できる。
    ラスタ画像だと編集が成立しない。

守る約束は他の LLM 経路と同じ:
  - 呼ぶ前に原価を見積もり、確認するまで呼ばない。
  - 生成物は台帳に紐付けて保存し、いつ・いくらで作ったかを残す。

編集の考え方:
  LLM が描いた SVG は「原本」として不変にしておき、利用者の編集は edits として
  別に持つ。描画時に原本へ edits を重ねる。こうすれば編集をやり直しても
  作り直し（＝再課金）にならないし、いつでも原本に戻せる。
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
from ledger import Ledger  # noqa: E402
from paths import app_dir  # noqa: E402
from projecttree import models as _models  # noqa: E402
from projecttree import provider as _prov  # noqa: E402

W, H = 800, 450          # 16:9。slides.py と同じ比率にして資料へそのまま貼れるようにする

STAGE_TITLES = {
    1: "状況確認", 2: "現状の課題", 3: "試行錯誤",
    4: "課題・変化", 5: "解決策、発展策の提案",
}

ILLUST_DDL = """
CREATE TABLE IF NOT EXISTS illustrations (
  illust_id  TEXT PRIMARY KEY,
  thread_id  TEXT NOT NULL,
  anchor     TEXT NOT NULL CHECK(anchor IN ('stage','event')),
  stage_no   INTEGER CHECK(stage_no BETWEEN 1 AND 5),
  event_id   TEXT,
  svg        TEXT NOT NULL,          -- LLM が描いた原本。編集では書き換えない。
  edits_json TEXT,                   -- 利用者の編集（位置・大きさ・注記）
  model      TEXT,
  cost_usd   REAL DEFAULT 0,
  photos     TEXT,                   -- 参考にしたアップロード画像のファイル名
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_illust_thread ON illustrations(thread_id, anchor, stage_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_illust_stage ON illustrations(thread_id, stage_no)
  WHERE anchor = 'stage';
CREATE UNIQUE INDEX IF NOT EXISTS idx_illust_event ON illustrations(thread_id, event_id)
  WHERE anchor = 'event';
"""


def ensure_tables(ledger: Ledger) -> None:
    ledger.conn.executescript(ILLUST_DDL)
    ledger.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def illust_dir() -> Path:
    d = app_dir() / "assets" / "illust"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------

SYSTEM = f"""建設・土木の現場の状況を、会議資料に載せる「イメージ図」として描いてください。
出力は SVG のみ。前置きも説明文も書かない。

決まりごと:
- 1枚だけ。ルートは <svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg"
  font-family="sans-serif"> とする。width/height 属性は付けない。
- 漫画・イラスト風。人物は棒人間や丸顔で可。重機・構造物は単純な図形の組み合わせ。
  写実性は不要で、状況が一目で伝わることを最優先にする。
- 意味のあるまとまり（人物・重機・構造物・吹き出し・注記など）は必ず
  <g class="pt-obj" id="任意の英数字ID" data-label="日本語の名前"> で包む。
  あとから利用者がこの単位で動かすので、包み忘れると編集できなくなる。
- <g> の中では絶対座標で描いてよい。transform は付けない（編集側が使う）。
- 文字は日本語。読める大きさ（font-size 13 以上）にする。
- 与えられた記録に無いものを描かない。数値を勝手に作らない。

絵が主役、文字は脇役（ここを外すと箇条書きの表になってしまう）:
- 記録を全部は描かない。その段階を最もよく表す場面を1つ選び、それを絵にする。
- 吹き出しや注記は合計4個まで。日付を並べた一覧にしない。
- 人物・重機・構造物・現場の様子を、画面のあちこちに具体的に描く。
  意味のない大きな単色の面（無地の四角）で画面を埋めない。
- 部材や人物は地面か他の部材の上に置く。宙に浮かせない。

ラベルの決まりごと（重なると読めなくなる）:
- 文字は単色で塗った矩形（rect）や吹き出しの上に置く。地面や構造物の上に直接置かない。
- ラベル同士を重ねない。対象の近くの空きスペースに置く。
- 1つのラベルは2行以内。長い記録は要点だけに削る。
- 背景は薄い色で全面を塗る。
"""

PHOTO_NOTE = """
- 参考写真を添付している。写真からは「どんな部材・設備があるか」「どんな色か」を読み取り、
  絵に登場させる。写真を写生するのではなく、写真に写っていたものを
  漫画・イラスト風に描き直すこと。カメラ角度までは真似しなくてよい。
"""

# 段階5だけは「起きたこと」ではなく「これからどうするか」を描く図になる。
PROPOSAL_NOTE = """
この図は段階5「解決策、発展策の提案」です。起きた課題を並べるだけの図にしないこと。

- 画面を左右に分ける。左に「今ある課題」、右に「どうするか（対応）」を描く。
  左から右へ太い矢印を引き、課題と対応が対になっていることを見せる。
- 対応は、与えられた記録・空白・食い違いから素直に導けるものだけにする。
  新しい日付・金額・数量を作らない。誰かが約束していない約束を書かない。
- 右側の対応には必ず「提案（未検証）」と分かる印を付ける。
  例: 右側の見出しに「提案・未検証」と書いた帯を置く。
  これは台帳に記録された事実ではなく、これから決めることだと読み手に伝えるため。
- 対応は3つまで。多く並べるより、効きそうなものを絞る。
"""


def _stage_context(ledger: Ledger, thread_id: str, stage_no: int) -> tuple[str, int]:
    """段階1つ分の文脈を組み立てる。戻り値: (本文, 記録件数)"""
    th = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    st = ledger.conn.execute(
        "SELECT stage_id, title, summary FROM stages WHERE thread_id = ? AND stage_no = ?",
        (thread_id, stage_no)).fetchone()
    if st is None:
        return "", 0

    evs = ledger.conn.execute(
        "SELECT e.occurred_on, e.kind, e.summary FROM stage_events se "
        "JOIN events e ON e.event_id = se.event_id "
        "WHERE se.stage_id = ? ORDER BY e.occurred_on LIMIT 12",
        (st["stage_id"],)).fetchall()

    lines = [f"案件: {th['name'] if th else thread_id}",
             f"段階{stage_no}「{STAGE_TITLES.get(stage_no, st['title'])}」",
             f"概要: {st['summary'] or ''}", "", "この段階の記録:"]
    lines += [f"- {e['occurred_on']} {e['summary']}" for e in evs]

    # 段階5「解決策、発展策の提案」は、他の段階と material が違う。
    # stage_events に入るのは patterns / gaps が検出した「未解決の論点」であって、
    # 提案そのものではない。そのまま絵にすると課題の列挙で終わってしまうので、
    # 対応を考える材料（記録の空白・食い違い・予測）を足して渡す。
    if stage_no == 5:
        gaps = ledger.conn.execute(
            "SELECT kind, description FROM gaps WHERE thread_id = ? LIMIT 6",
            (thread_id,)).fetchall()
        if gaps:
            lines += ["", "記録の空白（埋めるべきところ）:"]
            lines += [f"- ({g['kind']}) {g['description']}" for g in gaps]

        preds = ledger.conn.execute(
            "SELECT statement FROM insights WHERE thread_id = ? AND label = 'prediction' LIMIT 4",
            (thread_id,)).fetchall()
        if preds:
            lines += ["", "このままだとどうなるか（予測）:"]
            lines += [f"- {p['statement']}" for p in preds]

        disc = ledger.conn.execute(
            "SELECT DISTINCT o.name, d.attribute, d.explanation FROM discrepancies d "
            "JOIN objects o ON o.object_id = d.object_id "
            "JOIN claims c ON c.object_id = d.object_id "
            "JOIN events e ON e.doc_id = c.doc_id "
            "WHERE e.thread_id = ? AND d.status = 'open' LIMIT 4", (thread_id,)).fetchall()
        if disc:
            lines += ["", "未解消の食い違い（揃えるべきところ）:"]
            lines += [f"- {d['name']} / {d['attribute']}: {d['explanation']}" for d in disc]

    return "\n".join(lines), len(evs)


def _event_context(ledger: Ledger, thread_id: str, event_id: str) -> str:
    """記録1件分の文脈。時系列の空欄に差す小さめの図に使う。"""
    e = ledger.conn.execute(
        "SELECT e.occurred_on, e.kind, e.summary, e.span_quote, t.name "
        "FROM events e JOIN threads t ON t.thread_id = e.thread_id "
        "WHERE e.event_id = ?", (event_id,)).fetchone()
    if e is None:
        return ""
    lines = [f"案件: {e['name']}", f"日付: {e['occurred_on']}",
             f"内容: {e['summary']}"]
    if e["span_quote"]:
        lines.append(f"原文: {e['span_quote'][:200]}")
    return "\n".join(lines)


def estimate(context_chars: int, photos: int = 0) -> dict:
    """呼ぶ前の見積り。SVG は出力トークンを食うので多めに見る。"""
    in_tok = len(SYSTEM) // 2 + context_chars // 2 + photos * 2600
    est = _models.estimate("illustrate", in_tok, 3400)
    est["photos"] = photos
    est["note"] = "1枚あたりの概算。実測は生成後に返します。"
    return est


def _extract_svg(text: str) -> str | None:
    m = re.search(r"<svg\b.*?</svg>", text, re.S)
    return m.group(0) if m else None


def _sanitize_svg(svg: str) -> str:
    """外部参照とスクリプトを落とす。LLM の出力をそのまま画面に流さない。"""
    svg = re.sub(r"<script\b.*?</script>", "", svg, flags=re.S | re.I)
    svg = re.sub(r"\son\w+\s*=\s*\"[^\"]*\"", "", svg, flags=re.I)
    svg = re.sub(r"\son\w+\s*=\s*'[^']*'", "", svg, flags=re.I)
    # 外部 URL の参照を断つ（data: は自前で埋めた画像なので残す）
    svg = re.sub(r"(href|xlink:href)\s*=\s*\"(?!data:)[^\"]*\"", "", svg, flags=re.I)
    # width/height があるとブラウザ側で拡大縮小しにくいので落とす
    svg = re.sub(r"<svg([^>]*?)\swidth\s*=\s*\"[^\"]*\"", r"<svg\1", svg, count=1, flags=re.I)
    svg = re.sub(r"<svg([^>]*?)\sheight\s*=\s*\"[^\"]*\"", r"<svg\1", svg, count=1, flags=re.I)
    return svg.strip()


def _photo_blocks(photos: list[tuple[str, bytes, str]]) -> list[dict]:
    import base64
    out = []
    for name, data, media in photos[:3]:      # 参考写真は3枚まで
        out.append({"type": "image", "source": {
            "type": "base64", "media_type": media,
            "data": base64.b64encode(data).decode()}})
    return out


def generate(ledger: Ledger, thread_id: str, *, stage_no: int | None = None,
             event_id: str | None = None,
             photos: list[tuple[str, bytes, str]] | None = None,
             confirm: bool = False) -> dict:
    """イメージ図を1枚作る。confirm=False なら見積りだけ返し API は呼ばない。"""
    ensure_tables(ledger)
    photos = photos or []

    if stage_no is not None:
        ctx, n_ev = _stage_context(ledger, thread_id, stage_no)
        anchor = "stage"
        if not ctx:
            return {"status": "skipped",
                    "reason": f"段階{stage_no}がありません。先に段階分類を実行してください。"}
        if n_ev == 0:
            return {"status": "skipped",
                    "reason": f"段階{stage_no}に記録がありません。絵にする材料がないため作りません。"}
    elif event_id is not None:
        ctx = _event_context(ledger, thread_id, event_id)
        anchor = "event"
        if not ctx:
            return {"status": "skipped", "reason": "記録が見つかりません。"}
    else:
        raise ValueError("stage_no か event_id のどちらかを指定してください")

    est = estimate(len(ctx), len(photos))
    if not confirm:
        return {"status": "estimate", "estimate": est, "context_chars": len(ctx)}

    system = (SYSTEM
              + (PHOTO_NOTE if photos else "")
              + (PROPOSAL_NOTE if stage_no == 5 else ""))
    content: list[dict] = _photo_blocks(photos)
    content.append({"type": "text", "text": ctx})

    model = _models.model_for_task("illustrate")
    try:
        resp = _prov.get_client().messages.create(
            # SVG 本体だけで 3000〜6000 トークン使う。加えて拡張思考が先に
            # トークンを消費するため、上限が近いと thinking だけで打ち切られ、
            # テキストブロックが返らない。余裕を持たせる（実費は使った分のみ）。
            model=model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": content}])
    except TypeError as e:
        if "authentication" in str(e).lower():
            return {"status": "error", "reason": "APIキーが設定されていません。⚙設定 から登録してください。"}
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:200]}"}

    usage = llm.usage_dict(resp)
    cost = _prov.cost_usd(usage, model) or llm.cost_usd(usage, model)
    try:
        raw = _models.response_text(resp)
    except ValueError as e:
        return {"status": "error", "reason": str(e), "model": model}

    svg = _extract_svg(raw)
    if not svg:
        return {"status": "error", "reason": "SVG が返りませんでした（出力が途中で切れた可能性）",
                "model": model, "usage": usage, "cost_usd": round(cost, 4)}
    svg = _sanitize_svg(svg)

    iid = "ill_" + uuid.uuid4().hex[:20]
    names = json.dumps([p[0] for p in photos], ensure_ascii=False)
    # 同じ段階/記録に既にあれば差し替える（原本は1つに保つ）
    if anchor == "stage":
        ledger.conn.execute(
            "DELETE FROM illustrations WHERE thread_id = ? AND anchor = 'stage' AND stage_no = ?",
            (thread_id, stage_no))
    else:
        ledger.conn.execute(
            "DELETE FROM illustrations WHERE thread_id = ? AND anchor = 'event' AND event_id = ?",
            (thread_id, event_id))
    ledger.conn.execute(
        "INSERT INTO illustrations (illust_id, thread_id, anchor, stage_no, event_id, "
        "svg, edits_json, model, cost_usd, photos, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (iid, thread_id, anchor, stage_no, event_id, svg, model, cost, names, _now()))
    ledger.commit()

    return {"status": "ok", "illust_id": iid, "anchor": anchor,
            "stage_no": stage_no, "event_id": event_id,
            "objects": len(re.findall(r'class="pt-obj"', svg)),
            "svg_bytes": len(svg.encode("utf-8")),
            "model": model, "usage": usage,
            "cost_usd": round(cost, 4), "estimate_usd": est["usd"],
            "photos": [p[0] for p in photos]}


# --------------------------------------------------------------------------
# 取得・編集
# --------------------------------------------------------------------------

def _row(ledger: Ledger, thread_id: str, *, stage_no: int | None = None,
         event_id: str | None = None):
    ensure_tables(ledger)
    if stage_no is not None:
        return ledger.conn.execute(
            "SELECT * FROM illustrations WHERE thread_id = ? AND anchor = 'stage' AND stage_no = ?",
            (thread_id, stage_no)).fetchone()
    return ledger.conn.execute(
        "SELECT * FROM illustrations WHERE thread_id = ? AND anchor = 'event' AND event_id = ?",
        (thread_id, event_id)).fetchone()


def apply_edits(svg: str, edits: dict | None) -> str:
    """原本 SVG に編集を重ねた SVG を返す。原本は変えない。

    edits の形:
      {"objects": {"crane1": {"dx": 20, "dy": -10, "scale": 1.3, "hidden": false}},
       "notes": [{"x": 100, "y": 60, "text": "要確認", "color": "#c62828"}]}
    """
    if not edits:
        return svg

    objs = edits.get("objects") or {}
    for oid, e in objs.items():
        if not re.search(r'id="%s"' % re.escape(oid), svg):
            continue
        if e.get("hidden"):
            svg = re.sub(r'(<g\b[^>]*id="%s")' % re.escape(oid),
                         r'\1 style="display:none"', svg, count=1)
            continue
        dx, dy = float(e.get("dx", 0)), float(e.get("dy", 0))
        sc = float(e.get("scale", 1))
        if dx == 0 and dy == 0 and sc == 1:
            continue
        # 拡大は要素の中心を保つため、平行移動と組み合わせる
        cx, cy = e.get("cx"), e.get("cy")
        if sc != 1 and cx is not None and cy is not None:
            tr = (f"translate({dx + float(cx) * (1 - sc):.2f},"
                  f"{dy + float(cy) * (1 - sc):.2f}) scale({sc:.3f})")
        elif sc != 1:
            tr = f"translate({dx:.2f},{dy:.2f}) scale({sc:.3f})"
        else:
            tr = f"translate({dx:.2f},{dy:.2f})"
        svg = re.sub(r'(<g\b[^>]*id="%s"[^>]*)(>)' % re.escape(oid),
                     r'\1 transform="%s"\2' % tr, svg, count=1)

    notes = edits.get("notes") or []
    if notes:
        parts = ['<g class="pt-notes">']
        for nt in notes:
            x, y = float(nt.get("x", 20)), float(nt.get("y", 30))
            col = nt.get("color", "#c62828")
            txt = (nt.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if not txt:
                continue
            w = max(60, len(txt) * 15 + 16)
            parts.append(
                f'<rect x="{x - 8:.1f}" y="{y - 18:.1f}" width="{w}" height="26" rx="5" '
                f'fill="#fffbe6" stroke="{col}" stroke-width="1.5" opacity="0.95"/>'
                f'<text x="{x:.1f}" y="{y:.1f}" font-size="15" fill="{col}" '
                f'font-family="sans-serif">{txt}</text>')
        parts.append("</g>")
        svg = svg.replace("</svg>", "".join(parts) + "</svg>")
    return svg


def get_svg(ledger: Ledger, thread_id: str, *, stage_no: int | None = None,
            event_id: str | None = None, edited: bool = True) -> str | None:
    """保存済みイメージ図を返す。edited=True なら編集を反映した SVG。"""
    r = _row(ledger, thread_id, stage_no=stage_no, event_id=event_id)
    if r is None:
        return None
    if not edited:
        return r["svg"]
    try:
        edits = json.loads(r["edits_json"]) if r["edits_json"] else None
    except (TypeError, ValueError):
        edits = None
    return apply_edits(r["svg"], edits)


def save_edits(ledger: Ledger, thread_id: str, edits: dict, *,
               stage_no: int | None = None, event_id: str | None = None) -> dict:
    """編集を保存する。原本の SVG は書き換えないので、いつでも元に戻せる。"""
    r = _row(ledger, thread_id, stage_no=stage_no, event_id=event_id)
    if r is None:
        return {"status": "error", "reason": "イメージ図がありません。先に作成してください。"}
    ledger.conn.execute(
        "UPDATE illustrations SET edits_json = ? WHERE illust_id = ?",
        (json.dumps(edits, ensure_ascii=False), r["illust_id"]))
    ledger.commit()
    return {"status": "ok", "illust_id": r["illust_id"],
            "objects": len(edits.get("objects") or {}),
            "notes": len(edits.get("notes") or [])}


def reset_edits(ledger: Ledger, thread_id: str, *, stage_no: int | None = None,
                event_id: str | None = None) -> dict:
    r = _row(ledger, thread_id, stage_no=stage_no, event_id=event_id)
    if r is None:
        return {"status": "error", "reason": "イメージ図がありません。"}
    ledger.conn.execute(
        "UPDATE illustrations SET edits_json = NULL WHERE illust_id = ?", (r["illust_id"],))
    ledger.commit()
    return {"status": "ok", "note": "編集を破棄し、生成時の状態に戻しました。"}


def objects_of(ledger: Ledger, thread_id: str, *, stage_no: int | None = None,
               event_id: str | None = None) -> list[dict]:
    """編集できるオブジェクトの一覧。UI の編集リストに出す。"""
    r = _row(ledger, thread_id, stage_no=stage_no, event_id=event_id)
    if r is None:
        return []
    found = re.findall(r'<g\b[^>]*class="pt-obj"[^>]*>', r["svg"])
    out = []
    for tag in found:
        oid = re.search(r'id="([^"]+)"', tag)
        lab = re.search(r'data-label="([^"]+)"', tag)
        if oid:
            out.append({"id": oid.group(1),
                        "label": lab.group(1) if lab else oid.group(1)})
    return out


def listing(ledger: Ledger, thread_id: str) -> list[dict]:
    ensure_tables(ledger)
    rows = ledger.conn.execute(
        "SELECT illust_id, anchor, stage_no, event_id, model, cost_usd, photos, created_at, "
        "       (edits_json IS NOT NULL) AS edited "
        "FROM illustrations WHERE thread_id = ? ORDER BY anchor, stage_no, created_at",
        (thread_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# PNG 化（pptx / pdf / word へ貼るため）
# --------------------------------------------------------------------------

def to_png(svg: str, scale: float = 2.0) -> bytes | None:
    """SVG を PNG に変換する。失敗したら None（呼び出し側は既存画像へ退避する）。"""
    try:
        import fitz
        doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale))
        data = pix.tobytes("png")
        doc.close()
        return data
    except Exception:
        return None


def png_path(ledger: Ledger, thread_id: str, stage_no: int) -> Path | None:
    """段階のイメージ図を PNG にして保存し、そのパスを返す。無ければ None。"""
    svg = get_svg(ledger, thread_id, stage_no=stage_no)
    if not svg:
        return None
    data = to_png(svg)
    if not data:
        return None
    p = illust_dir() / f"{thread_id}_stage{stage_no}_illust.png"
    p.write_bytes(data)
    return p


def image_paths(ledger: Ledger, thread_id: str) -> dict[int, Path]:
    """段階番号 → イメージ図 PNG のパス。資料出力側はこれを見る。"""
    out: dict[int, Path] = {}
    for n in range(1, 6):
        p = png_path(ledger, thread_id, n)
        if p is not None:
            out[n] = p
    return out
