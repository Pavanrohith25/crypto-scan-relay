import unittest
from paper_learning_telegram import eligible,message

class PaperTelegramTests(unittest.TestCase):
    def setUp(self):
        self.t={'id':'X-1','symbol':'XUSDT','side':'SHORT','predicted_ms':1000,'entry_ms':2000,'entry_price':100.,'status':'OPEN','target_pct':5,'stop_pct':6}
        self.c={'enabled_after_ms':1000,'max_entry_age_seconds':600,'max_entry_drift_pct':.5}
    def test_old_and_late_rejected(self):
        self.assertFalse(eligible(self.t,self.c,603000,100)[0])
        self.c['enabled_after_ms']=1001
        self.assertFalse(eligible(self.t,self.c,3000,100)[0])
    def test_drift_and_invalid_quotes_rejected(self):
        for price in (101,99,float('nan'),0):self.assertFalse(eligible(self.t,self.c,3000,price)[0])
        self.assertTrue(eligible(self.t,self.c,3000,100.1)[0])
    def test_closed_not_new_signal(self):
        self.t['status']='TARGET'
        self.assertFalse(eligible(self.t,self.c,3000,100)[0])
    def test_message_is_explicitly_paper(self):
        s=message(self.t,100)
        for text in ('EXPERIMENTAL PAPER','unproven','95','106','No orders'):self.assertIn(text,s)
