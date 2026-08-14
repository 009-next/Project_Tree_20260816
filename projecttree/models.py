"""機能別のモデル割当（proposal.md 2-1-1 / 2-1-2 / 2-1-3）。

同じ工程でも要求される知能は違う。安い工程まで Opus を使うのは無駄なので、
「その工程が何を要求するか」で段階（tier）を決め、tier からプロバイダごとの
実モデル名を引く形にする。

tier の考え方:
  light  … 決められた形式へ落とすだけ。判断の余地が小さい（タグ付け・言語化）
  medium … 意味的な同一性判断や分類。文脈は要るが飛躍は不要（名寄せ・段階分類）
  heavy  … 仮説形成。飛躍と根拠付けの両立が要る（原因究明・提案）

既存の llm.py / provider.py は書き換えない。ここは割当表と選択ロジックだけを持つ。
"""

from __future__ import annotations

import os

# --------------------------------------------------------------------------
# 機能 → tier
# --------------------------------------------------------------------------

TASK_TIER: dict[str, str] = {
    # LLM による推論（原因究明・新発見・進捗率向上の提案）
    "infer_proposal": "heavy",
    # LLM による整理（タグ付け・段階分類・部位推論）
    "classify_stage": "medium",
    "infer_part": "medium",
    "resolve_object": "medium",   # 名寄せ
    # 抽出（構造化出力へ落とすだけ）
    "extract_event": "light",
    "verbalize": "light",
    # イメージ図の作画。図としての構図を組む必要があるため light では足りない。
    "illustrate": "medium",
    # 資料出力は台帳を読むだけなので LLM 不使用
    "export_md": "none",
    "export_pdf": "none",
    "export_docx": "none",
    "export_pptx": "none",
    "export_xlsx": "none",
    # モデル生成はローカル計算が既定（LLM を使うのは寸法推定を任せる場合のみ）
    "modelgen": "none",
    "modelgen_llm": "light",
}

TASK_LABEL: dict[str, str] = {
    "infer_proposal": "LLMによる推論（原因究明・新発見・提案）",
    "classify_stage": "LLMによる整理（段階のタグ付け）",
    "infer_part": "LLMによる整理（2D/3D部位と段階の対応）",
    "resolve_object": "名寄せ（表記ゆれの統合）",
    "extract_event": "イベント・Claim の抽出",
    "verbalize": "検出済みパターンの言語化",
    "illustrate": "資料用イメージ図の作画（SVG）",
    "export_md": "md 出力",
    "export_pdf": "pdf 出力",
    "export_docx": "word 出力",
    "export_pptx": "pptx 出力",
    "export_xlsx": "excel 出力",
    "modelgen": "2D/3Dモデル生成（ローカル）",
    "modelgen_llm": "2D/3Dモデル生成（寸法をLLMに推定させる場合）",
}

# --------------------------------------------------------------------------
# tier → 実モデル名
# --------------------------------------------------------------------------

_ANTHROPIC = {
    "light": "claude-haiku-4-5",
    "medium": "claude-sonnet-5",
    "heavy": "claude-opus-5",
}

# Orca Router 経由。実測（2026-08-13）で haiku-4.5 は 401 を返したため、
# light は sonnet へ格上げする（品質は上がりコストだけ増える安全側）。
_ORCA_CLAUDE = {
    "light": "anthropic/claude-sonnet-5",
    "medium": "anthropic/claude-sonnet-5",
    "heavy": "anthropic/claude-opus-5",
}

# Orca Router の非Claude系。安価な工程を更に安くするための選択肢。
# 実際に使えるかは probe_alternatives() で確認してから採用すること。
_ORCA_ALT = {
    "qwen": {
        "light": "qwen/qwen3-next-80b-a3b-instruct",
        "medium": "qwen/qwen3-max",
        "heavy": "qwen/qwen3-max",
    },
    "codex": {
        "light": "openai/gpt-5.2-codex",
        "medium": "openai/gpt-5.2-codex",
        "heavy": "openai/gpt-5.2-codex",
    },
}

# 参考単価（USD / 1M tokens）。原価表示の概算に使う。
RATES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "anthropic/claude-sonnet-5": (2.0, 10.0),
    "anthropic/claude-opus-5": (5.0, 25.0),
}


# 構造化出力（output_config.format.json_schema）が受け付けない検証キーワード。
# これらは API 側で 400 になるため、送る前に落として、値の妥当性は受信後にコードで確かめる。
_UNSUPPORTED_KEYS = (
    "minItems", "maxItems", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
)


