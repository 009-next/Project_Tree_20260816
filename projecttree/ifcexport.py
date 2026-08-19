"""台帳の記録を属性として載せた IFC を書き出す（CIM連携）。

CIM／BIM の実務でいちばん埋まらないのが**属性情報**。形状は作れても、
「この部材はいつ施工され、どんな協議を経て、今どうなっているか」は
手入力になるため、現場では空欄のまま渡ることが多い。

Project_Tree は議事録・メールから記録を抽出して台帳に持っているので、
そこを属性として流し込める。形状だけのモデルではなく、
**経緯が付いたモデル**を IFC で渡せる。

属性の作り方（LLM は使わない）:
  部材 → model_parts.stage_no → stages → stage_events → events
  という既存の紐付けを辿るだけ。ルールで決まるので再現性がある。
  LLM で部位と記録を対応づけた結果（part_events）があれば、そちらを優先する。

既存のモデル生成（modelgen / shapes_ext）には手を入れない。
台帳を読んで IFC を組み立てるだけの、独立した出力経路。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402

PSET = "Pset_ProjectTree"


class IfcUnavailable(Exception):
    """ifcopenshell が入っていない環境向け。"""


def available() -> bool:
    try:
        import ifcopenshell  # noqa: F401
        return True
    except Exception:
        return False


def _events_for_part(ledger: Ledger, thread_id: str, part_key: str,
                     stage_no: int) -> list[dict]:
    """部材に紐づく記録を集める。

    LLM が対応づけた part_events があればそれを使い、無ければ
    「同じ段階の記録」をルールで辿る。どちらも台帳の中だけで完結する。
    """
    rows = []
    try:
        rows = ledger.conn.execute(
            "SELECT e.occurred_on, e.kind, e.summary FROM part_events pe "
            "JOIN events e ON e.event_id = pe.event_id "
            "WHERE pe.thread_id = ? AND pe.part_key = ? "
            "ORDER BY e.occurred_on", (thread_id, part_key)).fetchall()
    except Exception:
        rows = []          # part_events は LLM 対応づけを一度も使っていないと無い

    if not rows:
        rows = ledger.conn.execute(
            "SELECT e.occurred_on, e.kind, e.summary FROM stages s "
            "JOIN stage_events se ON se.stage_id = s.stage_id "
            "JOIN events e ON e.event_id = se.event_id "
            "WHERE s.thread_id = ? AND s.stage_no = ? "
            "ORDER BY e.occurred_on", (thread_id, stage_no)).fetchall()

    return [{"date": r["occurred_on"], "kind": r["kind"], "summary": r["summary"]}
            for r in rows]


def build(ledger: Ledger, thread_id: str, out_path: Path,
          max_records: int = 8) -> dict:
    """1案件ぶんの IFC を作る。原価は 0（LLM を使わない）。"""
    if not available():
        raise IfcUnavailable(
            "ifcopenshell が入っていません。pip install ifcopenshell で導入できます。")

    import ifcopenshell.api as A

    th = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if th is None:
        raise ValueError(f"案件が見つかりません: {thread_id}")
    name = th["name"]

    parts = ledger.conn.execute(
        "SELECT part_key, name, shape, size_json, pos_json, stage_no, source "
        "FROM model_parts WHERE thread_id = ? ORDER BY stage_no, part_key",
        (thread_id,)).fetchall()
    if not parts:
        return {"status": "skipped",
                "reason": "この案件にはモデル部材がありません。"
                          "「基本構造の追加」か「記録から作成」で作れます。"}

    f = A.run("project.create_file", version="IFC4")
    proj = A.run("root.create_entity", f, ifc_class="IfcProject", name=name)
    A.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
    ctx = A.run("context.add_context", f, context_type="Model")
    body = A.run("context.add_context", f, context_type="Model",
                 context_identifier="Body", target_view="MODEL_VIEW", parent=ctx)
    site = A.run("root.create_entity", f, ifc_class="IfcSite", name="現場")
    A.run("aggregate.assign_object", f, products=[site], relating_object=proj)

    made, with_geom, total_records = 0, 0, 0
    for p in parts:
        el = A.run("root.create_entity", f,
                   ifc_class="IfcBuildingElementProxy", name=p["name"])
        A.run("spatial.assign_container", f, products=[el], relating_structure=site)

        # 形状。IFCの標準表現へ直方体で近似する（寸法と位置は台帳の値そのまま）。
        # アプリ内の表示は GLB を使うので、こちらは受け渡し用と割り切る。
        try:
            sx, sy, sz = (float(v) for v in json.loads(p["size_json"]))
            px, py, pz = (float(v) for v in json.loads(p["pos_json"]))
            rep = A.run("geometry.add_wall_representation", f, context=body,
                        length=sx, height=sy, thickness=sz)
            A.run("geometry.assign_representation", f, product=el, representation=rep)
            # 台帳は Y が高さ、IFC は Z が高さ。軸を入れ替える。
            A.run("geometry.edit_object_placement", f, product=el,
                  matrix=[[1, 0, 0, px], [0, 1, 0, pz], [0, 0, 1, py], [0, 0, 0, 1]])
            with_geom += 1
        except Exception:
            pass          # 形状が作れなくても属性は載せる

        evs = _events_for_part(ledger, thread_id, p["part_key"], p["stage_no"])
        total_records += len(evs)
        prog = ledger.conn.execute(
            "SELECT percent, status FROM part_progress "
            "WHERE thread_id = ? AND part_key = ?",
            (thread_id, p["part_key"])).fetchone()

        ps = A.run("pset.add_pset", f, product=el, name=PSET)
        A.run("pset.edit_pset", f, pset=ps, properties={
            "案件": name,
            "部材キー": p["part_key"],
            "施工段階": int(p["stage_no"]),
            "形状": p["shape"],
            "寸法_m": p["size_json"],
            "生成元": p["source"],
            "進捗率": int(prog["percent"]) if prog else 0,
            "状態": (prog["status"] if prog else "未設定"),
            "関連記録数": len(evs),
            "施工記録": " / ".join(
                f"{e['date']} {e['summary'][:60]}" for e in evs[:max_records]) or "なし",
            "出力日": date.today().isoformat(),
        })
        made += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    f.write(str(out_path))
    return {"status": "ok", "path": str(out_path),
            "bytes": out_path.stat().st_size,
            "elements": made, "with_geometry": with_geom,
            "records_attached": total_records, "cost_usd": 0.0,
            "note": "台帳の記録を属性（Pset_ProjectTree）として載せました。LLM 不使用のため原価は 0 です。"}
