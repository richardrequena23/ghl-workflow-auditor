"""The scale, checked from both ends.

A curve is only honest if it is argued with from the good end as well as the
bad one, so these live next to the rule tests rather than inside them.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import run_all  # noqa: E402
from ghlaudit.score import health  # noqa: E402


class ScaleCalibration(unittest.TestCase):
    """Does the grade mean anything?

    Every number in this file is an argument about a curve, and a curve that
    hands an F to a well-built account is not strict, it is broken — a tool that
    grades everyone the same way discriminates nothing and gets ignored. So the
    scale is checked from the good end as well as the bad one: a clean account
    has to come out at the top, a defect has to move the number, and size must
    not decide the grade on its own.
    """

    @staticmethod
    def _clean(n, weak=0):
        """`n` workflows with nothing wrong; `weak` of them get one medium."""
        good = ("Hi {{contact.first_name | there}}, it's Acme Plumbing — thanks "
                "for reaching out. Reply STOP to opt out.")
        poor = "Hi {{contact.first_name}}, it's Acme Plumbing — thanks. Reply STOP to opt out."
        return {"workflows": [
            {"id": f"w{i}", "name": f"Campaign {i}", "status": "published",
             "triggers": [{"type": "form_submitted",
                           "filters": [{"operator": "==", "field": "form.id",
                                        "value": f"form-{i}"}]}],
             "steps": [{"id": f"s{i}", "type": "sms", "name": "Hello",
                        "meta": {"message": poor if i < weak else good}}]}
            for i in range(n)]}

    def _score(self, bundle):
        acct = Account.load(bundle)
        findings, skips = run_all(acct)
        return health(findings, skips, len(list(acct.published()))), findings

    def test_a_clean_account_finds_nothing_and_grades_at_the_ceiling(self):
        h, findings = self._score(self._clean(13))
        self.assertEqual(findings, [])
        # 90, not 100: the coverage ceiling still applies, because a bundle of
        # workflows alone leaves nine checks with nothing to look at.
        self.assertEqual(h.grade, "A")

    def test_the_grade_falls_as_real_defects_are_added(self):
        grades = [self._score(self._clean(13, weak=w))[0].score
                  for w in (0, 3, 6, 13)]
        self.assertEqual(grades, sorted(grades, reverse=True))
        self.assertGreater(grades[0] - grades[-1], 10,
                           "thirteen real defects have to move the number")

    def test_size_alone_does_not_decide_the_grade(self):
        """A big clean account and a small one are both well built."""
        scores = {n: self._score(self._clean(n))[0].score for n in (1, 5, 13, 30)}
        self.assertEqual(len(set(scores.values())), 1, scores)


if __name__ == "__main__":
    unittest.main()
