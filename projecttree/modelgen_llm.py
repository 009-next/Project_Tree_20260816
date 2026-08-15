"""記録から 2D/3D モデルのパラメータを LLM に推定させる（プリセット非依存）。

modelgen.py は護岸・橋梁のプリセットしか持たないため、
案件名がキーワードに当たらない案件では 2D/3D モデルを作れなかった。
progress.from_photo は LLM を使うが写真が要る。
ここは「写真が無くても、台帳の記録だけからモデルを起こす」経路を足す。

方針は modelgen.py と同じ:
  LLM には寸法・形状のパラメータ JSON だけを出させ、
  実際の立体化・DXF/SVG 化はローカルのコードが行う。
  プリセットへは絶対に落とさない。返らなければ理由を返して終わる
  （黙ってそれらしい既製品を出すと、根拠のない図が資料に混ざるため）。

既存のコードは書き換えない。modelgen.py の組み立て・書き出し・登録関数を
そのまま呼び、source="llm" で登録する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
from ledger import Ledger  # noqa: E402
from paths import app_dir  # noqa: E402
from projecttree import modelgen as _mg  # noqa: E402
from projecttree import models as _models  # noqa: E402
from projecttree import provider as _prov  # noqa: E402
from projecttree import shapes_ext as _ext  # noqa: E402

TASK = "modelgen_llm"

SYSTEM = """建設・土木の案件記録から、その工事で作られる構造物の形を推定し、
3Dモデルの組み立てパラメータとして出してください。

出し方の決まり:
- 形は次の8種類から選ぶ。
    box      直方体。壁・床・基礎・躯体・土工など、いちばん基本
    cylinder 垂直に立つ円柱。橋脚・柱・立坑・煙突
    slope    台形断面。護岸・堤防のような両側が傾いた盛土
    arch     かまぼこ型（下が平らな半円）。トンネル・暗渠・アーチ屋根・カルバート
    pipe     横に寝た中空の管。上下水道管・配管・ヒューム管
    cone     円錐。ホッパー・土砂やアスファルト合材の仮置き山
    dome     半球。タンクの上部・貯水槽・ドーム屋根
    wedge    片流れ（片側だけ高い）。法面・片勾配の屋根・スロープ
- 構造物の性質に合う形を選ぶ。何でも box で済ませない。
  トンネルを box にする、管路を box にする、といったことはしない。
- size と pos は必ず3つの数値［X, Y, Z］で書く。単位はメートル。
- Y が高さ。地面は Y=0。部材は地面か他の部材の上に置き、宙に浮かせない。
- pos は部材の中心。たとえば高さ2mの基礎を地面に置くなら pos の Y は 1.0。
- stage は 1〜5。その部材が「どの段階で出来上がるか」を入れる。
  1=着手・仮設、2=基礎、3=主体構造、4=付帯・仕上げ、5=最終確認。
- 部材は4〜8個。記録に出てくる主要な構造物だけにする。
- part_key は英数字の識別子、name は日本語の部材名（例: 基礎工、外壁、屋根）。

形の使い分け（ここを外すと図が壊れる）:
- cylinder は「垂直に立つ円柱」になる。size は［直径, 高さ, 直径］と解釈される。
  橋脚・柱・立坑のように縦に立つものだけに使う。
- 横に寝た管は cylinder ではなく pipe を使う。size は［長さ, 外径, 外径］。
  上水道管・下水道管・ヒューム管・配管は必ず pipe にする。box にしない。
- トンネル・暗渠・カルバート・アーチ状の屋根は arch を使う。box にしない。
- 桁・梁のように断面が角ばった横長の部材は box でよい。

迷ったときの対応表:
    上下水道管・配管・管渠            -> pipe
    トンネル・暗渠・ボックスカルバート -> arch
    タンク上部・貯水槽・ドーム         -> dome
    土砂やアスファルトの仮置き山・ホッパー -> cone
    法面・片勾配の屋根・スロープ        -> wedge
    護岸・堤防のような両側が傾いた盛土  -> slope
    橋脚・柱・煙突・立坑              -> cylinder
    上記以外（壁・床・基礎・躯体・土工）-> box

