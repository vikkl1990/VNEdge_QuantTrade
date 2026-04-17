"""
ML Research Center — live analysis library for VN Edge.

Purpose (per user vision 2026-04-17):
  "Continuously analyze edge/WR, learn, adapt, suggest, block, research.
   Think like an innovation lab for VN Edge."

This module elevates the ml_training service from "HTML training dashboard"
to "active research server." The four tonight-shipped scripts
(analyze_weakspots, policy_search, cohort_health, continuous_learning_loop)
are refactored into importable library functions here, so:
  1. The aiohttp dashboard can expose them as HTTP endpoints
  2. A background scheduler can run them periodically and cache results
  3. Results are persistently logged for the UI timeline

Design rules:
  - READ-ONLY against storage/closed_signals.json
  - NEVER touches trading hot-path files (signal_tracker, scalp_strategy)
  - Side effects ONLY to storage/research/* (new directory)
  - All suggestions are human-in-loop (no auto-apply)

Capabilities exposed:
  - analyze_cohort_health()
  - mine_weakspots(days, min_n)
  - propose_policy_variants(days, target_cohorts)
  - audit_calibration(days)
  - edge_trajectory(days, bucket_hours)  [NEW — rolling WR+PF over time]
  - scan_suggestions()                    [NEW — ranked actions for human review]
  - active_vetoes()                       [NEW — current cohort freezes]
  - timeline(limit)                       [NEW — recent research events]

The ResearchCenter class below adds:
  - In-memory cache with TTL per capability
  - Background scheduler for periodic refresh
  - Event emission to storage/research/events.jsonl for audit timeline
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("bot.research_center")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE = PROJECT_ROOT / "storage"
RESEARCH_DIR = STORAGE / "research"
EVENTS_FILE = RESEARCH_DIR / "events.jsonl"
SUGGESTIONS_FILE = RESEARCH_DIR / "suggestions.json"
VETOES_FILE = RESEARCH_DIR / "active_vetoes.json"
PROMOTIONS_FILE = RESEARCH_DIR / "active_promotions.json"
SCANNER_WEIGHTS_FILE = STORAGE / "scanner_weights.json"
# Remembered mtime of scanner_weights.json — used to detect status changes and emit events
_SCANNER_WEIGHTS_MTIME_CACHE: Dict[str, float] = {"mtime": 0.0}
_SCANNER_STATUS_SNAPSHOT: Dict[str, Tuple[str, float]] = {}


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _parse_ts(x):
    if not x:
        return None
    try:
        return datetime.fromisoformat(x.replace("Z", "+00:00"))
    except Exception:
        return None


def _utc_to_session(utc_hour: int) -> str:
    if 0 <= utc_hour < 7:
        return "asia"
    if 7 <= utc_hour < 14:
        return "europe"
    if 14 <= utc_hour < 21:
        return "us"
    return "late"


def _load_trades(signals_path: Optional[Path] = None, days: int = 30) -> List[dict]:
    path = signals_path or (STORAGE / "closed_signals.json")
    if not path.exists():
        return []
    with open(path) as fh:
        data = json.load(fh)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for t in data:
        ts = _parse_ts(t.get("exit_time"))
        if ts and ts > cutoff:
            out.append(t)
    return out


def _cohort_key(t: dict, include_session: bool = False) -> tuple:
    md = t.get("metadata") or {}
    scn = md.get("setup_type") or md.get("scanner") or "?"
    reg = md.get("regime") or "?"
    side = t.get("side") or "?"
    if not include_session:
        return (scn, reg, side)
    entry_ts = _parse_ts(t.get("entry_time"))
    sess = md.get("session") or (_utc_to_session(entry_ts.hour) if entry_ts else "?")
    return (scn, reg, side, sess)


def _wr_stats(trades: List[dict]) -> Dict[str, Any]:
    if not trades:
        return {"n": 0, "wr": 0, "total_r": 0, "avg_r": 0}
    wins = sum(1 for t in trades if float(t.get("pnl_pct") or 0) > 0)
    total_r = sum(float(t.get("exit_r") or 0) for t in trades)
    n = len(trades)
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "wr": round(wins / n * 100, 2),
        "total_r": round(total_r, 3),
        "avg_r": round(total_r / n, 4),
    }


def _beta_95_ci(wins: int, losses: int) -> Tuple[float, float]:
    """Wilson-ish approximation if scipy unavailable."""
    try:
        from scipy.stats import beta
        lo, hi = beta.ppf([0.025, 0.975], wins + 1, losses + 1)
        return float(lo), float(hi)
    except Exception:
        import math
        n = wins + losses
        p = wins / n if n > 0 else 0.5
        se = math.sqrt(p * (1 - p) / max(n, 1))
        return max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se)


def _emit_event(kind: str, payload: dict):
    """Append structured event to the research timeline."""
    try:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        evt = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **payload}
        with open(EVENTS_FILE, "a") as fh:
            fh.write(json.dumps(evt, default=str) + "\n")
    except Exception as e:
        logger.warning("research_center: emit_event failed: %s", e)


# ─────────────────────────────────────────────────────────────────
# Capabilities (pure functions, callable from HTTP handlers)
# ─────────────────────────────────────────────────────────────────

def analyze_cohort_health(
    historical_days: int = 21,
    rolling_n: int = 50,
    alert_pp: float = 8.0,
    min_n: int = 30,
) -> Dict[str, Any]:
    """Flag cohorts where rolling WR has degraded >alert_pp below historical.

    Returns:
      {
        "generated_at": iso,
        "baseline_days": 21,
        "cohorts": [{cohort, n_hist, wr_hist, wr_recent, delta_pp, verdict}, ...],
        "alerts": [...subset with verdict=DEGRADED],
      }
    """
    trades = _load_trades(days=historical_days)
    by_cohort: Dict[tuple, List[dict]] = defaultdict(list)
    for t in trades:
        by_cohort[_cohort_key(t)].append(t)

    rows = []
    alerts = []
    for cohort, ts_list in by_cohort.items():
        if len(ts_list) < min_n:
            continue
        sorted_ts = sorted(ts_list, key=lambda t: _parse_ts(t.get("exit_time")) or datetime.min)
        recent = sorted_ts[-rolling_n:]
        hist = _wr_stats(sorted_ts)
        rec = _wr_stats(recent)
        delta = rec["wr"] - hist["wr"]
        verdict = "DEGRADED" if delta <= -alert_pp else ("IMPROVED" if delta >= alert_pp else "HEALTHY")
        row = {
            "cohort": f"{cohort[0]} × {cohort[1]} × {cohort[2]}",
            "scanner": cohort[0], "regime": cohort[1], "side": cohort[2],
            "n_historical": hist["n"],
            "wr_historical": hist["wr"],
            "wr_recent": rec["wr"],
            "delta_pp": round(delta, 1),
            "verdict": verdict,
        }
        rows.append(row)
        if verdict == "DEGRADED":
            alerts.append(row)
            _emit_event("cohort_degradation", row)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "historical_days": historical_days,
        "rolling_n": rolling_n,
        "alert_threshold_pp": alert_pp,
        "cohorts": sorted(rows, key=lambda r: r["delta_pp"]),
        "alerts": alerts,
        "healthy_count": sum(1 for r in rows if r["verdict"] == "HEALTHY"),
        "degraded_count": len(alerts),
    }


def mine_weakspots(days: int = 30, min_n: int = 30, gap_pp: float = 10.0) -> Dict[str, Any]:
    """Rank (scanner × regime × side × session) combos by WR vs baseline.

    Returns top weakspots (WR << baseline) and top strengths (WR >> baseline).
    """
    trades = _load_trades(days=days)
    if not trades:
        return {"error": "no trades in window"}
    base_wr = sum(1 for t in trades if float(t.get("pnl_pct") or 0) > 0) / len(trades) * 100

    by_3 = defaultdict(list)
    by_4 = defaultdict(list)
    for t in trades:
        by_3[_cohort_key(t, False)].append(t)
        by_4[_cohort_key(t, True)].append(t)

    def _rows(buckets, name_keys, n_min):
        out = []
        for key, ts in buckets.items():
            if len(ts) < n_min:
                continue
            st = _wr_stats(ts)
            gap = st["wr"] - base_wr
            row = dict(zip(name_keys, key))
            row.update({"n": st["n"], "wr": st["wr"], "gap_pp": round(gap, 1),
                        "total_r": st["total_r"], "avg_r": st["avg_r"]})
            out.append(row)
        return sorted(out, key=lambda r: r["gap_pp"])

    rows_3 = _rows(by_3, ("scanner", "regime", "side"), min_n)
    rows_4 = _rows(by_4, ("scanner", "regime", "side", "session"), max(min_n // 2, 10))
    weakspots = [r for r in rows_4 if r["gap_pp"] <= -gap_pp]
    strengths = sorted([r for r in rows_4 if r["gap_pp"] >= gap_pp], key=lambda r: -r["gap_pp"])[:10]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "min_n": min_n,
        "baseline_wr": round(base_wr, 2),
        "total_trades": len(trades),
        "by_3factor": rows_3,
        "by_4factor": rows_4,
        "weakspots": weakspots,
        "strengths": strengths,
    }


def propose_policy_variants(days: int = 30) -> Dict[str, Any]:
    """For each target cohort, propose min_confidence threshold variants
    with simulated WR lift and Beta posterior 95% CI.

    Variants are NEVER auto-applied — they land in suggestions for
    human approval.
    """
    targets = [
        ("structure_bounce", "high_volatility", "long", "*", {"min_confidence": 75}),
        ("structure_bounce", "high_volatility", "short", "*", {"min_confidence": 75}),
        ("structure_bounce", "sideways", "short", "us", {"min_confidence": 70}),
        ("structure_bounce", "sideways", "long", "asia_early", {"min_confidence": 70}),
        ("structure_bounce", "sideways", "long", "india_midday", {"min_confidence": 70}),
    ]
    trades = _load_trades(days=days)

    def _matches(t, scn, reg, side, sess):
        md = t.get("metadata") or {}
        if md.get("setup_type") != scn and md.get("scanner") != scn:
            return False
        if md.get("regime") != reg:
            return False
        if t.get("side") != side:
            return False
        if sess != "*":
            entry_ts = _parse_ts(t.get("entry_time"))
            t_sess = md.get("session") or (_utc_to_session(entry_ts.hour) if entry_ts else "?")
            if t_sess != sess:
                return False
        return True

    cohorts_out = []
    for (scn, reg, side, sess, cur) in targets:
        ct = [t for t in trades if _matches(t, scn, reg, side, sess)]
        if len(ct) < 20:
            continue
        base = _wr_stats(ct)
        lo, hi = _beta_95_ci(base["wins"], base["losses"])

        variants = []
        cur_conf = cur.get("min_confidence", 70)
        for conf in (cur_conf, cur_conf + 5, cur_conf + 10, cur_conf + 15):
            kept = [t for t in ct if (t.get("confidence") or 0) >= conf]
            if len(kept) < 10:
                continue
            st = _wr_stats(kept)
            kept_lo, kept_hi = _beta_95_ci(st["wins"], st["losses"])
            variants.append({
                "params": {"min_confidence": conf},
                "n_after_filter": st["n"],
                "kept_pct": round(st["n"] / len(ct) * 100, 1),
                "wr": st["wr"],
                "wr_lift_pp": round(st["wr"] - base["wr"], 2),
                "wr_95ci": [round(kept_lo * 100, 1), round(kept_hi * 100, 1)],
                "total_r": st["total_r"],
                "recommended": st["wr"] - base["wr"] > 3.0,
            })

        cohorts_out.append({
            "cohort": {"scanner": scn, "regime": reg, "side": side, "session": sess},
            "baseline_n": base["n"],
            "baseline_wr": base["wr"],
            "baseline_wr_95ci": [round(lo * 100, 1), round(hi * 100, 1)],
            "variants": variants,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "cohorts": cohorts_out,
    }


def edge_trajectory(days: int = 14, bucket_hours: int = 6) -> Dict[str, Any]:
    """Rolling WR + PF + trade count over time buckets. For UI timeline.

    Buckets of bucket_hours hours each, from `days` ago to now.
    """
    trades = _load_trades(days=days)
    now = datetime.now(timezone.utc)
    bucket_sec = bucket_hours * 3600
    buckets: Dict[int, List[dict]] = defaultdict(list)
    for t in trades:
        ts = _parse_ts(t.get("exit_time"))
        if not ts:
            continue
        bucket_idx = int((now - ts).total_seconds() // bucket_sec)
        buckets[bucket_idx].append(t)

    rows = []
    for b in sorted(buckets.keys(), reverse=True):
        ts_list = buckets[b]
        st = _wr_stats(ts_list)
        wins_pnl = sum(float(t.get("pnl_pct") or 0) for t in ts_list if float(t.get("pnl_pct") or 0) > 0)
        loss_pnl = sum(float(t.get("pnl_pct") or 0) for t in ts_list if float(t.get("pnl_pct") or 0) < 0)
        pf = wins_pnl / abs(loss_pnl) if loss_pnl else float("inf")
        bucket_end = (now - timedelta(seconds=b * bucket_sec)).replace(minute=0, second=0, microsecond=0)
        rows.append({
            "bucket_end": bucket_end.isoformat(),
            "hours_ago": b * bucket_hours,
            "trades": st["n"],
            "wr": st["wr"],
            "total_r": st["total_r"],
            "avg_r": st["avg_r"],
            "pf": round(pf, 2) if pf != float("inf") else None,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "bucket_hours": bucket_hours,
        "buckets": rows,
    }


def active_vetoes() -> Dict[str, Any]:
    """Read active cohort freezes. Returns empty if none set."""
    if not VETOES_FILE.exists():
        return {"vetoes": [], "count": 0}
    try:
        with open(VETOES_FILE) as fh:
            data = json.load(fh)
        return {
            "vetoes": data.get("vetoes", []),
            "count": len(data.get("vetoes", [])),
            "last_updated": data.get("last_updated"),
        }
    except Exception as e:
        return {"error": str(e), "vetoes": [], "count": 0}


def active_promotions() -> Dict[str, Any]:
    """Read approved cohort-level scanner promotions. These override the global
    scanner_weights.json status for the specific cohort only. Shadow-first:
    `mode` field gates whether the bot enforces or just logs."""
    if not PROMOTIONS_FILE.exists():
        return {"promotions": [], "count": 0}
    try:
        with open(PROMOTIONS_FILE) as fh:
            data = json.load(fh)
        return {
            "promotions": data.get("promotions", []),
            "count": len(data.get("promotions", [])),
            "last_updated": data.get("last_updated"),
        }
    except Exception as e:
        return {"error": str(e), "promotions": [], "count": 0}


def scanner_registry() -> Dict[str, Any]:
    """Read live per-scanner status from scanner_weights.json + detect transitions.

    Returns rich info for UI:
      - scanners: list of {name, status, weight, expectancy_r, win_rate, sample_count,
                   edge_ratio, avg_mae_r, avg_mfe_r, reason, recovery_stage, last_updated}
      - summary: {active, reduced, suppressed, shadow, total}
      - stale_seconds: how old the file is vs now
      - source_path: where it was loaded from (for ops debugging)

    Side effect: if any scanner transitioned status since the last read, emits
    a `scanner_status_change` event to events.jsonl.
    """
    result: Dict[str, Any] = {
        "scanners": [],
        "summary": {"active": 0, "reduced": 0, "suppressed": 0, "shadow": 0, "total": 0},
        "source_path": str(SCANNER_WEIGHTS_FILE),
        "stale_seconds": None,
        "loaded": False,
    }
    if not SCANNER_WEIGHTS_FILE.exists():
        result["error"] = "scanner_weights.json not found (expected sync from trading VM)"
        return result
    try:
        st = SCANNER_WEIGHTS_FILE.stat()
        result["stale_seconds"] = int(time.time() - st.st_mtime)
        with open(SCANNER_WEIGHTS_FILE) as fh:
            raw = json.load(fh) or {}
    except Exception as e:
        result["error"] = f"load failed: {e}"
        return result

    # scanner_weights.json is a flat dict of name -> ScannerState-as-dict
    if not isinstance(raw, dict):
        result["error"] = "unexpected schema (expected dict of scanners)"
        return result

    scanners: List[Dict[str, Any]] = []
    current_snapshot: Dict[str, Tuple[str, float]] = {}
    for name, state in raw.items():
        if not isinstance(state, dict):
            continue
        status = state.get("status", "active")
        weight = float(state.get("weight", 1.0))
        expectancy = float(state.get("expectancy_r", 0.0))
        win_rate = float(state.get("win_rate", 0.0))
        n = int(state.get("sample_count", 0))
        # win_rate may be stored as percent (71.5) or fraction (0.715) — normalise
        if win_rate > 1.5:
            wr_pct = round(win_rate, 2)
        else:
            wr_pct = round(win_rate * 100, 2)
        scanners.append({
            "name": name,
            "status": status,
            "weight": round(weight, 3),
            "expectancy_r": round(expectancy, 4),
            "avg_r": round(float(state.get("avg_r", 0.0)), 4),
            "total_r": round(float(state.get("total_r", 0.0)), 3),
            "win_rate": wr_pct,
            "sample_count": n,
            "edge_ratio": round(float(state.get("edge_ratio", 0.0)), 3),
            "avg_mae_r": round(float(state.get("avg_mae_r", 0.0)), 4),
            "avg_mfe_r": round(float(state.get("avg_mfe_r", 0.0)), 4),
            "rolling_expectancy": round(float(state.get("rolling_expectancy", 0.0)), 4),
            "recovery_stage": state.get("recovery_stage", ""),
            "reason": state.get("reason", ""),
            "last_updated": state.get("last_updated", ""),
        })
        current_snapshot[name] = (status, round(weight, 2))

    # Detect transitions vs last read — emit event for each change
    previous = _SCANNER_STATUS_SNAPSHOT
    for name, (new_status, new_weight) in current_snapshot.items():
        prev = previous.get(name)
        if prev is None:
            # First time we see this scanner — emit an informational event, not a change
            _emit_event("scanner_seen", {"scanner": name, "status": new_status, "weight": new_weight})
        elif prev != (new_status, new_weight):
            _emit_event("scanner_status_change", {
                "scanner": name,
                "from_status": prev[0], "from_weight": prev[1],
                "to_status": new_status, "to_weight": new_weight,
            })
    # Update snapshot
    _SCANNER_STATUS_SNAPSHOT.clear()
    _SCANNER_STATUS_SNAPSHOT.update(current_snapshot)

    # Summary counts
    summary = {"active": 0, "reduced": 0, "suppressed": 0, "shadow": 0, "total": len(scanners)}
    for s in scanners:
        summary[s["status"]] = summary.get(s["status"], 0) + 1

    # Sort: most-penalised first (suppressed > shadow > reduced > active),
    # then by sample_count desc so high-volume scanners lead within each tier.
    status_rank = {"suppressed": 0, "shadow": 1, "reduced": 2, "active": 3}
    scanners.sort(key=lambda s: (status_rank.get(s["status"], 4), -s["sample_count"]))

    result["loaded"] = True
    result["scanners"] = scanners
    result["summary"] = summary
    return result


def propose_scanner_promotions(
    min_n: int = 15, edge_gap_pp: float = 5.0, days: int = 30
) -> Dict[str, Any]:
    """Find cohorts that could earn a local 'ACTIVE' promotion even if the
    scanner is globally REDUCED/SUPPRESSED.

    Logic: A scanner with overall bad edge may still have a strong sub-cohort
    (e.g., liquidity_sweep is globally -0.14R but could be +0.30R in
    trending_up × long × us_session). This surfaces those pockets for
    human-in-loop approval. Writes nothing; UI calls the approve endpoint.

    Criteria for a candidate:
      - Scanner status is reduced/suppressed (has room to be promoted)
      - Cohort n >= min_n
      - Cohort WR >= baseline_wr + edge_gap_pp
      - Cohort avg_r > 0
    """
    registry = scanner_registry()
    if not registry.get("loaded"):
        return {"candidates": [], "count": 0, "source_note": registry.get("error", "registry unavailable")}

    # Index scanners by name
    penalised = {
        s["name"]: s for s in registry["scanners"]
        if s["status"] in ("reduced", "suppressed", "shadow")
    }
    if not penalised:
        return {"candidates": [], "count": 0, "source_note": "no penalised scanners — nothing to propose"}

    # Compute baseline + cohort WRs
    trades = _load_trades(days=days)
    if not trades:
        return {"candidates": [], "count": 0, "source_note": "no trades in window"}
    total_wins = sum(1 for t in trades if (t.get("exit_r") or 0) > 0)
    baseline_wr = (total_wins / len(trades)) * 100.0

    # Group by 3-factor (scanner × regime × side)
    by_cohort: Dict[tuple, List[dict]] = defaultdict(list)
    for t in trades:
        md = t.get("metadata") or {}
        scn = md.get("setup_type") or md.get("scanner") or "?"
        if scn not in penalised:
            continue  # we only care about penalised scanners' cohorts
        reg = md.get("regime") or "?"
        side = t.get("side") or "?"
        by_cohort[(scn, reg, side)].append(t)

    candidates: List[Dict[str, Any]] = []
    for (scn, reg, side), cohort_trades in by_cohort.items():
        n = len(cohort_trades)
        if n < min_n:
            continue
        wins = sum(1 for t in cohort_trades if (t.get("exit_r") or 0) > 0)
        wr = (wins / n) * 100.0
        avg_r = sum(t.get("exit_r") or 0 for t in cohort_trades) / n
        gap_pp = wr - baseline_wr
        if gap_pp < edge_gap_pp or avg_r <= 0:
            continue
        # Wilson lower bound (approximate, for stability filter)
        p = wins / n
        z = 1.96
        denom = 1 + z * z / n
        centre = p + z * z / (2 * n)
        spread = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
        wilson_lower = max(0.0, (centre - spread) / denom) * 100.0
        # Require lower CI to be near or above baseline — otherwise it's sampling noise
        if wilson_lower < baseline_wr - 3.0:
            continue

        candidates.append({
            "scanner": scn,
            "regime": reg,
            "side": side,
            "n": n,
            "wr": round(wr, 2),
            "avg_r": round(avg_r, 4),
            "gap_pp": round(gap_pp, 2),
            "wilson_lower_ci": round(wilson_lower, 2),
            "baseline_wr": round(baseline_wr, 2),
            "current_global_status": penalised[scn]["status"],
            "current_global_weight": penalised[scn]["weight"],
            "rationale": (
                f"Globally {penalised[scn]['status']} (w={penalised[scn]['weight']}) "
                f"but in {reg} × {side}: WR {wr:.1f}% on n={n}, "
                f"avg_r +{avg_r:.3f}R, lower_CI {wilson_lower:.1f}% > baseline-3"
            ),
        })

    # Sort by gap_pp (biggest upside first)
    candidates.sort(key=lambda c: -c["gap_pp"])

    return {
        "candidates": candidates,
        "count": len(candidates),
        "baseline_wr": round(baseline_wr, 2),
        "total_trades": len(trades),
        "min_n": min_n,
        "edge_gap_pp": edge_gap_pp,
        "days": days,
    }


def scan_suggestions() -> Dict[str, Any]:
    """Read pending suggestions for human review."""
    if not SUGGESTIONS_FILE.exists():
        return {"suggestions": [], "count": 0}
    try:
        with open(SUGGESTIONS_FILE) as fh:
            data = json.load(fh)
        return {
            "suggestions": data.get("suggestions", []),
            "count": len(data.get("suggestions", [])),
            "last_updated": data.get("last_updated"),
        }
    except Exception as e:
        return {"error": str(e), "suggestions": [], "count": 0}


def timeline(limit: int = 50) -> List[Dict[str, Any]]:
    """Read the most recent research events for UI display."""
    if not EVENTS_FILE.exists():
        return []
    lines = []
    try:
        with open(EVENTS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return lines[-limit:]


# ─────────────────────────────────────────────────────────────────
# Research Center — periodic scheduler + cache
# ─────────────────────────────────────────────────────────────────

class ResearchCenter:
    """In-process research engine: runs capabilities on a schedule and caches results.

    Wired into the ML dashboard process — the dashboard's HTTP handlers
    hit the cache, not the raw functions, so user-facing latency is <50ms.

    Schedule (tunable):
      cohort_health      every  5 min
      weakspots          every 30 min
      policy_variants    every 60 min
      edge_trajectory    every 10 min
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._task: Optional[asyncio.Task] = None

    def _set(self, key: str, value: Any, ttl_sec: int):
        self._cache[key] = {
            "value": value,
            "cached_at": time.time(),
            "ttl": ttl_sec,
        }

    def get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if not entry:
            return None
        if time.time() - entry["cached_at"] > entry["ttl"]:
            return None  # expired; caller can refresh
        return entry["value"]

    def refresh_all(self, force: bool = False):
        """Synchronously refresh all caches (for initial warm-up or manual refresh)."""
        logger.info("research_center: refreshing all capabilities (force=%s)", force)
        try:
            self._set("cohort_health", analyze_cohort_health(), 600)
        except Exception as e:
            logger.exception("cohort_health refresh failed: %s", e)
        try:
            self._set("weakspots", mine_weakspots(days=30), 3600)
        except Exception as e:
            logger.exception("weakspots refresh failed: %s", e)
        try:
            self._set("policy_variants", propose_policy_variants(days=30), 7200)
        except Exception as e:
            logger.exception("policy_variants refresh failed: %s", e)
        try:
            self._set("edge_trajectory", edge_trajectory(days=14, bucket_hours=6), 1200)
        except Exception as e:
            logger.exception("edge_trajectory refresh failed: %s", e)
        try:
            self._set("scanner_registry", scanner_registry(), 120)
        except Exception as e:
            logger.exception("scanner_registry refresh failed: %s", e)
        try:
            self._set("scanner_promotions", propose_scanner_promotions(), 1800)
        except Exception as e:
            logger.exception("scanner_promotions refresh failed: %s", e)
        _emit_event("cache_refresh", {"keys": list(self._cache.keys())})

    async def start(self):
        """Kick off periodic refresh loop."""
        self.refresh_all()
        self._task = asyncio.create_task(self._loop(), name="research_scheduler")
        logger.info("research_center: scheduler started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        """Simple tick-based scheduler. Each capability runs on its own cadence."""
        last_runs = {k: 0.0 for k in (
            "cohort_health", "weakspots", "policy_variants", "edge_trajectory",
            "scanner_registry", "scanner_promotions",
        )}
        intervals = {
            "cohort_health": 300,         # 5 min
            "weakspots": 1800,            # 30 min
            "policy_variants": 3600,      # 1 h
            "edge_trajectory": 600,       # 10 min
            "scanner_registry": 60,       # 1 min — catches status transitions fast
            "scanner_promotions": 900,    # 15 min — expensive (baseline + cohort groupby)
        }
        runners: Dict[str, Callable] = {
            "cohort_health": lambda: analyze_cohort_health(),
            "weakspots": lambda: mine_weakspots(days=30),
            "policy_variants": lambda: propose_policy_variants(days=30),
            "edge_trajectory": lambda: edge_trajectory(days=14, bucket_hours=6),
            "scanner_registry": lambda: scanner_registry(),
            "scanner_promotions": lambda: propose_scanner_promotions(),
        }
        ttls = {
            "cohort_health": 600, "weakspots": 3600, "policy_variants": 7200,
            "edge_trajectory": 1200, "scanner_registry": 120, "scanner_promotions": 1800,
        }

        try:
            while True:
                now = time.time()
                for key, interval in intervals.items():
                    if now - last_runs[key] >= interval:
                        try:
                            result = await asyncio.to_thread(runners[key])
                            self._set(key, result, ttls[key])
                            last_runs[key] = now
                            _emit_event("cache_update", {"key": key})
                        except Exception as e:
                            logger.warning("research_center: %s run failed: %s", key, e)
                # Sleep 60s between ticks
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass


# Module-level singleton accessor (used by dashboard)
_SINGLETON: Optional[ResearchCenter] = None


def get_center() -> ResearchCenter:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = ResearchCenter()
    return _SINGLETON
