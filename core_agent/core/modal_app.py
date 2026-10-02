"""
Strike Tips — Modal Cloud Deployment (SA + UK Research Edition)

Adds:
- South African scan: /scan
- UK scan: /scan-uk
- Combined scan: /scan-all
- Ranked picks: /picks
- Value list: /value
- Suggested stakes: /stakes
- Favourite threats/value alternatives: /threats
- Daily accumulators: /acca-safe and /acca-value
- Performance/research report: /report
- Existing status/chart/auth/webhook features
- Recommendation logging for later review

Important:
- SA analysis continues to use the project's existing StrikeTips engine.
- UK racecards use The Racing API when RACING_API_USER and
  RACING_API_PASSWORD are present in a Modal secret already attached
  to this app (for example strike-tips-search).
- Gemini analysis is optional. Set GEMINI_API_KEY in strike-tips-api-key.
- Research/stake suggestions are informational. This code does not
  automatically place bets from /picks or accumulator commands.
"""

import json
import logging
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import modal

from core_agent.core.logging_setup import configure_logging

configure_logging()
logger = logging.getLogger("modal-app")

# ---------------------------------------------------------------------
# Modal configuration
# ---------------------------------------------------------------------

image = modal.Image.from_dockerfile("Dockerfile")
app = modal.App("strike-tips-racing")

data_volume = modal.Volume.from_name("strike-tips-data", create_if_missing=True)

secrets = [
    modal.Secret.from_name("strike-tips-secrets"),
    modal.Secret.from_name("strike-tips-api-key"),
    modal.Secret.from_name("strike-tips-search"),
]

BASE_URL = "https://vetho-alton--strike-tips-racing-serve-api.modal.run"
TELEGRAM_WEBHOOK_URL = f"{BASE_URL}/telegram-webhook"

DATA_DIR = Path("/app/data")
LATEST_RECS_FILE = DATA_DIR / "latest_recommendations.json"
RECS_LOG_FILE = DATA_DIR / "recommendation_log.jsonl"
UK_LATEST_FILE = DATA_DIR / "uk_scan_latest.json"

# ---------------------------------------------------------------------
# Research / bankroll policy
# ---------------------------------------------------------------------

MIN_EDGE = 5.0
HIGH_EDGE = 10.0
VERY_HIGH_EDGE = 15.0

MIN_WIN_PROB = 0.25
MIN_ACCA_SAFE_PROB = 0.55
MIN_ACCA_VALUE_PROB = 0.45

MIN_ODDS = 1.60
MAX_SINGLE_ODDS = 12.0

KELLY_FRACTION = 0.25          # quarter Kelly
MAX_SINGLE_STAKE_PCT = 0.025   # 2.5% bankroll
MAX_DAILY_EXPOSURE_PCT = 0.10  # 10% bankroll

SAFE_ACCA_MIN_ODDS = 1.50
SAFE_ACCA_MAX_ODDS = 2.50
SAFE_ACCA_MAX_LEGS = 3

VALUE_ACCA_MIN_ODDS = 1.60
VALUE_ACCA_MAX_ODDS = 4.00
VALUE_ACCA_MAX_LEGS = 4


# ---------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------

def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _edge(vb: Dict[str, Any]) -> float:
    raw = (
        vb.get("edge_percent")
        or vb.get("edge")
        or vb.get("edge_percentage")
        or vb.get("estimated_edge")
        or 0
    )
    edge = _f(raw)
    if 0 < edge < 1:
        edge *= 100
    return edge


def _odds(vb: Dict[str, Any]) -> float:
    return _f(
        vb.get("odds_decimal")
        or vb.get("offered_odds")
        or vb.get("bookmaker_odds")
        or vb.get("odds")
        or 0
    )


def _prob(vb: Dict[str, Any], odds: float, edge_pct: float) -> float:
    raw = (
        vb.get("estimated_probability")
        or vb.get("win_probability")
        or vb.get("probability")
        or 0
    )
    p = _f(raw)
    if p > 1:
        p /= 100.0
    if 0 < p <= 1:
        return min(max(p, 0.001), 0.999)

    # If the model returned edge + odds but no probability, derive:
    # true probability ~= implied probability + edge percentage points.
    if odds > 1:
        implied = 1.0 / odds
        return min(max(implied + edge_pct / 100.0, 0.001), 0.999)
    return 0.0


def _confidence(prob: float, edge_pct: float, odds: float) -> str:
    if prob >= 0.55 and edge_pct >= 10 and 1.5 <= odds <= 4.0:
        return "HIGH"
    if prob >= 0.40 and edge_pct >= 7:
        return "MEDIUM"
    if edge_pct >= MIN_EDGE:
        return "VALUE"
    return "WATCH"


def _quarter_kelly_stake(bankroll: float, prob: float, odds: float) -> float:
    """Quarter-Kelly, capped at 2.5% of bankroll."""
    if bankroll <= 0 or odds <= 1 or not (0 < prob < 1):
        return 0.0
    b = odds - 1.0
    q = 1.0 - prob
    full_kelly = ((b * prob) - q) / b
    if full_kelly <= 0:
        return 0.0
    fraction = min(full_kelly * KELLY_FRACTION, MAX_SINGLE_STAKE_PCT)
    return round(bankroll * fraction, 2)


def _normalize_candidate(
    region: str,
    track: str,
    race: Dict[str, Any],
    vb: Dict[str, Any],
    bankroll: float,
) -> Optional[Dict[str, Any]]:
    horse = str(vb.get("horse") or vb.get("name") or vb.get("horse_name") or "").strip()
    if not horse:
        return None

    odds = _odds(vb)
    edge_pct = _edge(vb)
    prob = _prob(vb, odds, edge_pct)

    if odds <= 1 or prob <= 0:
        return None

    stake = _quarter_kelly_stake(bankroll, prob, odds)
    implied = 1.0 / odds

    return {
        "region": region,
        "track": track,
        "race_number": int(_f(race.get("race_number"), 0)),
        "race_time": race.get("race_time", "TBD"),
        "horse": horse,
        "odds": round(odds, 2),
        "probability": round(prob, 4),
        "implied_probability": round(implied, 4),
        "edge_percent": round(edge_pct, 2),
        "confidence": _confidence(prob, edge_pct, odds),
        "stake": stake,
        "reasoning": str(vb.get("reasoning") or race.get("ai_insight") or "").strip(),
    }


