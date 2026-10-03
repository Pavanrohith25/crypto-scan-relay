import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import experimental_telegram as mod

class ExperimentalTelegramTests(unittest.TestCase):
    def setUp(self):
        self.now=1800000000000
        self.cfg={'enabled':True,'enabled_after_ms':self.now-1000,'max_signal_age_seconds':600,'max_entry_drift_pct':.5}
        self.event={'event':'SWING_CONTINUATION_TRIGGER','symbol':'TESTUSDT','bias':'SHORT','trigger_price':100,'timestamp_ms':self.now}
        self.scan={'generated_at_ms':self.now,'analysis_pool':[{'symbol':'TESTUSDT','bias':'SHORT','setup_state':'CONTINUATION_TRIGGER','price':100,'quote_volume_24h':20000000}], 'market_context':{'btc_24h_change_pct':-1}}

    def test_fresh_short_only(self):
        self.assertTrue(mod.eligible(self.event,self.scan,self.cfg,{},self.now)[0])
        self.event['bias']='LONG'
        self.assertFalse(mod.eligible(self.event,self.scan,self.cfg,{},self.now)[0])

    def test_stale_and_pre_activation_events_blocked(self):
        for ts in [self.now-2000,self.now-700000,self.now+1]:
            self.event['timestamp_ms']=ts
            self.assertFalse(mod.eligible(self.event,self.scan,self.cfg,{},self.now)[0])

    def test_stale_price_and_missing_context_blocked(self):
        for change in ['stale','drift','missing','nan']:
            scan=copy.deepcopy(self.scan)
            if change=='stale':scan['generated_at_ms']-=700000
            if change=='drift':scan['analysis_pool'][0]['price']=99
            if change=='missing':scan['market_context']={}
            if change=='nan':scan['analysis_pool'][0]['price']=float('nan')
            self.assertFalse(mod.eligible(self.event,scan,self.cfg,{},self.now)[0])

    def test_daily_limit(self):
        self.assertFalse(mod.eligible(self.event,self.scan,self.cfg,{'last_alert_at_ms':self.now-1000},self.now)[0])

    def test_levels_and_no_claimed_probability(self):
        s=mod.format_message(self.event)
        self.assertIn('95.0000',s);self.assertIn('106.00',s)
        self.assertIn('EXPERIMENTAL',s);self.assertIn('unproven',s)
        self.assertNotIn('63.6',s)

    def test_send_dedup_and_activation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for file,data in [('config',self.cfg),('scan',self.scan)]:
                (root/file).write_text(json.dumps(data))
            (root/'events').write_text(json.dumps(self.event)+'\n')
            with patch.multiple(mod,CONFIG=root/'config',STATE=root/'state',EVENTS=root/'events',SCAN=root/'scan'),patch.dict(os.environ,TELEGRAM_BOT_TOKEN='test',TELEGRAM_CHAT_ID='test'),patch.object(mod.time,'time',return_value=self.now/1000),patch.object(mod,'late_entry_gate',return_value=(True,'test',None,None,None)),patch.object(mod,'send_telegram',return_value=True) as send:
                mod.main();mod.main()
                self.assertEqual(send.call_count,2) # activation plus exactly one setup
                state=json.loads((root/'state').read_text())
                self.assertEqual(len(state['sent_ids']),1)
