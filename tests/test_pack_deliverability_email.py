"""GHL059-GHL064 — email deliverability past the domain-verification line.

Every rule here gets a workflow that trips it and a correctly built one that
does not. The second half is the important half: these checks read message
copy, and a copy check that fires on a well-built email is the fastest way to
make a client stop believing the rest of the report.
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
FRAGMENT = os.path.join(HERE, "..", "examples", "packs",
                        "deliverability_email.json")
MINE = {"GHL059", "GHL060", "GHL061", "GHL062", "GHL063", "GHL064"}

# A compliant footer: postal address plus the unsubscribe token. Pasted into
# the emails that are supposed to PASS, so a test proves the rule fires on the
# defect rather than on every email that happens to be short.
FOOTER = "\n\nExample Co, 12 Example Way, Springfield IL 62704\n{{unsubscribe}}"

# GHL059 refuses to run unless the account's email configuration was supplied,
# because in GoHighLevel the postal address normally lives in the location-level
# footer and not in any workflow's step bodies. Without that context, "no address
# in the body" is not evidence of anything — measured on a real account it fired
# on nine of the eleven workflows that send email, i.e. all of them. So every
# test expecting GHL059 to FIRE has to hand it this context first.
#
# This blob deliberately carries NO address, leaving the message bodies as the
# only place left to look — which is the situation the rule is actually for.
# `address: ""` is load-bearing, not filler. It is how this fixture says the
# export LOOKED for a postal address and the account genuinely has none — as
# opposed to an export that never carried the field, which proves nothing and
# must not produce a federal-violation finding. See _carries_footer.
EMAIL_ACCOUNT = {"emailSettings": {"fromName": "Example Co",
                                   "fromEmail": "hello@example.com",
                                   "address": ""}}
# The same account with the address where it belongs. Configured this way it is
# compliant whatever the individual step bodies say.
EMAIL_ACCOUNT_WITH_ADDRESS = {"emailSettings": {
    "fromName": "Example Co",
    "footer": "Example Co, 12 Example Way, Springfield IL 62704"}}


def bundle(workflows, custom_values=None, **extra):
    data = {"workflows": workflows, "customValues": custom_values or {}}
    data.update(extra)
    return data


def audit(workflows, custom_values=None, config=None, **extra):
    return run(Account.load(bundle(workflows, custom_values, **extra),
                            config=config))


def audit_all(workflows, custom_values=None, config=None, **extra):
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


def email(name="Email", body="Hello there", subject="Hi", **meta):
    m = {"subject": subject, "body": body}
    m.update(meta)
    return {"type": "email", "name": name, "meta": m}


def wait(name="Wait"):
    return {"type": "wait", "name": name, "meta": {"stopOnResponse": True}}


FORM_TRIGGER = [{"type": "form_submitted", "name": "Enquiry",
                 "filters": [{"field": "form_name", "value": "Enquiry"}]}]
TAG_TRIGGER = [{"type": "contact_tag_added", "name": "Tagged",
                "filters": [{"tag": "prospect"}]}]

VERIFIED = [{"domain": "mail.acme.com", "verified": True}]
UNVERIFIED = [{"domain": "mail.acme.com", "verified": False}]


class PostalAddressInFooter(unittest.TestCase):
    """GHL059 — CAN-SPAM's physical address requirement."""

    LONG = ("Three quick things from us this month, and none of them need "
            "anything from you. Read on if you like.")

    def test_marketing_sequence_with_no_address(self):
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG)]
        self.assertIn("GHL059", rules_hit([wf("Newsletter", steps, TAG_TRIGGER)],
                                          **EMAIL_ACCOUNT))

    def test_it_skips_when_the_account_email_config_was_not_supplied(self):
        """The case that made this rule unusable on real exports.

        A GoHighLevel account keeps its postal address in the location footer,
        not in each workflow. Given no email config, the rule cannot tell a
        missing address from one it simply cannot see — so it must skip and say
        so, never assert a federal violation."""
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG)]
        self.assertNotIn("GHL059", rules_hit([wf("Newsletter", steps,
                                                 TAG_TRIGGER)]))
        self.assertIn("GHL059", skips_hit([wf("Newsletter", steps,
                                              TAG_TRIGGER)]))

    def test_a_half_supplied_email_config_skips_rather_than_accusing(self):
        """The trap that produced nine false federal-violation findings.

        An exporter that ships `emailSettings` carrying only sending-domain
        keys has said nothing about the footer — but it flips this rule out of
        its skip. Run against a real account whose postal address sits in its
        own location record, that produced a `high` finding on every workflow
        that sends mail. Missing data must skip; only data that went looking
        and came back empty may accuse.
        """
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG)]
        partial = {"emailSettings": {"hasLcEmail": True, "domain": "",
                                     "defaultEmailService": ""}}
        self.assertNotIn("GHL059", rules_hit([wf("Newsletter", steps,
                                                 TAG_TRIGGER)], **partial))
        self.assertIn("GHL059", skips_hit([wf("Newsletter", steps,
                                              TAG_TRIGGER)], **partial))

    def test_an_empty_address_field_is_evidence_the_export_looked(self):
        """`address: ""` means the account has none — that IS a violation."""
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG)]
        looked = {"emailSettings": {"fromName": "Example Co", "address": ""}}
        self.assertIn("GHL059", rules_hit([wf("Newsletter", steps,
                                              TAG_TRIGGER)], **looked))

    def test_supplied_templates_alone_are_enough_to_run(self):
        """A shared template is where the footer lives, so it counts."""
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG)]
        tpl = {"emailSettings": {"fromName": "Example Co"},
               "emailTemplates": [{"id": "t1", "name": "Base footer"}]}
        self.assertIn("GHL059", rules_hit([wf("Newsletter", steps,
                                              TAG_TRIGGER)], **tpl))

    def test_an_address_in_the_account_footer_clears_every_workflow(self):
        """Where the address belongs. Bodies need not repeat it."""
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG)]
        hit = rules_hit([wf("Newsletter", steps, TAG_TRIGGER)],
                        **EMAIL_ACCOUNT_WITH_ADDRESS)
        self.assertNotIn("GHL059", hit)

    def test_a_street_address_in_the_footer_clears_it(self):
        steps = [email("One", self.LONG + FOOTER), wait(),
                 email("Two", self.LONG + FOOTER)]
        self.assertNotIn("GHL059", rules_hit([wf("Newsletter", steps,
                                                 TAG_TRIGGER)]))

    def test_a_po_box_counts_as_an_address(self):
        steps = [email("One", self.LONG + "\nExample Co, PO Box 4471, "
                                          "Springfield IL 62704"),
                 wait(), email("Two", self.LONG + "\nPO Box 4471")]
        self.assertNotIn("GHL059", rules_hit([wf("Newsletter", steps,
                                                 TAG_TRIGGER)]))

    def test_the_location_merge_field_counts_as_an_address(self):
        """The correct way to write this footer leaves no street name in the
        export at all — flagging it would punish the best build in the file."""
        steps = [email("One", self.LONG + "\n{{location.full_address}}"),
                 wait(), email("Two", self.LONG + "\n{{location.full_address}}")]
        self.assertNotIn("GHL059", rules_hit([wf("Newsletter", steps,
                                                 TAG_TRIGGER)]))

    def test_a_single_email_off_a_form_is_transactional(self):
        steps = [email("Confirmation", self.LONG)]
        self.assertNotIn("GHL059", rules_hit([wf("Enquiry Reply", steps,
                                                 FORM_TRIGGER)]))

    def test_a_sequence_off_a_form_trigger_is_still_marketing(self):
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG)]
        self.assertIn("GHL059", rules_hit([wf("Enquiry Nurture", steps,
                                               FORM_TRIGGER)],
                                          **EMAIL_ACCOUNT))

    def test_config_can_mark_a_workflow_transactional(self):
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG)]
        cfg = AuditConfig.from_dict({"transactional_workflows": ["Receipts"]})
        self.assertNotIn("GHL059", rules_hit([wf("Receipts", steps)],
                                             config=cfg))

    def test_a_body_too_short_to_judge_is_left_alone(self):
        """An export carrying a template reference instead of the copy has a
        footer nobody can see, and a missing footer cannot be reported from
        an email this file does not contain."""
        steps = [email("One", "See template"), wait(), email("Two", "Ditto")]
        self.assertNotIn("GHL059", rules_hit([wf("Newsletter", steps,
                                                 TAG_TRIGGER)]))

    def test_reach_is_the_number_of_emails(self):
        steps = [email("One", self.LONG), wait(), email("Two", self.LONG),
                 {"type": "sms", "name": "Nudge", "meta": {"body": "hi"}}]
        found = findings_for("GHL059", [wf("Newsletter", steps, TAG_TRIGGER)],
                             **EMAIL_ACCOUNT)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].reach, 2)


