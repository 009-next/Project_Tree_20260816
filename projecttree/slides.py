"""資料用画像の生成（proposal.md 2-3）。

段階ごとに1枚の PNG を作る。この画像は3つの用途で使い回す。
  1. 画面上半分の年表カード
  2. pptx のスライド本体
  3. pdf / word の図版

LLM は呼ばない。台帳（stages / stage_events / events / gaps）を読んで描くだけなので
何度作り直しても原価は 0 で、結果も毎回同じになる。
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from ledger import Ledger  # noqa: E402
from paths import app_dir  # noqa: E402

W, H = 960, 540          # 16:9。pptx のスライドと同じ比率にしておく
MARGIN = 44

# 段階ごとの色。UI・pptx・pdf で同じ色を使い、どの資料でも段階が同じ色に見えるようにする。
STAGE_COLOR = {
    1: (0x3E, 0x6B, 0x9E),   # 状況確認 … 青
    2: (0x8A, 0x6D, 0x3B),   # 現状の課題 … 黄土
    3: (0x46, 0x7E, 0x5A),   # 試行錯誤 … 緑
    4: (0x8A, 0x5A, 0x3B),   # 課題・変化 … 橙
    5: (0xB0, 0x2A, 0x2A),   # 解決策・発展策 … 赤（exporters._STAGE_RGB と同色）
}
BG = (0x14, 0x18, 0x1F)
FG = (0xE6, 0xEC, 0xF4)
SUB = (0x9F, 0xB0, 0xC6)
LINE = (0x2A, 0x33, 0x42)

STAGE_TITLES = {
    1: "状況確認", 2: "現状の課題", 3: "試行錯誤",
    4: "課題・変化", 5: "解決策、発展策の提案",
}

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\YuGothR.ttc",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default(size)


def assets_dir() -> Path:
    d = app_dir() / "assets" / "slides"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# 描画部品
# --------------------------------------------------------------------------

def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """日本語は単語境界が無いので1文字ずつ詰めて折り返す。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        t = cur + ch
        if draw.textlength(t, font=font) > max_w and cur:
            lines.append(cur); cur = ch
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def _flow_bar(draw: ImageDraw.ImageDraw, active: int) -> None:
    """上部に5段階のフローを描き、今どこかを示す（ストーリーテリングの流れを常に見せる）。"""
    x, y, h = MARGIN, MARGIN, 30
    total_w = W - MARGIN * 2
    seg = total_w // 5
    f = _font(15)
    for n in range(1, 6):
        x0 = x + (n - 1) * seg
        on = (n == active)
        col = STAGE_COLOR[n] if on else LINE
        draw.rounded_rectangle([x0 + 3, y, x0 + seg - 3, y + h], radius=6,
                               fill=col if on else None, outline=col, width=2)
        label = f"{n}. {STAGE_TITLES[n]}"
        tw = draw.textlength(label, font=f)
        draw.text((x0 + (seg - tw) / 2, y + (h - 17) / 2), label,
                  font=f, fill=FG if on else SUB)
        if n < 5:
            cx = x0 + seg - 3
            draw.polygon([(cx, y + h / 2 - 4), (cx + 6, y + h / 2), (cx, y + h / 2 + 4)], fill=LINE)


