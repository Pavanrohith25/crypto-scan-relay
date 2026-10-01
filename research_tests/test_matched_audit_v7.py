import unittest
from research_tests.test_support_swing_v4 import bars
from research_matched_audit_v7 import outcome,select_and_label,loss_audit,HOUR

class MatchedAuditTests(unittest.TestCase):
    def test_stop_then_recovery_remains_loss(self):
        a=bars(120);a[0]['low']=96;a[40]['high']=106
        r=outcome(a,0,'LONG',3,120*HOUR)
        self.assertEqual(r['path'],'STOP_FIRST')
        self.assertEqual(r['post_stop_96h'],'RECOVERED_TO_ORIGINAL_TARGET')
        self.assertEqual(r['win'],0)

    def test_incomplete_followup_not_nonrecovery(self):
        a=bars(24);a[0]['low']=96
        r=outcome(a,0,'LONG',3,24*HOUR)
        self.assertEqual(r['post_stop_96h'],'INSUFFICIENT_FOLLOWUP')
        self.assertIsNone(loss_audit([r])['recovery_fraction_complete'])

    def test_gap_censors_before_later_target(self):
        a=bars(120);a[10:]['open_time_ms']+=HOUR;a[40]['high']=106
        r=outcome(a,0,'LONG',3,121*HOUR)
        self.assertEqual(r['path'],'DATA_GAP')
        self.assertEqual(r['exit_ms'],121*HOUR)

    def test_common_stop_panels_and_short(self):
        a=bars(120);a[0]['low']=96;a[40]['high']=106
        self.assertEqual(outcome(a,0,'LONG',3,120*HOUR)['win'],0)
        self.assertEqual(outcome(a,0,'LONG',6,120*HOUR)['win'],1)
        a=bars(120);a[40]['low']=94
        self.assertEqual(outcome(a,0,'SHORT',6,120*HOUR)['win'],1)

    def test_tie_break_liquidity_and_unresolved_occupancy(self):
        a=bars(120)
        common={'direction':'LONG','entry_ms':0,'bar_index':0}
        rows=[dict(common,symbol='A',quote_volume_24h=20),dict(common,symbol='B',quote_volume_24h=30)]
        selected=select_and_label(rows,{'A':a,'B':a},3,120*HOUR)
        self.assertEqual([r['symbol'] for r in selected],['B'])
        self.assertFalse(selected[0]['resolved'])

    def test_ambiguity_and_gap_through_stop(self):
        a=bars(120);a[1]['low']=90;a[1]['high']=106;a[1]['open']=94
        r=outcome(a,0,'LONG',3,120*HOUR)
        self.assertEqual(r['path'],'AMBIGUOUS')
        self.assertAlmostEqual(r['gross_return_pct'],-6.)


class EndToEndAdapterTests(unittest.TestCase):
    def test_main_uses_timestamped_candles_and_writes_all_panels(self):
        import contextlib
        import io
        import json
        import os
        import tempfile
        from pathlib import Path
        from research_matched_audit_v7 import main
        # Exercise the real main -> adapter -> feature_core path that failed in CI.
        with tempfile.TemporaryDirectory() as folder:
            old_cwd=os.getcwd()
            try:
                os.chdir(folder)
                model=Path('_prior_research/_ml_artifact_v2');model.mkdir(parents=True)
                raw=Path('_prior_research/_futures_raw_v2');raw.mkdir(parents=True)
                cache=Path('_support_raw_v4');cache.mkdir()
                symbols=['BTCUSDT']+[f'TEST{i:03}USDT' for i in range(149)]
                (model/'swing_model_v2_report.json').write_text(json.dumps({'split':{'test_start_ms':(264+120)*HOUR}}))
                (raw/'manifest.json').write_text(json.dumps({'start_ms':0,'eligible_symbols':symbols}))
                a=bars(244)
                for symbol in symbols:
                    import numpy as np
                    np.save(cache/f'{symbol}.npy',a)
                with contextlib.redirect_stdout(io.StringIO()):main()
                report=json.loads(Path('_matched_v7/report.json').read_text())
                self.assertEqual(report['contracts'],150)
                self.assertEqual(len(report['comparisons']),4)
                self.assertEqual(report['holdout_evaluations'],0)
                self.assertEqual(len(list(Path('_matched_v7').glob('*_trades.csv'))),4)
            finally:
                os.chdir(old_cwd)