def sanitize_schema(schema: dict) -> dict:
    """構造化出力へ渡せる形にスキーマを整える。元のスキーマは変更しない。

    落とした制約は description に残し、モデルへは言葉で伝える。
    """
    import copy

    def walk(node):
        if isinstance(node, dict):
            notes = []
            if "minItems" in node or "maxItems" in node:
                lo, hi = node.get("minItems"), node.get("maxItems")
                if lo == hi and lo is not None:
                    notes.append(f"要素はちょうど{lo}個")
                elif lo is not None or hi is not None:
                    notes.append(f"要素は{lo if lo is not None else 0}〜{hi if hi is not None else '制限なし'}個")
            if "minimum" in node or "maximum" in node:
                notes.append(f"値は{node.get('minimum', '下限なし')}〜{node.get('maximum', '上限なし')}")
            for k in _UNSUPPORTED_KEYS:
                node.pop(k, None)
            if "const" in node:                       # const も未対応。enum 1件で表す。
                node["enum"] = [node.pop("const")]
            if notes:
                node["description"] = (node.get("description", "") + " " + "／".join(notes)).strip()
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    s = copy.deepcopy(schema)
    walk(s)
    return s


def response_text(resp) -> str:
    """Messages API 応答から本文の text を取り出す。

    拡張思考が有効なモデルは content[0] が ThinkingBlock になることがあり、
    content[0].text で決め打ちすると AttributeError になる。type=='text' の
    ブロックを探して返す。
    """
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError("応答にテキストブロックがありません（content: "
                     f"{[getattr(b, 'type', None) for b in resp.content]}）")


def parse_json_response(resp) -> tuple[dict | None, str | None]:
    """構造化出力の応答を安全に読む。(data, None) か (None, 失敗理由) を返す。

    max_tokens 不足で出力が途中で切れると JSON が壊れる。ここで例外を
    吸収し、呼び出し側が 500 ではなく利用者に読める理由を返せるようにする。
    """
    import json as _json
    try:
        text = response_text(resp)
    except ValueError as e:
        return None, str(e)
    try:
        return _json.loads(text), None
    except _json.JSONDecodeError:
        reason = ("出力が途中で切れました（max_tokens 不足の可能性）。"
                  "対象を絞るか、再度実行してください。")
        stop = getattr(resp, "stop_reason", None)
        if stop:
            reason += f" [stop_reason={stop}]"
        return None, reason


def family() -> str:
    """使用するモデル系統。PT_MODEL_FAMILY=claude|qwen|codex（既定 claude）。"""
    v = (os.environ.get("PT_MODEL_FAMILY") or "claude").strip().lower()
    return v if v in ("claude", "qwen", "codex") else "claude"


def model_for_task(task: str, provider: str | None = None, fam: str | None = None) -> str | None:
    """機能名から実モデル名を返す。LLM 不使用の機能は None。"""
    tier = TASK_TIER.get(task, "medium")
    if tier == "none":
        return None
    if provider is None:
        from projecttree import provider as _p
        provider = _p.active_provider()
    fam = fam or family()

    if provider == "orca":
        if fam in _ORCA_ALT:
            return _ORCA_ALT[fam][tier]
        return _ORCA_CLAUDE[tier]
    return _ANTHROPIC[tier]


def estimate(task: str, input_tokens: int, output_tokens: int,
             provider: str | None = None, fam: str | None = None) -> dict:
    """機能単位の概算原価。"""
    model = model_for_task(task, provider, fam)
    if model is None:
        return {"task": task, "model": None, "usd": 0.0, "jpy": 0.0, "note": "LLM 不使用"}
    in_rate, out_rate = RATES.get(model, (2.0, 10.0))
    usd = input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate
    return {
        "task": task, "label": TASK_LABEL.get(task, task), "tier": TASK_TIER.get(task),
        "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
        "usd": round(usd, 4), "jpy": round(usd * 150, 1),
    }


def assignment_table(provider: str | None = None, fam: str | None = None) -> list[dict]:
    """UI へ返す割当表（proposal.md 2-1-3 の対象機能一覧）。"""
    rows = []
    for task in TASK_TIER:
        model = model_for_task(task, provider, fam)
        rows.append({
            "task": task,
            "label": TASK_LABEL.get(task, task),
            "tier": TASK_TIER[task],
            "model": model or "（LLM 不使用）",
            "uses_llm": model is not None,
        })
    return rows


def probe_alternatives(candidates: list[str] | None = None, timeout: float = 25.0) -> list[dict]:
    """Orca Router 上の非Claudeモデルが実際に使えるかを1回ずつ確認する。"""
    from projecttree import provider as _p

    if candidates is None:
        candidates = sorted({m for tiers in _ORCA_ALT.values() for m in tiers.values()})

    key = os.environ.get("ORCA_API_KEY")
    if not key:
        return [{"model": m, "ok": False, "error": "ORCA_API_KEY 未設定"} for m in candidates]

    import anthropic
    client = anthropic.Anthropic(base_url=_p.ORCA_BASE_URL, api_key=key).with_options(timeout=timeout)
    out = []
    for m in candidates:
        try:
            r = client.messages.create(model=m, max_tokens=16,
                                       messages=[{"role": "user", "content": "OK"}])
            out.append({"model": m, "ok": True,
                        "input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens})
        except Exception as e:
            out.append({"model": m, "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"})
    return out
