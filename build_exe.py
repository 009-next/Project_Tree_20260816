"""Project_Tree の exe を作る。

    python build_exe.py

出来上がり: dist/Project_Tree/Project_Tree.exe

PyInstaller は static/ などの読み取り専用リソースを exe の中へ入れるが、
ledger.db・assets/・uploads/ は「書き込む側」のデータなので exe の中には入れられない
（paths.app_dir() が exe と同じフォルダを指すため）。
そのため、ビルド後にこのスクリプトが exe の隣へコピーする。
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "Project_Tree"

# exe の隣に置く必要があるもの（書き込み対象・生成物）
DATA_FILES = ["ledger.db"]
# demo/ は当日デモの素材（取り込むフォルダ・0件の台帳・手順書）。
# exe だけを配っても 0 件から再現できるよう、隣へ置く。
DATA_DIRS = ["assets", "uploads", "demo"]


def main():
    print("PyInstaller を実行します（数分かかります）…")
    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "Project_Tree.spec"],
        cwd=ROOT,
    )
    if r.returncode != 0:
        print("ビルドに失敗しました。")
        return 1

    if not OUT.is_dir():
        print(f"出力フォルダが見つかりません: {OUT}")
        return 1

    print("台帳・生成物を exe の隣へコピーします…")
    for name in DATA_FILES:
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, OUT / name)
            print(f"  {name}")

    for name in DATA_DIRS:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, OUT / name, dirs_exist_ok=True)
            print(f"  {name}/")

    print()
    print("完成しました。次を実行すると起動します:")
    print(f"  {OUT / 'Project_Tree.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
