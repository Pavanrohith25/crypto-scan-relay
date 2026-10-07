"""Prospective spot-proxy learning only. No orders, alerts, or promotion."""
import hashlib
import json
import math
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

BAR = 300_000
FEATURES = ('momentum_3h_pct', 'momentum_12h_pct', 'momentum_24h_pct',
            'distance_from_1h_sma10_pct', 'volume_expansion_1h', 'swing_score')
SCALES = (10, 20, 30, 5, 3, 100)
VERSION = 'spot-paper-online-v1'


def vector(snapshot):
    values = []
    for key, scale in zip(FEATURES, SCALES):
        value = snapshot.get(key)
        if not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value):
            return None
        values.append(max(-3, min(3, value / scale)))
    return [1.0] + values


def initial_state(now):
    return {'version': VERSION, 'started_ms': now, 'trades': {},
            'models': {side: {'n': 0, 'weights': [0.] * 7} for side in ('LONG', 'SHORT')}}


def predict(model, x):
    z = max(-30, min(30, sum(a*b for a, b in zip(model['weights'], x))))
    return 1 / (1 + math.exp(-z))


def learn(model, x, label):
    error = label - predict(model, x)
    rate = .05 / math.sqrt(1 + model['n'] / 100)
    model['weights'] = [w + rate * (error * a - (.001*w if i else 0))
                        for i, (w, a) in enumerate(zip(model['weights'], x))]
    model['n'] += 1


