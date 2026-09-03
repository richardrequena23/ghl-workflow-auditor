"""Scale pack: each rule gets a build that trips it and one that does not.

The good shapes matter more here than anywhere else in the catalog, because
every defect in this pack has a legitimate twin that looks almost identical in
an export: a retry ladder IS a loop, a poll IS the same call twice, a no-show
recovery SHOULD re-enroll, and a hand-off between two workflows is normal until
the second one points back. A check that cannot tell them apart would flag the
competent builds hardest, since those are the ones that use these patterns at
all.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import run, run_all  # noqa: E402

MINE = ("GHL089", "GHL090", "GHL091", "GHL092", "GHL093", "GHL094",
        "GHL104")

FRAGMENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "examples", "packs", "scale_performance.json")


def bundle(workflows, custom_values=None, **extra):
    data = {"workflows": workflows, "customValues": custom_values or {}}
    data.update(extra)
    return data


def audit(workflows, custom_values=None, **extra):
    return run(Account.load(bundle(workflows, custom_values, **extra)))


def audit_all(workflows, custom_values=None, **extra):
    return run_all(Account.load(bundle(workflows, custom_values, **extra)))


def rules_hit(workflows, custom_values=None, **extra):
    return {f.rule for f in audit(workflows, custom_values, **extra)}


def findings_for(rule_id, workflows, custom_values=None, **extra):
    return [f for f in audit(workflows, custom_values, **extra)
            if f.rule == rule_id]


def wf(name, steps, triggers=(), status="published", settings=None):
    return {"_id": name, "name": name, "status": status, "steps": list(steps),
            "triggers": list(triggers), "settings": settings or {}}


def sms(name="Message", body="hello", **extra):
    step = {"type": "sms", "name": name, "meta": {"body": body}}
    step.update(extra)
    return step


def call(name="Call the API", url="https://api.example.com/v1/thing",
         method="POST", **extra):
    step = {"type": "webhook", "name": name,
            "meta": {"url": url, "method": method}}
    step.update(extra)
    return step


def get(name="Look it up", url="https://api.example.com/v1/company", **extra):
    return call(name, url, method="GET", **extra)


def pause(name="Wait", delay="2 minutes", **extra):
    step = {"type": "wait", "name": name, "meta": {"delay": delay}}
    step.update(extra)
    return step


def goto(target, name="Go back", **extra):
    step = {"type": "goto", "name": name, "meta": {"targetStepId": target}}
    step.update(extra)
    return step


def branch(name="Route", **extra):
    step = {"type": "if_else", "name": name,
            "meta": {"conditions": [{"field": "contact.type"}]}}
    step.update(extra)
    return step


def enroll(target, name="Hand off"):
    return {"type": "add_to_workflow", "name": name,
            "meta": {"targetWorkflowId": target}}


TAG = [{"type": "contact_tag_added", "name": "Tagged",
        "filters": [{"tag": "start"}]}]
FORM = [{"type": "form_submitted", "name": "Form",
         "filters": [{"field": "formId", "value": "form_intake"}]}]


class UnpausedLoop(unittest.TestCase):
    """GHL089 — a cycle in the graph with nothing slowing it down."""

    def test_a_goto_loop_with_no_wait_fires(self):
        steps = [call("Push", id="a", next="b"),
                 branch("Did it work?", id="b", next="c"),
                 goto("a", id="c")]
        self.assertIn("GHL089", rules_hit([wf("Sync", steps, TAG)]))

    def test_a_loop_containing_a_send_is_critical(self):
        steps = [sms("Nudge", id="a", next="b"), goto("a", id="b")]
        found = findings_for("GHL089", [wf("Nudge", steps, TAG)])
        self.assertEqual([f.severity for f in found], ["critical"])

    def test_a_loop_with_no_send_is_high(self):
        steps = [call("Push", id="a", next="b"), goto("a", id="b")]
        found = findings_for("GHL089", [wf("Sync", steps, TAG)])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_a_wait_on_the_lap_passes(self):
        """A retry ladder is a loop. The wait is what makes it a build."""
        steps = [call("Push", id="a", next="b"),
                 pause("Wait 5 minutes", "5 minutes", id="b", next="c"),
                 goto("a", id="c")]
        self.assertNotIn("GHL089", rules_hit([wf("Sync", steps, TAG)]))

    def test_a_wait_of_zero_is_not_a_pause(self):
        steps = [call("Push", id="a", next="b"),
                 pause("No wait", "0 minutes", id="b", next="c"),
                 goto("a", id="c")]
        self.assertIn("GHL089", rules_hit([wf("Sync", steps, TAG)]))

    def test_a_conditional_wait_with_no_stated_length_counts_as_a_pause(self):
        """'Wait until they reply' has no length and still stops them."""
        steps = [sms("Ask", id="a", next="b"),
                 {"type": "wait", "name": "Wait for a reply", "id": "b",
                  "next": "c", "meta": {"waitType": "contact_reply",
                                        "delay": "unlimited"}},
                 goto("a", id="c")]
        self.assertNotIn("GHL089", rules_hit([wf("Ask", steps, TAG)]))

    def test_a_straight_line_workflow_passes(self):
        steps = [sms("One", id="a", next="b"), call("Push", id="b", next="c"),
                 sms("Two", id="c")]
        self.assertNotIn("GHL089", rules_hit([wf("Seq", steps, TAG)]))

    def test_a_goto_that_jumps_forward_is_not_a_loop(self):
        steps = [goto("c", name="Skip ahead", id="a"),
                 sms("Skipped", id="b"), sms("Landing", id="c")]
        self.assertNotIn("GHL089", rules_hit([wf("Skip", steps, TAG)]))

    def test_a_step_pointing_at_itself_fires(self):
        steps = [{"type": "webhook", "name": "Poll", "id": "a", "next": "a",
                  "meta": {"url": "https://api.example.com/v1/poll"}}]
        self.assertIn("GHL089", rules_hit([wf("Poll", steps, TAG)]))

    def test_a_cycle_with_no_goto_step_in_it_fires(self):
        """The loop is in the wiring, not in a keyword.

        A branch child wired back to an earlier node is the same infinite loop
        and carries no Go-To step for a text search to find.
        """
        steps = [sms("One", id="a", next="b"),
                 branch("Again?", id="b", next="c"),
                 call("Push", id="c", next="a")]
        self.assertIn("GHL089", rules_hit([wf("Circle", steps, TAG)]))

    def test_a_flat_export_with_a_goto_by_name_fires(self):
        steps = [call("Push it"), sms("Tell them"),
                 {"type": "goto", "name": "Again",
                  "meta": {"targetStepName": "Push it"}}]
        self.assertIn("GHL089", rules_hit([wf("Flat", steps, TAG)]))

    def test_an_n8n_wait_node_counts_as_a_pause(self):
        """The model knows GoHighLevel's wait types; n8n's is namespaced.

        Reading "n8n-nodes-base.wait" as a step that stops nobody would report
        every n8n retry loop in the account as a runaway.
        """
        steps = [call("Push", id="a", next="b"),
                 {"type": "n8n-nodes-base.wait", "name": "Wait", "id": "b",
                  "next": "c", "parameters": {"amount": 5, "unit": "minutes"}},
                 goto("a", id="c")]
        self.assertNotIn("GHL089", rules_hit([wf("Sync", steps, TAG)]))

    def test_draft_workflows_are_left_alone(self):
        steps = [sms("Nudge", id="a", next="b"), goto("a", id="b")]
        self.assertNotIn("GHL089",
                         rules_hit([wf("Nudge", steps, TAG, status="draft")]))


class RepeatedLookup(unittest.TestCase):
    """GHL090 — the same answer fetched more than once per contact."""

    URL = "https://enrich.example.com/v1/company?domain={{ contact.website }}"

    def test_three_identical_lookups_fire(self):
        steps = [get("One", self.URL), branch(), get("Two", self.URL),
                 get("Three", self.URL)]
        self.assertIn("GHL090", rules_hit([wf("Enrich", steps, FORM)]))

    def test_three_is_high_and_two_is_medium(self):
        two = findings_for("GHL090", [wf(
            "Enrich", [get("One", self.URL), get("Two", self.URL)], FORM)])
        three = findings_for("GHL090", [wf(
            "Enrich", [get("One", self.URL), get("Two", self.URL),
                       get("Three", self.URL)], FORM)])
        self.assertEqual([f.severity for f in two], ["medium"])
        self.assertEqual([f.severity for f in three], ["high"])

    def test_a_wait_between_them_is_a_poll_and_passes(self):
        steps = [get("Check status", self.URL), pause("Wait 5 minutes",
                                                      "5 minutes"),
                 get("Check again", self.URL)]
        self.assertNotIn("GHL090", rules_hit([wf("Poll", steps, FORM)]))

    def test_different_endpoints_pass(self):
        steps = [get("Company", self.URL),
                 get("People", "https://enrich.example.com/v1/people")]
        self.assertNotIn("GHL090", rules_hit([wf("Enrich", steps, FORM)]))

    def test_spacing_inside_a_merge_field_still_matches(self):
        steps = [get("One", "https://api.example.com/c/{{ contact.id }}"),
                 get("Two", "https://api.example.com/c/{{contact.id}}")]
        self.assertIn("GHL090", rules_hit([wf("Enrich", steps, FORM)]))

    def test_a_repeated_post_is_left_to_the_idempotency_rule(self):
        steps = [call("Push", "https://api.example.com/v1/orders"),
                 call("Push again", "https://api.example.com/v1/orders")]
        self.assertNotIn("GHL090", rules_hit([wf("Sync", steps, FORM)]))

    def test_a_call_with_no_declared_method_is_not_guessed_at(self):
        step = {"type": "webhook", "name": "Fetch",
                "meta": {"url": "https://api.example.com/v1/company"}}
        self.assertNotIn("GHL090", rules_hit([wf("Enrich", [step, dict(step)],
                                                FORM)]))

    def test_lookups_on_two_branches_of_one_condition_pass(self):
        """Only one of them runs, so nothing is fetched twice."""
        steps = [branch("Which?", id="b1"),
                 get("Yes path", self.URL, id="c1", parentKey="b1-yes"),
                 get("No path", self.URL, id="c2", parentKey="b1-no")]
        self.assertNotIn("GHL090", rules_hit([wf("Route", steps, FORM)]))

    def test_one_lookup_passes(self):
        self.assertNotIn("GHL090", rules_hit(
            [wf("Enrich", [get("One", self.URL), sms()], FORM)]))


class LockstepRetries(unittest.TestCase):
    """GHL091 — every contact comes back at the same instant."""

    def ladder(self, delay="2 minutes", **wait_extra):
        return [call("Reserve stock", id="a", next="b"),
                pause("Wait", delay, id="b", next="c", **wait_extra),
                branch("Under 3 tries?", id="c", next="d"),
                goto("a", id="d")]

    def test_a_fixed_delay_retry_ladder_fires(self):
        self.assertIn("GHL091", rules_hit([wf("Stock", self.ladder(), FORM)]))

    def test_a_short_delay_is_high(self):
        found = findings_for("GHL091", [wf("Stock", self.ladder(), FORM)])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_an_hour_or_more_between_waves_is_medium(self):
        found = findings_for("GHL091",
                             [wf("Stock", self.ladder("6 hours"), FORM)])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_a_named_backoff_passes(self):
        steps = self.ladder()
        steps[1]["name"] = "Exponential backoff"
        self.assertNotIn("GHL091", rules_hit([wf("Stock", steps, FORM)]))

    def test_a_delay_built_from_a_field_passes(self):
        """A computed wait is a backoff whatever it is called."""
        steps = self.ladder("{{ contact.retry_delay }}")
        self.assertNotIn("GHL091", rules_hit([wf("Stock", steps, FORM)]))

    def test_a_loop_with_no_external_call_passes(self):
        steps = [sms("Nudge", id="a", next="b"),
                 pause("Wait", "2 minutes", id="b", next="c"),
                 goto("a", id="c")]
        self.assertNotIn("GHL091", rules_hit([wf("Nudge", steps, FORM)]))

    def test_a_call_that_is_not_in_a_loop_passes(self):
        steps = [call("Reserve stock", id="a", next="b"),
                 pause("Wait", "2 minutes", id="b", next="c"),
                 sms("Confirm", id="c")]
        self.assertNotIn("GHL091", rules_hit([wf("Stock", steps, FORM)]))

    def test_a_bounded_ladder_still_fires(self):
        """Bounded and backed off are different properties.

        An attempt counter stops one contact looping forever; it does nothing
        about a hundred contacts returning on the same second.
        """
        steps = self.ladder()
        steps[2]["name"] = "attempt_count under 3?"
        self.assertIn("GHL091", rules_hit([wf("Stock", steps, FORM)]))

    def test_a_wait_written_as_value_and_unit_is_read(self):
        steps = self.ladder()
        steps[1]["meta"] = {"value": 90, "unit": "minutes"}
        found = findings_for("GHL091", [wf("Stock", steps, FORM)])
        self.assertEqual([f.severity for f in found], ["medium"])


class EnrolmentPileup(unittest.TestCase):
    """GHL092 — the same contact inside one sequence several times over."""

    LONG = [sms("Day 0"), pause("Wait 10 days", "10 days"), sms("Day 10"),
            pause("Wait 20 days", "20 days"), sms("Day 30")]
    ON = {"allowReentry": True}

    def test_a_long_reenterable_sequence_fires(self):
        self.assertIn("GHL092", rules_hit(
            [wf("Nurture", self.LONG, FORM, settings=self.ON)]))

    def test_two_sends_after_the_wait_is_high(self):
        found = findings_for("GHL092", [wf("Nurture", self.LONG, FORM,
                                           settings=self.ON)])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_one_send_after_the_wait_is_medium(self):
        steps = [sms("Day 0"), pause("Wait 10 days", "10 days"), sms("Day 10")]
        found = findings_for("GHL092",
                             [wf("Nurture", steps, FORM, settings=self.ON)])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_reentry_off_passes(self):
        off = {"allowReentry": False}
        self.assertNotIn("GHL092", rules_hit(
            [wf("Nurture", self.LONG, FORM, settings=off)]))

    def test_a_short_sequence_passes(self):
        steps = [sms("One"), pause("Wait 2 hours", "2 hours"), sms("Two")]
        self.assertNotIn("GHL092", rules_hit(
            [wf("Speed to lead", steps, FORM, settings=self.ON)]))

    def test_an_appointment_trigger_passes(self):
        """A second appointment deserves a second reminder ladder."""
        appt = [{"type": "customer_booked_appointment", "name": "Booked",
                 "filters": [{"field": "status", "value": "confirmed"}]}]
        self.assertNotIn("GHL092", rules_hit(
            [wf("Reminders", self.LONG, appt, settings=self.ON)]))

    def test_a_trigger_that_can_only_fire_once_passes(self):
        created = [{"type": "contact_created", "name": "New contact"}]
        self.assertNotIn("GHL092", rules_hit(
            [wf("Onboarding", self.LONG, created, settings=self.ON)]))

    def test_a_guard_at_the_top_passes(self):
        guard = {"type": "if_else", "name": "Already in the sequence?",
                 "meta": {"conditions": [{"field": "contact.tags",
                                          "value": "in-nurture"}]}}
        steps = [guard] + self.LONG
        self.assertNotIn("GHL092", rules_hit(
            [wf("Nurture", steps, FORM, settings=self.ON)]))

    def test_cancelling_the_previous_run_at_the_top_passes(self):
        """Re-entry ON plus a "remove from this workflow" first is correct.

        The old run is cancelled before the new one starts, so the copies never
        overlap — and it is the fix this rule recommends, so firing on it would
        flag the account that took the advice.
        """
        steps = [{"type": "remove_from_workflow", "name": "Cancel the old run",
                  "meta": {"targetWorkflowId": "Nurture"}}] + self.LONG
        self.assertNotIn("GHL092", rules_hit(
            [wf("Nurture", steps, FORM, settings=self.ON)]))

    def test_removing_them_from_a_different_workflow_is_not_a_guard(self):
        steps = [{"type": "remove_from_workflow", "name": "Leave onboarding",
                  "meta": {"targetWorkflowId": "Onboarding"}}] + self.LONG
        self.assertIn("GHL092", rules_hit(
            [wf("Nurture", steps, FORM, settings=self.ON)]))

    def test_no_send_after_the_wait_passes(self):
        steps = [sms("Day 0"), pause("Wait 30 days", "30 days"),
                 {"type": "add_contact_tag", "name": "Mark cold",
                  "meta": {"tags": ["cold"]}}]
        self.assertNotIn("GHL092", rules_hit(
            [wf("Nurture", steps, FORM, settings=self.ON)]))

    def test_waits_on_two_branches_are_not_added_together(self):
        """Two days on either path is two days, not four.

        Summing every wait in the file would inflate the number in the
        finding, and an inflated number is how a whole report gets dismissed.
        """
        steps = [branch("Which?", id="b1"),
                 pause("Wait 2 days", "2 days", id="w1", parentKey="b1-yes",
                       next="m1"),
                 sms("Yes path", id="m1"),
                 pause("Wait 2 days", "2 days", id="w2", parentKey="b1-no",
                       next="m2"),
                 sms("No path", id="m2")]
        self.assertNotIn("GHL092", rules_hit(
            [wf("Route", steps, FORM, settings=self.ON)]))

    def test_two_waits_on_one_path_are_added_together(self):
        steps = [pause("Wait 2 days", "2 days"), sms("One"),
                 pause("Wait 2 days", "2 days"), sms("Two")]
        self.assertIn("GHL092", rules_hit(
            [wf("Seq", steps, FORM, settings=self.ON)]))


class CrossWorkflowCircle(unittest.TestCase):
    """GHL093 — workflows that put each other's contacts back in."""

    def pair(self, back_to="A"):
        return [wf("A", [sms("From A"), enroll("B")], TAG,
                   settings={"allowReentry": True}),
                wf("B", [sms("From B"), enroll(back_to)], TAG,
                   settings={"allowReentry": True})]

    def test_two_workflows_enrolling_each_other_fire(self):
        self.assertIn("GHL093", rules_hit(self.pair()))

    def test_a_circle_containing_sends_is_critical(self):
        found = findings_for("GHL093", self.pair())
        self.assertEqual([f.severity for f in found], ["critical"])

    def test_a_circle_with_no_sends_is_high(self):
        pair = [wf("A", [call("Push"), enroll("B")], TAG),
                wf("B", [call("Push"), enroll("A")], TAG)]
        found = findings_for("GHL093", pair)
        self.assertEqual([f.severity for f in found], ["high"])

    def test_a_one_way_handoff_passes(self):
        pair = [wf("A", [sms("From A"), enroll("B")], TAG),
                wf("B", [sms("From B")], TAG)]
        self.assertNotIn("GHL093", rules_hit(pair))

    def test_a_draft_target_cannot_close_the_circle(self):
        pair = [wf("A", [sms("From A"), enroll("B")], TAG),
                wf("B", [sms("From B"), enroll("A")], TAG, status="draft")]
        self.assertNotIn("GHL093", rules_hit(pair))

    def test_a_workflow_that_enrolls_itself_fires(self):
        one = [wf("A", [sms("From A"), enroll("A")], TAG)]
        found = findings_for("GHL093", one)
        self.assertEqual([f.title for f in found],
                         ["'A' enrolls contacts back into itself"])

    def test_a_target_named_rather_than_referenced_fires(self):
        pair = [wf("Feeder", [sms("Hi"),
                              {"type": "add_to_workflow", "name": "Hand off",
                               "meta": {"workflowName": "Closer"}}], TAG),
                wf("Closer", [sms("Hi"),
                              {"type": "add_to_workflow", "name": "Back",
                               "meta": {"workflowName": "Feeder"}}], TAG)]
        self.assertIn("GHL093", rules_hit(pair))

    def test_a_merge_built_target_is_not_guessed_at(self):
        pair = [wf("A", [sms("From A"),
                         {"type": "add_to_workflow", "name": "Hand off",
                          "meta": {
                              "workflowName": "{{ custom_values.next }}"}}], TAG),
                wf("B", [sms("From B"), enroll("A")], TAG)]
        self.assertNotIn("GHL093", rules_hit(pair))

    def test_removing_a_contact_from_a_workflow_is_not_enrolling_them(self):
        pair = [wf("A", [sms("From A"), enroll("B")], TAG),
                wf("B", [sms("From B"),
                         {"type": "remove_from_workflow", "name": "Clean up",
                          "meta": {"targetWorkflowId": "A"}}], TAG)]
        self.assertNotIn("GHL093", rules_hit(pair))

    def test_a_three_workflow_circle_is_reported_once(self):
        three = [wf("A", [sms("a"), enroll("B")], TAG),
                 wf("B", [sms("b"), enroll("C")], TAG),
                 wf("C", [sms("c"), enroll("A")], TAG)]
        found = findings_for("GHL093", three)
        self.assertEqual([f.title for f in found],
                         ["3 workflows enroll each other in a circle"])


class BranchNesting(unittest.TestCase):
    """GHL094 — more routes than anybody has walked."""

    def tree(self, depth):
        steps = [branch("Level 1", id="t1")]
        for level in range(2, depth + 1):
            steps.append(branch(f"Level {level}", id=f"t{level}",
                                parentKey=f"t{level - 1}-yes"))
        steps.append(sms("Leaf", id="leaf", parentKey=f"t{depth}-yes"))
        return steps

    def test_four_deep_fires(self):
        self.assertIn("GHL094", rules_hit([wf("Triage", self.tree(4), FORM)]))

    def test_three_deep_passes(self):
        self.assertNotIn("GHL094",
                         rules_hit([wf("Triage", self.tree(3), FORM)]))

    def test_four_deep_is_medium_and_six_deep_is_high(self):
        four = findings_for("GHL094", [wf("Triage", self.tree(4), FORM)])
        six = findings_for("GHL094", [wf("Triage", self.tree(6), FORM)])
        self.assertEqual([f.severity for f in four], ["medium"])
        self.assertEqual([f.severity for f in six], ["high"])

    def test_the_finding_counts_the_paths(self):
        found = findings_for("GHL094", [wf("Triage", self.tree(4), FORM)])
        self.assertIn("16 different paths", found[0].title)

    def test_the_finding_names_the_chain_of_conditions(self):
        found = findings_for("GHL094", [wf("Triage", self.tree(4), FORM)])
        self.assertEqual(found[0].step,
                         "Level 1 > Level 2 > Level 3 > Level 4")

    def test_wide_but_shallow_passes(self):
        """Six conditions in a row are six decisions. Six NESTED are 64."""
        steps = [branch(f"Check {i}", id=f"t{i}", next=f"t{i + 1}")
                 for i in range(1, 7)] + [sms("Leaf", id="t7")]
        self.assertNotIn("GHL094", rules_hit([wf("Checks", steps, FORM)]))

    def test_branches_nested_inline_are_read_too(self):
        deepest = {"type": "if_else", "name": "Level 4",
                   "branches": [{"name": "yes", "actions": [
                       {"type": "sms", "name": "Leaf",
                        "meta": {"body": "hi"}}]}]}
        third = {"type": "if_else", "name": "Level 3",
                 "branches": [{"name": "yes", "actions": [deepest]}]}
        second = {"type": "if_else", "name": "Level 2",
                  "branches": [{"name": "yes", "actions": [third]}]}
        first = {"type": "if_else", "name": "Level 1",
                 "branches": [{"name": "yes", "actions": [second]}]}
        self.assertIn("GHL094", rules_hit([wf("Triage", [first], FORM)]))

    def test_a_flat_export_skips_instead_of_passing(self):
        flat = [wf("Seq", [sms("One"), sms("Two")], FORM)]
        findings, skips = audit_all(flat)
        self.assertNotIn("GHL094", {f.rule for f in findings})
        self.assertIn("GHL094", {s.rule for s in skips})

    def test_an_export_with_wiring_does_not_skip(self):
        _, skips = audit_all([wf("Triage", self.tree(2), FORM)])
        self.assertNotIn("GHL094", {s.rule for s in skips})


