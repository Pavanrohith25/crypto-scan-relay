import unittest
from research_tests.test_support_swing_v4 import bars
from research_crypto_v34_v9 import verified_crypto, candidates, HOUR
from research_matched_audit_v7 import select_and_label


class CryptoReplayTests(unittest.TestCase):
    def test_unknown_and_unsourced_are_blocked(self):
        r={'contracts':{'A':{'classification':'CRYPTO','sources':[]},
                        'B':{'classification':'NON_CRYPTO','sources':['official']}}}
        for s in ('A','B','C'):
            self.assertFalse(verified_crypto(r,s))

    def test_filter_before_selection_refills_occupied_slot(self):
        a=bars(120)
        registry={'contracts':{'CRYPTO':{'classification':'CRYPTO','sources':['official']}}}
        events=[dict(symbol=s,direction='SHORT',entry_ms=0,bar_index=0,quote_volume_24h=v)
                for s,v in [('EQUITY',30),('CRYPTO',20)]]
        data={'EQUITY':a,'CRYPTO':a}
        old=select_and_label(events,data,6,120*HOUR)
        self.assertEqual(old[0]['symbol'],'EQUITY')
        filtered=[r for r in events if verified_crypto(registry,r['symbol'])]
        new=select_and_label(filtered,data,6,120*HOUR)
        self.assertEqual(new[0]['symbol'],'CRYPTO')
        self.assertFalse(new[0]['resolved'])

    def test_no_future_bar_changes_prior_candidates(self):
        import contextlib,io
        a=bars(340)
        with contextlib.redirect_stdout(io.StringIO()):
            before=candidates({'BTCUSDT':a})
            changed=a.copy();changed[300:]['close']=105;changed[300:]['high']=107
            after=candidates({'BTCUSDT':changed})
        self.assertEqual([r for r in before if r['entry_ms']<=300*HOUR],
                         [r for r in after if r['entry_ms']<=300*HOUR])

    def test_registry_blocks_stock_contracts_and_preserves_delisted_crypto(self):
        import json
        from research_crypto_v34_v9 import IDENTITY
        r=json.loads(IDENTITY.read_text())
        for s in ('MSFTUSDT','MUUSDT','QQQUSDT','INTCUSDT','XAUUSDT'):
            self.assertFalse(verified_crypto(r,s))
        for s in ('BTCUSDT','ETHUSDT','WAVESUSDT','OMGUSDT','XEMUSDT','AERGOUSDT'):
            self.assertTrue(verified_crypto(r,s))
