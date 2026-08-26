"""Calendar and booking pack — GHL065-GHL070.

Every check here fires on a broken booking lane and stays quiet on a correct
one. The quiet half is the important half: an account's booking workflows are
the ones the client looks at first, and a false positive in this section is the
one that makes them stop reading.

Roughly half the cases below are correct configurations that an earlier draft
of this pack reported. Each of those is named for the shape it defends —
cancellation-policy copy, a branch read as a ladder rung, a no-show FEE line, a
zone spelled out in words — because those are the sentences a real account is
full of, and a regex written against the broken case matches all of them.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import run, run_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FRAGMENT = os.path.join(HERE, "..", "examples", "packs", "calendar_booking.json")
BASE = os.path.join(HERE, "..", "examples", "base-account.json")

MINE = {"GHL065", "GHL066", "GHL067", "GHL068", "GHL069", "GHL070"}

# HighLevel object ids are base62 and about twenty characters long, with no
# separators. The pack only judges a booking link whose token looks like one,
# so the fixtures have to look like one too.
LIVE_CAL = "CVokAlI8fgw4WYWoCtQz"
DEAD_CAL = "7dQ2mKpX1nRvYtLzBa04"
CALENDARS = [{"id": LIVE_CAL, "name": "Strategy Call"}]


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


def skips_hit(workflows, custom_values=None, **extra):
    return {s.rule for s in audit_all(workflows, custom_values, **extra)[1]}


def findings_for(rule_id, workflows, custom_values=None, **extra):
    return [f for f in audit(workflows, custom_values, **extra)
            if f.rule == rule_id]


def wf(name, steps, triggers=(), status="published", settings=None):
    return {"_id": name, "name": name, "status": status, "steps": list(steps),
            "triggers": list(triggers), "settings": settings or {}}


def sms(name="Message", body="hello"):
    return {"type": "sms", "name": name, "meta": {"body": body}}


def email(name="Email", body="hello", subject="Hi"):
    return {"type": "email", "name": name,
            "meta": {"subject": subject, "body": body}}


def wait(name="Wait 2 days", value=2, unit="days"):
    """A plain duration wait — it counts from enrollment, not from the slot."""
    return {"type": "wait", "name": name,
            "meta": {"delay": {"value": value, "unit": unit}}}


def appt_wait(name="Until 24 hours before", value=24, unit="hours",
              direction="before", meta=None):
    """A wait anchored to the appointment, in the shape GHL exports it."""
    return {"type": "event_start_wait", "name": name,
            "meta": meta if meta is not None else {
                "waitType": "appointment_time",
                "offset": {"value": value, "unit": unit,
                           "direction": direction}}}


def booked(status="confirmed"):
    return {"type": "appointment_status", "name": "Booked",
            "filters": [{"field": "appointment_status", "value": status}]}


class ReminderTimedOffTheBooking(unittest.TestCase):
    """GHL065 — a reminder released by a duration wait, not by the slot."""

    def test_fixed_wait_before_a_dated_reminder_is_flagged(self):
        steps = [sms("Confirmation", "You're booked."), wait(),
                 sms("24 hour reminder", "Your call is tomorrow.")]
        self.assertIn("GHL065", rules_hit([wf("Reminders", steps, [booked()])]))

    def test_appointment_relative_wait_passes(self):
        steps = [sms("Confirmation", "You're booked."), appt_wait(),
                 sms("24 hour reminder", "Your call is tomorrow.")]
        self.assertNotIn("GHL065",
                         rules_hit([wf("Reminders", steps, [booked()])]))

    def test_a_wait_named_as_relative_counts_as_anchored(self):
        """The builder writes the anchor in the label when the export drops it."""
        steps = [{"type": "wait", "name": "Until 24 hours before"},
                 sms("24 hour reminder", "Your call is tomorrow.")]
        self.assertNotIn("GHL065",
                         rules_hit([wf("Reminders", steps, [booked()])]))

    def test_a_confirmation_with_no_wait_above_it_is_not_flagged(self):
        steps = [sms("Confirmation", "See you at your appointment time.")]
        self.assertNotIn("GHL065",
                         rules_hit([wf("Reminders", steps, [booked()])]))

    def test_copy_that_never_states_a_time_is_not_flagged(self):
        steps = [sms("Confirmation", "You're booked."), wait(),
                 sms("Nudge", "Anything you want to cover on the call?")]
        self.assertNotIn("GHL065",
                         rules_hit([wf("Reminders", steps, [booked()])]))

    def test_a_non_appointment_lane_is_not_checked(self):
        steps = [sms("One", "hi"), wait(), sms("Two", "Your call is tomorrow.")]
        trigger = [{"type": "contact_tag_added", "name": "Tagged",
                    "filters": [{"tag": "nurture-me"}]}]
        self.assertNotIn("GHL065", rules_hit([wf("Nurture", steps, trigger)]))

    def test_the_cancellation_lane_is_not_a_reminder_ladder(self):
        steps = [sms("Sorry", "No problem."), wait(),
                 sms("Rebook", "Can you do tomorrow?")]
        trigger = [{"type": "appointment_status", "name": "Cancelled",
                    "filters": [{"field": "appointment_status",
                                 "value": "cancelled"}]}]
        self.assertNotIn("GHL065", rules_hit([wf("Rebook", steps, trigger)]))

    def test_drafts_are_not_audited(self):
        steps = [sms("Confirmation", "You're booked."), wait(),
                 sms("24 hour reminder", "Your call is tomorrow.")]
        hits = rules_hit([wf("Reminders", steps, [booked()], status="draft")])
        self.assertNotIn("GHL065", hits)

    def test_one_finding_per_workflow(self):
        steps = [wait(), sms("A", "Your call is tomorrow."),
                 wait(), sms("B", "Your session starts in 1 hour.")]
        found = findings_for("GHL065", [wf("Reminders", steps, [booked()])])
        self.assertEqual(len(found), 1)

    # -- correct configurations an earlier draft reported ------------------

    def test_a_cancellation_policy_is_not_a_reminder(self):
        """'24 hours before your appointment' is the policy every confirmation
        carries. Read as a lead time it made the whole booking lane critical."""
        steps = [sms("Confirmation", "You're booked."), wait("Wait 1 hour", 1,
                                                             "hours"),
                 email("What to expect",
                       "Here is how the session runs. If you need to "
                       "reschedule, please give us at least 24 hours before "
                       "your appointment.")]
        self.assertNotIn("GHL065", rules_hit([wf("Confirm", steps, [booked()])]))

    def test_see_you_at_an_address_is_not_a_time(self):
        steps = [sms("Confirmation", "You're booked."), wait(),
                 sms("Directions",
                     "See you at 4400 Main St, suite 210. Parking is out back.")]
        self.assertNotIn("GHL065", rules_hit([wf("Confirm", steps, [booked()])]))

    def test_paperwork_timing_is_not_appointment_timing(self):
        """'in a day or two' is about the intake form, not about the call."""
        steps = [sms("Confirmation", "You're booked."), wait(),
                 sms("Intake", "Our team will send your form in a day or two.")]
        self.assertNotIn("GHL065", rules_hit([wf("Confirm", steps, [booked()])]))

    def test_a_no_show_fee_line_is_not_a_reminder(self):
        steps = [sms("Confirmation", "Booked."), wait(),
                 sms("Terms", "Cancel at least 24 hours before to avoid the "
                              "no-show fee.")]
        self.assertNotIn("GHL065", rules_hit([wf("Confirm", steps, [booked()])]))

    def test_a_step_label_naming_a_lead_time_is_evidence_on_its_own(self):
        """A label is never a cancellation policy — a builder only writes
        '1 hour before' on a step they meant to fire against the slot."""
        steps = [wait(), sms("Reminder 1 hour before", "Talk shortly.")]
        self.assertIn("GHL065", rules_hit([wf("Reminders", steps, [booked()])]))

    def test_a_duration_wait_labelled_after_does_not_excuse_the_ladder(self):
        """'Wait 1 day after booking' is a drip wait, not an appointment anchor,
        so it must not be accepted as the thing that times the reminder."""
        steps = [wait("Wait 1 day after booking", 1, "days"),
                 sms("Reminder", "Your call is tomorrow.")]
        self.assertIn("GHL065", rules_hit([wf("Reminders", steps, [booked()])]))


class ReminderOffsetsRunBackwards(unittest.TestCase):
    """GHL066 — a ladder that walks away from the appointment."""

    def test_one_hour_then_twenty_four_hours_is_flagged(self):
        steps = [appt_wait("Until 1 hour before", 1, "hours"), sms("Now"),
                 appt_wait("Until 24 hours before", 24, "hours"), sms("Later")]
        self.assertIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_twenty_four_hours_then_one_hour_passes(self):
        steps = [appt_wait("Until 24 hours before", 24, "hours"), sms("First"),
                 appt_wait("Until 1 hour before", 1, "hours"), sms("Second")]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_two_waits_targeting_the_same_moment_are_flagged(self):
        steps = [appt_wait("Until 1 hour before", 1, "hours"), sms("First"),
                 appt_wait("Also 1 hour before", 1, "hours"), sms("Second")]
        self.assertIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_before_then_after_passes(self):
        steps = [appt_wait("Until 1 hour before", 1, "hours"), sms("Reminder"),
                 appt_wait("2 hours after", 2, "hours", "after"),
                 sms("Recap")]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_after_then_before_is_flagged(self):
        steps = [appt_wait("2 hours after", 2, "hours", "after"), sms("Recap"),
                 appt_wait("Until 1 hour before", 1, "hours"), sms("Reminder")]
        self.assertIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_single_anchored_wait_cannot_be_out_of_order(self):
        steps = [appt_wait("Until 1 hour before", 1, "hours"), sms("Reminder")]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_an_anchored_wait_with_no_readable_offset_is_left_alone(self):
        """Anchored but silent about how far. None is not zero."""
        vague = {"type": "event_start_wait", "name": "Wait for the appointment",
                 "meta": {"waitType": "appointment_time"}}
        steps = [vague, sms("First"), vague, sms("Second")]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_duration_wait_between_rungs_does_not_hide_the_reversal(self):
        steps = [appt_wait("Until 1 hour before", 1, "hours"), sms("First"),
                 wait("Wait 10 minutes", 10, "minutes"),
                 appt_wait("Until 24 hours before", 24, "hours"), sms("Second")]
        self.assertIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_reach_counts_only_the_sends_below_the_bad_wait(self):
        steps = [appt_wait("Until 1 hour before", 1, "hours"), sms("First"),
                 appt_wait("Until 24 hours before", 24, "hours"),
                 sms("Second"), sms("Third")]
        found = findings_for("GHL066", [wf("Ladder", steps, [booked()])])
        self.assertEqual([f.reach for f in found], [2])

    def test_minutes_and_days_are_compared_on_the_same_scale(self):
        """90 minutes is closer to the call than 2 days — and must read that way."""
        steps = [appt_wait("Until 2 days before", 2, "days"), sms("First"),
                 appt_wait("Until 90 minutes before", 90, "minutes"),
                 sms("Second")]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    # -- correct configurations an earlier draft reported ------------------

    def test_two_branches_of_one_if_else_are_not_a_ladder(self):
        """Branch children are flattened into the step list. A same-day branch
        and an everything-else branch never run in sequence, so comparing them
        reported a reversal that cannot happen."""
        steps = [
            {"type": "if_else", "id": "b1", "name": "Booked for today?",
             "meta": {"branches": [{"id": "b1-yes", "name": "yes"},
                                   {"id": "b1-no", "name": "no"}]},
             "next": ["b1-yes", "b1-no"]},
            {"type": "event_start_wait", "id": "wyes", "parentKey": "b1-yes",
             "name": "Until 1 hour before",
             "meta": {"waitType": "appointment_time", "hoursBefore": 1}},
            {"type": "sms", "id": "syes", "parentKey": "wyes", "name": "Soon",
             "meta": {"body": "See you shortly."}},
            {"type": "event_start_wait", "id": "wno", "parentKey": "b1-no",
             "name": "Until 24 hours before",
             "meta": {"waitType": "appointment_time", "hoursBefore": 24}},
            {"type": "sms", "id": "sno", "parentKey": "wno", "name": "Day before",
             "meta": {"body": "Reminder about tomorrow."}},
        ]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_branch_between_the_rungs_of_a_flat_export_is_unknowable(self):
        """No wiring to read, a branch in the way: the order cannot be proven,
        and unknown has to mean quiet."""
        steps = [appt_wait("Until 1 hour before", 1, "hours"), sms("First"),
                 {"type": "if_else", "name": "VIP?", "meta": {"branches": []}},
                 appt_wait("Until 24 hours before", 24, "hours"), sms("Second")]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_wired_straight_chain_still_reports_the_reversal(self):
        steps = [
            {"type": "event_start_wait", "id": "w1",
             "name": "Until 1 hour before",
             "meta": {"waitType": "appointment_time", "hoursBefore": 1}},
            {"type": "sms", "id": "s1", "parentKey": "w1", "name": "A",
             "meta": {"body": "hi"}},
            {"type": "event_start_wait", "id": "w2", "parentKey": "s1",
             "name": "Until 24 hours before",
             "meta": {"waitType": "appointment_time", "hoursBefore": 24}},
            {"type": "sms", "id": "s2", "parentKey": "w2", "name": "B",
             "meta": {"body": "hi"}},
        ]
        self.assertIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_drip_wait_named_after_is_not_a_rung(self):
        """'Wait 1 day after booking' read as +1 day from the appointment turned
        an ordinary nurture pause into the top of a reversed ladder."""
        steps = [sms("Confirmation", "You're booked."),
                 wait("Wait 1 day after booking", 1, "days"),
                 email("Prep", "Here is what to bring."),
                 appt_wait("Until 1 hour before", 1, "hours"),
                 sms("Soon", "Talk shortly.")]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_labelled_wait_with_a_real_duration_is_a_duration_wait(self):
        """The configured delay outranks the label, in both directions."""
        steps = [{"type": "wait", "name": "Until 24 hours before",
                  "meta": {"delay": {"value": 3, "unit": "days"}}},
                 sms("First"),
                 appt_wait("Until 1 hour before", 1, "hours"), sms("Second")]
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))


class BookingLinkBypassesTheCalendar(unittest.TestCase):
    """GHL067 — links that book somewhere this account cannot see."""

    def test_third_party_scheduler_link_is_flagged(self):
        steps = [sms("Book", "Grab a time: https://calendly.com/acme/intro")]
        self.assertIn("GHL067", rules_hit([wf("Booking", steps)],
                                          calendars=CALENDARS))

    def test_widget_link_to_an_unknown_calendar_is_critical(self):
        steps = [sms("Book", "Here you go: "
                             f"https://api.leadconnectorhq.com/widget/booking/{DEAD_CAL}")]
        found = findings_for("GHL067", [wf("Booking", steps)],
                             calendars=CALENDARS)
        self.assertEqual([f.severity for f in found], ["critical"])

    def test_widget_link_to_a_live_calendar_passes(self):
        steps = [sms("Book", "Here you go: "
                             f"https://link.msgsndr.com/widget/booking/{LIVE_CAL}")]
        self.assertNotIn("GHL067", rules_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))

    def test_a_white_labelled_domain_is_read_the_same_way(self):
        steps = [sms("Book",
                     f"https://book.theclient.com/widget/booking/{LIVE_CAL}")]
        self.assertNotIn("GHL067", rules_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))

    def test_no_calendar_list_makes_the_widget_half_a_skip(self):
        steps = [sms("Book",
                     f"https://api.leadconnectorhq.com/widget/booking/{DEAD_CAL}")]
        self.assertIn("GHL067", skips_hit([wf("Booking", steps)]))

    def test_the_third_party_half_needs_no_account_context(self):
        steps = [sms("Book", "Grab a time: https://calendly.com/acme/intro")]
        self.assertIn("GHL067", rules_hit([wf("Booking", steps)]))
        self.assertNotIn("GHL067", skips_hit([wf("Booking", steps)]))

    def test_a_merge_field_link_carries_no_id_to_check(self):
        steps = [sms("Book", "Here you go: {{ custom_values.booking_link }}")]
        self.assertNotIn("GHL067", rules_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))
        self.assertNotIn("GHL067", skips_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))

    def test_an_internal_notification_is_not_a_message_to_a_contact(self):
        steps = [{"type": "internal_notification", "name": "Tell the rep",
                  "meta": {"body": "Book them: https://calendly.com/acme/intro"}}]
        self.assertNotIn("GHL067", rules_hit([wf("Alert", steps)],
                                             calendars=CALENDARS))

    def test_an_ordinary_link_is_not_a_booking_link(self):
        steps = [email("Welcome", "Read this first: https://example.com/guide")]
        self.assertNotIn("GHL067", rules_hit([wf("Welcome", steps)],
                                             calendars=CALENDARS))

    def test_drafts_are_not_audited(self):
        steps = [sms("Book", "Grab a time: https://calendly.com/acme/intro")]
        hits = rules_hit([wf("Booking", steps, status="draft")],
                         calendars=CALENDARS)
        self.assertNotIn("GHL067", hits)

    # -- correct configurations an earlier draft reported ------------------

    def test_one_link_repeated_through_an_email_is_one_finding(self):
        """Button, body copy, footer — a normal email carries the same link
        three times, and three identical findings is how a report loses a
        reader."""
        steps = [email("Book", "Pick a time: https://calendly.com/acme/intro\n"
                               "Or here: https://calendly.com/acme/intro\n"
                               "Footer: https://calendly.com/acme/intro")]
        found = findings_for("GHL067", [wf("Booking", steps)],
                             calendars=CALENDARS)
        self.assertEqual(len(found), 1)

    def test_a_readable_slug_is_not_an_id_and_is_not_judged(self):
        """The widget path also takes a slug. Calling 'strategy-call' a deleted
        calendar because it is not in the id list would be a guess."""
        steps = [sms("Book",
                     "https://link.msgsndr.com/widget/booking/strategy-call")]
        self.assertNotIn("GHL067", rules_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))
        self.assertNotIn("GHL067", skips_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))

    def test_a_token_matching_the_calendars_own_name_passes(self):
        steps = [sms("Book",
                     "https://link.msgsndr.com/widget/booking/strategycall")]
        self.assertNotIn("GHL067", rules_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))

    def test_the_same_dead_id_in_two_messages_is_one_finding(self):
        steps = [sms("Text", f"https://link.msgsndr.com/widget/booking/{DEAD_CAL}"),
                 email("Email", f"https://link.msgsndr.com/widget/booking/{DEAD_CAL}")]
        found = findings_for("GHL067", [wf("Booking", steps)],
                             calendars=CALENDARS)
        self.assertEqual(len(found), 1)

    def test_two_workflows_carrying_the_dead_id_are_both_reported(self):
        body = f"https://link.msgsndr.com/widget/booking/{DEAD_CAL}"
        found = findings_for("GHL067", [wf("One", [sms("A", body)]),
                                        wf("Two", [sms("B", body)])],
                             calendars=CALENDARS)
        self.assertEqual(sorted(f.workflow for f in found), ["One", "Two"])

    def test_two_different_schedulers_in_one_message_both_report(self):
        steps = [email("Book", "https://calendly.com/a/x or "
                               "https://acuityscheduling.com/schedule.php")]
        found = findings_for("GHL067", [wf("Booking", steps)],
                             calendars=CALENDARS)
        self.assertEqual(len(found), 2)


class AppointmentTimeWithoutATimezone(unittest.TestCase):
    """GHL068 — an hour with no zone attached to it."""

    def test_merged_appointment_time_with_no_zone_is_flagged(self):
        steps = [sms("Confirm", "You're set for {{ appointment.start_time }}.")]
        found = findings_for("GHL068", [wf("Confirm", steps, [booked()])])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_a_pinned_workflow_timezone_raises_it(self):
        steps = [sms("Confirm", "You're set for {{ appointment.start_time }}.")]
        found = findings_for("GHL068", [wf("Confirm", steps, [booked()],
                                           settings={"timezone":
                                                     "America/New_York"})])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_the_contacts_own_timezone_is_not_a_pin(self):
        steps = [sms("Confirm", "You're set for {{ appointment.start_time }}.")]
        found = findings_for("GHL068", [wf("Confirm", steps, [booked()],
                                           settings={"timezone": "contact"})])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_a_zone_abbreviation_in_the_copy_passes(self):
        steps = [sms("Confirm",
                     "You're set for {{ appointment.start_time }} EST.")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_a_merged_timezone_field_passes(self):
        steps = [sms("Confirm", "You're set for {{ appointment.start_time }} "
                                "({{ appointment.timezone }}).")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_saying_your_time_passes(self):
        steps = [sms("Confirm",
                     "You're set for {{ appointment.start_time }}, your time.")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_a_non_time_appointment_field_is_not_a_time(self):
        steps = [sms("Confirm", "You're booked for {{ appointment.title }}.")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_a_hardcoded_hour_is_not_this_rules_business(self):
        steps = [sms("Confirm", "You're set for 2pm.")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_lowercase_words_are_not_mistaken_for_zone_codes(self):
        """'ct' and 'mt' hide inside ordinary words; only the code counts."""
        steps = [sms("Confirm", "Your slot is {{ appointment.start_time }} - "
                                "we can't wait to meet you.")]
        self.assertIn("GHL068", rules_hit([wf("Confirm", steps, [booked()])]))

    def test_every_offending_step_is_counted_once_in_the_finding(self):
        steps = [sms("Confirm", "Set for {{ appointment.start_time }}."),
                 sms("Reminder", "Still on for {{ appointment.start_time }}.")]
        found = findings_for("GHL068", [wf("Confirm", steps, [booked()])])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].reach, 2)

    # -- correct configurations an earlier draft reported ------------------

    def test_a_zone_spelled_out_in_words_passes(self):
        steps = [sms("Confirm",
                     "You're set for {{ appointment.start_time }} Eastern Time.")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_a_parenthesised_zone_passes(self):
        steps = [sms("Confirm",
                     "Set for {{ appointment.start_time }} (Pacific).")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_an_iana_zone_written_into_the_copy_passes(self):
        steps = [sms("Confirm",
                     "Set for {{ appointment.start_time }} America/Denver.")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_a_fixed_offset_passes(self):
        steps = [sms("Confirm", "Set for {{ appointment.start_time }} GMT+2.")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_naming_the_zone_in_the_email_subject_passes(self):
        steps = [email("Confirm", "See you then.",
                       subject="Confirmed: {{ appointment.start_time }} CST")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))

    def test_the_finding_reads_correctly_for_a_single_message(self):
        """A report that says "these messages" about one message reads as a
        check that did not look at the account it is describing."""
        one = findings_for("GHL068", [wf("Confirm", [
            sms("Confirm", "Set for {{ appointment.start_time }}.")], [booked()])])
        self.assertIn("This message merges", one[0].symptom)
        two = findings_for("GHL068", [wf("Confirm", [
            sms("A", "Set for {{ appointment.start_time }}."),
            sms("B", "Still {{ appointment.start_time }}.")], [booked()])])
        self.assertIn("These messages merge", two[0].symptom)

    def test_central_daylight_spelled_out_passes(self):
        steps = [sms("Confirm", "Set for {{ appointment.start_time }} "
                                "Central Daylight Time.")]
        self.assertNotIn("GHL068",
                         rules_hit([wf("Confirm", steps, [booked()])]))


class NoShowCopyWithNoProof(unittest.TestCase):
    """GHL069 — apologising for a meeting that has not happened yet."""

    def test_recovery_copy_on_the_booked_lane_is_flagged(self):
        steps = [wait(), sms("Recovery", "Sorry we missed you - rebook?")]
        self.assertIn("GHL069", rules_hit([wf("Booked", steps, [booked()])]))

    def test_the_no_show_trigger_is_the_correct_lane(self):
        steps = [wait(), sms("Recovery", "Sorry we missed you - rebook?")]
        trigger = [{"type": "appointment_status", "name": "No show",
                    "filters": [{"field": "appointment_status",
                                 "value": "noshow"}]}]
        self.assertNotIn("GHL069", rules_hit([wf("Recovery", steps, trigger)]))

    def test_an_unfiltered_appointment_trigger_belongs_to_ghl001(self):
        steps = [wait(), sms("Recovery", "Sorry we missed you - rebook?")]
        trigger = [{"type": "appointment", "name": "Appt", "filters": []}]
        self.assertNotIn("GHL069", rules_hit([wf("Recovery", steps, trigger)]))

    def test_a_wait_until_after_the_appointment_is_the_gate(self):
        steps = [appt_wait("2 hours after", 2, "hours", "after"),
                 sms("Recovery", "Sorry we missed you - rebook?")]
        self.assertNotIn("GHL069", rules_hit([wf("Booked", steps, [booked()])]))

    def test_a_status_branch_above_the_send_is_the_gate(self):
        steps = [wait(),
                 {"type": "if_else", "name": "Appointment status = no-show",
                  "meta": {"branches": [{"name": "no-show",
                                         "actions": [{"type": "noop"}]}]}},
                 sms("Recovery", "Sorry we missed you - rebook?")]
        self.assertNotIn("GHL069", rules_hit([wf("Booked", steps, [booked()])]))

    def test_a_missed_call_text_back_is_not_a_no_show_lane(self):
        """Identical sentence, different lane — only structure separates them."""
        steps = [sms("Text back", "Sorry we missed your call! How can we help?"),
                 wait(), email("Follow up", "Following up on your call.")]
        trigger = [{"type": "call_status", "name": "Call",
                    "filters": [{"field": "status", "value": "no answer"}]}]
        self.assertNotIn("GHL069", rules_hit([wf("MCTB", steps, trigger)]))

    def test_the_cancellation_lane_is_not_flagged(self):
        steps = [wait(), sms("Rebook", "Sorry we missed you - another time?")]
        trigger = [{"type": "appointment_status", "name": "Cancelled",
                    "filters": [{"field": "appointment_status",
                                 "value": "cancelled"}]}]
        self.assertNotIn("GHL069", rules_hit([wf("Rebook", steps, trigger)]))

    def test_a_booking_lane_with_no_recovery_copy_passes(self):
        steps = [wait(), sms("Nudge", "Looking forward to speaking.")]
        self.assertNotIn("GHL069", rules_hit([wf("Booked", steps, [booked()])]))

    def test_a_calendar_reference_is_enough_to_establish_the_lane(self):
        steps = [{"type": "book_appointment", "name": "Book them",
                  "meta": {"calendarId": LIVE_CAL}},
                 wait(), sms("Recovery", "Sorry we missed you - rebook?")]
        trigger = [{"type": "contact_tag_added", "name": "Tagged",
                    "filters": [{"tag": "booked"}]}]
        self.assertIn("GHL069", rules_hit([wf("Booked", steps, trigger)],
                                          calendars=CALENDARS))

    def test_drafts_are_not_audited(self):
        steps = [wait(), sms("Recovery", "Sorry we missed you - rebook?")]
        hits = rules_hit([wf("Booked", steps, [booked()], status="draft")])
        self.assertNotIn("GHL069", hits)

    # -- correct configurations an earlier draft reported ------------------

    def test_a_no_show_fee_line_is_terms_not_an_apology(self):
        """Half the confirmations in a service business quote the no-show fee.
        It belongs there, and it is not recovery copy."""
        steps = [wait("Wait 1 hour", 1, "hours"),
                 sms("Policy", "Heads up: a no-show fee of $50 applies if you "
                               "cancel late.")]
        self.assertNotIn("GHL069", rules_hit([wf("Confirm", steps, [booked()])]))

    def test_a_no_show_policy_sentence_alongside_real_recovery_copy_still_fires(self):
        steps = [wait(),
                 sms("Recovery", "Sorry we missed you - want another slot? "
                                 "Our no-show fee is waived this once.")]
        self.assertIn("GHL069", rules_hit([wf("Booked", steps, [booked()])]))

    def test_the_tag_triggered_recovery_lane_is_correctly_gated(self):
        """The commonest build in the wild: one workflow tags on the no-show
        status, a second is triggered by that tag. The tag IS the gate."""
        steps = [{"type": "book_appointment", "name": "Rebook",
                  "meta": {"calendarId": LIVE_CAL}},
                 wait("Wait 1 hour", 1, "hours"),
                 sms("Recovery", "Sorry we missed you - want another slot?")]
        trigger = [{"type": "contact_tag_added", "name": "No show tagged",
                    "filters": [{"tag": "appointment-no-show"}]}]
        self.assertNotIn("GHL069", rules_hit([wf("Recovery", steps, trigger)],
                                             calendars=CALENDARS))

    def test_a_text_back_that_also_books_is_still_a_phone_lane(self):
        """A missed-call text-back with a Book Appointment step in it looks like
        a booking lane to every structural test. The trigger settles it."""
        steps = [sms("Text back", "Sorry we missed your call! Grab a time?"),
                 {"type": "book_appointment", "name": "Book",
                  "meta": {"calendarId": LIVE_CAL}},
                 wait("Wait 1 day", 1, "days"),
                 sms("Nudge", "Still happy to help.")]
        trigger = [{"type": "call_status", "name": "Missed call",
                    "filters": [{"field": "status", "value": "no answer"}]}]
        self.assertNotIn("GHL069", rules_hit([wf("MCTB", steps, trigger)],
                                             calendars=CALENDARS))

    def test_a_trigger_named_for_the_no_show_is_enough_proof(self):
        steps = [wait(), sms("Recovery", "Sorry we missed you - rebook?")]
        trigger = [{"type": "contact_tag_added", "name": "Marked no-show",
                    "filters": [{"tag": "recovery"}]}]
        self.assertNotIn("GHL069", rules_hit([wf("Recovery", steps, trigger)]))


class RemindersLeaveNoTimeToReschedule(unittest.TestCase):
    """GHL070 — reminders that arrive after the decision is made."""

    def test_a_single_last_minute_reminder_is_high(self):
        steps = [appt_wait("Until 15 minutes before", 15, "minutes"),
                 sms("Starting soon", "We're starting shortly.")]
        found = findings_for("GHL070", [wf("Ladder", steps, [booked()])])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_a_day_before_touch_clears_it(self):
        steps = [appt_wait("Until 24 hours before", 24, "hours"), sms("First"),
                 appt_wait("Until 1 hour before", 1, "hours"), sms("Second")]
        self.assertNotIn("GHL070", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_two_reminders_bunched_at_the_end_are_medium(self):
        steps = [appt_wait("Until 3 hours before", 3, "hours"), sms("First"),
                 appt_wait("Until 1 hour before", 1, "hours"), sms("Second")]
        found = findings_for("GHL070", [wf("Ladder", steps, [booked()])])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_exactly_twelve_hours_out_is_enough_room(self):
        steps = [appt_wait("Until 12 hours before", 12, "hours"),
                 sms("Reminder")]
        self.assertNotIn("GHL070", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_lone_reminder_three_hours_out_is_medium_not_high(self):
        steps = [appt_wait("Until 3 hours before", 3, "hours"), sms("Reminder")]
        found = findings_for("GHL070", [wf("Ladder", steps, [booked()])])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_a_wait_with_nothing_below_it_reminds_nobody(self):
        steps = [sms("Confirmation", "You're booked."),
                 appt_wait("Until 15 minutes before", 15, "minutes")]
        self.assertNotIn("GHL070", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_workflow_that_only_runs_after_the_call_is_not_a_ladder(self):
        steps = [appt_wait("2 hours after", 2, "hours", "after"),
                 sms("Recap", "Thanks for your time.")]
        self.assertNotIn("GHL070", rules_hit([wf("Recap", steps, [booked()])]))

    def test_duration_waits_are_not_reminders(self):
        steps = [wait("Wait 30 minutes", 30, "minutes"), sms("Nudge")]
        self.assertNotIn("GHL070", rules_hit([wf("Nudge", steps, [booked()])]))

    def test_an_anchored_wait_with_no_readable_offset_is_left_alone(self):
        vague = {"type": "event_start_wait", "name": "Before the appointment",
                 "meta": {"waitType": "appointment_time"}}
        self.assertNotIn("GHL070",
                         rules_hit([wf("Ladder", [vague, sms()], [booked()])]))

    # -- correct configurations an earlier draft reported ------------------

    def test_a_confirmation_above_the_wait_means_something_came_earlier(self):
        """Confirm at booking time, nudge an hour out. Calling that 'one
        reminder and nothing earlier' reads as a check that did not look."""
        steps = [sms("Confirmation", "You're booked - see you then."),
                 appt_wait("Until 1 hour before", 1, "hours"),
                 sms("Soon", "Talk shortly.")]
        found = findings_for("GHL070", [wf("Day of", steps, [booked()])])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_the_advisory_copy_reads_correctly_for_a_single_reminder(self):
        steps = [sms("Confirmation", "You're booked."),
                 appt_wait("Until 1 hour before", 1, "hours"), sms("Soon")]
        found = findings_for("GHL070", [wf("Day of", steps, [booked()])])
        self.assertIn("1 appointment-timed reminder in", found[0].symptom)
        self.assertNotIn("reminders in this workflow", found[0].symptom)

    def test_the_fix_points_at_the_calendars_own_reminder_settings(self):
        """The day-before touch may not be in a workflow at all, and the fix has
        to say so or the auditor argues with a client who is already covered."""
        steps = [appt_wait("Until 15 minutes before", 15, "minutes"), sms("Soon")]
        found = findings_for("GHL070", [wf("Ladder", steps, [booked()])])
        self.assertIn("calendar", found[0].fix.lower())


