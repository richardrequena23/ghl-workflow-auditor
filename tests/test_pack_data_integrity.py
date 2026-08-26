"""Data-integrity pack — GHL071-GHL076.

Every rule gets a workflow that trips it and a workflow that does not. These
six read written VALUES rather than message bodies, and two of them infer a
field's TYPE from its NAME, so most of the work is in the negatives: a bare
merge field, a correctly normalised number, a field whose name carries a
qualifier ("Budget Range", "SMS Opt In Date") and therefore holds something
other than what the rest of the name says. A data rule that cries wolf is worse
than no data rule, because the field it is wrong about is the one the client
checks first — so each negative below is a real build that an earlier draft of
this pack reported, and each is named for the build, not for the code path.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import run_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FRAGMENT = os.path.join(HERE, "..", "examples", "packs", "data_integrity.json")
MINE = {"GHL071", "GHL072", "GHL073", "GHL074", "GHL075", "GHL076"}


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


def wait(name="Wait", delay="3 days"):
    return {"type": "wait", "name": name, "meta": {"delay": delay}}


def write(field, value, name="Update field", type="update_contact_field"):
    return {"type": type, "name": name, "meta": {"field": field, "value": value}}


def tag(value, name="Tag", type="add_contact_tag"):
    return {"type": type, "name": name, "meta": {"tag": value}}


def fields(*keys):
    """The account's custom-field list, in the shape an export ships it."""
    return [{"fieldKey": f"contact.{k}", "name": k.replace("_", " ").title()}
            for k in keys]


def form_trigger(form_id="lead-form"):
    return {"type": "form_submitted", "name": "Form",
            "filters": [{"field": "form_id", "value": form_id}]}


class PhoneNormalisation(unittest.TestCase):
    """GHL071 — a number written in a shape only it matches."""

    def test_formatted_literal_fires(self):
        hits = rules_hit([wf("Intake", [write("phone", "(555) 123-4567")])])
        self.assertIn("GHL071", hits)

    def test_digits_with_no_country_code_fire(self):
        hits = rules_hit([wf("Intake", [write("mobile_phone", "5551234567")])])
        self.assertIn("GHL071", hits)

    def test_dot_separated_digits_fire(self):
        hits = rules_hit([wf("Intake",
                             [write("alternate_phone", "555.123.4567")])])
        self.assertIn("GHL071", hits)

    def test_e164_passes(self):
        hits = rules_hit([wf("Intake", [write("phone", "+15551234567")])])
        self.assertNotIn("GHL071", hits)

    def test_a_bare_merge_field_is_not_judged(self):
        """What the token holds is not in the export, so neither is a verdict."""
        hits = rules_hit([wf("Intake",
                             [write("phone", "{{ inbound_webhook.phone }}")])])
        self.assertNotIn("GHL071", hits)

    def test_country_code_prefixed_onto_a_token_passes(self):
        hits = rules_hit([wf("Intake",
                             [write("phone", "+1{{ contact.phone_raw }}")])])
        self.assertNotIn("GHL071", hits)

    def test_a_number_assembled_out_of_pieces_fires(self):
        steps = [write("phone",
                       "({{ contact.area_code }}) {{ contact.line_number }}")]
        self.assertIn("GHL071", rules_hit([wf("Intake", steps)]))

    def test_a_non_phone_field_with_the_same_value_passes(self):
        hits = rules_hit([wf("Intake", [write("office_note", "(555) 123-4567")])])
        self.assertNotIn("GHL071", hits)

    def test_a_value_that_is_not_a_phone_number_passes(self):
        hits = rules_hit([wf("Intake", [write("phone_type", "mobile")])])
        self.assertNotIn("GHL071", hits)

    # -- fields that carry a phone word and hold something else -----------
    def test_an_opt_in_date_is_not_a_broken_phone_number(self):
        """'SMS Opt In Date' holds a date, and a date has eight digits in it."""
        hits = rules_hit([wf("Intake",
                             [write("sms_opt_in_date", "2026-01-05")])])
        self.assertNotIn("GHL071", hits)

    def test_a_verification_timestamp_is_not_a_broken_phone_number(self):
        hits = rules_hit([wf("Intake",
                             [write("phone_verified_at", "1735689600")])])
        self.assertNotIn("GHL071", hits)

    def test_an_identifier_that_happens_to_be_digits_passes(self):
        hits = rules_hit([wf("Intake",
                             [write("whatsapp_group_id",
                                    "120363021234567890")])])
        self.assertNotIn("GHL071", hits)

    def test_the_fields_map_shape_is_read(self):
        steps = [{"type": "update_contact_field", "name": "Set",
                  "meta": {"fields": {"phone": "555-123-4567"}}}]
        self.assertIn("GHL071", rules_hit([wf("Intake", steps)]))

    def test_a_number_written_as_a_json_integer_is_still_judged(self):
        self.assertIn("GHL071",
                      rules_hit([wf("Intake", [write("phone", 5551234567)])]))

    def test_drafts_are_not_audited(self):
        hits = rules_hit([wf("Intake", [write("phone", "(555) 123-4567")],
                             status="draft")])
        self.assertNotIn("GHL071", hits)