def _extract_candidates_from_scan(
    scan_data: Dict[str, Any],
    bankroll: float,
    region: str = "ZA",
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for track, races in (scan_data or {}).items():
        if not isinstance(races, list):
            continue
        for race in races:
            if not isinstance(race, dict):
                continue
            for vb in race.get("value_bets", []) or []:
                if not isinstance(vb, dict):
                    continue
                c = _normalize_candidate(region, str(track), race, vb, bankroll)
                if c:
                    out.append(c)

    # Best research candidates first: confidence / probability / edge.
    conf_order = {"HIGH": 4, "MEDIUM": 3, "VALUE": 2, "WATCH": 1}
    out.sort(
        key=lambda x: (
            conf_order.get(x["confidence"], 0),
            x["probability"],
            x["edge_percent"],
        ),
        reverse=True,
    )
    return out


def _qualifying_picks(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    picks = [
        c for c in candidates
        if c["edge_percent"] >= MIN_EDGE
        and c["probability"] >= MIN_WIN_PROB
        and MIN_ODDS <= c["odds"] <= MAX_SINGLE_ODDS
        and c["stake"] > 0
    ]

    # Total daily exposure guard.
    if not picks:
        return []
    bankroll_est = max((c["stake"] / MAX_SINGLE_STAKE_PCT for c in picks if c["stake"] > 0), default=0)
    exposure_cap = bankroll_est * MAX_DAILY_EXPOSURE_PCT if bankroll_est else math.inf
    used = 0.0
    result = []
    for c in picks:
        if used + c["stake"] > exposure_cap:
            continue
        result.append(c)
        used += c["stake"]
    return result


def _format_pick(c: Dict[str, Any], idx: Optional[int] = None) -> str:
    prefix = f"{idx}. " if idx else ""
    return (
        f"{prefix}{c['track']} R{c['race_number']} — {c['horse']}\n"
        f"Odds: {c['odds']:.2f} | Win p: {c['probability']*100:.1f}% | "
        f"Edge: +{c['edge_percent']:.1f}%\n"
        f"Confidence: {c['confidence']} | Suggested stake: R{c['stake']:.2f}"
    )


def _save_recommendations(candidates: List[Dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date": date.today().isoformat(),
        "candidates": candidates,
    }
    LATEST_RECS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with RECS_LOG_FILE.open("a", encoding="utf-8") as f:
        for c in candidates:
            row = {"ts": datetime.now().isoformat(timespec="seconds"), **c}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_latest_recommendations() -> List[Dict[str, Any]]:
    try:
        data = json.loads(LATEST_RECS_FILE.read_text(encoding="utf-8"))
        if data.get("date") != date.today().isoformat():
            return []
        return list(data.get("candidates") or [])
    except Exception:
        return []


def _load_sa_scan_file() -> Dict[str, Any]:
    p = DATA_DIR / f"daily_scan_{date.today().isoformat()}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_accumulator(
    candidates: List[Dict[str, Any]],
    mode: str,
    bankroll: float,
) -> Dict[str, Any]:
    if mode == "safe":
        pool = [
            c for c in candidates
            if c["confidence"] == "HIGH"
            and c["probability"] >= MIN_ACCA_SAFE_PROB
            and c["edge_percent"] >= MIN_EDGE
            and SAFE_ACCA_MIN_ODDS <= c["odds"] <= SAFE_ACCA_MAX_ODDS
        ]
        max_legs = SAFE_ACCA_MAX_LEGS
        stake_pct = 0.005  # 0.5% bankroll
    else:
        pool = [
            c for c in candidates
            if c["confidence"] in {"HIGH", "MEDIUM"}
            and c["probability"] >= MIN_ACCA_VALUE_PROB
            and c["edge_percent"] >= 7.0
            and VALUE_ACCA_MIN_ODDS <= c["odds"] <= VALUE_ACCA_MAX_ODDS
        ]
        max_legs = VALUE_ACCA_MAX_LEGS
        stake_pct = 0.0075  # 0.75% bankroll

    # Avoid same race correlation.
    legs: List[Dict[str, Any]] = []
    seen_races = set()
    for c in pool:
        key = (c["region"], c["track"], c["race_number"])
        if key in seen_races:
            continue
        legs.append(c)
        seen_races.add(key)
        if len(legs) >= max_legs:
            break

    if len(legs) < 2:
        return {"ok": False, "message": "NO ACCUMULATOR TODAY — fewer than two selections meet the rules."}

    combined_odds = 1.0
    combined_prob = 1.0
    for c in legs:
        combined_odds *= c["odds"]
        combined_prob *= c["probability"]

    return {
        "ok": True,
        "mode": mode,
        "legs": legs,
        "combined_odds": round(combined_odds, 2),
        "combined_probability": round(combined_prob, 4),
        "stake": round(bankroll * stake_pct, 2),
    }


def _format_acca(acca: Dict[str, Any]) -> str:
    if not acca.get("ok"):
        return str(acca.get("message"))
    title = "SAFE" if acca["mode"] == "safe" else "VALUE"
    lines = [f"🏆 DAILY {title} ACCUMULATOR", ""]
    for i, c in enumerate(acca["legs"], 1):
        lines.append(
            f"{i}. {c['track']} R{c['race_number']} — {c['horse']} @ {c['odds']:.2f}"
            f" | p {c['probability']*100:.1f}% | edge +{c['edge_percent']:.1f}%"
        )
    lines += [
        "",
        f"Combined odds: {acca['combined_odds']:.2f}",
        f"Estimated combined probability: {acca['combined_probability']*100:.1f}%",
        f"Suggested accumulator stake: R{acca['stake']:.2f}",
    ]
    return "\n".join(lines)


def _snapshot_favourites() -> Dict[Tuple[str, int], Dict[str, Any]]:
    """
    Best-effort favourite lookup from market_snapshot_latest.json.
    Returns {(normalized_track, race_no): {horse, odds}}
    """
    p = DATA_DIR / "market_snapshot_latest.json"
    try:
        snap = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

    favs: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for ev in (snap.get("events") or {}).values():
        if not isinstance(ev, dict):
            continue
        track = str(ev.get("course") or "").strip().lower()
        race_no = int(_f(ev.get("raceNumber") or ev.get("race_number"), 0))
        runners = ev.get("runners") or []
        priced = []
        for r in runners:
            if not isinstance(r, dict):
                continue
            horse = str(r.get("name") or r.get("outcomeName") or "").strip()
            odds = _f(r.get("odds"), 0)
            if horse and odds > 1:
                priced.append((odds, horse))
        if priced:
            odds, horse = min(priced)
            favs[(track, race_no)] = {"horse": horse, "odds": odds}
    return favs


# ---------------------------------------------------------------------
# UK provider helpers — The Racing API + optional Gemini
# ---------------------------------------------------------------------

async def _fetch_uk_racecards() -> List[Dict[str, Any]]:
    import httpx

    user = os.environ.get("RACING_API_USER", "").strip()
    password = os.environ.get("RACING_API_PASSWORD", "").strip()
    if not user or not password:
        raise RuntimeError(
            "UK provider not configured. Add RACING_API_USER and "
            "RACING_API_PASSWORD to the Modal secret 'strike-tips-search'."
        )

    # Standard includes odds for UK/Irish racing. If the account does not
    # have Standard access, fall back to Basic/Free racecards.
    endpoints = [
        "https://api.theracingapi.com/v1/racecards/standard",
        "https://api.theracingapi.com/v1/racecards/basic",
        "https://api.theracingapi.com/v1/racecards/free",
    ]

    async with httpx.AsyncClient(auth=(user, password), timeout=45) as client:
        last_error = None
        for url in endpoints:
            try:
                r = await client.get(
                    url,
                    params=[("day", "today"), ("region_codes", "gb"), ("limit", "500")],
                )
                if r.status_code == 200:
                    data = r.json()
                    return list(data.get("racecards") or [])
                last_error = f"{r.status_code}: {r.text[:200]}"
            except Exception as exc:
                last_error = str(exc)
        raise RuntimeError(f"UK racecard API failed: {last_error}")


def _uk_runner_odds(runner: Dict[str, Any]) -> float:
    # Racing API schemas can expose odds in different nested forms by plan.
    for key in ("odds", "decimal_odds", "current_odds"):
        v = runner.get(key)
        if isinstance(v, (int, float, str)):
            o = _f(v, 0)
            if o > 1:
                return o
        if isinstance(v, list):
            # choose a plausible latest decimal quote
            vals = []
            for item in v:
                if isinstance(item, dict):
                    for k in ("decimal", "decimal_odds", "price"):
                        o = _f(item.get(k), 0)
                        if o > 1:
                            vals.append(o)
            if vals:
                return vals[-1]
        if isinstance(v, dict):
            for k in ("decimal", "decimal_odds", "price"):
                o = _f(v.get(k), 0)
                if o > 1:
                    return o
    return 0.0


async def _gemini_rank_uk_race(
    race: Dict[str, Any],
    racing_alpha: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Gemini is used as a synthesis layer, not as the sole source of racing facts.
    It sees the raw racecard plus Racing Alpha's derived signals, when available.

    Transient 429/5xx failures are retried, then the race continues without Gemini.
    """
    import asyncio
    import httpx

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return {}

    runners = race.get("runners") or []
    compact = []
    for r in runners:
        if not isinstance(r, dict):
            continue
        compact.append({
            "horse": r.get("horse") or r.get("name"),
            "number": (
                r.get("number")
                or r.get("horse_number")
                or r.get("saddle")
                or r.get("cloth_number")
            ),
            "draw": r.get("draw"),
            "age": r.get("age"),
            "form": r.get("form"),
            "last_run": r.get("last_run"),
            "ofr": r.get("ofr"),
            "rpr": r.get("rpr"),
            "ts": r.get("ts"),
            "jockey": r.get("jockey"),
            "trainer": r.get("trainer"),
            "lbs": r.get("lbs"),
            "odds": _uk_runner_odds(r) or None,
        })

    prompt = f"""
You are a horse-racing research model.

Use ONLY the supplied racecard and Racing Alpha signals.
Do not invent runners, odds, ratings, jockeys, trainers, or facts.

Race:
course={race.get('course')}
off_time={race.get('off_time')}
race_name={race.get('race_name')}
distance={race.get('distance')}
going={race.get('going')}
surface={race.get('surface')}
race_class={race.get('race_class')}
type={race.get('type')}

Raw runners:
{json.dumps(compact, ensure_ascii=False)}

Racing Alpha derived signals:
{json.dumps(racing_alpha or {}, ensure_ascii=False)[:18000]}

Return JSON only:
{{
  "selections": [
    {{
      "horse": "exact runner name",
      "estimated_probability": 0.00,
      "confidence": "HIGH|MEDIUM|WATCH",
      "reasoning": "maximum two sentences"
    }}
  ]
}}

Return at most 3 runners. Probabilities must be between 0 and 1.
Prefer evidence-backed selections. If evidence is weak, return fewer selections.
""".strip()

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }

    models = [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
    ]
    retry_delays = [1, 2, 4]

    async with httpx.AsyncClient(timeout=90) as client:
        for model in models:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent"
            )

            for attempt, delay in enumerate(retry_delays, start=1):
                try:
                    r = await client.post(
                        url,
                        params={"key": key},
                        json=payload,
                    )

                    if r.status_code == 200:
                        data = r.json()
                        text = (
                            data.get("candidates", [{}])[0]
                            .get("content", {})
                            .get("parts", [{}])[0]
                            .get("text", "")
                        )
                        try:
                            return json.loads(text)
                        except Exception:
                            logger.warning(
                                "Gemini returned non-JSON for %s",
                                race.get("race_name"),
                            )
                            return {}

                    if r.status_code in {408, 429, 500, 502, 503, 504}:
                        logger.warning(
                            "Gemini %s returned HTTP %s on attempt %s",
                            model,
                            r.status_code,
                            attempt,
                        )
                        await asyncio.sleep(delay)
                        continue

                    logger.warning(
                        "Gemini %s unavailable for this race: HTTP %s %s",
                        model,
                        r.status_code,
                        r.text[:200],
                    )
                    break

                except Exception as exc:
                    logger.warning(
                        "Gemini request error on %s attempt %s: %s",
                        model,
                        attempt,
                        exc,
                    )
                    await asyncio.sleep(delay)

    # Do not fail the whole UK scan just because Gemini is unavailable.
    return {}



def _ra_headers() -> Dict[str, str]:
    key = os.environ.get("RACING_ALPHA_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _fetch_racing_alpha_today() -> Dict[str, Any]:
    """
    Racing Alpha:
      GET /api/v1/today
    Keyless use is supported, but a free key raises the daily limit.
    """
    import httpx

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.get(
            "https://racingalpha.co.uk/api/v1/today",
            headers=_ra_headers(),
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}


async def _fetch_racing_alpha_race(race_id: str) -> Dict[str, Any]:
    """
    Racing Alpha:
      GET /api/v1/races/{race_id}
    Returns per-runner derived signals such as model scores, fair prices,
    value-vs-fair and draw-bias verdicts.
    """
    import httpx

    if not race_id:
        return {}

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.get(
            f"https://racingalpha.co.uk/api/v1/races/{race_id}",
            headers=_ra_headers(),
        )
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}


def _ra_today_index(today_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    races = today_payload.get("races") or []
    if isinstance(races, dict):
        races = list(races.values())

    out: Dict[str, Dict[str, Any]] = {}
    for race in races:
        if not isinstance(race, dict):
            continue
        rid = str(
            race.get("race_id")
            or race.get("raceId")
            or race.get("id")
            or ""
        ).strip()
        if rid:
            out[rid] = race
    return out


def _find_nested_runner_signals(payload: Any) -> List[Dict[str, Any]]:
    """
    Racing Alpha's API is explicitly a derived-signals feed.
    This helper is intentionally schema-tolerant so minor field-name changes
    do not break the bot.
    """
    candidates: List[Dict[str, Any]] = []

    def walk(node: Any):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    # A runner signal normally has some name + score/price/value field.
                    name = (
                        item.get("horse")
                        or item.get("runner")
                        or item.get("name")
                        or item.get("selection")
                    )
                    signal_keys = {
                        "score", "ai_score", "model_score", "rating",
                        "fair_price", "fair_odds", "fairPrice",
                        "value", "value_flag", "value_vs_fair",
                        "draw_bias", "draw_verdict",
                    }
                    if name and any(k in item for k in signal_keys):
                        candidates.append(item)
                    walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(payload)

    # Deduplicate by runner name, keeping the richest object.
    best: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        name = str(
            item.get("horse")
            or item.get("runner")
            or item.get("name")
            or item.get("selection")
            or ""
        ).strip().lower()
        if not name:
            continue
        if name not in best or len(item) > len(best[name]):
            best[name] = item

    return list(best.values())


def _ra_runner_name(signal: Dict[str, Any]) -> str:
    return str(
        signal.get("horse")
        or signal.get("runner")
        or signal.get("name")
        or signal.get("selection")
        or ""
    ).strip()


def _ra_score(signal: Dict[str, Any]) -> float:
    return _f(
        signal.get("ai_score")
        or signal.get("model_score")
        or signal.get("score")
        or signal.get("rating"),
        0,
    )


def _ra_fair_price(signal: Dict[str, Any]) -> float:
    return _f(
        signal.get("fair_price")
        or signal.get("fair_odds")
        or signal.get("fairPrice"),
        0,
    )


def _ra_value_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Racing Alpha value fields."""
    raw = None
    source = ""

    for key in ("value_flag", "value", "value_vs_fair", "edge", "value_edge"):
        if key in signal and signal.get(key) is not None:
            raw = signal.get(key)
            source = key
            break

    if raw is None:
        return {"flag": "", "percent": None, "raw": None, "source": source}

    if isinstance(raw, bool):
        return {
            "flag": "YES" if raw else "NO",
            "percent": None,
            "raw": raw,
            "source": source,
        }

    if isinstance(raw, str):
        s = raw.strip()
        lower = s.lower()
        if lower in {"yes", "true", "value", "positive"}:
            return {"flag": "YES", "percent": None, "raw": raw, "source": source}
        if lower in {"no", "false", "negative", "none"}:
            return {"flag": "NO", "percent": None, "raw": raw, "source": source}
        try:
            if s.endswith("%"):
                return {
                    "flag": "",
                    "percent": float(s[:-1]),
                    "raw": raw,
                    "source": source,
                }
            raw = float(s)
        except Exception:
            return {"flag": s, "percent": None, "raw": raw, "source": source}

    if isinstance(raw, (int, float)):
        val = float(raw)
        pct = val * 100.0 if -1.0 <= val <= 1.0 else val
        return {
            "flag": "",
            "percent": round(pct, 2),
            "raw": raw,
            "source": source,
        }

    return {"flag": "", "percent": None, "raw": raw, "source": source}


def _ra_draw_info(
    signal: Dict[str, Any],
    raw_runner: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Keep the runner draw number separate from a draw-bias verdict."""
    raw_runner = raw_runner or {}

    draw_number = (
        raw_runner.get("draw")
        or raw_runner.get("stall")
        or signal.get("draw_number")
        or signal.get("stall")
        or signal.get("draw")
    )

    bias_raw = (
        signal.get("draw_bias")
        or signal.get("draw_verdict")
        or signal.get("draw_bias_verdict")
        or signal.get("bias")
    )

    verdict = ""
    if isinstance(bias_raw, dict):
        verdict = str(
            bias_raw.get("verdict")
            or bias_raw.get("grade")
            or bias_raw.get("label")
            or bias_raw.get("assessment")
            or ""
        ).strip()
    elif bias_raw is not None and not isinstance(bias_raw, (int, float)):
        verdict = str(bias_raw).strip()

    return {
        "draw_number": draw_number if draw_number not in ("", None) else "TBD",
        "draw_bias": verdict,
    }


def _ra_probability_from_fair_price(fair_price: float) -> float:
    if fair_price > 1:
        return min(max(1.0 / fair_price, 0.001), 0.999)
    return 0.0


async def _analyse_uk_racecards(
    racecards: List[Dict[str, Any]],
    bankroll: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    UK research stack:
      The Racing API Free -> raw card structure
      Racing Alpha       -> derived signals/fair prices/model scores
      Gemini             -> synthesis/explanation, when available

    Live bookmaker value/stake calculations are only produced when actual
    bookmaker odds are available. Racing Alpha fair prices are kept separate
    from bookmaker odds.
    """
    structured: Dict[str, Any] = {}
    candidates: List[Dict[str, Any]] = []
    research_picks: List[Dict[str, Any]] = []
    meeting_race_counts: Dict[str, int] = {}

    try:
        ra_today = await _fetch_racing_alpha_today()
        ra_index = _ra_today_index(ra_today)
    except Exception as exc:
        logger.warning("Racing Alpha /today unavailable: %s", exc)
        ra_index = {}

    for race in racecards:
        if not isinstance(race, dict) or race.get("is_abandoned"):
            continue

        track = str(race.get("course") or "UK").strip()
        meeting_race_counts[track] = meeting_race_counts.get(track, 0) + 1
        race_no = int(_f(race.get("race_number"), 0)) or meeting_race_counts[track]

        runners = [r for r in (race.get("runners") or []) if isinstance(r, dict)]
        if not runners:
            continue

        race_id = str(
            race.get("race_id")
            or race.get("raceId")
            or race.get("id")
            or ""
        ).strip()

        ra_summary = ra_index.get(race_id, {})
        try:
            ra_detail = await _fetch_racing_alpha_race(race_id) if race_id else {}
        except Exception as exc:
            logger.warning("Racing Alpha race %s unavailable: %s", race_id, exc)
            ra_detail = {}

        ra_payload = {
            "today": ra_summary,
            "race": ra_detail,
        }
        ra_signals = _find_nested_runner_signals(ra_payload)
        ra_by_name = {
            _ra_runner_name(s).lower(): s
            for s in ra_signals
            if _ra_runner_name(s)
        }

        # Gemini receives Racing Alpha signals as evidence, but a Gemini outage
        # no longer prevents Racing Alpha-only research output.
        ai = await _gemini_rank_uk_race(race, ra_payload)

        raw_by_name = {
            str(r.get("horse") or r.get("name") or "").strip().lower(): r
            for r in runners
        }
        ai_by_name = {
            str(s.get("horse") or "").strip().lower(): s
            for s in (ai.get("selections", []) if isinstance(ai, dict) else [])
            if isinstance(s, dict) and s.get("horse")
        }

        value_bets = []
        research_for_race = []

        # Build the research slate primarily from Racing Alpha signals.
        names = set(ra_by_name) | set(ai_by_name)

        # If neither layer returned anything, do not invent selections.
        for name in names:
            actual = raw_by_name.get(name)
            if not actual:
                continue

            display_name = str(
                actual.get("horse")
                or actual.get("name")
                or _ra_runner_name(ra_by_name.get(name, {}))
                or name
            ).strip()

            ra_signal = ra_by_name.get(name, {})
            ai_signal = ai_by_name.get(name, {})

            score = _ra_score(ra_signal)
            fair_price = _ra_fair_price(ra_signal)
            ra_prob = _ra_probability_from_fair_price(fair_price)

            ai_prob = _f(ai_signal.get("estimated_probability"), 0)
            if ai_prob > 1:
                ai_prob /= 100.0

            # Use the fair-price implied probability when available because it
            # is a published derived signal. Otherwise use Gemini's estimate.
            prob = ra_prob if ra_prob > 0 else ai_prob
            if not (0 < prob < 1):
                continue

            horse_number = (
                actual.get("number")
                or actual.get("horse_number")
                or actual.get("saddle")
                or actual.get("cloth_number")
                or "TBD"
            )

            bookmaker_odds = _uk_runner_odds(actual)
            value_signal = _ra_value_signal(ra_signal)
            draw_info = _ra_draw_info(ra_signal, actual)

            if score >= 80 or prob >= 0.40:
                confidence = "HIGH"
            elif score >= 65 or prob >= 0.28:
                confidence = "MEDIUM"
            else:
                confidence = "WATCH"

            reasoning_parts = []
            if score:
                reasoning_parts.append(f"Racing Alpha score {score:.0f}/100")
            if fair_price > 1:
                reasoning_parts.append(f"fair price {fair_price:.2f}")
            if value_signal.get("flag"):
                reasoning_parts.append(f"value flag {value_signal['flag']}")
            if value_signal.get("percent") is not None:
                reasoning_parts.append(
                    f"value signal {value_signal['percent']:+.1f}%"
                )
            if draw_info.get("draw_number") not in ("TBD", None, ""):
                reasoning_parts.append(f"draw {draw_info['draw_number']}")
            if draw_info.get("draw_bias"):
                reasoning_parts.append(f"draw bias {draw_info['draw_bias']}")
            if ai_signal.get("reasoning"):
                reasoning_parts.append(str(ai_signal["reasoning"]).strip())

            rp = {
                "region": "GB",
                "track": track,
                "race_number": race_no,
                "race_id": race_id,
                "race_time": race.get("off_time") or race.get("off_dt") or "TBD",
                "horse_number": horse_number,
                "horse": display_name,
                "probability": round(prob, 4),
                "confidence": confidence,
                "reasoning": "; ".join(reasoning_parts)[:700],
                "odds": round(bookmaker_odds, 2) if bookmaker_odds > 1 else None,
                "racing_alpha_score": round(score, 2) if score else None,
                "fair_price": round(fair_price, 2) if fair_price > 1 else None,
                "racing_alpha_value_flag": value_signal.get("flag") or None,
                "racing_alpha_value_percent": value_signal.get("percent"),
                "draw_number": draw_info.get("draw_number"),
                "draw_bias": draw_info.get("draw_bias") or None,
            }

            research_picks.append(rp)
            research_for_race.append(rp)

            # Only compute a true market edge/stake if bookmaker odds exist.
            if bookmaker_odds > 1:
                implied = 1.0 / bookmaker_odds
                edge = (prob - implied) * 100
                value_bets.append({
                    "horse": display_name,
                    "odds_decimal": bookmaker_odds,
                    "estimated_probability": prob,
                    "edge_percent": edge,
                    "reasoning": rp["reasoning"],
                })

        race_obj = {
            "track": track,
            "race_number": race_no,
            "race_id": race_id,
            "race_time": race.get("off_time") or race.get("off_dt") or "TBD",
            "race_name": race.get("race_name"),
            "distance": race.get("distance"),
            "condition": race.get("going"),
            "surface": race.get("surface"),
            "race_class": race.get("race_class"),
            "research_picks": research_for_race,
            "value_bets": value_bets,
            "racing_alpha_available": bool(ra_detail or ra_summary),
            "attribution": "Signals by Racing Alpha",
        }

        structured.setdefault(track, []).append(race_obj)

        for vb in value_bets:
            c = _normalize_candidate("GB", track, race_obj, vb, bankroll)
            if c:
                candidates.append(c)

    candidates.sort(
        key=lambda c: (
            c["confidence"] == "HIGH",
            c["probability"],
            c["edge_percent"],
        ),
        reverse=True,
    )

    research_picks.sort(
        key=lambda c: (
            c.get("confidence") == "HIGH",
            _f(c.get("racing_alpha_score"), 0),
            c.get("probability", 0),
        ),
        reverse=True,
    )

    return structured, candidates, research_picks


def _uk_research_rank_score(c: Dict[str, Any]) -> float:
    """Research ranking score only; not a guaranteed win probability."""
    score = 0.0
    alpha = _f(c.get("racing_alpha_score"), 0)
    prob = _f(c.get("probability"), 0)
    value_pct = c.get("racing_alpha_value_percent")
    value_pct = _f(value_pct, 0) if value_pct is not None else 0

    score += alpha * 0.55
    score += min(max(prob * 100.0, 0), 100) * 0.35
    score += max(min(value_pct, 20), -20) * 0.50

    if c.get("confidence") == "HIGH":
        score += 8
    elif c.get("confidence") == "MEDIUM":
        score += 4

    if str(c.get("racing_alpha_value_flag") or "").upper() == "YES":
        score += 5

    bias = str(c.get("draw_bias") or "").lower()
    if any(word in bias for word in ("positive", "fav", "advantage", "good")):
        score += 2

    return round(score, 2)


def _rank_uk_research_picks(
    picks: List[Dict[str, Any]],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    ranked = [dict(p) for p in picks]

    for p in ranked:
        p["research_rank_score"] = _uk_research_rank_score(p)

    ranked.sort(
        key=lambda p: (
            p.get("research_rank_score", 0),
            _f(p.get("racing_alpha_score"), 0),
            _f(p.get("probability"), 0),
        ),
        reverse=True,
    )

    result = []
    seen_races = set()

    for p in ranked:
        key = (
            p.get("region"),
            str(p.get("track") or "").lower(),
            int(_f(p.get("race_number"), 0)),
        )
        if key in seen_races:
            continue

        result.append(p)
        seen_races.add(key)

        if len(result) >= limit:
            break

    return result


def _format_uk_research_pick(c: Dict[str, Any], idx: int) -> str:
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")

    lines = [
        f"{medal} {c['track']} R{c['race_number']} — #{c['horse_number']} {c['horse']}",
        f"⏰ Time: {c['race_time']}",
    ]

    if c.get("racing_alpha_score") is not None:
        lines.append(f"🧠 AI score: {c['racing_alpha_score']:.0f}/100")

    if c.get("fair_price"):
        lines.append(f"📐 Fair price: {c['fair_price']:.2f}")

    lines.append(f"📊 Research probability: {c['probability']*100:.1f}%")
    lines.append(f"🎯 Confidence: {c['confidence']}")

    if c.get("racing_alpha_value_flag"):
        lines.append(f"💎 Racing Alpha value flag: {c['racing_alpha_value_flag']}")

    if c.get("racing_alpha_value_percent") is not None:
        lines.append(f"💎 Value signal: {c['racing_alpha_value_percent']:+.1f}%")

    if c.get("draw_number") not in (None, "", "TBD"):
        lines.append(f"🎟️ Draw: {c['draw_number']}")

    if c.get("draw_bias"):
        lines.append(f"📍 Draw bias: {c['draw_bias']}")

    if c.get("odds"):
        lines.append(f"💰 Bookmaker odds: {c['odds']:.2f}")
    else:
        lines.append("💰 Bookmaker odds: unavailable on current Racing API plan")

    if c.get("reasoning"):
        lines.append(f"📝 {c['reasoning']}")

    return "\n".join(lines)


def _load_uk_research_from_file() -> List[Dict[str, Any]]:
    try:
        data = json.loads(UK_LATEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    picks: List[Dict[str, Any]] = []
    for races in data.values():
        if not isinstance(races, list):
            continue
        for race in races:
            if not isinstance(race, dict):
                continue
            for p in race.get("research_picks", []) or []:
                if isinstance(p, dict):
                    picks.append(p)

    return picks


# ---------------------------------------------------------------------
# Telegram / API
# ---------------------------------------------------------------------

@app.function(
    image=image,
    secrets=secrets,
    volumes={"/app/data": data_volume},
    memory=256,
    timeout=3600,
    env={
        "OLLAMA_HOST": os.getenv(
            "OLLAMA_HOST",
            "https://gmpho--strike-tips-ollama-cloud-ollama.modal.run",
        ),
        "TELEGRAM_TWA_URL": "https://strike-tips-hud.pages.dev",
    },
    scaledown_window=60,
    startup_timeout=300,
    min_containers=0,
    max_containers=3,
)
@modal.concurrent(max_inputs=10)
@modal.asgi_app()
def serve_api():
    from core_agent.api_pkg import app as fastapi_app

    from fastapi import Body

    @fastapi_app.post("/telegram-webhook")
    async def telegram_webhook(body: dict = Body(...)):
        import asyncio
        import re
        import telegram
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from core_agent.core.strike_brain import brain

        msg = body.get("message", {})
        text = (msg.get("text", "") or "").strip()
        chat_id = msg.get("chat", {}).get("id")
        if not text or not chat_id:
            return {"ok": True}

        logger.info("Telegram from %s: %.80s", chat_id, text)
        brain.initialize()
        bot = telegram.Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])

        # Access control
        from core_agent.config.settings import NOTIFICATIONS
        from core_agent.core.access_control import is_authorized, authorize

        owner_id = NOTIFICATIONS.telegram_chat_id
        pin = NOTIFICATIONS.access_pin

        if not is_authorized(chat_id, owner_id):
            try:
                if text.startswith("/auth"):
                    from core_agent.core.access_control import pin_locked, record_pin_attempt
                    if pin_locked(chat_id):
                        await bot.send_message(
                            chat_id=chat_id,
                            text="🔒 Too many failed attempts. Try again in 30 minutes.",
                        )
                        return {"ok": True}
                    parts = text.split()
                    if len(parts) == 2 and parts[1] == pin:
                        record_pin_attempt(chat_id, True)
                        authorize(chat_id)
                        await bot.send_message(chat_id=chat_id, text="✅ Access granted.")
                    else:
                        locked = record_pin_attempt(chat_id, False)
                        await bot.send_message(
                            chat_id=chat_id,
                            text="🔒 Invalid PIN." + (" Locked for 30 minutes." if locked else ""),
                        )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="🔒 Restricted Access\n\nSend /auth <PIN> to gain access.",
                    )
            except telegram.error.BadRequest:
                logger.warning("Cannot reach chat_id %s", chat_id)
            return {"ok": True}

        def _send_text_block(title: str, lines: List[str]) -> str:
            return title + "\n\n" + "\n\n".join(lines)

        try:
            if text.startswith("/"):
                cmd = text.split()[0].lower()

                if cmd == "/auth":
                    return {"ok": True}

                if cmd == "/start":
                    welcome = (
                        "🏇 Strike Tips Research Bot\n\n"
                        "🇿🇦 /scan — South Africa\n"
                        "🇬🇧 /scan-uk — United Kingdom\n"
                        "🏅 /uk-top — top UK research picks\n"
                        "🌍 /scan-all — SA + UK\n\n"
                        "🎯 /picks — strongest qualifying picks\n"
                        "💎 /value — all positive-value candidates\n"
                        "💰 /stakes — picks with stake suggestions\n"
                        "⚔️ /threats — value alternatives to favourites\n"
                        "🏆 /acca-safe — 2–3 leg conservative accumulator\n"
                        "🚀 /acca-value — 2–4 leg value accumulator\n"
                        "📈 /report — research/performance report\n"
                        "📊 /status — bankroll status\n"
                        "📉 /chart — 15-day chart\n"
                        "❓ /help — command guide"
                    )
                    kb = [[InlineKeyboardButton(
                        "🚀 Open Intelligence HUD",
                        web_app={"url": NOTIFICATIONS.twa_url},
                    )]]
                    await bot.send_message(
                        chat_id=chat_id,
                        text=welcome,
                        reply_markup=InlineKeyboardMarkup(kb),
                    )
                    return {"ok": True}

                if cmd == "/help":
                    help_text = (
                        "🧠 Strike Tips Commands\n\n"
                        "/scan — scan today's SA racing\n"
                        "/scan-uk — scan today's GB racing\n"
                        "/uk-top — ranked top UK research picks\n"
                        "/scan-all — run both regions\n"
                        "/picks — strongest filtered daily selections\n"
                        "/value — all candidates with edge >= 5%\n"
                        "/stakes — bankroll-aware quarter-Kelly suggestions\n"
                        "/threats — favourite vs strongest value alternative\n"
                        "/acca-safe — strict 2–3 leg accumulator\n"
                        "/acca-value — higher-risk 2–4 leg accumulator\n"
                        "/report — ROI/research summary\n"
                        "/status — bankroll and drawdown\n"
                        "/chart — 15-day performance chart\n"
                        "/clear — clear chat history\n\n"
                        "The bot is allowed to return NO BET or NO ACCUMULATOR "
                        "when the filters are not met."
                    )
                    await bot.send_message(chat_id=chat_id, text=help_text)
                    return {"ok": True}

                if cmd == "/status":
                    if not brain.strike:
                        await bot.send_message(chat_id=chat_id, text="❌ System not initialized")
                        return {"ok": True}
                    s = brain.strike.get_bankroll_status()
                    reply = (
                        "💰 Account Summary\n\n"
                        f"Balance: R{s['current_bankroll']:.2f}\n"
                        f"P&L: R{s['total_profit_loss']:.2f}\n"
                        f"Open Bets: {s['open_bets']}\n"
                        f"Drawdown: {s['drawdown_percent']:.1f}%"
                    )
                    await bot.send_message(chat_id=chat_id, text=reply)
                    return {"ok": True}

                if cmd == "/chart":
                    if not brain.strike:
                        await bot.send_message(chat_id=chat_id, text="❌ System not initialized")
                        return {"ok": True}
                    from core_agent.tools.visualizer import PerformanceVisualizer
                    history = brain.strike.bankroll.get_history_stats(days=15)
                    if not history:
                        await bot.send_message(chat_id=chat_id, text="⚠️ No betting history yet.")
                        return {"ok": True}
                    chart_bytes = await PerformanceVisualizer.generate_bankroll_chart(history)
                    if chart_bytes:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=chart_bytes,
                            caption="📈 Strike Tips — 15 Day Performance",
                        )
                    return {"ok": True}

                if cmd == "/scan":
                    await bot.send_message(
                        chat_id=chat_id,
                        text="🇿🇦 Starting South African racing scan…",
                    )
                    run_scan.spawn(chat_id)
                    return {"ok": True}

                if cmd == "/scan-uk":
                    await bot.send_message(
                        chat_id=chat_id,
                        text="🇬🇧 Starting UK racing scan…",
                    )
                    run_uk_scan.spawn(chat_id)
                    return {"ok": True}

                if cmd == "/scan-all":
                    await bot.send_message(
                        chat_id=chat_id,
                        text="🌍 Starting combined SA + UK scan…",
                    )
                    run_all_scans.spawn(chat_id)
                    return {"ok": True}

                if cmd == "/uk-top":
                    uk_research = _load_uk_research_from_file()
                    ranked = _rank_uk_research_picks(uk_research, limit=5)

                    if not ranked:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                "🇬🇧 No UK research shortlist is saved yet.\n"
                                "Run /scan-uk first."
                            ),
                        )
                        return {"ok": True}

                    blocks = [
                        _format_uk_research_pick(c, i)
                        for i, c in enumerate(ranked, 1)
                    ]

                    msg = (
                        "🇬🇧 UK TOP RESEARCH PICKS\n\n"
                        + "\n\n".join(blocks)
                        + "\n\n⚠️ Fair price is a research signal, not your bookmaker price. "
                          "No stake or true market-value recommendation is made without live odds.\n\n"
                          "Signals by Racing Alpha\n"
                          "18+ · BeGambleAware"
                    )

                    await bot.send_message(
                        chat_id=chat_id,
                        text=msg[:4000],
                    )
                    return {"ok": True}

                # Research commands use the latest saved recommendations.
                if cmd in {
                    "/picks", "/value", "/stakes", "/threats",
                    "/acca-safe", "/acca-value", "/report"
                }:
                    status = brain.strike.get_bankroll_status() if brain.strike else {}
                    bankroll = _f(status.get("current_bankroll"), 1000.0)

                    candidates = _load_latest_recommendations()
                    if not candidates:
                        # Rebuild from today's SA scan if available.
                        scan = _load_sa_scan_file()
                        if scan:
                            candidates = _extract_candidates_from_scan(scan, bankroll, "ZA")
                            _save_recommendations(candidates)

                    if cmd == "/picks":
                        picks = _qualifying_picks(candidates)
                        if not picks:
                            await bot.send_message(
                                chat_id=chat_id,
                                text="🚫 NO BET — no selection meets today's pick thresholds.",
                            )
                            return {"ok": True}
                        await bot.send_message(
                            chat_id=chat_id,
                            text=_send_text_block(
                                "🎯 TODAY'S STRONGEST PICKS",
                                [_format_pick(c, i) for i, c in enumerate(picks[:6], 1)],
                            )[:4000],
                        )
                        return {"ok": True}

                    if cmd == "/value":
                        vals = [c for c in candidates if c["edge_percent"] >= MIN_EDGE]
                        if not vals:
                            await bot.send_message(chat_id=chat_id, text="🚫 No qualifying value candidates.")
                            return {"ok": True}
                        await bot.send_message(
                            chat_id=chat_id,
                            text=_send_text_block(
                                "💎 VALUE CANDIDATES",
                                [_format_pick(c, i) for i, c in enumerate(vals[:10], 1)],
                            )[:4000],
                        )
                        return {"ok": True}

                    if cmd == "/stakes":
                        picks = _qualifying_picks(candidates)
                        if not picks:
                            await bot.send_message(chat_id=chat_id, text="🚫 No stake recommendations today.")
                            return {"ok": True}
                        total = sum(c["stake"] for c in picks)
                        lines = [_format_pick(c, i) for i, c in enumerate(picks[:8], 1)]
                        lines.append(
                            f"Total suggested exposure: R{total:.2f} "
                            f"({(total / bankroll * 100) if bankroll else 0:.1f}% of bankroll)"
                        )
                        await bot.send_message(
                            chat_id=chat_id,
                            text=_send_text_block("💰 STAKE PLAN", lines)[:4000],
                        )
                        return {"ok": True}

                    if cmd == "/threats":
                        favs = _snapshot_favourites()
                        lines = []
                        for c in _qualifying_picks(candidates):
                            key = (str(c["track"]).strip().lower(), c["race_number"])
                            fav = favs.get(key)
                            if fav and fav["horse"].strip().lower() != c["horse"].strip().lower():
                                lines.append(
                                    f"{c['track']} R{c['race_number']}\n"
                                    f"Favourite: {fav['horse']} @ {fav['odds']:.2f}\n"
                                    f"Value threat: {c['horse']} @ {c['odds']:.2f} | "
                                    f"p {c['probability']*100:.1f}% | edge +{c['edge_percent']:.1f}%"
                                )
                        if not lines:
                            lines = [
                                "No clean favourite-threat comparison is available from "
                                "today's market snapshot."
                            ]
                        await bot.send_message(
                            chat_id=chat_id,
                            text=_send_text_block("⚔️ FAVOURITE THREATS", lines[:8])[:4000],
                        )
                        return {"ok": True}

                    if cmd == "/acca-safe":
                        await bot.send_message(
                            chat_id=chat_id,
                            text=_format_acca(_build_accumulator(candidates, "safe", bankroll)),
                        )
                        return {"ok": True}

                    if cmd == "/acca-value":
                        await bot.send_message(
                            chat_id=chat_id,
                            text=_format_acca(_build_accumulator(candidates, "value", bankroll)),
                        )
                        return {"ok": True}

                    if cmd == "/report":
                        picks = _qualifying_picks(candidates)
                        perf = status.get("performance") or {}
                        lines = [
                            f"Bankroll: R{bankroll:.2f}",
                            f"Current P&L: R{_f(status.get('total_profit_loss')):.2f}",
                            f"Drawdown: {_f(status.get('drawdown_percent')):.1f}%",
                            f"Today's candidates: {len(candidates)}",
                            f"Today's qualifying picks: {len(picks)}",
                        ]
                        if isinstance(perf, dict):
                            for k in ("roi", "roi_percent", "win_rate", "strike_rate", "total_bets"):
                                if k in perf:
                                    lines.append(f"{k.replace('_', ' ').title()}: {perf[k]}")
                        lines += [
                            "",
                            "Research controls:",
                            "• Min edge: 5%",
                            "• Quarter-Kelly staking",
                            "• Max 2.5% bankroll per single",
                            "• Max ~10% total daily exposure",
                            "• Accumulators require 2+ qualifying independent races",
                        ]
                        await bot.send_message(
                            chat_id=chat_id,
                            text="📈 RESEARCH REPORT\n\n" + "\n".join(lines),
                        )
                        return {"ok": True}

                if cmd == "/clear":
                    await bot.send_message(chat_id=chat_id, text="🧹 Conversation history cleared.")
                    return {"ok": True}

                await bot.send_message(
                    chat_id=chat_id,
                    text=f"❓ Unknown command: {cmd}\nType /help for valid commands.",
                )
                return {"ok": True}

            # Existing AI chat pipeline for non-command messages.
            from core_agent.bus.events import InboundMessage
            from core_agent.agent.telegram_format import (
                markdown_table_to_pre,
                format_race_card_for_telegram,
                split_for_telegram,
            )

            async def _typing_loop():
                try:
                    while True:
                        await bot.send_chat_action(chat_id=chat_id, action="typing")
                        await asyncio.sleep(4)
                except Exception:
                    pass

            typing_task = asyncio.create_task(_typing_loop())

            inbound = InboundMessage(
                session_key=f"tg:{chat_id}",
                channel="telegram",
                chat_id=str(chat_id),
                content=text,
                user_id=msg.get("from", {}).get("id"),
            )
            bus = request.app.state.bus
            sub = bus.subscribe()
            await bus.publish(inbound)

            reply = ""
            try:
                while True:
                    out = await asyncio.wait_for(sub.get(), timeout=180)
                    if out.channel == "telegram" and str(out.chat_id) == str(chat_id) and out.done:
                        reply = out.content or ""
                        break
            except asyncio.TimeoutError:
                reply = "⏳ I'm still thinking. Try a simpler question or check back later."
            finally:
                typing_task.cancel()
                bus.unsubscribe(sub)

            chunks = split_for_telegram(
                format_race_card_for_telegram(markdown_table_to_pre(reply)),
                max_length=3800,
            )
            for chunk in chunks:
                try:
                    await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
                except Exception:
                    await bot.send_message(chat_id=chat_id, text=reply[:4000])

        except Exception as exc:
            logger.error("Webhook error: %s", exc, exc_info=True)
            try:
                await bot.send_message(chat_id=chat_id, text=f"Error: {exc!s}")
            except Exception:
                pass

        return {"ok": True}

    # Best-effort webhook registration on boot.
    import threading

    def _register_webhook_bg():
        import httpx
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return
        try:
            httpx.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": TELEGRAM_WEBHOOK_URL, "allowed_updates": ["message"]},
                timeout=10,
            )
        except Exception as exc:
            logger.warning("Webhook auto-registration failed: %s", exc)

    threading.Thread(target=_register_webhook_bg, daemon=True).start()
    return fastapi_app


