# -*- coding: utf-8 -*-
"""デモ用に台帳を「0件の状態」へ差し替える。元に戻すこともできる。

    python demo/reset_demo.py            0件の台帳に差し替える（現在のものは退避）
    python demo/reset_demo.py --restore  直近に退避した台帳へ戻す
    python demo/reset_demo.py --list     退避されている台帳の一覧

差し替える前の台帳は必ず ledger_backup_日時.db として残すので、
取り違えてもデモ用データは失われない。
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "ledger.db"
EMPTY = ROOT / "demo" / "空の台帳" / "ledger.db"


def backups():
    return sorted(ROOT.glob("ledger_backup_*.db"), reverse=True)


def summary(db: Path) -> str:
    import sqlite3
    try:
        c = sqlite3.connect(db)
        n = c.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        d = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        c.close()
        return f"案件{n}件 / 資料{d}件"
    except Exception as e:
        return f"読めません（{type(e).__name__}）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", action="store_true", help="退避した台帳へ戻す")
    ap.add_argument("--list", action="store_true", help="退避一覧を表示")
    args = ap.parse_args()

    if args.list:
        b = backups()
        if not b:
            print("退避された台帳はありません。")
            return 0
        for p in b:
            print(f"  {p.name}  {summary(p)}")
        return 0

    if args.restore:
        b = backups()
        if not b:
            print("戻せる台帳がありません。")
            return 1
        src = b[0]
        shutil.copy2(src, LIVE)
        print(f"戻しました: {src.name} -> ledger.db（{summary(LIVE)}）")
        return 0

    if not EMPTY.is_file():
        print(f"0件の台帳が見つかりません: {EMPTY}")
        return 1

    if LIVE.is_file():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = ROOT / f"ledger_backup_{stamp}.db"
        shutil.copy2(LIVE, dst)
        print(f"今の台帳を退避しました: {dst.name}（{summary(LIVE)}）")

    shutil.copy2(EMPTY, LIVE)
    print(f"0件の台帳に差し替えました（{summary(LIVE)}）")
    print()
    print("次の手順:")
    print("  1. Project_Tree.exe を起動（案件が0件であることを見せる）")
    print("  2. ⚙設定 でAPIキーを入れる")
    print("  3. 案件パネル →「プロジェクトフォルダを選ぶ」で demo/河川護岸整備工事 を取り込む")
    print("  4. 同じく demo/東部橋梁補修工事 を取り込む")
    print("  5. ▶出力 を押す（見積りダイアログが出る）")
    print()
    print("元に戻すには: python demo/reset_demo.py --restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
