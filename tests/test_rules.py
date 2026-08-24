"""Every rule gets a workflow that trips it and a workflow that does not.

A rule that only ever fires is as useless as one that never does — the false
positive is what makes people stop reading the report.
"""

import html as html_mod
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.config import AuditConfig  # noqa: E402
from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import CATEGORIES, RULES, run, run_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "examples", "broken-account.json")


def bundle(workflows, custom_values=None, **extra):
    data = {"workflows": workflows, "customValues": custom_values or {}}
    data.update(extra)
    return data


def audit(workflows, custom_values=None, config=None, **extra):
    return run(Account.load(bundle(workflows, custom_values, **extra),
                            config=config))


def audit_all(workflows, custom_values=None, config=None, **extra):
    """(findings, skips) — the skips matter as much as the findings."""
    return run_all(Account.load(bundle(workflows, custom_values, **extra),
                                config=config))


def rules_hit(workflows, custom_values=None, config=None, **extra):
    return {f.rule for f in audit(workflows, custom_values, config, **extra)}


def skips_hit(workflows, custom_values=None, config=None, **extra):
    return {s.rule for s in
            audit_all(workflows, custom_values, config, **extra)[1]}


def findings_for(rule_id, workflows, custom_values=None, config=None, **extra):
    return [f for f in audit(workflows, custom_values, config, **extra)
            if f.rule == rule_id]


def wf(name, steps, triggers=(), status="published", settings=None):
    return {"_id": name, "name": name, "status": status, "steps": list(steps),
            "triggers": list(triggers), "settings": settings or {}}


def sms(name="Message", body="hello"):
    return {"type": "sms", "name": name, "meta": {"body": body}}


def email(name="Email", body="hello", subject="Hi"):
    return {"type": "email", "name": name,
            "meta": {"subject": subject, "body": body}}


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

    def test_every_rule_declares_a_valid_severity_and_category(self):
        for r in RULES:
            self.assertIn(r.severity, ("critical", "high", "medium", "low"),
                          f"{r.id} has severity {r.severity!r}")
            self.assertIn(r.category, CATEGORIES,
                          f"{r.id} has category {r.category!r}")

    def test_rule_ids_are_contiguous_and_correctly_formatted(self):
        """Rule ids are quoted in reports and in the catalog listing, so a gap
        or a typo in one is a broken reference in a client's document."""
        ids = sorted(r.id for r in RULES)
        for rid in ids:
            self.assertRegex(rid, r"^GHL\d{3}$")
        numbers = [int(r[3:]) for r in ids]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

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


# ==========================================================================
# GHL019+ — the account-aware checks
# ==========================================================================

def cond_wait(name="Wait for a reply", timeout=None, kind="contact_reply"):
    meta = {"waitType": kind}
    if timeout is not None:
        meta["timeout"] = timeout
    return {"type": "wait", "name": name, "meta": meta}


