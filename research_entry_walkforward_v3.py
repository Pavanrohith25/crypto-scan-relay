"""Predeclared development experiment. Never evaluates the final v2 holdout.

Two pooled learners x original/delayed entry gates, three expanding time folds.
Each fold has separate fit, calibration, threshold-selection and evaluation
windows with 72h purges. The original target and risk/cost rules are unchanged.
"""
import gzip
import json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from train_swing_model_v1 import FEATURES, TARGET_COL, RETURN_COL, TIME_COL
from train_swing_model_v2 import (HOUR, compact_frame, calibrated_probs, logit,
                                 alert_indices, metrics_for_indices, qualifies)

EXTRA = ['target_to_atr', 'directional_taker_imbalance', 'momentum_acceleration_15m']
COLS = list(dict.fromkeys(FEATURES + [TIME_COL, 'symbol', 'direction', 'market_regime',
       'entry_eligible', TARGET_COL, RETURN_COL, 'exit_ms', 'entry_price', 'detection_price',
       'mae_first_15m_pct', 'mae_first_30m_pct', 'mae_first_60m_pct',
       'ambiguous_target5_stop1_5_48h']))


def load_development(root, boundary):
    frames, batch = [], []
    with gzip.open(root/'historical_futures_dataset_v2.jsonl.gz','rt') as f:
        for line in f:
            row=json.loads(line)
            if row[TIME_COL] >= boundary:
                continue
            batch.append({k:row[k] for k in COLS})
            if len(batch)==25000:
                frames.append(compact_frame(batch));batch=[]
        if batch: frames.append(compact_frame(batch))
    df=pd.concat(frames,ignore_index=True).sort_values([TIME_COL,'symbol']).reset_index(drop=True)
    if df[TIME_COL].max()>=boundary or (df.exit_ms>=boundary+48*HOUR).any():
        raise ValueError('development boundary violation')
    df['target_to_atr']=5/df.atr_1h_pct.clip(lower=.05)
    df['directional_taker_imbalance']=df.direction_sign*(df.taker_buy_ratio_1h-.5)
    df['momentum_acceleration_15m']=df.directional_momentum_15m-df.directional_momentum_45m/3
    return df


def delayed_retest_mask(df):
    """Observed-price state machine; never reads outcome or next-open columns.

    A setup must improve >=0.3% from its watch price within 2h, then show two
    consecutive 15m confirmations while still >=0.2% better than the watch
    price. Missing observations or direction/regime flips reset the sequence.
    """
    state={};keep=np.zeros(len(df),dtype=bool)
    columns=[TIME_COL,'symbol','direction','market_regime','detection_price','retest_ready',
             'confirmation_15m','anti_chase_abs_12h_pct','distance_from_15m_sma4_pct']
    for i,values in enumerate(df[columns].itertuples(index=False,name=None)):
        ts,symbol,direction,regime,price,retest,confirm,chase,distance=values
        sign=1 if direction=='LONG' else -1
        compatible=not ((direction=='LONG' and regime=='RISK_OFF') or
                        (direction=='SHORT' and regime=='RISK_ON'))
        s=state.get(symbol)
        if not compatible:
            state.pop(symbol,None);continue
        if s and (s['direction']!=direction or ts-s['last']!=900000 or ts-s['start']>2*HOUR):
            state.pop(symbol,None);s=None
        if s is None:
            if retest and chase<=8 and abs(distance)<=.75:
                state[symbol]={'start':ts,'last':ts,'direction':direction,'price':price,
                               'pulled':False,'confirmations':0}
            continue
        s['last']=ts
        improvement=sign*(price/s['price']-1)*100
        if improvement<=-.3: s['pulled']=True
        if s['pulled']:
            s['confirmations']=s['confirmations']+1 if confirm else 0
        if s['confirmations']>=2 and improvement<=-.2 and chase<=8 and abs(distance)<=.75:
            keep[i]=True;state.pop(symbol,None)
    return keep


def fold_windows(df):
    times=np.sort(df[TIME_COL].unique())
    # Fractions are frozen before the experiment; no final-test data involved.
    spans=[(.60,.73),(.73,.86),(.86,1.)]
    for number,(lo,hi) in enumerate(spans,1):
        at=lambda f:int(times[min(len(times)-1,int(len(times)*f))])
        evaluation_start=at(lo)
        evaluation_end=at(hi) if hi<1 else int(times[-1])+1
        calibration_start=at(lo-.24)
        selection_start=at(lo-.12)
        gap=72*HOUR
        yield number, {
            'fit':df[df[TIME_COL]<calibration_start-gap].copy(),
            'calibration':df[(df[TIME_COL]>=calibration_start)&(df[TIME_COL]<selection_start-gap)].copy(),
            'selection':df[(df[TIME_COL]>=selection_start)&(df[TIME_COL]<evaluation_start-gap)].copy(),
            'evaluation':df[(df[TIME_COL]>=evaluation_start)&(df[TIME_COL]<evaluation_end)].copy()}, {
            'calibration_start_ms':calibration_start,'selection_start_ms':selection_start,
            'evaluation_start_ms':evaluation_start,'evaluation_end_exclusive_ms':evaluation_end,'purge_hours':72}