class OffsetShapes(unittest.TestCase):
    """The same wait, written the way seven different exports write it.

    Every one of these has to read as 24 hours before the appointment. Missing
    a shape does not produce a wrong finding — it produces silence, which is
    the failure nobody notices.
    """

    def _reads_as_a_day_before(self, meta):
        """Run the shape twice: once ahead of a 1-hour rung, once behind it.

        Ahead of it the ladder is correct and everything must stay quiet;
        behind it the ladder runs backwards and GHL066 must fire. A shape that
        was never read at all is silent both times, so asserting only the
        quiet half would pass on a parser that does nothing.
        """
        shape = appt_wait("Reminder wait", meta=meta)
        hour = appt_wait("Until 1 hour before", 1, "hours")
        forward = rules_hit([wf("Ladder", [shape, sms("A"), hour, sms("B")],
                                [booked()])])
        reverse = rules_hit([wf("Ladder", [hour, sms("A"), shape, sms("B")],
                                [booked()])])
        self.assertNotIn("GHL066", forward)
        self.assertNotIn("GHL070", forward)
        self.assertIn("GHL066", reverse)

    def test_value_unit_direction(self):
        self._reads_as_a_day_before(
            {"waitType": "appointment_time",
             "offset": {"value": 24, "unit": "hours", "direction": "before"}})

    def test_hours_before_key(self):
        self._reads_as_a_day_before(
            {"waitType": "appointment_time", "hoursBefore": 24})

    def test_signed_offset_minutes(self):
        self._reads_as_a_day_before(
            {"waitType": "appointment_time", "offsetMinutes": -1440})

    def test_before_key_holding_a_duration_string(self):
        self._reads_as_a_day_before(
            {"waitType": "appointment_time", "before": "24 hours"})

    def test_offset_written_as_a_signed_string(self):
        self._reads_as_a_day_before(
            {"waitType": "appointment_time", "offset": "-24 hours"})

    def test_offset_written_in_words(self):
        self._reads_as_a_day_before(
            {"waitType": "appointment_time", "offset": "24 hours before"})

    def test_a_days_unit_and_a_minutes_unit_land_on_one_scale(self):
        self._reads_as_a_day_before(
            {"waitType": "appointment_time", "minutesBefore": 1440})

    def test_a_bare_number_with_no_unit_is_not_guessed_at(self):
        """{"before": 24} could be hours or days. Neither is worth inventing."""
        steps = [appt_wait("Reminder wait",
                           meta={"waitType": "appointment_time", "before": 24}),
                 sms("Reminder")]
        self.assertNotIn("GHL070", rules_hit([wf("Ladder", steps, [booked()])]))
        self.assertNotIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))

    def test_a_label_alone_reads_as_an_offset_when_nothing_else_does(self):
        """No config at all — the builder put the whole instruction in the name."""
        steps = [{"type": "wait", "name": "Until 1 hour before"}, sms("A"),
                 {"type": "wait", "name": "Until 24 hours before"}, sms("B")]
        self.assertIn("GHL066", rules_hit([wf("Ladder", steps, [booked()])]))


