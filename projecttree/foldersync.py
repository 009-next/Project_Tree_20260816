"""フォルダでの事前設定（Enhancement.md 3 / Enhancement02.md 1-3）。

プロジェクトフォルダをまるごと渡すと、決められたサブフォルダ名で中身を振り分ける。
LLM の推論・整理を使わずに、資料と 2D/3D モデルを台帳へ載せるための経路。

  2dmodel  .svg .step .blend        → 2Dモデルとして取り込む
  3dmodel  .dxf .stl .step .blend   → 3Dモデルとして取り込む
  pdf docx pptx xlsx txt md         → 資料として台帳へ取り込む

プロジェクト名はフォルダ名を流用する（Enhancement02.md 1-3）。

方針:
  - 振り分けは「どのサブフォルダに入っていたか」を第一の根拠にする。
    フォルダに入れた人の意図が一番確かな情報なので、拡張子より優先する。
  - サブフォルダ名に一致しないものは、拡張子で判断する。それも無理なら捨てる。
  - 取り込みは既存の intake.py / progress.py を呼ぶだけ。重複判定も既存のまま効く。
  - LLM は呼ばない。したがって原価は 0。
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402

# Enhancement.md 3 の表。キーはサブフォルダ名、値はそこに置くべき拡張子。
FOLDER_SPEC: dict[str, set[str]] = {
    "2dmodel": {".svg", ".step", ".stp", ".blend", ".dxf"},
    "3dmodel": {".dxf", ".stl", ".step", ".stp", ".blend", ".glb", ".gltf", ".obj", ".ply", ".ifc"},
    "pdf": {".pdf"},
    "docx": {".docx"},
    "pptx": {".pptx"},
    "xlsx": {".xlsx"},
    "txt": {".txt"},
    "md": {".md"},
}

# 資料として台帳へ入れるサブフォルダ
DOC_FOLDERS = {"pdf", "docx", "pptx", "xlsx", "txt", "md"}
MODEL_FOLDERS = {"2dmodel", "3dmodel"}

# 台帳の抽出が読める拡張子（intake.py の受け入れと揃える）
INTAKE_EXT = {".txt", ".md", ".pdf", ".docx", ".pptx", ".jpg", ".jpeg", ".png"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def spec() -> list[dict]:
    """UI に出す「どのフォルダに何を置くか」の一覧。"""
    return [{"folder": k, "ext": sorted(v),
             "kind": ("2Dモデル" if k == "2dmodel" else
                      "3Dモデル" if k == "3dmodel" else "資料")}
            for k, v in FOLDER_SPEC.items()]


def _parts(rel: str) -> list[str]:
    return [p for p in PurePosixPath(rel.replace("\\", "/")).parts if p not in (".", "")]


def project_name_of(paths: list[str]) -> str | None:
    """相対パス群の共通トップフォルダ名 = プロジェクト名。

    webkitdirectory は "案件A/pdf/報告.pdf" のような相対パスを寄越す。
    先頭要素が全ファイルで一致していれば、それがフォルダ名。
    """
    tops = {(_parts(p)[0] if len(_parts(p)) > 1 else None) for p in paths}
    tops.discard(None)
    if len(tops) == 1:
        return tops.pop()
    return None


def classify(rel_path: str) -> tuple[str | None, str]:
    """1ファイルの行き先を決める。戻り値: (kind, 理由)

    kind は 'doc' | '2d' | '3d' | None
    """
    parts = _parts(rel_path)
    ext = Path(parts[-1]).suffix.lower() if parts else ""
    folders = {p.lower() for p in parts[:-1]}

    # 第一根拠: 決められたサブフォルダに入っているか
    for name in MODEL_FOLDERS | DOC_FOLDERS:
        if name in folders:
            allowed = FOLDER_SPEC[name]
            if ext not in allowed:
                return None, f"{name}/ に置けない拡張子です（{ext or '拡張子なし'}）"
            if name == "2dmodel":
                return "2d", f"{name}/ に置かれていました"
            if name == "3dmodel":
                return "3d", f"{name}/ に置かれていました"
            return "doc", f"{name}/ に置かれていました"

    # 第二根拠: 拡張子で判断する
    if ext in INTAKE_EXT:
        return "doc", "拡張子から資料と判断しました"
    if ext in FOLDER_SPEC["3dmodel"]:
        return "3d", "拡張子から3Dモデルと判断しました"
    if ext in FOLDER_SPEC["2dmodel"]:
        return "2d", "拡張子から2Dモデルと判断しました"
    return None, f"対象外の形式です（{ext or '拡張子なし'}）"


def _doc_id_of(ledger: Ledger, data: bytes, filename: str, _intake) -> str | None:
    """既に台帳にある資料の doc_id を、中身から引き直す。

    intake_file は重複時に doc_id を返さない。しかし所属だけは記録したいので、
    intake と同じ手順（本文抽出 → 正規化 → SHA256）で doc_id を再現する。
    """
    try:
        import hashlib
        from projecttree import security as _sec
        safe = _sec.safe_name(filename)
        text, source_type = _intake.extract_text(data, safe)
        if source_type == "attachment":
            return None          # 画像は取込時刻が入るためハッシュを再現できない
        norm = _intake._normalize(text)
        if not norm.strip():
            return None
        return "doc_" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return None


def sync(ledger: Ledger, files: list[tuple[str, bytes]], *,
         thread_id: str | None = None,
         project_name: str | None = None,
         create_project: bool = True) -> dict:
    """フォルダの中身を振り分けて取り込む。LLM は呼ばないので原価は 0。

    files は (相対パス, 中身) の並び。相対パスは webkitRelativePath をそのまま渡す。
    """
    from projecttree import docthread as _docthread
    from projecttree import intake as _intake
    from projecttree import progress as _prg
    from projecttree import projects as _proj

    paths = [f[0] for f in files]
    folder = project_name_of(paths)
    name = (project_name or "").strip() or folder

    created = None
    if thread_id is None and create_project and name:
        r = _proj.create(ledger, name)
        thread_id = r["thread_id"]
        created = r

    _prg.ensure_tables(ledger)
    results: list[dict] = []
    counts = {"doc": 0, "2d": 0, "3d": 0, "skipped": 0, "rejected": 0}

    for rel, data in files:
        fname = _parts(rel)[-1] if _parts(rel) else rel
        kind, why = classify(rel)

        if kind is None:
            counts["rejected"] += 1
            results.append({"path": rel, "status": "rejected", "reason": why})
            continue

        try:
            if kind == "doc":
                r = _intake.intake_file(ledger, data, fname, thread_id)
                st = r.get("status", "added")
                # intake_file は thread_id を使わないので、どの案件のものかを
                # ここで別に控えておく。あとで threader の推定を上書きする。
                # 同一内容で skipped のときは doc_id が返らないので、
                # 中身のハッシュから引き直す（既存資料でも所属は記録したい）。
                doc_id = r.get("doc_id") or _doc_id_of(ledger, data, fname, _intake)
                if thread_id and doc_id:
                    _docthread.remember(ledger, doc_id, thread_id, source="folder")
                counts["doc" if st == "added" else "skipped"] += 1
                results.append({"path": rel, "status": st, "kind": "資料",
                                "title": r.get("title"), "reason": why})
            else:
                if thread_id is None:
                    counts["rejected"] += 1
                    results.append({"path": rel, "status": "rejected",
                                    "reason": "モデルの取込には案件が必要です"})
                    continue
                r = _prg.import_model(ledger, thread_id, fname, data)
                if r.get("status") == "ok":
                    counts[kind] += 1
                    results.append({"path": rel, "status": "ok",
                                    "kind": "2Dモデル" if kind == "2d" else "3Dモデル",
                                    "fmt": r.get("fmt"), "reason": why})
                else:
                    counts["rejected"] += 1
                    results.append({"path": rel, "status": "rejected",
                                    "reason": r.get("reason")})
        except Exception as e:
            counts["rejected"] += 1
            results.append({"path": rel, "status": "rejected",
                            "reason": f"{type(e).__name__}: {str(e)[:120]}"})

    return {
        "status": "ok",
        "thread_id": thread_id,
        "project_name": name,
        "folder_name": folder,
        "created_project": created,
        "counts": counts,
        "total": len(files),
        "results": results,
        "cost_usd": 0.0,
        "note": "フォルダ同期は LLM を使いません。取り込み後に「出力」で解析できます。",
    }
