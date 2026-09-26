import json
from pathlib import Path

# Swing Ranker v1 - SHADOW ONLY
# Reads new_swing.json and produces a regime-aware ranking.
# No Telegram alerts and no effect on live trading logic.

INPUT_FILE = Path("new_swing.json")
OUTPUT_FILE = Path("swing_rankings.json")


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, float(value)))


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def classify_market_regime(ctx):
    breadth = number(ctx.get("breadth_positive_pct"), 50.0)
    median_move = number(ctx.get("median_24h_change_pct"))
    btc = number(ctx.get("btc_24h_change_pct"))
    eth = number(ctx.get("eth_24h_change_pct"))

    score = 0

    if breadth >= 65:
        score += 2
    elif breadth >= 55:
        score += 1
    elif breadth <= 35:
        score -= 2
    elif breadth <= 45:
        score -= 1

    if median_move >= 1.0:
        score += 2
    elif median_move > 0:
        score += 1
    elif median_move <= -1.0:
        score -= 2
    elif median_move < 0:
        score -= 1

    if btc >= 0.5:
        score += 1
    elif btc <= -0.5:
        score -= 1

    if eth >= 0.5:
        score += 1
    elif eth <= -0.5:
        score -= 1

    if score >= 3:
        regime = "RISK_ON"
    elif score <= -3:
        regime = "RISK_OFF"
    else:
        regime = "MIXED"

    return regime, score


def directional(value, bias):
    value = number(value)
    return value if bias == "LONG" else -value


def score_candidate(row, market_ctx, regime):
    bias = str(row.get("bias") or "NEUTRAL").upper()

    if bias not in {"LONG", "SHORT"}:
        return {
            "swing_quality_score_v1": 0.0,
            "quality_components": {"neutral_or_unaligned": 1},
            "relative_strength_vs_btc_pct": None,
            "relative_strength_vs_market_pct": None,
        }

    sign = 1.0 if bias == "LONG" else -1.0

    m3 = directional(row.get("momentum_3h_pct"), bias)
    m12 = directional(row.get("momentum_12h_pct"), bias)
    m24 = directional(row.get("momentum_24h_pct"), bias)

    coin24 = number(row.get("price_change_24h_pct"))
    btc24 = number(market_ctx.get("btc_24h_change_pct"))
    median24 = number(market_ctx.get("median_24h_change_pct"))

    rs_btc = sign * (coin24 - btc24)
    rs_market = sign * (coin24 - median24)

    state = str(row.get("setup_state") or "TREND_ONLY").upper()
    distance = abs(number(row.get("distance_from_1h_sma10_pct")))
    volume_expansion = number(row.get("volume_expansion_1h"))
    quote_volume = number(row.get("quote_volume_24h"))

    score = 18.0
    components = {"directional_structure": 18.0}

    # Trend persistence: 24h carries the most weight.
    trend_points = 0.0

    if m24 >= 5:
        trend_points += 16
    elif m24 >= 2:
        trend_points += 12
    elif m24 >= 0.5:
        trend_points += 7
    elif m24 >= 0:
        trend_points += 3
    else:
        trend_points -= 10

    if m12 >= 3:
        trend_points += 10
    elif m12 >= 1:
        trend_points += 7
    elif m12 >= 0:
        trend_points += 3
    else:
        trend_points -= 7

    if m3 >= 0.5:
        trend_points += 7
    elif m3 >= 0:
        trend_points += 3
    elif m3 <= -1.5:
        trend_points -= 6
    else:
        trend_points -= 2

    score += trend_points
    components["trend_momentum"] = trend_points

    # Setup state / retest quality.
    if state == "CONTINUATION_TRIGGER":
        state_points = 15
    elif state == "RETEST_READY":
        state_points = 12
    else:
        state_points = 4

    score += state_points
    components["setup_state"] = state_points

    if distance <= 0.75:
        distance_points = 6
    elif distance <= 1.5:
        distance_points = 3
    elif distance <= 2.5:
        distance_points = 0
    else:
        distance_points = -5

    score += distance_points
    components["distance_from_1h_trend"] = distance_points

    # Participation.
    if volume_expansion >= 1.5:
        volume_points = 10
    elif volume_expansion >= 1.0:
        volume_points = 6
    elif volume_expansion >= 0.7:
        volume_points = 2
    elif volume_expansion < 0.5:
        volume_points = -6
    else:
        volume_points = -2

    score += volume_points
    components["volume_expansion"] = volume_points

    # Liquidity.
    if quote_volume >= 100_000_000:
        liquidity_points = 6
    elif quote_volume >= 50_000_000:
        liquidity_points = 4
    elif quote_volume >= 20_000_000:
        liquidity_points = 2
    elif quote_volume < 10_000_000:
        liquidity_points = -4
    else:
        liquidity_points = 0

    score += liquidity_points
    components["liquidity"] = liquidity_points

    # Relative strength / weakness.
    rs_points = 0.0

    if rs_market >= 5:
        rs_points += 8
    elif rs_market >= 2:
        rs_points += 6
    elif rs_market >= 0:
        rs_points += 2
    elif rs_market <= -2:
        rs_points -= 6

    if rs_btc >= 4:
        rs_points += 4
    elif rs_btc >= 1:
        rs_points += 2
    elif rs_btc <= -2:
        rs_points -= 4

    score += rs_points
    components["relative_strength"] = rs_points

    # Market regime adjustment.
    if bias == "LONG":
        regime_points = 7 if regime == "RISK_ON" else (-10 if regime == "RISK_OFF" else 0)
    else:
        regime_points = 7 if regime == "RISK_OFF" else (-10 if regime == "RISK_ON" else 0)

    score += regime_points
    components["market_regime"] = regime_points

    # Anti-chase.
    chase_points = 0.0
    if m12 >= 8:
        chase_points -= 12
    elif m12 >= 6:
        chase_points -= 7

    if bias == "LONG":
        pullback = number(row.get("pullback_from_12h_high_pct"))
        if pullback > -0.5:
            chase_points -= 4
        elif pullback < -7:
            chase_points -= 4
    else:
        bounce = number(row.get("bounce_from_12h_low_pct"))
        if bounce < 0.5:
            chase_points -= 4
        elif bounce > 7:
            chase_points -= 4

    score += chase_points
    components["anti_chase"] = chase_points

    # Warning penalties.
    warning_points = 0.0
    for warning in row.get("warnings") or []:
        text = str(warning).lower()
        if "volume participation is weak" in text:
            warning_points -= 5
        elif "extended" in text:
            warning_points -= 5
        else:
            warning_points -= 2

    score += warning_points
    components["warnings"] = warning_points

    return {
        "swing_quality_score_v1": round(clamp(score), 2),
        "quality_components": components,
        "relative_strength_vs_btc_pct": round(rs_btc, 3),
        "relative_strength_vs_market_pct": round(rs_market, 3),
    }


