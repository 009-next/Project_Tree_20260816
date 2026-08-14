"""API送信前の機密情報マスキング（Enhancement.md 4-6 / proposal.md 1-2）。

LLM へ送る前に、機密情報を検出して一時IDへ置換する。
復元表（マッピング）はプロセスのメモリ上だけに置き、LLM へは渡さない。

方針:
  - 形式が確実なもの（メール・電話・口座・マイナンバー）は正規表現で確実に落とす。
  - 人名・顧客名は「様/氏/さん」等の敬称と社名接尾辞を手がかりに一時IDへ置換する。
  - 「社外秘」等の分類ラベルを検出したら、送信可否の判断材料として呼び出し側へ返す。
  - LLM の応答に一時IDが残っていれば元に戻せるようにする（unmask）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 検出パターン
# --------------------------------------------------------------------------

_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # (種別, 一時IDの接頭辞, パターン)
    ("email", "EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # 数字列は英数字の途中で切り出さない。event_id（evt_0487788…）のような
    # 識別子の一部を電話番号と誤認すると、LLM へ渡すIDが壊れて根拠照合ができなくなる。
    ("mynumber", "MYNO", re.compile(
        r"(?<![0-9A-Za-z_])\d{4}[-\s]?\d{4}[-\s]?\d{4}(?![0-9A-Za-z_])")),
    ("phone", "TEL", re.compile(
        r"(?<![0-9A-Za-z_])(?:0\d{1,4}[-(]\d{1,4}[-)]?\d{3,4}"
        r"|0[789]0[-\s]\d{4}[-\s]\d{4}"
        r"|0\d{9,10})(?![0-9A-Za-z_])")),
    ("account", "ACCT", re.compile(
        r"(?:口座番号|普通|当座)\s*[:：]?\s*\d{6,8}(?![0-9A-Za-z_])"
        r"|(?<![0-9A-Za-z_])\d{7}(?![0-9A-Za-z_])(?=\s*(?:番|口座))")),
    # 住所は行内で完結させる（\s に改行を含めると次行まで飲み込む）
    ("address", "ADDR", re.compile(
        r"(?:〒[ \t]*\d{3}[-\s]?\d{4}[ \t]*)?"
        r"(?:北海道|京都府|大阪府|東京都|(?:[一-龥]{2,3}県))"
        r"[一-龥ぁ-んァ-ヶA-Za-z0-9\-－‐ー丁目番地号 \t]{3,40}")),
]

# 人名・組織名（敬称・法人格を手がかりにする）
_PERSON = re.compile(r"([一-龥ぁ-んァ-ヶA-Za-z]{2,10})\s*(様|氏|さん|部長|課長|係長|主任|所長)")
_COMPANY = re.compile(r"([一-龥ぁ-んァ-ヶA-Za-z0-9]{2,20})\s*(株式会社|有限会社|\(株\)|（株）|建設|工務店)")

# 機密分類ラベル
_LABELS = ["社外秘", "極秘", "取扱注意", "機密", "confidential", "個人情報", "輸出管理", "部外秘"]


@dataclass
class MaskResult:
    """マスキング結果。mapping は LLM へ渡さないこと。"""
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # 一時ID -> 原文
    counts: dict[str, int] = field(default_factory=dict)   # 種別 -> 件数
    labels: list[str] = field(default_factory=list)        # 検出した分類ラベル

    @property
    def masked_count(self) -> int:
        return sum(self.counts.values())

    @property
    def has_sensitive_label(self) -> bool:
        return bool(self.labels)

    def summary(self) -> dict:
        """UI へ返す要約。原文（mapping の値）は含めない。"""
        return {
            "masked_count": self.masked_count,
            "counts": self.counts,
            "labels": self.labels,
            "has_sensitive_label": self.has_sensitive_label,
            "chars": len(self.text),
        }


def detect_labels(text: str) -> list[str]:
    low = text.lower()
    return [w for w in _LABELS if (w.lower() in low)]


def mask(text: str, *, mask_names: bool = True) -> MaskResult:
    """機密情報を一時IDへ置換した本文と、復元表を返す。"""
    mapping: dict[str, str] = {}
    counts: dict[str, int] = {}
    seen: dict[str, str] = {}  # 原文 -> 一時ID（同じ値は同じIDにする）
    out = text

    def _replace(pattern: re.Pattern, prefix: str, kind: str, group: int | None = None):
        nonlocal out
        def sub(m: re.Match) -> str:
            raw = m.group(group) if group else m.group(0)
            if raw in seen:
                tid = seen[raw]
            else:
                tid = f"[{prefix}_{len([k for k in mapping if k.startswith('['+prefix)]) + 1:03d}]"
                seen[raw] = tid
                mapping[tid] = raw
                counts[kind] = counts.get(kind, 0) + 1
            if group:
                # 敬称・法人格は残し、名前部分だけ置換する
                return m.group(0).replace(raw, tid)
            return tid
        out = pattern.sub(sub, out)

    for kind, prefix, pat in _PATTERNS:
        _replace(pat, prefix, kind)

    if mask_names:
        _replace(_PERSON, "PERSON", "person", group=1)
        _replace(_COMPANY, "ORG", "org", group=1)

    return MaskResult(text=out, mapping=mapping, counts=counts, labels=detect_labels(text))


def unmask(text: str, mapping: dict[str, str]) -> str:
    """LLM の応答に残った一時IDを元の値へ戻す。"""
    for tid, raw in mapping.items():
        text = text.replace(tid, raw)
    return text


def preflight(text: str, *, allow_sensitive: bool = False) -> dict:
    """送信前チェック。送ってよいか、何文字送るかを利用者へ提示するための情報を返す。"""
    r = mask(text)
    s = r.summary()
    s["allowed"] = allow_sensitive or not r.has_sensitive_label
    s["reason"] = (
        "" if s["allowed"]
        else f"機密ラベル {'/'.join(r.labels)} を検出したため、既定では送信しません"
    )
    return s