# ---------------------------------------------------------------------
# Webhook registration
# ---------------------------------------------------------------------

@app.function(image=image, secrets=secrets, timeout=30)
def register_webhook():
    import httpx
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={"url": TELEGRAM_WEBHOOK_URL, "allowed_updates": ["message"]},
    )
    info = httpx.get(
        f"https://api.telegram.org/bot{token}/getWebhookInfo"
    ).json()
    print("setWebhook:", r.json())
    print("Webhook info:", info)
    return info


# ---------------------------------------------------------------------
# SA scans
# ---------------------------------------------------------------------

@app.function(
    image=image,
    secrets=secrets,
    volumes={"/app/data": data_volume},
    memory=1024,
    timeout=1800,
    max_containers=1,
    schedule=modal.Cron("0 5 * * *", timezone="Africa/Johannesburg"),
)
def daily_scan():
    import subprocess
    result = subprocess.run(
        ["python3", "core_agent/core/strike_tips.py", "scan"],
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return {"status": "complete"}


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/app/data": data_volume},
    memory=1024,
    timeout=1800,
    max_containers=1,
    schedule=modal.Cron("30 9 * * *", timezone="Africa/Johannesburg"),
)
def value_scan():
    import subprocess
    result = subprocess.run(
        ["python3", "core_agent/core/strike_tips.py", "scan"],
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return {"status": "complete"}


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/app/data": data_volume},
    memory=1024,
    timeout=1800,
    max_containers=1,
)
async def run_scan(chat_id: Optional[int] = None):
    import telegram
    from core_agent.core.strike_brain import brain

    brain.initialize()
    bot = telegram.Bot(token=os.environ["TELEGRAM_BOT_TOKEN"]) if chat_id else None

    async def _progress(track: str, i: int, total: int):
        if bot:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📊 SA scan progress: {i}/{total} — {track.title()} done…",
                )
            except Exception:
                pass

    result = await brain.strike.run_daily_scan(progress_callback=_progress)

    status = brain.strike.get_bankroll_status()
    bankroll = _f(status.get("current_bankroll"), 1000.0)
    scan_data = _load_sa_scan_file()
    candidates = _extract_candidates_from_scan(scan_data, bankroll, "ZA")
    _save_recommendations(candidates)

    if bot:
        picks = _qualifying_picks(candidates)
        msg = (
            "✅ South African Scan Complete\n\n"
            f"Tracks: {result.get('tracks_scanned', 0)}\n"
            f"Value bets found: {result.get('total_value_bets', 0)}\n"
            f"Qualifying research picks: {len(picks)}\n\n"
            "Use /picks, /stakes, /threats, /acca-safe or /acca-value."
        )
        await bot.send_message(chat_id=chat_id, text=msg)

    return {"status": "complete", **result}


