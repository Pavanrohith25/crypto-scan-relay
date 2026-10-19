import copy
import unittest
import paper_learning as p


class PaperLearningTests(unittest.TestCase):
    def setUp(self):
        self.state = p.initial_state(1_000_000)
        self.registry = {'BTCUSDT': {'classification': 'CRYPTO'}}
        self.event = {'event': 'SWING_CONTINUATION_TRIGGER', 'symbol': 'BTCUSDT',
                      'bias': 'LONG', 'timestamp_ms': 1_000_001,
                      'snapshot': {k: 1. for k in p.FEATURES}}

    def trade(self):
        return p.admit(self.state, [self.event], self.registry, 1_000_002)[0].copy()

    def bar(self, ts, high=102, low=99, op=100):
        return [ts, op, high, low, 100]

    def test_no_backfill_unknowns_or_missing_features(self):
        self.assertEqual(p.admit(self.state, [self.event], {}, 1_000_002), [])
        self.event['snapshot'].pop(p.FEATURES[0])
        self.assertEqual(p.admit(self.state, [self.event], self.registry, 1_000_002), [])
        self.event['timestamp_ms'] = 999_999
        self.assertEqual(p.admit(self.state, [self.event], self.registry, 1_000_002), [])

    def test_prediction_precedes_entry_and_deduplicates(self):
        t = self.trade()
        self.assertGreater(t['entry_ms'], t['predicted_ms'])
        self.assertIsNone(t['score'])
        self.assertEqual(p.admit(self.state, [self.event], self.registry, 1_000_003), [])

    def test_diagnostics_deduplicate_and_do_not_change_admission(self):
        log = []
        p.admit(self.state, [self.event], {}, 1_000_002, log)
        self.assertEqual(log[-1]['reason'], 'IDENTITY_NOT_VERIFIED_CRYPTO')
        p.admit(self.state, [self.event], {}, 1_000_003, log)
        self.assertEqual(len(log), 1)
        self.assertEqual(len(p.admit(self.state, [self.event], self.registry, 1_000_004, log)), 1)
        self.assertEqual(log[-1]['reason'], 'ACCEPTED')
        p.admit(self.state, [self.event], self.registry, 2_000_000, log)
        self.assertEqual(len(log), 2)

    def test_registry_expansion_never_replays_stale_signal(self):
        log = []
        p.admit(self.state, [self.event], {}, 1_000_002, log)
        self.assertEqual(p.admit(self.state, [self.event], self.registry, 1_600_002, log), [])
        self.assertEqual(log[-1]['reason'], 'STALE_AT_OBSERVATION')
        self.assertEqual(self.state['trades'], {})

    def test_open_position_and_cooldown_reasons(self):
        t = self.trade()
        event = copy.deepcopy(self.event)
        event['timestamp_ms'] += 1
        log = []
        p.admit(self.state, [event], self.registry, 1_000_003, log)
        self.assertEqual(log[-1]['reason'], 'POSITION_ALREADY_OPEN')
        self.state['trades'][t['id']]['status'] = 'STOP'
        p.admit(self.state, [event], self.registry, 1_000_004, log)
        self.assertEqual(log[-1]['reason'], 'SYMBOL_COOLDOWN')

    def test_diagnostic_reasons_and_persistence(self):
        import json
        for change, reason in [({'timestamp_ms': 999_999}, 'PRE_ACTIVATION'),
                               ({'timestamp_ms': 2_000_000}, 'FUTURE_TIMESTAMP'),
                               ({'bias': 'INVALID'}, 'INVALID_SIDE'),
                               ({'snapshot': {}}, 'INVALID_FEATURES')]:
            state = p.initial_state(1_000_000)
            event = {**self.event, **change}
            log = []
            p.admit(state, [event], self.registry, 1_000_002, log)
            self.assertEqual(log[-1]['reason'], reason)
            restored = json.loads(json.dumps(state))
            p.admit(restored, [event], self.registry, 1_000_003, log)
            self.assertEqual(len(log), 1)
            self.assertEqual(p.report(restored, 1_000_003)['admission_diagnostics']['latest_reason_counts'], {reason: 1})

    def test_closed_contiguous_bars_required(self):
        t = self.trade(); ts = t['entry_ms']
        self.assertFalse(p.advance(t, [self.bar(ts+ p.BAR, 110)], ts+3*p.BAR))
        self.assertEqual(t['status'], 'PENDING')
        self.assertFalse(p.advance(t, [self.bar(ts, 110)], ts+p.BAR-1))
        self.assertEqual(t['status'], 'PENDING')
        self.assertTrue(p.advance(t, [self.bar(ts, 110)], ts+p.BAR))
        self.assertEqual(t['status'], 'TARGET')

    def test_ambiguous_is_loss_and_stops_first_exit(self):
        t = self.trade(); ts = t['entry_ms']
        self.assertTrue(p.advance(t, [self.bar(ts, 106, 93), self.bar(ts+p.BAR, 120)], ts+2*p.BAR))
        self.assertEqual(t['status'], 'AMBIGUOUS_LOSS')
        self.assertAlmostEqual(t['net_return_pct'], -6.13)
        self.assertEqual(t['next_bar_ms'], ts+p.BAR)

    def test_gap_stop_fills_worse_than_stop(self):
        t = self.trade(); ts=t['entry_ms']
        p.advance(t, [self.bar(ts)], ts+p.BAR)
        self.assertTrue(p.advance(t, [[ts+p.BAR, 90, 106, 89, 100]], ts+2*p.BAR))
        self.assertEqual(t['status'], 'STOP')
        self.assertAlmostEqual(t['net_return_pct'], -10.13)

    def test_short_target(self):
        self.event['bias']='SHORT'
        t=self.trade(); ts=t['entry_ms']
        self.assertTrue(p.advance(t, [self.bar(ts, 101, 94)], ts+p.BAR))
        self.assertEqual(t['status'], 'TARGET')

    def test_updates_separate_models_not_old_predictions(self):
        self.state['models']['LONG']['n']=30
        t=self.trade(); old=copy.deepcopy(t)
        p.learn(self.state['models']['LONG'], t['features'], 1)
        self.assertGreater(p.predict(self.state['models']['LONG'], t['features']), .5)
        self.assertEqual(t, old)
        self.assertEqual(self.state['models']['SHORT']['n'], 0)

    def test_no_promotion_even_with_perfect_training(self):
        self.state['models']['LONG']['n']=1000
        r=p.report(self.state, 2_000_000)
        self.assertEqual(r['promotion'], 'BLOCKED')
        self.assertIsNone(r['panels']['LONG']['prequential_brier'])


if __name__ == '__main__':
    unittest.main()
