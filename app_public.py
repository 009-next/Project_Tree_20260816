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
from pathlib import Path

import uvicorn

from projecttree import slides as _slides
from projecttree.readonly import ReadOnlyMiddleware
from run_app import ensure_db, fix_asset_paths
from server import app

# 日本語フォントの在り処を足す。
# slides._FONT_CANDIDATES は Windows のパスしか持っていない。
# 公開先は Linux コンテナなので、そのままだと候補が1つも見つからず
# ImageFont.load_default() に落ち、段階カードの日本語が豆腐（□）になる。
# 画像は要求時に ensure_images() が作るため、閲覧専用でもこの経路は通る。
# Windows のパスは残したまま後ろに足すので、ローカル・exe の挙動は変わらない。
_LINUX_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
]
for _p in _LINUX_FONTS:
    if _p not in _slides._FONT_CANDIDATES:
        _slides._FONT_CANDIDATES.append(_p)
if not any(Path(p).exists() for p in _slides._FONT_CANDIDATES):
    print("警告: 日本語フォントが見つかりません。生成する画像の文字が豆腐になります。")

# 台帳の用意とパス補正は、ローカル起動と同じものを使い回す
ensure_db()
fix_asset_paths()

# 閲覧専用にする層を後から被せる（server.py には手を入れない）
app.add_middleware(ReadOnlyMiddleware)


if __name__ == "__main__":
    # Hugging Face Spaces は 7860 番を使う
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