class FromAddressAuthentication(unittest.TestCase):
    """GHL060 — whether DMARC can align on the From header."""

    def test_a_gmail_from_is_flagged(self):
        steps = [email("One", "Hi" + FOOTER, fromEmail="owner@gmail.com")]
        found = findings_for("GHL060", [wf("Quote", steps)],
                             emailDomains=VERIFIED)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "high")

    def test_a_reject_policy_domain_is_critical(self):
        """yahoo.com publishes p=reject, so this mail is refused rather than
        junked — a different conversation with the client than 'check spam'."""
        steps = [email("One", "Hi" + FOOTER, fromEmail="owner@yahoo.com")]
        found = findings_for("GHL060", [wf("Quote", steps)],
                             emailDomains=VERIFIED)
        self.assertEqual(found[0].severity, "critical")

    def test_the_verified_domain_passes(self):
        steps = [email("One", "Hi" + FOOTER, fromEmail="hi@mail.acme.com")]
        self.assertNotIn("GHL060", rules_hit([wf("Quote", steps)],
                                             emailDomains=VERIFIED))

    def test_relaxed_alignment_accepts_the_parent_domain(self):
        """DKIM on mail.acme.com aligns with a From at acme.com. Flagging it
        would report the standard GoHighLevel setup as broken."""
        steps = [email("One", "Hi" + FOOTER, fromEmail="hi@acme.com")]
        self.assertNotIn("GHL060", rules_hit([wf("Quote", steps)],
                                             emailDomains=VERIFIED))

    def test_a_listed_but_unverified_domain_is_flagged(self):
        domains = VERIFIED + [{"domain": "acmegroup.com", "verified": False}]
        steps = [email("One", "Hi" + FOOTER, fromEmail="hi@acmegroup.com")]
        found = findings_for("GHL060", [wf("Quote", steps)],
                             emailDomains=domains)
        self.assertEqual(len(found), 1)
        self.assertIn("never verified", found[0].title)

    def test_a_domain_missing_from_the_list_is_flagged(self):
        steps = [email("One", "Hi" + FOOTER, fromEmail="hi@otherbrand.com")]
        found = findings_for("GHL060", [wf("Quote", steps)],
                             emailDomains=VERIFIED)
        self.assertIn("not on this account", found[0].title)

    def test_an_account_with_nothing_verified_is_left_to_GHL025(self):
        """Repeating 'no verified domain' once per workflow is noise in a
        report that already says it once, account-wide."""
        steps = [email("One", "Hi" + FOOTER, fromEmail="hi@acme.com")]
        self.assertNotIn("GHL060", rules_hit([wf("Quote", steps)],
                                             emailDomains=UNVERIFIED))

    def test_a_consumer_mailbox_is_flagged_even_then(self):
        steps = [email("One", "Hi" + FOOTER, fromEmail="owner@gmail.com")]
        self.assertIn("GHL060", rules_hit([wf("Quote", steps)],
                                          emailDomains=UNVERIFIED))

    def test_no_from_address_is_not_a_finding(self):
        steps = [email("One", "Hi" + FOOTER)]
        self.assertNotIn("GHL060", rules_hit([wf("Quote", steps)],
                                             emailDomains=VERIFIED))

    def test_a_merge_field_from_address_is_not_guessed_at(self):
        steps = [email("One", "Hi" + FOOTER,
                       fromEmail="{{ custom_values.sender_email }}")]
        self.assertNotIn("GHL060", rules_hit([wf("Quote", steps)],
                                             emailDomains=VERIFIED))

    def test_no_domain_list_skips_the_business_domain_half(self):
        steps = [email("One", "Hi" + FOOTER, fromEmail="hi@acme.com")]
        self.assertIn("GHL060", skips_hit([wf("Quote", steps)]))

    def test_the_consumer_half_still_runs_with_no_domain_list(self):
        steps = [email("One", "Hi" + FOOTER, fromEmail="owner@gmail.com")]
        self.assertIn("GHL060", rules_hit([wf("Quote", steps)]))