class Overlap(unittest.TestCase):
    """One defect, one finding. Two rules on one root cause halves both."""

    def test_the_unfiltered_appointment_trigger_reports_once(self):
        """GHL001 owns it; GHL069 stands down even though the copy matches."""
        steps = [wait(), sms("Recovery", "Sorry we missed you - rebook?")]
        hits = rules_hit([wf("Recovery", steps,
                             [{"type": "appointment", "name": "Appt",
                               "filters": []}])])
        self.assertIn("GHL001", hits)
        self.assertNotIn("GHL069", hits)

    def test_a_reversed_ladder_is_not_also_reported_as_too_late(self):
        """GHL070 measures the earliest touch, which a reversal does not change."""
        steps = [appt_wait("Until 1 hour before", 1, "hours"), sms("A"),
                 appt_wait("Until 24 hours before", 24, "hours"), sms("B")]
        hits = rules_hit([wf("Ladder", steps, [booked()])])
        self.assertIn("GHL066", hits)
        self.assertNotIn("GHL070", hits)

    def test_recovery_copy_does_not_trip_the_reminder_check_as_well(self):
        steps = [wait(), sms("Recovery", "Sorry we missed you - rebook "
                                         "tomorrow?")]
        hits = rules_hit([wf("Booked", steps, [booked()])])
        self.assertIn("GHL069", hits)
        self.assertNotIn("GHL065", hits)

    def test_a_zone_free_reminder_on_a_fixed_wait_reports_both_defects(self):
        """These two ARE separate faults — the timing and the copy — so both
        belong in the report, on different steps of the same lane."""
        steps = [wait(), sms("Reminder", "Your call is tomorrow at "
                             "{{ appointment.start_time }}.")]
        hits = rules_hit([wf("Reminders", steps, [booked()])])
        self.assertEqual({"GHL065", "GHL068"} & hits, {"GHL065", "GHL068"})


