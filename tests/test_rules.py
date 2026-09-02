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
    def test_the_combined_opportunity_action_is_deprecated(self):
        steps = [{"type": "create_update_opportunity", "name": "Create/Update"}]
        self.assertIn("GHL007", rules_hit([wf("Intake", steps)]))

    def test_the_split_actions_are_the_replacement_not_the_problem(self):
        """The regression this rule shipped with, locked out.

        GoHighLevel split the combined Create/Update Opportunity action into
        two separate ones and is retiring the combined action. This rule flagged
        the two REPLACEMENTS as deprecated and told the owner to swap them for
        `internal_` variants that do not exist. It fired 21 times on one real
        account, all of it correct configuration, and the advice would have
        broken every one of those steps.
        """
        for t in ("create_opportunity", "update_opportunity",
                  "internal_create_opportunity"):
            steps = [{"type": t, "name": "Create"}]
            self.assertNotIn("GHL007", rules_hit([wf("Intake", steps)]),
                             f"{t} is a current action, not a deprecated one")

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

    def test_a_greeting_that_degrades_gracefully_passes(self):
        """'Hey {{first_name}} - x' blanks to 'Hey - x': correct SMS form."""
        steps = [sms("Welcome",
                     "Hey {{contact.first_name}} - thanks for reaching out")]
        self.assertNotIn("GHL016", rules_hit([wf("Welcome", steps)]))

    def test_a_period_hugging_the_name_still_fires(self):
        steps = [sms("Welcome", "Hi {{contact.first_name}}. Quick question")]
        self.assertIn("GHL016", rules_hit([wf("Welcome", steps)]))


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

    def test_a_confirmation_plus_its_pre_call_note_is_still_transactional(self):
        """Two messages about one booking are one transaction.

        Google's sender guidance names reservation confirmations as exempt from
        the one-click unsubscribe requirement. The rule used to exempt a
        transactional workflow only when it held exactly one email, so a real
        account's "Strategy Call - Booking & Confirmation v2" — confirmation
        plus a pre-call note — was told to add an unsubscribe link to an
        appointment confirmation. That is wrong advice and harmful advice:
        opting out of transactional mail is how someone stops receiving the
        reminder for the call they booked.
        """
        steps = [email("Confirmed", "You're booked for Tuesday."), wait(),
                 email("Before we talk", "Here's what to have ready.")]
        trig = [{"type": "customer_booked_appointment",
                 "filters": [{"field": "appointment_status",
                              "value": "confirmed"}]}]
        self.assertNotIn("GHL025",
                         rules_hit([wf("Booking & Confirmation", steps, trig)]))

    def test_a_third_email_makes_it_a_campaign_again(self):
        """A chase is marketing whatever triggered it.

        "Same-day rebook / Second touch / Close-out" off an appointment trigger
        is a sequence aimed at a person who did not respond, not a receipt.
        """
        steps = [email("Same day", "Want to grab another time?"), wait(),
                 email("Second touch", "Still keen?"), wait(),
                 email("Close out", "Last one from me.")]
        trig = [{"type": "customer_booked_appointment",
                 "filters": [{"field": "appointment_status",
                              "value": "noshow"}]}]
        self.assertIn("GHL025", rules_hit([wf("No Show Recovery", steps, trig)]))

    def test_config_can_mark_a_workflow_transactional(self):
        steps = [email("Receipt", "Your receipt"), wait(),
                 email("Receipt copy", "Copy of your receipt")]
        cfg = AuditConfig.from_dict({"transactional_workflows": ["Receipts"]})
        self.assertNotIn("GHL025", rules_hit([wf("Receipts", steps)],
                                             config=cfg))

    def test_a_one_minute_hold_is_not_an_hour_shift(self):
        """Speed-to-lead answering fast is not a 3am text.

        This rule exists because "wait 3 days" lands at whatever o'clock the
        trigger fired. A one-minute pause to let a form finish writing lands in
        the same minute, and the lead who submitted at 11:40pm is waiting for
        that reply. The rule's own docstring said flagging speed-to-lead would
        be noise; it did exactly that until the wait's duration was read.
        """
        steps = [{"type": "wait", "name": "Hold 1 minute",
                  "meta": {"startAfter": {"type": "minutes", "value": 1}}},
                 sms("Instant reply", "Got your enquiry - what time suits?")]
        self.assertNotIn("GHL029", rules_hit([wf("Speed to Lead", steps,
                         [{"type": "form_submitted"}])]))

    def test_a_multi_day_wait_still_needs_a_window(self):
        steps = [{"type": "wait", "name": "Wait 3 days",
                  "meta": {"startAfter": {"type": "days", "value": 3}}},
                 sms("Follow up", "Still thinking it over?")]
        self.assertIn("GHL029", rules_hit([wf("Nurture", steps,
                      [{"type": "form_submitted"}])]))

    def test_an_unbounded_wait_is_treated_as_long(self):
        """A reply-wait with no duration can hold for days. Assume it does."""
        steps = [{"type": "wait", "name": "Wait for a reply",
                  "meta": {"type": "reply"}},
                 sms("Last touch", "Closing your file.")]
        self.assertIn("GHL029", rules_hit([wf("Missed Call", steps,
                      [{"type": "form_submitted"}])]))

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

    def test_the_same_skeleton_with_different_words_is_not_a_clone(self):
        """The false positive this rule shipped with, locked out.

        A real account had a referral ask and a review ask: same trigger, same
        twenty steps in the same order, completely different copy. The rule saw
        one shape, called them "2 identical copies of the same workflow", and
        told the owner to unpublish one of two live campaigns. Reusing a
        skeleton is good practice, and a critical whose fix is "delete a
        campaign" has to be certain before it says so.
        """
        skeleton = lambda one, two: [sms("Ask 1", one), wait(), sms("Ask 2", two)]
        referral = wf("Referral Request",
                      skeleton("Anyone you know who needs the same work done?",
                               "No pressure - just checking if a name came to mind."),
                      [{"type": "contact_tag_added",
                        "filters": [{"tag": "job-complete"}]}])
        review = wf("Review Request",
                    skeleton("If it's what you needed, a review helps a lot.",
                             "No rush - here's the link if you've got a minute."),
                    [{"type": "contact_tag_added",
                      "filters": [{"tag": "job-complete"}]}])
        found = findings_for("GHL015", [referral, review])
        self.assertEqual(len(found), 1)
        # Still reported: they DO both enroll on the same tag, and a contact
        # tagged job-complete really does get both sequences at once. What must
        # not happen is the critical that says one of them is a duplicate.
        self.assertEqual(found[0].severity, "high")
        self.assertNotIn("identical copies", found[0].title)

    def test_a_snapshot_re_push_is_still_caught(self):
        """The case the rule exists for. Copy is copied too, so it still fires."""
        steps = [sms("Welcome", "Hi there, thanks for reaching out."),
                 wait(), sms("Nudge", "Just checking you saw my message.")]
        a = wf("Welcome", steps, [{"type": "contact_created"}])
        b = wf("Welcome (copy)", list(steps), [{"type": "contact_created"}])
        found = findings_for("GHL015", [a, b])
        self.assertEqual(found[0].severity, "critical")
        self.assertIn("identical copies", found[0].title)

    def test_the_same_message_in_html_and_plain_text_still_matches(self):
        """Encoding is not authorship. Two copies of one workflow are one."""
        from ghlaudit.model import parse_workflow
        plain = {"type": "email", "name": "Ask",
                 "meta": {"subject": "Hi", "body": "Thanks for your time."}}
        marked = {"type": "email", "name": "Ask",
                  "meta": {"subject": "Hi",
                           "body": "<p>Thanks   for your time.</p>"}}
        a = parse_workflow(wf("A", [plain], [{"type": "contact_created"}]))
        b = parse_workflow(wf("B", [marked], [{"type": "contact_created"}]))
        self.assertEqual(a.copy_fingerprint(), b.copy_fingerprint())

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
# GHL038-040 — windowed-wait drift, opportunity writers, stage loops
# ==========================================================================

