import unittest
from guardian import Guardian
class GuardianScoutTests(unittest.TestCase):
 def test_scout_valueerror_not_quota(self):
  x=Guardian().observe('[scout] invalid_candidate window=1-2 action=drop reason=ValueError'); self.assertNotEqual(x['category'],'quota')