def threshold_on_selection(frame,p):
    # Include the actual probability distribution; the old .35 floor left almost
    # every probability unexamined. The REQUIRED measured win rate stays 60%.
    thresholds=np.unique(np.r_[np.arange(.05,.951,.025),np.quantile(p,np.arange(.5,.991,.025))])
    rows=[{'threshold':float(t),**metrics_for_indices(frame,alert_indices(frame,p,float(t)))} for t in thresholds]
    enough=[r for r in rows if r['alerts']>=30]
    qualified=[r for r in enough if r['precision']>=.60 and r['avg_net_return_pct']>0 and r['profit_factor']>1]
    best=max(qualified,key=lambda r:r['net_return_sum_pct']) if qualified else None
    diagnostic=max(enough,key=lambda r:r['precision']) if enough else None
    return (best['threshold'] if best else 1.01),diagnostic


def main():
    from lightgbm import LGBMClassifier
    root=Path('_prior_research/_ml_artifact_v2')
    old=json.loads((root/'swing_model_v2_report.json').read_text())
    boundary=old['split']['test_start_ms']-72*HOUR
    df=load_development(root,boundary)
    df['delayed_eligible']=delayed_retest_mask(df)
    report={'experiment':'pooled-training-and-delayed-retest-v3','final_holdout_evaluations':0,
            'development_end_exclusive_ms':boundary,'target':'+5% before -1.5% within 48h',
            'development_rows':len(df),'original_eligible_rows':int(df.entry_eligible.sum()),
            'delayed_eligible_rows':int(df.delayed_eligible.sum()),'folds':[],
            'cost_pct':.18,'minimum_combined_evaluation_alerts':100,'minimum_precision':.60,
            'live_enabled':False,'candidates':{}}
    collected={};features=FEATURES+EXTRA
    for number,windows,dates in fold_windows(df):
        fit=windows['fit']
        # Six-hour observation blocks prevent hundreds of neighboring snapshots
        # from dominating a single coin's trend. Label overlap is still possible
        # within training; purge prevents overlap across evaluation boundaries.
        fit=fit.assign(block=fit[TIME_COL]//(6*HOUR)).drop_duplicates(['symbol','block'])
        models={
            'pooled_hist':HistGradientBoostingClassifier(max_iter=200,max_leaf_nodes=15,min_samples_leaf=80,
                          l2_regularization=4.,learning_rate=.05,early_stopping=False,random_state=42),
            'pooled_lightgbm':LGBMClassifier(n_estimators=250,num_leaves=15,min_child_samples=100,
                          reg_lambda=4.,learning_rate=.04,n_jobs=2,verbosity=-1,random_state=42,
                          deterministic=True,force_col_wise=True)}
        for model_name,model in models.items():
            model.fit(fit[features],fit[TARGET_COL])
            for gate in ['original','delayed']:
                name=model_name+'_'+gate
                frames={k:v.copy() for k,v in windows.items() if k!='fit'}
                if gate=='delayed':
                    for v in frames.values(): v['entry_eligible']=v['delayed_eligible'].astype(int)
                cal=frames['calibration'];cal=cal[cal.entry_eligible==1]
                row={'fold':number,'candidate':name,'dates':dates,'fit_rows':len(fit),'calibration_rows':len(cal)}
                if len(cal)<50 or cal[TARGET_COL].nunique()<2:
                    row['status']='INSUFFICIENT_CALIBRATION';report['folds'].append(row);continue
                calibrator=LogisticRegression(C=1.,max_iter=1000,random_state=42)
                calibrator.fit(logit(model.predict_proba(cal[features])[:,1]),cal[TARGET_COL])
                selection=frames['selection']
                p=calibrated_probs(model,calibrator,selection[features])
                threshold,diagnostic=threshold_on_selection(selection,p)
                evaluation=frames['evaluation']
                ep=calibrated_probs(model,calibrator,evaluation[features])
                idx=alert_indices(evaluation,ep,threshold)
                trades=evaluation.iloc[idx].copy()
                # Fold transitions have independent models; discard a fold's
                # final 48h entries so no carried position crosses a boundary.
                trades=trades[trades[TIME_COL]<dates['evaluation_end_exclusive_ms']-48*HOUR]
                collected.setdefault(name,[]).append(trades)
                metrics=metrics_for_indices(trades,np.arange(len(trades)))
                row.update(status='EVALUATED',threshold=threshold,selection_diagnostic=diagnostic,evaluation=metrics)
                report['folds'].append(row)
                print(json.dumps(row),flush=True)
    for name,frames in collected.items():
        trades=pd.concat(frames,ignore_index=True)
        m=metrics_for_indices(trades,np.arange(len(trades)))
        folds=[r for r in report['folds'] if r['candidate']==name and r.get('status')=='EVALUATED']
        stable=len(folds)==3 and all(r['evaluation']['alerts']>=20 and
               r['evaluation']['avg_net_return_pct']>0 for r in folds)
        report['candidates'][name]={'combined_development_evaluation':m,
                                  'positive_across_three_folds':stable,
                                  'eligible_for_frozen_final_test':bool(qualifies(m) and stable)}
    report['any_candidate_qualified']=any(r['eligible_for_frozen_final_test'] for r in report['candidates'].values())
    out=Path('_walkforward_v3');out.mkdir(exist_ok=True)
    (out/'development_report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    print('SUMMARY',json.dumps({k:v for k,v in report.items() if k!='folds'}),flush=True)


if __name__=='__main__':main()
