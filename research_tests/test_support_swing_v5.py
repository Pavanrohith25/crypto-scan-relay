import unittest
from research_tests.test_support_swing_v4 import bars
from research_support_swing_v5 import label

class HorizonTests(unittest.TestCase):
    def test_later_target_does_not_count(self):
        a=bars(96);a[24]['high']=106
        r=label('LONG',97,a[:24])
        self.assertEqual(r['path'],'TIMEOUT')
        self.assertEqual(r['holding_hours'],24)
        self.assertIsNone(label('LONG',97,a))

    def test_last_hour_target_counts(self):
        a=bars(24);a[23]['high']=106
        self.assertEqual(label('LONG',97,a)['win'],1)

    def test_stop_before_later_target(self):
        a=bars(24);a[0]['low']=96;a[23]['high']=106
        self.assertEqual(label('LONG',97,a)['path'],'STOP_FIRST')
