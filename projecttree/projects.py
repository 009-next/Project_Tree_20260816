"""プロジェクト（案件）の作成・改名・統合・削除。

これまで threads は threader.py が文書から自動生成するだけで、人が手で作ることも
消すこともできなかった。結果、スレッド過剰分割（同じ工事が別案件に割れる）を
直す手段が無かった。

ここで足すのは4つ:
  create  新しい案件を空で作る
  rename  名前と別名を直す
  merge   割れてしまった案件を1つにまとめる（過剰分割の実際の直し方）
  delete  案件を消す。中身がある場合は明示的に指示されない限り拒否する

削除は取り返しがつかないので、何がどれだけ消えるかを先に数えて返す（preview）。
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger  # noqa: E402

# thread_id を持つ従属テーブル。削除・統合はここを漏れなく回す。
# （存在しないテーブルは飛ばす。part_events などは機能を使うまで作られない）
DEPENDENT_TABLES = [
    "events", "stage_events", "stages", "model_parts", "assets",
    "gaps", "part_progress", "uploaded_models", "part_events",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _exists(ledger: Ledger, table: str) -> bool:
    return ledger.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _thread(ledger: Ledger, thread_id: str):
    return ledger.conn.execute(
        "SELECT thread_id, name, aliases, first_seen, last_seen, status FROM threads "
        "WHERE thread_id = ?", (thread_id,)).fetchone()


# --------------------------------------------------------------------------
# 一覧
# --------------------------------------------------------------------------

def listing(ledger: Ledger) -> list[dict]:
    """案件一覧。中身の件数を添えて返すので、消して良いか画面で判断できる。"""
    rows = ledger.conn.execute(
        "SELECT t.thread_id, t.name, t.aliases, t.first_seen, t.last_seen, t.status, "
        "       (SELECT COUNT(*) FROM events e WHERE e.thread_id = t.thread_id) AS events, "
        "       (SELECT COUNT(*) FROM stages s WHERE s.thread_id = t.thread_id) AS stages, "
        "       (SELECT COUNT(*) FROM model_parts m WHERE m.thread_id = t.thread_id) AS parts "
        "FROM threads t ORDER BY t.name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["aliases"] = json.loads(d["aliases"]) if d["aliases"] else []
        except (TypeError, ValueError):
            d["aliases"] = []
        out.append(d)
    return out


# --------------------------------------------------------------------------
# 作成 / 改名
# --------------------------------------------------------------------------

def create(ledger: Ledger, name: str, aliases: list[str] | None = None) -> dict:
    """新しい案件を空で作る。同名があれば作らずに既存を返す。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("案件名が空です")
    if len(name) > 120:
        raise ValueError("案件名は120文字以内にしてください")

    dup = ledger.conn.execute("SELECT thread_id FROM threads WHERE name = ?", (name,)).fetchone()
    if dup:
        return {"status": "exists", "thread_id": dup["thread_id"], "name": name}

    tid = "thr_" + uuid.uuid4().hex[:20]
    today = date.today().isoformat()
    # status は threads の CHECK 制約に合わせる（active / dormant / closed）
    ledger.conn.execute(
        "INSERT INTO threads (thread_id, name, aliases, first_seen, last_seen, status) "
        "VALUES (?, ?, ?, ?, ?, 'active')",
        (tid, name, json.dumps(aliases or [], ensure_ascii=False), today, today))
    ledger.commit()
    return {"status": "created", "thread_id": tid, "name": name,
            "note": "空の案件です。資料やメモを取り込むと時系列が育ちます。"}


def rename(ledger: Ledger, thread_id: str, name: str | None = None,
           aliases: list[str] | None = None) -> dict:
    """名前・別名を直す。別名は名寄せ（reconcile）が拾うので表記ゆれ対策に効く。"""
    t = _thread(ledger, thread_id)
    if t is None:
        raise ValueError(f"案件が見つかりません: {thread_id}")

    new_name = (name or "").strip() or t["name"]
    if len(new_name) > 120:
        raise ValueError("案件名は120文字以内にしてください")
    if new_name != t["name"]:
        dup = ledger.conn.execute(
            "SELECT thread_id FROM threads WHERE name = ? AND thread_id <> ?",
            (new_name, thread_id)).fetchone()
        if dup:
            raise ValueError(f"同名の案件が既にあります: {new_name}")

    new_aliases = t["aliases"] if aliases is None else json.dumps(aliases, ensure_ascii=False)
    ledger.conn.execute(
        "UPDATE threads SET name = ?, aliases = ? WHERE thread_id = ?",
        (new_name, new_aliases, thread_id))
    ledger.commit()
    return {"status": "renamed", "thread_id": thread_id, "name": new_name}