大きさの目安（画面に収める）:
- 構造物全体が、おおむね 40m（幅）× 20m（高さ）× 40m（奥行）に収まるようにする。
- 管路・道路・水路のように延々と続くものは、全長をそのまま描かない。
  代表的な1区間（20〜40m程度）だけを取り出して描く。
- 極端に細長い形（片側が他方の20倍以上）にしない。画面でほとんど見えなくなる。

守ること:
- 記録に書かれている構造物を作る。記録に無い設備を足さない。
- 寸法が記録に無い場合は、その種類の工事で一般的な寸法を使ってよい。
  ただし部材どうしが重なったり離れて浮いたりしないよう、辻褄を合わせる。
- 建物なら階数ぶん積む、管路なら延長方向に伸ばす、というように
  その工事の種類に合った形にする。すべてを同じ箱の羅列にしない。"""


def _context(ledger: Ledger, thread_id: str) -> tuple[str, int]:
    """案件1件分の文脈。戻り値: (本文, 記録件数)"""
    th = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if th is None:
        return "", 0

    evs = ledger.conn.execute(
        "SELECT occurred_on, kind, summary FROM events "
        "WHERE thread_id = ? ORDER BY occurred_on LIMIT 30",
        (thread_id,)).fetchall()

    lines = [f"案件: {th['name']}", "", "この案件の記録:"]
    lines += [f"- {e['occurred_on']} ({e['kind']}) {e['summary']}" for e in evs]

    sts = ledger.conn.execute(
        "SELECT stage_no, summary FROM stages WHERE thread_id = ? ORDER BY stage_no",
        (thread_id,)).fetchall()
    if sts:
        lines += ["", "段階ごとの概要:"]
        lines += [f"- 段階{s['stage_no']}: {(s['summary'] or '')[:120]}" for s in sts]

    return "\n".join(lines), len(evs)


def estimate(context_chars: int) -> dict:
    """呼ぶ前の見積り。UI はこれを confirm ダイアログに出す。"""
    in_tok = max(1, context_chars // 2) + len(SYSTEM) // 2
    est = _models.estimate(TASK, in_tok, 1600)
    est["model"] = _models.model_for_task(TASK)
    return est


def _validate(params: dict) -> tuple[list[dict], list[str]]:
    """スキーマで縛れないところをコードで確かめる。落ちたものは捨てる。"""
    good, dropped = [], []
    for p in params.get("parts", []) or []:
        key = p.get("part_key") or p.get("name") or "?"
        size, pos = p.get("size"), p.get("pos")
        if not (isinstance(size, list) and len(size) == 3):
            dropped.append(f"{key}: size が3要素でない"); continue
        if not (isinstance(pos, list) and len(pos) == 3):
            dropped.append(f"{key}: pos が3要素でない"); continue
        if p.get("shape") not in _ext.ALL_SHAPES:
            dropped.append(f"{key}: 未対応の形状 {p.get('shape')}"); continue
        try:
            stage = int(p.get("stage", 0))
        except (TypeError, ValueError):
            dropped.append(f"{key}: stage が数値でない"); continue
        if not 1 <= stage <= 5:
            dropped.append(f"{key}: stage が1〜5の外 ({stage})"); continue
        if any(not isinstance(v, (int, float)) or v <= 0 for v in size):
            dropped.append(f"{key}: size に0以下がある"); continue
        p["stage"] = stage
        good.append(p)
    return good, dropped


def generate(ledger: Ledger, thread_id: str, *, confirm: bool = False) -> dict:
    """記録から 2D/3D 一式を作る。confirm=False なら見積りだけ返し API は呼ばない。

    プリセットには落とさない。LLM が使える形を返せなければ、理由を返して終わる。
    """
    row = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if row is None:
        return {"status": "error", "reason": f"案件が見つかりません: {thread_id}"}

    ctx, n_ev = _context(ledger, thread_id)
    if n_ev == 0:
        return {"status": "error",
                "reason": "この案件には記録がありません。先に資料を取り込んでください。"}

    est = estimate(len(ctx))
    if not confirm:
        return {"status": "estimate", "estimate": est,
                "context_chars": len(ctx), "events": n_ev}

    model = _models.model_for_task(TASK)
    # 形状の選択肢だけを差し替えた写しを渡す。modelgen.PARAMS_SCHEMA は書き換えない。
    schema = _models.sanitize_schema(_ext.extended_schema(_mg.PARAMS_SCHEMA))
    try:
        resp = _prov.get_client().messages.create(
            model=model, max_tokens=8000, system=SYSTEM,
            messages=[{"role": "user", "content":
                       ctx + "\n\nこの案件の構造物のパラメータを出してください。"}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except TypeError as e:
        if "authentication" in str(e).lower():
            return {"status": "error",
                    "reason": "APIキーが設定されていません。⚙設定 から登録してください。"}
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:200]}"}

    params, perr = _models.parse_json_response(resp)
    if perr:
        return {"status": "error", "reason": perr, "model": model}

    usage = llm.usage_dict(resp)
    cost = _prov.cost_usd(usage, model) or llm.cost_usd(usage, model)

    good, dropped = _validate(params or {})
    if not good:
        return {"status": "error",
                "reason": "使える部材が返りませんでした（" + " / ".join(dropped[:3]) + "）",
                "model": model, "usage": usage, "cost_usd": round(cost, 4)}

    params = {"type": params.get("type") or "llm", "units": "m", "parts": good}

    # ここから先はローカル。立体化は拡張形状に対応した shapes_ext 側で行う
    # （box / cylinder / slope はその中で modelgen へ委譲される）。
    # 2D の平面図は部材の footprint しか使わないため modelgen のものをそのまま使う。
    out = app_dir() / "assets" / "models"
    out.mkdir(parents=True, exist_ok=True)
    made: dict = {}

    full_glb = out / f"{thread_id}_llm_full.glb"
    used = _ext.export_glb(params, full_glb)
    _mg.register_asset(ledger, thread_id, "3d", "glb", full_glb, source="llm")
    made["full_glb"] = (full_glb.name, len(used))

    stl = out / f"{thread_id}_llm_full.stl"
    _ext.export_stl(params, stl)
    _mg.register_asset(ledger, thread_id, "3d", "stl", stl, source="llm")
    made["stl"] = stl.name

    for stage_no in range(1, 6):
        p = out / f"{thread_id}_llm_stage{stage_no}.glb"
        u = _ext.export_glb(params, p, upto_stage=stage_no)
        if u:
            _mg.register_asset(ledger, thread_id, "3d", "glb", p,
                               stage_no=stage_no, source="llm")
            made[f"stage{stage_no}"] = len(u)

    dxf = out / f"{thread_id}_llm_plan.dxf"
    _mg.export_dxf(params, dxf)
    _mg.register_asset(ledger, thread_id, "2d", "dxf", dxf, source="llm")
    svg = out / f"{thread_id}_llm_plan.svg"
    _mg.export_svg(params, svg)
    _mg.register_asset(ledger, thread_id, "2d", "svg", svg, source="llm")
    made["2d"] = [dxf.name, svg.name]

    # 台帳の shape は CHECK(box/cylinder/slope) で縛られているので、
    # 拡張形状は基本形状へ落とし、実際の形は params_json の ext_shape に残す。
    _mg.persist_parts(ledger, thread_id,
                      {**params, "parts": _ext.to_ledger_parts(good)}, source="llm")

    return {"status": "ok", "thread_id": thread_id, "name": row["name"],
            "source": "llm", "model": model, "usage": usage,
            "cost_usd": round(cost, 4), "estimate_usd": est["usd"],
            "parts": [{"part_key": p["part_key"], "name": p["name"],
                       "shape": p["shape"], "stage": p["stage"]} for p in good],
            "dropped": dropped, "made": made,
            "note": "寸法の推定のみ LLM。立体化・DXF/SVG 化はローカルで実行しました。"}