class UnboundedWaitRules(unittest.TestCase):
    def test_conditional_wait_with_no_timeout_is_critical(self):
        steps = [sms("One"), cond_wait(), sms("Two"), sms("Three")]
        found = findings_for("GHL019", [wf("Speed to Lead", steps)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "critical")

    def test_the_finding_counts_the_sends_stranded_below_it(self):
        steps = [sms("One"), cond_wait(), sms("Two"), sms("Three")]
        found = findings_for("GHL019", [wf("Speed to Lead", steps)])
        self.assertIn("2 messages", found[0].title)
        self.assertEqual(found[0].reach, 2)

    def test_a_timeout_clears_it(self):
        steps = [sms("One"), cond_wait(timeout="3 days"), sms("Two")]
        self.assertNotIn("GHL019", rules_hit([wf("Speed to Lead", steps)]))

    def test_a_zero_timeout_is_not_a_timeout(self):
        steps = [sms("One"), cond_wait(timeout=0), sms("Two")]
        self.assertIn("GHL019", rules_hit([wf("Speed to Lead", steps)]))

    def test_a_plain_duration_wait_is_not_conditional(self):
        steps = [sms("One"), wait("Wait 2 days"), sms("Two")]
        self.assertNotIn("GHL019", rules_hit([wf("Nurture", steps)]))

    def test_nothing_below_it_downgrades_to_low(self):
        steps = [sms("One"), cond_wait()]
        found = findings_for("GHL019", [wf("Gate", steps)])
        self.assertEqual(found[0].severity, "low")

    def test_alternate_wait_spelling_is_recognised(self):
        steps = [sms("One"),
                 {"type": "wait", "name": "Hold",
                  "meta": {"mode": "Specific Conditions"}},
                 sms("Two")]
        self.assertIn("GHL019", rules_hit([wf("Gate", steps)]))

    def test_ghls_hybrid_reply_wait_with_startafter_is_bounded(self):
        """The builder's own shape: max in startAfter, timeout as a transition.

        Calling a three-day reply wait with an explicit timeout branch
        "unbounded" was this auditor's first false positive on a real account.
        """
        steps = [sms("One"),
                 {"type": "wait", "name": "Wait 3 days for a reply",
                  "attributes": {
                      "type": "reply",
                      "startAfter": {"type": "days", "value": 3,
                                     "when": "after"},
                      "transitions": [
                          {"name": "wait", "condition": "primary",
                           "attributes": {"type": "wait_reply",
                                          "description":
                                              "When contact replies"}},
                          {"name": "timeout", "condition": "timeout"}]}},
                 sms("Two")]
        self.assertNotIn("GHL019", rules_hit([wf("Reactivation", steps)]))

    def test_a_bare_reply_wait_with_no_maximum_is_still_flagged(self):
        steps = [sms("One"),
                 {"type": "wait", "name": "Wait for a reply",
                  "attributes": {"type": "reply", "transitions": []}},
                 sms("Two")]
        found = findings_for("GHL019", [wf("Speed to Lead", steps)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "critical")

    def test_a_zero_startafter_is_not_a_maximum(self):
        steps = [sms("One"),
                 {"type": "wait", "name": "Wait for a reply",
                  "attributes": {"type": "reply",
                                 "startAfter": {"type": "days", "value": 0}}},
                 sms("Two")]
        self.assertIn("GHL019", rules_hit([wf("Speed to Lead", steps)]))


INVENTORY = {
    "calendars": [{"id": "cal_live", "name": "Strategy Call"}],
    "users": [{"id": "usr_live", "name": "Dana", "active": True},
              {"id": "usr_gone", "name": "Sam", "active": False}],
    "pipelines": [{"id": "pipe_a", "name": "Sales",
                   "stages": [{"id": "stg_a", "name": "New"}]}],
    "forms": [{"id": "form_a", "name": "Intake"}],
}


class DanglingReferenceRules(unittest.TestCase):
    def test_missing_calendar_is_critical(self):
        steps = [{"type": "book_appointment", "name": "Book",
                  "meta": {"calendarId": "cal_deleted"}}]
        found = findings_for("GHL023", [wf("Intake", steps)], **INVENTORY)
        self.assertEqual(found, [])  # not this rule's business
        found = findings_for("GHL020", [wf("Intake", steps)], **INVENTORY)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "critical")

    def test_live_calendar_passes(self):
        steps = [{"type": "book_appointment", "name": "Book",
                  "meta": {"calendarId": "cal_live"}}]
        self.assertNotIn("GHL020", rules_hit([wf("Intake", steps)], **INVENTORY))

    def test_deactivated_user_is_flagged(self):
        steps = [{"type": "internal_notification", "name": "Ping",
                  "meta": {"userId": "usr_gone"}}]
        found = findings_for("GHL020", [wf("Intake", steps)], **INVENTORY)
        self.assertEqual(len(found), 1)
        self.assertIn("deactivated", found[0].title)

    def test_a_merge_field_reference_is_not_a_dangling_id(self):
        steps = [{"type": "book_appointment", "name": "Book",
                  "meta": {"calendarId": "{{ custom_values.calendar_id }}"}}]
        self.assertNotIn("GHL020", rules_hit([wf("Intake", steps)], **INVENTORY))

    def test_empty_round_robin_needs_no_inventory(self):
        steps = [{"type": "assign_round_robin", "name": "Route",
                  "meta": {"users": []}}]
        found = findings_for("GHL020", [wf("Intake", steps)])
        self.assertEqual(len(found), 1)
        self.assertIn("nobody in the rotation", found[0].title)

    def test_a_populated_round_robin_passes(self):
        steps = [{"type": "assign_round_robin", "name": "Route",
                  "meta": {"users": ["usr_live"]}}]
        self.assertNotIn("GHL020", rules_hit([wf("Intake", steps)], **INVENTORY))

    def test_it_skips_without_inventory_rather_than_passing(self):
        steps = [{"type": "book_appointment", "name": "Book",
                  "meta": {"calendarId": "cal_deleted"}}]
        findings, skips = audit_all([wf("Intake", steps)])
        self.assertNotIn("GHL020", {f.rule for f in findings})
        self.assertIn("GHL020", {s.rule for s in skips})


def branch_step(name="Route", branches=None):
    return {"type": "if_else", "name": name,
            "meta": {"branches": branches or []}}


class EmptyBranchRules(unittest.TestCase):
    def test_empty_none_branch_is_high(self):
        steps = [branch_step(branches=[
            {"name": "Qualified", "actions": [sms("Yes")]},
            {"name": "None", "actions": []}]), sms("After")]
        found = findings_for("GHL021", [wf("Intake", steps)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "high")

    def test_a_populated_branch_passes(self):
        steps = [branch_step(branches=[
            {"name": "Qualified", "actions": [sms("Yes")]},
            {"name": "None", "actions": [
                {"type": "add_contact_tag", "meta": {"tags": ["unrouted"]}}]}]),
            sms("After")]
        self.assertNotIn("GHL021", rules_hit([wf("Intake", steps)]))

    def test_a_terminal_filter_drops_to_low(self):
        steps = [sms("Before"), branch_step(branches=[
            {"name": "VIP", "actions": [{"type": "add_contact_tag"}]},
            {"name": "None", "actions": []}])]
        found = findings_for("GHL021", [wf("VIP filter", steps)])
        self.assertEqual(found[0].severity, "low")

    def test_an_explicit_else_key_is_read(self):
        steps = [{"type": "if_else", "name": "Route", "meta": {"else": []}},
                 sms("After")]
        self.assertIn("GHL021", rules_hit([wf("Intake", steps)]))

    def test_externally_wired_branches_are_not_called_empty(self):
        """GHL's builder shape: branch objects carry only conditions, and
        their ids reappear in the step's `next` links — the children hang off
        parentKey in the flat step list. Calling those branches empty produced
        one false "silent exit" per populated branch on a real account."""
        steps = [
            {"type": "if_else", "name": "Segment", "id": "seg",
             "next": ["b1", "b2"],
             "attributes": {"branches": [
                 {"id": "b1", "name": "Quoted",
                  "segments": [{"conditions": []}]},
                 {"id": "b2", "name": "Recent",
                  "segments": [{"conditions": []}]}]}},
            {"type": "sms", "name": "Quoted path", "id": "s1",
             "parentKey": "seg-Quoted", "meta": {"body": "hi, reply STOP to opt out"}},
            {"type": "sms", "name": "Recent path", "id": "s2",
             "parentKey": "seg-Recent", "meta": {"body": "hi, reply STOP to opt out"}},
        ]
        self.assertNotIn("GHL021", rules_hit([wf("Reactivation", steps)]))


def node(step_type, name, sid, nxt=None, parent=None, **meta):
    out = {"type": step_type, "name": name, "id": sid, "meta": meta}
    if nxt is not None:
        out["next"] = nxt
    if parent is not None:
        out["parentKey"] = parent
    return out


class WiringRules(unittest.TestCase):
    def test_dangling_next_pointer(self):
        steps = [node("sms", "One", "s1", nxt="s2"),
                 node("sms", "Two", "s2", nxt="s404")]
        found = findings_for("GHL022", [wf("Seq", steps)])
        self.assertTrue(any("points at a node" in f.title for f in found))

    def test_a_complete_chain_passes(self):
        steps = [node("sms", "One", "s1", nxt="s2"), node("sms", "Two", "s2")]
        self.assertNotIn("GHL022", rules_hit([wf("Seq", steps)]))

    def test_unreachable_step_is_dead_weight(self):
        steps = [node("sms", "One", "s1", nxt="s2"),
                 node("sms", "Two", "s2"),
                 node("sms", "Detached", "s9", parent="s7-gone")]
        found = findings_for("GHL022", [wf("Seq", steps)])
        orphan = [f for f in found if "nothing can reach" in f.title]
        self.assertEqual(len(orphan), 1)
        self.assertEqual(orphan[0].category, "dead_weight")

    def test_branch_children_are_reachable_through_their_parent(self):
        steps = [node("if_else", "Route", "s1"),
                 node("sms", "Yes", "s2", parent="s1-yes"),
                 node("sms", "No", "s3", parent="s1-no")]
        self.assertNotIn("GHL022", rules_hit([wf("Seq", steps)]))

    def test_a_flat_export_skips_instead_of_passing(self):
        findings, skips = audit_all([wf("Seq", [sms("One"), sms("Two")])])
        self.assertNotIn("GHL022", {f.rule for f in findings})
        self.assertIn("GHL022", {s.rule for s in skips})


FIELDS = {"customFields": [{"fieldKey": "contact.service_interest",
                            "name": "Service Interest"}]}


class MergeFieldRules(unittest.TestCase):
    def test_empty_custom_value_in_a_body(self):
        steps = [sms("Ask", "Book here: {{ custom_values.booking_link }}")]
        found = findings_for("GHL023", [wf("Welcome", steps)],
                             {"booking_link": ""})
        self.assertEqual(len(found), 1)
        self.assertIn("defined but empty", found[0].title)

    def test_a_filled_custom_value_passes(self):
        steps = [sms("Ask", "Book here: {{ custom_values.booking_link }}")]
        self.assertNotIn("GHL023", rules_hit([wf("Welcome", steps)],
                                             {"booking_link": "https://x.co/b"}))

    def test_an_empty_value_inside_a_url_is_critical(self):
        steps = [sms("Ask",
                     "Go to https://acme.com/{{custom_values.booking_link}}")]
        found = findings_for("GHL023", [wf("Welcome", steps)],
                             {"booking_link": ""})
        self.assertEqual(found[0].severity, "critical")

    def test_misspelled_contact_field(self):
        steps = [sms("Ask", "About {{ contact.servcie_interest }}?")]
        found = findings_for("GHL023", [wf("Welcome", steps)], **FIELDS)
        self.assertEqual(len(found), 1)
        self.assertIn("servcie_interest", found[0].title)

    def test_a_real_custom_field_passes(self):
        steps = [sms("Ask", "About {{ contact.service_interest }}?")]
        self.assertNotIn("GHL023", rules_hit([wf("Welcome", steps)], **FIELDS))

    def test_standard_contact_fields_are_never_flagged(self):
        steps = [sms("Ask", "{{contact.first_name}} {{contact.company_name}} "
                            "{{contact.email}} {{contact.phone}}")]
        self.assertNotIn("GHL023", rules_hit([wf("Welcome", steps)], **FIELDS))

    def test_without_the_field_list_it_skips(self):
        steps = [sms("Ask", "About {{ contact.servcie_interest }}?")]
        findings, skips = audit_all([wf("Welcome", steps)])
        self.assertEqual([f for f in findings if f.rule == "GHL023"], [])
        self.assertIn("GHL023", {s.rule for s in skips})


class SmsFallbackRules(unittest.TestCase):
    def test_fallback_filter_in_an_sms(self):
        steps = [sms("Hi", "Hey {{ contact.first_name | default: 'there' }}!")]
        self.assertIn("GHL024", rules_hit([wf("Welcome", steps)]))

    def test_the_same_filter_in_an_email_is_fine(self):
        steps = [email("Hi", "Hey {{ contact.first_name | default: 'there' }}!")]
        self.assertNotIn("GHL024", rules_hit([wf("Welcome", steps)]))

    def test_a_plain_sms_merge_field_is_not_flagged_here(self):
        steps = [sms("Hi", "Thanks for booking, {{ contact.first_name }}!")]
        self.assertNotIn("GHL024", rules_hit([wf("Welcome", steps)]))


class EmailDeliverabilityRules(unittest.TestCase):
    def test_marketing_email_with_no_unsubscribe(self):
        steps = [email("Tip 1", "Here is a tip"), wait(),
                 email("Tip 2", "Another tip")]
        self.assertIn("GHL025", rules_hit([wf("Nurture", steps)]))

    def test_an_unsubscribe_link_clears_it(self):
        steps = [email("Tip 1", "Here is a tip. {{unsubscribe}}"), wait(),
                 email("Tip 2", "Another. {{unsubscribe}}")]
        self.assertNotIn("GHL025", rules_hit([wf("Nurture", steps)]))

    def test_account_default_unsubscribe_clears_it(self):
        steps = [email("Tip 1", "Here is a tip"), wait(),
                 email("Tip 2", "Another tip")]
        self.assertNotIn("GHL025", rules_hit(
            [wf("Nurture", steps)],
            emailSettings={"default_unsubscribe": True},
            emailDomains=[{"domain": "mail.acme.com", "verified": True}]))

    def test_knowing_the_default_is_off_raises_the_severity(self):
        steps = [email("Tip 1", "Here is a tip"), wait(),
                 email("Tip 2", "Another tip")]
        unknown = findings_for("GHL025", [wf("Nurture", steps)])
        known_off = findings_for(
            "GHL025", [wf("Nurture", steps)],
            emailSettings={"default_unsubscribe": False},
            emailDomains=[{"domain": "mail.acme.com", "verified": True}])
        self.assertEqual(unknown[0].severity, "medium")
        self.assertEqual(known_off[0].severity, "high")

    def test_a_single_booking_confirmation_is_transactional(self):
        steps = [email("Confirmed", "You're booked for Tuesday.")]
        trig = [{"type": "customer_booked_appointment",
                 "filters": [{"field": "appointment_status",
                              "value": "confirmed"}]}]
        self.assertNotIn("GHL025", rules_hit([wf("Confirmation", steps, trig)]))

    def test_config_can_mark_a_workflow_transactional(self):
        steps = [email("Receipt", "Your receipt"), wait(),
                 email("Receipt copy", "Copy of your receipt")]
        cfg = AuditConfig.from_dict({"transactional_workflows": ["Receipts"]})
        self.assertNotIn("GHL025", rules_hit([wf("Receipts", steps)],
                                             config=cfg))

    def test_unverified_sending_domain(self):
        steps = [email("Tip", "Here is a tip. {{unsubscribe}}")]
        found = findings_for(
            "GHL025", [wf("Nurture", steps)],
            emailDomains=[{"domain": "mail.acme.com", "verified": False}])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].category, "deliverability")

    def test_a_verified_domain_passes(self):
        steps = [email("Tip", "Here is a tip. {{unsubscribe}}")]
        self.assertNotIn("GHL025", rules_hit(
            [wf("Nurture", steps)],
            emailDomains=[{"domain": "mail.acme.com", "verified": True}]))

    def test_no_domain_list_skips_the_domain_half(self):
        steps = [email("Tip", "Here is a tip. {{unsubscribe}}")]
        self.assertIn("GHL025", skips_hit([wf("Nurture", steps)]))