class BounceSuppression(unittest.TestCase):
    """GHL061 — nothing removes an address that already failed."""

    SENDERS = [wf("Nurture A", [email("One", "Hi" + FOOTER), wait(),
                                email("Two", "Hi" + FOOTER)]),
               wf("Nurture B", [email("Three", "Hi" + FOOTER)])]

    def test_an_account_that_never_handles_a_bounce(self):
        self.assertIn("GHL061", rules_hit(self.SENDERS))

    def test_a_bounce_workflow_that_tags_the_contact_clears_it(self):
        handler = wf("Email Hygiene", [
            {"type": "add_contact_tag", "name": "Mark invalid",
             "meta": {"tag": "email-invalid"}}],
            [{"type": "email_bounced", "name": "Bounced"}])
        self.assertNotIn("GHL061", rules_hit(self.SENDERS + [handler]))

    def test_a_complaint_workflow_that_sets_dnd_clears_it(self):
        handler = wf("Complaint Handler", [
            {"type": "update_contact", "name": "Set DND for email",
             "meta": {"dnd": True}}],
            [{"type": "email_complaint", "name": "Complained"}])
        self.assertNotIn("GHL061", rules_hit(self.SENDERS + [handler]))

    def test_the_event_can_be_declared_in_the_trigger_filters(self):
        """GoHighLevel's Email Events trigger carries the event type as a
        filter, not in the trigger name — reading only the name misses every
        correctly built handler."""
        handler = wf("Email Events", [
            {"type": "add_contact_tag", "name": "Flag it",
             "meta": {"tag": "bad-address"}}],
            [{"type": "email_event", "name": "Email event",
              "filters": [{"field": "event", "value": "bounced"}]}])
        self.assertNotIn("GHL061", rules_hit(self.SENDERS + [handler]))

    def test_a_bounce_alert_that_suppresses_nothing_does_not_count(self):
        handler = wf("Bounce Alert", [
            {"type": "internal_notification", "name": "Ping the office"}],
            [{"type": "email_bounced", "name": "Bounced"}])
        self.assertIn("GHL061", rules_hit(self.SENDERS + [handler]))

    def test_a_draft_handler_does_not_count(self):
        handler = wf("Email Hygiene", [
            {"type": "add_contact_tag", "name": "Mark invalid",
             "meta": {"tag": "email-invalid"}}],
            [{"type": "email_bounced", "name": "Bounced"}], status="draft")
        self.assertIn("GHL061", rules_hit(self.SENDERS + [handler]))

    def test_an_account_with_two_emails_is_below_the_bar(self):
        small = [wf("One Off", [email("One", "Hi" + FOOTER), wait(),
                                email("Two", "Hi" + FOOTER)])]
        self.assertNotIn("GHL061", rules_hit(small))

    def test_an_account_with_no_email_at_all_is_left_alone(self):
        texts = [wf("SMS Only", [{"type": "sms", "name": "Hi",
                                  "meta": {"body": "hello"}}])]
        self.assertNotIn("GHL061", rules_hit(texts))

    def test_reach_counts_every_email_in_the_account(self):
        found = findings_for("GHL061", self.SENDERS)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].reach, 3)
        self.assertEqual(found[0].workflow, "(account)")


