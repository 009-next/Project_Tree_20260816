"""画像をLLMで読み、台帳に載る文章へ落とす（proposal.md 2-1「画像をアップロードして推論」）。

これまで画像は intake.py で「添付」として記録するだけで本文を持たなかった。
本文が無いと抽出も段階分類も食い違い検出も効かないので、画像は台帳の中で死んでいた。

ここでやるのは1つだけ:
    画像 → 見たままの記述（日本語の文章）
その文章を既存の intake_prompt へ渡せば、以降は文書と全く同じ扱いになる。
つまり「画像を読む」処理はここで完結し、下流のパイプラインは何も変えなくてよい。

守る約束は他のLLM経路と同じ:
  - 呼ぶ前に原価を見積もり、確認するまで呼ばない
  - 見えないものを書かせない（推測は observations に入れず、uncertain へ回す）
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
from ledger import Ledger  # noqa: E402
from projecttree import models as _models  # noqa: E402
from projecttree import provider as _prov  # noqa: E402

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp"}

DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "この写真を一行で表す見出し。40字以内。"},
        "summary": {"type": "string", "description": "写っている状況の説明。200字以内。"},
        "observations": {
            "type": "array",
            "description": "写真から直接読み取れる事実。推測は入れない。最大8件。",
            "items": {"type": "string"},
        },
        "uncertain": {
            "type": "array",
            "description": "写真だけでは判断できないこと。最大5件。",
            "items": {"type": "string"},
        },
        "stage_no": {
            "type": "integer",
            "description": "ストーリーテリング5段階のどれか。1状況確認 2現状の課題 3試行錯誤 4課題・変化 5解決策の提案。値は1〜5",
        },
        "safety_concerns": {
            "type": "array",
            "description": "写真から見て取れる安全上の懸念。無ければ空配列。",
            "items": {"type": "string"},
        },
    },
    "required": ["title", "summary", "observations", "uncertain", "stage_no", "safety_concerns"],
    "additionalProperties": False,
}

DESCRIBE_SYSTEM = """建設・土木の現場写真を読み、記録として使える文章にしてください。

厳守事項:
- observations には、写真に実際に写っているものだけを書く。写っていないものを補わない。
- 判断がつかないことは observations に書かず、uncertain へ回す。
- 寸法や数量は、写真から読み取れる場合（標識・目盛り・銘板など）だけ書く。
- 人物が写っている場合、個人が特定できる記述はしない（「作業員2名」のように数だけ書く）。
"""


def media_type_for(filename: str) -> str | None:
    return MEDIA_TYPES.get(Path(filename).suffix.lower())


def estimate(image_bytes: int) -> dict:
    """呼ぶ前の見積り。画像トークンは概ね (幅×高さ)/750。"""
    est = _models.estimate("extract_event", 2600 + len(DESCRIBE_SYSTEM) // 2, 700)
    est["image_bytes"] = image_bytes
    est["note"] = "画像1枚あたりの概算。実測は解析後に返します。"
    return est


def describe(image_bytes: bytes, media_type: str) -> dict:
    """画像を1回だけ読み、構造化した記述を返す。台帳へは書かない。"""
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return {"status": "error",
                "reason": f"{len(image_bytes)/1048576:.1f}MB は上限 {MAX_IMAGE_BYTES//1048576}MB を超えます"}

    model = _models.model_for_task("extract_event")
    try:
        resp = _prov.get_client().messages.create(
            model=model, max_tokens=2200, system=DESCRIBE_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": "この写真を記録として文章にしてください。"},
            ]}],
            output_config={"format": {"type": "json_schema",
                                      "schema": _models.sanitize_schema(DESCRIBE_SCHEMA)}},
        )
    except TypeError as e:
        if "authentication" in str(e).lower():
            return {"status": "error", "reason": "APIキーが設定されていません。⚙設定 から登録してください。"}
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:200]}"}

    data, perr = _models.parse_json_response(resp)
    if perr:
        return {"status": "error", "reason": perr, "model": model}
    usage = llm.usage_dict(resp)
    cost = _prov.cost_usd(usage, model) or llm.cost_usd(usage, model)

    # 段階は 1〜5 に収める（スキーマで縛れないため受信後に確認する）
    try:
        sn = int(data.get("stage_no", 1))
    except (TypeError, ValueError):
        sn = 1
    data["stage_no"] = min(5, max(1, sn))

    return {"status": "ok", "description": data, "model": model,
            "usage": usage, "cost_usd": round(cost, 4)}


def to_document_text(d: dict, filename: str) -> str:
    """記述を、台帳へ入れる文章に組み立てる。

    出典が写真であることを本文に残す。後から見て「これは画像から起こした文章だ」と
    分かるようにしておかないと、原文照合ができない記録が紛れ込む。
    """
    lines = [f"# {d['title']}", "",
             f"（出典: 現場写真 {filename} / LLMが画像から起こした記述）", "",
             d["summary"], ""]
    if d.get("observations"):
        lines += ["## 写真から読み取れたこと"] + [f"- {o}" for o in d["observations"]] + [""]
    if d.get("safety_concerns"):
        lines += ["## 安全上の懸念"] + [f"- {s}" for s in d["safety_concerns"]] + [""]
    if d.get("uncertain"):
        lines += ["## 写真だけでは判断できないこと"] + [f"- {u}" for u in d["uncertain"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


def intake_image(ledger: Ledger, data: bytes, filename: str,
                 thread_id: str | None = None, *, confirm: bool = False) -> dict:
    """画像を読んで台帳へ取り込む。confirm=False なら見積りだけ返し API は呼ばない。"""
    mt = media_type_for(filename)
    if mt is None:
        return {"status": "rejected", "filename": filename,
                "reason": f"未対応の画像形式です（{Path(filename).suffix or '拡張子なし'}）"}

    est = estimate(len(data))
    if not confirm:
        return {"status": "estimate", "filename": filename, "estimate": est,
                "message": "この内容で画像を解析しますか。確認後に実行されます。"}

    r = describe(data, mt)
    if r["status"] != "ok":
        return {**r, "filename": filename}

    from projecttree import intake as _intake
    text = to_document_text(r["description"], filename)
    # intake_prompt は本文の先頭に「入力メモ」という見出しを足すため、
    # 画像から得た見出しがタイトルにならない。intake_file を直接使い、
    # 本文1行目（画像の見出し）をそのまま台帳のタイトルにする。
    doc_name = Path(filename).stem + "_画像記述.md"
    got = _intake.intake_file(ledger, text.encode("utf-8"), doc_name, thread_id)

    return {"status": got.get("status", "added"), "filename": filename,
            "doc_id": got.get("doc_id"), "title": got.get("title"),
            "stage_no": r["description"]["stage_no"],
            "observations": len(r["description"]["observations"]),
            "safety_concerns": r["description"]["safety_concerns"],
            "model": r["model"], "usage": r["usage"],
            "cost_usd": r["cost_usd"], "estimate_usd": est["usd"],
            "text_preview": text[:400]}
