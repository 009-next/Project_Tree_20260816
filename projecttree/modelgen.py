"""
Phase 2: 2D/3Dモデル生成（Enhancement.md 2-3-2, 2-3-3）

設計原則:
  LLM には「寸法・形状パラメータの JSON」だけを出させ、
  実際の形状生成はローカルで行う（insight.py の rule レーンと同じ思想）。
  既定は local（費用0円）。LLM も Meshy も任意の切り替え。

検証で判明した落とし穴（scratchpad で実機確認済み）:
  1. trimesh.creation.extrude_polygon は三角分割エンジン(triangle/mapbox-earcut)を
     要求し、未導入だと護岸の法面が生成できない。
     → 台形は頂点8・面12を直接構築する。追加依存ゼロ。
  2. trimesh 製 GLB は法線を含まず、three.js で真っ黒になる。
     → ビューア側で computeVertexNormals() を呼ぶ（static/projecttree.html 側で対応）。
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import trimesh
import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402

# LLM に出させる JSON のスキーマ。形状はこの3種に限定する
# （縛ることで、生成物が必ずローカルで組み立てられることを保証する）
PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "units": {"const": "m"},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "part_key": {"type": "string"},
                    "name": {"type": "string"},
                    "shape": {"enum": ["box", "cylinder", "slope"]},
                    "size": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                    "pos": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                    "stage": {"type": "integer", "minimum": 1, "maximum": 5},
                    "slope_ratio": {"type": ["number", "null"]},
                },
                "required": ["part_key", "name", "shape", "size", "pos", "stage"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["type", "units", "parts"],
    "additionalProperties": False,
}

# デモ用プリセット（Enhancement.md 1-3「護岸や橋梁工事で紹介」）
# LLM が使えない間はこれを使う。使える場合は LLM の出力で置き換える。
PRESETS = {
    "revetment": {
        "type": "revetment", "units": "m",
        "parts": [
            {"part_key": "base", "name": "基礎工", "shape": "box",
             "size": [30.0, 1.0, 3.0], "pos": [0, 0.5, 0], "stage": 1},
            {"part_key": "toe", "name": "根固工", "shape": "box",
             "size": [30.0, 0.8, 2.0], "pos": [0, 0.4, 2.5], "stage": 2},
            {"part_key": "block", "name": "護岸ブロック", "shape": "slope",
             "size": [30.0, 4.0, 5.0], "pos": [0, 1.0, 0], "stage": 3, "slope_ratio": 0.5},
            {"part_key": "capping", "name": "天端工", "shape": "box",
             "size": [30.0, 0.5, 2.0], "pos": [0, 5.0, -2.5], "stage": 4},
        ],
    },
    "bridge": {
        "type": "bridge", "units": "m",
        "parts": [
            {"part_key": "pier_1", "name": "橋脚P1", "shape": "cylinder",
             "size": [2.0, 8.0, 2.0], "pos": [-12, 4.0, 0], "stage": 1},
            {"part_key": "pier_2", "name": "橋脚P2", "shape": "cylinder",
             "size": [2.0, 8.0, 2.0], "pos": [12, 4.0, 0], "stage": 1},
            {"part_key": "girder", "name": "主桁", "shape": "box",
             "size": [40.0, 1.5, 8.0], "pos": [0, 8.75, 0], "stage": 3},
            {"part_key": "slab", "name": "床版", "shape": "box",
             "size": [40.0, 0.3, 9.0], "pos": [0, 9.65, 0], "stage": 4},
            {"part_key": "rail", "name": "高欄", "shape": "box",
             "size": [40.0, 1.0, 0.3], "pos": [0, 10.3, 4.35], "stage": 5},
        ],
    },
}

# 案件名からプリセットを選ぶキーワード（LLM 不使用の推定）
KEYWORD_PRESET = [
    (("護岸", "河川", "堤防"), "revetment"),
    (("橋梁", "橋", "高架"), "bridge"),
]


def pick_preset(thread_name: str) -> str | None:
    for keywords, preset in KEYWORD_PRESET:
        if any(k in thread_name for k in keywords):
            return preset
    return None


def build_part(p: dict) -> trimesh.Trimesh:
    """パラメータ1件 -> trimesh。"""
    sx, sy, sz = p["size"]
    shape = p["shape"]

    if shape == "box":
        m = trimesh.creation.box(extents=[sx, sy, sz])
    elif shape == "cylinder":
        m = trimesh.creation.cylinder(radius=sx / 2, height=sy, sections=24)
    elif shape == "slope":
        # 台形断面の押し出し。extrude_polygon は三角分割エンジンを要求するため使わない。
        r = p.get("slope_ratio") or 0.5
        top_z = max(sz - sy * r, 0.1)
        hx = sx / 2
        sec = [(-sz / 2, 0.0), (sz / 2, 0.0), (sz / 2 - (sz - top_z), sy), (-sz / 2, sy)]
        verts = [[-hx, yy, zz] for zz, yy in sec] + [[hx, yy, zz] for zz, yy in sec]
        faces = []
        for i in range(4):
            j = (i + 1) % 4
            faces += [[i, j, j + 4], [i, j + 4, i + 4]]
        faces += [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7]]
        m = trimesh.Trimesh(vertices=np.array(verts, dtype=float),
                            faces=np.array(faces, dtype=np.int64))
        m.fix_normals()
    else:
        raise ValueError(f"未対応の形状: {shape}")

    m.apply_translation(p["pos"])
    return m


def build_scene(params: dict, upto_stage: int | None = None) -> tuple[trimesh.Scene, list[dict]]:
    """upto_stage を指定すると、その段階までの部位だけを出す
    （Enhancement.md 1-3「指定段階と完成形のモデルの比較」）。"""
    scene = trimesh.Scene()
    used = []
    for p in params["parts"]:
        if upto_stage is not None and p["stage"] > upto_stage:
            continue
        mesh = build_part(p)
        # node_name / geom_name が three.js 側の o.name になる。クリック時の同定キー。
        scene.add_geometry(mesh, node_name=p["part_key"], geom_name=p["part_key"])
        used.append(p)
    return scene, used


def export_glb(params: dict, path: Path, upto_stage: int | None = None) -> list[dict]:
    scene, used = build_scene(params, upto_stage)
    scene.export(str(path))
    return used


def export_stl(params: dict, path: Path, upto_stage: int | None = None) -> None:
    scene, _ = build_scene(params, upto_stage)
    geoms = list(scene.geometry.values())
    if geoms:
        trimesh.util.concatenate(geoms).export(str(path))


def export_dxf(params: dict, path: Path) -> None:
    """2Dモデル: 平面図。部位ごとにレイヤーを分ける（クリック対象化のため）。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for p in params["parts"]:
        sx, _sy, sz = p["size"]
        cx, _cy, cz = p["pos"]
        msp.add_lwpolyline(
            [(cx - sx / 2, cz - sz / 2), (cx + sx / 2, cz - sz / 2),
             (cx + sx / 2, cz + sz / 2), (cx - sx / 2, cz + sz / 2)],
            close=True, dxfattribs={"layer": p["part_key"]},
        )
        msp.add_text(p["name"], dxfattribs={"layer": p["part_key"], "height": 0.6}) \
           .set_placement((cx - sx / 2, cz))
    doc.saveas(str(path))


def export_svg(params: dict, path: Path) -> None:
    """2Dモデル: 平面図。各部位に id と data-stage を付けてクリック可能にする。"""
    parts = params["parts"]
    minx = min(p["pos"][0] - p["size"][0] / 2 for p in parts) - 2
    maxx = max(p["pos"][0] + p["size"][0] / 2 for p in parts) + 2
    minz = min(p["pos"][2] - p["size"][2] / 2 for p in parts) - 2
    maxz = max(p["pos"][2] + p["size"][2] / 2 for p in parts) + 2
    w, h = maxx - minx, maxz - minz

    rects = []
    for p in parts:
        sx, _sy, sz = p["size"]
        cx, _cy, cz = p["pos"]
        rects.append(
            f'<rect class="pt-part" id="{p["part_key"]}" data-stage="{p["stage"]}" '
            f'data-name="{p["name"]}" '
            f'x="{cx - sx/2 - minx:.2f}" y="{cz - sz/2 - minz:.2f}" '
            f'width="{sx:.2f}" height="{sz:.2f}" '
            f'fill="#8ab4f8" stroke="#1a237e" stroke-width="0.12" opacity="0.75"/>'
        )
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.2f} {h:.2f}" '
           f'preserveAspectRatio="xMidYMid meet">' + "".join(rects) + "</svg>")
    path.write_text(svg, encoding="utf-8")