class ImageOnlyBody(unittest.TestCase):
    """GHL062 — a body with nothing in it for a filter or a reader."""

    IMG = ('<div><a href="https://acme.com/spring">'
           '<img src="https://cdn.acme.com/spring.png" width="600"></a></div>')
    PARAGRAPH = ("Spring check-ups are open from the 3rd. Evening slots go "
                 "first, so book early if Thursdays suit you best.")

    def test_an_image_with_no_copy_is_flagged(self):
        self.assertIn("GHL062", rules_hit(
            [wf("Promo", [email("Promo", self.IMG)])]))

    def test_an_image_above_real_copy_passes(self):
        self.assertNotIn("GHL062", rules_hit(
            [wf("Promo", [email("Promo", self.IMG + self.PARAGRAPH)])]))

    def test_a_compliance_footer_is_not_counted_as_copy(self):
        """The footer is in every email, so it cannot separate an image-only
        blast from a real message — it comes out before the copy is measured."""
        body = self.IMG + ("<p>Example Co, 12 Example Way, Springfield IL "
                           "62704 &middot; {{unsubscribe}}</p>")
        self.assertIn("GHL062", rules_hit([wf("Promo", [email("Promo", body)])]))

    def test_missing_alt_text_is_named_in_the_finding(self):
        found = findings_for("GHL062", [wf("Promo", [email("Promo", self.IMG)])])
        self.assertIn("alt text", found[0].title)

    def test_alt_text_present_is_not_reported_as_missing(self):
        body = '<img src="https://cdn.acme.com/spring.png" alt="Spring offer">'
        found = findings_for("GHL062", [wf("Promo", [email("Promo", body)])])
        self.assertEqual(len(found), 1)
        self.assertNotIn("alt text", found[0].title)

    def test_a_body_that_is_one_bare_link_is_flagged(self):
        self.assertIn("GHL062", rules_hit(
            [wf("Promo", [email("Promo", "Book here: https://acme.com/book")])]))

    def test_a_short_plain_text_email_passes(self):
        self.assertNotIn("GHL062", rules_hit(
            [wf("Promo", [email("Promo", "Thanks - see you Tuesday.")])]))

    def test_an_empty_body_is_not_judged(self):
        self.assertNotIn("GHL062", rules_hit(
            [wf("Promo", [{"type": "email", "name": "From a template",
                           "meta": {"subject": "Spring"}}])]))

    def test_an_sms_with_a_link_is_not_an_email_finding(self):
        self.assertNotIn("GHL062", rules_hit(
            [wf("Promo", [{"type": "sms", "name": "Nudge",
                           "meta": {"body": "https://acme.com/book"}}])]))