def windowed_wait(name="Wait 1 day"):
    return {"type": "wait", "name": name,
            "meta": {"delay": "1 day",
                     "window": {"start": "09:00", "end": "17:00"}}}


def opp_create(pipe="pipe_sales", stage="stg_new", kind="create_opportunity"):
    return {"type": kind, "name": "Create opportunity",
            "meta": {"pipelineId": pipe, "stageId": stage}}


def stage_trigger(stage):
    return {"type": "opportunity_stage_changed", "name": "Stage changed",
            "filters": [{"field": "stage", "value": stage}]}


OPTOUT_SMS = "quick check-in about your project. Reply STOP to opt out."


class WindowDriftRules(unittest.TestCase):
    def test_three_windowed_waits_in_a_row_are_flagged(self):
        steps = [sms(body=OPTOUT_SMS), windowed_wait("W1"), windowed_wait("W2"),
                 windowed_wait("W3"), sms("After", OPTOUT_SMS)]
        found = findings_for("GHL038", [wf("Drip", steps)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "low")
        self.assertEqual(found[0].reach, 1)

    def test_two_windowed_waits_pass(self):
        steps = [windowed_wait("W1"), windowed_wait("W2"),
                 sms("After", OPTOUT_SMS)]
        self.assertNotIn("GHL038", rules_hit([wf("Drip", steps)]))

    def test_an_unwindowed_wait_does_not_extend_the_streak(self):
        steps = [windowed_wait("W1"), windowed_wait("W2"), wait("Plain"),
                 windowed_wait("W3"), sms("After", OPTOUT_SMS)]
        self.assertNotIn("GHL038", rules_hit([wf("Drip", steps)]))

    def test_a_send_after_a_windowed_wait_is_not_a_night_send(self):
        """The wait's own window times the resume — GHL029 must not call the
        send after it a 3am text."""
        steps = [windowed_wait("W1"), sms("After", OPTOUT_SMS)]
        self.assertNotIn("GHL029", rules_hit([wf("Drip", steps)]))


class OpportunityWriterRules(unittest.TestCase):
    def test_two_creators_on_one_pipeline_are_flagged(self):
        a = wf("Form Intake", [opp_create()],
               [{"type": "form_submitted", "filters": []}])
        b = wf("Booking Flow", [opp_create()],
               [{"type": "survey_submitted", "filters": []}])
        found = findings_for("GHL039", [a, b])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "medium")

    def test_different_pipelines_pass(self):
        a = wf("Form Intake", [opp_create("pipe_a")],
               [{"type": "form_submitted", "filters": []}])
        b = wf("Booking Flow", [opp_create("pipe_b")],
               [{"type": "survey_submitted", "filters": []}])
        self.assertNotIn("GHL039", rules_hit([a, b]))

    def test_a_single_creator_passes(self):
        a = wf("Form Intake", [opp_create()],
               [{"type": "form_submitted", "filters": []}])
        self.assertNotIn("GHL039", rules_hit([a]))

    def test_reentry_on_a_writer_escalates_to_high(self):
        a = wf("Form Intake", [opp_create()],
               [{"type": "form_submitted", "filters": []}],
               settings={"allowMultiple": True})
        b = wf("Booking Flow", [opp_create()],
               [{"type": "survey_submitted", "filters": []}])
        self.assertEqual(findings_for("GHL039", [a, b])[0].severity, "high")

    def test_a_merge_field_pipeline_is_not_counted(self):
        a = wf("Form Intake", [opp_create("{{ custom_values.pipeline }}")],
               [{"type": "form_submitted", "filters": []}])
        b = wf("Booking Flow", [opp_create("{{ custom_values.pipeline }}")],
               [{"type": "survey_submitted", "filters": []}])
        self.assertNotIn("GHL039", rules_hit([a, b]))

    def test_updaters_do_not_count_as_creators(self):
        a = wf("Form Intake", [opp_create()],
               [{"type": "form_submitted", "filters": []}])
        b = wf("Stage Mover", [opp_create(kind="update_opportunity")],
               [{"type": "survey_submitted", "filters": []}])
        self.assertNotIn("GHL039", rules_hit([a, b]))


