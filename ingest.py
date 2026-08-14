"""
L1 取り込み。corpus/ を走査し、正規化・doc_id 確定・documents への登録を行う。

正規化ルール（claude.md で確定必須と規定）:
  改行コードを \\n に統一 → 各行末尾の空白を削除 → 先頭・末尾の空行を除去
  これより後にルールを変えると全 doc_id が変わり台帳が全滅する。
"""

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from ledger import Ledger

CORPUS_DIR = Path(__file__).parent / "corpus"
EXCLUDE = {"PLANTED.md", "GENERATION_LOG.md"}


def normalize_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def make_doc_id(content_sha256: str) -> str:
    return "doc_" + content_sha256[:12]


def infer_source_type(filename: str) -> str:
    if filename.endswith(".eml"):
        return "email"
    if "議事録" in filename:
        return "minutes"
    if "報告" in filename:
        return "report"
    return "attachment"


def extract_date_from_filename(filename: str) -> str:
    m = re.match(r"^(\d{8})_", filename)
    if not m:
        raise ValueError(f"ファイル名から日付を抽出できません: {filename}")
    d = m.group(1)
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def derive_title_from_filename(filename: str) -> str:
    name = re.sub(r"^\d{8}_[^_]+_", "", filename)
    name = re.sub(r"\.(md|eml)$", "", name)
    return name.replace("_", " ")


def parse_md_title(text: str, filename: str) -> str:
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return derive_title_from_filename(filename)


def parse_eml(text: str) -> dict:
    headers = {}
    body_lines = []
    in_body = False
    for line in text.split("\n"):
        if in_body:
            body_lines.append(line)
            continue
        if not line.strip():
            in_body = True
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    body = "\n".join(body_lines).strip()
    return {
        "from": headers.get("from"),
        "to": headers.get("to"),
        "subject": headers.get("subject"),
        "body": body,
    }


def build_document(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    text = normalize_text(raw)
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    doc_id = make_doc_id(content_sha256)
    filename = path.name
    occurred_at = extract_date_from_filename(filename)
    source_type = infer_source_type(filename)

    if source_type == "email":
        parsed = parse_eml(text)
        title = parsed["subject"] or derive_title_from_filename(filename)
        author = parsed["from"]
        participants = [p.strip() for p in (parsed["to"] or "").split(",") if p.strip()]
    else:
        title = parse_md_title(text, filename)
        author = None
        participants = []

    return {
        "doc_id": doc_id,
        "content_sha256": content_sha256,
        "source_path": f"corpus/{filename}",
        "source_type": source_type,
        "title": title,
        "occurred_at": occurred_at,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "author": author,
        "participants": participants,
        "text": text,
        "line_count": len(text.split("\n")),
    }


def scan_corpus(corpus_dir: Path) -> list[Path]:
    files = []
    for ext in ("*.md", "*.eml"):
        files.extend(corpus_dir.glob(ext))
    return sorted(p for p in files if p.name not in EXCLUDE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default=str(CORPUS_DIR))
    args = parser.parse_args()

    corpus_dir = Path(args.corpus)
    ledger = Ledger()
    ledger.init_db()

    new_count = 0
    skip_count = 0

    for path in scan_corpus(corpus_dir):
        doc = build_document(path)
        if ledger.document_exists(doc["content_sha256"]):
            skip_count += 1
            continue
        ledger.insert_document(doc)
        new_count += 1

    ledger.close()
    print(f"新規: {new_count} 件 / 既存スキップ: {skip_count} 件", file=sys.stderr)


if __name__ == "__main__":
    main()