class NoReplyAddress(unittest.TestCase):
    """GHL063 — the reply the sender asked for has nowhere to land."""

    ASK = ("Your treatment plan is ready and the pricing holds for 14 days. "
           "Just reply to this email with a yes." + FOOTER)
    NO_ASK = ("Your treatment plan is attached and the pricing holds for 14 "
              "days. Book any time on the link." + FOOTER)

    def test_a_reply_ask_from_a_no_reply_address(self):
        steps = [email("Plan", self.ASK, fromEmail="no-reply@mail.acme.com")]
        self.assertIn("GHL063", rules_hit([wf("Plan", steps)]))

    def test_no_reply_with_no_reply_ask_passes(self):
        steps = [email("Plan", self.NO_ASK, fromEmail="no-reply@mail.acme.com")]
        self.assertNotIn("GHL063", rules_hit([wf("Plan", steps)]))

    def test_a_monitored_from_address_passes(self):
        steps = [email("Plan", self.ASK, fromEmail="front.desk@mail.acme.com")]
        self.assertNotIn("GHL063", rules_hit([wf("Plan", steps)]))

    def test_a_monitored_reply_to_rescues_a_no_reply_from(self):
        """From: noreply@ WITH a real Reply-To is the correct configuration,
        and the check has to follow where the reply actually goes."""
        steps = [email("Plan", self.ASK, fromEmail="no-reply@mail.acme.com",
                       replyTo="front.desk@acme.com")]
        self.assertNotIn("GHL063", rules_hit([wf("Plan", steps)]))

    def test_a_no_reply_reply_to_is_flagged_behind_a_real_from(self):
        steps = [email("Plan", self.ASK, fromEmail="front.desk@acme.com",
                       replyTo="donotreply@acme.com")]
        self.assertIn("GHL063", rules_hit([wf("Plan", steps)]))

    def test_an_sms_style_opt_out_line_is_not_a_reply_ask(self):
        body = "Your plan is attached. Reply STOP to opt out." + FOOTER
        steps = [email("Plan", body, fromEmail="no-reply@mail.acme.com")]
        self.assertNotIn("GHL063", rules_hit([wf("Plan", steps)]))

    def test_reply_back_phrasing_is_caught(self):
        body = "Let me know either way - reply back when you have a minute."
        steps = [email("Plan", body + FOOTER,
                       fromEmail="noreply@mail.acme.com")]
        self.assertIn("GHL063", rules_hit([wf("Plan", steps)]))

    def test_a_subject_line_ask_counts(self):
        steps = [email("Plan", "Details attached." + FOOTER,
                       subject="Just reply with a yes",
                       fromEmail="no-reply@mail.acme.com")]
        self.assertIn("GHL063", rules_hit([wf("Plan", steps)]))

    def test_a_draft_workflow_is_not_audited(self):
        steps = [email("Plan", self.ASK, fromEmail="no-reply@mail.acme.com")]
        self.assertNotIn("GHL063", rules_hit([wf("Plan", steps,
                                                 status="draft")]))


