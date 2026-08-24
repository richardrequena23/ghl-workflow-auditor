"""Every rule gets a workflow that trips it and a workflow that does not.

A rule that only ever fires is as useless as one that never does — the false
positive is what makes people stop reading the report.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import RULES, run  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "examples", "broken-account.json")


def audit(workflows, custom_values=None):
    return run(Account.load({"workflows": workflows,
                             "customValues": custom_values or {}}))


def rules_hit(workflows, custom_values=None):
    return {f.rule for f in audit(workflows, custom_values)}


def wf(name, steps, triggers=(), status="published", settings=None):
    return {"_id": name, "name": name, "status": status, "steps": list(steps),
            "triggers": list(triggers), "settings": settings or {}}


def sms(name="Message", body="hello"):
    return {"type": "sms", "name": name, "meta": {"body": body}}


def wait(name="Wait", releases=False):
    meta = {"stopOnResponse": True} if releases else {"delay": "2 days"}
    return {"type": "wait", "name": name, "meta": meta}


class TriggerRules(unittest.TestCase):
    def test_unfiltered_appointment_trigger_is_critical(self):
        hits = rules_hit([wf("No Show Recovery", [sms()],
                             [{"type": "appointment", "name": "Appt", "filters": []}])])
        self.assertIn("GHL001", hits)

    def test_filtered_appointment_trigger_passes(self):
        hits = rules_hit([wf("No Show Recovery", [sms()], [
            {"type": "appointment", "name": "Appt",
             "filters": [{"field": "appointment_status", "value": "noshow"}]}])])
        self.assertNotIn("GHL001", hits)

    def test_unfiltered_call_trigger_is_critical(self):
        hits = rules_hit([wf("MCTB", [sms()],
                             [{"type": "call_status", "name": "Call", "filters": []}])])
        self.assertIn("GHL002", hits)

    def test_call_trigger_filtered_to_no_answer_passes(self):
        hits = rules_hit([wf("MCTB", [sms()], [
            {"type": "call_status", "name": "Call",
             "filters": [{"field": "status", "value": "no answer"}]}])])
        self.assertNotIn("GHL002", hits)

    def test_draft_workflows_are_not_audited_for_triggers(self):
        hits = rules_hit([wf("Draft", [sms()],
                             [{"type": "appointment", "filters": []}], status="draft")])
        self.assertNotIn("GHL001", hits)


class ReplyRules(unittest.TestCase):
    def test_sender_with_no_listener(self):
        hits = rules_hit([wf("Nurture", [sms("One"), wait(), sms("Two")])])
        self.assertIn("GHL003", hits)

    def test_wait_that_releases_on_reply_passes(self):
        hits = rules_hit([wf("Nurture", [sms("One"), wait(releases=True), sms("Two")])])
        self.assertNotIn("GHL003", hits)

    def test_single_message_is_not_flagged(self):
        self.assertNotIn("GHL003", rules_hit([wf("One-shot", [sms()])]))

    def test_alert_storm_needs_a_guard(self):
        steps = [sms(), {"type": "internal_notification", "name": "Alert"},
                 sms(), {"type": "internal_notification", "name": "Alert"}]
        self.assertIn("GHL009", rules_hit([wf("Alerts", steps)]))

    def test_engaged_guard_clears_alert_storm(self):
        steps = [{"type": "remove_contact_tag", "name": "Re-arm the reply alert",
                  "meta": {"tag": "engaged"}},
                 sms(), {"type": "internal_notification", "name": "Alert"},
                 sms(), {"type": "internal_notification", "name": "Alert"}]
        self.assertNotIn("GHL009", rules_hit([wf("Alerts", steps)]))


class TimingRules(unittest.TestCase):
    def test_send_window_on_reminder_ladder(self):
        steps = [sms("1 hour reminder", "Your call starts in 1 hour")]
        hits = rules_hit([wf("Reminders", steps,
                             settings={"sendingWindow": {"start": "09:00"},
                                       "timezone": "contact"})])
        self.assertIn("GHL004", hits)

    def test_send_window_on_plain_nurture_is_fine(self):
        hits = rules_hit([wf("Nurture", [sms("Tip 1", "Here is a tip")],
                             settings={"sendingWindow": {"start": "09:00"},
                                       "timezone": "contact"})])
        self.assertNotIn("GHL004", hits)

    def test_window_without_timezone(self):
        hits = rules_hit([wf("Nurture", [sms()],
                             settings={"sendingWindow": {"start": "09:00"}})])
        self.assertIn("GHL013", hits)

    def test_throttle_missing_on_reactivation(self):
        self.assertIn("GHL005", rules_hit([wf("Database Reactivation", [sms()])]))

    def test_drip_step_clears_throttle_rule(self):
        steps = [{"type": "drip", "name": "Throttle"}, sms()]
        self.assertNotIn("GHL005", rules_hit([wf("Database Reactivation", steps)]))


class PortabilityRules(unittest.TestCase):
    def test_hardcoded_webhook_url(self):
        steps = [{"type": "webhook", "name": "Push",
                  "meta": {"url": "https://hooks.example.com/inbound/1"}}]
        self.assertIn("GHL006", rules_hit([wf("Attribution", steps)]))

    def test_custom_value_webhook_url_passes(self):
        steps = [{"type": "webhook", "name": "Push",
                  "meta": {"url": "{{ custom_values.integration_webhook_url }}"}}]
        self.assertNotIn("GHL006", rules_hit([wf("Attribution", steps)]))

    def test_placeholder_custom_value(self):
        hits = rules_hit([wf("Review", [sms()])],
                         {"review_link": "REPLACE-WITH-CLIENT-GOOGLE-ID"})
        self.assertIn("GHL008", hits)

    def test_merge_field_with_no_matching_custom_value(self):
        steps = [sms("Ask", "Leave a review: {{ custom_values.review_link }}")]
        findings = audit([wf("Review", steps)], {"office_phone": "555"})
        self.assertTrue(any(f.rule == "GHL008" and "review_link" in f.step
                            for f in findings))

    def test_defined_custom_value_passes(self):
        steps = [sms("Ask", "Leave a review: {{ custom_values.review_link }}")]
        findings = audit([wf("Review", steps)], {"review_link": "https://g.page/x"})
        self.assertFalse(any(f.rule == "GHL008" for f in findings))


class DataRules(unittest.TestCase):
    def test_deprecated_opportunity_action(self):
        steps = [{"type": "create_opportunity", "name": "Create"}]
        self.assertIn("GHL007", rules_hit([wf("Intake", steps)]))

    def test_internal_variant_is_not_deprecated(self):
        steps = [{"type": "internal_create_opportunity", "name": "Create"}]
        self.assertNotIn("GHL007", rules_hit([wf("Intake", steps)]))

    def test_reentry_with_opportunity_creation(self):
        steps = [{"type": "create_opportunity", "name": "Create"}]
        hits = rules_hit([wf("Intake", steps, settings={"allowReentry": True})])
        self.assertIn("GHL011", hits)

    def test_reentry_off_passes(self):
        steps = [{"type": "create_opportunity", "name": "Create"}]
        hits = rules_hit([wf("Intake", steps, settings={"allowReentry": False})])
        self.assertNotIn("GHL011", hits)


class AskRules(unittest.TestCase):
    def test_review_ask_with_no_suppression_check(self):
        self.assertIn("GHL010", rules_hit([wf("Review Request", [sms("Ask")])]))

    def test_suppression_before_wait_only_is_flagged(self):
        steps = [{"type": "if_else", "name": "Had a complaint - skip",
                  "meta": {"tags": ["complaint", "refund"]}},
                 wait("Wait 7 days"), sms("Ask")]
        self.assertIn("GHL010", rules_hit([wf("Review Request", steps)]))

    def test_suppression_rechecked_after_the_wait_passes(self):
        steps = [{"type": "if_else", "name": "Had a complaint - skip",
                  "meta": {"tags": ["complaint"]}},
                 wait("Wait 7 days"),
                 {"type": "if_else", "name": "Still happy? complaint check",
                  "meta": {"tags": ["complaint"]}},
                 sms("Ask")]
        self.assertNotIn("GHL010", rules_hit([wf("Review Request", steps)]))

    def test_non_review_workflow_is_not_checked(self):
        self.assertNotIn("GHL010", rules_hit([wf("Speed to Lead", [sms()])]))


class HygieneRules(unittest.TestCase):
    def test_published_sandbox(self):
        self.assertIn("GHL012", rules_hit([wf("ZZ SANDBOX - probe", [sms()])]))

    def test_unpublished_sandbox_is_fine(self):
        hits = rules_hit([wf("ZZ SANDBOX - probe", [sms()], status="draft")])
        self.assertNotIn("GHL012", hits)


class Parsing(unittest.TestCase):
    def test_accepts_a_bare_list_of_workflows(self):
        acct = Account.load([wf("A", [sms()])])
        self.assertEqual(len(acct.workflows), 1)

    def test_accepts_alternative_field_names(self):
        acct = Account.load([{"id": "x", "title": "Alt shape", "state": "active",
                              "actions": [{"actionType": "sms", "label": "Hi"}],
                              "events": [{"eventType": "form_submitted"}]}])
        w = acct.workflows[0]
        self.assertEqual(w.name, "Alt shape")
        self.assertTrue(w.published)
        self.assertEqual(w.steps[0].type, "sms")
        self.assertEqual(w.triggers[0].type, "form_submitted")

    def test_custom_values_as_a_list(self):
        acct = Account.load({"workflows": [], "customValues": [
            {"name": "review_link", "value": "https://g.page/x"}]})
        self.assertEqual(acct.custom_values["review_link"], "https://g.page/x")

    def test_empty_input_does_not_explode(self):
        self.assertEqual(run(Account.load([])), [])


class Catalog(unittest.TestCase):
    def test_rule_ids_are_unique(self):
        ids = [r.id for r in RULES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_rule_has_a_test_that_trips_it(self):
        with open(EXAMPLE) as fh:
            findings = run(Account.load(json.load(fh)))
        tripped = {f.rule for f in findings}
        # The example account is the demo; it should exercise most of the catalog.
        self.assertGreaterEqual(len(tripped), 8)

    def test_min_severity_filters(self):
        with open(EXAMPLE) as fh:
            acct = Account.load(json.load(fh))
        self.assertTrue(all(f.severity in ("critical", "high")
                            for f in run(acct, min_severity="high")))

    def test_findings_are_sorted_most_severe_first(self):
        with open(EXAMPLE) as fh:
            findings = run(Account.load(json.load(fh)))
        sevs = [f.severity for f in findings]
        self.assertEqual(sevs, sorted(sevs, key=["critical", "high", "medium",
                                                 "low"].index))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AccountLevelAwareness(unittest.TestCase):
    """GHL003 changes its mind when the account has a central reply listener."""

    SEQUENCE = None  # set in setUp

    def setUp(self):
        self.sequence = wf("Speed to Lead", [sms("One"), wait(), sms("Two")])
        self.handler = wf("Reply Handler", [
            {"type": "remove_from_workflow", "name": "Stop every sequence"},
        ], [{"type": "inbound_message", "name": "Customer replied"}])

    def test_without_a_handler_it_is_high(self):
        findings = [f for f in audit([self.sequence]) if f.rule == "GHL003"]
        self.assertEqual(findings[0].severity, "high")

    def test_with_a_handler_it_drops_to_low(self):
        findings = [f for f in audit([self.sequence, self.handler])
                    if f.rule == "GHL003" and f.workflow == "Speed to Lead"]
        self.assertEqual(findings[0].severity, "low")
        self.assertIn("Reply Handler", findings[0].title)

    def test_a_draft_handler_does_not_count(self):
        self.handler["status"] = "draft"
        findings = [f for f in audit([self.sequence, self.handler])
                    if f.rule == "GHL003"]
        self.assertEqual(findings[0].severity, "high")

    def test_handler_is_detected(self):
        acct = Account.load([self.sequence, self.handler])
        self.assertIsNotNone(acct.reply_handler())
        self.assertEqual(acct.reply_handler().name, "Reply Handler")


def tag_step(*tags):
    return {"type": "add_contact_tag", "name": "Tag",
            "meta": {"tags": list(tags)}}


def tag_trigger(tag):
    return {"type": "contact_tag_added", "name": "Tag added",
            "filters": [{"tag": tag}]}


class TagLoopRules(unittest.TestCase):
    def pair(self, reentry=False):
        alert = wf("Hot Lead Alert", [tag_step("nurture-me")],
                   [tag_trigger("hot-lead")])
        nurture = wf("Long Term Nurture", [sms("One"), tag_step("hot-lead")],
                     [tag_trigger("nurture-me")],
                     settings={"allowReentry": True} if reentry else {})
        return alert, nurture

    def test_two_workflow_loop_is_found(self):
        findings = [f for f in audit(list(self.pair())) if f.rule == "GHL014"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")

    def test_reentry_inside_the_loop_makes_it_critical(self):
        findings = [f for f in audit(list(self.pair(reentry=True)))
                    if f.rule == "GHL014"]
        self.assertEqual(findings[0].severity, "critical")

    def test_workflow_that_triggers_itself(self):
        loop = wf("Self feeder", [sms(), tag_step("vip")], [tag_trigger("vip")])
        self.assertIn("GHL014", rules_hit([loop]))

    def test_tag_chain_that_does_not_close_passes(self):
        a = wf("A", [tag_step("stage-2")], [tag_trigger("stage-1")])
        b = wf("B", [sms()], [tag_trigger("stage-2")])
        self.assertNotIn("GHL014", rules_hit([a, b]))

    def test_draft_workflow_cannot_close_a_loop(self):
        alert, nurture = self.pair()
        nurture["status"] = "draft"
        self.assertNotIn("GHL014", rules_hit([alert, nurture]))


class DuplicateEnrollmentRules(unittest.TestCase):
    def test_identical_triggers_on_two_senders(self):
        a = wf("Welcome Text", [sms()], [{"type": "contact_created", "filters": []}])
        b = wf("New Lead Nurture", [sms()],
               [{"type": "contact_created", "filters": []}])
        findings = [f for f in audit([a, b]) if f.rule == "GHL015"]
        self.assertEqual(len(findings), 1)

    def test_different_filters_pass(self):
        a = wf("FB Leads", [sms()], [tag_trigger("fb")])
        b = wf("Google Leads", [sms()], [tag_trigger("google")])
        self.assertNotIn("GHL015", rules_hit([a, b]))

    def test_a_workflow_with_no_outbound_does_not_collide(self):
        a = wf("Welcome Text", [sms()], [{"type": "contact_created", "filters": []}])
        b = wf("Bookkeeping", [{"type": "add_contact_tag", "meta": {"tag": "new"}}],
               [{"type": "contact_created", "filters": []}])
        self.assertNotIn("GHL015", rules_hit([a, b]))


class CopyRules(unittest.TestCase):
    def test_bare_greeting_field(self):
        steps = [sms("Welcome", "Hi {{ contact.first_name }}, thanks for reaching out")]
        self.assertIn("GHL016", rules_hit([wf("Welcome", steps)]))

    def test_greeting_with_a_default_passes(self):
        steps = [sms("Welcome",
                     "Hi {{ contact.first_name | default:'there' }}, thanks!")]
        self.assertNotIn("GHL016", rules_hit([wf("Welcome", steps)]))

    def test_field_used_mid_sentence_is_not_a_greeting(self):
        steps = [sms("Welcome", "Thanks for booking, {{ contact.first_name }}!")]
        self.assertNotIn("GHL016", rules_hit([wf("Welcome", steps)]))


class ComplianceRules(unittest.TestCase):
    def test_multi_sms_sequence_without_opt_out(self):
        steps = [sms("One", "Quick question"), wait(), sms("Two", "Still there?")]
        self.assertIn("GHL017", rules_hit([wf("Cold Outreach", steps)]))

    def test_opt_out_language_clears_it(self):
        steps = [sms("One", "Quick question. Reply STOP to opt out."),
                 wait(), sms("Two", "Still there?")]
        self.assertNotIn("GHL017", rules_hit([wf("Cold Outreach", steps)]))

    def test_reminder_ladder_is_exempt(self):
        steps = [sms("24h", "See you tomorrow"), wait(), sms("1h", "Starting soon")]
        trig = [{"type": "customer_booked_appointment",
                 "filters": [{"field": "appointment_status", "value": "confirmed"}]}]
        self.assertNotIn("GHL017", rules_hit([wf("Reminders", steps, trig)]))

    def test_single_sms_bulk_campaign_is_still_flagged(self):
        steps = [{"type": "drip", "name": "Throttle"}, sms("Blast", "We're back!")]
        self.assertIn("GHL017", rules_hit([wf("Database Reactivation", steps)]))

    def test_single_conversational_sms_passes(self):
        self.assertNotIn("GHL017", rules_hit([wf("Quick reply", [sms()])]))


class OrphanTagRules(unittest.TestCase):
    def test_tag_nothing_adds(self):
        self.assertIn("GHL018",
                      rules_hit([wf("VIP Onboarding", [sms()], [tag_trigger("vip")])]))

    def test_tag_added_by_another_workflow_passes(self):
        a = wf("VIP Onboarding", [sms()], [tag_trigger("vip")])
        b = wf("Close Won", [tag_step("vip")], [{"type": "opportunity_status_changed"}])
        self.assertNotIn("GHL018", rules_hit([a, b]))

    def test_second_trigger_type_means_it_can_still_fire(self):
        a = wf("VIP Onboarding", [sms()],
               [tag_trigger("vip"), {"type": "form_submitted"}])
        self.assertNotIn("GHL018", rules_hit([a]))
