"""Crypto-identity-gated V3.4 short replay. All historical panels are diagnostic.

Do not relabel the R8-observed July–September interval as an untouched holdout.
No live activation, parameter search, or changes to V3.4 signal rules.
"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from historical_feature_validator_v1 import feature_core
from historical_futures_backfill_v2 import month_start, next_month, read_archive
from research_support_swing_v4 import HOUR, four_hour
from research_matched_audit_v7 import select_and_label
from research_target_first_v6 import metrics

STOP = 6.0
IDENTITY = Path('research_crypto_identity_v9.json')


def verified_crypto(registry, symbol):
    record = registry['contracts'].get(symbol, {})
    return record.get('classification') == 'CRYPTO' and bool(record.get('sources'))


def candidates(data):
    """Same timestamp windows, state transitions, liquidity and BTC veto as R7."""
    btc = data['BTCUSDT']; bt = btc['close_time_ms']; rows = []
    for symbol, a in data.items():
        h4 = four_hour(a); times = h4['close_time_ms']; previous = None
        one = [{**{k:float(r[k]) for k in ('close','high','low','quote_volume')},
                'close_time_ms':int(r['close_time_ms'])} for r in a]
        four = [{'close':float(r['close']), 'close_time_ms':int(r['close_time_ms'])} for r in h4]
        for i in range(240, len(a)):
            ts = int(a[i]['open_time_ms']); h = int(np.searchsorted(times, ts)); hist = a[i-30:i]
            if h<55 or ts-int(hist[-1]['close_time_ms'])!=1 or ts-int(times[h-1])>4*HOUR:
                previous = None; continue
            ctx = h4[h-55:h]
            if np.any(np.diff(hist['open_time_ms'])!=HOUR) or np.any(np.diff(ctx['open_time_ms'])!=4*HOUR):
                previous = None; continue
            q = float(hist[-24:]['quote_volume'].sum())
            f = feature_core(one[i-30:i], four[h-55:h], q)
            trigger = f['setup_state']=='CONTINUATION_TRIGGER' and previous!='CONTINUATION_TRIGGER'
            previous = f['setup_state']
            if not trigger or q<10_000_000 or f['bias'] not in ('LONG','SHORT'):
                continue
            b = int(np.searchsorted(bt, ts))
            if b<25 or ts-int(bt[b-1])>HOUR or np.any(np.diff(btc[b-25:b]['open_time_ms'])!=HOUR):
                continue
            move = (float(btc[b-1]['close'])/float(btc[b-25]['close'])-1)*100
            direction = f['bias']
            if (direction=='LONG' and move<-4) or (direction=='SHORT' and move>4):
                continue
            rows.append(dict(symbol=symbol,direction=direction,entry_ms=ts,bar_index=i,
                             quote_volume_24h=q,btc_24h_pct=move))
        print('SCANNED',symbol,len(rows),flush=True)
    return rows


def panel(data, registry, start, cutoff, out, prefix, baseline=False):
    raw = [r for r in candidates(data) if start<=r['entry_ms']<cutoff]
    short = [r for r in raw if r['direction']=='SHORT']
    # Identity filter happens BEFORE selection/occupancy, not on the selected ledger.
    crypto = [r for r in short if verified_crypto(registry,r['symbol'])]
    results = {}
    arms = {'short_only_original_universe':short,'short_only_verified_crypto':crypto}
    if baseline:
        arms['original_mixed_portfolio'] = raw
    for name, events in arms.items():
        rows = select_and_label(events, data, STOP, cutoff)
        results[name] = metrics(rows)
        pd.DataFrame(rows).to_csv(out/f'{prefix}_{name}_trades.csv',index=False)
        if name=='original_mixed_portfolio':
            subgroup = [r for r in rows if r['direction']=='SHORT']
            # Fail closed if the recreated signal logic diverges from the published R7 result.
            if (len(rows),sum(r['win'] for r in rows),len(subgroup),sum(r['win'] for r in subgroup))!=(247,140,140,89):
                raise RuntimeError('R7 baseline parity failed')
            results['original_short_subgroup'] = metrics(subgroup)
    results['candidate_counts'] = dict(all=len(raw),short=len(short),verified_crypto=len(crypto))
    print('PANEL',prefix,json.dumps(results),flush=True)
    return results


def fetch_window(symbol, start, end):
    """Read complete published months and trim; no partial-month daily request storm."""
    cursor = month_start(datetime.fromtimestamp(start/1000,tz=timezone.utc))
    chunks, evidence = [], []
    while int(cursor.timestamp()*1000)<end:
        key = f'data/futures/um/monthly/klines/{symbol}/1h/{symbol}-1h-{cursor:%Y-%m}.zip'
        a, record = read_archive(key); evidence.append(record)
        if a is not None:
            chunks.append(a)
        cursor = next_month(cursor)
    if not chunks:
        return symbol,None,dict(status='NO_HISTORY',archives=evidence)
    a = np.concatenate(chunks); a = a[np.argsort(a['open_time_ms'])]
    a = a[(a['open_time_ms']>=start)&(a['close_time_ms']<end)]
    if len(a)<241:
        return symbol,None,dict(status='SHORT_HISTORY',archives=evidence)
    if np.any(np.diff(a['open_time_ms'])<=0) or np.any(a['close_time_ms']!=a['open_time_ms']+HOUR-1):
        raise ValueError(f'{symbol}: invalid timestamps')
    for k in ('open','high','low','close','quote_volume'):
        if not np.isfinite(a[k]).all() or (a[k]<0).any() or (k!='quote_volume' and (a[k]==0).any()):
            raise ValueError(f'{symbol}: invalid {k}')
    if np.any(a['high']<np.maximum(a['open'],a['close'])) or np.any(a['low']>np.minimum(a['open'],a['close'])):
        raise ValueError(f'{symbol}: invalid OHLC')
    return symbol,a,dict(status='PASS',rows=len(a),archives=evidence)


def main():
    out=Path('_crypto_v34_v9');out.mkdir(exist_ok=True)
    registry=json.loads(IDENTITY.read_text())
    source=Path('_prior_research')
    manifest=json.loads((source/'_futures_raw_v2/manifest.json').read_text())
    old=json.loads((source/'_ml_artifact_v2/swing_model_v2_report.json').read_text())
    start=int(manifest['start_ms']);seen_start=int(old['split']['test_start_ms'])
    cutoff=(seen_start-120*HOUR)//(24*HOUR)*(24*HOUR);end=int(manifest['end_exclusive_ms'])
    if not set(manifest['eligible_symbols']).issubset(registry['contracts']):
        raise RuntimeError('identity registry does not cover original frozen universe')
    classification={s:registry['contracts'][s] for s in manifest['eligible_symbols']}
    (out/'identity_audit.json').write_text(json.dumps(classification,indent=2))
    data={}
    for symbol in manifest['eligible_symbols']:
        p=Path('_support_raw_v4')/f'{symbol}.npy'
        if p.exists():
            a=np.load(p,allow_pickle=False);a=a[(a['open_time_ms']>=start)&(a['close_time_ms']<cutoff)]
            if len(a)>=241:data[symbol]=a
    if len(data)<150 or 'BTCUSDT' not in data:raise RuntimeError('development coverage failure')
    report=dict(strategy='V3.4 candle proxy; short-only; TP5/SL6; no forced deadline',
                identity_sha256=hashlib.sha256(IDENTITY.read_bytes()).hexdigest(),
                telegram_enabled=False,fresh_holdout_evaluations=0,
                universe_counts={c:sum(r['classification']==c for r in classification.values()) for c in ('CRYPTO','NON_CRYPTO','UNVERIFIED')},
                limitations=['Verified subset only; unknown identity exclusions can introduce coverage bias',
                    'Asset identity evidence reviewed retrospectively; not a complete historical security master',
                    'All evaluated dates are development/previously observed; no fresh validation claim',
                    '1h futures live data and exact live parity remain unavailable/unverified',
                    'Modeled funding/fees, no leverage or liquidation simulation'])
    report['development']=panel(data,registry,start+240*HOUR,cutoff,out,'development',baseline=True)
    (out/'report.json').write_text(json.dumps(report,indent=2))
    data={};coverage={};warm=(seen_start-280*HOUR)//(24*HOUR)*(24*HOUR)
    def load(symbol):
        try:return fetch_window(symbol,warm,end)
        except Exception as exc:return symbol,None,dict(status='FAIL',error=str(exc))
    with ThreadPoolExecutor(max_workers=4) as pool:
        for symbol,a,evidence in pool.map(load,manifest['eligible_symbols']):
            coverage[symbol]=evidence
            if a is not None:data[symbol]=a
            print('LOADED',symbol,evidence['status'],flush=True)
    (out/'coverage.json').write_text(json.dumps(coverage,indent=2))
    if len(data)<150 or 'BTCUSDT' not in data:raise RuntimeError('seen-period coverage failure')
    report['previously_seen_period']=panel(data,registry,seen_start,end,out,'seen_period')
    report['fresh_validation_status']='NOT_RUN: no substantial untouched period identified; prospective specification is committed separately'
    (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    print('SUMMARY',json.dumps(report,allow_nan=False),flush=True)


if __name__=='__main__':main()