class SpamSubjectLine(unittest.TestCase):
    """GHL064 — content the filter can read, on a domain it cannot trust."""

    SHOUTY = "CONGRATULATIONS - you have been selected for 100% FREE setup!!"

    def test_a_shouty_subject_on_an_unauthenticated_domain(self):
        steps = [email("Offer", "Details inside." + FOOTER,
                       subject=self.SHOUTY,
                       fromEmail="promos@send.acme.com")]
        self.assertIn("GHL064", rules_hit([wf("Offer", steps)],
                                          emailDomains=UNVERIFIED))

    def test_the_same_subject_on_a_verified_domain_passes(self):
        """A sender with reputation to spend survives a punchy subject. The
        finding is the combination, and half of it is not a finding."""
        steps = [email("Offer", "Details inside." + FOOTER,
                       subject=self.SHOUTY, fromEmail="promos@mail.acme.com")]
        self.assertNotIn("GHL064", rules_hit([wf("Offer", steps)],
                                             emailDomains=VERIFIED))

    def test_one_signal_alone_is_not_enough(self):
        steps = [email("Offer", "Details inside." + FOOTER,
                       subject="Your FINAL invoice is attached")]
        self.assertNotIn("GHL064", rules_hit([wf("Offer", steps)],
                                             emailDomains=UNVERIFIED))

    def test_a_fully_capitalised_subject_counts_double(self):
        steps = [email("Offer", "Details inside." + FOOTER,
                       subject="FINAL NOTICE ABOUT YOUR ACCOUNT")]
        self.assertIn("GHL064", rules_hit([wf("Offer", steps)],
                                          emailDomains=UNVERIFIED))

    def test_a_safe_acronym_is_not_shouting(self):
        steps = [email("Offer", "Details inside." + FOOTER,
                       subject="Please RSVP by Friday!")]
        self.assertNotIn("GHL064", rules_hit([wf("Offer", steps)],
                                             emailDomains=UNVERIFIED))

    def test_an_ordinary_subject_passes(self):
        steps = [email("Offer", "Details inside." + FOOTER,
                       subject="Your appointment on Thursday")]
        self.assertNotIn("GHL064", rules_hit([wf("Offer", steps)],
                                             emailDomains=UNVERIFIED))

    def test_an_email_with_no_subject_is_not_judged(self):
        steps = [{"type": "email", "name": "Templated",
                  "meta": {"body": "Details inside." + FOOTER}}]
        self.assertNotIn("GHL064", rules_hit([wf("Offer", steps)],
                                             emailDomains=UNVERIFIED))

    def test_the_finding_names_what_it_read(self):
        steps = [email("Offer", "Details inside." + FOOTER,
                       subject=self.SHOUTY)]
        found = findings_for("GHL064", [wf("Offer", steps)],
                             emailDomains=UNVERIFIED)
        self.assertEqual(len(found), 1)
        self.assertIn("stacked punctuation", found[0].symptom)
        self.assertIn("CONGRATULATIONS", found[0].symptom)
        self.assertIn("you have been selected", found[0].symptom)

    def test_no_domain_list_skips_rather_than_guesses(self):
        steps = [email("Offer", "Details inside." + FOOTER,
                       subject=self.SHOUTY)]
        self.assertIn("GHL064", skips_hit([wf("Offer", steps)]))
        self.assertNotIn("GHL064", rules_hit([wf("Offer", steps)]))