def render_stage(ledger: Ledger, thread_id: str, stage_no: int) -> Image.Image:
    """1段階分のスライド画像を作る。"""
    th = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    thread_name = th["name"] if th else thread_id

    st = ledger.conn.execute(
        "SELECT stage_id, title, summary FROM stages WHERE thread_id = ? AND stage_no = ?",
        (thread_id, stage_no)).fetchone()

    events = []
    if st:
        events = ledger.conn.execute(
            "SELECT e.occurred_on, e.kind, e.summary FROM stage_events se "
            "JOIN events e ON e.event_id = se.event_id "
            "WHERE se.stage_id = ? ORDER BY e.occurred_on",
            (st["stage_id"],)).fetchall()

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    col = STAGE_COLOR[stage_no]

    _flow_bar(d, stage_no)

    # 案件名と段階見出し
    y = MARGIN + 52
    d.text((MARGIN, y), thread_name, font=_font(17), fill=SUB)
    y += 28
    d.rectangle([MARGIN, y + 6, MARGIN + 6, y + 42], fill=col)
    d.text((MARGIN + 18, y), f"{stage_no}. {STAGE_TITLES[stage_no]}", font=_font(34), fill=FG)
    y += 58
    d.line([MARGIN, y, W - MARGIN, y], fill=LINE, width=1)
    y += 18

    # 段階サマリ（SQL が作った文。LLM ではない）
    if st and st["summary"]:
        f = _font(17)
        for ln in _wrap(d, st["summary"], f, W - MARGIN * 2)[:3]:
            d.text((MARGIN, y), ln, font=f, fill=FG); y += 25
        y += 10

    # 時系列（最大7件。入り切らない分は件数で示す）
    f_date, f_body = _font(15), _font(16)
    shown = 0
    for e in events:
        if y > H - MARGIN - 34:
            break
        d.ellipse([MARGIN + 2, y + 7, MARGIN + 10, y + 15], fill=col)
        d.text((MARGIN + 22, y), e["occurred_on"] or "日付不明", font=f_date, fill=SUB)
        body = _wrap(d, e["summary"], f_body, W - MARGIN * 2 - 130)[:1]
        if body:
            d.text((MARGIN + 128, y - 1), body[0], font=f_body, fill=FG)
        y += 26
        shown += 1
    if len(events) > shown:
        d.text((MARGIN + 22, y + 2), f"ほか {len(events) - shown} 件", font=f_date, fill=SUB)

    # 右下に出典の件数。「この画像は何件の記録から作られたか」を必ず見せる。
    foot = f"記録 {len(events)} 件から生成 / 出典は台帳で参照可"
    fw = d.textlength(foot, font=f_date)
    d.text((W - MARGIN - fw, H - MARGIN + 6), foot, font=f_date, fill=SUB)

    return img


# --------------------------------------------------------------------------
# 生成・登録
# --------------------------------------------------------------------------

class ThreadNotFound(Exception):
    """指定された案件が台帳に無い。"""


def _require_thread(ledger: Ledger, thread_id: str) -> None:
    """案件の実在を確かめる。

    SQLite は既定で外部キーを強制しないため、存在しない thread_id でも
    assets へ行が入り、ディスクにも画像が残ってしまう。書き込む前に止める。
    """
    if ledger.conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone() is None:
        raise ThreadNotFound(f"案件が見つかりません: {thread_id}")


def generate(ledger: Ledger, thread_id: str, *, stages: list[int] | None = None) -> list[dict]:
    """段階ごとの PNG を生成し、assets へ登録する。既存の同一段階の行は差し替える。"""
    _require_thread(ledger, thread_id)
    out = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    d = assets_dir()
    for n in (stages or [1, 2, 3, 4, 5]):
        img = render_stage(ledger, thread_id, n)
        path = d / f"{thread_id}_stage{n}.png"
        img.save(path, "PNG")

        ledger.conn.execute(
            "DELETE FROM assets WHERE thread_id = ? AND stage_no = ? AND kind = 'image' AND fmt = 'png'",
            (thread_id, n))
        ledger.conn.execute(
            "INSERT INTO assets (asset_id, thread_id, stage_no, kind, fmt, path, generated_at, source) "
            "VALUES (?, ?, ?, 'image', 'png', ?, ?, 'local')",
            ("ast_" + uuid.uuid4().hex[:20], thread_id, n, str(path), now))
        out.append({"stage_no": n, "path": str(path), "bytes": path.stat().st_size})
    ledger.commit()
    return out


def image_paths(ledger: Ledger, thread_id: str) -> dict[int, Path]:
    """段階番号 → 画像パス。無い段階は入らない。"""
    rows = ledger.conn.execute(
        "SELECT stage_no, path FROM assets WHERE thread_id = ? AND kind = 'image' AND fmt = 'png' "
        "ORDER BY stage_no", (thread_id,)).fetchall()
    return {r["stage_no"]: Path(r["path"]) for r in rows if Path(r["path"]).exists()}


def ensure_images(ledger: Ledger, thread_id: str) -> dict[int, Path]:
    """無ければ作ってから返す。pdf/word/pptx の出力側はこれを呼べばよい。"""
    got = image_paths(ledger, thread_id)
    missing = [n for n in range(1, 6) if n not in got]
    if missing:
        generate(ledger, thread_id, stages=missing)
        got = image_paths(ledger, thread_id)
    return got


def main():
    import argparse
    p = argparse.ArgumentParser(description="段階ごとの資料用画像を生成")
    p.add_argument("--thread", required=True)
    args = p.parse_args()
    lg = Ledger()
    for r in generate(lg, args.thread):
        print(f"stage{r['stage_no']}: {r['path']} ({r['bytes']:,} B)")
    lg.close()


if __name__ == "__main__":
    main()
