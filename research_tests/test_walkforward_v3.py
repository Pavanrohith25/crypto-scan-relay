import unittest
import numpy as np
import pandas as pd
from research_entry_walkforward_v3 import delayed_retest_mask, fold_windows
from train_swing_model_v2 import HOUR


class WalkForwardTests(unittest.TestCase):
    def sequence(self, prices, direction='LONG'):
        return pd.DataFrame([{'asof_ms':i*900000,'symbol':'A','direction':direction,
            'market_regime':'MIXED','detection_price':p,'retest_ready':1,'confirmation_15m':int(i>=2),
            'anti_chase_abs_12h_pct':2.,'distance_from_15m_sma4_pct':.1}
            for i,p in enumerate(prices)])

    def test_wait_pullback_then_two_confirmations(self):
        df=self.sequence([100,99.5,99.6,99.7])
        self.assertEqual(delayed_retest_mask(df).tolist(),[False,False,False,True])
        short=self.sequence([100,100.5,100.4,100.3],'SHORT')
        self.assertEqual(delayed_retest_mask(short).tolist(),[False,False,False,True])

    def test_chasing_and_gaps_do_not_trigger(self):
        df=self.sequence([100,99.5,99.9,100.2])
        self.assertFalse(delayed_retest_mask(df).any())
        df=self.sequence([100,99.5,99.6,99.7])
        df.loc[2:,'asof_ms']+=900000
        self.assertFalse(delayed_retest_mask(df).any())

    def test_future_outcomes_have_no_effect(self):
        df=self.sequence([100,99.5,99.6,99.7])
        before=delayed_retest_mask(df)
        df['target5_before_stop1_5_48h']=[1,0,1,0]
        df['entry_price']=[500,10,999,1000]
        np.testing.assert_array_equal(before,delayed_retest_mask(df))

    def test_four_windows_have_purged_boundaries(self):
        df=pd.DataFrame({'asof_ms':np.arange(2000)*6*HOUR})
        for _,windows,_ in fold_windows(df):
            frames=[windows[k] for k in ['fit','calibration','selection','evaluation']]
            for a,b in zip(frames,frames[1:]):
                self.assertLess(a.asof_ms.max()+72*HOUR,b.asof_ms.min())


if __name__=='__main__':unittest.main()
