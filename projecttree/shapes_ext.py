"""形状の追加ライブラリ（プリセット非依存のモデル生成を厚くするため）。

modelgen.py が持つ形状は box / cylinder / slope の3種だけで、
実際に記録から起こしたモデルは 24部位中 21が直方体になっていた。
トンネル・管路・タンク・法面のように、直方体では意味が伝わらない
構造物のために、扱える形を足す。

方針（既存を書き換えないための約束）:
  1. modelgen.py は一切変更しない。box / cylinder / slope の組み立ては
     そのまま modelgen.build_part へ委譲する。
  2. model_parts.shape は CHECK(box/cylinder/slope) で縛られているため、
     DDLを変えずに済むよう「台帳には基本形状を、実際の形は params_json の
     ext_shape に」書き分ける。既存DBもそのまま使える。
  3. Blender連携は台帳の shape を読むので、拡張形状は近い基本形状として
     組まれる（何も出ないより良い）。アプリ本体の表示は GLB を使うため、
     こちらは拡張形状のまま正しく出る。

size は [X, Y, Z] で Y が高さ。pos は部材の中心。modelgen と同じ約束。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from projecttree import modelgen as _mg  # noqa: E402

# 拡張形状 -> 台帳に入れる基本形状（CHECK制約を満たすため）
EXTRA_SHAPES: dict[str, str] = {
    "arch": "box",       # かまぼこ型。トンネル・暗渠・アーチ屋根
    "pipe": "cylinder",  # 横に寝た中空管。管路・配管
    "cone": "cylinder",  # 円錐。ホッパー・土砂の仮置き
    "dome": "cylinder",  # 半球。タンク上部・貯水槽
    "wedge": "slope",    # 片流れ。法面・片勾配の屋根
}

ALL_SHAPES = ["box", "cylinder", "slope"] + list(EXTRA_SHAPES)


def base_shape(shape: str) -> str:
    """台帳の shape 列に入れてよい値へ落とす。"""
    return EXTRA_SHAPES.get(shape, shape)


def _prism(section: list[tuple[float, float]], length: float) -> trimesh.Trimesh:
    """(z, y) の断面を X 方向へ押し出す。三角分割ライブラリに依存しない。

    断面は凸で、反時計回りに与える前提。側面 + 前後の扇形三角で閉じる。
    """
    n = len(section)
    hx = length / 2.0
    verts = [[-hx, y, z] for z, y in section] + [[hx, y, z] for z, y in section]
    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces.append([i, j, j + n])
        faces.append([i, j + n, i + n])
    for i in range(1, n - 1):          # 前面
        faces.append([0, i + 1, i])
    for i in range(1, n - 1):          # 背面
        faces.append([n, n + i, n + i + 1])
    m = trimesh.Trimesh(vertices=np.array(verts, dtype=float),
                        faces=np.array(faces, dtype=np.int64))
    m.fix_normals()
    return m


def _arch(sx: float, sy: float, sz: float, segments: int = 16) -> trimesh.Trimesh:
    """かまぼこ型。幅 sz・高さ sy の半円（下は平ら）を長さ sx へ押し出す。"""
    r = sz / 2.0
    sec = [(-r, 0.0), (r, 0.0)]
    for i in range(1, segments):
        t = np.pi * i / segments
        sec.append((r * np.cos(t), sy * np.sin(t)))
    return _prism(sec, sx)


def _wedge(sx: float, sy: float, sz: float) -> trimesh.Trimesh:
    """片流れ。Z の片側が高さ sy、反対側が 0 まで落ちる。"""
    sec = [(-sz / 2, 0.0), (sz / 2, 0.0), (sz / 2, sy)]
    return _prism(sec, sx)


def _pipe(sx: float, sy: float, sz: float) -> trimesh.Trimesh:
    """横に寝た中空管。外径は min(sy, sz)、長さは sx。"""
    outer = max(min(sy, sz), 1e-3) / 2.0
    inner = outer * 0.72
    m = trimesh.creation.annulus(r_min=inner, r_max=outer, height=sx)
    # annulus は Z 方向に伸びるので、X 方向へ倒す
    m.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
    return m


def _cone(sx: float, sy: float, sz: float) -> trimesh.Trimesh:
    """円錐。底面の直径は min(sx, sz)、高さは sy。"""
    r = max(min(sx, sz), 1e-3) / 2.0
    m = trimesh.creation.cone(radius=r, height=sy, sections=24)
    # cone は底面が原点。中心が原点に来るよう下げる
    m.apply_translation([0, 0, -sy / 2.0])
    m.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    return m


def _dome(sx: float, sy: float, sz: float, segments: int = 16) -> trimesh.Trimesh:
    """半球。底面の直径は min(sx, sz)、高さは sy。"""
    r = max(min(sx, sz), 1e-3) / 2.0
    profile = [[0.0, 0.0]]
    for i in range(segments + 1):
        t = np.pi / 2 * i / segments
        profile.append([r * np.cos(t), sy * np.sin(t)])
    m = trimesh.creation.revolve(np.array(profile, dtype=float), sections=24)
    m.apply_translation([0, 0, -sy / 2.0])
    m.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    return m


_BUILDERS = {"arch": _arch, "pipe": _pipe, "cone": _cone, "dome": _dome, "wedge": _wedge}


def build_part(p: dict) -> trimesh.Trimesh:
    """1部材 -> trimesh。拡張形状はここで、基本形状は modelgen へ委譲する。"""
    shape = p.get("ext_shape") or p.get("shape")
    if shape not in _BUILDERS:
        return _mg.build_part(p)          # 既存の実装をそのまま使う
    sx, sy, sz = (float(v) for v in p["size"])
    mesh = _BUILDERS[shape](sx, sy, sz)
    mesh.apply_translation([float(v) for v in p["pos"]])
    return mesh


def build_scene(params: dict, upto_stage: int | None = None):
    """modelgen.build_scene と同じ約束で、拡張形状も組めるようにしたもの。"""
    scene = trimesh.Scene()
    used = []
    for p in params["parts"]:
        if upto_stage is not None and p["stage"] > upto_stage:
            continue
        mesh = build_part(p)
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


def extended_schema(base_schema: dict) -> dict:
    """形状の enum だけを拡張した写しを返す。元のスキーマは書き換えない。"""
    import copy
    s = copy.deepcopy(base_schema)
    try:
        s["properties"]["parts"]["items"]["properties"]["shape"]["enum"] = list(ALL_SHAPES)
    except (KeyError, TypeError):
        pass
    return s


def to_ledger_parts(parts: list[dict]) -> list[dict]:
    """台帳へ入れる形へ直す。

    shape は CHECK を満たす基本形状にし、実際の形は ext_shape として残す。
    persist_parts は既知のキー以外を params_json へ入れるので、
    ext_shape はそのまま params_json に載る。
    """
    out = []
    for p in parts:
        shape = p.get("shape")
        q = dict(p)
        if shape in EXTRA_SHAPES:
            q["shape"] = EXTRA_SHAPES[shape]
            q["ext_shape"] = shape
        out.append(q)
    return out
