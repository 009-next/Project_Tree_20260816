"""
rule レーン: パターン検出。LLM は使わない（claude.md 設計原則）。
LLM はここで確定した根拠を言語化するだけ（insight.py）。
"""

import statistics
import sys
import uuid
from datetime import date, datetime, timezone

from ledger import Ledger

MIN_OCCURRENCES = 3          # 「反復」とみなす最小回数
INTERVAL_STDEV_MAX_DAYS = 20  # 間隔のばらつき上限
CHAIN_WINDOW_DAYS = 25        # concern -> issue とみなす最大日数
CHAIN_MIN_PAIRS = 2           # 「連鎖」とみなす最小組数


def _parse(d: str) -> date:
    return datetime.strptime(d[:10], "%Y-%m-%d").date()


def _dedup_same_magnitude(events: list) -> list:
    """同一 mag_value を持つ行は、同じ変更の重複言及とみなし最初の1件だけ残す。"""
    seen_values = set()
    kept = []
    for e in sorted(events, key=lambda r: r["occurred_on"]):
        # mag_value は sqlite Row から取得。丸め誤差を避けるため文字列化して比較。
        key = str(e["mag_value"])
        if key in seen_values:
            continue
        seen_values.add(key)
        kept.append(e)
    return kept


def detect_recurrence(ledger: Ledger) -> list[dict]:
    rows = ledger.conn.execute(
        """SELECT event_id, thread_id, kind, occurred_on, mag_value, mag_unit FROM events
           WHERE kind IN ('schedule_change', 'cost_change')
             AND mag_value IS NOT NULL AND thread_id IS NOT NULL
           ORDER BY thread_id, kind, occurred_on"""
    ).fetchall()

    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["thread_id"], r["kind"]), []).append(r)

    results = []
    for (thread_id, kind), raw_events in groups.items():
        # 同一 magnitude 値の重複言及は「1回の変更が複数文書で確認された」だけであり、
        # 反復ではない。genuine な反復は毎回 magnitude が異なる。
        events = _dedup_same_magnitude(raw_events)
        if len(events) < MIN_OCCURRENCES:
            continue
        dates = [_parse(e["occurred_on"]) for e in events]
        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        if len(gaps) < 2 or min(gaps) <= 0:
            continue
        stdev = statistics.pstdev(gaps)
        if stdev > INTERVAL_STDEV_MAX_DAYS:
            continue
        results.append({
            "thread_id": thread_id,
            "pattern_type": "recurrence",
            "basis_event_ids": [e["event_id"] for e in events],
            "pattern_evidence": {
                "occurrences": len(events),
                "dates": [e["occurred_on"] for e in events],
                "interval_days_mean": round(statistics.mean(gaps), 1),
                "interval_days_stdev": round(stdev, 1),
                "kind_from": kind, "kind_to": kind,
            },
        })
    return results


def detect_chain(ledger: Ledger) -> list[dict]:
    rows = ledger.conn.execute(
        """SELECT event_id, thread_id, kind, occurred_on FROM events
           WHERE kind IN ('concern', 'issue') AND thread_id IS NOT NULL
           ORDER BY thread_id, occurred_on"""
    ).fetchall()

    by_thread: dict[str, list] = {}
    for r in rows:
        by_thread.setdefault(r["thread_id"], []).append(r)

    results = []
    for thread_id, events in by_thread.items():
        concerns = [e for e in events if e["kind"] == "concern"]
        issues = [e for e in events if e["kind"] == "issue"]
        pairs = []
        used_issue_ids = set()
        for c in concerns:
            c_date = _parse(c["occurred_on"])
            best = None
            for i in issues:
                if i["event_id"] in used_issue_ids:
                    continue
                i_date = _parse(i["occurred_on"])
                lag = (i_date - c_date).days
                if 0 < lag <= CHAIN_WINDOW_DAYS:
                    if best is None or lag < best[1]:
                        best = (i, lag)
            if best:
                used_issue_ids.add(best[0]["event_id"])
                pairs.append((c, best[0], best[1]))

        if len(pairs) < CHAIN_MIN_PAIRS:
            continue

        basis_ids = []
        for c, i, _ in pairs:
            basis_ids += [c["event_id"], i["event_id"]]
        lags = [lag for _, _, lag in pairs]
        results.append({
            "thread_id": thread_id,
            "pattern_type": "chain",
            "basis_event_ids": basis_ids,
            "pattern_evidence": {
                "occurrences": len(pairs),
                "dates": [c["occurred_on"] for c, _, _ in pairs] + [i["occurred_on"] for _, i, _ in pairs],
                "interval_days_mean": round(statistics.mean(lags), 1),
                "interval_days_stdev": round(statistics.pstdev(lags), 1) if len(lags) > 1 else 0.0,
                "kind_from": "concern", "kind_to": "issue",
            },
        })
    return results


def main():
    ledger = Ledger()
    recurrence = detect_recurrence(ledger)
    chain = detect_chain(ledger)
    ledger.close()

    print(f"recurrence: {len(recurrence)} 件", file=sys.stderr)
    for r in recurrence:
        ev = r["pattern_evidence"]
        print(f"  thread={r['thread_id']} occurrences={ev['occurrences']} "
              f"mean={ev['interval_days_mean']}d stdev={ev['interval_days_stdev']}d", file=sys.stderr)

    print(f"chain: {len(chain)} 件", file=sys.stderr)
    for r in chain:
        ev = r["pattern_evidence"]
        print(f"  thread={r['thread_id']} pairs={ev['occurrences']} "
              f"mean_lag={ev['interval_days_mean']}d", file=sys.stderr)

    return recurrence, chain


if __name__ == "__main__":
    main()