class DeadWeightRules(unittest.TestCase):
    def test_zero_enrollments_with_a_live_trigger_is_medium(self):
        w = wf("Intake", [sms()], [{"type": "form_submitted"}])
        found = findings_for("GHL026", [w],
                             stats={"Intake": {"enrollments": 0}})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "medium")

    def test_zero_enrollments_with_no_trigger_is_low(self):
        w = wf("Manual only", [sms()])
        found = findings_for("GHL026", [w],
                             stats={"Manual only": {"enrollments": 0}})
        self.assertEqual(found[0].severity, "low")

    def test_a_busy_workflow_passes(self):
        w = wf("Intake", [sms()], [{"type": "form_submitted"}])
        self.assertNotIn("GHL026", rules_hit([w], stats={"Intake": 412}))

    def test_stats_can_be_keyed_by_id(self):
        w = wf("Intake", [sms()], [{"type": "form_submitted"}])
        self.assertIn("GHL026", rules_hit([w], stats={"Intake": 0}))

    def test_no_stats_skips_rather_than_passing(self):
        self.assertIn("GHL026", skips_hit([wf("Intake", [sms()])]))

    def test_the_window_comes_from_config(self):
        w = wf("Intake", [sms()], [{"type": "form_submitted"}])
        cfg = AuditConfig.from_dict({"stats_window_days": 30})
        found = findings_for("GHL026", [w], config=cfg,
                             stats={"Intake": 0})
        self.assertIn("30 days", found[0].title)