class StageLoopRules(unittest.TestCase):
    def test_a_two_workflow_stage_cycle_is_flagged_once(self):
        a = wf("Reopen", [opp_create(stage="stg_new",
                                     kind="update_opportunity")],
               [stage_trigger("stg_booked")])
        b = wf("Rebook", [opp_create(stage="stg_booked",
                                     kind="update_opportunity")],
               [stage_trigger("stg_new")])
        found = findings_for("GHL040", [a, b])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "medium")

    def test_a_one_way_stage_write_passes(self):
        a = wf("Reopen", [opp_create(stage="stg_new",
                                     kind="update_opportunity")],
               [stage_trigger("stg_booked")])
        b = wf("Listener", [sms(body=OPTOUT_SMS)], [stage_trigger("stg_new")])
        self.assertNotIn("GHL040", rules_hit([a, b]))

    def test_a_self_loop_is_flagged(self):
        a = wf("Loop", [opp_create(stage="stg_new",
                                   kind="update_opportunity")],
               [stage_trigger("stg_new")])
        found = findings_for("GHL040", [a])
        self.assertEqual(len(found), 1)
        self.assertIn("itself", found[0].title)

    def test_reentry_in_the_cycle_escalates_to_high(self):
        a = wf("Reopen", [opp_create(stage="stg_new",
                                     kind="update_opportunity")],
               [stage_trigger("stg_booked")],
               settings={"allowMultiple": True})
        b = wf("Rebook", [opp_create(stage="stg_booked",
                                     kind="update_opportunity")],
               [stage_trigger("stg_new")])
        self.assertEqual(findings_for("GHL040", [a, b])[0].severity, "high")

    def test_stage_triggers_are_not_read_as_tag_triggers(self):
        """'stage' contains 'tag' (s-TAG-e) — a substring test fed stage ids
        into the tag dead-weight and tag-loop checks as phantom tags."""
        a = wf("Reopen", [sms(body=OPTOUT_SMS)], [stage_trigger("stg_booked")])
        self.assertNotIn("GHL018", rules_hit([a], tags=["stg_booked"]))


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

    def test_one_rule_repeated_is_one_root_cause(self):
        from ghlaudit.rules import Finding
        from ghlaudit.score import root_causes
        many = [Finding(rule="GHL041", severity="high", workflow=f"w{i}",
                        title="t", symptom="s", fix="f") for i in range(13)]
        roots = root_causes(many)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].sites, 13)
        self.assertEqual(len(roots[0].workflows), 13)

    def test_a_habit_costs_less_than_the_same_count_of_separate_defects(self):
        """Thirteen sites of one rule must not grade like thirteen defects.

        This is the whole point of grouping: an account with one systemic habit
        has one thing to fix and should not sink below an account with a dozen
        unrelated problems.
        """
        from ghlaudit.rules import Finding
        habit = [Finding(rule="GHL041", severity="high", workflow=f"w{i}",
                         title="t", symptom="s", fix="f") for i in range(13)]
        spread = [Finding(rule=f"GHL{i:03d}", severity="high", workflow="w",
                          title="t", symptom="s", fix="f")
                  for i in range(1, 14)]
        self.assertGreater(self.health(habit, [], 13).score,
                           self.health(spread, [], 13).score)

    def test_more_sites_still_costs_more(self):
        """Diminishing, not free. Spread is real damage and has to register."""
        from ghlaudit.rules import Finding
        def n_sites(n):
            f = [Finding(rule="GHL041", severity="high", workflow=f"w{i}",
                         title="t", symptom="s", fix="f") for i in range(n)]
            return self.health(f, [], 13).score
        self.assertGreater(n_sites(1), n_sites(4))
        self.assertGreater(n_sites(4), n_sites(13))

    def test_a_systemic_high_still_outweighs_one_isolated_critical(self):
        from ghlaudit.rules import Finding
        habit = [Finding(rule="GHL041", severity="high", workflow=f"w{i}",
                         title="t", symptom="s", fix="f") for i in range(13)]
        crit = [Finding(rule="GHL015", severity="critical", workflow="w",
                        title="t", symptom="s", fix="f")]
        self.assertLess(self.health(habit, [], 13).score,
                        self.health(crit, [], 13).score)

    def test_the_categories_and_the_headline_share_one_budget(self):
        """No category may outrank the headline while carrying all the damage.

        The bug this locks out: every category used to be scored against the
        whole account's tolerance while the headline was scored against that
        same figure for all five at once, so a report could show categories at
        A and B above an F. If one category holds every finding, its score and
        the headline must not disagree in that direction.
        """
        from ghlaudit.rules import Finding
        f = [Finding(rule=f"GHL{i:03d}", severity="high", workflow=f"w{i}",
                     title="t", symptom="s", fix="f", category="compliance")
             for i in range(1, 9)]
        hs = self.health(f, [], 13)
        compliance = next(c for c in hs.categories if c.key == "compliance")
        self.assertLessEqual(compliance.score, hs.score)

    def test_a_small_category_is_not_judged_on_a_big_one_s_allowance(self):
        """Ten rules do not get the same tolerance as fifty-four."""
        from ghlaudit.score import _tolerance
        self.assertLess(_tolerance(13, "deliverability"),
                        _tolerance(13, "routing"))
        self.assertLess(_tolerance(13, "routing"), _tolerance(13))

    def test_the_summary_line_leads_with_root_causes(self):
        from ghlaudit.report import summary_line
        from ghlaudit.rules import Finding
        many = [Finding(rule="GHL041", severity="high", workflow=f"w{i}",
                        title="t", symptom="s", fix="f") for i in range(13)]
        line = summary_line(many, 13)
        self.assertIn("1 root cause showing up in 13 places", line)

    def test_the_fix_list_names_one_job_once(self):
        """A to-do list that repeats the same job is a worse to-do list."""
        from ghlaudit.rules import Finding
        many = [Finding(rule="GHL041", severity="high", workflow=f"w{i}",
                        title="t", symptom="s", fix="f") for i in range(13)]
        hs = self.health(many, [], 13)
        self.assertEqual(len(hs.ranked), 13)
        self.assertEqual(len(hs.fix_order), 1)
        self.assertEqual(hs.fix_order[0][1], 13)

    def test_the_client_report_still_names_every_site(self):
        """Grouping must not cost the reader the list of places to go.

        Sites and workflows are counted separately on purpose: four AI-verdict
        branches inside one sequence are four places to fix and one workflow to
        open, and a report that calls that "4 workflows" is simply wrong.
        """
        from ghlaudit.report import as_html
        from ghlaudit.rules import Finding
        f = [Finding(rule="GHL041", severity="high", workflow="Qualification",
                     step=f"Sync verdict - {tag}", title="t", symptom="s",
                     fix="f") for tag in ("HOT", "WARM", "COLD")]
        html = as_html(f, 13, [])
        self.assertIn("3 places across 1 workflow", html)
        for tag in ("HOT", "WARM", "COLD"):
            self.assertIn(f"Sync verdict - {tag}", html)
        self.assertNotIn("3 workflows", html)

    def test_two_sends_with_no_pause_between_them_is_not_a_drip(self):
        """No wait, no window for a reply to land in, nothing to ignore.

        A real reply-triggered workflow sent the booking link by SMS and by
        email, back to back, and was flagged for "2 outbound messages, nothing
        listening for a reply" — with a symptom describing a day-2 follow-up it
        did not have. One touch delivered twice is not a sequence.
        """
        steps = [sms("Link", "Here's my calendar."),
                 email("Link", "Here's my calendar.")]
        self.assertNotIn("GHL003", rules_hit([wf("Reply Handler", steps,
                         [{"type": "customer_replied"}])]))

    def test_a_send_after_a_wait_is_still_a_drip(self):
        steps = [sms("Touch 1", "You free this week?"), wait(),
                 sms("Touch 2", "Last one from me.")]
        self.assertIn("GHL003", rules_hit([wf("Nurture", steps,
                      [{"type": "contact_created"}])]))

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


