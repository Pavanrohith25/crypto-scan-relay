"""Versioned Telegram-only rewards and a shadow expected-R learner. No trading gate."""
import hashlib
import json
import math
import time
from pathlib import Path

VERSION = 'telegram-reward-v1'
ROOT = Path('paper_learning')


def new_state():
    return {'version': VERSION, 'records': {}, 'models': {
        side: {'n': 0, 'weights': [0.] * 7} for side in ('LONG', 'SHORT')}}


def forecast(model, features):
    return max(-2., min(2., sum(w*x for w,x in zip(model['weights'], features))))


def reward_snapshot(side, features):
    path=ROOT/'telegram_rewards.json'
    state=json.loads(path.read_text()) if path.exists() else new_state()
    model=state['models'][side]
    return {'version': VERSION, 'model_n': model['n'],
            'expected_r': forecast(model,features),
            'model_hash': hashlib.sha256(json.dumps(model,sort_keys=True).encode()).hexdigest()}


def update_model(model, features, reward):
    # Normalized SGD on realized net R. Bound the update, never the ledger loss.
    prediction=forecast(model,features)
    error=max(-3.,min(3.,reward-prediction))
    norm=max(1.,sum(x*x for x in features))
    rate=.15/math.sqrt(1+model['n']/100)
    model['weights']=[w+rate*(error*x/norm-(.001*w if i else 0))
                      for i,(w,x) in enumerate(zip(model['weights'],features))]
    model['n']+=1


def reconcile(state, trades, telegram, cutoff, now):
    if state['version'] != VERSION:raise ValueError('Reward migration required')
    added=[]
    for tid,sent in sorted(telegram.get('sent',{}).items(),key=lambda item:(item[1],item[0])):
        if sent < cutoff or tid in state['records']:continue
        t=trades.get(tid)
        if not t or t['status'] not in ('TARGET','STOP','AMBIGUOUS_LOSS'):continue
        # Only outcomes whose barrier candle starts after notification can train.
        # Earlier crossing times within the delivery candle cannot be established.
        if t['resolved_bar_ms'] < sent:
            state['records'][tid]={'id':tid,'excluded':'EXIT_CANDLE_PRECEDES_DELIVERY','sent_ms':sent}
            continue
        if t['label_available_ms'] > now:continue
        risk=float(t['stop_pct']);net=float(t['net_return_pct']);x=t['features']
        if risk<=0 or len(x)!=7 or not all(math.isfinite(v) for v in [risk,net,*x]):
            raise ValueError('Invalid reward input')
        reward=net/risk
        record={'id':tid,'symbol':t['symbol'],'side':t['side'],'status':t['status'],
                'sent_ms':sent,'recorded_ms':now,'net_return_pct':net,'reward_r':reward,
                'stressed_reward_r':float(t['stressed_net_return_pct'])/risk,
                'mae_pct':t['mae_pct'],'mfe_pct':t['mfe_pct'],'holding_hours':t['holding_hours'],
                'features':x,'snapshot':t['snapshot'],
                'forecast_at_delivery':telegram.get('reward_predictions',{}).get(tid),
                'definition':'paper net price return / planned stop percent; not account P&L'}
        update_model(state['models'][t['side']],x,reward)
        state['records'][tid]=record;added.append(record)
    return added


def report(state, trades, telegram, cutoff, now):
    rows=[r for r in state['records'].values() if 'reward_r' in r]
    sent={k:v for k,v in telegram.get('sent',{}).items() if v>=cutoff}
    valid=[r for r in rows if r.get('forecast_at_delivery',{} ) is not None and
           r.get('forecast_at_delivery',{}).get('model_n',0)>=30]
    def mse(key):
        return sum((key(r)-r['reward_r'])**2 for r in valid)/len(valid) if valid else None
    return {'version':VERSION,'as_of_ms':now,'enabled_after_ms':cutoff,'promotion':'BLOCKED',
            'sent_calls':len(sent),'scored_outcomes':len(rows),
            'pending':sum(tid not in state['records'] for tid in sent),
            'excluded':sum('excluded' in r for r in state['records'].values()),
            'wins':sum(r['status']=='TARGET' for r in rows),
            'losses':sum(r['status']!='TARGET' for r in rows),
            'total_reward_r':sum(r['reward_r'] for r in rows),
            'mean_reward_r':sum(r['reward_r'] for r in rows)/len(rows) if rows else None,
            'total_stressed_reward_r':sum(r['stressed_reward_r'] for r in rows),
            'model_updates':{s:m['n'] for s,m in state['models'].items()},
            'evaluated_forecasts_after_warmup':len(valid),
            'forward_squared_error':mse(lambda r:r['forecast_at_delivery']['expected_r']),
            'zero_reward_baseline_squared_error':mse(lambda r:0),
            'limitations':['Paper spot paths and assumed costs, not actual fills or user exits.',
                'R sums are not portfolio returns; trades may overlap and correlate.',
                'No causal proof of why a trade lost; model learns feature/outcome associations.',
                'Shadow learner does not change live alert rules.']}


def main():
    config=json.loads(Path('telegram_reward_config.json').read_text())
    if not config['enabled']:return
    path=ROOT/'telegram_rewards.json'
    state=json.loads(path.read_text()) if path.exists() else new_state()
    trades=json.loads((ROOT/'state.json').read_text())['trades']
    telpath=ROOT/'telegram.json'
    telegram=json.loads(telpath.read_text()) if telpath.exists() else {'sent':{}}
    now=int(time.time()*1000)
    added=reconcile(state,trades,telegram,config['enabled_after_ms'],now)
    path.write_text(json.dumps(state,indent=2,sort_keys=True,allow_nan=False)+'\n')
    summary=report(state,trades,telegram,config['enabled_after_ms'],now)
    (ROOT/'telegram_reward_report.json').write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n')
    print(json.dumps(summary))


if __name__=='__main__':main()
