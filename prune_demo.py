# -*- coding: utf-8 -*-
"""デモ提出用に、残す案件以外を台帳から削除する。

残すのは、紹介する2案件（護岸・橋梁）と、
プリセット非依存のLLM生成を検証した2案件。

--apply を付けるまでは何も消さない（既定は下見のみ）。
実体ファイル（イメージ図PNG・モデルGLB等）も併せて片付ける。
"""

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "ledger.db"

KEEP_KEYWORDS = ["護岸", "橋梁"]
KEEP_NAMES = ["旧緑ヶ丘小学校校舎解体工事（P20）", "上水道管更新工事"]


def keep_threads(conn):
    keep, drop = [], []
    for r in conn.execute("SELECT thread_id, name FROM threads"):
        nm = r["name"]
        if any(k in nm for k in KEEP_KEYWORDS) or nm in KEEP_NAMES:
            keep.append(r["thread_id"])
        else:
            drop.append(r["thread_id"])
    return keep, drop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に削除する")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    keep, drop = keep_threads(conn)
    if not keep:
        print("残す案件が0件。中止する。")
        return 1
    qk = ",".join("?" * len(keep))

    # 残す案件に紐づく記録・資料は必ず残す
    keep_events = [r[0] for r in conn.execute(
        f"SELECT event_id FROM events WHERE thread_id IN ({qk})", keep)]
    keep_docs = [r[0] for r in conn.execute(
        f"SELECT DISTINCT doc_id FROM events WHERE thread_id IN ({qk})", keep)]
    keep_objs = [r[0] for r in conn.execute(
        f"SELECT DISTINCT object_id FROM claims WHERE doc_id IN "
        f"({','.join('?' * len(keep_docs))}) AND object_id IS NOT NULL", keep_docs
    )] if keep_docs else []

    print(f"残す案件 {len(keep)} / 消す案件 {len(drop)}")
    print(f"残す記録 {len(keep_events)} / 残す資料 {len(keep_docs)} / 残す対象物 {len(keep_objs)}")

    # 消す実体ファイル
    files = []
    for tbl, col in (("assets", "path"), ("uploaded_models", "path")):
        rows = conn.execute(
            f"SELECT {col} AS p FROM {tbl} WHERE thread_id NOT IN ({qk})", keep).fetchall()
        files += [r["p"] for r in rows]
    print(f"消す生成ファイル {len(files)} 個")

    if not args.apply:
        print("\n下見のみ。実行するには --apply を付ける。")
        return 0

    cur = conn.cursor()
    qd = ",".join("?" * len(drop))
    ke = ",".join("?" * len(keep_events)) or "''"
    kd = ",".join("?" * len(keep_docs)) or "''"
    ko = ",".join("?" * len(keep_objs)) or "''"

    # 子から順に消す
    cur.execute(f"DELETE FROM stage_events WHERE stage_id IN "
                f"(SELECT stage_id FROM stages WHERE thread_id IN ({qd}))", drop)
    for t in ("stages", "model_parts", "part_progress", "part_events",
              "illustrations", "assets", "uploaded_models", "gaps", "insights",
              "document_threads", "thread_visibility"):
        cur.execute(f"DELETE FROM {t} WHERE thread_id IN ({qd})", drop)
    cur.execute(f"DELETE FROM events WHERE thread_id IN ({qd})", drop)
    # どの案件にも属さない記録も消す（thread_id が空のもの）
    cur.execute(f"DELETE FROM events WHERE thread_id IS NULL AND event_id NOT IN ({ke})",
                keep_events)
    cur.execute(f"DELETE FROM discrepancies WHERE object_id NOT IN ({ko})", keep_objs)
    cur.execute(f"DELETE FROM claims WHERE doc_id NOT IN ({kd})", keep_docs)
    cur.execute(f"DELETE FROM objects WHERE object_id NOT IN ({ko})", keep_objs)
    cur.execute(f"DELETE FROM image_refs WHERE doc_id NOT IN ({kd})", keep_docs)
    cur.execute(f"DELETE FROM documents WHERE doc_id NOT IN ({kd})", keep_docs)
    cur.execute(f"DELETE FROM threads WHERE thread_id IN ({qd})", drop)
    conn.commit()

    removed = 0
    for p in files:
        f = Path(p)
        if f.is_file():
            try:
                f.unlink(); removed += 1
            except OSError as e:
                print("  消せない:", f.name, e)
    print(f"生成ファイルを {removed} 個削除")

    conn.execute("VACUUM")
    conn.close()
    print("完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
