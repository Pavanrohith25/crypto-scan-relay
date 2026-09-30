"""Fixed development screens: +5% before structure stop, no forced time exit."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from research_support_swing_v4 import HOUR, four_hour, portfolio, setup as hourly_setup
from research_support_swing_v5 import setup as four_hour_setup
from train_swing_model_v2 import wilson_interval

CONFIGS=[(tf,kind) for tf in ('1h','4h') for kind in ('trend_support','sweep_reclaim','trend_breakout')]


def breakout(history,h4,direction,tf):
    last=history[-1] if tf=='1h' else h4[-1]
    prev=history[-2] if tf=='1h' else h4[-2]
    price=float(last['close']);sign=1 if direction=='LONG' else -1
    prior=h4[:-1];level=float(prior[-18:]['high'].max() if sign==1 else prior[-18:]['low'].min())
    ma20=float(h4[-20:]['close'].mean());ma50=float(h4[-50:]['close'].mean())
    old20=float(h4[-25:-5]['close'].mean())
    atr=float(np.maximum(h4[-14:]['high']-h4[-14:]['low'],np.maximum(abs(h4[-14:]['high']-h4[-15:-1]['close']),abs(h4[-14:]['low']-h4[-15:-1]['close']))).mean())
    q=float(history['quote_volume'].sum())
    if q<10_000_000 or atr<=0:return None
    if not (sign*(ma20-ma50)>0 and sign*(ma20-old20)>0 and sign*(price-level)>0 and sign*(float(prev['close'])-level)<=0):return None
    signal_bars=history if tf=='1h' else h4
    if float(last['quote_volume'])<1.5*float(signal_bars[-21:-1]['quote_volume'].mean()):return None
    stop=(min(level,float(last['low']))-.25*atr) if sign==1 else (max(level,float(last['high']))+.25*atr)
    risk=sign*(price-stop)/price*100
    if not .25<=risk<=5/1.5:return None
    return {'stop_price':stop,'estimated_reward_risk':5/risk,'estimated_risk_pct':risk}


def label(direction,stop,bars):
    if len(bars)==0:return None
    sign=1 if direction=='LONG' else -1;entry=float(bars[0]['open'])
    risk=sign*(entry-stop)/entry*100
    if not .25<=risk<=5/1.5:return None
    gaps=np.flatnonzero(np.diff(bars['open_time_ms'])!=HOUR)
    reason='DATA_GAP' if len(gaps) else 'DATA_END'
    if len(gaps):bars=bars[:int(gaps[0])+1]
    target=entry*(1+sign*.05)
    hit_tp=(bars['high']>=target) if sign==1 else (bars['low']<=target)
    hit_sl=(bars['low']<=stop) if sign==1 else (bars['high']>=stop)
    hits=np.flatnonzero(hit_tp|hit_sl)
    j=int(hits[0]) if len(hits) else len(bars)-1
    resolved=bool(len(hits));win=bool(resolved and hit_tp[j] and not hit_sl[j])
    path=('TARGET_FIRST' if win else ('AMBIGUOUS' if hit_tp[j] else 'STOP_FIRST')) if resolved else reason
    ret=5. if win else min(-risk,sign*(float(bars[j]['open'])/entry-1)*100) if resolved else sign*(float(bars[j]['close'])/entry-1)*100
    hours=j+1;cost=.12+.01*np.ceil(hours/8)
    adverse=sign*((bars[:j+1]['low'] if sign==1 else bars[:j+1]['high'])/entry-1)*100
    # An unresolved position blocks its portfolio slot through the data end,
    # including when a gap prevents us knowing its true exit.
    return {'entry_ms':int(bars[0]['open_time_ms']),'exit_ms':int(bars[j]['close_time_ms'])+1,
            'stop_pct':risk,'path':path,'resolved':resolved,'win':int(win),'holding_hours':hours,
            'gross_return_pct':ret,'net_return_pct':float(ret-cost),'net_return_R':float((ret-cost)/risk),
            'mae_pct':float(min(0,adverse.min())),'cost_pct':float(cost)}


def metrics(rows):
    n=len(rows)
    if not n:return {'trades':0,'wins':0,'unresolved':0,'target_fraction_all':None}
    resolved=[r for r in rows if r['resolved']];wins=sum(r['win'] for r in rows);unresolved=n-len(resolved)
    net=np.array([r['net_return_pct'] for r in resolved]);loss=-net[net<0].sum()
    return {'trades':n,'wins':wins,'resolved':len(resolved),'unresolved':unresolved,
            'target_fraction_all':wins/n,'eventual_success_bounds':[wins/n,(wins+unresolved)/n],
            'resolved_win_rate':wins/len(resolved) if resolved else None,
            'wilson_all_descriptive':wilson_interval(wins,n),
            'resolved_avg_net_pct':float(net.mean()) if len(net) else None,
            'resolved_profit_factor':float(net[net>0].sum()/loss) if loss else None,
            'all_positions_mtm_avg_net_pct':float(np.mean([r['net_return_pct'] for r in rows])),
            'resolved_avg_net_stress_pct':float(np.mean([r['net_return_pct']-.38 for r in resolved])) if resolved else None,
            'median_hold_hours':float(np.median([r['holding_hours'] for r in rows])),
            'p95_hold_hours':float(np.quantile([r['holding_hours'] for r in rows],.95)),
            'mean_stop_pct':float(np.mean([r['stop_pct'] for r in rows])),
            'ambiguous_losses':sum(r['path']=='AMBIGUOUS' for r in rows)}


def main():
    out=Path('_target_first_v6');out.mkdir(exist_ok=True)
    source=Path('_prior_research')
    old=json.loads((source/'_ml_artifact_v2/swing_model_v2_report.json').read_text())
    cutoff=(old['split']['test_start_ms']-120*HOUR)//(24*HOUR)*(24*HOUR)
    manifest=json.loads((source/'_futures_raw_v2/manifest.json').read_text())
    data={}
    for symbol in manifest['eligible_symbols']:
        p=Path('_support_raw_v4')/f'{symbol}.npy'
        if not p.exists():continue
        a=np.load(p,allow_pickle=False);a=a[a['close_time_ms']<cutoff]
        if len(a):data[symbol]=a
    assert len(data)>=150 and 'BTCUSDT' in data
    candidates={f'{tf}_{kind}':[] for tf,kind in CONFIGS}
    btc=data['BTCUSDT'];btc_time=btc['close_time_ms']
    for symbol,a in data.items():
        h4=four_hour(a);times=h4['close_time_ms']
        for i in range(240,len(a)):
            ts=int(a[i]['open_time_ms']);h=int(np.searchsorted(times,ts))
            if h<55:continue
            history=a[i-24:i];context=h4[h-55:h]
            if ts-int(history[-1]['close_time_ms'])!=1 or np.any(np.diff(history['open_time_ms'])!=HOUR) or np.any(np.diff(context['open_time_ms'])!=4*HOUR):continue
            if ts-int(times[h-1])>4*HOUR:continue
            b=int(np.searchsorted(btc_time,ts))
            if b<25 or ts-int(btc_time[b-1])>HOUR or np.any(np.diff(btc[b-25:b]['open_time_ms'])!=HOUR):continue
            change=(float(btc[b-1]['close'])/float(btc[b-25]['close'])-1)*100
            for tf,kind in CONFIGS:
                if tf=='4h' and (ts%(4*HOUR)!=0 or ts-int(times[h-1])!=1):continue
                for direction in ('LONG','SHORT'):
                    if (direction=='LONG' and change<-4) or (direction=='SHORT' and change>4):continue
                    signal=breakout(history,context,direction,tf) if kind=='trend_breakout' else (hourly_setup if tf=='1h' else four_hour_setup)(history,context,direction,kind)
                    if signal is None:continue
                    outcome=label(direction,signal['stop_price'],a[i:])
                    if outcome is None:continue
                    if not outcome['resolved']:outcome['exit_ms']=cutoff
                    candidates[f'{tf}_{kind}'].append({'symbol':symbol,'direction':direction,**signal,**outcome})
        print('Scanned',symbol,flush=True)
    boundaries=np.linspace(manifest['start_ms']+240*HOUR,cutoff,4)
    report={'research_only':True,'holdout_evaluations':0,'target_pct':5,'forced_time_exit':False,
            'contracts':len(data),'development_end_exclusive_ms':cutoff,'strategies':{},
            'limitations':['Six fixed comparisons; reused development data is not an unseen validation set',
                          'Hourly target/stop ambiguity loses; unresolved trades remain in denominator',
                          'Funding allowance 0.01 percent per 8h is not actual funding; fees/slippage 0.12 percent',
                          'Frozen universe can introduce selection/survivorship bias',
                          'Entry-period summaries share market regimes and are not independent trials',
                          'No leverage/liquidation simulation; returns are underlying position returns']}
    for name,rows in candidates.items():
        selected=portfolio(rows);m=metrics(selected)
        periods=[metrics([r for r in selected if boundaries[j]<=r['entry_ms']<boundaries[j+1]]) for j in range(3)]
        qualifies=bool(m['trades']>=100 and m['target_fraction_all']>=.60 and m['unresolved']/m['trades']<=.05
                       and m['resolved_avg_net_pct']>0 and m['all_positions_mtm_avg_net_pct']>0
                       and all(p['trades']>=20 and p.get('resolved_avg_net_pct',-1) is not None and p.get('resolved_avg_net_pct',-1)>0 for p in periods))
        report['strategies'][name]={'raw_candidates':len(rows),'portfolio':m,'entry_periods':periods,'eligible_for_further_validation':qualifies}
        pd.DataFrame(selected).to_csv(out/f'{name}_trades.csv',index=False)
    (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    print('SUMMARY',json.dumps(report,allow_nan=False),flush=True)

if __name__=='__main__':main()
