import unittest
from unittest.mock import patch
from research_tests.test_support_swing_v4 import bars
from research_support_swing_v4 import four_hour, HOUR
from research_coinglass_4h_v8 import label, evaluate


class FourHourTests(unittest.TestCase):
    def test_ambiguous_short_is_loss(self):
        a = bars(240); a[0]['low'] = 94; a[0]['high'] = 107
        r = label(four_hour(a), 0, 240*HOUR)
        self.assertEqual(r['path'], 'AMBIGUOUS')
        self.assertEqual(r['win'], 0)

    def test_gap_does_not_allow_later_win(self):
        a = four_hour(bars(240)); a[2:]['open_time_ms'] += 4*HOUR
        a[3]['low'] = 90
        r = label(a, 0, 244*HOUR)
        self.assertEqual(r['path'], 'DATA_GAP')
        self.assertEqual(r['exit_ms'], 244*HOUR)

    def test_warmup_cannot_enter_and_unresolved_blocks_slot(self):
        a = four_hour(bars(320))
        with patch('research_coinglass_4h_v8.breakout', return_value={'eligible':True}):
            rows = evaluate({'BTCUSDT':a}, 280*HOUR, 320*HOUR)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['entry_ms'], 280*HOUR)
        self.assertFalse(rows[0]['resolved'])

    def test_gap_through_stop_fills_worse(self):
        a = four_hour(bars(240)); a[1]['open'] = 109; a[1]['high'] = 110
        self.assertAlmostEqual(label(a, 0, 240*HOUR)['gross_return_pct'], -9)
