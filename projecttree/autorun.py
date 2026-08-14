"""「出力」ボタンの中身（Enhancement02.md 1-2）。

1次情報を入れたあと、利用者が押すボタンは1つで済むべきだ、という要求。
押すと以下が順に走る。

  1. プロジェクトの自動作成      … 案件が無ければ作る
  2. 時系列・文章の作成          … extractor（資料 → events / claims）
  3. 案件への割り当て            … threader（events に thread_id を付ける）
  4. 段階の推論                  … stages（ルール）＋ 決まらない分だけ LLM
  5. 2D/3Dモデルの自動作成       … modelgen（プリセット。LLM 不使用）
  6. 進捗率                      … progress（台帳の記録から機械的に算出）
  7. モデルへの情報付加          … infer_parts（部位 ↔ 記録の対応）
  8. 資料用イメージ図            … illustrate（段階ごと）
  9. 推論結果                    … inference（原因究明・新発見・提案）

設計の要点:
  - 走らせる前に全工程の原価を合計して見せる。押した瞬間に課金が始まる作りにしない。
  - 各工程は「やることが無ければ skipped」を返す。既にあるものを作り直して
    二重課金しない。
  - 途中で失敗しても、そこまでの成果は台帳に残る。工程ごとに結果を返す。
  - 既存の各モジュールを呼ぶだけで、処理そのものはここに書かない。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
from ledger import Ledger  # noqa: E402
from paths import app_dir  # noqa: E402

# 実行順。名前は UI にそのまま出す。
STEPS = [
    ("project", "プロジェクトの確認・作成"),
    ("extract", "時系列・文章の作成"),
    # extractor は events を作るが thread_id は付けない。案件への割り当ては
    # threader の仕事。これを飛ばすと以降の工程が全て空振りする。
    ("thread", "案件への割り当て"),
    ("stages", "段階の分類"),
    ("model", "2D/3Dモデルの作成"),
    ("progress", "進捗率の算出"),
    ("parts", "モデル部位と記録の対応付け"),
    ("illust", "資料用イメージ図の作成"),
    ("infer", "推論（原因究明・新発見・提案）"),
]

# LLM を使う工程だけ。使わない工程は原価 0 なので見積りに出さない。
LLM_STEPS = {"extract", "thread", "stages", "parts", "illust", "infer"}

DEFAULT_QUESTION = "この案件の問題の原因は何か。進捗率を上げるために何ができるか。"


def _thread_name(ledger: Ledger, thread_id: str) -> str:
    r = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    return r["name"] if r else thread_id


# --------------------------------------------------------------------------
# 見積り
# --------------------------------------------------------------------------

def estimate(ledger: Ledger, thread_id: str | None, *,
             steps: list[str] | None = None) -> dict:
    """走らせる前に、工程ごとの原価を積み上げて返す。API は呼ばない。"""
    from projecttree import illustrate as _ill
    from projecttree import inference as _inf

    want = set(steps or [s for s, _ in STEPS])
    rows: list[dict] = []
    total = 0.0

    if thread_id is None:
        for key, label in STEPS:
            if key not in want:
                continue
            rows.append({"step": key, "label": label, "usd": 0.0,
                         "note": "案件を作ってから見積もります"})
        return {"steps": rows, "total_usd": 0.0,
                "note": "新規案件のため、実行時に工程ごとの原価を確定します。"}

    pending = ledger.conn.execute(
        "SELECT COUNT(*) c FROM documents d WHERE NOT EXISTS "
        "(SELECT 1 FROM events e WHERE e.doc_id = d.doc_id)").fetchone()["c"]
    n_events = ledger.conn.execute(
        "SELECT COUNT(*) c FROM events WHERE thread_id = ?", (thread_id,)).fetchone()["c"]

    for key, label in STEPS:
        if key not in want:
            continue
        usd, note = 0.0, ""
        if key == "extract":
            # 資料1件あたり実測で概ね $0.01。未抽出のぶんだけ掛かる。
            usd = round(pending * 0.01, 4)
            note = f"未抽出 {pending} 件" if pending else "未抽出なし"
        elif key == "thread":
            # 未割り当てのイベント1件あたり概ね $0.002。
            unassigned = ledger.conn.execute(
                "SELECT COUNT(*) c FROM events WHERE thread_id IS NULL").fetchone()["c"]
            usd = round(unassigned * 0.002, 4)
            note = f"未割り当て {unassigned} 件" if unassigned else "未割り当てなし"
        elif key == "stages":
            usd, note = 0.0, "ルール分類は原価 0（決まらない分のみ別途 LLM）"
        elif key == "parts":
            e = _inf.preflight("infer_part", _inf.PART_SYSTEM, "x" * (n_events * 60), n_events * 45)
            usd, note = e["usd"], e["model"]
        elif key == "illust":
            n_st = ledger.conn.execute(
                "SELECT COUNT(*) c FROM stages s JOIN stage_events se ON se.stage_id = s.stage_id "
                "WHERE s.thread_id = ? GROUP BY s.stage_no", (thread_id,)).fetchall()
            have = ledger.conn.execute(
                "SELECT COUNT(*) c FROM illustrations WHERE thread_id = ? AND anchor = 'stage'",
                (thread_id,)).fetchone()["c"] if _has_illust(ledger) else 0
            need = max(0, len(n_st) - have)
            per = _ill.estimate(900)["usd"]
            usd, note = round(per * need, 4), f"未作成 {need} 段階分"
        elif key == "infer":
            e = _inf.preflight("infer_proposal", _inf.INFER_SYSTEM, "x" * (n_events * 60), 1200)
            usd, note = e["usd"], e["model"]
        else:
            note = "LLM 不使用"
        total += usd
        rows.append({"step": key, "label": label, "usd": usd, "note": note,
                     "uses_llm": key in LLM_STEPS})

    return {"thread_id": thread_id, "name": _thread_name(ledger, thread_id),
            "steps": rows, "total_usd": round(total, 4),
            "total_jpy": round(total * 150, 1),
            "note": "概算です。実測は実行後に返します。"}


def _has_illust(ledger: Ledger) -> bool:
    return ledger.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='illustrations'"
    ).fetchone() is not None


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------

def run(ledger: Ledger, *, thread_id: str | None = None,
        project_name: str | None = None,
        question: str | None = None,
        steps: list[str] | None = None,
        illustrate_stages: bool = True) -> dict:
    """一括実行。工程ごとの結果と実測原価を返す。"""
    from projecttree import illustrate as _ill
    from projecttree import inference as _inf
    from projecttree import modelgen as _mg
    from projecttree import progress as _prg
    from projecttree import projects as _proj
    from projecttree import stages as _stg

    want = set(steps or [s for s, _ in STEPS])
    results: list[dict] = []
    total = 0.0

    def add(key: str, label: str, r: dict):
        nonlocal total
        c = r.get("cost_usd") or 0
        total += c
        results.append({"step": key, "label": label, **r, "cost_usd": round(c, 4)})

    # 1. プロジェクト -------------------------------------------------------
    if "project" in want:
        if thread_id:
            add("project", "プロジェクトの確認・作成",
                {"status": "skipped", "reason": "既存の案件を使います",
                 "thread_id": thread_id, "name": _thread_name(ledger, thread_id)})
        else:
            name = (project_name or "").strip() or "新規案件"
            r = _proj.create(ledger, name)
            thread_id = r["thread_id"]
            add("project", "プロジェクトの確認・作成", r)

    if not thread_id:
        return {"status": "error", "reason": "案件が特定できません", "steps": results}

    # 2. 時系列・文章 -------------------------------------------------------
    if "extract" in want:
        add("extract", "時系列・文章の作成", _run_extract(ledger, thread_id))

    # 3. 案件への割り当て ---------------------------------------------------
    if "thread" in want:
        add("thread", "案件への割り当て", _run_thread(ledger, thread_id))

    # 4. 段階 ---------------------------------------------------------------
    if "stages" in want:
        try:
            made = _stg.build_stages(ledger, thread_id, method="rule")
            r = {"status": "ok", "stages": made, "cost_usd": 0.0,
                 "note": "ルール分類のため原価 0"}
            llm_r = _inf.classify_stages_llm(ledger, thread_id)
            if llm_r.get("status") == "ok":
                r["llm_applied"] = llm_r.get("applied")
                r["cost_usd"] = llm_r.get("cost_usd", 0)
            else:
                r["llm"] = llm_r.get("reason") or llm_r.get("status")
        except Exception as e:
            r = {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:160]}"}
        add("stages", "段階の分類", r)

    # 4. 2D/3Dモデル --------------------------------------------------------
    if "model" in want:
        add("model", "2D/3Dモデルの作成", _run_model(ledger, thread_id, _mg, _prg))

    # 5. 進捗率 -------------------------------------------------------------
    if "progress" in want:
        try:
            _prg.ensure_tables(ledger)
            n = _prg.sync_from_ledger(ledger, thread_id)
            ov = _prg.overall(ledger, thread_id)
            r = {"status": "ok", "updated": n, "percent": ov.get("percent"),
                 "parts": ov.get("parts"), "cost_usd": 0.0, "note": "台帳から算出（原価 0）"}
        except Exception as e:
            r = {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:160]}"}
        add("progress", "進捗率の算出", r)

    # 6. モデル部位と記録の対応 ---------------------------------------------
    if "parts" in want:
        try:
            _inf.ensure_tables(ledger)
            r = _inf.infer_parts(ledger, thread_id)
        except Exception as e:
            r = {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:160]}"}
        add("parts", "モデル部位と記録の対応付け", r)

    # 7. 資料用イメージ図 ---------------------------------------------------
    if "illust" in want and illustrate_stages:
        add("illust", "資料用イメージ図の作成", _run_illust(ledger, thread_id, _ill))

    # 8. 推論 ---------------------------------------------------------------
    if "infer" in want:
        try:
            r = _inf.infer_from_prompt(ledger, thread_id, question or DEFAULT_QUESTION)
        except Exception as e:
            r = {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:160]}"}
        add("infer", "推論（原因究明・新発見・提案）", r)

    ok = sum(1 for r in results if r.get("status") == "ok")
    return {"status": "ok", "thread_id": thread_id,
            "name": _thread_name(ledger, thread_id),
            "steps": results, "succeeded": ok, "total": len(results),
            "total_cost_usd": round(total, 4), "total_cost_jpy": round(total * 150, 1)}


def _run_extract(ledger: Ledger, thread_id: str) -> dict:
    """未抽出の資料から events / claims を作る。extractor をそのまま使う。"""
    pending = ledger.documents_without_events()
    if not pending:
        return {"status": "skipped", "reason": "未抽出の資料はありません", "cost_usd": 0.0}
    try:
        import extractor
    except Exception as e:
        return {"status": "error", "reason": f"extractor を読み込めません: {e}"}

    # llm.client は import 時に作られるため、画面から後で設定したキーを持っていない。
    # extractor.py は llm.client を直に使うので、そのままだと全件 401 で落ちる
    # （events が 1 件も増えないのに成功に見える）。呼ぶ直前に差し替える。
    # extractor.py / llm.py は変更しない。
    from projecttree import provider as _prov
    prev_client = llm.client
    try:
        llm.client = _prov.get_client()
    except Exception as e:
        return {"status": "error",
                "reason": f"APIキーを解決できません（⚙設定 から登録してください）: {e}"}

    before = ledger.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    cost_before = ledger.conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM runs").fetchone()["s"]

    # extractor.main() は argparse で sys.argv を読む。サーバー内から呼ぶと
    # uvicorn の引数を拾ってしまうので、呼び出しの間だけ差し替える。
    # extractor.py 自体は変更しない。
    argv = sys.argv
    try:
        sys.argv = ["extractor"]
        extractor.main()
    except SystemExit:
        pass
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        sys.argv = argv
        llm.client = prev_client

    after = ledger.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    cost_after = ledger.conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM runs").fetchone()["s"]
    added = after - before
    cost = round(cost_after - cost_before, 4)
    if added == 0:
        # 画像の添付など、そもそも本文を持たない資料からは何も取れない。
        # それしか残っていないなら失敗ではないので、素通しにする。
        textless = sum(1 for d in pending if d["source_type"] == "attachment")
        if textless == len(pending):
            return {"status": "skipped", "documents": len(pending), "events_added": 0,
                    "cost_usd": cost,
                    "reason": f"残り {len(pending)} 件は本文を持たない添付のみです"}
        # 本文のある資料があったのにイベントが1件も増えていない。成功として通すと
        # 後続の工程が空回りするだけなので、ここで気づけるようにする。
        return {"status": "error", "documents": len(pending), "events_added": 0,
                "cost_usd": cost,
                "reason": "資料はありましたが、抽出できたイベントが0件でした。"
                          "APIキーと接続先（⚙設定）を確認してください。"}
    return {"status": "ok", "documents": len(pending),
            "events_added": added, "cost_usd": cost}


def _run_thread(ledger: Ledger, thread_id: str) -> dict:
    """thread_id が付いていない events を案件へ割り当てる。

    extractor は events を作るところまでで、どの案件のものかは判定しない。
    その判定は threader が持っているので、そのまま呼ぶ。
    """
    unassigned = ledger.conn.execute(
        "SELECT COUNT(*) c FROM events WHERE thread_id IS NULL").fetchone()["c"]
    if not unassigned:
        return {"status": "skipped", "reason": "未割り当てのイベントはありません",
                "cost_usd": 0.0}
    try:
        import threader
    except Exception as e:
        return {"status": "error", "reason": f"threader を読み込めません: {e}"}

    from projecttree import provider as _prov
    prev_client = llm.client
    try:
        llm.client = _prov.get_client()
    except Exception as e:
        return {"status": "error",
                "reason": f"APIキーを解決できません（⚙設定 から登録してください）: {e}"}

    cost_before = ledger.conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM runs").fetchone()["s"]
    thread_err = None
    try:
        threader.build_threads()
    except Exception as e:
        # ここで抜けると、取込時の指定による補正まで飛んでしまう。
        # 割り当てに失敗しても、明示された所属は反映させたいので先へ進む。
        thread_err = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        llm.client = prev_client

    left = ledger.conn.execute(
        "SELECT COUNT(*) c FROM events WHERE thread_id IS NULL").fetchone()["c"]
    cost_after = ledger.conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM runs").fetchone()["s"]

    # threader は既存の threads を見ずに、資料から読んだ案件名で新しく作る。
    # そのため実行のたびに同名の案件が増え、記録は新しい方へ、モデルや段階は
    # 古い方へ残る、という分裂が起きる（実際 5 回の実行で 22 件が 166 件に
    # 膨らんだ）。走らせた直後に、同名で割れたものをすべて 1 つに戻す。
    merged = _merge_same_name(ledger, thread_id)
    dedup = None
    try:
        from projecttree import projects as _pj
        d = _pj.merge_duplicates(ledger, dry_run=False)
        if d.get("merged_threads"):
            dedup = d["merged_threads"]
    except Exception:
        pass          # 統合に失敗しても、割り当てそのものは成立している

    # 取り込み時に「この案件のもの」と指定された資料は、その指定を優先する。
    # threader は名前が似ているだけの別案件へ引き寄せることがあるため。
    from projecttree import docthread as _dt
    fixed = _dt.reassign(ledger)

    mine = ledger.conn.execute(
        "SELECT COUNT(*) c FROM events WHERE thread_id = ?", (thread_id,)).fetchone()["c"]
    r = {"status": "ok", "assigned": unassigned - left, "remaining": left,
         "events_in_thread": mine,
         "cost_usd": round(cost_after - cost_before, 4)}
    notes = []
    if thread_err:
        r["status"] = "error"
        r["reason"] = f"案件の推定に失敗しました（{thread_err}）"
    if merged:
        r["merged_duplicates"] = merged
        notes.append(f"同名で割れた案件 {len(merged)} 件を統合")
    if fixed.get("moved_events"):
        r["reassigned"] = fixed
        notes.append(f"取込時の指定に従い {fixed['moved_events']} 件の記録を戻した")
    if dedup:
        r["deduplicated"] = dedup
        notes.append(f"同名で割れた案件 {dedup} 件を自動統合")
    if notes:
        r["note"] = " / ".join(notes)
    return r


def _merge_same_name(ledger: Ledger, thread_id: str) -> list[str]:
    """選択中の案件と同名の重複スレッドを、選択中の案件へ寄せる。

    寄せるのは、選択中の案件に記録が無い場合だけにする。両方に記録がある
    ときは人が判断すべきで、勝手にまとめてよい話ではない。
    """
    row = ledger.conn.execute(
        "SELECT name FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if row is None:
        return []
    mine = ledger.conn.execute(
        "SELECT COUNT(*) c FROM events WHERE thread_id = ?", (thread_id,)).fetchone()["c"]
    if mine:
        return []

    dups = [r["thread_id"] for r in ledger.conn.execute(
        "SELECT thread_id FROM threads WHERE name = ? AND thread_id <> ?",
        (row["name"], thread_id)).fetchall()]
    if not dups:
        return []
    try:
        from projecttree import projects as _proj
        _proj.merge(ledger, thread_id, dups)
    except Exception:
        return []
    return dups


def _run_model(ledger: Ledger, thread_id: str, _mg, _prg) -> dict:
    """モデルが無ければプリセットから作る。既にあれば作り直さない。"""
    have = ledger.conn.execute(
        "SELECT COUNT(*) c FROM model_parts WHERE thread_id = ?", (thread_id,)).fetchone()["c"]
    if have:
        return {"status": "skipped", "reason": f"既に {have} 部材あります", "cost_usd": 0.0}

    name = _thread_name(ledger, thread_id)
    preset = _mg.pick_preset(name)
    if preset is None:
        return {"status": "skipped",
                "reason": f"「{name}」に合うプリセットがありません。"
                          "「基本構造の追加」か「写真から作成」で作れます。",
                "cost_usd": 0.0}
    try:
        out = app_dir() / "assets" / "models"
        out.mkdir(parents=True, exist_ok=True)
        r = _mg.generate_for_thread(ledger, thread_id, out, preset=preset)
        _prg.ensure_tables(ledger)
        _prg.sync_from_ledger(ledger, thread_id)
        return {"status": "ok", "preset": preset,
                "parts": ledger.conn.execute(
                    "SELECT COUNT(*) c FROM model_parts WHERE thread_id = ?",
                    (thread_id,)).fetchone()["c"],
                "cost_usd": 0.0, "note": "ローカル生成のため原価 0", "detail": r}
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:160]}"}


def _run_illust(ledger: Ledger, thread_id: str, _ill) -> dict:
    """記録のある段階のうち、まだイメージ図が無いものだけ作る。"""
    _ill.ensure_tables(ledger)
    have = {r["stage_no"] for r in ledger.conn.execute(
        "SELECT stage_no FROM illustrations WHERE thread_id = ? AND anchor = 'stage'",
        (thread_id,)).fetchall()}
    with_ev = [r["stage_no"] for r in ledger.conn.execute(
        "SELECT s.stage_no, COUNT(se.event_id) c FROM stages s "
        "JOIN stage_events se ON se.stage_id = s.stage_id "
        "WHERE s.thread_id = ? GROUP BY s.stage_no HAVING c > 0", (thread_id,)).fetchall()]
    todo = [n for n in with_ev if n not in have]
    if not todo:
        return {"status": "skipped",
                "reason": ("記録のある段階がありません" if not with_ev
                           else f"{len(have)} 段階分は作成済みです"),
                "cost_usd": 0.0}

    made, cost, errs = [], 0.0, []
    for n in todo:
        r = _ill.generate(ledger, thread_id, stage_no=n, confirm=True)
        cost += r.get("cost_usd") or 0
        if r.get("status") == "ok":
            made.append(n)
        else:
            errs.append({"stage_no": n, "reason": r.get("reason")})
    return {"status": "ok" if made else "error",
            "created_stages": made, "errors": errs,
            "cost_usd": round(cost, 4)}
