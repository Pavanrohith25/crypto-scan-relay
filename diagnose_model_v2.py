"""Diagnose the first run using development data only; preserve final holdout."""
import gzip
import json
from pathlib import Path
import pandas as pd


def main():
    root = Path('_prior_research/_ml_artifact_v2')
    report = json.loads((root / 'swing_model_v2_report.json').read_text())
    meta = json.loads((root / 'historical_futures_dataset_v2_meta.json').read_text())
    boundary = report['split']['test_start_ms'] - 72 * 3600000
    result = {'holdout_labels_accessed': False, 'development_end_exclusive_ms': boundary,
              'source_rows': meta['row_count'], 'entry_eligible_rows': meta['entry_eligible_rows'],
              'usable_contracts': len(meta['symbols_usable']), 'models': {}}
    for name, r in report['candidate_validation_reports'].items():
        rows = r['threshold_table']
        enough = [x for x in rows if x['alerts'] >= 100]
        active = [x for x in rows if x['alerts'] > 0]
        result['models'][name] = {
            'best_precision_at_100_alerts': max(enough, key=lambda x:x['precision']) if enough else None,
            'highest_precision_any_sample': max(active, key=lambda x:x['precision']) if active else None,
            'largest_sample': max(rows, key=lambda x:x['alerts']),
            'validation_brier': r['validation_brier'],
            'validation_average_precision': r['validation_average_precision']}
    columns = ['asof_ms','symbol','direction','market_regime','entry_eligible','retest_ready',
               'confirmation_15m','trigger_like_1h','anti_chase_abs_12h_pct','atr_1h_pct',
               'target5_before_stop1_0_48h','target5_before_stop1_5_48h',
               'target5_before_stop2_5_48h','target5_before_stop3_0_48h',
               'eventual_target5_48h','sim_return_target5_stop1_5_48h_pct',
               'mae_first_15m_pct','mae_first_60m_pct']
    rows = []
    with gzip.open(root / 'historical_futures_dataset_v2.jsonl.gz','rt') as f:
        for line in f:
            row = json.loads(line)
            # Only timestamp is inspected to enforce the boundary; no holdout
            # outcomes are included in diagnostics, grouping or selection.
            if row['asof_ms'] >= boundary:
                continue
            rows.append({c:row[c] for c in columns})
    df = pd.DataFrame(rows)
    target = 'target5_before_stop1_5_48h'
    def summarize(frame):
        labels = [c for c in columns if c.startswith('target5_before') or c=='eventual_target5_48h']
        return {'observations':len(frame), 'contracts':int(frame['symbol'].nunique()),
                'rates':{c:float(frame[c].mean()) for c in labels},
                'mean_return_after_cost_pct':float(frame['sim_return_target5_stop1_5_48h_pct'].mean()-.18)}
    eligible = df[df.entry_eligible==1]
    result['development_all'] = summarize(df)
    result['development_eligible'] = summarize(eligible)
    val = eligible[eligible.asof_ms >= report['split']['validation_start_ms']]
    result['validation_eligible'] = summarize(val)
    result['validation_by_direction_regime'] = {str(k):summarize(g) for k,g in val.groupby(['direction','market_regime'])}
    result['validation_by_atr'] = {str(k):summarize(g) for k,g in val.groupby(pd.cut(val.atr_1h_pct,[0,.5,1,2,4,100]),observed=True)}
    out = Path('_diagnosis');out.mkdir(exist_ok=True)
    (out/'diagnosis.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    (out/'source_model_report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__': main()
