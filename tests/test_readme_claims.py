"""The README quotes numbers about this repo. They are checked here.

A count copied into prose is the copy that rots: the rule badge said 52 and the
test count said 963 while the suite had moved past both, and nothing failed. The
numbers a reader uses to decide whether to trust the tool should not be able to
drift from the tool, for the same reason docs/RULES.md is generated rather than
written. If one of these fails, fix the README — the code is right.
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.rules import RULES  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
README = os.path.join(HERE, "..", "README.md")
LEDGER = os.path.join(HERE, "..", "calibration", "verdicts.json")


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


class ReadmeCalibrationClaims(unittest.TestCase):
    """The false-positive rate is the number a client actually weighs.

    It is also the one most likely to rot, because it changes every time a rule
    is narrowed and nothing about editing a rule reminds anybody to edit the
    README. It already rotted once in the other direction: the summary rated a
    catalog that no longer existed, so the prose quoted 26.9% while the catalog
    it described emitted no false positives at all.

    Only the LIFETIME figures are checked. The live rate needs the account
    exports, which are somebody's private data and are not in this repo — CI
    could not re-derive it if it wanted to.
    """

    def setUp(self):
        with open(LEDGER, encoding="utf-8") as fh:
            self.rows = list(json.load(fh)["verdicts"].values())

    def test_the_lifetime_counts_match_the_ledger(self):
        text = readme()
        row = re.search(r"\|\s*Lifetime[^|]*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", text)
        self.assertIsNotNone(row, "the README no longer states lifetime counts")
        judged = [r for r in self.rows if r.get("verdict")]
        fp = [r for r in judged if r["verdict"] == "false_positive"]
        self.assertEqual(int(row.group(1)), len(judged),
                         "the README's lifetime judged count disagrees with "
                         "calibration/verdicts.json")
        self.assertEqual(int(row.group(2)), len(fp),
                         "the README's lifetime false-positive count disagrees "
                         "with calibration/verdicts.json")

    def test_nothing_in_the_ledger_is_unjudged(self):
        """The README says so, and an unjudged finding is not a passing one."""
        unjudged = [r for r in self.rows if not r.get("verdict")]
        self.assertEqual(
            [r["rule"] for r in unjudged], [],
            "the README claims nothing is unjudged; record a verdict or "
            "change the README")


if __name__ == "__main__":
    unittest.main()
