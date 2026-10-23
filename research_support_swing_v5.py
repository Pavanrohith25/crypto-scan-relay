"""Development-only support/reclaim swing benchmark requested by the user.

Signal context is 4h, entry confirmation 4h, target 5%, maximum hold 24h.
Two predeclared strategies, structure stops, minimum 1.5 reward/risk. No final
holdout labels or automatic live promotion. One-hour barrier ambiguity loses.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from historical_futures_backfill_v2 import read_archive, month_start, next_month
from train_swing_model_v2 import wilson_interval

HOUR=3600000
COST=.24  # Retain conservative v4 total cost for comparable research
STRATEGIES=('trend_support','sweep_reclaim')


def fetch_hourly(symbol,start,end,cache):
    chunks=[];evidence=[];cursor=month_start(start)
    while cursor<end:
        nxt=next_month(cursor)
        if nxt<=end:
            keys=[f'data/futures/um/monthly/klines/{symbol}/1h/{symbol}-1h-{cursor:%Y-%m}.zip']
        else:
            days=(end-max(cursor,start)).days
            keys=[f'data/futures/um/daily/klines/{symbol}/1h/{symbol}-1h-{max(cursor,start)+timedelta(days=i):%Y-%m-%d}.zip'
                  for i in range(days)]
        for key in keys:
            a,record=read_archive(key);evidence.append(record)
            if a is not None: chunks.append(a)
        cursor=nxt
    if not chunks: return symbol,None,{'status':'NO_HISTORY','archives':evidence}
    a=np.concatenate(chunks);a=a[np.argsort(a['open_time_ms'])]
    lo,hi=int(start.timestamp()*1000),int(end.timestamp()*1000)
    a=a[(a['open_time_ms']>=lo)&(a['close_time_ms']<hi)]
    if len(a)<250:return symbol,None,{'status':'SHORT_HISTORY','rows':len(a),'archives':evidence}
    if (np.diff(a['open_time_ms'])<=0).any() or (a['close_time_ms']!=a['open_time_ms']+HOUR-1).any():
        raise ValueError(f'{symbol}: invalid timestamps')
    if any(not np.isfinite(a[k]).all() for k in ['open','high','low','close','quote_volume']):
        raise ValueError(f'{symbol}: nonfinite candles')
    if any((a[k]<=0).any() for k in ['open','high','low','close']) or (a['quote_volume']<0).any():
        raise ValueError(f'{symbol}: invalid prices/volume')
    if ((a['high']<np.maximum(a['open'],a['close']))|(a['low']>np.minimum(a['open'],a['close']))).any():
        raise ValueError(f'{symbol}: invalid OHLC')
    cache.mkdir(exist_ok=True,parents=True);np.save(cache/f'{symbol}.npy',a)
    return symbol,a,{'status':'PASS','rows':len(a),'gaps':int((np.diff(a['open_time_ms'])!=HOUR).sum()),'archives':evidence}


def four_hour(a):
    frame=pd.DataFrame.from_records(a)
    frame['bucket']=frame.open_time_ms//(4*HOUR)
    group=frame.groupby('bucket',sort=True)
    out=group.agg(open_time_ms=('open_time_ms','min'),close_time_ms=('close_time_ms','max'),
                  open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),
                  quote_volume=('quote_volume','sum'),count=('open','size'))
    out=out[(out['count']==4)&(out.open_time_ms%(4*HOUR)==0)&(out.close_time_ms-out.open_time_ms==4*HOUR-1)]
    return out.to_records(index=False)


def setup(history,h4,direction,strategy):
    """All supplied bars must already be closed. Stops never use future pivots."""
    sign=1 if direction=='LONG' else -1
    if len(history)<24 or len(h4)<55:return None
    if np.any(np.diff(history['open_time_ms'])!=HOUR) or np.any(np.diff(h4['open_time_ms'])!=4*HOUR):return None
    last=h4[-1];price=float(last['close'])
    prior=h4[:-1]  # reference level existed before the latest 4h candle
    support=float(prior[-18:]['low'].min());resistance=float(prior[-18:]['high'].max())
    atr=float(np.mean(np.maximum(h4[-14:]['high']-h4[-14:]['low'],
                     np.maximum(abs(h4[-14:]['high']-h4[-15:-1]['close']),abs(h4[-14:]['low']-h4[-15:-1]['close'])))))
    if atr<=0:return None
    ma20=float(h4[-20:]['close'].mean());ma50=float(h4[-50:]['close'].mean())
    old20=float(h4[-25:-5]['close'].mean())
    long_trend=ma20>ma50 and ma20>old20
    short_trend=ma20<ma50 and ma20<old20
    q=float(history[-24:]['quote_volume'].sum())
    if q<10_000_000:return None
    if direction=='LONG':
        reversal=price>float(last['open']) and price>float(h4[-2]['close'])
        touch=float(last['low'])<=support+.5*atr and price>=support
        sweep=float(last['low'])<support and price>support
        stop=min(support,float(last['low']))-.25*atr
        room=float(prior[-42:]['high'].max())/price-1
        allowed=(long_trend and touch) if strategy=='trend_support' else sweep
    else:
        reversal=price<float(last['open']) and price<float(h4[-2]['close'])
        touch=float(last['high'])>=resistance-.5*atr and price<=resistance
        sweep=float(last['high'])>resistance and price<resistance
        stop=max(resistance,float(last['high']))+.25*atr
        room=1-float(prior[-42:]['low'].min())/price
        allowed=(short_trend and touch) if strategy=='trend_support' else sweep
    risk=sign*(price-stop)/price*100
    if not (allowed and reversal and .25<=risk<=5/1.5 and room>=.05):return None
    return {'stop_price':stop,'detection_price':price,'estimated_risk_pct':risk,
            'estimated_reward_risk':5/risk,'support':support,'resistance':resistance,
            'atr_4h':atr,'quote_volume_24h':q}


def label(direction,stop,bars):
    if len(bars)!=24 or np.any(np.diff(bars['open_time_ms'])!=HOUR):return None
    sign=1 if direction=='LONG' else -1;entry=float(bars[0]['open'])
    risk=sign*(entry-stop)/entry*100
    if not .25<=risk<=5/1.5:return None
    target=entry*(1+sign*.05)
    path='TIMEOUT';ret=sign*(float(bars[-1]['close'])/entry-1)*100;exit_i=23
    for i,bar in enumerate(bars):
        tp=bar['high']>=target if sign==1 else bar['low']<=target
        sl=bar['low']<=stop if sign==1 else bar['high']>=stop
        if sl:
            path='AMBIGUOUS' if tp else 'STOP_FIRST';ret=min(-risk,sign*(float(bar['open'])/entry-1)*100);exit_i=i;break
        if tp:path='TARGET_FIRST';ret=5.;exit_i=i;break
    window=bars[:exit_i+1]
    adv=sign*((window['low'] if sign==1 else window['high'])/entry-1)*100
    return {'entry_ms':int(bars[0]['open_time_ms']),'entry_price':entry,'stop_pct':risk,
            'exit_ms':int(bars[exit_i]['close_time_ms'])+1,'path':path,'win':int(path=='TARGET_FIRST'),
            'gross_return_pct':ret,'net_return_pct':ret-COST,'net_return_R':(ret-COST)/risk,
            'mae_before_exit_pct':float(min(0,adv.min())),'holding_hours':exit_i+1}


def portfolio(rows):
    ordered=sorted(rows,key=lambda r:(r['entry_ms'],-r['estimated_reward_risk'],r['symbol']))
    active=[];last={};kept=[]
    for r in ordered:
        ts=r['entry_ms'];symbol=r['symbol'];direction=r['direction']
        active=[x for x in active if x['exit_ms']>ts]
        if any(x['symbol']==symbol or x['direction']==direction for x in active):continue
        if symbol in last and ts-last[symbol]<24*HOUR:continue
        kept.append(r);active.append(r);last[symbol]=ts
    return kept


def summary(rows):
    if not rows:return {'trades':0,'precision':None,'avg_net_return_pct':None,'profit_factor':None}
    r=np.array([x['net_return_pct'] for x in rows]);wins=sum(x['win'] for x in rows)
    losses=float(-r[r<0].sum());gains=float(r[r>0].sum())
    ordered=sorted(rows,key=lambda x:x['exit_ms']);equity=np.r_[0,np.cumsum([x['net_return_R'] for x in ordered])]
    return {'trades':len(rows),'wins':wins,'precision':wins/len(rows),'wilson_95_interval':wilson_interval(wins,len(rows)),
            'avg_net_return_pct':float(r.mean()),'profit_factor':gains/losses if losses else 999.,
            'mean_stop_pct':float(np.mean([x['stop_pct'] for x in rows])),
            'mean_adverse_movement_pct':float(np.mean([x['mae_before_exit_pct'] for x in rows])),
            'p95_adverse_magnitude_pct':float(np.quantile([-x['mae_before_exit_pct'] for x in rows],.95)),
            'mean_hold_hours':float(np.mean([x['holding_hours'] for x in rows])),
            'closed_trade_max_drawdown_R':float((np.maximum.accumulate(equity)-equity).max()),
            'avg_return_at_0_50pct_cost':float(np.mean([x['gross_return_pct']-.5 for x in rows])),
            'ambiguous_losses':sum(x['path']=='AMBIGUOUS' for x in rows)}


def main():
    source=Path('_prior_research');out=Path('_support_v5');out.mkdir(exist_ok=True)
    old=json.loads((source/'_ml_artifact_v2/swing_model_v2_report.json').read_text())
    manifest=json.loads((source/'_futures_raw_v2/manifest.json').read_text())
    start=datetime.fromtimestamp(manifest['start_ms']/1000,tz=timezone.utc)
    # Collect only development candles; the original final holdout is excluded.
    end=datetime.fromtimestamp(old['split']['test_start_ms']/1000,tz=timezone.utc)-timedelta(hours=120)
    end=end.replace(hour=0,minute=0,second=0,microsecond=0)
    symbols=manifest['eligible_symbols'];data={};coverage={}
    def download(symbol):
        try:
            a=np.load(Path('_support_raw_v4')/f'{symbol}.npy',allow_pickle=False)
            lo,hi=int(start.timestamp()*1000),int(end.timestamp()*1000)
            a=a[(a['open_time_ms']>=lo)&(a['close_time_ms']<hi)]
            return symbol,a,{'status':'PASS','rows':len(a),'source':'frozen v4 development archive'}
        except Exception as e:return symbol,None,{'status':'FAIL','error':str(e)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for symbol,a,evidence in pool.map(download,symbols):
            coverage[symbol]=evidence
            if a is not None:data[symbol]=a
            print('Loaded',symbol,evidence['status'],flush=True)
    (out/'coverage.json').write_text(json.dumps(coverage,indent=2))
    if len(data)<150 or not {'BTCUSDT','ETHUSDT'}.issubset(data):raise RuntimeError('inadequate historical coverage')
    # BTC 24h veto is based only on closed hourly candles, not outcome data.
    btc=data['BTCUSDT'];btc_time=btc['close_time_ms']
    candidates={k:[] for k in STRATEGIES}
    for symbol,a in data.items():
        h4=four_hour(a);times=h4['close_time_ms']
        for i in range(240,len(a)-24+1):
            ts=int(a[i]['open_time_ms'])
            if ts%(4*HOUR)!=0:continue
            h=int(np.searchsorted(times,ts,side='left'))
            if h<55 or ts-int(times[h-1])!=1:continue
            b=int(np.searchsorted(btc_time,ts,side='left'))
            if b<25 or ts-int(btc_time[b-1])>HOUR or btc[b-1]['open_time_ms']-btc[b-25]['open_time_ms']!=24*HOUR:continue
            btc_change=(float(btc[b-1]['close'])/float(btc[b-25]['close'])-1)*100
            hist=a[i-24:i];context=h4[h-55:h]
            if ts-int(hist[-1]['close_time_ms'])!=1:continue
            for direction in ('LONG','SHORT'):
                if (direction=='LONG' and btc_change<-4) or (direction=='SHORT' and btc_change>4):continue
                for strategy in STRATEGIES:
                    signal=setup(hist,context,direction,strategy)
                    if signal is None:continue
                    outcome=label(direction,signal['stop_price'],a[i:i+24])
                    if outcome is not None:candidates[strategy].append({'symbol':symbol,'direction':direction,**signal,**outcome})
        print('Scanned',symbol,{k:len(v) for k,v in candidates.items()},flush=True)
    lo=int(start.timestamp()*1000)+240*HOUR;hi=int(end.timestamp()*1000)-24*HOUR
    boundaries=np.linspace(lo,hi,4).astype('int64')
    result={'research_only':True,'target_pct':5,'signal_timeframe':'4h','max_hold_hours':24,'stop_rule':'support/resistance plus 0.25 x 4h ATR',
            'minimum_estimated_reward_risk':1.5,'cost_pct':COST,'contracts':len(data),
            'holdout_evaluations':0,'development_end_exclusive_ms':int(end.timestamp()*1000),'strategies':{},
            'limitations':['Hourly candles cannot resolve intrabar target/stop order; ambiguity counted as a loss',
                          'Total costs retain conservative 0.24 percent allowance; funding is not reconstructed',
                          'Adverse movement includes the full exit candle and can overestimate pre-exit excursion',
                          'Fixed strategies are development screens, not validated 60% live models']}
    for strategy,rows in candidates.items():
        selected=portfolio(rows);m=summary(selected)
        periods=[summary([r for r in selected if boundaries[j]<=r['entry_ms']<boundaries[j+1]]) for j in range(3)]
        qualify=bool(m['trades']>=100 and m['precision']>=.60 and m['avg_net_return_pct']>0 and m['profit_factor']>1
                     and all(p['trades']>=20 and p['avg_net_return_pct']>0 for p in periods))
        result['strategies'][strategy]={'raw_candidates':len(rows),'portfolio':m,'three_periods':periods,
                                        'qualifies_for_final_test_design':qualify}
        pd.DataFrame(selected).to_csv(out/f'{strategy}_trades.csv',index=False)
    (out/'report.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    print('SUMMARY',json.dumps(result,allow_nan=False),flush=True)


if __name__=='__main__':main()