# ---------------------------------------------------------------------
# UK scan
# ---------------------------------------------------------------------

@app.function(
    image=image,
    secrets=secrets,
    volumes={"/app/data": data_volume},
    memory=1024,
    timeout=1800,
    max_containers=1,
)
async def run_uk_scan(chat_id: Optional[int] = None):
    import telegram
    from core_agent.core.strike_brain import brain

    brain.initialize()
    bankroll = _f(
        brain.strike.get_bankroll_status().get("current_bankroll"),
        1000.0,
    )
    bot = telegram.Bot(
        token=os.environ["TELEGRAM_BOT_TOKEN"]
    ) if chat_id else None

    try:
        racecards = await _fetch_uk_racecards()
        structured, uk_candidates, research_picks = await _analyse_uk_racecards(
            racecards,
            bankroll,
        )

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UK_LATEST_FILE.write_text(
            json.dumps(structured, indent=2),
            encoding="utf-8",
        )

        # Only selections with actual bookmaker odds enter the value/staking DB.
        existing = _load_latest_recommendations()
        merged = [
            c for c in existing
            if c.get("region") != "GB"
        ] + uk_candidates
        merged.sort(
            key=lambda c: (
                c["confidence"] == "HIGH",
                c["probability"],
                c["edge_percent"],
            ),
            reverse=True,
        )
        _save_recommendations(merged)

        tracks = len(structured)
        picks = _qualifying_picks(uk_candidates)

        if bot:
            if picks:
                preview = "\n\n".join(
                    _format_pick(c, i)
                    for i, c in enumerate(picks[:5], 1)
                )
                msg = (
                    "✅ UK VALUE SCAN COMPLETE\n\n"
                    f"Meetings: {tracks}\n"
                    f"Value-qualified picks: {len(picks)}\n\n"
                    f"{preview}\n\n"
                    "Signals by Racing Alpha\n"
                    "18+ · BeGambleAware"
                )

            elif research_picks:
                ranked = _rank_uk_research_picks(
                    research_picks,
                    limit=5,
                )

                blocks = [
                    _format_uk_research_pick(c, i)
                    for i, c in enumerate(ranked, 1)
                ]

                msg = (
                    "🇬🇧 UK TOP RESEARCH PICKS\n\n"
                    f"Meetings scanned: {tracks}\n"
                    f"Research selections analysed: {len(research_picks)}\n"
                    f"Top shortlist: {len(ranked)}\n\n"
                    + "\n\n".join(blocks)
                    + "\n\n⚠️ Fair price is a research signal, not your bookmaker price. "
                      "Stake/value calculations are withheld unless live bookmaker odds are available.\n\n"
                      "Signals by Racing Alpha\n"
                      "18+ · BeGambleAware"
                )

            else:
                msg = (
                    "✅ UK Scan Complete\n\n"
                    f"Meetings: {tracks}\n"
                    "No evidence-backed UK research selections were returned.\n\n"
                    "Signals by Racing Alpha\n"
                    "18+ · BeGambleAware"
                )

            await bot.send_message(
                chat_id=chat_id,
                text=msg[:4000],
            )

        return {
            "status": "complete",
            "region": "GB",
            "meetings": tracks,
            "research_picks": len(research_picks),
            "value_candidates": len(uk_candidates),
            "qualifying_value_picks": len(picks),
        }

    except Exception as exc:
        logger.error(
            "UK scan failed: %s",
            exc,
            exc_info=True,
        )
        if bot:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ UK scan failed:\n{exc}",
            )
        return {
            "status": "error",
            "error": str(exc),
        }