class PackExample(unittest.TestCase):
    """The shipped fragment has to demonstrate all six, and skip none."""

    def setUp(self):
        with open(FRAGMENT) as fh:
            self.acct = Account.load(json.load(fh))
        self.findings, self.skips = run_all(self.acct)

    def test_every_rule_in_the_pack_fires_on_it(self):
        tripped = {f.rule for f in self.findings} & MINE
        self.assertEqual(sorted(MINE - tripped), [])

    def test_no_rule_in_the_pack_skips_on_it(self):
        self.assertEqual(sorted({s.rule for s in self.skips} & MINE), [])

    def test_every_finding_explains_what_it_costs(self):
        bare = [f.rule for f in self.findings
                if f.rule in MINE and not f.cost.strip()]
        self.assertEqual(bare, [])

    def test_workflow_names_are_namespaced_to_this_pack(self):
        """The fragment is merged with every other pack's into one example
        file, and Account.load keys some lookups by name."""
        for w in self.acct.workflows:
            self.assertTrue(w.name.startswith("Email Deliverability Demo - "),
                            w.name)


class Robustness(unittest.TestCase):
    """Real exports are malformed. A traceback here stops the other 99 checks."""

    CASES = [
        [],
        {},
        [{"name": "x", "status": "published", "steps": None,
          "triggers": None, "settings": None}],
        [{"name": "x", "status": "published", "steps": "not a list"}],
        [{"name": "x", "status": "published", "steps": ["a bare string"]}],
        [{"name": "x", "status": "published", "triggers": ["tag_added"],
          "steps": [{"type": "email", "meta": {"body": ["a", "list"],
                                               "subject": None}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "email", "meta": {"fromEmail": ["a@b.com"],
                                               "replyTo": {"x": 1}}}]}],
        [{"name": "x", "status": "published",
          "steps": [{"type": "email", "meta": {"subject": 7, "body": 9}}]}],
        {"workflows": [{"name": "x", "status": "published",
                        "steps": [{"type": "email",
                                   "meta": {"body": "hi", "fromEmail": "a@b"}}]}],
         "emailDomains": "acme.com"},
        {"workflows": [], "emailDomains": [None, "acme.com", {"nope": 1}]},
    ]

    def test_no_input_shape_raises(self):
        for data in self.CASES:
            run_all(Account.load(data))

    def test_a_domain_supplied_as_a_bare_string_is_handled(self):
        """`"emailDomains": ["acme.com"]` loads as verified, which is what the
        model decided — the pack must read it, not assume the dict shape."""
        steps = [email("One", "Hi" + FOOTER, fromEmail="hi@acme.com")]
        self.assertNotIn("GHL060", rules_hit([wf("Quote", steps)],
                                             emailDomains=["acme.com"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
