"""
アプリのベースディレクトリ解決。
PyInstaller で固めた exe の場合、bundle 内は読み取り専用なので、
ledger.db 等の書き込み対象は exe と同じディレクトリに置く（永続化・再抽出のため）。
static/ 等の読み取り専用リソースは PyInstaller の展開先（sys._MEIPASS）から読む。
"""

import sys
from pathlib import Path


def app_dir() -> Path:
    """書き込み可能な基点。exe の隣、通常実行時はこのファイルのあるディレクトリ。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def resource_dir() -> Path:
    """読み取り専用リソースの基点。PyInstaller展開時は _MEIPASS、通常実行時は app_dir() と同じ。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent
