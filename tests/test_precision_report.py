"""The calibration tool measures the catalog, so something has to measure it.

It had no tests. That is a gap with teeth: this is the script that produces the
false-positive number, and if it lies the whole catalog's credibility goes with
it. It lied in two ways before these tests existed — it scored a version of the
catalog that no longer ran, and it lost a verdict whenever a finding was
reworded.
"""

import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from scripts import precision_report as pr  # noqa: E402


class Finding:
    """The three fields the ledger keys on, and a title that moves."""

    def __init__(self, rule, workflow, step, title):
        self.rule, self.workflow, self.step, self.title = rule, workflow, step, title


class Keys(unittest.TestCase):
    def test_rewording_a_finding_changes_its_exact_key(self):
        a = Finding("GHL018", "Referral Request", "", "No workflow adds 'job-complete'")
        b = Finding("GHL018", "Referral Request", "", "Published, sends 2 messages")
        self.assertNotEqual(pr.key_of("acct", a), pr.key_of("acct", b))

    def test_rewording_a_finding_does_not_change_its_soft_key(self):
        """The whole point: a verdict survives a better sentence."""
        a = Finding("GHL018", "Referral Request", "", "No workflow adds 'job-complete'")
        b = Finding("GHL018", "Referral Request", "", "Published, sends 2 messages")
        self.assertEqual(pr.soft_key_of("acct", a), pr.soft_key_of("acct", b))

    def test_the_soft_key_still_separates_different_findings(self):
        base = Finding("GHL025", "Speed to Lead", "Backup email", "t")
        other_rule = Finding("GHL018", "Speed to Lead", "Backup email", "t")
        other_wf = Finding("GHL025", "No Show Recovery", "Backup email", "t")
        other_step = Finding("GHL025", "Speed to Lead", "Last touch", "t")
        keys = {pr.soft_key_of("acct", f)
                for f in (base, other_rule, other_wf, other_step)}
        self.assertEqual(len(keys), 4)

    def test_the_soft_key_separates_accounts(self):
        f = Finding("GHL025", "Speed to Lead", "Backup email", "t")
        self.assertNotEqual(pr.soft_key_of("acct-a", f), pr.soft_key_of("acct-b", f))

    def test_a_ledger_row_keys_the_same_way_a_finding_does(self):
        f = Finding("GHL025", "Speed to Lead", "Backup email", "t")
        row = {"rule": "GHL025", "workflow": "Speed to Lead",
               "step": "Backup email", "title": "anything else"}
        self.assertEqual(pr.soft_key_of("acct", f),
                         pr.soft_key_of_row("acct", row))

    def test_a_row_with_a_null_step_matches_a_finding_with_none(self):
        f = Finding("GHL018", "Referral Request", None, "t")
        row = {"rule": "GHL018", "workflow": "Referral Request", "step": None}
        self.assertEqual(pr.soft_key_of("acct", f),
                         pr.soft_key_of_row("acct", row))


class LiveSummary(unittest.TestCase):
    """--live must rate the catalog that exists, not the one that used to."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger_path = os.path.join(self.tmp, "verdicts.json")
        self._real_ledger = pr.LEDGER
        pr.LEDGER = self.ledger_path

    def tearDown(self):
        pr.LEDGER = self._real_ledger

    def write(self, ledger):
        with io.open(self.ledger_path, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh)

    def run_summary(self, live, emitted):
        """Run summary() with _current_keys stubbed to a known emission."""
        real = pr._current_keys
        pr._current_keys = lambda ledger: emitted
        out = io.StringIO()
        real_stdout = sys.stdout
        sys.stdout = out
        try:
            pr.summary(live=live)
        finally:
            sys.stdout = real_stdout
            pr._current_keys = real
        return out.getvalue()

    def test_a_fixed_rule_leaves_the_lifetime_rate_untouched(self):
        """The defect this was written for, stated as a test."""
        ledger = {"accounts": {"a": {"path": "/x", "workflows": 1}},
                  "verdicts": {
                      "old": {"rule": "GHL007", "account": "a", "workflow": "W",
                              "step": "s", "title": "t", "severity": "low",
                              "verdict": "false_positive", "note": ""},
                      "now": {"rule": "GHL041", "account": "a", "workflow": "W",
                              "step": "s2", "title": "t2", "severity": "high",
                              "verdict": "real", "note": ""}}}
        self.write(ledger)
        text = self.run_summary(live=False, emitted=None)
        self.assertIn("50.0%", text)

    def test_live_drops_a_finding_the_catalog_no_longer_emits(self):
        ledger = {"accounts": {"a": {"path": "/x", "workflows": 1}},
                  "verdicts": {
                      "old": {"rule": "GHL007", "account": "a", "workflow": "W",
                              "step": "s", "title": "t", "severity": "low",
                              "verdict": "false_positive", "note": ""},
                      "now": {"rule": "GHL041", "account": "a", "workflow": "W",
                              "step": "s2", "title": "t2", "severity": "high",
                              "verdict": "real", "note": ""}}}
        self.write(ledger)
        text = self.run_summary(live=True, emitted=({"now"}, set(), ["a"], []))
        self.assertIn("LIVE: 0 false positives", text)
        self.assertIn("RETIRED", text)
        self.assertIn("GHL007", text)

    def test_a_missing_export_is_reported_not_assumed(self):
        """Neither counted nor dismissed — the export simply is not there."""
        ledger = {"accounts": {"gone": {"path": "/nope", "workflows": 1},
                               "here": {"path": "/x", "workflows": 1}},
                  "verdicts": {
                      "lost": {"rule": "GHL007", "account": "gone",
                               "workflow": "W", "step": "s", "title": "t",
                               "severity": "low", "verdict": "false_positive",
                               "note": ""},
                      "kept": {"rule": "GHL041", "account": "here",
                               "workflow": "W", "step": "s2", "title": "t2",
                               "severity": "high", "verdict": "real",
                               "note": ""}}}
        self.write(ledger)
        text = self.run_summary(
            live=True, emitted=({"kept"}, set(), ["here"], ["gone"]))
        self.assertIn("UNVERIFIABLE", text)
        self.assertIn("gone", text)
        self.assertIn("LIVE: 0 false positives in 1 judged", text)

    def test_a_reworded_finding_keeps_its_verdict_and_is_counted_once(self):
        """Two ledger rows, one live finding — the verdict must not vote twice."""
        row = {"rule": "GHL018", "account": "a", "workflow": "Referral",
               "step": "", "severity": "high", "verdict": "real", "note": ""}
        old = dict(row, title="No workflow adds 'job-complete'")
        new = dict(row, title="Published, sends 2 messages")
        ledger = {"accounts": {"a": {"path": "/x", "workflows": 1}},
                  "verdicts": {"old": old, "new": new}}
        self.write(ledger)
        soft = pr.soft_key_of_row("a", new)
        text = self.run_summary(live=True, emitted=({"new"}, {soft}, ["a"], []))
        self.assertIn("LIVE: 0 false positives in 1 judged", text)
        self.assertNotIn("judged REAL and have stopped firing", text)
        self.assertIn("SUPERSEDED", text)

    def test_a_rule_that_goes_quiet_on_a_real_finding_is_flagged(self):
        """A narrowing that silences a true problem is a regression."""
        ledger = {"accounts": {"a": {"path": "/x", "workflows": 1}},
                  "verdicts": {
                      "quiet": {"rule": "GHL025", "account": "a",
                                "workflow": "Speed to Lead", "step": "Backup",
                                "title": "t", "severity": "medium",
                                "verdict": "real", "note": ""},
                      "kept": {"rule": "GHL041", "account": "a", "workflow": "W",
                               "step": "s2", "title": "t2", "severity": "high",
                               "verdict": "real", "note": ""}}}
        self.write(ledger)
        text = self.run_summary(live=True, emitted=({"kept"}, set(), ["a"], []))
        self.assertIn("judged REAL and have stopped firing", text)
        self.assertIn("GHL025", text)


if __name__ == "__main__":
    unittest.main()
