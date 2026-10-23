import unittest
import numpy as np
from historical_futures_backfill_v2 import DTYPE
from research_support_swing_v4 import HOUR, four_hour, label, portfolio


def bars(n=96):
    a=np.zeros(n,dtype=DTYPE)
    a['open_time_ms']=np.arange(n)*HOUR;a['close_time_ms']=a['open_time_ms']+HOUR-1
    for k in ['open','high','low','close']:a[k]=100.
    a['quote_volume']=1_000_000
    return a


class SupportSwingTests(unittest.TestCase):
    def test_target_on_day_four_counts(self):
        a=bars();a[80]['high']=106
        r=label('LONG',97,a)
        self.assertEqual(r['win'],1);self.assertEqual(r['holding_hours'],81)
        self.assertAlmostEqual(r['stop_pct'],3.)

    def test_structure_stop_is_not_fixed_1_5_percent(self):
        a=bars();a[0]['low']=98;a[80]['high']=106
        self.assertEqual(label('LONG',97,a)['win'],1)
        self.assertEqual(label('LONG',98.5,a)['win'],0)

    def test_same_hour_ambiguity_and_gap_through_stop_lose(self):
        a=bars();a[1]['low']=96;a[1]['high']=106
        self.assertEqual(label('LONG',97,a)['path'],'AMBIGUOUS')
        a=bars()
        for k in ['open','low','high','close']:a[1][k]=95
        self.assertAlmostEqual(label('LONG',97,a)['gross_return_pct'],-5.)

    def test_missing_hour_or_excessive_risk_rejected(self):
        a=bars();a[50:]['open_time_ms']+=HOUR
        self.assertIsNone(label('LONG',97,a))
        self.assertIsNone(label('LONG',90,bars()))

    def test_aggregation_uses_complete_four_hour_bars(self):
        a=bars(7)
        self.assertEqual(len(four_hour(a)),1)
        self.assertEqual(len(four_hour(np.delete(a,1))),0)

    def test_correlated_positions_not_stacked(self):
        rows=[{'symbol':'A','direction':'LONG','entry_ms':0,'exit_ms':96*HOUR,'estimated_reward_risk':2},
              {'symbol':'B','direction':'LONG','entry_ms':HOUR,'exit_ms':100*HOUR,'estimated_reward_risk':3}]
        self.assertEqual(len(portfolio(rows)),1)


if __name__=='__main__':unittest.main()
