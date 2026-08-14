"""
Phase 5: セキュリティ（Enhancement.md 4章のうち追加依存なしで実装できるもの）

方針:
  4-4 プロンプトインジェクション対策は、既存の台帳側の設計が既に該当している
  （extractor.verify_span の原文照合、schema.json の構造化出力、
    _validate_llm_insight の「違反は破棄・修正させない」）。
  ここでは残りの 4-1 / 4-2 / 4-3 / 4-5 を実装する。
  4-6 SQLCipher は追加依存のため今回は見送り（設計のみ）。
"""

import hashlib
import os
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --------------------------------------------------------------------------
# 4-1 ローカルAPIをセッショントークンで保護
# --------------------------------------------------------------------------

class SessionToken:
    """起動ごとに256bitのランダムトークンを生成する。
    アプリ起動中だけ有効で、ディスクには書かない。"""

    def __init__(self):
        self._token = secrets.token_urlsafe(32)

    @property
    def token(self) -> str:
        return self._token

    def verify(self, presented: str | None) -> bool:
        if not presented:
            return False
        # Bearer 形式も受け付ける
        if presented.startswith("Bearer "):
            presented = presented[7:]
        # タイミング攻撃対策
        return secrets.compare_digest(presented, self._token)


ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def host_allowed(host_header: str | None) -> bool:
    """Host を 127.0.0.1 / localhost に限定する（4-1）。"""
    if not host_header:
        return False
    host = host_header.split(":")[0].strip().lower()
    return host in ALLOWED_HOSTS


# --------------------------------------------------------------------------
# 4-2 APIキーの保護
# --------------------------------------------------------------------------

class ApiKeyStore:
    """APIキーはメモリだけで保持する。
    ディスクにもログにも書かない。画面表示は末尾4文字のみ（4-2）。"""

    def __init__(self):
        self._key: str | None = None

    def set(self, key: str) -> None:
        k = (key or "").strip()
        if not k:
            raise ValueError("APIキーが空です")
        self._key = k
        # 既存の llm.py は module-level で anthropic.Anthropic() を作り
        # 環境変数を読むため、プロセス内の環境変数にも反映する
        os.environ["ANTHROPIC_API_KEY"] = k

    def clear(self) -> None:
        self._key = None
        os.environ.pop("ANTHROPIC_API_KEY", None)

    @property
    def is_set(self) -> bool:
        return bool(self._key)

    def masked(self) -> str:
        """画面表示用。末尾4文字以外は伏せる。"""
        if not self._key:
            return "(未設定)"
        return "*" * 8 + self._key[-4:]

    def __repr__(self) -> str:  # ログ・例外へ漏らさない
        return f"<ApiKeyStore set={self.is_set}>"


# --------------------------------------------------------------------------
# 4-3 PDF制限
# --------------------------------------------------------------------------

PDF_MAGIC = b"%PDF-"
MAX_PDF_BYTES = 30 * 1024 * 1024   # 30MB
MAX_PDF_PAGES = 300


class PdfRejected(Exception):
    pass


def validate_pdf(data: bytes, filename: str = "") -> dict:
    """拡張子ではなくマジックナンバーで判定し、危険な要素を拒否する（4-3）。
    戻り値には内部用のランダムIDを含める（元のファイル名は使わない）。"""
    if len(data) > MAX_PDF_BYTES:
        raise PdfRejected(f"サイズ超過: {len(data):,} bytes > {MAX_PDF_BYTES:,}")
    if not data.startswith(PDF_MAGIC):
        raise PdfRejected("PDFのマジックナンバーが一致しません")

    # 暗号化・JavaScript・埋め込みファイルを拒否
    lowered = data[:2_000_000]
    for marker, reason in ((b"/Encrypt", "暗号化PDF"),
                           (b"/JavaScript", "JavaScript埋め込み"),
                           (b"/JS", "JavaScript埋め込み"),
                           (b"/EmbeddedFile", "ファイル埋め込み"),
                           (b"/Launch", "外部プログラム起動アクション")):
        if marker in lowered:
            raise PdfRejected(f"拒否: {reason}")

    pages = None
    try:
        import fitz  # PyMuPDF（導入済み）
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.is_encrypted:
                raise PdfRejected("拒否: 暗号化PDF")
            pages = doc.page_count
            if pages > MAX_PDF_PAGES:
                raise PdfRejected(f"ページ数超過: {pages} > {MAX_PDF_PAGES}")
    except PdfRejected:
        raise
    except Exception as e:
        raise PdfRejected(f"PDF解析に失敗: {e}")

    return {
        # ファイル名は内部ランダムIDへ変換（パストラバーサル・名前経由の攻撃を断つ）
        "internal_id": "pdf_" + secrets.token_hex(12),
        "pages": pages,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def safe_name(name: str) -> str:
    """パストラバーサルを拒否する。3D側 起動.py と同じ Path(...).name 方式。"""
    base = Path(name).name
    if not base or base in (".", ".."):
        raise ValueError("不正なファイル名")
    return base


# --------------------------------------------------------------------------
# 4-5 原価攻撃・DoS対策
# --------------------------------------------------------------------------

# 既存 llm.cost_usd と同じ単価表（USD / 1M tokens）
_RATES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}

USD_JPY = 150.0  # 表示用の概算レート


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> dict:
    """解析前に推定原価を提示するための計算（4-5）。"""
    in_rate, out_rate = _RATES.get(model, (0.0, 0.0))
    usd = input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate
    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "usd": round(usd, 4), "jpy": round(usd * USD_JPY, 1)}


@dataclass
class BudgetGuard:
    """日次の円予算を超えたらAPIを止める（4-5）。"""
    daily_limit_jpy: float = 500.0
    _spent: dict = field(default_factory=dict)

    def _today(self) -> str:
        return date.today().isoformat()

    def spent_today(self) -> float:
        return self._spent.get(self._today(), 0.0)

    def remaining(self) -> float:
        return max(0.0, self.daily_limit_jpy - self.spent_today())

    def can_spend(self, jpy: float) -> bool:
        return self.spent_today() + jpy <= self.daily_limit_jpy

    def record(self, jpy: float) -> None:
        k = self._today()
        self._spent[k] = self._spent.get(k, 0.0) + jpy


@dataclass
class RateLimiter:
    """1分あたりの解析回数を制限する（4-5）。"""
    max_per_minute: int = 20
    _hits: list = field(default_factory=list)

    def allow(self, now: float | None = None) -> bool:
        import time
        t = now if now is not None else time.time()
        self._hits = [h for h in self._hits if t - h < 60]
        if len(self._hits) >= self.max_per_minute:
            return False
        self._hits.append(t)
        return True


# アプリ全体で共有するインスタンス
SESSION = SessionToken()
API_KEY = ApiKeyStore()
BUDGET = BudgetGuard()
LIMITER = RateLimiter()