def persist_parts(ledger: Ledger, thread_id: str, params: dict, source: str = "local") -> None:
    """model_parts に部位と段階の対応を保存する（Enhancement.md 2-4）。"""
    ledger.conn.execute("DELETE FROM model_parts WHERE thread_id = ?", (thread_id,))
    for p in params["parts"]:
        ledger.conn.execute(
            "INSERT INTO model_parts (part_id, thread_id, part_key, name, shape, "
            "size_json, pos_json, stage_no, params_json, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("prt_" + uuid.uuid4().hex[:20], thread_id, p["part_key"], p["name"], p["shape"],
             json.dumps(p["size"]), json.dumps(p["pos"]), p["stage"],
             json.dumps({k: v for k, v in p.items()
                         if k not in ("part_key", "name", "shape", "size", "pos", "stage")},
                        ensure_ascii=False),
             source),
        )
    ledger.commit()


def register_asset(ledger: Ledger, thread_id: str, kind: str, fmt: str,
                   path: Path, stage_no: int | None = None, source: str = "local") -> None:
    ledger.conn.execute(
        "INSERT INTO assets (asset_id, thread_id, stage_no, kind, fmt, path, generated_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ast_" + uuid.uuid4().hex[:20], thread_id, stage_no, kind, fmt, str(path),
         datetime.now(timezone.utc).isoformat(timespec="seconds"), source),
    )
    ledger.commit()


def generate_for_thread(ledger: Ledger, thread_id: str, out_dir: Path,
                        preset: str | None = None) -> dict:
    """1案件分の 2D/3D 一式を生成し、DBへ登録する。"""
    row = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"thread が見つかりません: {thread_id}")
    name = row["name"]

    key = preset or pick_preset(name)
    if key is None:
        return {"thread_id": thread_id, "name": name, "skipped": "対応するプリセットなし"}

    params = PRESETS[key]
    out_dir.mkdir(parents=True, exist_ok=True)
    made = {}

    # 完成形
    full_glb = out_dir / f"{thread_id}_full.glb"
    used = export_glb(params, full_glb)
    register_asset(ledger, thread_id, "3d", "glb", full_glb)
    made["full_glb"] = (full_glb.name, len(used))

    stl = out_dir / f"{thread_id}_full.stl"
    export_stl(params, stl)
    register_asset(ledger, thread_id, "3d", "stl", stl)
    made["stl"] = stl.name

    # 段階別（指定段階と完成形の比較）
    for stage_no in range(1, 6):
        p = out_dir / f"{thread_id}_stage{stage_no}.glb"
        u = export_glb(params, p, upto_stage=stage_no)
        if u:
            register_asset(ledger, thread_id, "3d", "glb", p, stage_no=stage_no)
            made[f"stage{stage_no}"] = len(u)

    # 2D
    dxf = out_dir / f"{thread_id}_plan.dxf"
    export_dxf(params, dxf)
    register_asset(ledger, thread_id, "2d", "dxf", dxf)
    svg = out_dir / f"{thread_id}_plan.svg"
    export_svg(params, svg)
    register_asset(ledger, thread_id, "2d", "svg", svg)
    made["2d"] = [dxf.name, svg.name]

    persist_parts(ledger, thread_id, params)
    made["preset"] = key
    made["name"] = name
    return made


def main():
    parser = argparse.ArgumentParser(description="2D/3Dモデル生成")
    parser.add_argument("--thread", default=None, help="thread_id。省略時はプリセット該当案件すべて")
    parser.add_argument("--out", default=None, help="出力先。既定は static/models")
    parser.add_argument("--preset", default=None, choices=list(PRESETS), help="プリセットを明示指定")
    args = parser.parse_args()

    ledger = Ledger()
    ledger.init_db()
    out_dir = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "static" / "models"

    if args.thread:
        targets = [args.thread]
    else:
        rows = ledger.conn.execute("SELECT thread_id, name FROM threads").fetchall()
        targets = [r["thread_id"] for r in rows if pick_preset(r["name"])]

    for tid in targets:
        result = generate_for_thread(ledger, tid, out_dir, preset=args.preset)
        if "skipped" in result:
            print(f"  SKIP {result['name']}: {result['skipped']}", file=sys.stderr)
        else:
            print(f"  {result['name']} [{result['preset']}]: "
                  f"完成形{result['full_glb'][1]}部位 / "
                  f"段階別 {[result.get(f'stage{n}') for n in range(1,6)]}", file=sys.stderr)

    ledger.close()


if __name__ == "__main__":
    main()