# --------------------------------------------------------------------------
# 統合（スレッド過剰分割の直し方）
# --------------------------------------------------------------------------

def merge(ledger: Ledger, into_id: str, from_ids: list[str]) -> dict:
    """from_ids の中身を into_id へ寄せ、from 側の案件を消す。

    段階（stages）は移送先に同じ段階番号の行があると UNIQUE 制約に当たるため、
    寄せた後に作り直す前提で、移送元の stages / stage_events は捨てる。
    元の記録（events）は失わない。
    """
    dst = _thread(ledger, into_id)
    if dst is None:
        raise ValueError(f"統合先が見つかりません: {into_id}")
    from_ids = [i for i in from_ids if i and i != into_id]
    if not from_ids:
        raise ValueError("統合元が指定されていません")

    moved = {"events": 0, "gaps": 0, "model_parts": 0, "assets": 0,
             "uploaded_models": 0, "part_progress": 0, "part_events": 0}
    aliases = set()
    try:
        aliases |= set(json.loads(dst["aliases"]) if dst["aliases"] else [])
    except (TypeError, ValueError):
        pass

    for src_id in from_ids:
        src = _thread(ledger, src_id)
        if src is None:
            continue
        aliases.add(src["name"])
        try:
            aliases |= set(json.loads(src["aliases"]) if src["aliases"] else [])
        except (TypeError, ValueError):
            pass

        # 段階は作り直す前提で捨てる（記録そのものは events に残る）
        for row in ledger.conn.execute(
                "SELECT stage_id FROM stages WHERE thread_id = ?", (src_id,)).fetchall():
            ledger.conn.execute("DELETE FROM stage_events WHERE stage_id = ?", (row["stage_id"],))
        ledger.conn.execute("DELETE FROM stages WHERE thread_id = ?", (src_id,))

        for table in ("events", "gaps", "model_parts", "assets",
                      "uploaded_models", "part_progress", "part_events"):
            if not _exists(ledger, table):
                continue
            if table == "model_parts":
                # part_key は (thread_id, part_key) で一意。衝突する部材は寄せずに捨てる。
                ledger.conn.execute(
                    "DELETE FROM model_parts WHERE thread_id = ? AND part_key IN "
                    "(SELECT part_key FROM model_parts WHERE thread_id = ?)", (src_id, into_id))
            cur = ledger.conn.execute(
                f"UPDATE {table} SET thread_id = ? WHERE thread_id = ?", (into_id, src_id))
            moved[table] = moved.get(table, 0) + cur.rowcount

        ledger.conn.execute("DELETE FROM threads WHERE thread_id = ?", (src_id,))

    aliases.discard(dst["name"])
    span = ledger.conn.execute(
        "SELECT MIN(occurred_on) a, MAX(occurred_on) b FROM events WHERE thread_id = ?",
        (into_id,)).fetchone()
    ledger.conn.execute(
        "UPDATE threads SET aliases = ?, first_seen = COALESCE(?, first_seen), "
        "last_seen = COALESCE(?, last_seen) WHERE thread_id = ?",
        (json.dumps(sorted(aliases), ensure_ascii=False), span["a"], span["b"], into_id))
    ledger.commit()

    return {"status": "merged", "into": into_id, "into_name": dst["name"],
            "merged_count": len(from_ids), "moved": moved,
            "aliases": sorted(aliases),
            "note": "段階は作り直しが必要です（段階分類を再実行してください）。"}


# --------------------------------------------------------------------------
# 削除
# --------------------------------------------------------------------------

def duplicate_groups(ledger: Ledger) -> list[dict]:
    """同名で割れている案件を洗い出す。

    threader は既存の threads を見ずに、資料から読んだ案件名で毎回新しく作る。
    そのため実行のたびに同名の案件が増え、記録は新しい方へ、モデルや段階は
    古い方へ残る、という分裂が起きる。まずどこが割れているかを返す。
    """
    rows = ledger.conn.execute(
        "SELECT t.thread_id, t.name, t.first_seen, "
        "  (SELECT COUNT(*) FROM events e WHERE e.thread_id = t.thread_id) AS events, "
        "  (SELECT COUNT(*) FROM model_parts m WHERE m.thread_id = t.thread_id) AS parts, "
        "  (SELECT COUNT(*) FROM stages s WHERE s.thread_id = t.thread_id) AS stages "
        "FROM threads t ORDER BY t.name").fetchall()

    by_name: dict[str, list[dict]] = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(dict(r))

    out = []
    for name, members in by_name.items():
        if len(members) < 2:
            continue
        out.append({"name": name, "count": len(members),
                    "keep": _pick_keeper(members),
                    "members": members})
    return out