class OptOutCleared(unittest.TestCase):
    """GHL072 — the record of consent, overwritten."""

    def test_dnd_written_false_fires(self):
        hits = rules_hit([wf("Reactivation", [write("dnd", False)])])
        self.assertIn("GHL072", hits)

    def test_dnd_written_true_passes(self):
        hits = rules_hit([wf("Suppression", [write("dnd", True)])])
        self.assertNotIn("GHL072", hits)

    def test_unsubscribed_written_no_fires(self):
        hits = rules_hit([wf("Cleanup", [write("email_unsubscribed", "no")])])
        self.assertIn("GHL072", hits)

    def test_a_dedicated_dnd_step_fires(self):
        steps = [{"type": "set_dnd", "name": "DND off", "meta": {"dnd": "off"}}]
        self.assertIn("GHL072", rules_hit([wf("Cleanup", steps)]))

    def test_an_opt_in_field_is_not_read_as_an_opt_out(self):
        """email_opt_in = false records a refusal; it does not clear one."""
        hits = rules_hit([wf("Intake", [write("email_opt_in", "false")])])
        self.assertNotIn("GHL072", hits)

    def test_a_send_below_the_clear_makes_it_critical(self):
        found = findings_for("GHL072",
                             [wf("Reactivation", [write("dnd", False), sms()])])
        self.assertEqual([f.severity for f in found], ["critical"])

    def test_no_send_below_stays_high(self):
        found = findings_for("GHL072", [wf("Cleanup", [write("dnd", "0")])])
        self.assertEqual([f.severity for f in found], ["high"])

    def test_an_unrelated_step_carrying_a_dnd_default_is_ignored(self):
        """Only a step that is about writing fields or DND is read as one."""
        steps = [{"type": "sms", "name": "Text", "meta": {"body": "hi",
                                                          "dnd": False}}]
        self.assertNotIn("GHL072", rules_hit([wf("Nurture", steps)]))

    def test_a_contact_sync_echoing_the_current_dnd_state_is_ignored(self):
        """An Update Contact action ships the whole contact shape.

        `"dnd": false` in that payload is what the record currently says, not
        an instruction to clear it — reading it as one reported every
        contact-sync step in the account as a consent wipe.
        """
        steps = [{"type": "update_contact", "name": "Sync the name",
                  "meta": {"first_name": "Bob", "dnd": False}}]
        self.assertNotIn("GHL072", rules_hit([wf("Sync", steps)]))

    def test_an_opt_out_reason_is_not_the_opt_out_flag(self):
        """Writing 'none' into Opt Out Reason changes nothing about consent."""
        hits = rules_hit([wf("Cleanup", [write("opt_out_reason", "none")])])
        self.assertNotIn("GHL072", hits)

    def test_the_unsubscribe_link_field_is_not_the_flag(self):
        hits = rules_hit([wf("Cleanup", [write("unsubscribe_link", "cleared")])])
        self.assertNotIn("GHL072", hits)

    def test_a_dnd_expiry_field_is_not_the_flag(self):
        hits = rules_hit([wf("Cleanup", [write("dnd_until", "none")])])
        self.assertNotIn("GHL072", hits)

    def test_drafts_are_not_audited(self):
        hits = rules_hit([wf("Reactivation", [write("dnd", False)],
                             status="draft")])
        self.assertNotIn("GHL072", hits)


