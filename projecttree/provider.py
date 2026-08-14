"""LLM プロバイダの切り替え（Anthropic 直 / Orca Router）。

Anthropic 直の利用上限に当たっている間も、Anthropic SDK 互換のプロキシである
Orca Router 経由で同じパイプラインを動かせるようにする。

既存の llm.py は書き換えない。パイプライン側は
    from projecttree.provider import get_client, model_for
のように使い、llm.py の定数はモデル種別の識別子として引き続き利用する。

選択の優先順位（環境変数で明示されたものが最優先）:
    1. PT_LLM_PROVIDER=orca|anthropic
    2. ORCA_API_KEY があれば orca
    3. ANTHROPIC_API_KEY があれば anthropic
    4. どちらも無ければ anthropic（呼び出し時に SDK 側で認証エラー）
"""

from __future__ import annotations

import os

import anthropic

import llm as _llm

ORCA_BASE_URL = "https://api.orcarouter.ai"

# llm.py のモデル定数 → Orca Router 側のモデル名。
# Orca は "anthropic/" 接頭辞とドット区切りを使う（例: anthropic/claude-sonnet-5）。
# 実測（2026-08-13）: sonnet-5 / opus-5 は応答するが haiku-4.5 は 401 を返すため、
# haiku 相当の用途は sonnet-5 へ寄せる。品質は上がり、コストだけ増える方向なので安全側。
_ORCA_MODEL_MAP = {
    "claude-haiku-4-5": "anthropic/claude-sonnet-5",
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-opus-5": "anthropic/claude-opus-5",
}

# Orca 経由で実際に使うモデルの単価（USD / 1M tokens）。
# 課金は Orca 側の従量になるため、原価表示は Anthropic 定価を基準にした概算とする。
_ORCA_RATES = {
    "anthropic/claude-sonnet-5": (2.0, 10.0),
    "anthropic/claude-opus-5": (5.0, 25.0),
}


def _orca_key() -> str | None:
    return os.environ.get("ORCA_API_KEY") or None


def active_provider() -> str:
    """現在有効なプロバイダ名を返す（"orca" / "anthropic"）。"""
    explicit = (os.environ.get("PT_LLM_PROVIDER") or "").strip().lower()
    if explicit in ("orca", "anthropic"):
        return explicit
    if _orca_key():
        return "orca"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "anthropic"


def get_client() -> anthropic.Anthropic:
    """有効なプロバイダに向いた Anthropic SDK クライアントを返す。

    Orca Router は Anthropic SDK 互換なので、base_url と api_key を差し替えるだけでよい。
    呼び出し側のコード（messages.create / stream / output_config）は変えなくて済む。
    """
    if active_provider() == "orca":
        key = _orca_key()
        if not key:
            raise RuntimeError("ORCA_API_KEY が設定されていません")
        return anthropic.Anthropic(base_url=ORCA_BASE_URL, api_key=key)
    return anthropic.Anthropic()


def model_for(model_const: str) -> str:
    """llm.py のモデル定数を、有効なプロバイダで通る実モデル名へ変換する。"""
    if active_provider() == "orca":
        return _ORCA_MODEL_MAP.get(model_const, f"anthropic/{model_const}")
    return model_const


def cost_usd(usage: dict, model: str) -> float:
    """プロバイダに応じた概算原価。Anthropic 直なら既存の llm.cost_usd をそのまま使う。"""
    if model in _ORCA_RATES:
        in_rate, out_rate = _ORCA_RATES[model]
        return (
            usage.get("input_tokens", 0) / 1_000_000 * in_rate
            + usage.get("output_tokens", 0) / 1_000_000 * out_rate
            + usage.get("cache_read_tokens", 0) / 1_000_000 * in_rate * 0.1
            + usage.get("cache_write_tokens", 0) / 1_000_000 * in_rate * 1.25
        )
    return _llm.cost_usd(usage, model)


def status() -> dict:
    """UI へ返す状態。キー本体は返さない（末尾4文字のみ）。"""
    key = _orca_key()
    return {
        "provider": active_provider(),
        "orca_key_set": bool(key),
        "orca_key_masked": ("…" + key[-4:]) if key else "(未設定)",
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "orca_base_url": ORCA_BASE_URL,
        "models": {
            "extract": model_for(_llm.MODEL_EXTRACT),
            "thread": model_for(_llm.MODEL_THREAD),
            "rule_text": model_for(_llm.MODEL_RULE_TEXT),
            "explore": model_for(_llm.MODEL_EXPLORE),
        },
    }


def probe(model_const: str = "claude-sonnet-5", timeout: float = 30.0) -> dict:
    """実際に1回だけ呼び出して疎通を確認する（最小トークン）。"""
    model = model_for(model_const)
    try:
        client = get_client().with_options(timeout=timeout)
        r = client.messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": "OK"}],
        )
        return {
            "ok": True, "provider": active_provider(), "model": model,
            "input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens,
        }
    except Exception as e:  # 疎通確認なので例外の型を問わず状態として返す
        return {"ok": False, "provider": active_provider(), "model": model,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
