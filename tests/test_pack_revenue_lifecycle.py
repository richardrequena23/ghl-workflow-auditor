"""Revenue and lifecycle pack — GHL095-GHL100.

Every rule gets a workflow that trips it and a workflow that does not. These six
read the COMMERCIAL state of a contact, and most of them decide their lane from
copy or from a trigger's wording, so the negatives carry the weight: a quote
that expires is not a plan that expires, an appointment cancellation is not a
refund, and a webhook carrying an order is not a lead arriving. Each of those
distinctions has a test here, because getting one wrong points the finding at
the workflow that was built correctly.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import run_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FRAGMENT = os.path.join(HERE, "..", "examples", "packs", "revenue_lifecycle.json")
MINE = {"GHL095", "GHL096", "GHL097", "GHL098", "GHL099", "GHL100"}


def bundle(workflows, **extra):
    data = {"workflows": workflows}
    data.update(extra)
    return data


def audit_all(workflows, **extra):
    """(findings, skips) — the skips matter as much as the findings."""
    return run_all(Account.load(bundle(workflows, **extra)))


def audit(workflows, **extra):
    return audit_all(workflows, **extra)[0]


def rules_hit(workflows, **extra):
    return {f.rule for f in audit(workflows, **extra)}


def skips_hit(workflows, **extra):
    return {s.rule for s in audit_all(workflows, **extra)[1]}


def findings_for(rule_id, workflows, **extra):
    return [f for f in audit(workflows, **extra) if f.rule == rule_id]


def wf(name, steps, triggers=(), status="published", settings=None):
    return {"_id": name, "name": name, "status": status, "steps": list(steps),
            "triggers": list(triggers), "settings": settings or {}}


def sms(name="Message", body="hello"):
    return {"type": "sms", "name": name, "meta": {"body": body}}


def email(name="Email", body="hello", subject="Hi"):
    return {"type": "email", "name": name,
            "meta": {"subject": subject, "body": body}}


def wait(spec="2 days", name="Wait"):
    return {"type": "wait", "name": name, "meta": {"delay": spec}}


def notify(name="Alert the rep"):
    return {"type": "internal_notification", "name": name,
            "meta": {"body": "New lead"}}


def tag(value, name="Tag them"):
    return {"type": "add_contact_tag", "name": name, "meta": {"tag": value}}


def write(field, value, name="Update field"):
    return {"type": "update_contact_field", "name": name,
            "meta": {"field": field, "value": value}}


def opportunity(pipeline="pipe_a", stage="stg_new", name="Create deal",
                type="create_opportunity"):
    meta = {}
    if pipeline is not None:
        meta["pipelineId"] = pipeline
    if stage is not None:
        meta["stageId"] = stage
    return {"type": type, "name": name, "meta": meta}


def webhook_trigger(name="Lead vendor push", filters=()):
    return {"type": "inbound_webhook", "name": name, "filters": list(filters)}


def form_trigger(form_id="lead-form", name="Form"):
    return {"type": "form_submitted", "name": name,
            "filters": [{"field": "form_id", "value": form_id}]}


def order_trigger(name="Order submitted"):
    return {"type": "order_submitted", "name": name, "filters": []}


def sale_wf(name="Sales - Order Landed"):
    """A workflow whose only job is to prove the account sees conversions."""
    return wf(name, [notify("Tell the team")], [order_trigger()])


def churn_wf(name="Support - Cancellation Logged"):
    """Something in the account that labels a customer as gone."""
    return wf(name, [tag("refunded")], [form_trigger("cancel-form")])


class SpeedToLead(unittest.TestCase):
    """GHL095 — the first reply, parked behind a wait."""

    def test_wait_before_the_first_message_fires(self):
        steps = [wait("45 minutes"), sms()]
        self.assertIn("GHL095", rules_hit([wf("Intake", steps,
                                              [webhook_trigger()])]))

    def test_message_first_then_the_wait_passes(self):
        steps = [sms(), wait("45 minutes"), sms("Second")]
        self.assertNotIn("GHL095", rules_hit([wf("Intake", steps,
                                                 [form_trigger()])]))

    def test_a_short_wait_is_left_alone(self):
        """Under five minutes the ordering is a judgement call, not a defect."""
        steps = [wait("2 minutes"), sms()]
        self.assertNotIn("GHL095", rules_hit([wf("Intake", steps,
                                                 [form_trigger()])]))

    def test_an_hour_or_more_is_high(self):
        found = findings_for("GHL095",
                             [wf("Intake", [wait("2 days"), sms()],
                                 [form_trigger()])])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_under_an_hour_is_medium(self):
        found = findings_for("GHL095",
                             [wf("Intake", [wait("30 minutes"), sms()],
                                 [form_trigger()])])
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_stacked_waits_are_added_up(self):
        steps = [wait("3 minutes"), wait("4 minutes"), sms()]
        found = findings_for("GHL095", [wf("Intake", steps, [form_trigger()])])
        self.assertEqual(len(found), 1)
        self.assertIn("7 minutes", found[0].title)

    def test_a_human_told_before_the_wait_passes(self):
        """Somebody WAS notified inside the window — that is a staffing call."""
        steps = [notify(), wait("2 days"), sms()]
        self.assertNotIn("GHL095", rules_hit([wf("Intake", steps,
                                                 [form_trigger()])]))

    def test_an_assignment_before_the_wait_passes(self):
        steps = [{"type": "assign_round_robin", "name": "Route it",
                  "meta": {"users": ["u1"]}}, wait("2 days"), sms()]
        self.assertNotIn("GHL095", rules_hit([wf("Intake", steps,
                                                 [form_trigger()])]))

    def test_a_wait_with_no_readable_length_passes(self):
        """No number is better than the wrong number in a client's report."""
        steps = [{"type": "wait", "name": "Pause", "meta": {"delay": 30}}, sms()]
        self.assertNotIn("GHL095", rules_hit([wf("Intake", steps,
                                                 [form_trigger()])]))

    def test_an_event_wait_is_not_a_delay(self):
        steps = [{"type": "wait", "name": "Until they reply",
                  "meta": {"waitType": "reply", "startAfter":
                           {"type": "days", "value": 3}}}, sms()]
        self.assertNotIn("GHL095", rules_hit([wf("Intake", steps,
                                                 [form_trigger()])]))

    def test_a_tag_trigger_is_not_a_lead_arriving(self):
        trg = [{"type": "contact_tag_added", "name": "Tagged",
                "filters": [{"field": "tag", "value": "nurture"}]}]
        self.assertNotIn("GHL095", rules_hit([wf("Nurture",
                                                 [wait("2 days"), sms()], trg)]))

    def test_a_workflow_that_never_messages_the_lead_passes(self):
        steps = [wait("2 days"), notify()]
        self.assertNotIn("GHL095", rules_hit([wf("Intake", steps,
                                                 [form_trigger()])]))

    def test_drafts_are_not_audited(self):
        steps = [wait("2 days"), sms()]
        self.assertNotIn("GHL095", rules_hit([wf("Intake", steps,
                                                 [form_trigger()],
                                                 status="draft")]))