class DynamicTags(unittest.TestCase):
    """GHL073 — a tag list that grows one entry per contact."""

    def test_tag_built_from_a_merge_field_fires(self):
        self.assertIn("GHL073",
                      rules_hit([wf("Segmenting",
                                    [tag("city-{{ contact.city }}", "By city")])]))

    def test_a_static_tag_passes(self):
        hits = rules_hit([wf("Segmenting", [tag("hot-lead", "Hot")])])
        self.assertNotIn("GHL073", hits)

    def test_removing_a_tag_does_not_mint_one(self):
        steps = [tag("city-{{ contact.city }}", "Clear",
                     type="remove_contact_tag")]
        self.assertNotIn("GHL073", rules_hit([wf("Segmenting", steps)]))

    def test_only_the_dynamic_tag_in_a_list_is_reported(self):
        steps = [{"type": "add_contact_tag", "name": "Tags",
                  "meta": {"tags": ["hot-lead", "source-{{ contact.source }}"]}}]
        found = findings_for("GHL073", [wf("Segmenting", steps)])
        self.assertEqual(len(found), 1)
        self.assertIn("contact.source", found[0].title)

    def test_a_custom_value_is_one_tag_for_the_whole_account(self):
        """`{{ custom_values.x }}` is a location-wide constant.

        Parameterising a tag name that way is how an agency ships a snapshot:
        every contact gets the same tag, which is the opposite of the defect.
        """
        steps = [tag("{{ custom_values.current_promo_tag }}", "Promo")]
        self.assertNotIn("GHL073", rules_hit([wf("Segmenting", steps)]))

    def test_a_prefix_on_a_custom_value_is_still_one_tag(self):
        steps = [tag("promo-{{ custom_values.promo_code }}", "Promo")]
        self.assertNotIn("GHL073", rules_hit([wf("Segmenting", steps)]))

    def test_a_contact_token_beside_a_custom_value_still_fires(self):
        steps = [tag("{{ custom_values.season }}-{{ contact.city }}", "Mixed")]
        self.assertIn("GHL073", rules_hit([wf("Segmenting", steps)]))

    def test_a_merge_field_in_a_message_is_not_a_tag(self):
        hits = rules_hit([wf("Nurture", [sms("Hi", "Hey {{ contact.city }}")])])
        self.assertNotIn("GHL073", hits)

    def test_drafts_are_not_audited(self):
        steps = [tag("city-{{ contact.city }}", "By city")]
        self.assertNotIn("GHL073", rules_hit([wf("Segmenting", steps,
                                                 status="draft")]))