class ManifestRules(unittest.TestCase):
    MANIFEST = {"required_steps": {"Attribution": ["Push to reporting"]}}

    def test_missing_required_step(self):
        cfg = AuditConfig.from_dict(self.MANIFEST)
        w = wf("Attribution", [sms("Notify")])
        found = findings_for("GHL027", [w], config=cfg)
        self.assertEqual(len(found), 1)
        self.assertIn("Push to reporting", found[0].step)

    def test_present_required_step_passes(self):
        cfg = AuditConfig.from_dict(self.MANIFEST)
        w = wf("Attribution", [{"type": "webhook", "name": "Push to reporting"}])
        self.assertNotIn("GHL027", rules_hit([w], config=cfg))

    def test_workflow_name_matching_ignores_case_and_spacing(self):
        cfg = AuditConfig.from_dict(self.MANIFEST)
        w = wf("  attribution ", [{"type": "webhook",
                                   "name": "Push to reporting"}])
        self.assertNotIn("GHL027", rules_hit([w], config=cfg))

    def test_a_workflow_the_manifest_names_but_the_account_lacks(self):
        cfg = AuditConfig.from_dict(self.MANIFEST)
        found = findings_for("GHL027", [wf("Something else", [sms()])],
                             config=cfg)
        self.assertEqual(len(found), 1)
        self.assertIn("does not have", found[0].title)

    def test_no_manifest_skips(self):
        self.assertIn("GHL027", skips_hit([wf("Attribution", [sms()])]))


# ==========================================================================
# Config replacing what used to be hardcoded account-specific maps
# ==========================================================================

class ReentryPolicyRules(unittest.TestCase):
    def test_reentry_on_when_it_should_be_off(self):
        cfg = AuditConfig.from_dict({"reentry_policy": {"Speed to Lead": False}})
        w = wf("Speed to Lead", [sms()], settings={"allowReentry": True})
        found = findings_for("GHL011", [w], config=cfg)
        self.assertEqual(len(found), 1)
        self.assertIn("meant to block it", found[0].title)

    def test_reentry_off_when_it_should_be_on(self):
        cfg = AuditConfig.from_dict({"reentry_policy": {"No Show": True}})
        w = wf("No Show", [sms()], settings={"allowReentry": False})
        found = findings_for("GHL011", [w], config=cfg)
        self.assertIn("meant to allow it", found[0].title)

    def test_matching_the_policy_passes(self):
        cfg = AuditConfig.from_dict({"reentry_policy": {"Speed to Lead": False}})
        w = wf("Speed to Lead", [sms()], settings={"allowReentry": False})
        self.assertNotIn("GHL011", rules_hit([w], config=cfg))

    def test_no_policy_means_no_opinion(self):
        w = wf("Speed to Lead", [sms()], settings={"allowReentry": True})
        self.assertNotIn("GHL011", rules_hit([w]))


class SendWindowPolicyRules(unittest.TestCase):
    def test_a_wiped_window_is_high(self):
        cfg = AuditConfig.from_dict({"send_window_policy": {
            "Nurture": {"start": "09:00", "end": "20:00"}}})
        found = findings_for("GHL013", [wf("Nurture", [sms()])], config=cfg)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "high")

    def test_a_drifted_window_is_medium(self):
        cfg = AuditConfig.from_dict({"send_window_policy": {
            "Nurture": {"start": "09:00", "end": "20:00"}}})
        w = wf("Nurture", [sms()],
               settings={"sendingWindow": {"start": "06:00", "end": "20:00"},
                         "timezone": "contact"})
        found = findings_for("GHL013", [w], config=cfg)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "medium")

    def test_a_matching_window_passes(self):
        cfg = AuditConfig.from_dict({"send_window_policy": {
            "Nurture": {"start": "09:00", "end": "20:00"}}})
        w = wf("Nurture", [sms()],
               settings={"sendingWindow": {"start": "09:00", "end": "20:00"},
                         "timezone": "contact"})
        self.assertNotIn("GHL013", rules_hit([w], config=cfg))

    def test_a_window_that_should_not_be_there(self):
        cfg = AuditConfig.from_dict({"send_window_policy": {"Speed": None}})
        w = wf("Speed", [sms()],
               settings={"sendingWindow": {"start": "09:00", "end": "20:00"},
                         "timezone": "contact"})
        found = findings_for("GHL013", [w], config=cfg)
        self.assertIn("should not have one", found[0].title)

    def test_null_policy_with_no_window_passes(self):
        cfg = AuditConfig.from_dict({"send_window_policy": {"Speed": None}})
        self.assertNotIn("GHL013", rules_hit([wf("Speed", [sms()])], config=cfg))


class DuplicateWorkflowRules(unittest.TestCase):
    def test_trigger_spelling_differences_still_collide(self):
        a = wf("Welcome A", [sms()],
               [{"type": "contactTagAdded", "filters": [{"tag": "New"}]}])
        b = wf("Welcome B", [sms()],
               [{"type": "contact_tag_added",
                 "filters": [{"field": "tag", "value": "new"}]}])
        self.assertIn("GHL015", rules_hit([a, b]))

    def test_identical_workflows_are_critical(self):
        steps = [sms("One"), wait(), sms("Two")]
        a = wf("Welcome", steps, [{"type": "contact_created"}])
        b = wf("Welcome (copy)", steps, [{"type": "contact_created"}])
        found = findings_for("GHL015", [a, b])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "critical")
        self.assertIn("identical copies", found[0].title)

    def test_different_shapes_on_one_trigger_stay_high(self):
        a = wf("Welcome Text", [sms()], [{"type": "contact_created"}])
        b = wf("Welcome Email", [email()], [{"type": "contact_created"}])
        found = findings_for("GHL015", [a, b])
        self.assertEqual(found[0].severity, "high")


