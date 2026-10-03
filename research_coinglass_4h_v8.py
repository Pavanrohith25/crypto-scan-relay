"""Frozen 4h-only short benchmark for the confirmed CoinGlass entitlement.

R6 breakout eligibility, fixed 5% TP / 6% SL, no deadline, short-only portfolio.
Archive candles are a reference benchmark, NOT CoinGlass/live parity evidence.
No parameter selection and no automatic Telegram activation.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from research_support_swing_v4 import HOUR, four_hour, fetch_hourly
from research_target_first_v6 import breakout, metrics

BAR = 4 * HOUR
STOP = 6.0


def label(a, i, cutoff):
    entry = float(a[i]['open'])
    bars = a[i:]
    gaps = np.flatnonzero(np.diff(bars['open_time_ms']) != BAR)
    if len(gaps):
        bars = bars[:int(gaps[0]) + 1]
    tp = bars['low'] <= entry * .95
    sl = bars['high'] >= entry * 1.06
    hits = np.flatnonzero(tp | sl)
    resolved = bool(len(hits))
    j = int(hits[0]) if resolved else len(bars) - 1
    win = bool(resolved and tp[j] and not sl[j])
    gross = 5.0 if win else min(-STOP, (1 - float(bars[j]['open']) / entry) * 100) if resolved else (1 - float(bars[j]['close']) / entry) * 100
    hours = (j + 1) * 4
    cost = .12 + .01 * np.ceil(hours / 8)
    path = ('TARGET_FIRST' if win else 'AMBIGUOUS' if tp[j] else 'STOP_FIRST') if resolved else ('DATA_GAP' if len(gaps) else 'DATA_END')
    return dict(entry_ms=int(a[i]['open_time_ms']), entry_price=entry,
                exit_ms=int(bars[j]['close_time_ms']) + 1 if resolved else cutoff,
                resolved=resolved, win=int(win), path=path, stop_pct=STOP,
                holding_hours=hours, gross_return_pct=gross, cost_pct=float(cost),
                net_return_pct=float(gross-cost), net_return_R=float((gross-cost)/STOP))


def evaluate(data, entry_start, cutoff):
    btc = data['BTCUSDT']
    candidates = []
    for symbol, a in data.items():
        for i in range(55, len(a)):
            ts = int(a[i]['open_time_ms'])
            if ts < entry_start or ts >= cutoff:
                continue
            ctx = a[i-55:i]
            if ts - int(ctx[-1]['close_time_ms']) != 1 or np.any(np.diff(ctx['open_time_ms']) != BAR):
                continue
            b = int(np.searchsorted(btc['close_time_ms'], ts))
            if b < 7 or ts-int(btc[b-1]['close_time_ms']) != 1 or np.any(np.diff(btc[b-7:b]['open_time_ms']) != BAR):
                continue
            btc_move = (float(btc[b-1]['close']) / float(btc[b-7]['close']) - 1) * 100
            if btc_move > 4:
                continue
            # For tf=4h, R6 uses history only for 24h quote volume.
            if breakout(ctx[-6:], ctx, 'SHORT', '4h') is None:
                continue
            candidates.append(dict(symbol=symbol, direction='SHORT', entry_ms=ts,
                                   bar_index=i, quote_volume_24h=float(ctx[-6:]['quote_volume'].sum())))
    selected, last, busy_until = [], {}, -1
    for r in sorted(candidates, key=lambda x: (x['entry_ms'], -x['quote_volume_24h'], x['symbol'])):
        ts, symbol = r['entry_ms'], r['symbol']
        if ts < busy_until or ts - last.get(symbol, -10**18) < 24*HOUR:
            continue
        row = {**r, **label(data[symbol], r['bar_index'], cutoff)}
        selected.append(row)
        busy_until, last[symbol] = row['exit_ms'], ts
    return selected


def main():
    out = Path('_coinglass_4h_v8'); out.mkdir(exist_ok=True)
    prior = Path('_prior_research')
    manifest = json.loads((prior/'_futures_raw_v2/manifest.json').read_text())
    old = json.loads((prior/'_ml_artifact_v2/swing_model_v2_report.json').read_text())
    start, end = int(manifest['start_ms']), int(manifest['end_exclusive_ms'])
    test_start = int(old['split']['test_start_ms'])
    dev_end = (test_start-120*HOUR)//(24*HOUR)*(24*HOUR)
    spec = dict(strategy='r6_4h_breakout_short_only', target_pct=5, stop_pct=6,
                forced_time_exit=False, candle_interval='4h', entry='next 4h open',
                maximum_positions=1, per_coin_cooldown_hours=24,
                primary_test_start_ms=test_start, end_exclusive_ms=end,
                promotion='no automatic activation; live provider parity still required',
                gate='>=50 resolved, all-trade hit rate >50%, descriptive Wilson lower >50%, positive stressed resolved mean and all-position MTM mean')
    (out/'frozen_spec.json').write_text(json.dumps(spec, indent=2))
    dev = {}
    for symbol in manifest['eligible_symbols']:
        p = Path('_support_raw_v4')/f'{symbol}.npy'
        if p.exists():
            a = np.load(p, allow_pickle=False)
            dev[symbol] = four_hour(a[(a['open_time_ms']>=start)&(a['close_time_ms']<dev_end)])
    if len(dev)<150 or 'BTCUSDT' not in dev:
        raise RuntimeError('inadequate development coverage')
    dev_rows = evaluate(dev, start+55*BAR, dev_end)
    pd.DataFrame(dev_rows).to_csv(out/'development_trades.csv', index=False)
    print('DEVELOPMENT', json.dumps(metrics(dev_rows)), flush=True)
    # Same frozen universe; use warmup only for features, never count warmup entries.
    warm_start = (test_start-60*BAR)//(24*HOUR)*(24*HOUR)
    lo = datetime.fromtimestamp(warm_start/1000, tz=timezone.utc)
    hi = datetime.fromtimestamp(end/1000, tz=timezone.utc)
    data, coverage = {}, {}
    def load(symbol):
        try:
            return fetch_hourly(symbol, lo, hi, Path('_holdout_4h_v8_raw'))
        except Exception as exc:
            return symbol, None, dict(status='FAIL', error=str(exc))
    with ThreadPoolExecutor(max_workers=4) as pool:
        for symbol, a, evidence in pool.map(load, manifest['eligible_symbols']):
            coverage[symbol] = evidence
            if a is not None:
                data[symbol] = four_hour(a)
            print('HOLDOUT_LOADED', symbol, evidence['status'], flush=True)
    (out/'coverage.json').write_text(json.dumps(coverage, indent=2))
    if len(data)<150 or 'BTCUSDT' not in data:
        raise RuntimeError('inadequate test coverage')
    rows = evaluate(data, test_start, end)
    result = metrics(rows)
    enough = result.get('resolved', 0) >= 50
    lower = result.get('wilson_all_descriptive', [0, 1])[0]
    passes = bool(enough and result['target_fraction_all']>.5 and lower>.5
                  and result['resolved_avg_net_stress_pct']>0 and result['all_positions_mtm_avg_net_pct']>0)
    report = dict(spec=spec, development=metrics(dev_rows), holdout=result,
                  test_contracts=len(data), holdout_evaluations=1, evidence_gate_passed=passes,
                  telegram_enabled=False, limitations=[
                      'Historical archives, not CoinGlass candles: volume/candle parity remains unverified',
                      'Four-hour TP/SL ambiguity is a loss; gap-censored positions block slot to end',
                      'Fees/slippage/funding are assumptions; no leverage/liquidation simulation',
                      'Wilson interval is descriptive and does not account for market dependence',
                      'This test period is consumed: subsequent tuning needs new evaluation data'])
    pd.DataFrame(rows).to_csv(out/'holdout_trades.csv', index=False)
    (out/'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    print('SUMMARY', json.dumps(report, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