class ConversionExit(unittest.TestCase):
    """GHL096 — the cadence that keeps selling after the sale."""

    def cadence(self, name="Lead Nurture", steps=None, triggers=None):
        return wf(name, steps or [sms("One"), wait(), sms("Two")],
                  triggers or [form_trigger()])

    def test_nurture_with_no_conversion_check_fires(self):
        self.assertIn("GHL096", rules_hit([self.cadence()]))

    def test_a_remove_from_workflow_step_passes(self):
        steps = [sms("One"), wait(),
                 {"type": "remove_from_workflow", "name": "Exit if booked",
                  "meta": {"workflowId": "other"}}, sms("Two")]
        self.assertNotIn("GHL096", rules_hit([self.cadence(steps=steps)]))

    def test_a_branch_on_the_appointment_passes(self):
        steps = [sms("One"), wait(),
                 {"type": "if_else", "name": "Has an appointment?",
                  "meta": {"branches": [{"name": "Booked", "actions": [sms()]},
                                        {"name": "Not booked",
                                         "actions": [sms("Two")]}]}}]
        self.assertNotIn("GHL096", rules_hit([self.cadence(steps=steps)]))

    def test_reply_detection_alone_is_not_a_conversion_check(self):
        """Booking through a link is silent — there is no reply to catch."""
        steps = [sms("One"),
                 {"type": "wait", "name": "Wait", "meta": {"stopOnResponse": True}},
                 sms("Two")]
        self.assertIn("GHL096", rules_hit([self.cadence(steps=steps)]))

    def test_a_post_sale_lane_passes(self):
        self.assertNotIn("GHL096",
                         rules_hit([self.cadence(name="Onboarding Drip",
                                                 triggers=[order_trigger()])]))

    def test_an_appointment_lane_passes(self):
        trg = [{"type": "appointment_status", "name": "Appt",
                "filters": [{"field": "status", "value": "confirmed"}]}]
        self.assertNotIn("GHL096",
                         rules_hit([self.cadence(name="Reminder Ladder",
                                                 triggers=trg)]))

    def test_a_review_ask_belongs_to_the_other_rule(self):
        self.assertNotIn("GHL096",
                         rules_hit([self.cadence(name="Review Request")]))

    def test_a_single_message_is_not_a_cadence(self):
        self.assertNotIn("GHL096",
                         rules_hit([self.cadence(steps=[sms(), wait()])]))

    def test_a_cadence_that_never_waits_passes(self):
        self.assertNotIn("GHL096",
                         rules_hit([self.cadence(steps=[sms("One"), sms("Two")])]))

    def test_a_transactional_sequence_is_not_a_selling_lane(self):
        steps = [sms("One", "Your order is on the way."), wait(),
                 sms("Two", "It has been delivered.")]
        self.assertNotIn("GHL096",
                         rules_hit([self.cadence(name="Delivery Updates",
                                                 steps=steps)]))

    def test_booking_copy_makes_it_a_selling_lane(self):
        steps = [sms("One", "Want to book a call this week?"), wait(),
                 sms("Two", "Last nudge.")]
        self.assertIn("GHL096", rules_hit([self.cadence(name="Enquiry Handling",
                                                        steps=steps)]))

    def test_an_account_wide_guard_downgrades_it(self):
        guard = wf("Booked - Clean Up", [
            {"type": "remove_from_workflow", "name": "Pull from cadences",
             "meta": {"workflowId": "Lead Nurture"}}],
            [{"type": "appointment_booked", "name": "Booked", "filters": []}])
        found = findings_for("GHL096", [self.cadence(), guard])
        self.assertEqual([f.severity for f in found], ["low"])

    def test_drafts_are_not_audited(self):
        cadence = self.cadence()
        cadence["status"] = "draft"
        self.assertNotIn("GHL096", rules_hit([cadence]))


class RefundSuppression(unittest.TestCase):
    """GHL097 — the refund that stops nothing."""

    def refund(self, steps, name="Chargeback Received"):
        trg = [webhook_trigger("Stripe chargeback received")]
        return [wf(name, steps, trg), wf("Nurture", [sms(), wait(), sms()],
                                         [form_trigger()])]

    def test_notification_only_fires(self):
        self.assertIn("GHL097", rules_hit(self.refund([notify()])))

    def test_tagging_the_customer_passes(self):
        """Marking is half the job, and GHL098 owns whether anyone honours it."""
        self.assertNotIn("GHL097",
                         rules_hit(self.refund([notify(), tag("refunded")])))

    def test_removing_them_from_workflows_passes(self):
        steps = [notify(), {"type": "remove_from_workflow", "name": "Pull out",
                            "meta": {"workflowId": "Nurture"}}]
        self.assertNotIn("GHL097", rules_hit(self.refund(steps)))

    def test_setting_dnd_passes(self):
        steps = [notify(), {"type": "set_dnd", "name": "Stop messaging",
                            "meta": {"dnd": True}}]
        self.assertNotIn("GHL097", rules_hit(self.refund(steps)))

    def test_writing_a_churn_field_passes(self):
        steps = [notify(), write("customer_status", "refunded")]
        self.assertNotIn("GHL097", rules_hit(self.refund(steps)))

    def test_a_cancelled_appointment_is_not_a_refund(self):
        trg = [{"type": "appointment_status", "name": "Appointment cancelled",
                "filters": [{"field": "status", "value": "cancelled"}]}]
        workflows = [wf("Cancelled - Rebook Ask", [notify()], trg),
                     wf("Nurture", [sms(), wait(), sms()], [form_trigger()])]
        self.assertNotIn("GHL097", rules_hit(workflows))

    def test_a_cancelled_subscription_is_a_refund_lane(self):
        trg = [{"type": "subscription_cancelled", "name": "Plan cancelled",
                "filters": []}]
        workflows = [wf("Plan Cancelled", [notify()], trg),
                     wf("Nurture", [sms(), wait(), sms()], [form_trigger()])]
        self.assertIn("GHL097", rules_hit(workflows))

    def test_an_account_that_sends_nothing_else_passes(self):
        """Nothing to be pulled out of, so the fix would be a no-op."""
        trg = [webhook_trigger("Stripe refund")]
        self.assertNotIn("GHL097", rules_hit([wf("Refunds", [notify()], trg)]))

    def test_drafts_are_not_audited(self):
        workflows = self.refund([notify()])
        workflows[0]["status"] = "draft"
        self.assertNotIn("GHL097", rules_hit(workflows))


