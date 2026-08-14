"""Blender 連携（アプリから直接 Blender へ3Dモデルを組ませる）。

Blender の MCP アドオンは 127.0.0.1:9876 で待ち受け、
    {"type": "execute_code", "params": {"code": "..."}}
を1リクエスト1接続で受け取る素朴なソケットサーバーになっている。
Claude Code の MCP を経由せず、アプリから同じ口を叩けば、
画面の操作だけで Blender 側にモデルを組ませられる。

方針は modelgen.py と同じ:
  形状のパラメータは台帳（model_parts）が持ち、Blender には組み立てだけをさせる。
  つまり Blender が落ちていてもアプリは動く。ここは「あれば使える追加経路」。

Blender 側に残すもの:
  pt_thread_id / pt_part_key / pt_stage をカスタムプロパティで書き込む。
  これで Blender で編集して書き戻しても、どの案件のどの部材かを見失わない。
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402
from paths import app_dir  # noqa: E402

HOST, PORT = "127.0.0.1", 9876
TIMEOUT = 30.0

# 段階ごとの色。slides.STAGE_COLOR と同じ配色を 0-1 に直したもの。
STAGE_RGB = {
    1: (0.243, 0.420, 0.620),
    2: (0.541, 0.427, 0.231),
    3: (0.275, 0.494, 0.353),
    4: (0.541, 0.353, 0.231),
    5: (0.690, 0.165, 0.165),
}


def _send(payload: dict, timeout: float = TIMEOUT) -> dict:
    """1コマンドを送って応答を受け取る。Blender が落ちていても例外は投げない。"""
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(json.dumps(payload).encode("utf-8"))
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
                # 応答が JSON として閉じたら読み終わり
                try:
                    return json.loads(b"".join(chunks).decode("utf-8"))
                except json.JSONDecodeError:
                    continue
        raw = b"".join(chunks).decode("utf-8", "replace")
        return {"status": "error", "message": f"応答を解釈できませんでした: {raw[:200]}"}
    except (ConnectionRefusedError, OSError, socket.timeout) as e:
        return {"status": "error",
                "message": "Blender に接続できません。Blender を起動し、"
                           "3Dビューのサイドバー（N キー）→ BlenderMCP タブで "
                           "「Connect to MCP server」を押してください。",
                "detail": f"{type(e).__name__}: {e}"}


def available() -> dict:
    """Blender が繋がるかだけを確かめる。"""
    r = _send({"type": "get_scene_info", "params": {}}, timeout=6.0)
    if r.get("status") == "error":
        return {"connected": False, "reason": r.get("message"), "port": PORT}
    info = r.get("result")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except json.JSONDecodeError:
            info = {"raw": info[:200]}
    return {"connected": True, "port": PORT, "scene": info}


def _run(code: str, timeout: float = TIMEOUT) -> dict:
    r = _send({"type": "execute_code", "params": {"code": code}}, timeout=timeout)
    if r.get("status") == "error":
        return {"status": "error", "reason": r.get("message"), "detail": r.get("detail")}
    return {"status": "ok", "result": r.get("result")}


def parts_of(ledger: Ledger, thread_id: str) -> list[dict]:
    """台帳の部材をそのまま渡せる形にする。"""
    rows = ledger.conn.execute(
        "SELECT part_key, name, shape, size_json, pos_json, stage_no, params_json "
        "FROM model_parts WHERE thread_id = ? ORDER BY stage_no, part_key",
        (thread_id,)).fetchall()
    out = []
    for r in rows:
        try:
            extra = json.loads(r["params_json"] or "{}")
        except (TypeError, ValueError):
            extra = {}
        out.append({
            "part_key": r["part_key"], "name": r["name"], "shape": r["shape"],
            "size": json.loads(r["size_json"]), "pos": json.loads(r["pos_json"]),
            "stage": r["stage_no"], "slope_ratio": extra.get("slope_ratio", 0.5),
        })
    return out


_BUILD_TEMPLATE = '''
import bpy, json
from mathutils import Vector

DATA = json.loads(r"""__DATA__""")
parts = DATA["parts"]
tid = DATA["thread_id"]
name = DATA["name"]
STAGE_RGB = {int(k): tuple(v) + (1.0,) for k, v in DATA["colors"].items()}

coll_name = "PT_" + tid[-8:]
old = bpy.data.collections.get(coll_name)
if old is not None:
    for ob in list(old.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.data.collections.remove(old)
coll = bpy.data.collections.new(coll_name)
bpy.context.scene.collection.children.link(coll)

def mat_for(stage):
    nm = "PT_stage%d" % stage
    m = bpy.data.materials.get(nm)
    if m is None:
        m = bpy.data.materials.new(nm)
        m.use_nodes = True
        m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = STAGE_RGB[stage]
    return m

made = []
for p in parts:
    sx, sy, sz = p["size"]
    # アプリは Y が高さ、Blender は Z が高さ。軸を入れ替える。
    px, py, pz = p["pos"][0], p["pos"][2], p["pos"][1]

    if p["shape"] == "box":
        bpy.ops.mesh.primitive_cube_add(size=1, location=(px, py, pz))
        ob = bpy.context.active_object
        ob.scale = (sx, sz, sy)
    elif p["shape"] == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(radius=sx / 2, depth=sy, location=(px, py, pz))
        ob = bpy.context.active_object
    elif p["shape"] == "slope":
        # 台形断面を頂点から直接作る（三角分割ライブラリに依存しない）
        r = p.get("slope_ratio", 0.5)
        top = sz * (1 - r)
        hx, hz, hy = sx / 2, sz / 2, sy / 2
        verts = [(-hx, -hz, -hy), (hx, -hz, -hy), (hx, hz, -hy), (-hx, hz, -hy),
                 (-hx, -top / 2, hy), (hx, -top / 2, hy), (hx, top / 2, hy), (-hx, top / 2, hy)]
        faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4),
                 (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        me = bpy.data.meshes.new(p["part_key"])
        me.from_pydata(verts, [], faces)
        me.update()
        ob = bpy.data.objects.new(p["part_key"], me)
        bpy.context.scene.collection.objects.link(ob)
        ob.location = (px, py, pz)
    else:
        continue

    ob.name = "%d_%s" % (p["stage"], p["name"])
    ob.data.materials.clear()
    ob.data.materials.append(mat_for(p["stage"]))
    ob["pt_thread_id"] = tid
    ob["pt_part_key"] = p["part_key"]
    ob["pt_stage"] = p["stage"]
    for c in list(ob.users_collection):
        c.objects.unlink(ob)
    coll.objects.link(ob)
    made.append(ob.name)

bpy.ops.object.select_all(action="DESELECT")
for area in bpy.context.screen.areas:
    if area.type == "VIEW_3D":
        area.spaces[0].shading.type = "MATERIAL"
        if coll.objects:
            r3d = area.spaces[0].region_3d
            cen = sum((o.matrix_world.translation for o in coll.objects), Vector()) / len(coll.objects)
            r3d.view_location = cen
            r3d.view_distance = max(20.0, max(max(p["size"]) for p in parts) * 1.6)
        break

print(json.dumps({"created": made, "collection": coll_name, "project": name}, ensure_ascii=False))
'''


def build(ledger: Ledger, thread_id: str) -> dict:
    """台帳の部材を Blender 側に組み立てる。原価は 0（LLM を使わない）。"""
    row = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if row is None:
        return {"status": "error", "reason": f"案件が見つかりません: {thread_id}"}
    parts = parts_of(ledger, thread_id)
    if not parts:
        return {"status": "skipped",
                "reason": "この案件にはモデル部材がありません。先に「基本構造の追加」か「写真から作成」を実行してください。"}

    payload = {"thread_id": thread_id, "name": row["name"], "parts": parts,
               "colors": {str(k): list(v) for k, v in STAGE_RGB.items()}}
    code = _BUILD_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    r = _run(code, timeout=60.0)
    if r["status"] != "ok":
        return r
    return {"status": "ok", "parts": len(parts), "project": row["name"],
            "blender": r["result"], "cost_usd": 0.0,
            "note": "台帳の寸法をそのまま組み立てました。LLM 不使用のため原価は 0 です。"}


def show_upto(thread_id: str, upto_stage: int | None) -> dict:
    """アプリの表示段階スライダーと同じ絞り込みを Blender 側にも掛ける。"""
    upto = 99 if upto_stage is None else int(upto_stage)
    code = (
        'import bpy, json\n'
        f'coll = bpy.data.collections.get("PT_{thread_id[-8:]}")\n'
        'if coll is None:\n'
        '    print(json.dumps({"error": "コレクションがありません"}))\n'
        'else:\n'
        '    shown = []\n'
        '    for ob in coll.objects:\n'
        f'        ob.hide_viewport = ob.get("pt_stage", 1) > {upto}\n'
        '        if not ob.hide_viewport:\n'
        '            shown.append(ob.name)\n'
        '    print(json.dumps({"shown": shown}, ensure_ascii=False))\n'
    )
    return _run(code, timeout=20.0)


def export_glb(ledger: Ledger, thread_id: str) -> dict:
    """Blender 側で組んだものを GLB で書き出し、台帳の assets へ登録する。"""
    out = app_dir() / "assets" / "blender"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{thread_id}_blender.glb"

    code = (
        'import bpy, os, json\n'
        f'coll = bpy.data.collections.get("PT_{thread_id[-8:]}")\n'
        'if coll is None or not coll.objects:\n'
        '    print(json.dumps({"error": "書き出す部材がありません"}))\n'
        'else:\n'
        '    for ob in coll.objects:\n'
        '        ob.hide_viewport = False\n'
        '    bpy.ops.object.select_all(action="DESELECT")\n'
        '    for ob in coll.objects:\n'
        '        ob.select_set(True)\n'
        '    bpy.context.view_layer.objects.active = coll.objects[0]\n'
        f'    p = r"{path}"\n'
        '    bpy.ops.export_scene.gltf(filepath=p, export_format="GLB",\n'
        '                              use_selection=True, export_extras=True, export_apply=True)\n'
        '    bpy.ops.object.select_all(action="DESELECT")\n'
        '    print(json.dumps({"path": p, "bytes": os.path.getsize(p),\n'
        '                      "objects": len(coll.objects)}, ensure_ascii=False))\n'
    )
    r = _run(code, timeout=90.0)
    if r["status"] != "ok":
        return r
    if not path.exists():
        return {"status": "error", "reason": "GLB が書き出されませんでした", "blender": r["result"]}

    from projecttree import modelgen as _mg
    _mg.register_asset(ledger, thread_id, "3d", "glb", path, source="local")
    return {"status": "ok", "path": str(path), "bytes": path.stat().st_size,
            "blender": r["result"],
            "note": "段階情報（pt_stage / pt_part_key）を含めて書き出しました。"}
