"""The README quotes two numbers about this repo. Both are checked here.

A count copied into prose is the copy that rots: the rule badge said 52 and the
test count said 963 while the suite had moved past both, and nothing failed. The
numbers a reader uses to decide whether to trust the tool should not be able to
drift from the tool, for the same reason docs/RULES.md is generated rather than
written. If one of these fails, fix the README — the code is right.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.rules import RULES  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(HERE, "..", "README.md")


def readme():
    with open(README, encoding="utf-8") as fh:
        return fh.read()


class ReadmeCounts(unittest.TestCase):
    def test_the_rules_badge_matches_the_registry(self):
        text = readme()
        m = re.search(r"badge/rules-(\d+)-", text)
        self.assertIsNotNone(m, "the rules badge is gone from the README")
        self.assertEqual(
            int(m.group(1)), len(RULES),
            "the README rules badge disagrees with the rule registry")

    def test_every_rule_count_in_the_prose_matches(self):
        text = readme()
        claims = [int(n) for n in re.findall(r"\*\*all (\d+) rules\*\*", text)]
        self.assertTrue(claims, "the README no longer states a rule count")
        for claimed in claims:
            self.assertEqual(claimed, len(RULES))

    def test_the_test_count_matches_the_suite(self):
        text = readme()
        m = re.search(r"\*\*([\d,]+) tests\*\*", text)
        self.assertIsNotNone(m, "the README no longer states a test count")
        claimed = int(m.group(1).replace(",", ""))
        suite = unittest.defaultTestLoader.discover(HERE, top_level_dir=HERE)
        self.assertEqual(
            claimed, suite.countTestCases(),
            "the README test count disagrees with the suite that is actually here")


if __name__ == "__main__":
    unittest.main()