# --------------------------------------------------------------------------
# GHL041-GHL052 — the reliability rules
# --------------------------------------------------------------------------

def webhook(name="Sync", url="https://api.example.com/sync", **meta):
    m = {"url": url}
    m.update(meta)
    return {"type": "webhook", "name": name, "meta": m}


class ExternalCallFailurePathRules(unittest.TestCase):
    def test_webhook_with_no_saved_response_is_flagged(self):
        found = findings_for("GHL041", [wf("Order Sync", [webhook()])])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_saved_response_with_a_branch_after_it_passes(self):
        steps = [webhook(saveResponse=True),
                 {"type": "if_else", "name": "Did the sync succeed?",
                  "meta": {"conditions": [{"field": "response.status"}]}}]
        self.assertNotIn("GHL041", rules_hit([wf("Order Sync", steps)]))

    def test_saved_but_unread_response_is_a_medium(self):
        found = findings_for("GHL041",
                             [wf("Order Sync", [webhook(saveResponse=True)])])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_draft_workflows_are_not_checked(self):
        hits = rules_hit([wf("Order Sync", [webhook()], status="draft")])
        self.assertNotIn("GHL041", hits)


class RetrySilentlyDisabledRules(unittest.TestCase):
    def test_retry_plus_continue_is_flagged(self):
        steps = [{"type": "n8n-nodes-base.httpRequest", "name": "Call API",
                  "meta": {"retryOnFail": True, "maxTries": 3,
                           "onError": "continueRegularOutput"}}]
        self.assertIn("GHL042", rules_hit([wf("Sync Worker", steps)]))

    def test_retry_with_stop_workflow_passes(self):
        steps = [{"type": "n8n-nodes-base.httpRequest", "name": "Call API",
                  "meta": {"retryOnFail": True, "maxTries": 3,
                           "onError": "stopWorkflow"}}]
        self.assertNotIn("GHL042", rules_hit([wf("Sync Worker", steps)]))

    def test_a_ghl_step_declaring_neither_key_is_left_alone(self):
        self.assertNotIn("GHL042", rules_hit([wf("Seq", [sms()])]))