class AppointmentExitRules(unittest.TestCase):
    """GHL028 — a reminder ladder with no way out when the appointment dies."""

    LADDER = [sms("Confirm", "You're booked."), wait("Until 24h before"),
              sms("24h reminder", "See you tomorrow.")]
    APPT = [{"type": "appointment_status", "name": "Booked",
             "filters": [{"field": "appointment_status", "value": "confirmed"}]}]
    CANCELLED = [{"type": "appointment_status", "name": "Cancelled",
                  "filters": [{"field": "appointment_status",
                               "value": "cancelled"}]}]

    def test_a_ladder_with_no_cancel_handling_is_critical(self):
        hits = findings_for("GHL028", [wf("Reminders", self.LADDER, self.APPT)])
        self.assertEqual([f.severity for f in hits], ["critical"])

    def test_a_remove_step_inside_the_workflow_clears_it(self):
        steps = self.LADDER + [{"type": "remove_from_workflow", "name": "Done"}]
        self.assertNotIn("GHL028",
                         rules_hit([wf("Reminders", steps, self.APPT)]))

    def test_an_account_wide_cancel_guard_downgrades_to_low(self):
        guard = wf("Cancelled - Cleanup",
                   [{"type": "remove_from_workflow", "name": "Pull out"}],
                   self.CANCELLED)
        hits = findings_for("GHL028",
                            [wf("Reminders", self.LADDER, self.APPT), guard])
        self.assertEqual([f.severity for f in hits], ["low"])

    def test_the_cancel_triggered_workflow_itself_is_not_flagged(self):
        rebook = wf("Rebook Ask", [sms("A"), wait(), sms("B")], self.CANCELLED)
        self.assertNotIn("GHL028", rules_hit([rebook]))

    def test_a_single_confirmation_is_not_a_ladder(self):
        one = wf("Confirmation", [sms("Confirm", "You're booked.")], self.APPT)
        self.assertNotIn("GHL028", rules_hit([one]))

    def test_a_status_gate_counts_as_an_exit(self):
        steps = [sms("Confirm"), wait(),
                 {"type": "if_else", "name": "Still booked?",
                  "meta": {"conditions": [{"field": "appointment_status",
                                           "value": "cancelled"}]}},
                 sms("Reminder")]
        self.assertNotIn("GHL028",
                         rules_hit([wf("Reminders", steps, self.APPT)]))


class SendWindowCoverageRules(unittest.TestCase):
    """GHL029 — delayed sends with nothing keeping them out of the night."""

    TAG = [{"type": "contact_tag_added", "name": "Tagged",
            "filters": [{"tag": "nurture"}]}]

    def test_a_delayed_sms_with_no_window_is_flagged(self):
        hits = rules_hit([wf("Nurture", [wait(), sms("Day 3")], self.TAG)])
        self.assertIn("GHL029", hits)

    def test_an_instant_reply_is_not_flagged(self):
        hits = rules_hit([wf("Speed to lead",
                             [sms("Instant"), wait(), email("Day 2")],
                             self.TAG)])
        self.assertNotIn("GHL029", hits)

    def test_a_send_window_clears_it(self):
        w = wf("Nurture", [wait(), sms("Day 3")], self.TAG,
               settings={"sendingWindow": {"start": "09:00", "end": "20:00"},
                         "timezone": "contact"})
        self.assertNotIn("GHL029", rules_hit([w]))

    def test_a_documented_window_policy_defers_to_the_drift_check(self):
        cfg = AuditConfig.from_dict({"send_window_policy": {
            "Nurture": {"start": "09:00", "end": "20:00"}}})
        hits = rules_hit([wf("Nurture", [wait(), sms("Day 3")], self.TAG)],
                         config=cfg)
        self.assertNotIn("GHL029", hits)
        self.assertIn("GHL013", hits)  # the wiped-window check reports it instead

    def test_appointment_ladders_are_left_to_the_reminder_rules(self):
        appt = [{"type": "appointment_status", "name": "Booked",
                 "filters": [{"field": "appointment_status",
                              "value": "confirmed"}]}]
        hits = rules_hit([wf("Reminders", [wait(), sms("Reminder")], appt)])
        self.assertNotIn("GHL029", hits)


class ReentrySettingRules(unittest.TestCase):
    """GHL030 — the toggle HighLevel documents it ignores."""

    APPT = [{"type": "appointment_status", "name": "Booked",
             "filters": [{"field": "appointment_status", "value": "confirmed"}]}]

    def test_reentry_off_on_an_appointment_trigger_is_flagged(self):
        w = wf("Confirmations", [sms()], self.APPT,
               settings={"allowReentry": False})
        self.assertIn("GHL030", rules_hit([w]))

    def test_reentry_on_is_fine(self):
        w = wf("Confirmations", [sms()], self.APPT,
               settings={"allowReentry": True})
        self.assertNotIn("GHL030", rules_hit([w]))

    def test_a_mixed_trigger_set_is_governed_by_the_toggle(self):
        trig = self.APPT + [{"type": "contact_tag_added",
                             "filters": [{"tag": "vip"}]}]
        w = wf("Confirmations", [sms()], trig,
               settings={"allowReentry": False})
        self.assertNotIn("GHL030", rules_hit([w]))

    def test_a_tag_trigger_is_not_flagged(self):
        trig = [{"type": "contact_tag_added", "filters": [{"tag": "vip"}]}]
        w = wf("Nurture", [sms()], trig, settings={"allowReentry": False})
        self.assertNotIn("GHL030", rules_hit([w]))

    def test_an_invoice_trigger_counts_too(self):
        trig = [{"type": "invoice_paid", "name": "Paid",
                 "filters": [{"field": "status", "value": "paid"}]}]
        w = wf("Receipts", [email()], trig, settings={"allowReentry": False})
        self.assertIn("GHL030", rules_hit([w]))


class SmsNumberRules(unittest.TestCase):
    """GHL031 — SMS steps with nothing behind them to send from."""

    def test_no_number_list_reports_a_skip(self):
        self.assertIn("GHL031", skips_hit([wf("Welcome", [sms()])]))

    def test_an_email_only_account_needs_no_number(self):
        findings, skips = audit_all([wf("Welcome", [email()])])
        self.assertNotIn("GHL031", {s.rule for s in skips})
        self.assertNotIn("GHL031", {f.rule for f in findings})

    def test_no_sms_capable_number_is_account_critical(self):
        hits = findings_for(
            "GHL031", [wf("Welcome", [sms()])],
            phoneNumbers=[{"number": "+15550001111", "sms": False}])
        self.assertEqual([f.severity for f in hits], ["critical"])
        self.assertEqual(hits[0].workflow, "(account)")

    def test_a_capable_number_passes(self):
        hits = findings_for(
            "GHL031", [wf("Welcome", [sms()])],
            phoneNumbers=[{"number": "+15550001111", "sms": True}])
        self.assertEqual(hits, [])

    def test_a_from_number_outside_the_location_is_flagged(self):
        step = {"type": "sms", "name": "Welcome",
                "meta": {"fromNumber": "+15559998888", "body": "hi"}}
        hits = findings_for(
            "GHL031", [wf("Welcome", [step])],
            phoneNumbers=[{"number": "+15550001111", "sms": True}])
        self.assertEqual([f.severity for f in hits], ["high"])

    def test_country_code_formatting_does_not_false_positive(self):
        step = {"type": "sms", "name": "Welcome",
                "meta": {"fromNumber": "(555) 000-1111", "body": "hi"}}
        hits = findings_for(
            "GHL031", [wf("Welcome", [step])],
            phoneNumbers=[{"number": "+15550001111", "sms": True}])
        self.assertEqual(hits, [])

    def test_a_merge_field_from_number_is_not_guessed_about(self):
        step = {"type": "sms", "name": "Welcome",
                "meta": {"fromNumber": "{{ custom_values.sms_number }}",
                         "body": "hi"}}
        hits = findings_for(
            "GHL031", [wf("Welcome", [step])],
            phoneNumbers=[{"number": "+15550001111", "sms": True}])
        self.assertEqual(hits, [])


