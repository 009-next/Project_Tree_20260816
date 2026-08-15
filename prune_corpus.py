# -*- coding: utf-8 -*-
"""台帳に残っている資料に対応する corpus ファイルだけを残す。

--apply を付けるまでは下見のみ。
corpus は提出物ではない（GitHub にも公開URLにも入らない）元データ置き場。
"""

import argparse
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent
CORPUS = pathlib.Path(r"C:\Users\ryoh0\AI\業務用\202604\AGENTS\202608LLM\clatest\corpus")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(ROOT / "ledger.db")
    conn.row_factory = sqlite3.Row
    keep = set()
    for r in conn.execute("SELECT source_path FROM documents"):
        sp = (r["source_path"] or "").replace(chr(92), "/")
        keep.add(pathlib.Path(sp).name)
    conn.close()

    if not CORPUS.is_dir():
        print("corpus フォルダが無い:", CORPUS)
        return 1

    allf = [p for p in CORPUS.rglob("*") if p.is_file()]
    kept = [p for p in allf if p.name in keep]
    drop = [p for p in allf if p.name not in keep]

    print(f"corpus 全 {len(allf)} ファイル")
    print(f"  残す（台帳の資料 {len(keep)} 件に対応）: {len(kept)}")
    print(f"  消す: {len(drop)}")
    print("\n残すファイル例:")
    for p in kept[:6]:
        print("   ", p.name)
    print("\n消すファイル例:")
    for p in drop[:5]:
        print("   ", p.name)

    if not args.apply:
        print("\n下見のみ。実行するには --apply を付ける。")
        return 0

    n = 0
    for p in drop:
        try:
            p.unlink()
            n += 1
        except OSError as e:
            print("  消せない:", p.name, e)
    print(f"\n{n} ファイルを削除")
    return 0


if __name__ == "__main__":
    sys.exit(main())