class Robustness(unittest.TestCase):
    """Malformed exports must report or stay quiet — never raise."""

    CASES = [
        [],
        {},
        None,
        "a string",
        42,
        [None, "x", 7],
        [{"name": "x", "status": "published", "steps": None, "triggers": None,
          "settings": None}],
        [{"name": "x", "status": "published", "steps": "not a list"}],
        [{"name": "x", "status": "published", "steps": ["a bare string"]}],
        [{"name": "x", "status": "published", "triggers": "tag_added",
          "steps": [{"type": "wait", "meta": ["not", "a", "dict"]}]}],
        [{"name": "x", "status": "published", "triggers": [["a", "b"], None, 7],
          "steps": [{"type": "event_start_wait", "meta": {"offset": []}}]}],
        [{"name": "x", "status": "published", "settings": "windowed",
          "steps": [{"type": "sms", "meta": {"body": None}}]}],
        [{"_id": 12345, "name": 999, "status": True,
          "steps": [{"type": 7, "name": None, "meta": {"body": 12}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "event_start_wait",
                     "meta": {"offset": {"value": "soon", "unit": "bananas"}}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "event_start_wait",
                     "meta": {"waitType": "appointment", "hoursBefore": True}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "event_start_wait",
                     "meta": {"waitType": "appointment",
                              "before": {"value": None}}}]}],
        {"workflows": [{"name": "x", "status": "published", "steps": [
            {"type": "sms", "meta": {"body": "https://link.msgsndr.com/"
                                             "widget/booking/AbCdEfGhIjKlMnOpQrSt"}}]}],
         "calendars": "not a list"},
        {"workflows": [{"name": "x", "status": "published", "steps": [
            {"type": "sms", "meta": {"body": "https://link.msgsndr.com/"
                                             "widget/booking/AbCdEfGhIjKlMnOpQrSt"}}]}],
         "calendars": {"AbCdEfGhIjKlMnOpQrSt": None}},
    ]

    def test_no_input_shape_raises(self):
        for data in self.CASES:
            findings, _ = run_all(Account.load(data))
            self.assertTrue(all(f.cost.strip() for f in findings
                                if f.rule in MINE))

    def test_a_parent_cycle_between_two_waits_terminates(self):
        steps = [
            {"type": "event_start_wait", "id": "a", "parentKey": "b",
             "name": "Until 1 hour before",
             "meta": {"waitType": "appointment_time", "hoursBefore": 1}},
            {"type": "sms", "id": "m", "parentKey": "a", "meta": {"body": "hi"}},
            {"type": "event_start_wait", "id": "b", "parentKey": "a",
             "name": "Until 24 hours before",
             "meta": {"waitType": "appointment_time", "hoursBefore": 24}},
            {"type": "sms", "id": "n", "parentKey": "b", "meta": {"body": "hi"}},
        ]
        run_all(Account.load([wf("Ladder", steps, [booked()])]))

    def test_a_step_that_is_its_own_parent_terminates(self):
        steps = [{"type": "event_start_wait", "id": "a", "parentKey": "a",
                  "meta": {"waitType": "appointment_time", "hoursBefore": 1}},
                 {"type": "sms", "id": "b", "parentKey": "a",
                  "meta": {"body": "hi"}}]
        run_all(Account.load([wf("Ladder", steps, [booked()])]))

    def test_duplicate_step_ids_do_not_hang_the_path_walk(self):
        steps = [{"type": "event_start_wait", "id": "dup",
                  "meta": {"waitType": "appointment_time", "hoursBefore": 1}},
                 {"type": "sms", "id": "dup", "parentKey": "dup",
                  "meta": {"body": "hi"}},
                 {"type": "event_start_wait", "id": "dup", "parentKey": "dup",
                  "meta": {"waitType": "appointment_time", "hoursBefore": 24}},
                 {"type": "sms", "id": "z", "parentKey": "dup",
                  "meta": {"body": "hi"}}]
        run_all(Account.load([wf("Ladder", steps, [booked()])]))

    def test_a_settings_value_that_is_a_list_does_not_break_the_timezone_read(self):
        steps = [sms("Confirm", "Set for {{ appointment.start_time }}.")]
        found = findings_for("GHL068", [wf("Confirm", steps, [booked()],
                                           settings={"timezone": ["a", "b"]})])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_deeply_nested_wait_settings_are_read_without_recursion_trouble(self):
        deep = {"type": "wait", "name": "w",
                "meta": {"a": {"b": {"c": {"d": {"appointment": "yes",
                                                 "offset": {"value": 2}}}}}}}
        run_all(Account.load([wf("Ladder", [deep, sms()], [booked()])]))

    def test_a_booking_link_with_no_id_after_it_is_ignored(self):
        steps = [sms("Book", "https://link.msgsndr.com/widget/booking/")]
        self.assertNotIn("GHL067", rules_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))
        self.assertNotIn("GHL067", skips_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))

    def test_a_host_that_merely_ends_in_a_scheduler_word_is_not_one(self):
        steps = [sms("Book", "https://local.com/cal and https://mycal.com/x")]
        self.assertNotIn("GHL067", rules_hit([wf("Booking", steps)],
                                             calendars=CALENDARS))


