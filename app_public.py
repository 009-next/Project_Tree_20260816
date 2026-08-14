"""公開URL（Hugging Face Spaces）用の起動口。

run_app.py はローカル用で、127.0.0.1 に固定して空きポートを探す作りになっている。
公開先のコンテナでは 0.0.0.0 の指定ポートで待ち受ける必要があるため、
起動の仕方だけを差し替えた入口をここに置く。run_app.py は書き換えない。

ここで足すのは 2 つだけ:
  1. 閲覧専用ガード（projecttree/readonly.py）
  2. 0.0.0.0 での待ち受け

アプリ本体（server.py の app）はそのまま読み込んで使う。
"""

import os

import uvicorn

from projecttree.readonly import ReadOnlyMiddleware
from run_app import ensure_db, fix_asset_paths
from server import app

# 台帳の用意とパス補正は、ローカル起動と同じものを使い回す
ensure_db()
fix_asset_paths()

# 閲覧専用にする層を後から被せる（server.py には手を入れない）
app.add_middleware(ReadOnlyMiddleware)


if __name__ == "__main__":
    # Hugging Face Spaces は 7860 番を使う
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