class DeadFieldWrites(unittest.TestCase):
    """GHL074 — data collected that nothing consumes."""

    def test_a_field_nobody_reads_fires(self):
        hits = rules_hit([wf("Intake", [write("intake_channel", "web")])],
                         customFields=fields("intake_channel"))
        self.assertIn("GHL074", hits)

    def test_a_field_read_in_a_message_passes(self):
        steps = [write("intake_channel", "web"),
                 sms("Recap", "You came in via {{ contact.intake_channel }}")]
        hits = rules_hit([wf("Intake", steps)],
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_a_field_read_by_a_trigger_filter_passes(self):
        listener = wf("Router", [sms()], [
            {"type": "contact_changed", "name": "Channel set",
             "filters": [{"field": "intake_channel", "value": "web"}]}])
        hits = rules_hit([wf("Intake", [write("intake_channel", "web")]),
                          listener],
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_a_trigger_that_names_the_field_outside_filters_passes(self):
        """Not every export puts the watched field under `filters`.

        Reading only that key reported a field with a live listener on it as
        dead data — so the whole trigger is searched, whatever shape it is in.
        """
        listener = wf("Router", [sms()],
                      [{"type": "contact_changed", "name": "Channel set",
                        "meta": {"field": "intake_channel"}}])
        hits = rules_hit([wf("Intake", [write("intake_channel", "web")]),
                          listener],
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_a_field_read_inside_another_write_passes(self):
        """One field copied into another is a reader like any other.

        The summary field it is copied INTO is itself unread here, so the rule
        is expected to report that one and only that one.
        """
        steps = [write("intake_channel", "web"),
                 write("lead_summary", "came in via {{ contact.intake_channel }}",
                       name="Build summary")]
        found = findings_for("GHL074", [wf("Intake", steps)],
                             customFields=fields("intake_channel", "lead_summary"))
        self.assertEqual([f.step for f in found], ["Build summary"])

    def test_a_source_field_named_beside_the_target_counts_as_a_read(self):
        steps = [{"type": "update_contact_field", "name": "Copy it across",
                  "meta": {"field": "lead_summary", "value": "x",
                           "sourceField": "contact.intake_channel"}}]
        found = findings_for("GHL074",
                             [wf("Intake", [write("intake_channel", "web")]),
                              wf("Copy", steps)],
                             customFields=fields("intake_channel",
                                                 "lead_summary"))
        self.assertEqual([f.step for f in found], ["Copy it across"])

    def test_a_field_read_by_a_custom_value_passes(self):
        hits = rules_hit([wf("Intake", [write("intake_channel", "web")])],
                         customValues={"Recap": "came in via "
                                                "{{ contact.intake_channel }}"},
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_a_field_read_by_a_draft_workflow_passes(self):
        """A paused build that reads it means the data is early, not dead."""
        reader = wf("Paused", [sms("Recap", "{{ contact.intake_channel }}")],
                    status="draft")
        hits = rules_hit([wf("Intake", [write("intake_channel", "web")]),
                          reader],
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_a_field_whose_name_prefixes_another_stays_quiet(self):
        """The blob is slugged, so every separator in it is an underscore.

        That makes 'intake_channel' inside 'intake_channel_history' look like a
        read of the shorter field, and this check declines rather than risk the
        opposite: treating the underscore as a boundary would stop
        '{{ contact.intake_channel }}' in a message from counting at all, and
        report a field the account reads every day as dead.
        """
        steps = [write("intake_channel", "web"),
                 sms("Recap", "see {{ contact.intake_channel_history }}")]
        hits = rules_hit([wf("Intake", steps)],
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_consent_evidence_is_not_dead_data(self):
        """'Why they opted out' is written for an audit, not for a step.

        Nothing reading it back is the point of the field, so reporting it
        reads as not knowing what the field is for.
        """
        steps = [write("opt_out_reason", "replied STOP"),
                 write("sms_consent_source", "web form")]
        hits = rules_hit([wf("Opt Out Handler", steps)],
                         customFields=fields("opt_out_reason",
                                             "sms_consent_source"))
        self.assertNotIn("GHL074", hits)

    def test_a_standard_field_is_never_dead(self):
        hits = rules_hit([wf("Intake", [write("first_name", "there")])],
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_a_key_the_account_does_not_have_is_left_to_ghl023(self):
        hits = rules_hit([wf("Intake", [write("mispelt_channel", "web")])],
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_no_custom_field_list_skips_instead_of_guessing(self):
        workflows = [wf("Intake", [write("intake_channel", "web")])]
        self.assertIn("GHL074", skips_hit(workflows))
        self.assertNotIn("GHL074", rules_hit(workflows))

    def test_the_skip_names_what_would_let_it_run(self):
        _, skips = audit_all([wf("Intake", [write("intake_channel", "web")])])
        mine = [s for s in skips if s.rule == "GHL074"]
        self.assertEqual(len(mine), 1)
        self.assertIn("customFields", mine[0].needs)

    def test_the_finding_uses_the_fields_display_name(self):
        found = findings_for("GHL074",
                             [wf("Intake", [write("intake_channel", "web")])],
                             customFields=fields("intake_channel"))
        self.assertEqual(len(found), 1)
        self.assertIn("Intake Channel", found[0].title)


class TypeMismatch(unittest.TestCase):
    """GHL075 — a value the field's type cannot hold."""

    def test_free_text_in_a_date_field_fires(self):
        hits = rules_hit([wf("Intake", [write("follow_up_date", "ASAP")])])
        self.assertIn("GHL075", hits)

    def test_a_real_date_passes(self):
        hits = rules_hit([wf("Intake", [write("follow_up_date", "2026-01-05")])])
        self.assertNotIn("GHL075", hits)

    def test_an_iso_timestamp_passes(self):
        hits = rules_hit([wf("Intake",
                             [write("follow_up_date", "2026-01-05T09:30:00Z")])])
        self.assertNotIn("GHL075", hits)

    def test_a_mispicked_text_merge_field_fires(self):
        steps = [write("follow_up_date", "{{ contact.first_name }}")]
        hits = rules_hit([wf("Intake", steps)])
        self.assertIn("GHL075", hits)

    def test_an_unknown_merge_field_is_not_judged(self):
        hits = rules_hit([wf("Intake",
                             [write("follow_up_date",
                                    "{{ contact.requested_date }}")])])
        self.assertNotIn("GHL075", hits)

    def test_a_merge_field_glued_to_words_fires(self):
        steps = [write("follow_up_date", "{{ contact.preferred_day }} at 4pm")]
        self.assertIn("GHL075", rules_hit([wf("Intake", steps)]))

    def test_a_date_token_with_an_iso_time_tail_passes(self):
        """T and Z are format markers, not words — nothing is proven broken."""
        steps = [write("follow_up_date",
                       "{{ appointment.start_date }}T00:00:00Z")]
        self.assertNotIn("GHL075", rules_hit([wf("Intake", steps)]))

    def test_a_date_composed_of_two_appointment_tokens_passes(self):
        """A date token beside a time token is a composition, not a mistake."""
        steps = [write("last_appointment_date",
                       "{{ appointment.start_date }} {{ appointment.start_time }}")]
        self.assertNotIn("GHL075", rules_hit([wf("Intake", steps)]))

    def test_free_text_in_a_number_field_fires(self):
        hits = rules_hit([wf("Intake", [write("deal_amount", "TBD")])])
        self.assertIn("GHL075", hits)

    def test_two_merge_fields_run_together_in_a_number_field_fire(self):
        steps = [write("deal_amount",
                       "{{ contact.deposit }}{{ contact.balance }}")]
        self.assertIn("GHL075", rules_hit([wf("Intake", steps)]))

    def test_a_formatted_currency_amount_passes(self):
        hits = rules_hit([wf("Intake", [write("deal_amount", "$1,200")])])
        self.assertNotIn("GHL075", hits)

    def test_a_currency_symbol_in_front_of_a_token_passes(self):
        hits = rules_hit([wf("Intake",
                             [write("deal_amount", "${{ contact.quote }}")])])
        self.assertNotIn("GHL075", hits)

    def test_a_text_field_is_not_type_checked(self):
        hits = rules_hit([wf("Intake", [write("service_interest", "ASAP")])])
        self.assertNotIn("GHL075", hits)

    # -- names that carry a qualifier hold a label, not a value -----------
    def test_a_budget_field_is_a_dropdown_of_written_bands(self):
        """On a lead form 'Budget' is a picklist, not a quantity."""
        hits = rules_hit([wf("Intake", [write("budget", "under $5k")]),
                          wf("Intake 2", [write("budget_range", "not sure")])])
        self.assertNotIn("GHL075", hits)

    def test_an_age_group_is_a_band(self):
        hits = rules_hit([wf("Intake", [write("age_group", "25-34")])])
        self.assertNotIn("GHL075", hits)

    def test_a_price_band_is_text(self):
        hits = rules_hit([wf("Intake", [write("price_band", "premium")])])
        self.assertNotIn("GHL075", hits)

    def test_a_score_label_is_a_letter_grade(self):
        hits = rules_hit([wf("Intake", [write("total_score_label", "A")])])
        self.assertNotIn("GHL075", hits)

    def test_a_date_preference_is_a_written_answer(self):
        hits = rules_hit([wf("Intake",
                             [write("date_preference", "weekday mornings")])])
        self.assertNotIn("GHL075", hits)

    def test_a_notes_field_beside_an_amount_is_text(self):
        hits = rules_hit([wf("Intake",
                             [write("amount_notes", "paid in two parts")])])
        self.assertNotIn("GHL075", hits)

    def test_a_phone_field_is_left_to_ghl071(self):
        hits = rules_hit([wf("Intake", [write("phone", "(555) 123-4567")])])
        self.assertNotIn("GHL075", hits)

    def test_today_is_not_flagged(self):
        hits = rules_hit([wf("Intake", [write("follow_up_date", "today")])])
        self.assertNotIn("GHL075", hits)

    def test_drafts_are_not_audited(self):
        hits = rules_hit([wf("Intake", [write("follow_up_date", "ASAP")],
                             status="draft")])
        self.assertNotIn("GHL075", hits)


class OverwrittenBeforeAnythingReadsIt(unittest.TestCase):
    """GHL076 — two writes to one field with nothing in between them."""

    def test_adjacent_writes_with_different_values_fire(self):
        steps = [write("lead_stage", "new-enquiry", name="Mark them new"),
                 write("lead_stage", "contacted", name="Mark them contacted")]
        self.assertIn("GHL076", rules_hit([wf("Intake", steps)]))

    def test_only_another_fields_write_in_between_still_fires(self):
        """A field write cannot read anything, so it does not rescue the first."""
        steps = [write("lead_stage", "new-enquiry", name="One"),
                 write("lead_owner", "dana", name="Set the owner"),
                 write("lead_stage", "contacted", name="Two")]
        self.assertIn("GHL076", rules_hit([wf("Intake", steps)]))

    def test_a_wait_between_them_is_a_design(self):
        """'In Sequence' for three days, then 'No Reply', is the intended build."""
        steps = [write("lead_stage", "in-sequence", name="One"),
                 wait(), write("lead_stage", "no-reply", name="Two")]
        self.assertNotIn("GHL076", rules_hit([wf("Nurture", steps)]))

    def test_a_message_between_them_can_read_the_first_value(self):
        steps = [write("lead_stage", "in-sequence", name="One"),
                 sms("Text", "Hi"),
                 write("lead_stage", "texted", name="Two")]
        self.assertNotIn("GHL076", rules_hit([wf("Nurture", steps)]))

    def test_a_branch_in_the_workflow_stops_the_check(self):
        """The two writes may be arms of the branch, and then neither is dead."""
        steps = [{"type": "if_else", "name": "Hot?", "meta": {"conditions": []}},
                 write("lead_stage", "hot", name="One"),
                 write("lead_stage", "cold", name="Two")]
        self.assertNotIn("GHL076", rules_hit([wf("Routing", steps)]))

    def test_a_wired_export_is_not_read_in_file_order(self):
        """With ids and links the flat order proves nothing about the run."""
        steps = [dict(write("lead_stage", "hot", name="One"), id="a", next="b"),
                 dict(write("lead_stage", "cold", name="Two"), id="b")]
        self.assertNotIn("GHL076", rules_hit([wf("Routing", steps)]))

    def test_the_same_value_twice_is_a_duplicate_not_a_contradiction(self):
        steps = [write("lead_stage", "hot", name="One"),
                 write("lead_stage", "hot", name="Two")]
        self.assertNotIn("GHL076", rules_hit([wf("Intake", steps)]))

    def test_case_alone_is_not_a_contradiction(self):
        steps = [write("lead_stage", "Hot", name="One"),
                 write("lead_stage", "hot", name="Two")]
        self.assertNotIn("GHL076", rules_hit([wf("Intake", steps)]))

    def test_a_merge_field_second_write_is_not_judged(self):
        steps = [write("lead_stage", "hot", name="One"),
                 write("lead_stage", "{{ contact.grade }}", name="Two")]
        self.assertNotIn("GHL076", rules_hit([wf("Intake", steps)]))

    def test_clearing_a_field_before_setting_it_passes(self):
        """An empty write is a deliberate clear; clearing then setting is fine."""
        steps = [write("lead_stage", "", name="Clear"),
                 write("lead_stage", "contacted", name="Set")]
        self.assertNotIn("GHL076", rules_hit([wf("Intake", steps)]))

    def test_two_different_fields_in_a_row_pass(self):
        steps = [write("lead_stage", "hot", name="One"),
                 write("lead_grade", "cold", name="Two")]
        self.assertNotIn("GHL076", rules_hit([wf("Intake", steps)]))

    def test_two_workflows_writing_one_field_are_left_to_ghl047(self):
        """The cross-workflow race is GHL047's; reporting it twice is the defect."""
        workflows = [wf("Web Intake", [write("lead_stage", "hot")],
                        [form_trigger("web")]),
                     wf("Buyer Sync", [write("lead_stage", "cold")],
                        [form_trigger("web")])]
        self.assertNotIn("GHL076", rules_hit(workflows))

    def test_one_action_assigning_a_field_twice_is_not_judged(self):
        """A field list is a set of assignments; the file does not order them."""
        steps = [{"type": "update_contact_field", "name": "Set",
                  "meta": {"fields": [{"field": "lead_stage", "value": "hot"},
                                      {"field": "lead_stage", "value": "cold"}]}}]
        self.assertNotIn("GHL076", rules_hit([wf("Intake", steps)]))

    def test_the_finding_names_both_steps_and_both_values(self):
        steps = [write("lead_stage", "new-enquiry", name="Mark them new"),
                 write("lead_stage", "contacted", name="Mark them contacted")]
        found = findings_for("GHL076", [wf("Intake", steps)])
        self.assertEqual(len(found), 1)
        self.assertIn("'new-enquiry'", found[0].title)
        self.assertIn("'contacted'", found[0].title)
        self.assertIn("Mark them new", found[0].symptom)
        self.assertEqual(found[0].step, "Mark them contacted")

    def test_drafts_are_not_audited(self):
        steps = [write("lead_stage", "hot", name="One"),
                 write("lead_stage", "cold", name="Two")]
        self.assertNotIn("GHL076", rules_hit([wf("Intake", steps,
                                                 status="draft")]))


class Robustness(unittest.TestCase):
    """Malformed exports must report or pass, never raise.

    A traceback here stops the other ninety-odd checks, so every shape a real
    export has arrived in gets run through the pack once.
    """

    CASES = [
        [{"name": "x", "status": "published", "steps": None}],
        [{"name": "x", "status": "published", "steps": "not a list"}],
        [{"name": "x", "status": "published", "steps": ["a bare string"]}],
        [{"name": "x", "status": "published", "settings": "windowed",
          "steps": [], "triggers": None}],
        [{"name": "x", "status": "published", "triggers": [["a", "b"]],
          "steps": [{"type": "update_contact_field", "meta": "nope"}]}],
        [{"_id": 12345, "name": 999, "status": True,
          "steps": [{"type": 7, "name": None}]}],
        [{"name": "x", "status": "published", "steps": [
            {"type": "update_contact_field",
             "meta": {"field": ["a", "b"], "value": {"deep": 1}}},
            {"type": "update_contact_field", "meta": {"fields": [7, "x", None]}},
            {"type": "update_contact_field",
             "meta": {"fields": {"7": {"value": None}}}},
            {"type": "update_contact_field",
             "meta": {"field": "follow_up_date", "value": {"deep": True}}},
            {"type": "set_dnd", "meta": None},
            {"type": "add_contact_tag", "meta": {"tags": {"a": None}}}]}],
        {"workflows": [{"name": "x", "status": "published",
                        "steps": [{"type": 7, "name": None}],
                        "triggers": ["bare", 42, None, {"type": ["a"]}]}],
         "customFields": ["contact.phone"]},
        {"workflows": [{"name": "x", "status": "published",
                        "steps": [{"type": "update_contact_field",
                                   "meta": {"field": "phone",
                                            "value": "(555) 123-4567"}}]}],
         "customFields": "nope", "customValues": [1, "two", None]},
    ]

    def test_no_input_shape_raises(self):
        for data in self.CASES:
            run_all(Account.load(data))

    def test_a_trigger_whose_filters_are_not_a_list_is_still_read(self):
        """The read check searches the whole trigger, not one blessed key."""
        listener = {"type": "contact_changed", "name": "Watch",
                    "filters": "intake_channel is set"}
        hits = rules_hit([wf("Intake", [write("intake_channel", "web")]),
                          wf("Router", [sms()], [listener])],
                         customFields=fields("intake_channel"))
        self.assertNotIn("GHL074", hits)

    def test_nested_value_objects_are_read(self):
        steps = [{"type": "update_contact_field", "name": "Set",
                  "meta": {"fields": {"phone": {"value": "(555) 123-4567"}}}}]
        self.assertIn("GHL071", rules_hit([wf("Intake", steps)]))

    def test_the_list_of_field_objects_shape_is_read(self):
        steps = [{"type": "update_contact_field", "name": "Set",
                  "meta": {"fields": [{"field": "dnd", "value": False}]}}]
        self.assertIn("GHL072", rules_hit([wf("Cleanup", steps)]))

    def test_a_flat_step_with_no_settings_block_is_read(self):
        steps = [{"type": "update_contact_field", "name": "Set",
                  "field": "phone", "value": "(555) 123-4567"}]
        self.assertIn("GHL071", rules_hit([wf("Intake", steps)]))


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

    def test_no_two_of_my_rules_report_the_same_step(self):
        """One defect, one finding — a step reported twice reads as noise."""
        seen: dict = {}
        for f in self.findings:
            if f.rule not in MINE:
                continue
            key = (f.workflow, f.step)
            self.assertNotIn(key, seen, f"{f.rule} repeats {seen.get(key)}")
            seen[key] = f.rule

    def test_the_workflow_names_are_namespaced_to_this_pack(self):
        """Names are unique across packs or the merged example silently clashes."""
        for workflow in self.acct.workflows:
            self.assertTrue(workflow.name.startswith("Data Integrity Demo - "),
                            workflow.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