def _pick_keeper(members: list[dict]) -> str:
    """同名グループの中で、どれを残すかを決める。

    利用者の作業（モデル・段階）が載っている案件を最優先で残す。そこを消すと
    3Dモデルや進捗の設定がやり直しになるため。次点は記録が多いもの。
    """
    def score(m: dict):
        worked = 1 if (m["parts"] or m["stages"]) else 0
        return (worked, m["events"], m["parts"] + m["stages"])
    return max(members, key=score)["thread_id"]


def merge_duplicates(ledger: Ledger, *, dry_run: bool = True) -> dict:
    """同名で割れた案件をまとめる。dry_run=True なら何も変更しない。"""
    groups = duplicate_groups(ledger)
    plan = []
    for g in groups:
        others = [m["thread_id"] for m in g["members"] if m["thread_id"] != g["keep"]]
        keeper = next(m for m in g["members"] if m["thread_id"] == g["keep"])
        plan.append({
            "name": g["name"], "keep": g["keep"],
            "keep_detail": {"events": keeper["events"], "parts": keeper["parts"],
                            "stages": keeper["stages"]},
            "merge": others,
            "events_moved": sum(m["events"] for m in g["members"]
                                if m["thread_id"] != g["keep"]),
        })

    if dry_run:
        return {"status": "dry_run", "groups": len(plan), "plan": plan,
                "note": "dry_run のため変更していません。"}

    merged = 0
    for item in plan:
        if not item["merge"]:
            continue
        try:
            merge(ledger, item["keep"], item["merge"])
            merged += len(item["merge"])
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return {"status": "ok", "groups": len(plan), "merged_threads": merged, "plan": plan}


def delete_preview(ledger: Ledger, thread_id: str) -> dict:
    """消す前に、何がどれだけ消えるかを数える。"""
    t = _thread(ledger, thread_id)
    if t is None:
        raise ValueError(f"案件が見つかりません: {thread_id}")

    counts: dict[str, int] = {}
    for table in DEPENDENT_TABLES:
        if table == "stage_events":
            if _exists(ledger, "stage_events"):
                counts["stage_events"] = ledger.conn.execute(
                    "SELECT COUNT(*) c FROM stage_events WHERE stage_id IN "
                    "(SELECT stage_id FROM stages WHERE thread_id = ?)", (thread_id,)
                ).fetchone()["c"]
            continue
        if not _exists(ledger, table):
            continue
        counts[table] = ledger.conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE thread_id = ?", (thread_id,)).fetchone()["c"]

    total = sum(counts.values())
    return {"thread_id": thread_id, "name": t["name"], "counts": counts,
            "total": total, "empty": total == 0,
            "requires_cascade": total > 0,
            "note": ("空の案件です。そのまま削除できます。" if total == 0 else
                     "中身があります。削除するには cascade を指定してください。")}


def delete(ledger: Ledger, thread_id: str, *, cascade: bool = False,
           keep_files: bool = True) -> dict:
    """案件を削除する。中身がある場合は cascade=True が無いと拒否する。

    生成済みのファイル（画像・モデル）は既定で残す。台帳から参照が消えるだけで、
    ディスク上の成果物を勝手に消すことはしない。
    """
    pv = delete_preview(ledger, thread_id)
    if pv["total"] > 0 and not cascade:
        return {"status": "refused", **pv}

    removed_files = []
    if not keep_files and _exists(ledger, "assets"):
        for r in ledger.conn.execute(
                "SELECT path FROM assets WHERE thread_id = ?", (thread_id,)).fetchall():
            p = Path(r["path"])
            try:
                if p.is_file():
                    p.unlink()
                    removed_files.append(str(p))
            except OSError:
                pass

    if _exists(ledger, "stage_events"):
        ledger.conn.execute(
            "DELETE FROM stage_events WHERE stage_id IN "
            "(SELECT stage_id FROM stages WHERE thread_id = ?)", (thread_id,))
    for table in DEPENDENT_TABLES:
        if table == "stage_events" or not _exists(ledger, table):
            continue
        ledger.conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
    ledger.conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
    ledger.commit()

    return {"status": "deleted", "thread_id": thread_id, "name": pv["name"],
            "deleted": pv["counts"], "removed_files": len(removed_files),
            "note": ("生成ファイルはディスクに残しています。" if keep_files
                     else f"生成ファイル {len(removed_files)} 件も削除しました。")}