def admit(state, events, registry, now):
    records = []
    for event in sorted(events, key=lambda e: e.get('timestamp_ms', 0)):
        if event.get('event') != 'SWING_CONTINUATION_TRIGGER':
            continue
        ts = event.get('timestamp_ms', 0)
        symbol, side = event.get('symbol'), event.get('bias')
        if ts < state['started_ms'] or not 0 <= now-ts <= 600_000:
            continue
        if registry.get(symbol, {}).get('classification') != 'CRYPTO' or side not in state['models']:
            continue
        tid = str(event.get('trigger_id') or f'{symbol}-{ts}')
        if tid in state['trades']:
            continue
        previous = [t for t in state['trades'].values() if t['symbol'] == symbol]
        if any(t['status'] in ('PENDING', 'OPEN') or now-t['predicted_ms'] < 86_400_000 for t in previous):
            continue
        x = vector(event.get('snapshot') or {})
        if x is None:
            continue
        model = state['models'][side]
        trade = {'id': tid, 'symbol': symbol, 'side': side, 'features': x,
                 'snapshot': event['snapshot'], 'signal_ms': ts, 'predicted_ms': now,
                 'entry_ms': (now // BAR + 1) * BAR, 'next_bar_ms': (now // BAR + 1) * BAR,
                 'model_n': model['n'], 'model_hash': hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest(),
                 'score': predict(model, x) if model['n'] >= 30 else None,
                 'status': 'PENDING', 'mae_pct': 0., 'mfe_pct': 0., 'checked_ms': 0,
                 'target_pct': 5., 'stop_pct': 6., 'source': 'BINANCE_SPOT_PROXY'}
        state['trades'][tid] = trade
        records.append({'event': 'PREDICTION', **trade})
    return records


def advance(trade, bars, now):
    """Require every closed bar; never cross a missing interval or use forming bars."""
    if not bars and trade['next_bar_ms'] + BAR <= now:
        trade['data_error'] = 'NO_CLOSED_BARS'
    for bar in bars:
        ts = int(bar[0])
        if ts < trade['next_bar_ms']:
            continue
        if ts != trade['next_bar_ms']:
            trade['data_error'] = 'MISSING_BAR'
            break
        if ts + BAR > now:
            break
        op, high, low, close = map(float, (bar[1], bar[2], bar[3], bar[4]))
        if not all(math.isfinite(v) and v > 0 for v in (op, high, low, close)) or not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError('Invalid OHLC')
        if trade['status'] == 'PENDING':
            trade.update(entry_price=op, status='OPEN')
        entry = trade['entry_price']
        sign = 1 if trade['side'] == 'LONG' else -1
        opening = sign * (op / entry - 1) * 100
        favorable = ((high/entry-1) if sign == 1 else (1-low/entry))*100
        adverse = ((1-low/entry) if sign == 1 else (high/entry-1))*100
        trade['mae_pct'] = max(trade['mae_pct'], adverse)
        trade['mfe_pct'] = max(trade['mfe_pct'], favorable)
        trade['mark_return_pct'] = sign * (close/entry-1)*100
        trade['next_bar_ms'] = ts + BAR
        trade.pop('data_error', None)
        # Opening gaps establish ordering before the rest of the candle.
        hit_target, hit_stop = favorable >= 5-1e-9, adverse >= 6-1e-9
        if opening <= -6 or opening >= 5 or hit_target or hit_stop:
            ambiguous = opening > -6 and opening < 5 and hit_target and hit_stop
            won = opening >= 5 or (opening > -6 and hit_target and not hit_stop)
            gross = min(-6., opening) if opening <= -6 else (5. if won else -6.)
            hours = (ts + BAR-trade['entry_ms']) / 3_600_000
            trade.update(status='TARGET' if won else ('AMBIGUOUS_LOSS' if ambiguous else 'STOP'),
                         resolved_bar_ms=ts, label_available_ms=now, holding_hours=hours,
                         net_return_pct=gross-.12-.01*math.ceil(hours/8),
                         stressed_net_return_pct=gross-.50-.01*math.ceil(hours/8))
            return True
    return False


def fetch_bars(symbol, start, now):
    query = urlencode({'symbol': symbol, 'interval': '5m', 'startTime': start,
                       'endTime': now-1, 'limit': 1000})
    with urlopen('https://data-api.binance.vision/api/v3/klines?' + query, timeout=15) as r:
        data = json.load(r)
    if not isinstance(data, list):
        raise ValueError('Non-list candle response')
    return data


def report(state, now):
    panels = {}
    for side in ('LONG', 'SHORT'):
        trades = [t for t in state['trades'].values() if t['side'] == side]
        closed = [t for t in trades if t['status'] not in ('PENDING', 'OPEN')]
        scored = [t for t in closed if t['score'] is not None]
        selected = [t for t in scored if t['score'] >= .6]
        panels[side] = {'entries': len(trades), 'resolved': len(closed),
            'open_or_pending': len(trades)-len(closed),
            'wins': sum(t['status'] == 'TARGET' for t in closed),
            'model_updates': state['models'][side]['n'],
            'scored_resolved': len(scored),
            'prequential_brier': sum((t['score']-(t['status']=='TARGET'))**2 for t in scored)/len(scored) if scored else None,
            'fixed_half_brier_baseline': .25 if scored else None,
            'mean_net_pct': sum(t['net_return_pct'] for t in closed)/len(closed) if closed else None,
            'selected_entries': sum(t['score'] is not None and t['score'] >= .6 for t in trades),
            'selected_resolved': len(selected),
            'selected_mean_stressed_pct': sum(t['stressed_net_return_pct'] for t in selected)/len(selected) if selected else None,
            'data_errors': sum('data_error' in t for t in trades)}
    return {'version': VERSION, 'as_of_ms': now, 'started_ms': state['started_ms'],
            'mode': 'PAPER_ONLY', 'promotion': 'BLOCKED', 'panels': panels,
            'limitations': ['Spot proxy; shorts are hypothetical, not futures execution.',
                'Scores are uncalibrated; first 30 resolved trades per side are warmup.',
                'Costs are assumptions, not observed funding or borrow costs.',
                'MAE/MFE include full exit candle; intrabar ordering is unknown.',
                'Correlated trades, incomplete crypto universe, no portfolio/leverage simulation.',
                'No live strategy promotion or Telegram integration.']}


def main():
    now = int(time.time()*1000)
    root = Path('paper_learning')
    root.mkdir(exist_ok=True)
    path = root/'state.json'
    state = json.loads(path.read_text()) if path.exists() else initial_state(now)
    if state['version'] != VERSION:
        raise ValueError('Explicit migration required')
    registry = json.loads(Path('paper_learning_identity.json').read_text())['contracts']
    records = []
    # Update only from closed outcomes observed before admitting today's predictions.
    pending = sorted((t for t in state['trades'].values() if t['status'] in ('PENDING', 'OPEN')),
                     key=lambda t: (t['checked_ms'], t['entry_ms'], t['id']))
    for trade in pending[:12]:
        if trade['next_bar_ms'] + BAR > now:
            continue
        trade['checked_ms'] = now
        try:
            if advance(trade, fetch_bars(trade['symbol'], trade['next_bar_ms'], now), now):
                learn(state['models'][trade['side']], trade['features'], int(trade['status']=='TARGET'))
                records.append({'event': 'OUTCOME', **trade})
        except Exception as exc:
            trade['data_error'] = type(exc).__name__ + ': ' + str(exc)[:120]
    events = [json.loads(line) for line in Path('swing_events.jsonl').read_text().splitlines() if line.strip()]
    # Use actual post-fetch time so the planned entry cannot precede prediction creation.
    now = int(time.time()*1000)
    records.extend(admit(state, events, registry, now))
    with (root/'journal.jsonl').open('a') as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True, allow_nan=False)+'\n')
    path.write_text(json.dumps(state, indent=2, sort_keys=True, allow_nan=False)+'\n')
    summary = report(state, now)
    (root/'report.json').write_text(json.dumps(summary, indent=2, sort_keys=True)+'\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
