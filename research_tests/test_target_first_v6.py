import unittest
from research_tests.test_support_swing_v4 import bars
from research_target_first_v6 import label, metrics

class TargetFirstTests(unittest.TestCase):
    def test_target_after_four_days(self):
        a=bars(150);a[130]['high']=106
        r=label('LONG',97,a)
        self.assertEqual(r['win'],1)
        self.assertEqual(r['holding_hours'],131)

    def test_stop_before_recovery_is_loss(self):
        a=bars(150);a[0]['low']=96;a[130]['high']=106
        self.assertEqual(label('LONG',97,a)['path'],'STOP_FIRST')

    def test_unresolved_in_denominator(self):
        a=bars(48);r=label('LONG',97,a)
        self.assertFalse(r['resolved'])
        a[30]['high']=106;win=label('LONG',97,a)
        m=metrics([r,win])
        self.assertEqual(m['target_fraction_all'],.5)
        self.assertEqual(m['resolved_win_rate'],1.)
        self.assertEqual(m['eventual_success_bounds'],[.5,1.])

    def test_missing_data_censors_before_later_target(self):
        a=bars(48);a[20:]['open_time_ms']+=3600000;a[30]['high']=106
        self.assertEqual(label('LONG',97,a)['path'],'DATA_GAP')

    def test_short_and_ambiguity(self):
        a=bars(48);a[5]['low']=94
        self.assertEqual(label('SHORT',103,a)['win'],1)
        a[5]['high']=104
        self.assertEqual(label('SHORT',103,a)['path'],'AMBIGUOUS')

    def test_gap_fill_and_holding_cost(self):
        a=bars(48);a[0]['open']=100;a[1]['open']=95;a[1]['low']=95
        self.assertAlmostEqual(label('LONG',97,a)['gross_return_pct'],-5)
        self.assertGreater(label('LONG',97,bars(48))['cost_pct'],label('LONG',97,bars(8))['cost_pct'])