# ---------------------------------------------------------------------
# Combined scan
# ---------------------------------------------------------------------

@app.function(
    image=image,
    secrets=secrets,
    volumes={"/app/data": data_volume},
    memory=1024,
    timeout=3600,
    max_containers=1,
)
async def run_all_scans(chat_id: Optional[int] = None):
    # Fire both on separate containers; each reports to Telegram.
    run_scan.spawn(chat_id)
    run_uk_scan.spawn(chat_id)
    return {"status": "started", "regions": ["ZA", "GB"]}


# ---------------------------------------------------------------------
# Existing odds monitor / keep warm
# ---------------------------------------------------------------------

@app.function(
    image=image,
    secrets=[modal.Secret.from_name("cloudflare-mcp")] + secrets,
    volumes={"/app/data": data_volume},
    memory=1024,
    timeout=900,
    max_containers=1,
    scaledown_window=60,
    schedule=modal.Cron("*/5 * * * *", timezone="Africa/Johannesburg"),
    env={
        "OLLAMA_HOST": os.getenv(
            "OLLAMA_HOST",
            "https://gmpho--strike-tips-ollama-cloud-ollama.modal.run",
        )
    },
)
async def run_odds_monitor():
    from core_agent.core.adaptive_odds_monitor import AdaptiveOddsMonitor
    monitor = AdaptiveOddsMonitor()
    await monitor.initialize()
    await monitor.run_single_cycle()
    logger.info("Odds monitor single cycle complete")


@app.function(image=image, secrets=secrets, timeout=30)
def keep_warm():
    import httpx
    try:
        r = httpx.get(f"{BASE_URL}/api/health", timeout=10)
        return {"status": "pinged", "http": r.status_code}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
