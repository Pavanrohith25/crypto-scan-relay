import copy
import unittest
from telegram_rewards import new_state,reconcile,forecast,update_model,report

class RewardTests(unittest.TestCase):
    def setUp(self):
        self.s=new_state();self.tel={'sent':{'X':200},'reward_predictions':{'X':{'model_n':0,'expected_r':0}}}
        self.t={'X':{'symbol':'XUSDT','side':'SHORT','status':'STOP','resolved_bar_ms':300,'label_available_ms':400,'stop_pct':6,'net_return_pct':-6.13,'stressed_net_return_pct':-6.51,'features':[1.,.2,0,0,0,0,.5],'snapshot':{},'mae_pct':6.2,'mfe_pct':1,'holding_hours':2}}
    def test_loss_penalizes_once(self):
        r=reconcile(self.s,self.t,self.tel,100,500)
        self.assertAlmostEqual(r[0]['reward_r'],-6.13/6)
        self.assertLess(forecast(self.s['models']['SHORT'],self.t['X']['features']),0)
        prior=copy.deepcopy(self.s)
        self.assertEqual(reconcile(self.s,self.t,self.tel,100,600),[])
        self.assertEqual(prior,self.s)
        self.assertEqual(self.s['models']['LONG']['n'],0)
    def test_win_reward_costs(self):
        self.t['X'].update(status='TARGET',net_return_pct=4.87)
        r=reconcile(self.s,self.t,self.tel,100,500)
        self.assertAlmostEqual(r[0]['reward_r'],4.87/6)
        self.assertGreater(forecast(self.s['models']['SHORT'],self.t['X']['features']),0)
    def test_no_reward_unsent_old_open_or_future(self):
        for tel,cutoff,status,now in [({'sent':{}},100,'STOP',500),(self.tel,201,'STOP',500),(self.tel,100,'OPEN',500),(self.tel,100,'STOP',399)]:
            self.t['X']['status']=status
            self.assertEqual(reconcile(new_state(),self.t,tel,cutoff,now),[])
    def test_pre_notification_bar_excluded(self):
        self.t['X']['resolved_bar_ms']=199
        self.assertEqual(reconcile(self.s,self.t,self.tel,100,500),[])
        self.assertIn('excluded',self.s['records']['X'])
        self.assertEqual(self.s['models']['SHORT']['n'],0)
    def test_forecast_snapshot_frozen(self):
        original=copy.deepcopy(self.tel)
        reconcile(self.s,self.t,self.tel,100,500)
        self.assertEqual(self.tel,original)
        r=report(self.s,self.t,self.tel,100,500)
        self.assertEqual(r['promotion'],'BLOCKED')
        self.assertIsNone(r['forward_squared_error'])
    def test_gap_loss_not_capped_in_ledger(self):
        self.t['X']['net_return_pct']=-18.13
        r=reconcile(self.s,self.t,self.tel,100,500)
        self.assertLess(r[0]['reward_r'],-3)