class OpportunityStageRules(unittest.TestCase):
    """GHL032 — a pipeline chosen, a stage left to the default."""

    def test_a_pipeline_with_no_stage_is_flagged(self):
        step = {"type": "create_opportunity", "name": "Create opp",
                "meta": {"pipelineId": "pipe_x"}}
        self.assertIn("GHL032", rules_hit([wf("Booked", [step])]))

    def test_the_finding_names_the_stage_it_lands_in(self):
        step = {"type": "create_opportunity", "name": "Create opp",
                "meta": {"pipelineId": "pipe_x"}}
        hits = findings_for(
            "GHL032", [wf("Booked", [step])],
            pipelines=[{"id": "pipe_x", "name": "Sales",
                        "stages": [{"id": "s1", "name": "New Lead"},
                                   {"id": "s2", "name": "Booked"}]}])
        self.assertEqual(len(hits), 1)
        self.assertIn("New Lead", hits[0].title)

    def test_an_explicit_stage_passes(self):
        step = {"type": "create_opportunity", "name": "Create opp",
                "meta": {"pipelineId": "pipe_x", "stageId": "s2"}}
        self.assertNotIn("GHL032", rules_hit([wf("Booked", [step])]))

    def test_no_pipeline_at_all_is_not_this_rules_business(self):
        step = {"type": "create_opportunity", "name": "Create opp"}
        self.assertNotIn("GHL032", rules_hit([wf("Booked", [step])]))


class OrderTriggerRules(unittest.TestCase):
    """GHL033 — a thank-you on the trigger that fires before payment."""

    ORDER_FORM = [{"type": "order_form_submitted", "name": "Order form",
                   "filters": []}]

    def test_confirmation_copy_on_the_submission_trigger(self):
        w = wf("Purchase Confirmation",
               [email("Thanks",
                      "Thanks for your purchase - your order is confirmed.")],
               self.ORDER_FORM)
        self.assertIn("GHL033", rules_hit([w]))

    def test_the_post_payment_trigger_passes(self):
        w = wf("Purchase Confirmation",
               [email("Thanks",
                      "Thanks for your purchase - your order is confirmed.")],
               [{"type": "order_submitted", "name": "Order", "filters": []}])
        self.assertNotIn("GHL033", rules_hit([w]))

    def test_neutral_checkout_copy_passes(self):
        w = wf("Abandoned Checkout",
               [email("Hold on",
                      "We saved your details - finish checkout any time.")],
               self.ORDER_FORM)
        self.assertNotIn("GHL033", rules_hit([w]))


class ShortenerRules(unittest.TestCase):
    """GHL034 — a shared shortener domain inside a text message."""

    def test_a_bitly_link_in_an_sms_is_flagged(self):
        w = wf("Nurture", [sms("Nudge", "Book here: https://bit.ly/3xR2mQ "
                                        "Reply STOP to opt out.")])
        self.assertIn("GHL034", rules_hit([w]))

    def test_a_full_domain_passes(self):
        w = wf("Nurture", [sms("Nudge", "Book here: "
                                        "https://book.example-client.com/call "
                                        "Reply STOP to opt out.")])
        self.assertNotIn("GHL034", rules_hit([w]))

    def test_a_shortener_in_an_email_is_not_an_sms_problem(self):
        w = wf("Nurture", [email("Nudge", "Book here: https://bit.ly/3xR2mQ")])
        self.assertNotIn("GHL034", rules_hit([w]))


class WebhookEndpointRules(unittest.TestCase):
    """GHL035 — webhooks still pointing at the tool used to debug them."""

    def _hook(self, url):
        return {"type": "webhook", "name": "Push", "meta": {"url": url}}

    def test_webhook_site_is_flagged_high(self):
        hits = findings_for(
            "GHL035", [wf("Sync", [self._hook("https://webhook.site/abc123")])])
        self.assertEqual([f.severity for f in hits], ["high"])

    def test_an_ngrok_tunnel_is_flagged(self):
        self.assertIn("GHL035", rules_hit(
            [wf("Sync", [self._hook("https://f00d.ngrok-free.app/hook")])]))

    def test_plain_http_is_flagged_medium(self):
        hits = findings_for(
            "GHL035",
            [wf("Sync", [self._hook("http://api.example-client.com/hook")])])
        self.assertEqual([f.severity for f in hits], ["medium"])

    def test_a_production_https_endpoint_passes(self):
        self.assertNotIn("GHL035", rules_hit(
            [wf("Sync", [self._hook("https://api.example-client.com/hook")])]))


class DeprecatedTriggerRules(unittest.TestCase):
    """GHL036 — the booking trigger that skips manual bookings."""

    def test_customer_booked_appointment_is_flagged(self):
        trig = [{"type": "customer_booked_appointment", "name": "Booked",
                 "filters": [{"field": "appointment_status",
                              "value": "confirmed"}]}]
        self.assertIn("GHL036", rules_hit([wf("Reminders", [sms()], trig)]))

    def test_appointment_status_is_the_supported_shape(self):
        trig = [{"type": "appointment_status", "name": "Booked",
                 "filters": [{"field": "appointment_status",
                              "value": "confirmed"}]}]
        self.assertNotIn("GHL036", rules_hit([wf("Reminders", [sms()], trig)]))


class DraftRules(unittest.TestCase):
    """GHL037 — finished builds that were never switched on."""

    TRIG = [{"type": "contact_tag_added", "name": "Tagged",
             "filters": [{"tag": "referral-ready"}]}]

    def test_a_finished_draft_with_sends_is_flagged(self):
        w = wf("Referral Ask", [sms(), wait(), sms()], self.TRIG,
               status="draft")
        hits = findings_for("GHL037", [w])
        self.assertEqual([f.severity for f in hits], ["medium"])

    def test_published_workflows_are_not_drafts(self):
        w = wf("Referral Ask", [sms(), wait(), sms()], self.TRIG)
        self.assertNotIn("GHL037", rules_hit([w]))

    def test_a_draft_named_as_wip_is_deliberate(self):
        w = wf("WIP - Referral Ask", [sms()], self.TRIG, status="draft")
        self.assertNotIn("GHL037", rules_hit([w]))

    def test_a_draft_with_no_trigger_cannot_fire_anyway(self):
        w = wf("Referral Ask", [sms()], (), status="draft")
        self.assertNotIn("GHL037", rules_hit([w]))

    def test_a_draft_with_no_sends_is_low(self):
        w = wf("Tag Bookkeeping",
               [{"type": "add_contact_tag", "meta": {"tags": ["x"]}}],
               self.TRIG, status="draft")
        hits = findings_for("GHL037", [w])
        self.assertEqual([f.severity for f in hits], ["low"])


# ==========================================================================
# Scoring and reporting
# ==========================================================================