class UpsellScreen(unittest.TestCase):
    """GHL098 — the renewal ask that ignores the account's own marker."""

    def ask(self, name="Annual Renewal Push", steps=None, triggers=None):
        return wf(name, steps or [sms("Nudge", "Time to renew your cover?")],
                  triggers or [order_trigger()])

    def test_renewal_ask_with_a_churn_marker_in_the_account_fires(self):
        self.assertIn("GHL098", rules_hit([self.ask(), churn_wf()]))

    def test_no_churn_marker_anywhere_stays_quiet(self):
        """Nothing labels these customers, so there is nothing to screen on."""
        self.assertNotIn("GHL098", rules_hit([self.ask()]))

    def test_a_branch_on_the_refund_tag_passes(self):
        steps = [{"type": "if_else", "name": "Refunded?",
                  "meta": {"branches": [{"name": "Refunded", "actions": []},
                                        {"name": "Clean", "actions": [sms()]}]}},
                 sms("Nudge", "Time to renew your cover?")]
        self.assertNotIn("GHL098",
                         rules_hit([self.ask(steps=steps), churn_wf()]))

    def test_a_trigger_filtered_on_the_marker_passes(self):
        trg = [{"type": "order_submitted", "name": "Renewal window",
                "filters": [{"field": "tag", "operator": "not_has",
                             "value": "refunded"}]}]
        self.assertNotIn("GHL098",
                         rules_hit([self.ask(triggers=trg), churn_wf()]))

    def test_a_review_ask_belongs_to_the_other_rule(self):
        self.assertNotIn("GHL098",
                         rules_hit([self.ask(name="Review Request - Renewal"),
                                    churn_wf()]))

    def test_a_first_sale_follow_up_is_not_an_upsell(self):
        steps = [sms("Nudge", "Following up on the quote we put together.")]
        self.assertNotIn("GHL098",
                         rules_hit([self.ask(name="Quote Follow Up",
                                             steps=steps), churn_wf()]))

    def test_an_expiring_quote_is_not_an_expiring_plan(self):
        steps = [sms("Nudge", "Last note before the quote expires on Friday.")]
        self.assertNotIn("GHL098",
                         rules_hit([self.ask(name="Quote Chase", steps=steps),
                                    churn_wf()]))

    def test_an_expiring_membership_is_an_upsell(self):
        steps = [sms("Nudge", "Your membership expires next week.")]
        self.assertIn("GHL098",
                      rules_hit([self.ask(name="Members", steps=steps),
                                 churn_wf()]))

    def test_a_cancelled_appointment_tag_is_not_a_churn_marker(self):
        marker = wf("Appt Cleanup", [tag("appointment-cancelled")],
                    [{"type": "appointment_status", "name": "Appt",
                      "filters": [{"field": "status", "value": "cancelled"}]}])
        self.assertNotIn("GHL098", rules_hit([self.ask(), marker]))

    def test_a_workflow_that_sends_nothing_passes(self):
        self.assertNotIn("GHL098",
                         rules_hit([self.ask(steps=[notify()]), churn_wf()]))

    def test_drafts_are_not_audited(self):
        ask = self.ask()
        ask["status"] = "draft"
        self.assertNotIn("GHL098", rules_hit([ask, churn_wf()]))


