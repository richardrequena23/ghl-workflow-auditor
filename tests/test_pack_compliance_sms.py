"""GHL053-GHL058 — the SMS compliance pack.

Every rule here accuses a client of something expensive: texting without
consent, texting outside the legal hours, texting content the carriers will not
carry. So every rule gets a workflow that trips it AND the nearest correct
workflow that must not, because the false positive is the one that gets the
whole report thrown away.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.config import AuditConfig  # noqa: E402
from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import run, run_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FRAGMENT = os.path.join(HERE, "..", "examples", "packs", "compliance_sms.json")
MINE = {"GHL053", "GHL054", "GHL055", "GHL056", "GHL057", "GHL058"}


def bundle(workflows, custom_values=None, **extra):
    data = {"workflows": workflows, "customValues": custom_values or {}}
    data.update(extra)
    return data


def audit(workflows, custom_values=None, config=None, **extra):
    return run(Account.load(bundle(workflows, custom_values, **extra),
                            config=config))


def rules_hit(workflows, custom_values=None, config=None, **extra):
    return {f.rule for f in audit(workflows, custom_values, config, **extra)}


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


def wait(name="Wait", **meta):
    return {"type": "wait", "name": name, "meta": dict(meta) or {"delay": "2 days"}}


def trigger(kind, name="Trigger", filters=()):
    return {"type": kind, "name": name, "filters": list(filters)}


TAG_TRIGGER = trigger("contact_tag_added", "Tag added",
                      [{"field": "tag", "value": "reactivate"}])
FORM_TRIGGER = trigger("form_submitted", "Form", [{"field": "form", "value": "Intake"}])
IDENTIFIED = "Hi {{ contact.first_name }}, this is Dana at Northgate Roofing."


class ConsentBeforeTheFirstText(unittest.TestCase):
    """GHL053 — an SMS campaign aimed at a list, with no opt-in check."""

    def test_triggerless_list_campaign_fires(self):
        self.assertIn("GHL053", rules_hit([wf("Cold List Blast", [sms()])]))

    def test_opt_in_gate_clears_it(self):
        steps = [{"type": "if_else", "name": "Has SMS opt-in?",
                  "meta": {"field": "sms_opt_in", "value": "yes"}}, sms()]
        self.assertNotIn("GHL053", rules_hit([wf("Cold List Blast", steps)]))

    def test_tag_triggered_reactivation_fires(self):
        hits = rules_hit([wf("Database Reactivation", [sms()], [TAG_TRIGGER])])
        self.assertIn("GHL053", hits)

    def test_ordinary_tag_triggered_sequence_does_not_fire(self):
        """The normal way to run any campaign in GHL must not trip this."""
        hits = rules_hit([wf("Review Request After Close", [sms()], [TAG_TRIGGER])])
        self.assertNotIn("GHL053", hits)

    def test_form_trigger_is_its_own_consent_record(self):
        hits = rules_hit([wf("Purchased List Blast", [sms()], [FORM_TRIGGER])])
        self.assertNotIn("GHL053", hits)

    def test_opt_in_tag_on_the_trigger_clears_it(self):
        trg = trigger("contact_tag_added", "Tag added",
                      [{"field": "tag", "value": "sms-opt-in"}])
        self.assertNotIn("GHL053", rules_hit([wf("Cold List Blast", [sms()], [trg])]))

    def test_a_name_that_merely_contains_list_is_not_a_list(self):
        """'Specialist' and 'Waitlist' contain 'list'. Neither is a bought one."""
        hits = rules_hit([wf("Specialist Waitlist Follow Up", [sms()],
                             [TAG_TRIGGER])])
        self.assertNotIn("GHL053", hits)

    def test_email_only_list_campaign_is_not_an_sms_finding(self):
        self.assertNotIn("GHL053", rules_hit([wf("Cold List Blast", [email()])]))

    def test_inbound_reply_trigger_does_not_fire(self):
        trg = trigger("inbound_message", "They replied")
        self.assertNotIn("GHL053", rules_hit([wf("Cold List Blast", [sms()], [trg])]))

    def test_draft_is_not_audited(self):
        hits = rules_hit([wf("Cold List Blast", [sms()], status="draft")])
        self.assertNotIn("GHL053", hits)


class QuietHoursBounds(unittest.TestCase):
    """GHL054 — the window exists, and its numbers are outside the law."""

    def _window(self, start, end, steps=None, settings_extra=None):
        settings = {"sendingWindow": {"start": start, "end": end},
                    "timezone": "contact"}
        settings.update(settings_extra or {})
        return wf("Follow Up", steps or [sms()], [TAG_TRIGGER], settings=settings)

    def test_opening_before_8am_is_critical(self):
        found = findings_for("GHL054", [self._window("07:00", "20:00")])
        self.assertEqual([f.severity for f in found], ["critical"])

    def test_closing_after_9pm_is_critical(self):
        found = findings_for("GHL054", [self._window("09:00", "22:00")])
        self.assertEqual([f.severity for f in found], ["critical"])

    def test_a_legal_window_passes(self):
        self.assertNotIn("GHL054", rules_hit([self._window("09:00", "20:00")]))

    def test_the_safe_nationwide_default_passes(self):
        self.assertNotIn("GHL054", rules_hit([self._window("08:00", "20:00")]))

    def test_the_federal_ceiling_is_only_the_state_finding(self):
        """9pm clears the federal rule and is still exposed in 8pm states."""
        found = findings_for("GHL054", [self._window("08:00", "21:00")])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_half_past_eight_is_the_state_finding(self):
        found = findings_for("GHL054", [self._window("08:00", "20:30")])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_overnight_window_fires(self):
        found = findings_for("GHL054", [self._window("20:00", "08:00")])
        self.assertEqual([f.severity for f in found], ["critical"])
        self.assertIn("overnight", found[0].title)

    def test_am_pm_bounds_are_read(self):
        found = findings_for("GHL054", [self._window("9am", "10pm")])
        self.assertEqual([f.severity for f in found], ["critical"])

    def test_email_only_workflow_is_not_a_quiet_hours_finding(self):
        hits = rules_hit([self._window("06:00", "23:00", steps=[email()])])
        self.assertNotIn("GHL054", hits)

    def test_unreadable_bounds_say_nothing(self):
        window = self._window("{{ custom_values.window_start }}",
                              "{{ custom_values.window_end }}")
        self.assertNotIn("GHL054", rules_hit([window]))

    def test_window_on_a_wait_step_is_checked_too(self):
        steps = [{"type": "wait", "name": "Overnight hold",
                  "meta": {"delay": "1 day",
                           "window": {"start": "06:00", "end": "23:00"}}},
                 sms()]
        found = findings_for("GHL054", [wf("Drip", steps, [TAG_TRIGGER])])
        self.assertEqual([f.step for f in found], ["Overnight hold"])


class ReplyKeywords(unittest.TestCase):
    """GHL055 — a dead opt-out word, or a live one offered for the wrong job."""

    def test_custom_opt_out_word_nothing_listens_for(self):
        body = "You're on standby. Reply REMOVE to be taken off."
        self.assertIn("GHL055", rules_hit([wf("Standby", [sms(body=body)])]))

    def test_standard_stop_language_passes(self):
        body = "Quote's on the way. Reply STOP to opt out."
        self.assertNotIn("GHL055", rules_hit([wf("Standby", [sms(body=body)])]))

    def test_a_listener_for_the_word_clears_it(self):
        body = "You're on standby. Reply REMOVE to be taken off."
        catcher = wf("Keyword Catcher", [{"type": "set_dnd", "name": "DND"}],
                     [trigger("inbound_message", "Keyword",
                              [{"field": "message", "value": "remove"}])])
        hits = rules_hit([wf("Standby", [sms(body=body)]), catcher])
        self.assertNotIn("GHL055", hits)

    def test_a_working_keyword_in_the_same_text_clears_it(self):
        body = "Reply BOOK to grab a time, or STOP to opt out."
        self.assertNotIn("GHL055", rules_hit([wf("Booking", [sms(body=body)])]))

    def test_confirmation_keyword_is_not_an_opt_out(self):
        body = "Reply YES to confirm Tuesday at 10am."
        self.assertNotIn("GHL055", rules_hit([wf("Confirm", [sms(body=body)])]))

    def test_cancel_offered_for_an_appointment_fires(self):
        body = "You're booked for Tuesday. Reply CANCEL to cancel your appointment."
        found = findings_for("GHL055", [wf("Booking", [sms(body=body)])])
        self.assertEqual(len(found), 1)
        self.assertIn("opts", found[0].title)

    def test_end_offered_to_end_the_messages_is_correct_usage(self):
        body = "Reply END to end these messages."
        self.assertNotIn("GHL055", rules_hit([wf("Nudge", [sms(body=body)])]))

    def test_intent_before_the_keyword_is_read(self):
        body = "To opt out, reply OFF and we'll take you off."
        self.assertIn("GHL055", rules_hit([wf("Nudge", [sms(body=body)])]))

    def test_email_copy_is_not_checked(self):
        body = "You're on standby. Reply REMOVE to be taken off."
        self.assertNotIn("GHL055", rules_hit([wf("Standby", [email(body=body)])]))

    def test_draft_is_not_audited(self):
        body = "You're on standby. Reply REMOVE to be taken off."
        hits = rules_hit([wf("Standby", [sms(body=body)], status="draft")])
        self.assertNotIn("GHL055", hits)


class RestrictedContent(unittest.TestCase):
    """GHL056 — SHAFT and its 10DLC neighbours, in a message body."""

    def _text(self, body):
        return [wf("Promo", [sms(body=body)], [TAG_TRIGGER])]

    def test_alcohol_fires(self):
        hits = rules_hit(self._text("Two-for-one wine flights on Friday."))
        self.assertIn("GHL056", hits)

    def test_cannabis_fires(self):
        hits = rules_hit(self._text("New dispensary opening on 5th."))
        self.assertIn("GHL056", hits)

    def test_firearms_fire(self):
        hits = rules_hit(self._text("Handgun safety course, seats left."))
        self.assertIn("GHL056", hits)

    def test_gambling_fires(self):
        hits = rules_hit(self._text("Casino night Saturday - tables from 7pm."))
        self.assertIn("GHL056", hits)

    def test_debt_relief_fires(self):
        hits = rules_hit(self._text("Ask us about debt relief options today."))
        self.assertIn("GHL056", hits)

    def test_ordinary_copy_passes(self):
        hits = rules_hit(self._text("Your roof inspection is booked for Tuesday."))
        self.assertNotIn("GHL056", hits)

    def test_lawn_care_copy_is_not_cannabis(self):
        """'weed' is left out of the pattern on purpose — this is why."""
        hits = rules_hit(self._text("Weed control and a spring tidy - want a quote?"))
        self.assertNotIn("GHL056", hits)

    def test_step_names_are_not_scanned(self):
        steps = [sms(name="Wine Club Reminder", body="Your table is confirmed.")]
        self.assertNotIn("GHL056", rules_hit([wf("Promo", steps, [TAG_TRIGGER])]))

    def test_email_bodies_are_not_scanned(self):
        steps = [email(body="Two-for-one wine flights on Friday.")]
        self.assertNotIn("GHL056", rules_hit([wf("Promo", steps, [TAG_TRIGGER])]))

    def test_the_matched_phrase_is_reported(self):
        found = findings_for("GHL056", self._text("Free tequila tasting Friday."))
        self.assertIn("tequila", found[0].title)


class FirstTouchIdentification(unittest.TestCase):
    """GHL057 — the opening message with no business name in it."""

    def _opener(self, body, triggers=(TAG_TRIGGER,), steps=None):
        return [wf("Opener", steps or [sms(body=body)], list(triggers))]

    def test_anonymous_opener_fires(self):
        self.assertIn("GHL057", rules_hit(self._opener("Still thinking about it?")))

    def test_named_business_passes(self):
        self.assertNotIn("GHL057", rules_hit(self._opener(IDENTIFIED)))

    def test_brand_before_here_passes(self):
        body = "Northgate Roofing here - still want that quote?"
        self.assertNotIn("GHL057", rules_hit(self._opener(body)))

    def test_location_merge_field_passes(self):
        body = "Hi {{ contact.first_name }}, {{ location.name }} here."
        self.assertNotIn("GHL057", rules_hit(self._opener(body)))

    def test_business_name_custom_value_passes(self):
        body = "Quick one from {{ custom_values.business_name }} - free Tuesday?"
        self.assertNotIn("GHL057", rules_hit(self._opener(body)))

    def test_reply_to_a_form_is_not_a_cold_opener(self):
        hits = rules_hit(self._opener("Still thinking about it?", (FORM_TRIGGER,)))
        self.assertNotIn("GHL057", hits)

    def test_appointment_trigger_is_exempt(self):
        trg = trigger("appointment_status", "Appt",
                      [{"field": "appointment_status", "value": "noshow"}])
        hits = rules_hit(self._opener("Sorry we missed you.", (trg,)))
        self.assertNotIn("GHL057", hits)

    def test_reply_triggered_workflow_is_exempt(self):
        trg = trigger("inbound_message", "They replied")
        hits = rules_hit(self._opener("Got it - when suits?", (trg,)))
        self.assertNotIn("GHL057", hits)

    def test_email_first_is_not_checked(self):
        steps = [email(body="Welcome"), sms(body="Still thinking about it?")]
        self.assertNotIn("GHL057", rules_hit(self._opener("", steps=steps)))

    def test_empty_body_says_nothing(self):
        self.assertNotIn("GHL057", rules_hit(self._opener("   ")))


class TextsPerDay(unittest.TestCase):
    """GHL058 — three messages on one phone inside twenty-four hours."""

    def test_three_texts_in_five_hours_fires(self):
        steps = [sms("One"), wait(delay="2 hours"), sms("Two"),
                 wait(delay="3 hours"), sms("Three")]
        found = findings_for("GHL058", [wf("Push", steps, [FORM_TRIGGER])])
        self.assertEqual(len(found), 1)
        self.assertIn("3 texts", found[0].title)

    def test_value_and_unit_shape_is_read(self):
        steps = [sms("One"), {"type": "wait", "name": "Wait",
                              "meta": {"value": 90, "unit": "minutes"}},
                 sms("Two"), {"type": "wait", "name": "Wait",
                              "meta": {"value": 2, "unit": "hours"}},
                 sms("Three")]
        self.assertIn("GHL058", rules_hit([wf("Push", steps, [FORM_TRIGGER])]))

    def test_a_text_a_day_passes(self):
        steps = [sms("One"), wait(delay="1 day"), sms("Two"),
                 wait(delay="1 day"), sms("Three")]
        self.assertNotIn("GHL058", rules_hit([wf("Drip", steps, [FORM_TRIGGER])]))

    def test_two_in_an_hour_passes(self):
        steps = [sms("One"), wait(delay="30 minutes"), sms("Two")]
        self.assertNotIn("GHL058", rules_hit([wf("Push", steps, [FORM_TRIGGER])]))

    def test_a_reply_wait_stops_the_clock(self):
        """A wait that ends on an event can release in a second or never."""
        steps = [sms("One"),
                 {"type": "wait", "name": "Wait for reply",
                  "meta": {"waitType": "contact_reply", "delay": "unlimited"}},
                 sms("Two"), sms("Three")]
        self.assertNotIn("GHL058", rules_hit([wf("Push", steps, [FORM_TRIGGER])]))

    def test_an_unreadable_delay_is_not_guessed_at(self):
        steps = [sms("One"), {"type": "wait", "name": "Wait 4 hours", "meta": {}},
                 sms("Two"), sms("Three")]
        self.assertNotIn("GHL058", rules_hit([wf("Push", steps, [FORM_TRIGGER])]))

    def test_a_drip_step_stops_the_clock(self):
        steps = [sms("One"), {"type": "drip", "name": "Throttle"}, sms("Two"),
                 sms("Three")]
        self.assertNotIn("GHL058", rules_hit([wf("Push", steps, [FORM_TRIGGER])]))

    def test_appointment_reminder_ladder_is_exempt(self):
        trg = trigger("appointment", "Appt",
                      [{"field": "appointment_status", "value": "confirmed"}])
        steps = [sms("Day before"), wait(delay="4 hours"), sms("Morning of"),
                 wait(delay="2 hours"), sms("One hour")]
        self.assertNotIn("GHL058", rules_hit([wf("Reminders", steps, [trg])]))

    def test_transactional_workflows_are_exempt(self):
        steps = [sms("One"), wait(delay="1 hour"), sms("Two"),
                 wait(delay="1 hour"), sms("Three")]
        config = AuditConfig.from_dict({"transactional_workflows": ["Receipts"]})
        hits = rules_hit([wf("Receipts", steps, [FORM_TRIGGER])], config=config)
        self.assertNotIn("GHL058", hits)

    def test_the_worst_day_is_the_one_reported(self):
        """A long drip with three touches bunched at the end still counts."""
        steps = [sms("One"), wait(delay="30 days"), sms("Two"),
                 wait(delay="1 hour"), sms("Three"), wait(delay="1 hour"),
                 sms("Four")]
        found = findings_for("GHL058", [wf("Drip", steps, [FORM_TRIGGER])])
        self.assertEqual(len(found), 1)
        self.assertIn("3 texts", found[0].title)
        self.assertIn("2 hours", found[0].title)


class Robustness(unittest.TestCase):
    """Malformed exports must report or stay quiet, never raise."""

    CASES = [
        [{"name": "x", "status": "published", "steps": None, "triggers": None,
          "settings": None}],
        [{"name": "Cold List", "status": "published", "steps": "not a list"}],
        [{"name": "Cold List", "status": "published", "steps": ["bare string"],
          "triggers": [["a", "b"]]}],
        [{"name": "Cold List", "status": "published", "settings": "windowed",
          "steps": [{"type": "sms", "meta": {"body": ["a", "list"]}}]}],
        [{"name": "Blast", "status": "published",
          "settings": {"sendingWindow": {"start": ["07:00"], "end": None}},
          "steps": [{"type": "sms", "name": None}]}],
        [{"name": "Blast", "status": "published",
          "steps": [{"type": "wait", "meta": {"delay": {"value": "soon"}}},
                    {"type": "sms"}, {"type": "sms"}, {"type": "sms"}]}],
    ]

    def test_no_input_shape_raises(self):
        for data in self.CASES:
            run_all(Account.load(data))


class ExampleFragment(unittest.TestCase):
    """The shipped fragment is the proof: all six fire, nothing skips."""

    def setUp(self):
        with open(FRAGMENT) as fh:
            self.acct = Account.load(json.load(fh))
        self.findings, self.skips = run_all(self.acct)

    def test_every_rule_in_this_pack_fires(self):
        tripped = {f.rule for f in self.findings} & MINE
        self.assertEqual(sorted(MINE - tripped), [])

    def test_no_rule_in_this_pack_skips(self):
        self.assertEqual(sorted({s.rule for s in self.skips} & MINE), [])

    def test_every_finding_explains_what_it_costs(self):
        bare = [f.rule for f in self.findings if f.rule in MINE and not f.cost.strip()]
        self.assertEqual(sorted(set(bare)), [])

    def test_workflow_names_are_namespaced_to_this_pack(self):
        strays = [w.name for w in self.acct.workflows
                  if not w.name.startswith("Compliance SMS - ")]
        self.assertEqual(strays, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
