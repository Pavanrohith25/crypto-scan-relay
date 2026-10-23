"""Frozen, matched development comparison; no rule selection or live changes."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from historical_feature_validator_v1 import feature_core
from research_support_swing_v4 import HOUR, four_hour
from research_target_first_v6 import breakout, metrics

STOP_PCTS=(3.,6.)
RECOVERY_HOURS=96


def outcome(a,i,direction,stop_pct,cutoff):
    sign=1 if direction=='LONG' else -1
    entry=float(a[i]['open']);bars=a[i:];gaps=np.flatnonzero(np.diff(bars['open_time_ms'])!=HOUR)
    reason='DATA_GAP' if len(gaps) else 'DATA_END'
    if len(gaps):bars=bars[:int(gaps[0])+1]
    target=entry*(1+sign*.05);stop=entry*(1-sign*stop_pct/100)
    tp=bars['high']>=target if sign==1 else bars['low']<=target
    sl=bars['low']<=stop if sign==1 else bars['high']>=stop
    hits=np.flatnonzero(tp|sl);resolved=bool(len(hits));j=int(hits[0]) if resolved else len(bars)-1
    win=bool(resolved and tp[j] and not sl[j]);path=('TARGET_FIRST' if win else 'AMBIGUOUS' if tp[j] else 'STOP_FIRST') if resolved else reason
    ret=5. if win else min(-stop_pct,sign*(float(bars[j]['open'])/entry-1)*100) if resolved else sign*(float(bars[j]['close'])/entry-1)*100
    cost=.12+.01*np.ceil((j+1)/8)
    adv=sign*((bars[:j+1]['low'] if sign==1 else bars[:j+1]['high'])/entry-1)*100
    recovery='NOT_APPLICABLE';continued=None
    if resolved and not win:
        # Never infer recovery from the same ambiguous stop candle.
        after=bars[j+1:j+1+RECOVERY_HOURS]
        recovery='INSUFFICIENT_FOLLOWUP'
        if len(after)==RECOVERY_HOURS:
            reached=(after['high']>=target) if sign==1 else (after['low']<=target)
            recovery='RECOVERED_TO_ORIGINAL_TARGET' if reached.any() else 'NO_TARGET_WITHIN_96H'
            adverse=sign*((after['low'] if sign==1 else after['high'])/entry-1)*100
            continued=bool(adverse.min()<=-2*stop_pct)
    return {'entry_ms':int(a[i]['open_time_ms']),'entry_price':entry,'stop_price':stop,
            'exit_ms':int(bars[j]['close_time_ms'])+1 if resolved else cutoff,
            'last_observed_ms':int(bars[j]['close_time_ms'])+1,'resolved':resolved,'path':path,'win':int(win),
            'stop_pct':stop_pct,'holding_hours':j+1,'gross_return_pct':ret,'cost_pct':float(cost),
            'net_return_pct':float(ret-cost),'net_return_R':float((ret-cost)/stop_pct),
            'mae_pct':float(min(0,adv.min())),'post_stop_96h':recovery,'continued_to_minus_2R':continued}


def select_and_label(candidates,data,stop_pct,cutoff):
    # Shared tie-break uses observable liquidity, never a strategy-specific score.
    rows=sorted(candidates,key=lambda x:(x['entry_ms'],-x['quote_volume_24h'],x['symbol']))
    active=[];last={};selected=[]
    for r in rows:
        ts=r['entry_ms'];symbol=r['symbol'];direction=r['direction']
        active=[p for p in active if p['exit_ms']>ts]
        if any(p['symbol']==symbol or p['direction']==direction for p in active):continue
        if symbol in last and ts-last[symbol]<24*HOUR:continue
        row={**r,**outcome(data[symbol],r['bar_index'],direction,stop_pct,cutoff)}
        selected.append(row);active.append(row);last[symbol]=ts
    return selected


def loss_audit(rows):
    losses=[r for r in rows if r['resolved'] and not r['win']]
    counts={k:sum(r['post_stop_96h']==k for r in losses) for k in ('RECOVERED_TO_ORIGINAL_TARGET','NO_TARGET_WITHIN_96H','INSUFFICIENT_FOLLOWUP')}
    complete=counts['RECOVERED_TO_ORIGINAL_TARGET']+counts['NO_TARGET_WITHIN_96H']
    return {'stopped_trades':len(losses),**counts,'complete_96h_followup':complete,
            'recovery_fraction_complete':counts['RECOVERED_TO_ORIGINAL_TARGET']/complete if complete else None,
            'continued_to_minus_2R':sum(r['continued_to_minus_2R'] is True for r in losses),
            'note':'Recovery and further adverse excursion can both occur; neither changes a stop loss into a win.'}


def main():
    source=Path('_prior_research');out=Path('_matched_v7');out.mkdir(exist_ok=True)
    old=json.loads((source/'_ml_artifact_v2/swing_model_v2_report.json').read_text())
    manifest=json.loads((source/'_futures_raw_v2/manifest.json').read_text())
    cutoff=(old['split']['test_start_ms']-120*HOUR)//(24*HOUR)*(24*HOUR)
    start=manifest['start_ms'];data={}
    for symbol in manifest['eligible_symbols']:
        p=Path('_support_raw_v4')/f'{symbol}.npy'
        if p.exists():
            a=np.load(p,allow_pickle=False);a=a[(a['open_time_ms']>=start)&(a['close_time_ms']<cutoff)]
            if len(a)>=241:data[symbol]=a
    assert len(data)>=150 and 'BTCUSDT' in data
    btc=data['BTCUSDT'];bt=btc['close_time_ms'];candidates={'v34_candle_proxy':[],'r6_4h_breakout':[]}
    for symbol,a in data.items():
        h4=four_hour(a);times=h4['close_time_ms'];prev=None
        one=[{**{k:float(r[k]) for k in ('close','high','low','quote_volume')},
              'close_time_ms':int(r['close_time_ms'])} for r in a]
        four=[{'close':float(r['close']),'close_time_ms':int(r['close_time_ms'])} for r in h4]
        for i in range(240,len(a)):
            ts=int(a[i]['open_time_ms']);h=int(np.searchsorted(times,ts));hist=a[i-30:i]
            if h<55 or ts-int(hist[-1]['close_time_ms'])!=1 or ts-int(times[h-1])>4*HOUR:
                prev=None;continue
            ctx=h4[h-55:h]
            if np.any(np.diff(hist['open_time_ms'])!=HOUR) or np.any(np.diff(ctx['open_time_ms'])!=4*HOUR):
                prev=None;continue
            q=float(hist[-24:]['quote_volume'].sum())
            f=feature_core(one[i-30:i],four[h-55:h],q)
            state=f['setup_state'];is_trigger=state=='CONTINUATION_TRIGGER' and prev!='CONTINUATION_TRIGGER'
            prev=state
            # State progresses even while the common liquidity/regime gate blocks entry.
            if q<10_000_000:continue
            b=int(np.searchsorted(bt,ts))
            if b<25 or ts-int(bt[b-1])>HOUR or np.any(np.diff(btc[b-25:b]['open_time_ms'])!=HOUR):continue
            btc_move=(float(btc[b-1]['close'])/float(btc[b-25]['close'])-1)*100
            atr=float(np.maximum(ctx[-14:]['high']-ctx[-14:]['low'],np.maximum(abs(ctx[-14:]['high']-ctx[-15:-1]['close']),abs(ctx[-14:]['low']-ctx[-15:-1]['close']))).mean())
            for strategy in candidates:
                directions=[f['bias']] if strategy=='v34_candle_proxy' and is_trigger else ['LONG','SHORT'] if strategy=='r6_4h_breakout' and ts%(4*HOUR)==0 and ts-int(times[h-1])==1 else []
                for direction in directions:
                    if direction not in ('LONG','SHORT'):continue
                    if (direction=='LONG' and btc_move<-4) or (direction=='SHORT' and btc_move>4):continue
                    if strategy=='r6_4h_breakout' and breakout(hist[-24:],ctx,direction,'4h') is None:continue
                    sign=1 if direction=='LONG' else -1
                    extension=sign*(float(hist[-1]['close'])-float(hist[-20:]['close'].mean()))/atr if atr else 0.
                    candidates[strategy].append({'symbol':symbol,'direction':direction,'entry_ms':ts,'bar_index':i,
                        'quote_volume_24h':q,'btc_24h_pct':btc_move,
                        'btc_alignment':'ALIGNED' if sign*btc_move>0 else 'OPPOSED_OR_FLAT',
                        'extension_atr':extension,'extension_bucket':'OVER_2_ATR' if extension>2 else 'AT_MOST_2_ATR'})
        print('Scanned',symbol,{k:len(v) for k,v in candidates.items()},flush=True)
    boundaries=np.linspace(start+240*HOUR,cutoff,4)
    report={'research_only':True,'holdout_evaluations':0,'contracts':len(data),'development_start_ms':start,
            'development_end_exclusive_ms':cutoff,'target_pct':5,'forced_time_exit':False,'common_stops_pct':STOP_PCTS,
            'comparisons':{},'limitations':['V3.4 candle proxy on futures is not full live spot discovery/Telegram replay',
                'Both arms share hourly observations, liquidity floor, BTC veto and exposure limits; native signal timeframes differ',
                'R6 keeps original structural-risk signal eligibility but execution stops are replaced in matched panels',
                'Fees/slippage .12 percent plus .01 percent per started 8h is an assumption, not actual funding',
                'Unresolved positions remain in denominator and block portfolio slots through cutoff; gap MTM can be stale',
                'Intrahour ambiguity loses; no leverage/liquidation simulation',
                'Development data previously studied; subgroup diagnostics are not independent validation or new trading filters',
                'No causal claim that late entry or market direction caused a loss; recovery window fixed at 96h after stop']}
    for strategy,raw in candidates.items():
        for stop in STOP_PCTS:
            key=f'{strategy}_stop_{stop:g}'
            rows=select_and_label(raw,data,stop,cutoff)
            periods=[metrics([r for r in rows if boundaries[j]<=r['entry_ms']<boundaries[j+1]]) for j in range(3)]
            report['comparisons'][key]={'raw_candidates':len(raw),'portfolio':metrics(rows),'loss_audit':loss_audit(rows),
                'entry_periods':periods,'by_direction':{v:metrics([r for r in rows if r['direction']==v]) for v in ('LONG','SHORT')},
                'by_btc_alignment':{v:metrics([r for r in rows if r['btc_alignment']==v]) for v in ('ALIGNED','OPPOSED_OR_FLAT')},
                'by_extension':{v:metrics([r for r in rows if r['extension_bucket']==v]) for v in ('OVER_2_ATR','AT_MOST_2_ATR')}}
            pd.DataFrame(rows).to_csv(out/f'{key}_trades.csv',index=False)
    (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    compact={k:{'portfolio':v['portfolio'],'loss_audit':v['loss_audit']} for k,v in report['comparisons'].items()}
    print('SUMMARY',json.dumps(compact,allow_nan=False),flush=True)

if __name__=='__main__':main()
