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
