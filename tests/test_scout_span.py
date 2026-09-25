import unittest
from scout_span import accept_span
class ScoutSpanTests(unittest.TestCase):
 def test_keep(self): self.assertEqual(accept_span(7452.62,7488.31,7452.5,7569.6,15000),(7452.62,7488.31,'keep'))
 def test_short_center(self):
  s,e,a=accept_span(5794.13,5798.05,5740,5900,12000); self.assertEqual(a,'expand_short'); self.assertLess(abs((s+e)/2-5796.09),1)
 def test_long(self):
  s,e,a=accept_span(100,250,90,260,1000); self.assertEqual(a,'clamp_long'); self.assertLessEqual(e-s,90.001)
 def test_relative(self): self.assertEqual(accept_span(10,40,1000,1120,2000),(1010.0,1040.0,'keep'))
 def test_ms(self): self.assertEqual(accept_span(1000000,1040000,1000,1120,2000),(1000.0,1040.0,'keep'))
 def test_vod_clamp(self):
  s,e,a=accept_span(11980,12020,11900,12030,12000); self.assertEqual(e,12000.0)
