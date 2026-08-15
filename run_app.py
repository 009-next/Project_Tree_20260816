"""
exe のエントリポイント。uvicorn を CLI ではなくコードから直接起動する
（凍結された exe には python/uvicorn の CLI が存在しないため）。
起動後にブラウザを自動で開く。

Project_Tree（Enhancement.md）対応:
  既定の入口を /projecttree にする。
  従来の台帳UI（/）もそのまま残るので、どちらも使える。
"""

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from ledger import Ledger
from paths import app_dir
from server import app as fastapi_app

DEFAULT_PORT = 8010
PORT_SCAN = 20  # 使用中なら +20 まで空きを探す
ENTRY_PATH = "/projecttree"


def find_free_port(start: int = DEFAULT_PORT) -> int:
    """既に別のアプリがポートを使っていても起動できるようにする。
    （clatest2 の別アプリが 8000 を使っている等の実例があるため）"""
    for p in range(start, start + PORT_SCAN + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"空きポートが見つかりません（{start}〜{start + PORT_SCAN}）")


def ensure_db():
    """初回起動時、ledger.db が exe の隣に無ければ空の台帳を作成する。
    既存 DB がある場合も IF NOT EXISTS で新テーブルだけ追加される。"""
    db_path = app_dir() / "ledger.db"
    ledger = Ledger(db_path)
    ledger.init_db()
    ledger.close()


def fix_asset_paths():
    """assets / uploaded_models の path 列を、このアプリのフォルダ配下へ向け直す。

    clone や移動の直後は、path が別環境の絶対パスのまま残る。サーバーは
    app_dir() の外にあるファイルを配信しない（403）ため、
    「実体が見つからない」だけでなく「フォルダの外を指している」場合も補正する。
    ファイル名を手掛かりに app_dir() 配下の実ファイルへ差し替える。
    既に正しく収まっているものは何もしない。"""
    base = app_dir().resolve()
    db_path = base / "ledger.db"
    if not db_path.is_file():
        return
    ledger = Ledger(db_path)
    conn = ledger.conn
    for table, pk, col in (("assets", "asset_id", "path"), ("uploaded_models", "upload_id", "path")):
        try:
            rows = conn.execute(f"SELECT {pk} AS id, {col} AS p FROM {table}").fetchall()
        except Exception:
            continue
        for row in rows:
            p = Path(row["p"])
            try:
                inside = p.is_file() and str(p.resolve()).startswith(str(base))
            except OSError:
                inside = False
            if inside:
                continue
            hit = next(base.rglob(p.name), None)
            if hit is not None:
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE {pk} = ?",
                             (str(hit.resolve()), row["id"]))
    conn.commit()
    ledger.close()


def open_browser_when_ready(port: int):
    time.sleep(1.5)
    webbrowser.open(f"http://127.0.0.1:{port}{ENTRY_PATH}")


def _build_stamp() -> str:
    """このビルドがいつ作られたか。exe と開発実行のどちらでも分かるようにする。"""
    from datetime import datetime
    try:
        if getattr(sys, "frozen", False):
            target = Path(sys.executable)
        else:
            target = Path(__file__)
        return datetime.fromtimestamp(target.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "不明"


def _thread_count() -> str:
    """開いている台帳の案件数。想定と違うデータを見ていないかの手掛かりにする。"""
    try:
        ledger = Ledger(app_dir() / "ledger.db")
        n = ledger.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        ledger.close()
        return f"{n} 件"
    except Exception:
        return "不明"


def auto_ingest():
    """起動前に「取込フォルダ」へ置かれた案件フォルダを取り込む。

    フォルダが無ければ何もしないので、従来どおりの起動になる。
    LLM は呼ばないため原価は 0 円。
    """
    try:
        from projecttree import autoingest as _ai
        ledger = Ledger(app_dir() / "ledger.db")
        try:
            res = _ai.run(ledger, app_dir())
        finally:
            ledger.close()
        return _ai.summary_line(res)
    except Exception as e:
        # 取り込みに失敗してもアプリは起動させる（デモ中に開かないのが最悪）
        return f"取込フォルダの読み込みに失敗しました（{type(e).__name__}）。起動は続けます。"


def main():
    ensure_db()
    fix_asset_paths()
    ingested = auto_ingest()
    port = find_free_port()
    threading.Thread(target=open_browser_when_ready, args=(port,), daemon=True).start()

    print(f"Project_Tree を起動します: http://127.0.0.1:{port}{ENTRY_PATH}")
    print(f"  従来の台帳UI            : http://127.0.0.1:{port}/")
    print(f"  台帳ファイル            : {app_dir() / 'ledger.db'}")
    # どのビルド・どのデータで動いているのかを起動時に必ず示す。
    # ポートが自動で変わったとき、古いタブを見ていることに気づけるようにするため。
    print(f"  ビルド日時              : {_build_stamp()}")
    if ingested:
        print(f"  {ingested}")
    print(f"  案件数                  : {_thread_count()}")
    if port != DEFAULT_PORT:
        print(f"  ※ ポート {DEFAULT_PORT} が使用中のため {port} で起動しました。")
        print(f"     ブラウザの古いタブ（{DEFAULT_PORT} 番）を見ていないか確認してください。")
    print("終了するにはこのウィンドウを閉じてください。")

    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