class ErrorWorkflowRules(unittest.TestCase):
    N8N_STEP = {"type": "n8n-nodes-base.httpRequest", "name": "Call API",
                "typeVersion": 4, "meta": {}}

    def test_n8n_workflow_without_error_workflow_is_flagged(self):
        self.assertIn("GHL043", rules_hit([wf("Sync", [self.N8N_STEP])]))

    def test_n8n_workflow_with_error_workflow_passes(self):
        w = wf("Sync", [self.N8N_STEP],
               settings={"errorWorkflow": "wf_error_handler"})
        self.assertNotIn("GHL043", rules_hit([w]))

    def test_a_ghl_workflow_is_not_held_to_an_n8n_setting(self):
        self.assertNotIn("GHL043", rules_hit([wf("Seq", [sms()])]))


class CreateVsUpsertRules(unittest.TestCase):
    def test_create_contact_is_flagged_as_duplicate_risk(self):
        steps = [{"type": "create_contact", "name": "New lead",
                  "meta": {"email": "{{ inboundWebhookRequest.email }}"}}]
        self.assertIn("GHL044", rules_hit([wf("Lead Import", steps)]))

    def test_upsert_contact_passes(self):
        steps = [{"type": "upsert_contact", "name": "New or existing lead",
                  "meta": {"email": "{{ inboundWebhookRequest.email }}"}}]
        self.assertNotIn("GHL044", rules_hit([wf("Lead Import", steps)]))