class Scoring(unittest.TestCase):
    def setUp(self):
        from ghlaudit.score import health
        self.health = health

    def test_a_clean_account_scores_100(self):
        self.assertEqual(self.health([], [], 10).score, 100)
        self.assertEqual(self.health([], [], 10).grade, "A")

    def test_more_damage_lowers_the_score(self):
        findings, _ = audit_all([wf("No Show", [sms()],
                                    [{"type": "appointment", "filters": []}])])
        one = self.health(findings, [], 10).score
        self.assertLess(one, 100)
        self.assertGreater(one, 0)

    def test_the_score_never_reaches_zero(self):
        from ghlaudit.rules import Finding
        many = [Finding(rule="GHL001", severity="critical", workflow=f"w{i}",
                        title="t", symptom="s", fix="f") for i in range(400)]
        self.assertGreater(self.health(many, [], 5).score, 0)

    def test_a_bigger_account_absorbs_the_same_damage_better(self):
        from ghlaudit.rules import Finding
        f = [Finding(rule="GHL001", severity="critical", workflow="w",
                     title="t", symptom="s", fix="f")]
        self.assertGreater(self.health(f, [], 40).score,
                           self.health(f, [], 4).score)

    def test_every_finding_lands_in_a_known_category(self):
        findings, _ = run_all(Account.from_file(EXAMPLE))
        for f in findings:
            self.assertIn(f.category, CATEGORIES)

    def test_a_category_whose_checks_all_skipped_is_not_assessed(self):
        hs = self.health([], skips_as_objects(), 5)
        dead = next(c for c in hs.categories if c.key == "dead_weight")
        self.assertFalse(dead.assessed)

    def test_findings_are_ranked_by_cost_not_rule_order(self):
        findings, skips = run_all(Account.from_file(EXAMPLE))
        ranked = self.health(findings, skips, 13).ranked
        scores = [f.cost_score() for f in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_reach_widens_the_blast_radius(self):
        from ghlaudit.rules import Finding
        small = Finding(rule="GHL003", severity="high", workflow="a",
                        title="t", symptom="s", fix="f", reach=1)
        big = Finding(rule="GHL003", severity="high", workflow="b",
                      title="t", symptom="s", fix="f", reach=8)
        self.assertGreater(big.cost_score(), small.cost_score())


def skips_as_objects():
    """Every dead-weight check reporting itself as unrunnable."""
    from ghlaudit.rules import RULES, Skip
    return [Skip(rule=r.id, title=r.title, reason="test", needs="test",
                 category="dead_weight")
            for r in RULES if r.category == "dead_weight"]


class Reporting(unittest.TestCase):
    def setUp(self):
        self.acct = Account.from_file(EXAMPLE)
        self.findings, self.skips = run_all(self.acct)
        self.n = len(self.acct.workflows)

    def test_text_report_renders(self):
        from ghlaudit.report import as_text
        body = as_text(self.findings, self.n, self.skips)
        self.assertIn("Account health:", body)
        self.assertIn("Fix in this order", body)

    def test_markdown_report_renders(self):
        from ghlaudit.report import as_markdown
        body = as_markdown(self.findings, self.n, self.skips)
        self.assertIn("# GoHighLevel account audit", body)
        self.assertIn("| Category | Score | Findings |", body)

    def test_json_report_is_valid_and_carries_the_score(self):
        from ghlaudit.report import as_json
        data = json.loads(as_json(self.findings, self.n, self.skips))
        self.assertIn("score", data)
        self.assertEqual(len(data["findings"]), len(self.findings))
        self.assertEqual(data["checks_total"], len(RULES))

    def test_html_report_is_self_contained(self):
        import re
        from ghlaudit.report import as_html
        page = as_html(self.findings, self.n, self.skips)
        self.assertTrue(page.startswith("<!doctype html>"))
        refs = re.findall(r'(?:src|href)=["\']([^"\']+)', page)
        self.assertEqual([r for r in refs if r.startswith(("http", "//"))], [])
        self.assertIn("<style>", page)

    def test_html_escapes_workflow_names(self):
        from ghlaudit.report import as_html
        steps = [{"type": "sms", "name": "x", "meta": {"body": "TODO"}}]
        findings, skips = audit_all([wf("<script>alert(1)</script>", steps)])
        page = as_html(findings, 1, skips)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_html_survives_an_account_with_no_findings(self):
        from ghlaudit.report import as_html
        page = as_html([], 3, [])
        self.assertIn("Nothing found", page)

    def test_skips_are_shown_as_gaps_not_passes(self):
        from ghlaudit.report import as_html
        findings, skips = audit_all([wf("Seq", [sms("a"), sms("b")])])
        page = as_html(findings, 1, skips)
        self.assertIn("could not check", page)

    def test_prepared_by_lands_in_header_and_footer(self):
        from ghlaudit.report import as_html
        page = as_html(self.findings, self.n, self.skips,
                       prepared_by="Dana <Auditor>")
        self.assertEqual(page.count("Prepared by Dana &lt;Auditor&gt;"), 2)

    def test_no_prepared_by_means_no_byline(self):
        from ghlaudit.report import as_html
        self.assertNotIn("Prepared by",
                         as_html(self.findings, self.n, self.skips))

    def test_executive_summary_names_the_most_expensive_finding(self):
        from ghlaudit.report import as_html
        from ghlaudit.score import health
        page = as_html(self.findings, self.n, self.skips)
        top = health(self.findings, self.skips, self.n).ranked[0]
        self.assertIn("Executive summary", page)
        summary = page.split("Executive summary")[1].split("<h2>")[0]
        self.assertIn(html_mod.escape(top.workflow, quote=False), summary)

    def test_executive_summary_on_a_clean_account_says_so(self):
        from ghlaudit.report import as_html
        summary = as_html([], 3, []).split("Executive summary")[1]
        self.assertIn("No defects were found", summary.split("<h2>")[0])

    def test_cli_html_carries_the_default_byline(self):
        import tempfile
        from ghlaudit.cli import main
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "r.html")
            self.assertEqual(main([EXAMPLE, "-f", "html", "-o", out]), 0)
            with open(out) as fh:
                page = fh.read()
        self.assertIn("Prepared by Richard Requena", page)

    def test_cli_byline_can_be_switched_off(self):
        import tempfile
        from ghlaudit.cli import main
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "r.html")
            main([EXAMPLE, "-f", "html", "-o", out, "--prepared-by", ""])
            with open(out) as fh:
                page = fh.read()
        self.assertNotIn("Prepared by", page)


class StepConfigAliases(unittest.TestCase):
    def test_attributes_is_read_as_the_settings_holder(self):
        """GHL's own builder API nests step settings under `attributes`."""
        from ghlaudit.model import parse_step
        step = parse_step({"type": "sms", "name": "x",
                           "attributes": {"body": "hi"}})
        self.assertEqual(step.config().get("body"), "hi")


