"""起動前に置いておいたフォルダを、起動時にそのまま取り込む。

当日のデモで「アプリを開いた時点で資料が入っている」状態を作るための経路。
画面からフォルダを選ぶ操作（案件パネル →プロジェクトフォルダを選ぶ）と
同じことを、起動時に自動でやるだけで、取り込みの中身は変えていない。

置き場所（exe / run_app.py と同じ場所の直下）:

    取込フォルダ/
      ├── 河川護岸整備工事/    ← フォルダ名がそのまま案件名になる
      │     ├── 議事録.md
      │     └── メール.eml
      └── 東部橋梁補修工事/

方針:
  - このフォルダが無ければ何もしない（従来どおりの起動になる）
  - LLM は呼ばない。取り込みだけなので原価は 0 円
  - 同じ内容の資料は内容ハッシュで重複と判定され、二重に入らない。
    そのため起動を繰り返しても増殖しない
  - 取り込み済みの目印を .取込済み として各フォルダに残し、
    次回以降は読み飛ばす（消せばまた取り込む）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402

INBOX_NAMES = ("取込フォルダ", "inbox")
DONE_MARK = ".取込済み"


def find_inbox(base: Path) -> Path | None:
    for name in INBOX_NAMES:
        p = base / name
        if p.is_dir():
            return p
    return None


def _collect(folder: Path) -> list[tuple[str, bytes]]:
    """1案件フォルダの中身を (相対パス, 中身) の並びにする。"""
    out: list[tuple[str, bytes]] = []
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.name == DONE_MARK:
            continue
        try:
            out.append((f"{folder.name}/{f.relative_to(folder).as_posix()}", f.read_bytes()))
        except OSError:
            continue
    return out


def run(ledger: Ledger, base: Path) -> dict:
    """取込フォルダを見て、まだ取り込んでいない案件フォルダを取り込む。"""
    inbox = find_inbox(base)
    if inbox is None:
        return {"status": "skipped", "reason": "取込フォルダがありません", "projects": []}

    from projecttree import foldersync as _fsync

    done, results = [], []
    for folder in sorted(p for p in inbox.iterdir() if p.is_dir()):
        if (folder / DONE_MARK).exists():
            results.append({"name": folder.name, "status": "skipped",
                            "reason": "取込済み（.取込済み を消すと再度取り込みます）"})
            continue
        files = _collect(folder)
        if not files:
            results.append({"name": folder.name, "status": "skipped", "reason": "ファイルがありません"})
            continue
        try:
            r = _fsync.sync(ledger, files, project_name=folder.name, create_project=True)
        except Exception as e:
            results.append({"name": folder.name, "status": "error",
                            "reason": f"{type(e).__name__}: {str(e)[:120]}"})
            continue
        counts = r.get("counts") or {}
        results.append({"name": folder.name, "status": "ok",
                        "thread_id": r.get("thread_id"), "counts": counts})
        done.append(folder.name)
        try:
            (folder / DONE_MARK).write_text(
                "このフォルダは取り込み済みです。消すと次回起動時にまた取り込みます。\n",
                encoding="utf-8")
        except OSError:
            pass

    return {"status": "ok" if done else "nothing",
            "inbox": str(inbox), "projects": results}


def summary_line(res: dict) -> str | None:
    """起動時の表示用に1行へまとめる。取り込むものが無ければ None。"""
    if res.get("status") != "ok":
        return None
    ok = [p for p in res["projects"] if p.get("status") == "ok"]
    if not ok:
        return None
    docs = sum((p.get("counts") or {}).get("doc", 0) for p in ok)
    names = "・".join(p["name"] for p in ok)
    return f"取込フォルダから {len(ok)} 案件・資料 {docs} 件を取り込みました（{names}）"