INBOUND_HOOK = {"type": "inbound_webhook", "name": "From the store"}


class InboundDedupeRules(unittest.TestCase):
    def test_side_effect_with_no_dedupe_guard_is_flagged(self):
        w = wf("Order Intake", [sms("Thanks")], [INBOUND_HOOK])
        self.assertIn("GHL045", rules_hit([w]))

    def test_event_id_check_before_the_side_effect_passes(self):
        steps = [{"type": "if_else", "name": "Duplicate event_id? exit",
                  "meta": {"conditions": [
                      {"field": "contact.last_event_id",
                       "value": "{{ inboundWebhookRequest.event_id }}"}]}},
                 sms("Thanks")]
        w = wf("Order Intake", steps, [INBOUND_HOOK])
        self.assertNotIn("GHL045", rules_hit([w]))

    def test_non_webhook_triggers_are_not_checked(self):
        w = wf("Welcome", [sms("Hi")], [{"type": "form_submitted"}])
        self.assertNotIn("GHL045", rules_hit([w]))

    def test_a_webhook_workflow_with_no_side_effects_is_left_alone(self):
        steps = [{"type": "update_contact_field", "name": "Stamp it",
                  "meta": {"field": "last_seen", "value": "now"}}]
        w = wf("Order Intake", steps, [INBOUND_HOOK])
        self.assertNotIn("GHL045", rules_hit([w]))


class RetryLoopBoundRules(unittest.TestCase):
    def test_goto_loop_with_no_counter_is_flagged(self):
        steps = [webhook(), wait("Wait 5 minutes"),
                 {"type": "goto", "name": "Back to the call",
                  "meta": {"targetStepId": "step_1"}}]
        self.assertIn("GHL046", rules_hit([wf("Sync Retry", steps)]))

    def test_goto_guarded_by_an_attempt_counter_passes(self):
        steps = [webhook(), wait("Wait 5 minutes"),
                 {"type": "if_else", "name": "attempt_count under 3?",
                  "meta": {"conditions": [
                      {"field": "contact.attempt_count", "operator": "lt",
                       "value": "3"}]}},
                 {"type": "goto", "name": "Back to the call",
                  "meta": {"targetStepId": "step_1"}}]
        self.assertNotIn("GHL046", rules_hit([wf("Sync Retry", steps)]))

    def test_a_workflow_with_no_goto_is_left_alone(self):
        self.assertNotIn("GHL046", rules_hit([wf("Seq", [sms()])]))

    def test_gotowebinar_node_is_a_product_name_not_a_loop(self):
        # Corpus regression (Gotowebinar_Automate.json): a substring match on
        # "goto" tripped on n8n's GoToWebinar integration node. No loop here.
        steps = [{"type": "n8n-nodes-base.goToWebinar", "name": "GoToWebinar2",
                  "parameters": {"operation": "create",
                                 "resource": "registrant"}},
                 sms("Confirm")]
        self.assertNotIn("GHL046", rules_hit([wf("Webinar Signup", steps)]))

    def test_namespaced_goto_still_counts_as_a_loop(self):
        steps = [webhook(), wait("Wait 5 minutes"),
                 {"type": "workflow.go_to", "name": "Back to the call",
                  "meta": {"targetStepId": "step_1"}}]
        self.assertIn("GHL046", rules_hit([wf("Sync Retry", steps)]))