def main():
    payload = load_json(INPUT_FILE, {})

    if not isinstance(payload, dict):
        raise SystemExit("[Swing Ranker] invalid new_swing.json")

    pool = payload.get("analysis_pool")
    if not isinstance(pool, list):
        raise SystemExit("[Swing Ranker] analysis_pool missing")

    market_ctx = payload.get("market_context")
    if not isinstance(market_ctx, dict):
        market_ctx = {}

    regime, regime_score = classify_market_regime(market_ctx)

    ranked = []

    for row in pool:
        if not isinstance(row, dict):
            continue

        item = {
            "symbol": row.get("symbol"),
            "bias": row.get("bias"),
            "setup_state": row.get("setup_state"),
            "price": row.get("price"),
            "swing_score": row.get("swing_score"),
            "activity_rank": row.get("activity_rank"),
            "momentum_3h_pct": row.get("momentum_3h_pct"),
            "momentum_12h_pct": row.get("momentum_12h_pct"),
            "momentum_24h_pct": row.get("momentum_24h_pct"),
            "volume_expansion_1h": row.get("volume_expansion_1h"),
            "quote_volume_24h": row.get("quote_volume_24h"),
            "price_change_24h_pct": row.get("price_change_24h_pct"),
        }

        item.update(score_candidate(row, market_ctx, regime))
        ranked.append(item)

    directional_rankings = [
        x for x in ranked if str(x.get("bias") or "").upper() in {"LONG", "SHORT"}
    ]

    directional_rankings.sort(
        key=lambda x: (
            x.get("swing_quality_score_v1", 0),
            -(x.get("activity_rank") or 9999),
        ),
        reverse=True,
    )

    for index, row in enumerate(directional_rankings, start=1):
        row["swing_quality_rank_v1"] = index

    output = {
        "version": "swing-ranker-v1",
        "generated_at_ms": payload.get("generated_at_ms"),
        "scanner_version": payload.get("version"),
        "strategy": payload.get("strategy"),
        "market_regime_v1": regime,
        "market_regime_score_v1": regime_score,
        "market_context": market_ctx,
        "analysis_pool_count": len(pool),
        "directional_ranked_count": len(directional_rankings),
        "rankings": directional_rankings,
    }

    OUTPUT_FILE.write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n"
    )

    top = directional_rankings[:5]
    print(
        f"[Swing Ranker] regime={regime} score={regime_score}; "
        f"ranked={len(directional_rankings)}; "
        f"top={[(x['symbol'], x['swing_quality_score_v1']) for x in top]}"
    )


if __name__ == "__main__":
    main()