class PackHygiene(unittest.TestCase):
    """Contract checks: no skips, every finding priced, nothing crashes."""

    MALFORMED = [
        [],
        {},
        [{"name": "x", "status": "published", "steps": None,
          "triggers": None, "settings": None}],
        [{"name": "x", "status": "published", "steps": "not a list"}],
        [{"name": "x", "status": "published", "steps": ["a bare string"]}],
        [{"name": "x", "status": "published", "steps": [],
          "triggers": "form"}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "wait", "meta": "a string, not a dict"}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "wait", "meta": {"delay": ["3", "days"]}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "goto", "meta": {"targetStepId": ["a", "b"]}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "webhook", "id": "a", "next": ["a", "a"],
                     "meta": {"url": ["https://api.example.com"]}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "add_to_workflow", "meta": {"workflowId": 7}}]}],
        [{"name": "x", "status": "published", "settings": "allowReentry",
          "steps": [{"type": "if_else", "id": "a", "parentKey": "a"}]}],
    ]

    def test_no_input_shape_raises(self):
        for data in self.MALFORMED:
            run_all(Account.load(data))

    def test_a_workflow_wired_into_a_knot_terminates(self):
        """Every step pointing at every other step is a legal export."""
        steps = [{"type": "sms", "name": str(i), "id": str(i),
                  "next": [str(j) for j in range(12)]} for i in range(12)]
        run_all(Account.load([wf("Knot", steps, TAG)]))

    def test_none_of_these_rules_ever_skips_on_the_fragment(self):
        """Each of the six is answerable from the export the fragment ships.

        GHL094 is the one that CAN skip — nesting is unreadable in a flat
        export — so the fragment has to carry a wired workflow, and this is
        the test that says so.
        """
        _, skips = run_all(Account.from_file(FRAGMENT))
        self.assertEqual([s.rule for s in skips if s.rule in MINE], [])

    def test_the_fragment_trips_all_six(self):
        findings, _ = run_all(Account.from_file(FRAGMENT))
        tripped = {f.rule for f in findings if f.rule in MINE}
        self.assertEqual(sorted(tripped), sorted(MINE))

    def test_every_finding_says_what_it_costs(self):
        findings, _ = run_all(Account.from_file(FRAGMENT))
        bare = [f.rule for f in findings
                if f.rule in MINE and not f.cost.strip()]
        self.assertEqual(bare, [])

    def test_every_finding_names_a_step_or_a_workflow(self):
        findings, _ = run_all(Account.from_file(FRAGMENT))
        for f in findings:
            if f.rule in MINE:
                self.assertTrue(f.step.strip(), f"{f.rule} points at nothing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# --------------------------------------------------------------------------
# GHL104
# --------------------------------------------------------------------------

OPENER = ("Hey {{contact.first_name}} - thanks for reaching out - I've got your "
          "details in front of me now. What's the best time to reach you today?")
CLOSER = ("Grab a time that works and we'll take it from there: "
          "https://api.leadconnectorhq.com/widget/booking/CVokAlI8fgw4WYWoCtQz")


def form_trigger():
    return {"type": "form_submitted", "name": "Form submitted", "filters": []}


def tag_trigger(tag):
    return {"type": "contact_tag_added", "name": "Tag added",
            "filters": [{"tag": tag}]}


def tag_step(*tags):
    return {"type": "add_contact_tag", "name": "Tag", "meta": {"tags": list(tags)}}


def chain(router_steps=None, direct_steps=None, nudge_steps=None,
          router_status="published", nudge_status="published",
          direct_status="published", settings=None):
    router = wf("Intake Router", router_steps or [tag_step("qualify")],
                triggers=[form_trigger()], status=router_status)
    direct = wf("First Touch", direct_steps or [pause("Hold", "1 minute"),
                                                 sms("Instant reply", OPENER)],
                triggers=[form_trigger()], status=direct_status,
                settings=settings)
    nudge = wf("Qualified Nudge", nudge_steps or [sms("Booking nudge", CLOSER)],
               triggers=[tag_trigger("qualify")], status=nudge_status)
    return [router, direct, nudge]


class ChainedEnrollmentCollision(unittest.TestCase):
    """GHL104 — one event, two first messages, through a tag nothing shows
    next to the trigger it came from."""

    def test_the_chain_is_reported_and_the_backwards_order_is_high(self):
        hits = findings_for("GHL104", chain())
        self.assertEqual([f.severity for f in hits], ["high"])
        self.assertEqual(hits[0].workflow, "First Touch")
        self.assertIn("Qualified Nudge via 'Intake Router'", hits[0].step)
        self.assertIn("the order is wrong", hits[0].symptom)

    def test_two_conversations_without_the_copy_signals_is_medium(self):
        hits = findings_for("GHL104", chain(
            direct_steps=[pause("Hold", "1 minute"),
                          sms("Welcome", "Welcome aboard - more soon.")]))
        self.assertEqual([f.severity for f in hits], ["medium"])
        self.assertNotIn("the order is wrong", hits[0].symptom)

    def test_the_close_going_second_is_not_backwards(self):
        """Direct path sends at once, the chained close waits two minutes:
        still two conversations, but the introduction leads."""
        hits = findings_for("GHL104", chain(
            direct_steps=[sms("Instant reply", OPENER)],
            nudge_steps=[pause("Hold", "2 minutes"), sms("Nudge", CLOSER)]))
        self.assertEqual([f.severity for f in hits], ["medium"])

    def test_an_hour_apart_is_not_one_moment(self):
        self.assertEqual(findings_for("GHL104", chain(
            nudge_steps=[pause("Hold", "90 minutes"), sms("Nudge", CLOSER)])),
            [])

    def test_a_wait_of_unknown_length_drops_the_pair(self):
        reply_wait = {"type": "wait", "name": "Until they reply",
                      "meta": {"waitType": "reply"}}
        self.assertEqual(findings_for("GHL104", chain(
            nudge_steps=[reply_wait, sms("Nudge", CLOSER)])), [])

    def test_a_pause_before_the_tag_breaks_the_chain(self):
        self.assertEqual(findings_for("GHL104", chain(
            router_steps=[pause("Think", "2 hours"), tag_step("qualify")])),
            [])

    def test_a_workflow_handing_off_to_its_own_follow_on_is_not_a_collision(self):
        """Router sends, then tags its own follow-on: a sequence, not a race.
        Without a third workflow on the same event there is nothing to
        collide with."""
        flows = chain(router_steps=[sms("Intro", OPENER), tag_step("qualify")])
        self.assertEqual(findings_for("GHL104", flows[::2]), [])

    def test_coordinated_workflows_are_left_alone(self):
        stop = {"type": "remove_from_workflow", "name": "Stop first touch",
                "meta": {"workflow_id": "First Touch"}}
        self.assertEqual(findings_for("GHL104", chain(
            nudge_steps=[stop, sms("Booking nudge", CLOSER)])), [])

    def test_a_draft_anywhere_in_the_chain_is_silent(self):
        self.assertEqual(findings_for("GHL104", chain(router_status="draft")), [])
        self.assertEqual(findings_for("GHL104", chain(nudge_status="draft")), [])
        self.assertEqual(findings_for("GHL104", chain(direct_status="draft")), [])

    def test_a_different_event_is_not_a_collision(self):
        flows = chain()
        flows[1]["triggers"] = [{"type": "contact_created", "name": "New",
                                 "filters": []}]
        self.assertEqual(findings_for("GHL104", flows), [])

    def test_the_tag_has_to_be_the_one_the_nudge_listens_for(self):
        self.assertEqual(findings_for("GHL104", chain(
            router_steps=[tag_step("something-else")])), [])

    def test_a_nudge_that_sends_nothing_is_not_a_conversation(self):
        self.assertEqual(findings_for("GHL104", chain(
            nudge_steps=[tag_step("scored")])), [])

    def test_send_windows_are_quoted_when_they_differ(self):
        flows = chain(settings={"window": {"start": "09:00", "end": "20:00"}})
        flows[2]["settings"] = {"window": {"start": "08:00", "end": "20:00"}}
        hits = findings_for("GHL104", flows)
        self.assertEqual(len(hits), 1)
        self.assertIn("08:00-20:00", hits[0].symptom)
        self.assertIn("09:00-20:00", hits[0].symptom)

    def test_one_finding_per_pair(self):
        flows = chain()
        flows[0]["steps"] = [tag_step("qualify"), tag_step("qualify")]
        self.assertEqual(len(findings_for("GHL104", flows)), 1)