class PipelineNeverAdvances(unittest.TestCase):
    """GHL099 — the pipeline that only ever fills."""

    def test_creates_with_no_advance_fires(self):
        workflows = [wf("Intake", [opportunity(), sms()], [form_trigger()]),
                     sale_wf()]
        self.assertIn("GHL099", rules_hit(workflows))

    def test_an_update_on_the_same_pipeline_passes(self):
        mover = wf("Stage Sync", [opportunity(stage="stg_won",
                                              name="Move to won",
                                              type="update_opportunity")],
                   [{"type": "opportunity_stage_changed", "name": "Stage",
                     "filters": []}])
        workflows = [wf("Intake", [opportunity(), sms()], [form_trigger()]),
                     mover, sale_wf()]
        self.assertNotIn("GHL099", rules_hit(workflows))

    def test_an_update_on_a_different_pipeline_still_fires(self):
        mover = wf("Other Stage Sync",
                   [opportunity(pipeline="pipe_b", stage="stg_won",
                                type="update_opportunity")],
                   [{"type": "opportunity_stage_changed", "name": "Stage",
                     "filters": []}])
        workflows = [wf("Intake", [opportunity(), sms()], [form_trigger()]),
                     mover, sale_wf()]
        self.assertIn("GHL099", rules_hit(workflows))

    def test_an_account_with_no_conversion_events_stays_quiet(self):
        """Nothing here could advance the deal automatically anyway."""
        workflows = [wf("Intake", [opportunity(), sms()], [form_trigger()])]
        self.assertNotIn("GHL099", rules_hit(workflows))

    def test_an_update_that_names_no_pipeline_stops_the_check(self):
        """That step could be moving any deal in the account — no verdict."""
        mover = wf("Stage Sync", [{"type": "update_opportunity",
                                   "name": "Move it",
                                   "meta": {"stageId": "stg_won"}}],
                   [{"type": "opportunity_stage_changed", "name": "Stage",
                     "filters": []}])
        workflows = [wf("Intake", [opportunity(), sms()], [form_trigger()]),
                     mover, sale_wf()]
        self.assertNotIn("GHL099", rules_hit(workflows))

    def test_a_merge_field_pipeline_is_not_attributed(self):
        workflows = [wf("Intake", [opportunity(pipeline="{{ custom_values.p }}"),
                                   sms()], [form_trigger()]), sale_wf()]
        self.assertNotIn("GHL099", rules_hit(workflows))

    def test_the_pipeline_is_named_from_the_inventory(self):
        workflows = [wf("Intake", [opportunity(), sms()], [form_trigger()]),
                     sale_wf()]
        found = findings_for("GHL099", workflows, pipelines=[
            {"id": "pipe_a", "name": "New Business",
             "stages": [{"id": "stg_new", "name": "New Lead"}]}])
        self.assertEqual(len(found), 1)
        self.assertIn("New Business", found[0].title)

    def test_a_draft_creator_is_not_counted(self):
        workflows = [wf("Intake", [opportunity(), sms()], [form_trigger()],
                        status="draft"), sale_wf()]
        self.assertNotIn("GHL099", rules_hit(workflows))


class WebhookAttribution(unittest.TestCase):
    """GHL100 — leads bought and arriving anonymous."""

    def intake(self, steps=None, triggers=None, name="Vendor Lead Intake"):
        return wf(name, steps or [sms()], triggers or [webhook_trigger()])

    def test_webhook_intake_with_no_source_fires(self):
        self.assertIn("GHL100", rules_hit([self.intake()]))

    def test_writing_a_lead_source_passes(self):
        steps = [write("lead_source", "acme-leads"), sms()]
        self.assertNotIn("GHL100", rules_hit([self.intake(steps=steps)]))

    def test_writing_a_utm_campaign_passes(self):
        steps = [write("utm_campaign", "{{ inbound_webhook.campaign }}"), sms()]
        self.assertNotIn("GHL100", rules_hit([self.intake(steps=steps)]))

    def test_a_source_tag_passes(self):
        self.assertNotIn("GHL100",
                         rules_hit([self.intake(steps=[tag("source-acme"),
                                                       sms()])]))

    def test_an_unrelated_field_write_does_not_count(self):
        steps = [write("resource_pack", "starter"), sms()]
        self.assertIn("GHL100", rules_hit([self.intake(steps=steps)]))

    def test_a_form_submission_passes(self):
        """A form-created contact already carries the platform's attribution."""
        self.assertNotIn("GHL100",
                         rules_hit([self.intake(triggers=[form_trigger()])]))

    def test_a_transaction_webhook_is_not_a_lead(self):
        trg = [webhook_trigger("Stripe chargeback received")]
        self.assertNotIn("GHL100", rules_hit([self.intake(triggers=trg)]))

    def test_an_order_webhook_is_not_a_lead(self):
        trg = [webhook_trigger("Store order placed")]
        self.assertNotIn("GHL100", rules_hit([self.intake(triggers=trg)]))

    def test_a_webhook_that_does_nothing_with_the_lead_passes(self):
        steps = [{"type": "webhook", "name": "Forward it",
                  "meta": {"url": "https://example.com/hook"}}]
        self.assertNotIn("GHL100", rules_hit([self.intake(steps=steps)]))

    def test_a_notification_counts_as_working_the_lead(self):
        self.assertIn("GHL100", rules_hit([self.intake(steps=[notify()])]))

    def test_an_opportunity_counts_as_working_the_lead(self):
        self.assertIn("GHL100", rules_hit([self.intake(steps=[opportunity()])]))

    def test_drafts_are_not_audited(self):
        self.assertNotIn("GHL100",
                         rules_hit([wf("Vendor Lead Intake", [sms()],
                                       [webhook_trigger()], status="draft")]))