class ConfigLoading(unittest.TestCase):
    def test_an_empty_config_is_valid(self):
        cfg = AuditConfig.from_dict({})
        self.assertEqual(cfg.owned_domains, [])
        self.assertEqual(cfg.stats_window_days, 90)

    def test_garbage_config_does_not_explode(self):
        self.assertEqual(AuditConfig.from_dict("nope").stats_window_days, 90)
        self.assertEqual(
            AuditConfig.from_dict({"stats_window_days": "soon"}
                                  ).stats_window_days, 90)

    def test_owned_domains_match_subdomains(self):
        cfg = AuditConfig.from_dict({"owned_domains": ["acme.com"]})
        self.assertTrue(cfg.owns_host("book.acme.com"))
        self.assertTrue(cfg.owns_host("ACME.com"))
        self.assertFalse(cfg.owns_host("notacme.com"))
        self.assertFalse(cfg.owns_host(""))

    def test_a_bundle_can_carry_its_own_config(self):
        acct = Account.load({"workflows": [], "config": {
            "owned_domains": ["acme.com"], "stats_window_days": 30}})
        self.assertEqual(acct.config.stats_window_days, 30)

    def test_an_explicit_config_replaces_the_bundles(self):
        acct = Account.load(
            {"workflows": [], "config": {"stats_window_days": 30}},
            config=AuditConfig.from_dict({"stats_window_days": 7}))
        self.assertEqual(acct.config.stats_window_days, 7)


class InventoryLoading(unittest.TestCase):
    def test_nothing_supplied_means_nothing_provided(self):
        self.assertEqual(Account.load([]).inventory.provided, set())

    def test_users_accept_both_shapes(self):
        listed = Account.load({"workflows": [], "users": [
            {"id": "u1", "name": "Dana", "active": False}]}).inventory
        self.assertFalse(listed.users["u1"]["active"])
        mapped = Account.load({"workflows": [],
                               "users": {"u1": "Dana"}}).inventory
        self.assertTrue(mapped.users["u1"]["active"])

    def test_pipelines_carry_their_stages(self):
        inv = Account.load({"workflows": [], "pipelines": [
            {"id": "p1", "name": "Sales",
             "stages": [{"id": "s1", "name": "New"}]}]}).inventory
        self.assertEqual(inv.stages["s1"]["pipeline"], "p1")
        self.assertTrue(inv.has("pipelines", "stages"))

    def test_custom_fields_resolve_by_bare_key_and_full_key(self):
        inv = Account.load({"workflows": [], "customFields": [
            {"fieldKey": "contact.service_interest",
             "name": "Service Interest"}]}).inventory
        self.assertIn("service_interest", inv.custom_fields)
        self.assertIn("contact_service_interest", inv.custom_fields)

    def test_a_malformed_bucket_is_not_treated_as_supplied(self):
        """`"users": "dana"` is a typo, not an empty account.

        Counting it as supplied-but-empty would make every userId in the
        account look like a reference to a deleted user — a whole rule's worth
        of false positives from one bad key.
        """
        inv = Account.load({"workflows": [], "users": "dana",
                            "pipelines": 7}).inventory
        self.assertNotIn("users", inv.provided)
        self.assertNotIn("pipelines", inv.provided)

    def test_an_explicitly_empty_list_does_count_as_supplied(self):
        inv = Account.load({"workflows": [], "calendars": []}).inventory
        self.assertIn("calendars", inv.provided)

    def test_a_garbage_user_list_does_not_produce_dangling_findings(self):
        steps = [{"type": "internal_notification", "name": "Ping",
                  "meta": {"userId": "usr_live"}}]
        self.assertNotIn("GHL020", rules_hit([wf("Intake", steps)],
                                             users="dana"))

    def test_phone_capability_is_read(self):
        inv = Account.load({"workflows": [], "phoneNumbers": [
            {"number": "+15550001111", "capabilities": {"sms": False}},
            {"number": "+15550002222", "capabilities": {"sms": True}}]
        }).inventory
        self.assertEqual(len(inv.sms_capable_numbers), 1)


class ExampleAccount(unittest.TestCase):
    """The shipped example is the demo, so it has to actually demo everything."""

    def setUp(self):
        self.acct = Account.from_file(EXAMPLE)
        self.findings, self.skips = run_all(self.acct)

    def test_every_rule_in_the_catalog_fires_on_it(self):
        tripped = {f.rule for f in self.findings}
        missing = sorted({r.id for r in RULES} - tripped)
        self.assertEqual(missing, [], f"rules that never fire: {missing}")

    def test_no_check_is_skipped_on_it(self):
        self.assertEqual([s.rule for s in self.skips], [])

    def test_every_finding_explains_what_it_costs(self):
        bare = [f.rule for f in self.findings if not f.cost.strip()]
        self.assertEqual(sorted(set(bare)), [])


class Robustness(unittest.TestCase):
    """Malformed exports must skip or report, never crash.

    Real exports are messy: null step lists, a trigger that is an array, a
    settings value that is a string. A traceback mid-audit is worse than a
    wrong finding, because it stops the other 26 checks running.
    """

    CASES = [
        [],
        {},
        [{"name": "x", "status": "published", "steps": None,
          "triggers": None, "settings": None}],
        [{"name": "x", "status": "published", "steps": "not a list"}],
        [{"name": "x", "status": "published", "steps": ["a bare string"]}],
        [{"name": "x", "status": "published", "triggers": [["a", "b"]],
          "steps": []}],
        [{"name": "x", "status": "published", "settings": "windowed",
          "steps": []}],
        [{"_id": 12345, "name": 999, "status": True,
          "steps": [{"type": 7, "name": None}]}],
        {"workflows": [], "customValues": [1, "two", None, {"nope": 1}]},
        {"workflows": [], "users": "dana", "pipelines": 7, "stats": "none"},
    ]

    def test_no_input_shape_raises(self):
        from ghlaudit.report import as_html, as_json, as_markdown, as_text
        for data in self.CASES:
            acct = Account.load(data)
            findings, skips = run_all(acct)
            for render in (as_text, as_markdown, as_json, as_html):
                render(findings, len(acct.workflows), skips)

    def test_a_step_pointing_at_itself_does_not_hang(self):
        steps = [{"type": "sms", "name": "Loop", "id": "a", "next": "a"}]
        run_all(Account.load([wf("Seq", steps)]))

    def test_a_parent_cycle_terminates(self):
        """Two steps naming each other as parent must not spin the walk.

        No orphan is reported here on purpose: with no step declaring itself a
        root, the first step is taken as the entry point, and from there both
        are reachable. Claiming a cycle would need more than this export shows.
        """
        steps = [{"type": "sms", "name": "A", "id": "a", "parentKey": "b"},
                 {"type": "sms", "name": "B", "id": "b", "parentKey": "a"}]
        found = findings_for("GHL022", [wf("Seq", steps)])
        self.assertEqual([f.title for f in found], [])

    def test_unicode_and_markup_in_names_survive_every_renderer(self):
        from ghlaudit.report import as_html, as_json, as_markdown, as_text
        steps = [{"type": "sms", "name": "x", "meta": {"body": "TODO"}}]
        acct = Account.load([wf("\u65e5\u672c\u8a9e <b>&amp;</b>", steps)])
        findings, skips = run_all(acct)
        for render in (as_text, as_markdown, as_json, as_html):
            self.assertTrue(render(findings, 1, skips))