class NoFalsePositivesOnTheCleanAccount(unittest.TestCase):
    """The shipped example of a CORRECTLY built account.

    It is the only fixture in the repo written to be right rather than wrong,
    which makes it the sharpest false-positive test this pack has.
    """

    def setUp(self):
        with open(BASE) as fh:
            self.findings, self.skips = run_all(Account.load(json.load(fh)))

    def test_none_of_these_six_fire_on_it(self):
        self.assertEqual(sorted({f.rule for f in self.findings} & MINE), [])

    def test_none_of_them_skips_on_it_either(self):
        self.assertEqual(sorted({s.rule for s in self.skips} & MINE), [])


class Fragment(unittest.TestCase):
    """The pack's slice of the shipped example has to demo all six."""

    def setUp(self):
        with open(FRAGMENT) as fh:
            self.acct = Account.load(json.load(fh))
        self.findings, self.skips = run_all(self.acct)
        self.mine = [f for f in self.findings if f.rule in MINE]

    def test_all_six_rules_fire_on_it(self):
        tripped = {f.rule for f in self.findings} & MINE
        self.assertEqual(sorted(MINE - tripped), [])

    def test_none_of_them_skips_on_it(self):
        self.assertEqual(sorted({s.rule for s in self.skips} & MINE), [])

    def test_every_finding_explains_what_it_costs(self):
        bare = [f.rule for f in self.mine if not f.cost.strip()]
        self.assertEqual(sorted(set(bare)), [])

    def test_every_finding_names_the_step_it_is_about(self):
        self.assertEqual([f.rule for f in self.mine if not f.step.strip()], [])

    def test_the_workflows_are_named_for_this_pack(self):
        """Names collide across fragments; the prefix is what keeps them apart."""
        for workflow in self.acct.workflows:
            self.assertTrue(workflow.name.startswith("Calendar Booking Demo - "),
                            workflow.name)

    def test_the_symptom_is_written_for_a_business_owner(self):
        """Long enough to say what the customer experiences, and free of the
        words that make a report read like a brochure."""
        banned = ("robust", "leverage", "seamless", "utilize", "best practice",
                  "synerg", "cutting-edge")
        for finding in self.mine:
            self.assertGreater(len(finding.symptom), 120, finding.rule)
            for word in banned:
                self.assertNotIn(word, finding.symptom.lower(), finding.rule)
                self.assertNotIn(word, finding.fix.lower(), finding.rule)

    def test_every_fix_is_an_action_somebody_can_take(self):
        for finding in self.mine:
            self.assertGreater(len(finding.fix), 60, finding.rule)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