class MalformedInput(unittest.TestCase):
    """Real exports carry nulls, bare strings and lists where dicts belong.

    A traceback in one rule takes the other ninety-nine down with it, so the
    contract is that nothing here raises — not that anything here is found.
    """

    CASES = [
        [],
        [{}],
        [{"name": "x", "status": "published", "steps": None, "triggers": None}],
        [{"name": "x", "status": "published", "steps": "not a list"}],
        [{"name": "x", "status": "published", "steps": ["a bare string"],
          "triggers": ["inbound_webhook"]}],
        [{"name": "x", "status": "published", "triggers": [["a", "b"]],
          "steps": [{"type": "wait", "meta": "nope"},
                    {"type": "create_opportunity", "meta": None}]}],
        [{"name": "x", "status": "published", "steps": [
            {"type": "wait", "meta": {"delay": {"value": None, "unit": None}}},
            {"type": "wait", "meta": {"duration": ["2 days"]}},
            {"type": "update_contact_field",
             "meta": {"field": ["a"], "value": {"deep": 1}}},
            {"type": "update_contact_field", "meta": {"fields": [7, None]}},
            {"type": "add_contact_tag", "meta": {"tags": {"a": None}}},
            {"type": "create_opportunity", "meta": {"pipelineId": ["p"]}},
            {"type": "update_opportunity", "meta": {"pipelineId": None}}],
          "triggers": [{"type": "order_submitted", "filters": "nope"}]}],
        {"workflows": [{"name": "x", "status": "published",
                        "steps": [{"type": 7, "name": None}],
                        "triggers": ["bare", 42, None]}],
         "pipelines": "not a list"},
    ]

    def test_no_input_shape_raises(self):
        for data in self.CASES:
            run_all(Account.load(data))

    def test_a_duration_object_is_read(self):
        steps = [{"type": "wait", "name": "Pause",
                  "meta": {"duration": {"value": 6, "unit": "hours"}}},
                 sms()]
        found = findings_for("GHL095", [wf("Intake", steps, [form_trigger()])])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_a_value_and_unit_pair_is_read(self):
        """And the length is reported in the unit a person would say it in."""
        steps = [{"type": "wait", "name": "Pause",
                  "meta": {"value": 90, "unit": "minutes"}}, sms()]
        found = findings_for("GHL095", [wf("Intake", steps, [form_trigger()])])
        self.assertEqual(len(found), 1)
        self.assertIn("1.5 hours", found[0].title)

    def test_a_wait_named_with_its_length_is_read(self):
        steps = [{"type": "wait", "name": "Wait 3 hours", "meta": {}}, sms()]
        self.assertIn("GHL095", rules_hit([wf("Intake", steps,
                                              [form_trigger()])]))

    def test_the_fields_map_shape_is_read_for_a_source(self):
        steps = [{"type": "update_contact_field", "name": "Set",
                  "meta": {"fields": {"lead_source": "acme"}}}, sms()]
        self.assertNotIn("GHL100", rules_hit([wf("Intake", steps,
                                                 [webhook_trigger()])]))


class Fragment(unittest.TestCase):
    """The pack's slice of the shipped example has to demo all six."""

    def setUp(self):
        with open(FRAGMENT) as fh:
            self.acct = Account.load(json.load(fh))
        self.findings, self.skips = run_all(self.acct)

    def test_every_rule_in_the_pack_fires_on_it(self):
        tripped = {f.rule for f in self.findings} & MINE
        self.assertEqual(sorted(MINE - tripped), [])

    def test_no_check_in_the_pack_is_skipped_on_it(self):
        self.assertEqual(sorted({s.rule for s in self.skips} & MINE), [])

    def test_every_finding_explains_what_it_costs(self):
        bare = [f.rule for f in self.findings
                if f.rule in MINE and not f.cost.strip()]
        self.assertEqual(sorted(set(bare)), [])

    def test_the_workflow_names_are_namespaced_to_this_pack(self):
        """Names are unique across packs or the merged example silently clashes."""
        for workflow in self.acct.workflows:
            self.assertTrue(
                workflow.name.startswith("Revenue Lifecycle Demo - "),
                workflow.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