def field_write(field, name="Set the field"):
    return {"type": "update_contact_field", "name": name,
            "meta": {"field": field, "value": "x"}}


class FieldOwnershipRules(unittest.TestCase):
    def test_two_workflows_writing_one_field_is_flagged(self):
        hits = rules_hit([wf("Intake", [field_write("lead_state")]),
                          wf("Booking", [field_write("lead_state")])])
        self.assertIn("GHL047", hits)

    def test_different_fields_pass(self):
        hits = rules_hit([wf("Intake", [field_write("lead_state")]),
                          wf("Booking", [field_write("booked_at")])])
        self.assertNotIn("GHL047", hits)

    def test_one_workflow_writing_twice_is_not_a_race(self):
        w = wf("Intake", [field_write("lead_state"),
                          field_write("lead_state", "Set it again")])
        self.assertNotIn("GHL047", rules_hit([w]))

    def test_merge_field_targets_are_not_compared(self):
        hits = rules_hit([
            wf("Intake", [field_write("{{ contact.chosen_field }}")]),
            wf("Booking", [field_write("{{ contact.chosen_field }}")])])
        self.assertNotIn("GHL047", hits)


SCHEDULE = {"type": "schedule", "name": "Every night"}


class HeartbeatRules(unittest.TestCase):
    def test_scheduled_workflow_with_no_outbound_call_is_flagged(self):
        w = wf("Nightly Sweep", [field_write("last_swept_at")], [SCHEDULE])
        self.assertIn("GHL048", rules_hit([w]))

    def test_scheduled_workflow_with_a_webhook_is_left_alone(self):
        w = wf("Nightly Sweep", [field_write("last_swept_at"),
                                 webhook("Ping the monitor")], [SCHEDULE])
        self.assertNotIn("GHL048", rules_hit([w]))

    def test_event_triggered_workflows_are_not_checked(self):
        w = wf("Welcome", [sms("Hi")], [{"type": "form_submitted"}])
        self.assertNotIn("GHL048", rules_hit([w]))


def ai_step(name="Classify the reply", **meta):
    return {"type": "chatgpt", "name": name, "meta": meta}


class AiEnumRules(unittest.TestCase):
    def test_unconstrained_ai_output_feeding_a_branch_is_flagged(self):
        steps = [ai_step(prompt="What does this lead want?"),
                 {"type": "if_else", "name": "Route on intent",
                  "meta": {"conditions": [{"field": "contact.intent"}]}}]
        self.assertIn("GHL049", rules_hit([wf("AI Router", steps)]))

    def test_enum_constrained_ai_step_passes(self):
        steps = [ai_step(prompt="Classify the reply",
                         options=["interested", "objection", "opt_out"]),
                 {"type": "if_else", "name": "Route on intent",
                  "meta": {"conditions": [{"field": "contact.intent"}]}}]
        self.assertNotIn("GHL049", rules_hit([wf("AI Router", steps)]))

    def test_ai_step_with_no_branch_after_it_is_left_alone(self):
        steps = [ai_step(prompt="Summarise the conversation"),
                 sms("A human-written follow-up")]
        self.assertNotIn("GHL049", rules_hit([wf("AI Notes", steps)]))

    def test_unconstrained_ai_step_with_nested_parameters_is_flagged(self):
        # n8n-shaped AI node: settings live under `parameters`, not `meta`.
        steps = [{"type": "n8n-nodes-base.openAi", "name": "Classify",
                  "parameters": {"prompt": "What does this lead want?"}},
                 {"type": "if_else", "name": "Route on intent",
                  "meta": {"conditions": [{"field": "contact.intent"}]}}]
        self.assertIn("GHL049", rules_hit([wf("AI Router", steps)]))

    def test_enum_nested_under_parameters_options_passes(self):
        # The enum can hide a level down (parameters.options.categories) —
        # the constraint must be found there, not only at the top level.
        steps = [{"type": "n8n-nodes-base.openAi", "name": "Classify",
                  "parameters": {"prompt": "Classify the reply",
                                 "options": {"categories": [
                                     "interested", "objection", "opt_out"]}}},
                 {"type": "if_else", "name": "Route on intent",
                  "meta": {"conditions": [{"field": "contact.intent"}]}}]
        self.assertNotIn("GHL049", rules_hit([wf("AI Router", steps)]))

    def test_email_and_wait_steps_are_not_mistaken_for_ai(self):
        steps = [email("Plain email"), wait(),
                 {"type": "if_else", "name": "Opened?", "meta": {}}]
        self.assertNotIn("GHL049", rules_hit([wf("Nurture", steps)]))


