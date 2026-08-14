"""Hugging Face Spaces へ上げるフォルダを組み立てる。

    python deploy/huggingface/make_space.py

出来上がり: ../Project_Tree_space/
    そのフォルダを Space の git リポジトリへコピーして push すれば公開される。

公開版は閲覧専用（app_public.py が読み取り専用ガードを被せる）。
exe やビルド成果物、開発用の資料は含めない。
"""

import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT = ROOT.parent / "Project_Tree_space"

# 公開に必要なファイル
FILES = [
    "app_public.py", "run_app.py", "server.py", "ledger.py", "paths.py", "llm.py",
    "extractor.py", "threader.py", "patterns.py", "gaps.py", "insight.py",
    "reconcile.py", "ingest.py", "schema.json", "requirements.txt", "ledger.db",
]
DIRS = ["projecttree", "static", "assets", "uploads"]


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    for name in FILES:
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, OUT / name)

    for name in DIRS:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, OUT / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # Space の設定ファイル
    shutil.copy2(HERE / "Dockerfile", OUT / "Dockerfile")
    shutil.copy2(HERE / "README_space.md", OUT / "README.md")

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    count = sum(1 for f in OUT.rglob("*") if f.is_file())
    print(f"組み立てました: {OUT}")
    print(f"  {count} ファイル / {total / 1024 / 1024:.1f} MB")
    print()
    print("次の手順:")
    print("  1. https://huggingface.co/new-space で Space を作る（SDK は Docker）")
    print("  2. git clone https://huggingface.co/spaces/<user>/<space>")
    print(f"  3. {OUT} の中身を clone 先へコピー")
    print("  4. git add -A && git commit -m 'Project_Tree' && git push")


if __name__ == "__main__":
    main()