class AiApprovalRules(unittest.TestCase):
    def test_automatic_send_of_ai_output_is_flagged(self):
        steps = [ai_step("Draft a reply"),
                 sms("Send it", body="{{ ai.reply_draft }}")]
        self.assertIn("GHL050", rules_hit([wf("AI Responder", steps)]))

    def test_manual_send_is_its_own_gate(self):
        steps = [ai_step("Draft a reply"),
                 {"type": "manual_sms", "name": "Review and send",
                  "meta": {"body": "{{ ai.reply_draft }}"}}]
        self.assertNotIn("GHL050", rules_hit([wf("AI Responder", steps)]))

    def test_human_written_copy_is_left_alone(self):
        steps = [ai_step("Classify the reply"),
                 sms("Send it", body="Thanks - a human wrote this.")]
        self.assertNotIn("GHL050", rules_hit([wf("AI Responder", steps)]))


class LegacySignatureRules(unittest.TestCase):
    def test_legacy_only_header_is_critical(self):
        steps = [webhook(headers={"X-WH-Signature": "{{ secret }}"})]
        found = findings_for("GHL051", [wf("Verify Gateway", steps)])
        self.assertEqual([f.severity for f in found], ["critical"])
        self.assertIn("2026", found[0].title)

    def test_both_headers_read_as_a_migration_in_hand(self):
        steps = [webhook(headers={"X-WH-Signature": "{{ old }}",
                                  "X-GHL-Signature": "{{ new }}"})]
        self.assertNotIn("GHL051", rules_hit([wf("Verify Gateway", steps)]))

    def test_legacy_header_in_a_custom_value_is_flagged(self):
        found = findings_for(
            "GHL051", [wf("Seq", [sms()])],
            custom_values={"signature_header": "X-WH-Signature"})
        self.assertEqual([f.workflow for f in found], ["(custom values)"])

    def test_an_account_that_never_mentions_it_is_left_alone(self):
        self.assertNotIn("GHL051", rules_hit([wf("Seq", [sms()])]))


class PoisonResponseRules(unittest.TestCase):
    def test_declared_500_on_a_bad_record_is_flagged(self):
        steps = [{"type": "webhook_reply", "name": "Reject bad records",
                  "meta": {"responseCode": 500}}]
        self.assertIn("GHL052", rules_hit([wf("Order Intake", steps,
                                              [INBOUND_HOOK])]))

    def test_a_200_ack_passes(self):
        steps = [{"type": "webhook_reply", "name": "Ack everything",
                  "meta": {"responseCode": 200}}]
        self.assertNotIn("GHL052", rules_hit([wf("Order Intake", steps,
                                                 [INBOUND_HOOK])]))

    def test_steps_with_no_declared_status_are_left_alone(self):
        self.assertNotIn("GHL052", rules_hit([wf("Seq", [sms()])]))

    def test_n8n_nested_options_response_code_500_is_flagged(self):
        # Corpus regression: n8n's respondToWebhook declares the code at
        # parameters.options.responseCode — 41 corpus workflows carried a
        # non-2xx there and a top-level-only config scan missed all of them.
        steps = [{"type": "n8n-nodes-base.respondToWebhook",
                  "name": "Respond to Webhook",
                  "parameters": {"respondWith": "text",
                                 "options": {"responseCode": 500}}}]
        self.assertIn("GHL052", rules_hit([wf("Order Intake", steps,
                                              [INBOUND_HOOK])]))

    def test_n8n_nested_options_200_ack_passes(self):
        steps = [{"type": "n8n-nodes-base.respondToWebhook",
                  "name": "Respond to Webhook",
                  "parameters": {"respondWith": "text",
                                 "options": {"responseCode": 200}}}]
        self.assertNotIn("GHL052", rules_hit([wf("Order Intake", steps,
                                                 [INBOUND_HOOK])]))
