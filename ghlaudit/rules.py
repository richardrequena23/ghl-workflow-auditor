"""The rule catalog.

Each rule encodes a failure I have actually shipped, found, or cleaned up in a live
GoHighLevel account. They are not style opinions — every one of them has a symptom a
customer can see, written in the `symptom` field. That is the part that matters when
you hand the report to a business owner rather than to a developer.

Severity:
    critical  the account is texting customers something wrong, right now
    high      it will misfire under normal use, not just at an edge
    medium    it will bite on scale, on handover, or on a bad day
    low       correctness is fine; maintenance or future-proofing is not

Category — the five axes the health score is graded on:
    compliance     what the law and the carriers require of you
    deliverability whether the message physically arrives
    routing        whether the right person gets the right message at the right time
    hygiene        content and maintainability: placeholders, blank merges, dead refs
    dead_weight    things that exist and do nothing

A rule that cannot run — because it needs account context nobody supplied —
yields a `Skip`, never nothing. An unrun check is a hole in the audit, and the
report says so out loud. A clean report that was never actually run is the one
failure mode this tool cannot afford.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .model import (Account, FALLBACK_FILTER, MERGE_FIELD, PLACEHOLDER,
                    SMS_TYPES, STANDARD_CONTACT_FIELDS, URL, Workflow, slug)

SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = ("compliance", "deliverability", "routing", "hygiene", "dead_weight")

# What one finding of each severity is worth when the report is ranked. The
# numbers are not money — they are a consistent ordering, so that the first
# thing a client reads is the thing that costs them most, not the rule that
# happened to be defined first.
COST_BASE = {"critical": 1000, "high": 400, "medium": 120, "low": 30}


@dataclass
class Finding:
    rule: str
    severity: str
    workflow: str
    title: str
    symptom: str
    fix: str
    step: str = ""
    category: str = "routing"
    # One line on what this costs in money or leads. It is the sentence a
    # business owner reads; everything else in the finding explains it.
    cost: str = ""
    # How many outbound messages sit in the blast radius. A defect in a
    # six-message sequence burns six times as much goodwill as one in a
    # single-touch workflow, and the ranking has to see that.
    reach: int = 0

    def cost_score(self) -> float:
        return COST_BASE[self.severity] * (1 + min(self.reach, 10) / 5.0)

    def sort_key(self):
        return (SEVERITIES.index(self.severity), self.workflow, self.rule)

    def cost_key(self):
        return (-self.cost_score(), SEVERITIES.index(self.severity), self.rule,
                self.workflow)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "severity": self.severity,
            "category": self.category, "workflow": self.workflow,
            "step": self.step, "title": self.title, "symptom": self.symptom,
            "cost": self.cost, "fix": self.fix,
            "cost_score": round(self.cost_score()),
        }


@dataclass
class Skip:
    """A check that could not run, and exactly what would let it."""

    rule: str
    title: str
    reason: str
    needs: str = ""
    category: str = "routing"

    def to_dict(self) -> dict:
        return {"rule": self.rule, "title": self.title, "reason": self.reason,
                "needs": self.needs, "category": self.category}


@dataclass
class Rule:
    id: str
    title: str
    severity: str
    check: Callable[[Account], Iterable]
    tags: tuple = field(default_factory=tuple)
    category: str = "routing"


RULES: list[Rule] = []


def rule(rule_id: str, title: str, severity: str, category: str, *tags):
    if category not in CATEGORIES:  # pragma: no cover - developer error
        raise ValueError(f"{rule_id}: unknown category {category!r}")

    def wrap(fn):
        RULES.append(Rule(rule_id, title, severity, fn, tags, category))
        return fn
    return wrap


def _rule_meta(rule_id: str) -> Rule:
    for r in RULES:
        if r.id == rule_id:
            return r
    raise KeyError(rule_id)  # pragma: no cover


def _finding(r_id, sev, wf, title, symptom, fix, step="", cost="",
             category=None, reach=None) -> Finding:
    return Finding(rule=r_id, severity=sev, workflow=wf.name, title=title,
                   symptom=symptom, fix=fix, step=step, cost=cost,
                   category=category or _rule_meta(r_id).category,
                   reach=len(wf.outbound) if reach is None else reach)


# --------------------------------------------------------------------------
# Triggers that fire more often than their author thinks
# --------------------------------------------------------------------------

APPOINTMENT_TRIGGERS = ("appointment", "customer_booked_appointment",
                        "appointment_status", "booked_appointment")
# The words that mean "this trigger was narrowed to one appointment status".
APPT_STATUS_WORDS = ("noshow", "no-show", "no_show", "confirmed", "cancelled",
                     "canceled", "showed", "invalid", "status", "booked", "new")


@rule("GHL001", "Appointment trigger is not filtered by status", "critical", "routing", "triggers")
def appointment_trigger_unfiltered(acct: Account):
    for wf in acct.published():
        for trg in wf.triggers:
            if not any(t in trg.type.lower() for t in APPOINTMENT_TRIGGERS):
                continue
            blob = trg.filter_blob()
            if not trg.filters() or not any(w in blob for w in APPT_STATUS_WORDS):
                yield _finding(
                    "GHL001", "critical", wf,
                    "Appointment trigger fires on every status change",
                    "GoHighLevel re-fires an appointment trigger on EVERY status "
                    "change, not just the one you had in mind. Unfiltered, booking a "
                    "call can enroll the contact in no-show recovery — so someone who "
                    "just booked gets a text saying sorry we missed you.",
                    "Add a status filter to the trigger (Appointment Status is "
                    "'no-show', 'confirmed', 'cancelled' — pick the one this workflow "
                    "is actually for). Test by booking, then cancelling, a real slot.",
                    step=trg.name or trg.type,
                    cost="Every booking this trigger touches gets an insulting message "
                         "within seconds of paying you attention. You lose the "
                         "appointment you just won, and the lead tells people.")


@rule("GHL002", "Call trigger is not filtered to missed calls", "critical", "routing", "triggers")
def call_trigger_unfiltered(acct: Account):
    for wf in acct.published():
        for trg in wf.triggers:
            if "call" not in trg.type.lower():
                continue
            blob = trg.filter_blob()
            if not any(w in blob for w in ("no answer", "no-answer", "noanswer",
                                           "missed", "busy", "failed", "voicemail")):
                yield _finding(
                    "GHL002", "critical", wf,
                    "Call trigger is not narrowed to missed calls",
                    "A call-status trigger with no status filter also fires on calls "
                    "that CONNECTED. The customer you just spent ten minutes on the "
                    "phone with receives 'sorry we missed you — when's good?'",
                    "Filter the trigger to Call Status = no-answer (and busy/failed "
                    "if you want those too). Completed calls must not enter this "
                    "workflow.",
                    step=trg.name or trg.type,
                    cost="Every answered call becomes a contradicted customer. It reads "
                         "as 'they aren't even listening', on the call that went well.")


# --------------------------------------------------------------------------
# Workflows that talk but never listen
# --------------------------------------------------------------------------

@rule("GHL003", "Multi-touch sequence never checks for a reply", "high", "routing", "replies")
def sender_with_no_listener(acct: Account):
    handler = acct.reply_handler()
    for wf in acct.published():
        if len(wf.outbound) < 2 or wf.has_reply_release():
            continue
        # Two sends with no pause between them cannot ignore a reply — there is
        # no window for one to arrive in. An SMS and the same message by email,
        # fired together, is one touch delivered twice.
        if not wf.sends_across_a_wait():
            continue
        if handler is not None and handler.id != wf.id:
            # The account has a central listener, so this is a design choice rather
            # than a defect. Still worth surfacing: the listener has to actually name
            # this workflow in its remove step, and that is easy to forget to update.
            yield _finding(
                "GHL003", "low", wf,
                "Reply handling is delegated to "
                f"'{handler.name}' — confirm this workflow is in its remove list",
                "This sequence has no reply detection of its own, which is correct "
                "when a central listener pulls contacts out of it. But the listener "
                "removes contacts from a named list of workflows, and a sequence added "
                "later is easy to leave off it. If this one is missing, a lead who "
                "answers keeps receiving the rest of the sequence.",
                f"Open '{handler.name}' and check that its remove-from-workflow step "
                "names this workflow.",
                cost="If this sequence was left off the listener's list, every lead who "
                     "replies to it keeps getting messaged — a silent leak in the one "
                     "place the account was supposed to be safe.")
            continue
        yield _finding(
            "GHL003", "high", wf,
            f"{len(wf.outbound)} outbound messages, nothing listening for a reply",
            "Every step in this workflow is a sender. A lead who answers the first "
            "message stays inside the sequence and still gets the day-2 follow-up and "
            "the 'last touch' — then gets tagged as no-response on the way out. This "
            "is the single most common way a warm lead is burned by the system that "
            "was meant to warm them.",
            "Either set the waits to release on a reply, or add a reply-handler "
            "workflow that removes the contact from this one the moment they answer. "
            "Prefer the second — one listener can clean up after every sequence.",
            cost="Your warmest leads — the ones who replied — are the ones this burns. "
                 "Every reply that keeps receiving the sequence is a booked call you "
                 "already earned and then talked yourself out of.")


@rule("GHL009", "Reply alert has no once-per-conversation guard", "medium", "routing", "replies")
def alert_storm(acct: Account):
    for wf in acct.published():
        alerts = wf.steps_of("internal_notification", "notification", "slack")
        if not alerts:
            continue
        blob = wf.text().lower()
        guarded = any(k in blob for k in ("engaged", "alerted", "notified",
                                          "alert-sent", "re-arm", "rearm"))
        if not guarded and len(alerts) > 1:
            yield _finding(
                "GHL009", "medium", wf,
                f"{len(alerts)} internal alerts with no guard tag",
                "Without a guard, the assigned rep is pinged on every single inbound "
                "message. People mute alerts they cannot keep up with, and then they "
                "miss the one that mattered.",
                "Add an 'engaged' guard tag: the alert fires only if the tag is "
                "absent, then adds it. Each new campaign removes the tag at the top so "
                "the rep gets exactly one loud ping per conversation per campaign.",
                cost="Reps mute alerts they cannot keep up with, usually inside a week. "
                     "After that the account has no notification layer at all and "
                     "nobody has noticed yet.")


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

TIME_CRITICAL = re.compile(
    r"remind|reminder|starting soon|in 1 hour|in one hour|10 minutes|"
    r"see you at|your call (is|starts)|confirm", re.I)


@rule("GHL004", "Send window applied to time-critical messages", "high", "routing", "timing")
def send_window_on_time_critical(acct: Account):
    for wf in acct.published():
        if not wf.send_window():
            continue
        hits = [s for s in wf.outbound if TIME_CRITICAL.search(s.name + " " + s.text())]
        if hits:
            yield _finding(
                "GHL004", "high", wf,
                "Quiet hours will delay an appointment reminder",
                "A send window does not SKIP an action, it HOLDS it until the window "
                "reopens. Put one on a reminder ladder and the '1 hour before' text "
                "arrives the next morning — after the call it was reminding them "
                "about.",
                "Take the send window off this workflow. Windows belong on nurture and "
                "bulk campaigns, never on anything timed off an appointment.",
                step=hits[0].name or hits[0].type,
                cost="The reminder that was meant to stop a no-show arrives after the "
                     "call. You pay for the no-show and for the reminder that caused it.")


@rule("GHL013", "Send window with no contact timezone", "low", "compliance", "timing")
def window_without_timezone(acct: Account):
    for wf in acct.published():
        window = wf.send_window()
        if not window:
            continue
        blob = json.dumps(wf.settings).lower()
        if "timezone" not in blob and "time_zone" not in blob:
            yield _finding(
                "GHL013", "low", wf,
                "Send window is evaluated in account time, not the contact's",
                "A 9am-8pm window set in account time texts your California leads at "
                "6am if the account runs on Eastern. It is legal exposure as well as a "
                "bad first impression.",
                "Set the workflow to use the contact's timezone alongside the window.",
                cost="Out-of-state leads get texted before they are awake. That is an "
                     "opt-out, a complaint, and quiet-hours exposure — for a message "
                     "that was supposed to be polite.")

    # Quiet hours drift silently. Editing a workflow's steps through the API can
    # reset its settings, and a window that was correct last month can be blank
    # today with nothing in the UI marking the change. Comparing against the
    # window the build was SUPPOSED to have is the only way to catch that, and
    # only the caller can say what that was.
    for wf in acct.published():
        configured, want = acct.config.wants_window(wf.name)
        if not configured:
            continue
        have = wf.send_window()
        if want is None:
            if have:
                yield _finding(
                    "GHL013", "medium", wf,
                    "Send window present on a workflow that should not have one",
                    f"This workflow is carrying a send window ({have}) and your "
                    "build notes say it should have none. A window HOLDS a "
                    "message until the window reopens rather than skipping it, "
                    "so on anything time-sensitive it delivers late — which for "
                    "a reminder or an instant lead response is the same as not "
                    "delivering at all.",
                    "Clear the send window on this workflow.",
                    cost="Time-sensitive messages arrive after they mattered.")
            continue
        if not have:
            yield _finding(
                "GHL013", "high", wf,
                "Send window has been wiped from this workflow",
                "Your build notes specify a send window here and the workflow "
                "has none at all. Quiet hours do not fail loudly — the workflow "
                "just starts sending at whatever hour its trigger happens to "
                "fire, including 3am. Settings drift like this usually follows "
                "an API edit that rewrote the workflow and dropped everything "
                "it was not told to preserve.",
                f"Restore the window ({want.get('start')}-{want.get('end')}) and "
                "re-check the other workflows edited in the same batch — this "
                "kind of drift is never limited to one.",
                cost="Texts at 3am. Opt-outs, complaints, and quiet-hours "
                     "exposure in every state with an 8pm cutoff.")
            continue
        drifted = [k for k in ("start", "end", "days")
                   if k in want and have.get(k) != want.get(k)]
        if drifted:
            yield _finding(
                "GHL013", "medium", wf,
                "Send window does not match the documented one",
                f"Configured window is {have}; the build notes say it should be "
                f"{want}. The fields that differ: {', '.join(drifted)}.",
                "Reset the window to the documented values, or update the notes "
                "if the change was intentional.",
                cost="Messages go out at hours nobody signed off on.")


@rule("GHL005", "Bulk campaign has no throttle", "medium", "deliverability", "scale")
def mass_send_without_throttle(acct: Account):
    for wf in acct.published():
        name = wf.name.lower()
        bulky = any(k in name for k in ("reactivat", "database", "blast", "bulk",
                                        "list", "dormant", "cold", "winback",
                                        "win-back"))
        if not (bulky and wf.outbound):
            continue
        if wf.steps_of("drip"):
            continue
        yield _finding(
            "GHL005", "medium", wf,
            "Reactivation campaign fires at every contact at once",
            "A database reactivation with no throttle can put a year of dormant leads "
            "into the pipeline on a Tuesday morning. Generating thirty appointments "
            "for a business that can service ten does not read as success to the "
            "client — it reads as chaos, and the extra leads are burned.",
            "Put a drip step at the top of the workflow — 100 contacts per 24 hours is "
            "a sane default. Ask the client what they can actually service first.",
            cost="A year of dormant leads spent in one morning. The ones nobody can call "
                 "back in time are burned permanently, and the volume spike is exactly "
                 "what gets a sending number carrier-filtered.")


# --------------------------------------------------------------------------
# Things that break on handover
# --------------------------------------------------------------------------

@rule("GHL006", "Webhook posts to a hardcoded URL", "medium", "hygiene", "portability")
def hardcoded_endpoint(acct: Account):
    for wf in acct.workflows:
        for step in wf.steps_of("webhook", "http_request", "outbound_webhook"):
            blob = step.text()
            urls = URL.findall(blob)
            if urls and "custom_values." not in blob:
                yield _finding(
                    "GHL006", "medium", wf,
                    "Webhook URL is hardcoded into the workflow",
                    "Clone this account into a client's and the webhook still posts to "
                    "YOUR endpoint. It does not error — it quietly sends their customer "
                    "data to the wrong place, which is worse than breaking.",
                    "Move the endpoint into a custom value and reference "
                    "{{ custom_values.integration_webhook_url }}. One settings change "
                    "then moves the whole system between accounts.",
                    step=step.name or urls[0][:60],
                    cost="On the day this build is cloned, one client's customer data "
                         "starts posting to another client's endpoint. Nothing errors, "
                         "so nobody finds out from the software.")


@rule("GHL008", "Unresolved placeholder in a live workflow", "high", "hygiene", "portability")
def unresolved_placeholder(acct: Account):
    for name, value in acct.custom_values.items():
        if value and PLACEHOLDER.search(str(value)):
            yield Finding(
                rule="GHL008", severity="high", workflow="(custom values)",
                step=name,
                title=f"Custom value '{name}' is still a placeholder",
                symptom=f"Its value is {value!r}. Any workflow that merges this field "
                        "sends the placeholder text to a real customer, or posts to a "
                        "URL that does not exist.",
                fix="Set the real value in Settings -> Custom Values before the "
                    "workflows using it are published.",
                category="hygiene", reach=3,
                cost="Every message merging this field ships the placeholder text to a "
                     "paying customer, or a link that goes nowhere.")
    for wf in acct.published():
        for step in wf.outbound:
            if PLACEHOLDER.search(step.text()):
                yield _finding(
                    "GHL008", "high", wf,
                    "Outbound message contains placeholder text",
                    "A published message still has REPLACE-WITH / TODO text in the "
                    "body. It will be sent to a customer exactly as written.",
                    "Fill in the copy, or unpublish the workflow until it is ready.",
                    step=step.name or step.type,
                    cost="A customer receives your notes to yourself. It is the single "
                         "fastest way to look like nobody is running this account.")

    used = set()
    for wf in acct.published():
        used |= wf.custom_values_used()
    if acct.custom_values:
        defined = {slug(k) for k in acct.custom_values}
        for key in sorted(k for k in used if slug(k) not in defined):
            yield Finding(
                rule="GHL008", severity="high", workflow="(custom values)", step=key,
                title=f"Merge field references a custom value that does not exist: {key}",
                symptom="GoHighLevel renders an undefined merge field as empty text. "
                        "The message still sends — with a blank where the link, name "
                        "or address should have been.",
                fix=f"Create the custom value '{key}', or correct the merge field.",
                category="hygiene", reach=2,
                cost="The message sends with a blank where the booking link, the "
                     "business name or the address should have been. Replies to it "
                     "cost you the lead and a support conversation.")


# The action GoHighLevel actually deprecated is the COMBINED create-or-update
# opportunity action, replaced by two separate ones. Matching on "this type
# mentions an opportunity and does both" keeps the rule quiet when the export
# uses a naming this does not recognise, which is the right way round to fail:
# see the note on the rule below for what happened when it was the other way.
COMBINED_OPPORTUNITY = re.compile(
    r"(?=.*opportunit)(?=.*creat)(?=.*updat)", re.I)


@rule("GHL007", "Deprecated opportunity action", "low", "hygiene", "maintenance")
def deprecated_action(acct: Account):
    """The combined create-or-update opportunity action.

    ⛔ This rule used to flag `create_opportunity` and `update_opportunity` and
    tell the owner to swap them for `internal_` variants. That was backwards
    and it was destructive advice. GoHighLevel split the combined
    "Create/Update Opportunity" action into two separate actions and is
    deprecating the COMBINED one for new workflows — `create_opportunity` is
    the recommended replacement, not the problem. There is no `internal_`
    action to swap to.

    It fired 21 times on one real account, on entirely correct configuration,
    and its own rescue tool refused to act on it because GHL099 said the
    opposite. Two rules in the same catalog disagreeing, with neither checked
    against the vendor's documentation, is how a catalog loses a client's
    trust in all hundred of them.

    Even where the combined action IS present, GoHighLevel keeps existing
    workflows running — so this stays `low` and says so.
    """
    for wf in acct.workflows:
        for step in wf.steps:
            if not COMBINED_OPPORTUNITY.search(step.type):
                continue
            yield _finding(
                "GHL007", "low", wf,
                f"'{step.type}' is the combined opportunity action",
                "GoHighLevel split this into separate Create Opportunity and "
                "Update Opportunity actions and is retiring the combined one "
                "for new workflows. Existing workflows keep running, so "
                "nothing is broken today. The split actions do things this one "
                "cannot: update the opportunity that triggered the workflow, "
                "and act on one found by a Find Opportunity step.",
                "Replace it with the separate 'Create Opportunity' or 'Update "
                "Opportunity' action next time you touch this workflow, and "
                "re-select the pipeline and stage afterwards.",
                step=step.name or step.type,
                cost="Nothing today. It is maintenance debt that comes due on "
                     "somebody else's schedule, not yours.")


@rule("GHL011", "Re-enrollment on a workflow that creates records", "medium", "routing", "data")
def reentry_creates_duplicates(acct: Account):
    for wf in acct.published():
        creates = wf.steps_of("create_opportunity", "internal_create_opportunity")
        if not creates:
            continue
        blob = json.dumps(wf.settings).lower()
        allows = any(k in blob for k in ("allowmultiple", "allow_multiple",
                                         "reentry", "re_entry", "allowreentry"))
        if allows and "false" not in blob:
            yield _finding(
                "GHL011", "medium", wf,
                "Re-enrollment is on and the workflow creates opportunities",
                "Every re-entry mints another opportunity for the same person. The "
                "pipeline inflates, forecasts stop meaning anything, and the sales team "
                "works the same lead twice.",
                "Either turn re-enrollment off, or guard the create step with a check "
                "that no open opportunity already exists for this contact.",
                cost="Your pipeline value is wrong by however many duplicates it has "
                     "minted. Every forecast built on it is wrong the same way, and "
                     "two reps end up working the same person.")

    # Re-entry is a per-workflow DECISION, not a value that is right or wrong on
    # its own: a speed-to-lead wants it off as double-submit protection, a
    # no-show recovery wants it on so repeat no-shows re-enroll. Only the person
    # who designed the account knows which — so the intended setting comes from
    # config, and drift in EITHER direction is a finding. With no config there
    # is nothing to drift from, and this half stays quiet rather than guessing.
    for wf in acct.published():
        want = acct.config.wants_reentry(wf.name)
        if want is None:
            continue
        have = _allows_reentry(wf)
        if have == want:
            continue
        if want:
            yield _finding(
                "GHL011", "high", wf,
                "Re-enrollment is OFF, and this workflow is meant to allow it",
                "Your build notes say this workflow should re-enroll a contact "
                "each time its trigger fires, and the setting is off. A contact "
                "who is already inside — including one parked in a wait from "
                "weeks ago — will not re-enter, so the second event is simply "
                "ignored. Nothing reports it: the trigger fires and nothing "
                "happens.",
                "Turn 'Allow Re-entry' on for this workflow. If it was turned "
                "off deliberately, update the manifest so the next audit does "
                "not keep raising it.",
                cost="Every repeat event is dropped. On a no-show recovery that "
                     "means the second no-show is never chased at all.")
        else:
            yield _finding(
                "GHL011", "high", wf,
                "Re-enrollment is ON, and this workflow is meant to block it",
                "Your build notes say this workflow should run once per contact "
                "and re-entry is enabled. A lead who submits the form three "
                "times in a day gets three parallel copies of the sequence, "
                "running simultaneously from the same number.",
                "Turn 'Allow Re-entry' off. Note that HighLevel documents "
                "appointment- and invoice-triggered workflows as always allowing "
                "multiple entry regardless of the toggle — if this workflow has "
                "one of those triggers, use a guard tag instead of the setting.",
                cost="Duplicate sequences to the same person at the same time. It "
                     "is the most common reason a client says the system 'spams' "
                     "their leads.")


# --------------------------------------------------------------------------
# Asks that go out to the wrong person
# --------------------------------------------------------------------------

SUPPRESSION = ("unhappy", "complaint", "refund", "dispute", "cancel", "churn",
               "do-not-contact", "dnc", "opt-out", "optout")


@rule("GHL010", "Review or referral ask is only screened at enrollment", "medium", "routing", "asks")
def tag_check_at_enrollment_only(acct: Account):
    for wf in acct.published():
        name = wf.name.lower()
        if not any(k in name for k in ("review", "referral", "testimonial", "nps")):
            continue
        checks, waits, sends = [], [], []
        for i, step in enumerate(wf.steps):
            blob = (step.name + " " + step.text()).lower()
            if step.type in ("if_else", "condition", "branch") and any(
                    s in blob for s in SUPPRESSION):
                checks.append(i)
            if step.is_wait:
                waits.append(i)
            if step.is_outbound:
                sends.append(i)
        if not sends:
            continue

        if not checks:
            yield _finding(
                "GHL010", "medium", wf,
                "Nothing stops an unhappy customer being asked for a review",
                "The workflow asks every contact who reaches it. A customer who filed "
                "a complaint or a refund last week still gets 'how did we do?' — and "
                "some of them answer, in public, on Google.",
                "Branch on the complaint/refund/unhappy tags before the ask, and "
                "re-check them immediately before EACH send, not only at enrollment.",
                cost="You are inviting your unhappiest customers to review you in "
                     "public. One of them accepting costs more than the whole "
                     "campaign returns.")
            continue

        # A send is stale if a wait sits between the last suppression check and it.
        stale = [i for i in sends
                 if any(c < w < i for c in checks for w in waits)
                 and not any(c > max((w for w in waits if w < i), default=-1)
                             and c < i for c in checks)]
        if stale:
            step = wf.steps[stale[0]]
            yield _finding(
                "GHL010", "medium", wf,
                "Suppression tags are checked before the wait, never after",
                "This workflow waits days before it asks. The check happens on the way "
                "in, so anyone who becomes unhappy DURING the wait is still asked. The "
                "longer the wait, the more likely that is.",
                "Repeat the suppression branch immediately before each send. Evaluate "
                "at send time, not at enrollment time.",
                step=step.name or step.type,
                cost="Anyone who became unhappy during the wait is still asked. The "
                     "longer the wait, the more of them there are.")


@rule("GHL012", "Sandbox or test workflow is published", "low", "hygiene", "hygiene")
def published_sandbox(acct: Account):
    for wf in acct.published():
        name = wf.name.lower()
        if any(k in name for k in ("sandbox", "test", "probe", "ignore", "do not use",
                                   "do-not-use", "copy of", "untitled", "zz")):
            yield _finding(
                "GHL012", "low", wf,
                "A workflow named like a scratch build is live",
                "Anything published can enroll a real contact. A test build that "
                "reaches a customer is embarrassing at best.",
                "Unpublish it, or rename it if it is genuinely production.",
                cost="A live test build can enroll a real customer. Cheap to fix, "
                     "embarrassing to explain.")


# --------------------------------------------------------------------------
# Workflows that trigger each other — nothing in the UI shows this
# --------------------------------------------------------------------------

def _allows_reentry(wf: Workflow) -> bool:
    blob = json.dumps(wf.settings).lower()
    return any(k in blob for k in ("allowmultiple", "allow_multiple", "reentry",
                                   "re_entry", "allowreentry")) and "false" not in blob


@rule("GHL014", "Workflows re-trigger each other through tags", "critical", "routing", "triggers", "data")
def tag_trigger_loop(acct: Account):
    pubs = list(acct.published())
    by_id = {w.id: w for w in pubs}
    listeners: dict[str, list[str]] = {}
    for w in pubs:
        for tag in w.trigger_tags():
            listeners.setdefault(tag, []).append(w.id)
    edges: dict[str, set[str]] = {}
    for w in pubs:
        for tag in w.tags_added():
            for other in listeners.get(tag, []):
                edges.setdefault(w.id, set()).add(other)

    reported: set[frozenset] = set()

    def cycles_from(start: str, node: str, path: list[str]):
        for nxt in sorted(edges.get(node, ())):
            if nxt == start:
                yield path[:]
            elif nxt not in path:
                yield from cycles_from(start, nxt, path + [nxt])

    for start in sorted(edges):
        for cycle in cycles_from(start, start, [start]):
            key = frozenset(cycle)
            if key in reported:
                continue
            reported.add(key)
            wfs = [by_id[i] for i in cycle]
            names = " -> ".join(w.name for w in wfs) + f" -> {wfs[0].name}"
            reenters = any(_allows_reentry(w) for w in wfs)
            if len(wfs) == 1:
                title = f"'{wfs[0].name}' adds the tag that triggers itself"
            else:
                title = "Tag loop: " + " <-> ".join(w.name for w in wfs)
            yield Finding(
                rule="GHL014", severity="critical" if reenters else "high",
                workflow=wfs[0].name, step=names, title=title,
                symptom="Each workflow in this chain adds a tag that enrolls the "
                        "next one, and the chain closes back on itself. "
                        + ("Re-enrollment is ON inside the loop, so one contact "
                           "cycles through it forever — messages, opportunities and "
                           "alerts included — until someone notices in the "
                           "conversation feed."
                           if reenters else
                           "Re-enrollment is off, which caps it at one lap today — "
                           "but the first person to toggle 'allow re-entry' turns "
                           "this into an infinite messaging loop, and nothing in "
                           "the builder will warn them."),
                fix="Break the cycle at its weakest link: remove the add-tag step, "
                    "narrow the trigger, or have each workflow remove its own "
                    "trigger tag as its first step so a lap cannot restart.",
                category="routing", reach=sum(len(w.outbound) for w in wfs),
                cost="One contact can lap this chain indefinitely. Every lap is "
                     "another round of messages to the same person, another "
                     "opportunity in the pipeline, and another alert the team "
                     "learns to ignore.")


@rule("GHL015", "Two workflows enroll on the identical trigger", "high", "routing", "triggers")
def duplicate_enrollment(acct: Account):
    """Two senders racing on one event.

    Triggers are compared on a CANONICAL signature, not on the raw JSON:
    `contact_tag_added` and `contactTagAdded` are the same trigger, and
    `{"tag": "vip"}` and `{"field": "tag", "value": "vip"}` are the same filter.
    Comparing the literal export text reports these as two unrelated triggers
    and the collision — the actual defect — goes unreported.
    """
    groups: dict[tuple, list[Workflow]] = {}
    for w in acct.published():
        if not w.outbound:
            continue
        for sig in w.trigger_signatures():
            groups.setdefault(sig, []).append(w)

    seen: set[tuple] = set()
    for sig, wfs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        ttype = sig[0]
        by_name = {}
        for w in wfs:
            by_name.setdefault(w.name, w)
        names = sorted(by_name)
        if len(names) < 2:
            continue
        key = (ttype, tuple(names))
        if key in seen:
            continue
        seen.add(key)

        # Same trigger, same ordered action types AND the same message copy is
        # not a collision, it is the same workflow existing twice — what a
        # snapshot re-pushed onto a non-blank sub-account leaves behind.
        #
        # The copy check is not optional. Structure alone flagged a real
        # account's referral ask and its review ask as "2 identical copies" and
        # told the owner to unpublish one: same trigger, same twenty steps, and
        # completely different words. Reusing a skeleton is good practice, and
        # a critical whose fix is "delete a live campaign" has to be certain.
        # A snapshot re-push copies the copy too, so requiring it costs the
        # rule nothing on the case it exists to catch.
        shapes = {by_name[n].shape() for n in names}
        copies = {by_name[n].copy_fingerprint() for n in names}
        cloned = len(shapes) == 1 and len(copies) == 1 and len(names) > 1
        reach = max(len(by_name[n].outbound) for n in names)

        if cloned:
            yield Finding(
                rule="GHL015", severity="critical", workflow=names[0],
                step=", ".join(names[1:]), category="routing", reach=reach,
                title=f"{len(names)} identical copies of the same workflow are live",
                symptom="These workflows have the same trigger AND the same "
                        "sequence of actions: " + ", ".join(names) + ". That is "
                        "not two workflows racing, it is one workflow existing "
                        f"{len(names)} times — every contact who enrolls receives "
                        "each message twice, back to back, from the same number.",
                cost="Every single message this workflow sends is sent twice. To a "
                     "recipient that is indistinguishable from spam, and it is the "
                     "fastest way to earn a carrier complaint.",
                fix="Keep one, unpublish the rest. Duplicates like this usually "
                    "come from re-importing a snapshot onto an account that was "
                    "not blank — check the whole account for other pairs before "
                    "assuming this is the only one.")
            continue

        yield Finding(
            rule="GHL015", severity="high", workflow=names[0],
            step=", ".join(names[1:]), category="routing", reach=reach,
            title=f"{len(names)} workflows fire on the same '{ttype}' event "
                  "with equivalent filters",
            symptom="One event enrolls the contact in all of these at once: "
                    + ", ".join(names) + ". Each one sends its own messages, so "
                    "the lead hears from two sequences in the same afternoon and "
                    "reads it as spam. The builder shows each workflow alone — "
                    "this collision is invisible until a customer complains.",
            cost="Every lead through this trigger gets two conversations started "
                 "at once. The lead cannot tell which to answer, so a lot of "
                 "them answer neither.",
            fix="Decide which workflow owns this event and narrow or remove the "
                "trigger on the others. If both genuinely must run, make one of "
                "them internal-only (no outbound steps).")


# --------------------------------------------------------------------------
# Message copy that fails quietly
# --------------------------------------------------------------------------

GREETING_FIELD = re.compile(
    r"\b(hi|hey|hello|good\s+(?:morning|afternoon|evening))\s*,?\s*"
    r"\{\{\s*contact\.(first_name|name|full_name|last_name)\s*\}\}"
    r"[ \t]*([,.!?;:])", re.I)


@rule("GHL016", "Greeting merges a contact field with no fallback", "medium", "hygiene", "copy")
def bare_greeting_field(acct: Account):
    """Fires only when an empty field actually breaks the sentence.

    The tell is punctuation hugging the merge: 'Hey {{first_name}},' renders
    as 'Hey ,'. Copy that sets the name off with spaces — 'Hey
    {{first_name}} - quick question' — survives a blank as 'Hey - quick
    question' and is the correct SMS form, so it must pass; flagging it
    punished the account that already degraded gracefully.
    """
    for wf in acct.published():
        for step in wf.outbound:
            m = GREETING_FIELD.search(step.text())
            if m:
                yield _finding(
                    "GHL016", "medium", wf,
                    f"Greeting renders as '{m.group(1)} {m.group(3)}' when "
                    f"{m.group(2)} is empty",
                    "Imported lists and form fills routinely leave the name field "
                    "blank, and GoHighLevel renders a missing merge field as empty "
                    "text — the message still sends, opening with 'Hi ,'. That is "
                    "the tell of an automated blast, to exactly the lead you were "
                    "trying to sound personal for.",
                    "Give the merge field a default value, or write the opener so "
                    "it survives an empty field: set the name off with spaces and "
                    "no touching punctuation ('Hey {{first_name}} - quick "
                    "question' blanks to 'Hey - quick question'), or drop the "
                    "name ('Hey there'). Then fix the import so names arrive."
                    + (" Note that in SMS there is no fallback to give it — "
                       "HighLevel documents fallback values for email only — so "
                       "the copy itself has to survive the blank."
                       if step.is_sms else ""),
                    step=step.name or step.type,
                    cost="The first line of your first message announces that it was "
                         "automated. Reply rates on a 'Hi ,' opener are the lowest "
                         "of anything in the account.")
                break  # one finding per workflow is enough to make the point


OPT_OUT = re.compile(
    r"\b(?:reply|text|txt|send)\s+['\"]?stop\b|\bstop\s+to\s+(?:opt|unsub|end)|"
    r"\bopt[- ]?out\b|\bunsubscribe\b", re.I)
BULK_WORDS = ("reactivat", "database", "blast", "bulk", "list", "dormant", "cold",
              "winback", "win-back", "nurture")


@rule("GHL017", "SMS sequence carries no opt-out language", "high", "compliance", "compliance")
def sms_without_opt_out(acct: Account):
    for wf in acct.published():
        if any("appointment" in t.type.lower() or "booked" in t.type.lower()
               for t in wf.triggers):
            continue  # booking confirmations are a conversation the contact started
        texts = wf.steps_of(*SMS_TYPES)
        if not texts:
            continue
        bulky = any(k in wf.name.lower() for k in BULK_WORDS)
        if len(texts) < 2 and not bulky:
            continue
        if OPT_OUT.search(wf.text()):
            continue
        yield _finding(
            "GHL017", "high", wf,
            f"{len(texts)} SMS send{'s' if len(texts) != 1 else ''}, "
            "no 'reply STOP' anywhere in the sequence",
            "Carriers run A2P filtering on every message from a registered "
            "number, and campaigns without opt-out language are exactly what "
            "gets a number flagged. Nothing errors when that happens — "
            "deliverability just quietly dies, for every workflow in the "
            "account, and the client's report says leads 'stopped replying'.",
            "Put opt-out language ('Reply STOP to opt out') in the first SMS of "
            "the sequence. It also keeps the campaign consistent with what was "
            "declared in the A2P registration.",
            step=texts[0].name or texts[0].type,
            cost="A filtered number does not fail loudly — it stops arriving. When "
                 "that happens it takes every workflow in the account down with it, "
                 "and the symptom the client reports is 'leads stopped replying'.")


@rule("GHL018", "Nothing adds the tag this workflow waits for", "high", "dead_weight", "triggers", "hygiene")
def orphan_tag_trigger(acct: Account):
    """A tag-triggered workflow whose tag nothing in the account applies.

    A tag trigger fires on the tag arriving from anywhere — a form, a bulk
    action, an integration, a human clicking it — so an unfed tag is not proof
    the workflow is dead, and this rule does not claim it is.

    It does grade on what is at stake, because two very different situations
    were being reported identically. A tag-triggered workflow with no outbound
    steps is housekeeping: if it never runs, nobody notices, and that is a low.
    A published workflow that texts and emails customers, whose only way in is
    a tag that appears NOWHERE else in this export — not added, not removed,
    not branched on, not named in any copy — is a build that was paid for and
    may never have sent a single message. The evidence does not prove it is
    dead, but it is the only trace an audit can see, and reporting it at the
    same weight as a tidy-up note buries it.

    The "appears nowhere else" test is what separates the two. A tag a human
    really does apply by hand tends to leave fingerprints — a branch that reads
    it, a step that clears it, a mention in a message. A tag with no fingerprint
    at all is most often a step that was never built or a name that was typed
    twice, differently.
    """
    added: set[str] = set()
    for w in acct.workflows:
        added |= w.tags_added()

    # Every place a tag name can legitimately show up other than the trigger
    # that waits for it: an add-tag or remove-tag step, a branch condition, a
    # filter, message copy. Built once, from the whole account.
    elsewhere = " ".join(
        [w.text() for w in acct.workflows]
        + [t.filter_blob() for w in acct.workflows for t in w.triggers
           if not w.published or True]
    ).lower()

    for wf in acct.published():
        if not wf.triggers or not all("tag" in t.type.lower() for t in wf.triggers):
            continue  # another trigger type can still start it
        tags = wf.trigger_tags()
        if not tags or tags & added:
            continue
        # The caller can tell us a tag is applied by a human, a form, a bulk
        # action or an integration. That is the one thing an export cannot show,
        # and without a way to say it a correctly built account gets flagged at
        # high severity forever. Only silence the tags actually declared.
        tags = {t for t in tags if not acct.config.tag_comes_from_outside(t)}
        if not tags:
            continue
        missing = ", ".join(sorted(tags))

        # What is sitting behind the closed door?
        sends = len(wf.sms_steps) + len(wf.email_steps)
        # Does any of these tags leave a trace outside its own trigger? Each
        # trigger serialises its tag twice (the value and the filter blob), so
        # the floor for "only the trigger knows about it" is two per waiting
        # workflow — anything above that is a real second mention.
        waiting = sum(1 for w in acct.workflows for t in w.triggers
                      if tags & t.tag_values())
        traces = sum(elsewhere.count(t.lower()) for t in tags)
        unmentioned = traces <= waiting * 2

        if sends and unmentioned:
            yield _finding(
                "GHL018", "high", wf,
                f"Published, sends {sends} message{'s' if sends != 1 else ''}, "
                f"and the only way in is '{missing}' — which nothing here adds",
                "This workflow's only trigger waits for that tag, and the tag "
                "appears nowhere else in this account: no step adds it, no step "
                "removes it, no branch reads it, no message mentions it. The "
                "workflow is published and configured to contact customers, so "
                "either something outside this export applies the tag, or this "
                "sequence has never run for anybody. An audit cannot tell those "
                "apart from the outside — but nothing here supports the first "
                "one, and the usual cause is an add-tag step that was never "
                "built, or the same tag typed two different ways.",
                "Find where the tag is meant to come from and confirm it in the "
                "contact record of someone who should have entered. If another "
                "workflow was supposed to apply it, add that step; if it is "
                "applied by hand or by an integration, say so in the workflow "
                "description so the next audit stops flagging it.",
                reach=sends,
                cost=f"{sends} customer-facing message"
                     f"{'s are' if sends != 1 else ' is'} configured here and may "
                     "never have been sent. The work is built and paid for; it is "
                     "one tag away from running.")
            continue

        yield _finding(
            "GHL018", "low", wf,
            f"No workflow in this account adds '{missing}'",
            "This workflow only starts when that tag lands on a contact, and no "
            "workflow here applies it. If the tag comes from a form, a bulk "
            "action or a human, all is well — but if another workflow was "
            "supposed to add it, that step is missing or the tag name is "
            "misspelled, and this sequence has silently never run. Tag names "
            "must match exactly.",
            "Confirm where the tag is meant to come from. If it is another "
            "workflow, add or correct its add-tag step; if it is manual, note "
            "that in the workflow name so the next auditor does not ask.",
            cost="If the tag was supposed to come from another workflow, this "
                 "sequence has never run once — and everything it was built to do "
                 "has silently not been happening.")


# --------------------------------------------------------------------------
# GHL019+ — the checks that need the account, not just one workflow
# --------------------------------------------------------------------------

@rule("GHL019", "Conditional wait with no timeout", "critical", "routing", "waits")
def unbounded_conditional_wait(acct: Account):
    """A wait that ends on an event, with nothing to end it if the event never comes.

    A duration wait always finishes. A wait for a reply, an action, or a
    condition finishes only if that thing happens — and HighLevel's timeout on
    those is optional. Leave it off and the contact is parked in the workflow
    permanently: no error, no tag, no trace, and every step below the wait
    never runs for them.
    """
    for wf in acct.published():
        for i, step in enumerate(wf.steps):
            if not step.wait_is_conditional():
                continue
            if step.wait_timeout() is not None:
                continue
            downstream = wf.outbound_after(i)
            leak = len(downstream)
            if leak == 0:
                # Nothing below it, so nobody is missing a message. It still
                # holds contacts in the workflow forever, which distorts every
                # re-entry check — worth saying, not worth alarming about.
                yield _finding(
                    "GHL019", "low", wf,
                    "Conditional wait with no timeout, at the end of the workflow",
                    "This wait ends only when its condition is met, and nothing "
                    "below it would have run anyway. Contacts who never meet the "
                    "condition stay parked in the workflow indefinitely, which is "
                    "harmless for messaging but means the workflow's contact count "
                    "never goes down and re-entry can behave unexpectedly.",
                    "Set a maximum wait so contacts exit cleanly, even if only to "
                    "keep the workflow's numbers honest.",
                    step=step.name or step.type, reach=0,
                    cost="Nothing directly. It hides the real enrollment count, "
                         "which makes every other number about this workflow wrong.")
                continue
            yield _finding(
                "GHL019", "critical", wf,
                f"Wait for an event with no timeout — {leak} message"
                f"{'s' if leak != 1 else ''} below it never send",
                "This wait resumes on an event (a reply, an action, a condition) "
                "and has no maximum. A contact who never produces that event is "
                f"parked here forever: the {leak} outbound step"
                f"{'s' if leak != 1 else ''} below never run for them, they are "
                "never marked as unresponsive, and nothing in GoHighLevel reports "
                "it. In an audit this is usually where the 'leads that just went "
                "quiet' actually went.",
                "Set a maximum wait on the step (a day, a week — whatever matches "
                "the sequence), and branch the timeout path to the follow-up you "
                "would have wanted anyway.",
                step=step.name or step.type, reach=leak,
                cost="Every lead who does not reply is silently dropped instead of "
                     "followed up. These are leads you already paid to acquire and "
                     "then simply stopped contacting.")


@rule("GHL020", "Reference to something that does not exist", "critical", "hygiene", "references")
def dangling_reference(acct: Account):
    """Steps pointing at a calendar, user, pipeline or template that is gone.

    HighLevel documents that it SKIPS a step whose reference it cannot resolve,
    and its own in-builder error highlighting does not check for this. So a
    workflow can be published, green, and quietly doing nothing at that step.

    This needs the account's object lists. Without them a missing ID and an ID
    that simply was not exported look identical, so the rule reports itself as
    skipped rather than guessing.
    """
    inv = acct.inventory

    # The half that needs nothing: a rotation with nobody in it.
    for wf in acct.published():
        for step in wf.steps:
            pool = step.user_pool()
            if pool is not None and len(pool) == 0:
                yield _finding(
                    "GHL020", "high", wf,
                    "Round-robin assignment with nobody in the rotation",
                    "This step distributes contacts across a list of users and the "
                    "list is empty. HighLevel skips an assignment it cannot make "
                    "and carries on, so the contact continues through the workflow "
                    "unassigned — and every later step that notifies 'the assigned "
                    "user' has nobody to notify.",
                    "Add the users who should receive these contacts, or remove "
                    "the step and assign on the calendar instead.",
                    step=step.name or step.type,
                    cost="Leads land in the account owned by nobody. Nobody calls a "
                         "lead that is not theirs.")

    checkable = ("calendar", "user", "pipeline", "stage", "form", "survey",
                 "template", "workflow")
    bucket_for = {"calendar": "calendars", "user": "users",
                  "pipeline": "pipelines", "stage": "stages", "form": "forms",
                  "survey": "surveys", "template": "templates"}
    workflow_ids = {w.id for w in acct.workflows if w.id}

    # One class of reference needs nothing but the export in hand: a step that
    # jumps a contact into another workflow, or pulls them out of one, names a
    # workflow — and the workflows are right here. This used to sit inside the
    # object-list branch below, so on a workflows-only export the whole rule
    # reported itself skipped and never made the one check it could have made.
    # A critical check that goes blind on data it is holding is worse than a
    # missing check, because the coverage line says it was only unsupplied.
    misses: dict = {}
    if workflow_ids:
        for wf in acct.published():
            for step in wf.steps:
                for kind, ident in step.entity_refs():
                    if kind == "workflow" and ident not in workflow_ids:
                        misses.setdefault((wf.name, kind), []).append(
                            (step.name or step.type, ident))

    have_any = any(inv.has(bucket_for[k]) for k in checkable if k in bucket_for)
    if not have_any:
        # Still a skip: the calendar, user, pipeline and template references
        # genuinely cannot be judged, and coverage must keep saying so. The
        # workflow-to-workflow findings above stand on their own.
        yield from _dangling_findings(acct, misses)
        yield Skip(
            rule="GHL020",
            title="Reference to something that does not exist",
            reason="No account object lists were supplied, so a reference to a "
                   "deleted calendar, user, pipeline or template cannot be told "
                   "apart from one that simply was not in this export. "
                   "Workflow-to-workflow references were checked.",
            needs="calendars / users / pipelines / forms / surveys / "
                  "emailTemplates in the input bundle",
            category="hygiene")
        return

    for wf in acct.published():
        for step in wf.steps:
            for kind, ident in step.entity_refs():
                if kind == "workflow":
                    continue  # already done above
                bucket = bucket_for.get(kind)
                if not bucket or not inv.has(bucket):
                    continue
                known = inv.known(kind)
                if ident not in known:
                    misses.setdefault((wf.name, kind), []).append(
                        (step.name or step.type, ident))
                elif kind == "user" and not known[ident].get("active", True):
                    yield _finding(
                        "GHL020", "high", wf,
                        f"Step targets a deactivated user ({known[ident]['name']})",
                        "The user this step assigns or notifies is no longer active "
                        "in the account. HighLevel does not warn about this — the "
                        "step simply reaches nobody.",
                        "Point the step at an active user, or at a role, so it "
                        "survives the next person leaving.",
                        step=step.name or step.type,
                        cost="Every notification this step sends goes nowhere. "
                             "Leads it was meant to surface are never seen.")

    yield from _dangling_findings(acct, misses)


def _dangling_findings(acct: Account, misses: dict):
    """Emit one critical per (workflow, reference kind) that did not resolve."""
    for (wf_name, kind), hits in sorted(misses.items()):
        wf = next(w for w in acct.published() if w.name == wf_name)
        names = ", ".join(sorted({h[0] for h in hits}))
        yield _finding(
            "GHL020", "critical", wf,
            f"{len(hits)} step{'s point' if len(hits) != 1 else ' points'} "
            f"at a {kind} that no longer exists",
            f"These steps reference a {kind} ID that is not in this account: "
            + ", ".join(sorted({h[1] for h in hits}))
            + ". HighLevel skips a step it cannot resolve and continues the "
              "workflow without raising anything — so the workflow reports as "
              "running normally while that step does nothing at all.",
            f"Re-point the step at a live {kind}. If this account came from a "
            "snapshot, expect more of these — snapshots carry structure, not "
            "the objects the structure pointed at.",
            step=names,
            cost="The step is doing nothing, and has been since whatever it "
                 "pointed at was deleted. Whatever it was supposed to do — book, "
                 "assign, notify, send — has silently not been happening.")


@rule("GHL021", "Condition branch with nothing in it", "high", "routing", "branches")
def empty_branch(acct: Account):
    """An If/Else path with no actions — a silent exit.

    HighLevel creates the None/Else branch automatically and it cannot be
    deleted. Left empty, every contact who matches none of the conditions leaves
    the workflow there: untagged, unmessaged, and impossible to find later
    because nothing recorded that it happened.
    """
    for wf in acct.published():
        for i, step in enumerate(wf.steps):
            if not step.is_branch:
                continue
            branches = step.branches()
            if not branches:
                continue
            empties = [label for label, kids in branches if not kids]
            if not empties:
                continue
            terminal = not any(s.is_outbound for s in wf.steps[i + 1:])
            label = ", ".join(empties)
            if terminal:
                yield _finding(
                    "GHL021", "low", wf,
                    f"Branch '{label}' is empty, at the end of the workflow",
                    "This condition drops everyone who does not match, and there "
                    "was nothing after it to drop them out of. That is a valid "
                    "way to build a filter — the cost is only that the people it "
                    "drops leave no trace, so you cannot size the population "
                    "later.",
                    "If the filter is deliberate, add a tag on the empty path "
                    "('unrouted', 'did-not-qualify') so the group is countable.",
                    step=step.name or step.type, reach=0,
                    cost="Nothing breaks. You just have no way of knowing how many "
                         "contacts this filter is rejecting.")
                continue
            yield _finding(
                "GHL021", "high", wf,
                f"Branch '{label}' is empty — contacts matching nothing "
                "silently exit here",
                "GoHighLevel creates the None/Else path automatically and you "
                "cannot remove it. Empty, it is a trapdoor: every contact who "
                "matches none of the conditions leaves the workflow at this step "
                "with no tag, no message and no record. Nothing downstream ever "
                "runs for them, and nothing anywhere says so.",
                "Put something on the empty path — even just an 'unrouted' tag. "
                "Then the population becomes visible and you can decide what it "
                "deserves. A branch you can count is a branch you can fix.",
                step=step.name or step.type,
                cost="An unknown share of your contacts fall out of this workflow "
                     "before it does anything for them. You cannot even measure "
                     "how many, which is why this survives so long.")


@rule("GHL022", "Broken step wiring", "high", "routing", "wiring")
def broken_wiring(acct: Account):
    """Links that point at nothing, and steps nothing points at.

    This only applies to exports that carry node ids and links. A flat ordered
    step list has no wiring to break, so there is nothing to check and nothing
    to claim — which is why the rule reports a skip instead of a pass when it
    finds no wiring anywhere in the file.
    """
    wired = [wf for wf in acct.published() if wf.has_wiring]
    if not wired:
        yield Skip(
            rule="GHL022",
            title="Broken step wiring",
            reason="No workflow in this export carries step ids and links, so "
                   "there is no wiring to check. A flat list of steps in order "
                   "cannot have a broken connection.",
            needs="an export that includes each step's id and its next/parentKey",
            category="routing")
        return

    for wf in wired:
        ids = wf.step_ids()
        by_id = {s.step_id: s for s in wf.steps if s.step_id}

        dangling = []
        for step in wf.steps:
            for target in step.next_ids():
                if target not in ids:
                    dangling.append((step.name or step.type, target))
        if dangling:
            yield _finding(
                "GHL022", "high", wf,
                f"{len(dangling)} step link"
                f"{'s point' if len(dangling) != 1 else ' points'} at a node "
                "that is not in this workflow",
                "A step's next-step pointer names a node that does not exist "
                "here — usually the remains of a step someone deleted from a "
                "live workflow. The run reaches that point and has nowhere to "
                "go, so everything intended to follow simply does not happen.",
                "Open the workflow in the builder and re-connect the step to "
                "whatever should follow it. Then check whether contacts are "
                "currently parked at that point.",
                step=", ".join(name for name, _ in dangling[:4]),
                cost="The sequence stops dead at this step. Every contact who "
                     "reaches it gets nothing else, and the workflow still "
                     "reports as running.")

        # Reachability: start from the roots and follow both link shapes.
        reachable = {s.step_id for s in wf.steps
                     if s.step_id and not s.parent_key}
        if not reachable and wf.steps and wf.steps[0].step_id:
            reachable = {wf.steps[0].step_id}
        changed = True
        while changed:
            changed = False
            for step in wf.steps:
                sid = step.step_id
                if not sid or sid in reachable:
                    continue
                parent = step.parent_key
                # GoHighLevel writes a branch child's parentKey as
                # "<parentId>-<branchName>", so this is a prefix test.
                if any(parent == p or parent.startswith(p + "-")
                       for p in reachable):
                    reachable.add(sid)
                    changed = True
                    continue
                if any(sid in by_id[p].next_ids() for p in reachable if p in by_id):
                    reachable.add(sid)
                    changed = True
        orphans = [s for s in wf.steps if s.step_id and s.step_id not in reachable]
        if orphans:
            yield _finding(
                "GHL022", "medium", wf,
                f"{len(orphans)} step{'s' if len(orphans) != 1 else ''} "
                f"nothing can reach",
                "These steps are in the workflow but no path from the trigger "
                "arrives at them. They are leftovers — a branch someone detached, "
                "or a step dragged out of the flow and never deleted. They will "
                "never run, and they make the workflow read as if it does more "
                "than it does.",
                "Delete them, or re-connect them if they were meant to be in the "
                "flow. Either way the canvas should show what actually executes.",
                step=", ".join((s.name or s.type) for s in orphans[:4]),
                category="dead_weight", reach=0,
                cost="Nothing today. The cost lands on whoever edits this next and "
                     "believes the canvas.")


def _merged_into_a_url(body: str, token: str) -> bool:
    """Is this merge field sitting inside a link?

    A blank in a sentence is embarrassing. A blank inside a URL is a link that
    goes nowhere, so the severity differs — which means the check has to be
    reliable rather than clever. It takes the unbroken run of text around each
    occurrence and asks whether that run looks like a URL at all.
    """
    for match in re.finditer(re.escape(token), body):
        start = max(body.rfind(" ", 0, match.start()),
                    body.rfind("\n", 0, match.start())) + 1
        ends = [i for i in (body.find(" ", match.end()),
                            body.find("\n", match.end())) if i != -1]
        run = body[start:min(ends) if ends else len(body)]
        if "://" in run or run.lower().startswith("www."):
            return True
        # A merge token contains spaces ({{ custom_values.x }}), so the run can
        # stop before the closing braces. Look just past them for a path or an
        # extension, which is the other way a field ends up inside a link.
        if re.match(r"\s*\}\}[/?#]|\s*\}\}\.[a-z]{2,}",
                    body[match.end():match.end() + 60]):
            return True
    return False


@rule("GHL023", "Merge field that resolves to nothing", "high", "hygiene", "copy")
def merge_field_integrity(acct: Account):
    """Fields that render blank in a message that still sends.

    Two failures, and the second is the one that ships broken booking links:
      * the custom value does not exist  (already caught by GHL008)
      * the custom value exists and is EMPTY

    Plus a contact field the account does not have, which needs the account's
    custom-field list to check.
    """
    slugs = acct.custom_value_slugs()

    for wf in acct.published():
        for step in wf.outbound:
            body = step.bodies() or step.text()
            for key in set(re.findall(
                    r"\{\{\s*custom_values\.([a-zA-Z0-9_]+)", body)):
                entry = slugs.get(slug(key))
                if entry is None or entry[1].strip():
                    continue  # missing is GHL008's; non-empty is fine
                in_url = _merged_into_a_url(body, "custom_values." + key)
                yield _finding(
                    "GHL023", "critical" if in_url else "high", wf,
                    f"Custom value '{entry[0]}' is defined but empty",
                    f"The message merges {{{{ custom_values.{key} }}}} and that "
                    "value is an empty string in this account. GoHighLevel does "
                    "not treat an empty merge field as an error — it renders it "
                    "as nothing and sends the message anyway."
                    + (" It sits inside a URL here, so the link the contact "
                       "receives is malformed." if in_url else ""),
                    f"Fill in '{entry[0]}' under Settings -> Custom Values. If the "
                    "value genuinely varies per contact, it should be a custom "
                    "FIELD, not a custom value.",
                    step=step.name or step.type,
                    cost="Every message using it goes out with a gap where the "
                         "booking link or the business name should be. The "
                         "recipient reads it as broken and does not reply.")

    inv = acct.inventory
    if not inv.has("custom_fields"):
        yield Skip(
            rule="GHL023",
            title="Contact merge field that the account does not have",
            reason="The account's custom-field list was not supplied, so a "
                   "misspelled merge field cannot be told apart from a real "
                   "custom field that was simply not exported.",
            needs="customFields in the input bundle",
            category="hygiene")
        return

    for wf in acct.published():
        for step in wf.outbound:
            body = step.bodies() or step.text()
            unknown = set()
            for token in MERGE_FIELD.findall(body):
                if "." not in token:
                    continue
                namespace, _, name = token.partition(".")
                if namespace != "contact":
                    continue
                if name in STANDARD_CONTACT_FIELDS:
                    continue
                if slug(name) in inv.custom_fields:
                    continue
                unknown.add(name)
            for name in sorted(unknown):
                yield _finding(
                    "GHL023", "medium", wf,
                    f"No contact field named '{name}' exists in this account",
                    f"The message merges {{{{ contact.{name} }}}} and this "
                    "account has no standard or custom field by that name. A "
                    "merge field GoHighLevel cannot resolve renders as empty "
                    "text and the message sends regardless — so the sentence "
                    "built around it arrives with a hole in it. The usual cause "
                    "is a spelling or a field that existed in the account this "
                    "was copied from.",
                    f"Correct the merge field, or create the custom field "
                    f"'{name}'. Check the field's key, not its display name — "
                    "they are frequently different.",
                    step=step.name or step.type,
                    cost="The message sends with a blank in the middle of a "
                         "sentence. It reads as broken software, which is what "
                         "it is.")


@rule("GHL024", "Fallback filter in an SMS", "medium", "hygiene", "copy")
def sms_fallback_filter(acct: Account):
    """A default value written into an SMS, where defaults are not supported.

    HighLevel's merge-field documentation lists fallback values as supported in
    "email templates, workflow emails, campaign emails, and bulk emails". SMS is
    not on that list.

    UNVERIFIED: what SMS actually does with the filter — render the literal
    text, or drop the fallback and leave a blank — is not documented anywhere I
    could find, and I have not tested it on a live send. The finding is worded
    to say that, because both outcomes are wrong and the author needs to know
    the safety net they think they have is not there either way.
    """
    for wf in acct.published():
        for step in wf.steps:
            if not step.is_sms:
                continue
            body = step.bodies() or step.text()
            if not FALLBACK_FILTER.search(body):
                continue
            yield _finding(
                "GHL024", "medium", wf,
                "SMS uses a fallback value, and SMS has no fallbacks",
                "This text has a merge field with a default written into it "
                "(the `| default:` filter). HighLevel documents fallback values "
                "for email templates and email sends only — SMS is not on that "
                "list. So the protection the copy appears to have is not there: "
                "either the filter renders as literal text in the message, or "
                "it is ignored and the field renders blank. I have not tested "
                "which, and both are a message you would not want sent.",
                "Take the filter out and write the sentence so it survives an "
                "empty field — 'Hey there' instead of 'Hey {{first_name}}'. In "
                "SMS the copy is the only fallback you get.",
                step=step.name or step.type,
                cost="Either your customers see template syntax in a text "
                     "message, or they see a gap. Neither reads as a business "
                     "worth replying to.")


@rule("GHL025", "Email that will not land", "high", "compliance", "deliverability")
def email_deliverability(acct: Account):
    """Unsubscribe and sending-domain authentication.

    Google and Yahoo's bulk-sender rules made the unsubscribe link a delivery
    requirement, not just a legal one — and an unauthenticated sending domain
    is why "our emails go to spam" is usually not about the copy at all.
    """
    inv = acct.inventory
    settings = inv.email_settings or {}
    default_unsub = settings.get("default_unsubscribe",
                                 settings.get("defaultUnsubscribe"))

    for wf in acct.published():
        emails = wf.email_steps
        if not emails or acct.config.is_transactional(wf.name):
            continue
        # A receipt or a booking confirmation is transactional and does not need
        # one; a sequence is marketing whatever it is called.
        #
        # The line sits at two emails, not one. Google's sender guidance names
        # reservation confirmations as exempt, and a booking workflow that
        # sends the confirmation and then a pre-call note is still that one
        # reservation — the second message is anchored to the same event, not a
        # second attempt at the person. This account had exactly that shape
        # ("Strategy Call - Booking & Confirmation v2": confirmation + pre-call)
        # and the rule told its owner to put an unsubscribe link on an
        # appointment confirmation, which is both wrong and harmful: opting out
        # of transactional mail is how someone stops receiving their reminder.
        #
        # A third email is where it stops being one transaction. By then the
        # workflow is chasing — "Same-day rebook / Second touch / Close-out" is
        # a campaign whatever triggered it, and that one still gets flagged.
        # ⛔ `form_submitted` does NOT belong in this list, and used to.
        # Google's exemption is for transactions and reservations — receipts,
        # order and payment confirmations, appointment confirmations. A form
        # submission is a lead capture. The mail that follows it is the
        # definition of marketing, and on a real account this exemption hid a
        # published cold-chase ("Speed to Lead - 5 Minute Response": instant
        # SMS, backup email, booking link, last touch) from a compliance rule,
        # purely because it sent exactly two emails off a form trigger. An
        # order form still qualifies: `order_form_submitted` matches on
        # "order", which is the trigger that names an actual transaction.
        transactional = any(
            any(k in t.canonical_type() for k in
                ("appointment", "order", "payment", "invoice"))
            for t in wf.triggers) and len(emails) <= 2
        if transactional:
            continue
        blob = wf.bodies() or wf.text()
        if re.search(r"unsubscribe|\{\{\s*unsubscribe", blob, re.I):
            continue
        if default_unsub is True:
            continue
        # Knowing the account-level default is the difference between a fact and
        # a suspicion. Told it is off, this is a confirmed gap. Told nothing, it
        # may already be covered account-wide — so it is raised as something to
        # confirm, not as a defect, and the wording says which.
        yield _finding(
            "GHL025", "high" if default_unsub is False else "medium", wf,
            f"{len(emails)} marketing email{'s' if len(emails) != 1 else ''} "
            "with no unsubscribe link",
            "No unsubscribe token appears in any of these email bodies"
            + ("" if default_unsub is None else
               ", and this account's default unsubscribe link is switched off")
            + ". Since the 2024 bulk-sender rules, Gmail and Yahoo treat a "
              "missing one-click unsubscribe as a reason to reject or junk the "
              "message — so this is a delivery problem before it is a legal "
              "one. The recipients who wanted out and could not find the link "
              "mark it as spam instead, which costs the sending domain far more."
            + ("" if default_unsub is not None else
               " I could not see this account's default-unsubscribe setting, so "
               "if it is switched on at the account level you are already "
               "covered — worth confirming."),
            "Turn the account-level default unsubscribe link on, or put "
            "{{unsubscribe}} in the footer of each of these emails. Then send "
            "one to a Gmail address and check the headers show a "
            "List-Unsubscribe entry.",
            step=emails[0].name or emails[0].type,
            category="compliance",
            cost="Spam complaints from people who could not unsubscribe are the "
                 "fastest way to burn a sending domain — and a burned domain "
                 "takes every email in the account down with it.")

    if not inv.has("email_domains"):
        yield Skip(
            rule="GHL025",
            title="Sending domain is not authenticated",
            reason="No sending-domain list was supplied, so whether this "
                   "account sends from an authenticated domain is unknown. "
                   "Authentication state lives in DNS and in the account's "
                   "email settings, not in the workflows.",
            needs="emailDomains in the input bundle (domain + verified)",
            category="deliverability")
        return

    senders = [w for w in acct.published() if w.email_steps]
    if senders and not inv.verified_email_domains:
        listed = ", ".join(d["domain"] for d in inv.email_domains) or "none at all"
        yield Finding(
            rule="GHL025", severity="high", workflow="(account)",
            step=listed, category="deliverability",
            reach=sum(len(w.email_steps) for w in senders),
            title="No verified sending domain — every email in the account is "
                  "sending unauthenticated",
            symptom=f"{len(senders)} published workflows send email and this "
                    f"account has no verified sending domain ({listed}). "
                    "Unauthenticated mail fails SPF, DKIM and DMARC alignment at "
                    "the receiving end. Gmail and Microsoft do not put that in "
                    "spam — increasingly they refuse it at the SMTP layer, which "
                    "is why 'we checked, we're not in the spam folder' proves "
                    "nothing.",
            cost="Some unknown share of every email this account sends is never "
                 "seen. It costs nothing to send and nothing arrives, so the "
                 "reports all look fine.",
            fix="Set up a dedicated sending subdomain and complete its DNS "
                "verification, then re-check SPF, DKIM and DMARC before "
                "sending anything else at volume.")


@rule("GHL026", "Workflow with no enrollments", "low", "dead_weight", "stats")
def dead_weight(acct: Account):
    """Published, and nothing has entered it.

    Needs enrollment counts, which are not in a workflow export. A zero here is
    two very different things — dead weight nobody deleted, or a workflow whose
    trigger has silently never fired — and the finding says which one it looks
    like rather than picking for you.
    """
    inv = acct.inventory
    if not inv.has("stats"):
        yield Skip(
            rule="GHL026",
            title="Workflow with no enrollments",
            reason="No enrollment counts were supplied. A workflow export "
                   "cannot show whether anything has ever entered a workflow, "
                   "so dead weight is indistinguishable from a workflow that "
                   "just started yesterday.",
            needs='stats in the input bundle: {"<workflow id or name>": '
                  '{"enrollments": 0}}',
            category="dead_weight")
        return

    days = acct.config.stats_window_days
    for wf in acct.published():
        count = inv.enrollments(wf)
        if count is None or count > 0:
            continue
        live_trigger = bool(wf.triggers)
        sends = len(wf.outbound)
        if live_trigger and sends:
            yield _finding(
                "GHL026", "medium", wf,
                f"Published with a live trigger, and nothing has entered it in "
                f"{days} days",
                "This workflow is published, has a trigger, and sends "
                f"{sends} message{'s' if sends != 1 else ''} — and no contact "
                f"has enrolled in {days} days. Published workflows do not sit "
                "idle by accident: either the trigger references something that "
                "no longer fires (a deleted form, a tag nobody applies, a "
                "calendar that moved), or this is a build that was finished and "
                "then never connected to anything.",
                "Trace the trigger back to its source and confirm that source "
                "still exists and still fires. If it was retired, unpublish the "
                "workflow so the next person does not have to work this out.",
                reach=sends,
                cost="Whatever this workflow was built to do has not been "
                     "happening. If it is the speed-to-lead or the reminder "
                     "ladder, that is every lead and every appointment it "
                     "should have touched.")
            continue
        yield _finding(
            "GHL026", "low", wf,
            f"Published, no enrollments in {days} days",
            "Nothing has entered this workflow in the reporting window and it "
            "has no trigger that could bring anyone in. It is published, which "
            "means it can still be enrolled into by hand or by a bulk action, "
            "but as configured it does nothing.",
            "Unpublish it. A workflow list that only contains live workflows is "
            "the single biggest quality-of-life improvement in an inherited "
            "account.",
            reach=0,
            cost="Nothing directly. It costs the next person's time, every time "
                 "they have to work out whether it matters.")


@rule("GHL027", "Required step is missing", "high", "routing", "manifest")
def required_step_missing(acct: Account):
    """The build manifest says this workflow contains a step; it does not.

    This is what catches a rebuild script that replaced a whole workflow and
    dropped the integration webhook somebody else had spliced into it — the
    class of failure where the workflow looks completely correct on its own and
    is only wrong against what the build was supposed to be.

    It is entirely caller-supplied. Without a manifest there is nothing to check
    against and the rule says so.
    """
    manifest = acct.config.required_steps
    if not manifest:
        yield Skip(
            rule="GHL027",
            title="Required step is missing",
            reason="No build manifest was supplied, so there is nothing to "
                   "check the workflows against. Only the person who designed "
                   "the account knows which steps are load-bearing.",
            needs='required_steps in --config: {"<workflow name>": '
                  '["<step name>", ...]}',
            category="routing")
        return

    by_norm = {" ".join(w.name.split()).strip().lower(): w
               for w in acct.workflows}
    for wf_key, wanted in sorted(manifest.items()):
        wf = by_norm.get(wf_key)
        if wf is None:
            yield Finding(
                rule="GHL027", severity="high", workflow=wf_key,
                step=", ".join(wanted), category="routing", reach=0,
                title="The manifest names a workflow this account does not have",
                symptom=f"The build manifest expects a workflow called "
                        f"'{wf_key}' and there is none by that name. Either it "
                        "was renamed, it was never deployed, or it was deleted.",
                cost="Everything the manifest expected that workflow to do is "
                     "not happening anywhere in this account.",
                fix="Confirm whether the workflow was renamed (update the "
                    "manifest) or is genuinely missing (redeploy it).")
            continue
        have = {s.name.strip().lower() for s in wf.steps if s.name}
        missing = [n for n in wanted if n.strip().lower() not in have]
        if not missing:
            continue
        yield _finding(
            "GHL027", "high", wf,
            f"{len(missing)} required step{'s are' if len(missing) != 1 else ' is'} "
            "missing from this workflow",
            "The build manifest lists these steps as required here and they are "
            "not present: " + ", ".join(repr(m) for m in missing) + ". This is "
            "the shape of failure that follows a rebuild — a script that "
            "replaces a whole workflow knows nothing about steps another script "
            "spliced in, so they disappear and the workflow still looks right.",
            "Re-run whatever adds these steps (they are usually idempotent), "
            "then re-audit before calling the deploy finished.",
            step=", ".join(missing),
            cost="The integration or notification these steps carried is "
                 "silently not firing. Downstream systems keep reporting "
                 "healthy because nothing tells them a message stopped coming.")


# --------------------------------------------------------------------------
# GHL028+ — the failures practitioners actually report, distilled from the
# vendor's own support portal and its idea board. Each one is a configuration
# GoHighLevel accepts as valid and then executes exactly as written.
# --------------------------------------------------------------------------

def _cancel_guard(acct: Account):
    """The workflow, if any, that pulls contacts out when an appointment dies.

    GoHighLevel implements a reschedule as delete-and-recreate: "booked" fires
    again for the new slot and nothing ever fires for the old one. So the only
    thing standing between a cancelled or moved appointment and a reminder
    ladder still counting down to it is a workflow that listens for Cancelled
    and removes the contact. Finding it is what lets the reminder rule tell
    "unhandled" apart from "handled somewhere else".
    """
    for wf in acct.published():
        for t in wf.triggers:
            if "appointment" not in t.type.lower():
                continue
            if "cancel" in t.filter_blob() and wf.exits():
                return wf
    return None


@rule("GHL028", "Reminders for an appointment that no longer exists",
      "critical", "routing", "appointments", "timing")
def reminder_ladder_without_an_exit(acct: Account):
    """An appointment-triggered sequence with no way out when the slot dies.

    A reschedule in GoHighLevel deletes the appointment and creates a new one.
    "Appointment Booked" fires again for the new time; "Cancelled" never fires
    for the old one. A reminder ladder with no exit therefore runs BOTH ladders
    — the contact gets reminders for two different times, one of them for a slot
    that no longer exists. A working auditor calls duplicate reminders for old
    times the most common fault found in audits, and the cancelled-reminder
    complaint thread has been open with the vendor since 2019.

    The exit can live in this workflow (a Remove-from-Workflow, a status gate)
    or in a dedicated cancellation workflow that cleans up account-wide — both
    count, which is why the whole account is read before anything is flagged.
    """
    guard = _cancel_guard(acct)
    for wf in acct.published():
        appt = [t for t in wf.triggers
                if any(a in t.type.lower() for a in APPOINTMENT_TRIGGERS)]
        if not appt:
            continue
        # A workflow the cancellation itself triggers is the cleanup lane, not
        # the risk — its appointment is already gone.
        if all("cancel" in t.filter_blob() for t in appt):
            continue
        if len(wf.outbound) < 2 or not any(s.is_wait for s in wf.steps):
            continue
        if wf.exits():
            continue
        if any(s.is_branch and re.search(r"cancel|resched|status",
                                         s.name + " " + s.text(), re.I)
               for s in wf.steps):
            continue
        if guard is not None:
            yield _finding(
                "GHL028", "low", wf,
                f"Cancellations are handled by '{guard.name}' — confirm it "
                "removes contacts from this workflow",
                "This sequence has no exit of its own for a cancelled or "
                "rescheduled appointment, which is correct when a dedicated "
                "cancellation workflow pulls contacts out account-wide. But "
                "that workflow removes contacts from a named list, and a "
                "sequence added later is easy to leave off it. If this one is "
                "missing, its reminders keep firing for the dead slot.",
                f"Open '{guard.name}' and check its remove step names this "
                "workflow.",
                cost="If this sequence was left off the cleanup list, every "
                     "cancelled appointment still gets its full reminder "
                     "ladder — for a call that is not happening.")
            continue
        yield _finding(
            "GHL028", "critical", wf,
            f"{len(wf.outbound)} reminders keep sending after a cancel or "
            "reschedule",
            "Nothing removes a contact from this sequence when their "
            "appointment is cancelled or moved. On a reschedule GoHighLevel "
            "deletes the appointment and books a new one — the booked trigger "
            "fires again, Cancelled never fires, and the contact is now inside "
            "this ladder twice, getting reminders for two different times. On "
            "a plain cancellation they keep getting reminders for a slot that "
            "no longer exists. Nothing in this account handles either case.",
            "Add an Appointment Status trigger covering Cancelled that removes "
            "the contact from this workflow, or gate each reminder on an "
            "If/Else that re-checks the appointment status before sending.",
            cost="Every cancelled or moved appointment keeps being reminded "
                 "about the old time. It reads as a business that does not "
                 "know its own calendar, on the exact lead who was engaged "
                 "enough to book.")


@rule("GHL029", "Delayed messages with no send window", "high", "compliance",
      "timing")
def delayed_sends_without_a_window(acct: Account):
    """An SMS or call fired after a wait, in a workflow with no send window.

    A send that follows its trigger immediately inherits the trigger's hour —
    the lead who submits a form at 11pm expects the instant reply at 11pm, and
    flagging speed-to-lead for answering fast would be noise. A send that
    follows a WAIT inherits nothing: "wait 3 days" lands at whatever hour the
    trigger happened to fire, three days later. Without a window that is the
    3am text.

    Appointment-triggered workflows are exempt — their sends are timed off the
    slot, and GHL004 exists precisely because a window on those HOLDS the
    reminder past the call. Workflows with a documented window policy are
    exempt too: GHL013 already reports drift against the documented window,
    which is the sharper finding.
    """
    for wf in acct.published():
        if wf.send_window():
            continue
        if acct.config.is_transactional(wf.name):
            continue
        configured, _ = acct.config.wants_window(wf.name)
        if configured:
            continue
        if any(any(a in t.type.lower() for a in APPOINTMENT_TRIGGERS)
               for t in wf.triggers):
            continue
        waited = False
        delayed = []
        for s in wf.steps:
            if s.is_wait:
                # A wait carrying its own send window resumes inside that
                # window, so the send right after it is timed, not stray.
                #
                # A short hold does not move a send into a different hour
                # either. This rule's whole premise is that "wait 3 days" lands
                # at whatever o'clock the trigger fired — a one-minute pause to
                # let a form finish writing lands in the same minute, and the
                # lead who submitted at 11:40pm is expecting that reply. The
                # docstring above already said flagging speed-to-lead would be
                # noise; without this it did exactly that.
                minutes = s.wait_minutes()
                brief = minutes is not None and minutes < 60
                waited = not _step_window(s) and not brief
            elif waited and (s.is_sms or s.type in ("call", "manual_call",
                                                    "voicemail")):
                delayed.append(s)
        if not delayed:
            continue
        yield _finding(
            "GHL029", "high", wf,
            f"{len(delayed)} message{'s' if len(delayed) != 1 else ''} can "
            "fire at any hour of the night",
            "These sends sit after a wait and this workflow has no send "
            "window, so they go out at whatever hour the trigger originally "
            "fired — a form submitted at 11:40pm produces a follow-up text at "
            "11:40pm three days later. Quiet-hours rules in several states "
            "stop at 8pm, and the recipient's first waking act is an opt-out "
            "either way.",
            "Set a send window on this workflow (9am-8pm in the contact's "
            "timezone is the safe default). Leave windows OFF instant "
            "responses and appointment reminders — they belong on exactly "
            "this kind of delayed follow-up.",
            step=delayed[0].name or delayed[0].type, reach=len(delayed),
            cost="Texts at 3am. Opt-outs, complaints, and quiet-hours "
                 "exposure in every state with an 8pm cutoff — from a "
                 "follow-up that was supposed to be polite.")


@rule("GHL030", "Re-entry setting that does nothing", "medium", "routing",
      "settings")
def reentry_toggle_is_a_noop(acct: Account):
    """'Allow Re-entry' switched off where HighLevel documents it is ignored.

    The settings doc is explicit: workflows using an appointment- or
    invoice-based trigger ALWAYS allow a contact to enter multiple times. The
    toggle still renders, still saves, and does nothing — so the builder
    believes double-entry is impossible on exactly the workflows where it is
    guaranteed. Nobody reports this as a bug because nobody ever learns the
    setting was ignored; a static check is the only way it surfaces.
    """
    exempt = ("appointment", "invoice")
    for wf in acct.published():
        if not wf.triggers:
            continue
        if not all(any(k in t.type.lower() for k in exempt)
                   for t in wf.triggers):
            continue
        declared_off = any(
            wf.settings.get(k) is False
            for k in ("allowReentry", "allow_reentry", "allowMultiple",
                      "allow_multiple", "reentry", "re_entry"))
        if not declared_off:
            continue
        yield _finding(
            "GHL030", "medium", wf,
            "Re-entry is OFF, and on this trigger the toggle is ignored",
            "This workflow's triggers are appointment- or invoice-based, and "
            "HighLevel documents that those always allow multiple entry "
            "regardless of the Allow Re-entry setting. The OFF here is "
            "cosmetic: a contact whose appointment fires the trigger twice "
            "enters twice, and whoever built this believes the setting says "
            "otherwise.",
            "If one run per contact matters here, enforce it with a guard — "
            "an If/Else on a tag this workflow adds on entry — instead of the "
            "toggle. Then leave a note in the workflow name or description, "
            "because the next builder will trust the toggle too.",
            cost="Nothing until a trigger fires twice for one contact — then "
                 "they get the whole sequence twice, and the setting everyone "
                 "checked says it cannot happen.")


def _declared_from_number(step) -> str:
    """The hard-coded sending number on a step, if it carries one.

    Pool selections ("default number", "user's number") carry no literal
    number and return nothing here — they cannot be checked and must not be
    guessed about.
    """
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                nk = re.sub(r"[^a-z]", "", str(k).lower())
                if nk in ("fromnumber", "fromphone", "fromphonenumber",
                          "sendernumber", "sendfrom"):
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                found = walk(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = walk(v)
                if found:
                    return found
        return ""
    return walk(step.raw)


def _same_number(a: str, b: str) -> bool:
    """Digit-string equality, tolerant of one side carrying a country code.

    Comparison is on FULL digit strings only. Matching on the last ten digits
    would equate a UK mobile with a real US number that belongs to a stranger.
    """
    return bool(a) and bool(b) and (a == b or a == "1" + b or b == "1" + a)


@rule("GHL031", "SMS steps with no number that can send them", "critical",
      "deliverability", "sms")
def sms_without_a_sending_number(acct: Account):
    """The workflow sends SMS; the location has nothing to send it from.

    HighLevel's own guidance: SMS actions fail SILENTLY when the location has
    no phone number provisioned. The workflow runs, the contact appears in the
    logs, the step is skipped, and no error is raised anywhere. The same
    applies to a step whose hard-coded from-number was released, moved to
    another sub-account, or is voice-only — the vendor has a dedicated KB
    article for that error alone.
    """
    inv = acct.inventory
    senders = [wf for wf in acct.published() if wf.sms_steps]
    if not senders:
        return
    if not inv.has("phone_numbers"):
        yield Skip(
            rule="GHL031",
            title="SMS steps with no number that can send them",
            reason="The location's phone-number list was not supplied, so "
                   "whether these SMS steps have an SMS-capable number behind "
                   "them is unknown. The numbers live in the location's phone "
                   "settings, not in the workflows.",
            needs="phoneNumbers in the input bundle (number + sms capability)",
            category="deliverability")
        return
    if not inv.sms_capable_numbers:
        yield Finding(
            rule="GHL031", severity="critical", workflow="(account)",
            step=f"{len(senders)} workflows send SMS",
            category="deliverability",
            reach=sum(len(w.sms_steps) for w in senders),
            title="No SMS-capable number in the location — every SMS step "
                  "fails silently",
            symptom=f"{len(senders)} published workflow"
                    f"{'s' if len(senders) != 1 else ''} contain SMS steps and "
                    "this location has no SMS-capable phone number. HighLevel "
                    "documents exactly what happens: the workflow runs, the "
                    "contact shows in the logs, the SMS step is skipped, and "
                    "no error appears anywhere. Every text this account "
                    "believes it is sending, it is not.",
            cost="The entire SMS layer — speed-to-lead, reminders, follow-ups "
                 "— has been doing nothing. Every metric that assumes those "
                 "texts went out is fiction.",
            fix="Provision a number (LC Phone or Twilio) for this location "
                "and confirm it is SMS-capable and A2P-registered, then send "
                "one real test text before trusting any workflow again.")
        return

    known = [re.sub(r"\D", "", n["number"]) for n in inv.sms_capable_numbers]
    for wf in senders:
        for step in wf.sms_steps:
            declared = _declared_from_number(step)
            if not declared or "{{" in declared:
                continue
            digits = re.sub(r"\D", "", declared)
            if not digits:
                continue
            if any(_same_number(digits, k) for k in known):
                continue
            yield _finding(
                "GHL031", "high", wf,
                f"From-number {declared} is not an SMS-capable number in "
                "this location",
                "This step names its sending number explicitly, and that "
                "number is not in the location's SMS-capable list. That is "
                "the shape of a number that was released, moved to another "
                "sub-account, or provisioned voice-only — and HighLevel "
                "answers it with 'not a valid, SMS-capable phone number' "
                "and no delivered message.",
                "Point the step at one of the location's live SMS numbers, "
                "or clear the from-number so it uses the location default.",
                step=step.name or step.type,
                cost="Every text from this step fails. The sequence around "
                     "it keeps running, so the contact gets the day-3 email "
                     "referring to a text they never received.")


@rule("GHL032", "Opportunity created with no stage", "high", "routing", "data")
def opportunity_with_no_stage(acct: Account):
    """A Create Opportunity with a pipeline chosen and the stage left blank.

    HighLevel's action doc: "If left blank, it defaults to the first stage in
    the selected pipeline." So the opportunity a booking workflow creates
    files itself as a brand-new lead — and every stage-based automation,
    report and forecast downstream is quietly wrong about it. The first stage
    MAY be intended, which is why this finding asks for confirmation instead
    of declaring a defect.
    """
    inv = acct.inventory
    for wf in acct.published():
        for step in wf.steps_of("create_opportunity",
                                "internal_create_opportunity",
                                "add_opportunity"):
            cfg = step.config()
            pipe = stage = None
            for k, v in cfg.items():
                nk = re.sub(r"[^a-z]", "", str(k).lower())
                if nk in ("pipelineid", "pipeline"):
                    pipe = v
                elif nk in ("stageid", "pipelinestageid", "stage"):
                    stage = v
            if not pipe or (isinstance(pipe, str) and "{{" in pipe):
                continue
            if stage not in (None, "", [], {}):
                continue
            first = next((s["name"] for s in inv.stages.values()
                          if s.get("pipeline") == str(pipe) and s.get("name")),
                         None)
            lands = (f"its first stage, '{first}'" if first
                     else "its first stage")
            yield _finding(
                "GHL032", "high", wf,
                f"Stage left blank — every opportunity lands in {lands}",
                "This step picks a pipeline and no stage, and HighLevel "
                "defaults a blank stage to the first stage of the pipeline. "
                f"Every opportunity it creates lands in {lands}, whatever "
                "this workflow actually means — so a 'call booked' created "
                "here is indistinguishable from a lead nobody has touched, "
                "and every automation or report keyed on stage reads it "
                "wrong.",
                "Set the stage explicitly on this step. If the first stage "
                "genuinely is the intent, set it anyway — an explicit choice "
                "survives someone reordering the pipeline; the default does "
                "not.",
                step=step.name or step.type,
                cost="Pipeline reporting is silently wrong by every "
                     "opportunity this creates. Forecasts, stage automations "
                     "and the sales team's queue all inherit the error.")


CONFIRMATION_COPY = re.compile(
    r"thank(s| you)[^.!\n]{0,50}\b(purchase|order)\b|"
    r"\b(order|purchase|payment)\b[^.!\n]{0,30}\b(confirmed|complete[d]?|"
    r"received|successful)\b|your receipt|receipt (is )?(attached|below)",
    re.I)


@rule("GHL033", "Purchase confirmation on the pre-payment trigger", "medium",
      "routing", "triggers")
def thanks_before_payment(acct: Account):
    """"Thanks for your purchase" wired to Order Form SUBMITTED.

    Order Form Submitted fires on the form-submission event, earlier in
    checkout, and carries no product or price filters. Order Submitted fires
    only after successful order creation. Confirmation copy on the first one
    risks thanking people whose card declined — the vendor's docs do not state
    whether the submission event fires on a decline, so this is raised as a
    risk to confirm, not a certainty.
    """
    for wf in acct.published():
        if not any(t.canonical_type() == "order_form_submitted"
                   for t in wf.triggers):
            continue
        blob = wf.bodies() or wf.text()
        grants = [s for s in wf.steps
                  if any(k in s.type.lower() for k in ("membership", "grant",
                                                       "course_access"))]
        if not CONFIRMATION_COPY.search(blob) and not grants:
            continue
        yield _finding(
            "GHL033", "medium", wf,
            "Confirmation copy on the trigger that fires before payment "
            "settles",
            "This workflow triggers on Order Form Submitted — the "
            "form-submission event, which happens earlier in checkout than "
            "the order itself — and its content reads as a purchase "
            "confirmation"
            + (" (and it grants access)" if grants else "")
            + ". Order Submitted is the trigger that fires only after the "
              "order is actually created. Whether the submission event also "
              "fires when the card declines is not documented, which is the "
              "problem: a confirmation should not be built on a trigger "
              "whose relationship to payment is a maybe.",
            "Move the confirmation to the Order Submitted (or Payment "
            "Received) trigger. Keep Order Form Submitted for what it is "
            "good at — abandoned-checkout recovery for people who submitted "
            "the form and never completed the order.",
            cost="Best case, a premature thank-you. Worst case, declined "
                 "cards get the confirmation and the product access, and "
                 "you find out from the revenue report.")


PUBLIC_SHORTENERS = ("bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly",
                     "is.gd", "buff.ly", "rb.gy", "cutt.ly", "tiny.cc",
                     "shorturl.at", "t.ly", "s.id", "soo.gd", "v.gd",
                     "rebrand.ly", "short.io", "bl.ink")


@rule("GHL034", "Public link shortener in an SMS", "medium", "deliverability",
      "sms")
def shortener_in_sms(acct: Account):
    """bit.ly in a text message — a named driver of carrier filtering.

    HighLevel's own 30007 guidance lists public link shorteners alongside
    missing opt-out language as a top reason carriers filter messages, and
    shared shortener domains are likewise a documented A2P campaign rejection
    reason. The messages are accepted, then never delivered — which is the
    worst failure shape, because the sending report says everything went out.
    """
    for wf in acct.published():
        for step in wf.sms_steps:
            body = step.bodies() or step.text()
            hosts = []
            for url in URL.findall(body):
                m = re.match(r"https?://([^/:\s]+)", url, re.I)
                host = m.group(1).lower() if m else ""
                if any(host == s or host.endswith("." + s)
                       for s in PUBLIC_SHORTENERS) \
                        and not acct.config.owns_host(host):
                    hosts.append(host)
            if not hosts:
                continue
            yield _finding(
                "GHL034", "medium", wf,
                f"SMS contains a public shortener ({', '.join(sorted(set(hosts)))})",
                "Carriers cannot see where a shared shortener domain leads, "
                "so they filter on the domain's reputation — which is the "
                "pooled reputation of everyone who ever used it, spammers "
                "included. HighLevel names public shorteners as a top cause "
                "of error 30007: message accepted, never delivered, no "
                "bounce. The delivery report still shows it as sent.",
                "Use GoHighLevel's trigger links or a branded short domain "
                "instead. If click tracking is the point, trigger links also "
                "fire workflow events, which a bit.ly cannot.",
                step=step.name or step.type,
                cost="Some unknown share of these texts is silently "
                     "filtered. You pay for every send, the report says "
                     "delivered, and the leads who never got the link read "
                     "as 'unresponsive'.")


TEST_ENDPOINT = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|10\.\d+\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+)$|"
    r"(^|\.)webhook\.site$|(^|\.)requestbin\.com$|(^|\.)pipedream\.net$|"
    r"(^|\.)ngrok(-free)?\.(io|app|dev)$|"
    r"\.(test|local|localdomain|invalid)$",
    re.I)


@rule("GHL035", "Webhook aimed at a test endpoint", "high", "hygiene",
      "portability")
def webhook_to_nowhere(acct: Account):
    """An outbound webhook still pointing at the tool used to debug it.

    webhook.site, ngrok tunnels, localhost — the URLs that made sense on the
    day the integration was built and mean 'nowhere' in production. HighLevel
    skips a failed webhook silently and the workflow continues, so the lead
    data this step was supposed to deliver has simply not been arriving, with
    no error to say so. The check reads the URL only; it never posts to it —
    a live probe could fire a real side effect.
    """
    for wf in acct.published():
        for step in wf.steps_of("webhook", "http_request", "outbound_webhook"):
            for url in URL.findall(step.text()):
                m = re.match(r"(https?)://([^/:\s]+)", url, re.I)
                if not m:
                    continue
                scheme, host = m.group(1).lower(), m.group(2).lower()
                if TEST_ENDPOINT.search(host):
                    yield _finding(
                        "GHL035", "high", wf,
                        f"Webhook posts to {host} — a debugging endpoint",
                        "This URL is a test or tunnel host: the kind used to "
                        "inspect payloads while building, not to receive them "
                        "in production. Tunnels expire and inspection pages "
                        "discard, so whatever this integration was meant to "
                        "deliver — attribution, reporting, a CRM sync — has "
                        "not been arriving. HighLevel skips a failed webhook "
                        "and moves on, so nothing ever errored.",
                        "Point the webhook at the production endpoint (via a "
                        "custom value, so it survives cloning), and check "
                        "how long it has been posting into the void — that "
                        "is how much data needs re-sending.",
                        step=step.name or step.type,
                        cost="Every contact through this step since the URL "
                             "was left here is missing from whatever system "
                             "sits behind it. The gap is invisible until "
                             "someone reconciles the two ends.")
                elif scheme == "http":
                    yield _finding(
                        "GHL035", "medium", wf,
                        "Webhook posts contact data over plain http",
                        "This webhook sends its payload unencrypted. The "
                        "payload is contact data — names, phones, emails, "
                        "answers — and an http endpoint is also the shape of "
                        "a URL typed quickly during a build and never "
                        "revisited. Many receivers now refuse http outright, "
                        "and HighLevel would skip that failure silently.",
                        "Switch the endpoint to https. If the receiver "
                        "genuinely has no TLS, that is the thing to fix.",
                        step=step.name or step.type,
                        cost="Customer data crosses the network readable by "
                             "anyone on the path — a compliance finding in "
                             "any audit, for the cost of one letter.")


@rule("GHL036", "Deprecated appointment trigger", "medium", "routing",
      "triggers")
def deprecated_booking_trigger(acct: Account):
    """"Customer Booked Appointment" — fires for self-bookings only.

    The vendor documents two problems in one trigger: it only fires when the
    customer books through a widget or link (an appointment a team member
    books by hand never enters the workflow), and it is being deprecated in
    favour of Appointment Status with Modified By = Customer. Confirmations
    and reminders built on it silently skip every manually-booked client.
    """
    for wf in acct.published():
        for t in wf.triggers:
            if "customerbooked" not in re.sub(r"[^a-z]", "", t.type.lower()):
                continue
            yield _finding(
                "GHL036", "medium", wf,
                "Trigger only fires for self-booked appointments, and is "
                "being retired",
                "'Customer Booked Appointment' fires only when the contact "
                "books through a calendar widget or booking link. Anyone a "
                "team member books by hand never enters this workflow — no "
                "confirmation, no reminders — and staff-booked clients are "
                "exactly the ones nobody double-checks. HighLevel is also "
                "deprecating this trigger, so the gap comes with a deadline.",
                "Rebuild on Appointment Status (status: confirmed, Modified "
                "By: Customer to keep the old behaviour — drop the Modified "
                "By filter to cover staff bookings too, which is usually "
                "what was wanted all along).",
                step=t.name or t.type,
                cost="Every manually-booked appointment runs without "
                     "confirmations or reminders. Those no-shows get "
                     "blamed on the client.")


@rule("GHL037", "Finished build sitting in draft", "medium", "dead_weight",
      "hygiene")
def draft_with_a_live_trigger(acct: Account):
    """Built, saved, never published — so it has never run.

    Saved and published are independent states in GoHighLevel, snapshots
    deploy their workflows in draft, and 'the workflow was never published'
    is the first item in every troubleshooting guide the vendor and its
    operators publish. A draft carrying a configured trigger and real steps
    is the shape of a build someone finished and forgot to turn on — flagged
    for confirmation, because an intentional work-in-progress looks the same.
    """
    for wf in acct.workflows:
        if wf.published:
            continue
        if not wf.triggers or not wf.steps:
            continue
        if re.search(r"\b(sandbox|test|probe|wip|draft|old|archived?|backup|"
                     r"untitled|deprecated|copy of|do.?not.?use|zz)\b",
                     wf.name, re.I):
            continue
        sends = len(wf.outbound)
        yield _finding(
            "GHL037", "medium" if sends else "low", wf,
            "Complete workflow, configured trigger — never published",
            "This workflow has a trigger and "
            f"{len(wf.steps)} step{'s' if len(wf.steps) != 1 else ''}"
            + (f" including {sends} outbound send"
               f"{'s' if sends != 1 else ''}" if sends else "")
            + ", and it is in draft, so none of it has ever run. Saved is "
              "not published — they are separate states, and snapshots "
              "deploy in draft — which makes a finished-looking draft the "
              "single most common reason 'the automation never fired'. If "
              "it is genuinely still being built, name it so ('WIP -') and "
              "this check will leave it alone.",
            "Publish it if it is finished. If it is abandoned, delete it or "
            "mark the name, so the next person does not have to work out "
            "which it is.",
            cost="Everything this workflow was built to do has never "
                 "happened. If something else was supposed to depend on it, "
                 "that has been failing quietly too.")


def _nk(key) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


WINDOW_KEYS = {"window", "sendingwindow", "sendwindow", "advancewindow",
               "advancewindowsettings", "timewindow"}


def _step_window(step) -> bool:
    cfg = step.config()
    if not isinstance(cfg, dict):
        return False
    return any(_nk(k) in WINDOW_KEYS and v not in (None, "", {}, [], False)
               for k, v in cfg.items())


@rule("GHL038", "Stacked windowed waits drift the whole sequence", "low",
      "routing", "timing")
def compounding_window_drift(acct: Account):
    """Three or more consecutive waits, each carrying its own send window.

    Every windowed wait that lands outside its window rolls forward to the
    next opening — usually the next morning. Stack three of them and the
    drift compounds: a sequence written as five days runs in eight, and every
    touch lands at window-open, exactly when everyone else's does. The
    operator guidance is one line: apply windows to the steps that send, not
    to structural waits. Advisory by design — short windows add little and
    the pacing may be intended.
    """
    for wf in acct.published():
        streak: list = []
        for i, step in enumerate(wf.steps):
            if step.is_wait and _step_window(step):
                streak.append((i, step))
                continue
            # Anything else — a send, a branch, even a plain wait — breaks the
            # streak. Only strictly consecutive windowed waits are flagged;
            # the field guidance this encodes is written about exactly that
            # stack, and an advisory rule earns its keep by underclaiming.
            if len(streak) >= 3:
                yield _drift_finding(wf, streak)
            streak = []
        if len(streak) >= 3:
            yield _drift_finding(wf, streak)


def _drift_finding(wf, streak):
    last_i = streak[-1][0]
    below = len(wf.outbound_after(last_i))
    names = ", ".join(s.name or s.type for _, s in streak)
    return _finding(
        "GHL038", "low", wf,
        f"{len(streak)} windowed waits in a row — each boundary can add "
        "a day",
        "Each of these waits carries its own send window, and every time one "
        "of them ends outside its window the resume rolls forward to the next "
        "opening. Stacked, the drift compounds: the sequence takes days "
        "longer than it was written to, and the touches bunch at "
        "window-open alongside everything else the account sends. If the "
        "pacing is deliberate, this costs nothing — that judgement is the "
        "reader's, which is why this is advisory.",
        "Put the window on the steps that send, not on structural waits. One "
        "window on the SMS step gives the same quiet hours without "
        "compounding the delay.",
        step=names, reach=below,
        cost="Later touches in this sequence land days later than designed — "
             "against a lead who is going cold on someone else's schedule, "
             "not yours.")


OPP_CREATE_TYPES = ("create_opportunity", "internal_create_opportunity",
                    "add_opportunity")
OPP_WRITE_TYPES = OPP_CREATE_TYPES + ("update_opportunity",
                                      "internal_update_opportunity",
                                      "update_opportunity_status")


def _pipe_stage(step):
    """(pipeline_id, stage_id) off an opportunity step, merge fields excluded."""
    pipe = stage = None
    cfg = step.config()
    for k, v in (cfg.items() if isinstance(cfg, dict) else ()):
        if not isinstance(v, str) or "{{" in v or not v:
            continue
        nk = _nk(k)
        if nk in ("pipelineid", "pipeline"):
            pipe = v
        elif nk in ("stageid", "pipelinestageid", "stage"):
            stage = v
    return pipe, stage


@rule("GHL039", "Several workflows create opportunities on one pipeline",
      "medium", "routing", "data")
def multiple_opportunity_writers(acct: Account):
    """Two or more live workflows each running Create Opportunity on the same
    pipeline — the headline cause in every duplicate-opportunity teardown.

    One on the form workflow, another on the appointment workflow, a third
    shipped inside a snapshot: each is correct alone, and together one
    contact who submits and then books gets two opportunities. Pipeline
    counts, conversion rates and forecasts all double-count from then on.
    Raised as a possible duplicate source, never a confirmed one — mutually
    exclusive entry filters can make two writers legitimate, and that is not
    decidable from configuration.
    """
    writers: dict = {}
    for wf in acct.published():
        for step in wf.steps_of(*OPP_CREATE_TYPES):
            pipe, _ = _pipe_stage(step)
            if pipe:
                writers.setdefault(pipe, {})[wf.name] = wf
    inv = acct.inventory
    for pipe in sorted(writers):
        wfs = [writers[pipe][n] for n in sorted(writers[pipe])]
        if len(wfs) < 2:
            continue
        label = inv.pipelines.get(str(pipe)) or pipe
        reenters = [w.name for w in wfs if _allows_reentry(w)]
        names = ", ".join(w.name for w in wfs)
        yield Finding(
            rule="GHL039", severity="high" if reenters else "medium",
            workflow=wfs[0].name, step=", ".join(w.name for w in wfs[1:]),
            category="routing",
            reach=sum(len(w.outbound) for w in wfs),
            title=f"{len(wfs)} workflows each create an opportunity on "
                  f"'{label}'",
            symptom=f"Each of these creates its own opportunity on the same "
                    f"pipeline: {names}. A contact who passes through more "
                    "than one of them — submits a form, then books a call — "
                    "gets one opportunity per workflow, and every pipeline "
                    "count, conversion rate and forecast double-counts from "
                    "then on."
                    + (" Re-enrollment is on for: " + ", ".join(reenters)
                       + " — so one contact can mint a new opportunity on "
                         "every lap." if reenters else "")
                    + " If the entry conditions are genuinely mutually "
                      "exclusive this is fine — this is raised as a possible "
                      "duplicate source to confirm, not a verdict.",
            fix="Pick one workflow to own opportunity creation and have the "
                "others update the existing record instead (or gate creation "
                "behind an 'opportunity exists' check). If several must "
                "create, make their entry filters provably exclusive.",
            cost="Pipeline reporting double-counts every contact who touches "
                 "two of these paths. The sales team works the same person "
                 "as two deals, and the forecast is quietly inflated.")


STAGE_TRIGGER_TYPES = ("opportunity_status", "opportunity_stage")


def _trigger_stages(trigger) -> set:
    """Stage ids/names this trigger listens for, lowercased."""
    out = set()
    for f in trigger.filters():
        if not isinstance(f, dict):
            continue
        field_names_stage = any(
            _nk(k) in ("field", "key", "property", "name", "attribute")
            and "stage" in str(v).lower() for k, v in f.items())
        for k, v in f.items():
            nk = _nk(k)
            values = v if isinstance(v, (list, tuple)) else [v]
            if "stage" in nk or (field_names_stage and
                                 nk in ("value", "values", "val")):
                out.update(str(x).strip().lower() for x in values
                           if isinstance(x, str) and x.strip())
    return out


@rule("GHL040", "Workflows re-trigger each other through pipeline stages",
      "medium", "routing", "triggers", "data")
def stage_write_cycle(acct: Account):
    """The pipeline-stage analogue of the tag loop (GHL014).

    Workflow A moves the opportunity to a stage that enrolls workflow B; B
    moves it to a stage that enrolls A. Each is correct alone. Together they
    loop — or bounce a contact out of a sequence that was still mid-flight.
    Nothing in the builder shows the pair. Reported as a possible conflict:
    filters or one-shot guards the analysis cannot see may break the loop in
    practice.
    """
    pubs = list(acct.published())
    by_id = {w.id: w for w in pubs}
    listeners: dict = {}
    for w in pubs:
        for t in w.triggers:
            if t.canonical_type() in STAGE_TRIGGER_TYPES:
                for stage in _trigger_stages(t):
                    listeners.setdefault(stage, []).append(w.id)
    edges: dict = {}
    for w in pubs:
        for step in w.steps_of(*OPP_WRITE_TYPES):
            _, stage = _pipe_stage(step)
            if not stage:
                continue
            for other in listeners.get(stage.strip().lower(), []):
                edges.setdefault(w.id, set()).add(other)

    reported = set()

    def cycles_from(start, node, path):
        for nxt in sorted(edges.get(node, ())):
            if nxt == start:
                yield path[:]
            elif nxt not in path:
                yield from cycles_from(start, nxt, path + [nxt])

    for start in sorted(edges):
        for cycle in cycles_from(start, start, [start]):
            key = frozenset(cycle)
            if key in reported:
                continue
            reported.add(key)
            wfs = [by_id[i] for i in cycle]
            reenters = any(_allows_reentry(w) for w in wfs)
            if len(wfs) == 1:
                title = (f"'{wfs[0].name}' writes the stage that "
                         "triggers itself")
            else:
                title = ("Stage loop: "
                         + " <-> ".join(w.name for w in wfs))
            yield Finding(
                rule="GHL040", severity="high" if reenters else "medium",
                workflow=wfs[0].name,
                step=" -> ".join(w.name for w in wfs), category="routing",
                reach=sum(len(w.outbound) for w in wfs),
                title=title,
                symptom="Each workflow in this chain moves the opportunity "
                        "to a stage that enrolls the next one, and the chain "
                        "closes on itself. "
                        + ("Re-enrollment is on inside the loop, so one "
                           "opportunity can cycle through it repeatedly — "
                           "stage history, alerts and any messaging along "
                           "the way included."
                           if reenters else
                           "Re-enrollment is off, which caps it at one lap "
                           "today — but each lap still yanks the contact "
                           "between sequences, and the first person to allow "
                           "re-entry makes it spin.")
                        + " Filters this analysis cannot evaluate may break "
                          "the loop in practice, so treat this as a possible "
                          "conflict to walk through, not a verdict.",
                fix="Decide which workflow owns each stage transition. Break "
                    "the cycle at its weakest link: narrow one trigger, or "
                    "drop the stage write that closes the loop.",
                cost="Opportunities ping-pong between stages, so stage "
                     "history and conversion timing are fiction — and any "
                     "messages hanging off these stages re-send on every "
                     "lap.")


# --------------------------------------------------------------------------
# GHL041+ — reliability engineering: the patterns the turnaround buyer is
# actually paying for. Each rule encodes one named pattern from the reliability
# pattern library (idempotency, bounded retries, DLQ, heartbeats, AI gating),
# checked mechanically against the export. The buyer's own words: "edge cases
# that were not handled, API failures, data synchronization issues and a lack
# of proper monitoring and error recovery."
# --------------------------------------------------------------------------

WEBHOOK_CALL_TYPES = ("webhook", "http_request", "outbound_webhook",
                      "custom_webhook")

SAVE_RESPONSE_KEYS = {"saveresponse", "savewebhookresponse",
                      "saveresponsedata", "storeresponse", "captureresponse"}


@rule("GHL041", "External call whose failure is invisible", "high", "routing",
      "reliability", "webhooks")
def external_call_without_a_failure_path(acct: Account):
    """A webhook/API call with no error branch wired.

    HighLevel documents that a failed Custom Webhook is skipped and the
    contact continues down the workflow — the retry behaviour of the workflow
    action is unspecified, so the safe design assumption is that a failure is
    silently skipped. The senior build never assumes the call succeeded: it
    enables "Save response from this Webhook" and branches on the response
    with an If/Else, routing no-response to the error path. Without that, the
    failure path is a crash the platform swallows, not a route.
    """
    for wf in acct.published():
        for i, step in enumerate(wf.steps):
            if step.type not in WEBHOOK_CALL_TYPES:
                continue
            cfg = step.config()
            saves = any(_nk(k) in SAVE_RESPONSE_KEYS
                        and v not in (None, "", False, 0)
                        for k, v in (cfg.items() if isinstance(cfg, dict)
                                     else ()))
            branches_after = any(s.is_branch for s in wf.steps[i + 1:])
            if saves and branches_after:
                continue
            if saves:
                yield _finding(
                    "GHL041", "medium", wf,
                    "Webhook response is captured, and nothing reads it",
                    "This call saves its response, but no If/Else after it "
                    "ever branches on what came back. A failed call is "
                    "recorded and then ignored: the contact continues down "
                    "the workflow as if the integration succeeded, and the "
                    "captured error sits unread in the execution log.",
                    "Add an If/Else after the call that checks the saved "
                    "response. No response captured = treat as failure and "
                    "route to an error path — a tag like 'err:sync-failed' "
                    "plus a notification is enough to make failures visible.",
                    step=step.name or step.type,
                    cost="Failures are logged and nobody is told. The "
                         "integration can be down for weeks while every "
                         "contact sails past the broken step.")
                continue
            yield _finding(
                "GHL041", "high", wf,
                "External call with no error branch — a failure is silently "
                "skipped",
                "HighLevel skips a failed webhook action and continues the "
                "workflow, and this step neither saves its response nor has "
                "any branch downstream that could notice a failure. Every "
                "record this call was supposed to deliver on a bad day — an "
                "outage, a rate limit, an expired token — is simply lost, "
                "and nothing anywhere reports it. This is the exact shape of "
                "'leads just never showed up in the other system'.",
                "Enable 'Save response from this Webhook' on the step, then "
                "branch on the response with an If/Else: success continues, "
                "no-response routes to an error path (tag the contact "
                "'err:sync-failed', set a last_error field, notify a human). "
                "The failure path must be a route, not a crash.",
                step=step.name or step.type,
                cost="Every contact that hits this step while the endpoint "
                     "is down vanishes from the downstream system. The gap "
                     "is invisible until someone reconciles the two ends, "
                     "months of leads later.")


RETRY_ON_KEYS = {"retryonfail", "retryenabled", "retriesenabled"}
CONTINUE_KEYS = {"onerror", "onfail", "errorbehavior"}


@rule("GHL042", "Retries enabled and silently disabled", "high", "routing",
      "reliability", "retries")
def retry_silently_disabled(acct: Account):
    """Retry On Fail enabled while On Error is set to a Continue option.

    n8n's documented behaviour: if a node enables Retry On Fail AND sets On
    Error to one of the Continue options, Max Tries and Wait Between Tries
    are ignored — the node continues on the FIRST failure instead of
    retrying. So 'I turned on retries' and 'retries actually happen' are
    different states, and lots of production workflows are quietly in the
    first one. It looks configured and is not, which is what makes it
    high-yield in an audit. The check reads the step's own declared settings,
    so it fires only on exports that carry them (n8n and n8n-style bundles);
    a GoHighLevel-native step declares neither key and stays out of it.
    """
    for wf in acct.workflows:
        for step in wf.steps:
            cfg = step.config()
            if not isinstance(cfg, dict):
                continue
            retry_on = any(
                _nk(k) in RETRY_ON_KEYS and (v is True or
                                             str(v).strip().lower() == "true")
                for k, v in cfg.items())
            if not retry_on:
                continue
            continues = any(
                (_nk(k) in CONTINUE_KEYS and "continue" in str(v).lower()) or
                (_nk(k) == "continueonfail" and (v is True or
                                                 str(v).strip().lower() == "true"))
                for k, v in cfg.items())
            if not continues:
                continue
            yield _finding(
                "GHL042", "high", wf,
                "Retry On Fail is on, and the Continue setting disables it",
                "This step enables Retry On Fail and also sets its on-error "
                "behaviour to a Continue option. n8n documents what that "
                "combination does: Max Tries and Wait Between Tries are "
                "ignored, and the step continues after the FIRST failure "
                "instead of retrying. The retry policy someone deliberately "
                "configured here has never once run — a transient blip or a "
                "rate limit that one retry would have absorbed goes straight "
                "down the error path, or worse, straight through.",
                "Decide which behaviour this step actually wants. For real "
                "retries, set On Error to 'Stop Workflow' (or catch the "
                "error output AFTER the retries are exhausted). For "
                "continue-on-error, remove the retry setting so the config "
                "stops claiming a resilience it does not have.",
                step=step.name or step.type,
                cost="Every transient failure is treated as final. Records "
                     "that one retry would have saved are lost or sent down "
                     "the error path, while the config says they were "
                     "retried.")


def _looks_like_n8n(wf: Workflow) -> bool:
    """Does this workflow carry n8n's export shape?

    n8n node types are namespaced ('n8n-nodes-base.httpRequest') and each
    node carries a typeVersion. A GoHighLevel workflow has neither, which is
    what keeps an n8n-only check from firing on every GHL export it reads.
    """
    return any("n8n-nodes" in s.type
               or "typeversion" in {_nk(k) for k in s.raw}
               for s in wf.steps)


@rule("GHL043", "n8n workflow with no error workflow attached", "medium",
      "routing", "reliability", "n8n")
def n8n_without_error_workflow(acct: Account):
    """An n8n workflow whose failures go nowhere.

    n8n's error workflow — configured per workflow in Options -> Settings ->
    Error workflow — is the platform's native failure sink: every failed
    execution lands there with the workflow name, the failing node, the error
    message and a clickable link to the failed run. One error workflow can be
    reused across many workflows; that reuse is the whole point. A workflow
    without one fails into the executions list, which nobody reads until a
    client complains. Scoped to workflows that are identifiably n8n exports —
    a GoHighLevel workflow has no such setting to check.
    """
    for wf in acct.workflows:
        if not _looks_like_n8n(wf):
            continue
        attached = any("errorworkflow" in _nk(k) and str(v).strip()
                       for k, v in wf.settings.items())
        if attached:
            continue
        yield _finding(
            "GHL043", "medium", wf,
            "No error workflow set — failures land in a list nobody reads",
            "This n8n workflow has no error workflow attached. When an "
            "execution fails, the failure is recorded in the executions "
            "list and nothing else happens: no alert, no log row, no "
            "notification. The default state of an account like this is "
            "that things have been broken for weeks and nobody knows — "
            "monitoring is the layer that converts silent failures into "
            "alerts, and this workflow does not have it.",
            "Create one shared error workflow (Error Trigger -> append to a "
            "failed-events table -> alert) and attach it to every workflow "
            "in Options -> Settings -> Error workflow. The error payload "
            "carries the failing node and a direct link to the execution, "
            "so one listener covers the whole instance.",
            cost="Failures are silent. The first person to find out an "
                 "automation broke is the client, weeks later, asking where "
                 "their leads went.")


CONTACT_CREATE_TYPES = ("create_contact", "add_contact",
                        "internal_create_contact")


@rule("GHL044", "Contact created where an upsert belonged", "medium",
      "routing", "reliability", "data")
def create_where_upsert_belonged(acct: Account):
    """A blind Create Contact — the classic duplicate-contact source.

    The reliable pattern is to upsert against a stable identifier, never to
    branch into separate create/update paths: a replayed webhook or a second
    form fill then updates the existing record instead of minting a twin.
    Duplicate contacts are the #1 visible symptom of a broken CRM automation
    and the thing the client complains about first. Whether a GHL create is
    deduplicated depends on the Location-level 'Allow Duplicate Contacts'
    setting — configuration this export does not carry — so this is raised
    as a duplicate risk to confirm, not a verdict.
    """
    for wf in acct.published():
        for step in wf.steps_of(*CONTACT_CREATE_TYPES):
            yield _finding(
                "GHL044", "medium", wf,
                "Create Contact used where an upsert would dedupe",
                "This step creates a contact outright. Whether that mints a "
                "duplicate depends on the sub-account's 'Allow Duplicate "
                "Contacts' setting (Settings -> Business Profile -> Contact "
                "Preferences) — with duplicates allowed, a replayed webhook, "
                "a double-submitted form or the same lead arriving from two "
                "sources creates a second record, and every conversation, "
                "tag and opportunity splits across the twins. Webhooks are "
                "at-least-once: duplicates are normal, not exceptional, and "
                "a create is the one write that is never safe to run twice.",
                "Use an upsert keyed on email or phone instead (the Upsert "
                "Contact API follows the location's dedupe fields), and "
                "confirm 'Allow Duplicate Contacts' is off unless it is "
                "deliberately on. Where the record comes from an outside "
                "system, store that system's ID in a custom field and match "
                "on it first — it survives an email change; email-matching "
                "alone does not.",
                step=step.name or step.type,
                cost="Duplicate contacts — the defect the client sees first "
                     "and judges the whole build by. Two records for one "
                     "person means two sequences, split history, and a rep "
                     "working the wrong twin.")


DEDUPE_HINT = re.compile(
    r"event[_ -]?id|webhook[_ -]?id|message[_ -]?id|delivery[_ -]?id|"
    r"last[_ -]?event|dedup|duplicate|idempoten|already[_ -]?processed", re.I)

SIDE_EFFECT_TYPES = set(OPP_CREATE_TYPES) | set(CONTACT_CREATE_TYPES) | \
    set(WEBHOOK_CALL_TYPES)


@rule("GHL045", "Inbound webhook processed with no dedupe check", "high",
      "routing", "reliability", "webhooks")
def inbound_webhook_without_dedupe(acct: Account):
    """An inbound-webhook workflow whose first act is a side effect.

    Webhook delivery is at-least-once — the sender retries on any timeout,
    so duplicates are normal, not exceptional. GHL's own integrator guidance
    says to store webhook IDs to prevent duplicate processing and make the
    processing idempotent. The Inbound Webhook trigger has no built-in
    dedupe, so the guard has to be the workflow's first action: an If/Else
    comparing the inbound event ID against a stored last_event_id, exiting
    on a match. Without it, every redelivered event runs the whole workflow
    again — two contacts, two opportunities, two SMS.
    """
    for wf in acct.published():
        if not any("inbound" in t.type.lower() and "webhook" in t.type.lower()
                   for t in wf.triggers):
            continue
        effect_at = next(
            (i for i, s in enumerate(wf.steps)
             if s.is_outbound or s.type in SIDE_EFFECT_TYPES
             or s.tags_added()), None)
        if effect_at is None:
            continue
        guarded = any(
            s.is_branch and DEDUPE_HINT.search(s.name + " " + s.text())
            for s in wf.steps[:effect_at])
        if guarded:
            continue
        yield _finding(
            "GHL045", "high", wf,
            "Inbound webhook runs its side effects with no duplicate guard",
            "This workflow triggers on an inbound webhook and its first "
            "side effect runs with nothing checking whether the same event "
            "was already processed. Webhook delivery is at-least-once: any "
            "slow response makes the sender retry, and the retry carries "
            "the same event. Each redelivery runs this workflow again — "
            "duplicate records, duplicate messages, duplicate everything — "
            "and HighLevel's own integration guidance is explicit that "
            "duplicates are expected behaviour to be handled, not a bug.",
            "Store the sender's event ID in a contact field (last_event_id) "
            "and make the workflow's first step an If/Else: inbound ID "
            "equals the stored one, exit; otherwise write it and continue. "
            "Re-entry settings guard 'same contact, same funnel' — they do "
            "not guard 'same event delivered twice', so this check is "
            "needed even with re-entry off.",
            cost="Every webhook retry doubles the work: two contacts, two "
                 "opportunities, or the same SMS twice back to back. The "
                 "sender's retries are routine, so this fires in normal "
                 "operation, not just on a bad day.")


ATTEMPT_HINT = re.compile(
    r"attempt|retry[_ -]?count|retry[_ -]?number|tries|loop[_ -]?count|"
    r"max[_ -]?retries", re.I)

# A goto/jump construct IS the whole step type: nothing follows the "to"
# except an optional structural suffix. A substring match on "goto" tripped
# on n8n's `n8n-nodes-base.goToWebinar` — a product name, not a loop — so
# the leaf of the (possibly namespaced) type must match exactly.
GOTO_TYPE = re.compile(
    r"^go[\-_ ]?to(?:[\-_ ]?(?:step|action|node|event|workflow))?$", re.I)


def _is_goto(step_type: str) -> bool:
    leaf = re.split(r"[./:]", str(step_type or ""))[-1].strip()
    return bool(GOTO_TYPE.match(leaf))


@rule("GHL046", "Retry loop with no attempt counter", "high", "routing",
      "reliability", "loops")
def retry_loop_without_a_bound(acct: Account):
    """A Go-To loop with nothing counting the laps.

    The GHL retry ladder is built with Wait + Go-To — and the poison-message
    guard on it is an attempt_count field, incremented each lap, routing the
    contact out to a dead-letter path at three. Without that counter, a
    contact whose record can never succeed (a malformed phone number, a
    payload the endpoint always rejects) laps the loop forever: bounded
    retries are the difference between a retry policy and a runaway. The
    same rule everywhere: limit total attempts, or one failing dependency
    consumes the system.
    """
    for wf in acct.published():
        gotos = [s for s in wf.steps if _is_goto(s.type)]
        if not gotos:
            continue
        blob = wf.text() + " " + " ".join(s.name for s in wf.steps)
        if ATTEMPT_HINT.search(blob):
            continue
        yield _finding(
            "GHL046", "high", wf,
            "Go-To loop with nothing counting the attempts",
            "This workflow jumps back with a Go-To and nothing in it tracks "
            "how many laps a contact has taken. A contact who can never "
            "succeed — a malformed number, a record the endpoint always "
            "rejects — is a poison message: it loops forever, re-running "
            "every step inside the loop on each lap, and no error is ever "
            "raised because each individual lap looks like normal "
            "execution.",
            "Add an attempt_count custom field: set it from a computed "
            "value on each lap (not a blind increment, so a replay cannot "
            "corrupt it), and branch before the Go-To — at 3 attempts, "
            "route the contact out of the loop to a failure path (tag, "
            "notify, log) instead of around again.",
            step=gotos[0].name or gotos[0].type,
            cost="One bad record can loop indefinitely — burning sends, "
                 "API calls and alert noise on every lap, forever, for a "
                 "contact that was never going to succeed.")


FIELD_WRITE_TYPE = re.compile(
    r"update[_ -]?(contact[_ -]?)?field|update[_ -]?custom[_ -]?field|"
    r"set[_ -]?(contact[_ -]?)?field|update[_ -]?contact$|edit[_ -]?field",
    re.I)
FIELD_KEY_NAMES = {"field", "fieldkey", "fieldid", "customfield",
                   "customfieldid", "fieldname", "targetfield"}


def _fields_written(step: Step) -> set[str]:
    """The contact-field keys this step writes, merge tokens excluded."""
    out: set[str] = set()
    cfg = step.config()
    if not isinstance(cfg, dict):
        return out
    for k, v in cfg.items():
        if _nk(k) in FIELD_KEY_NAMES and isinstance(v, str) \
                and v.strip() and "{{" not in v:
            out.add(slug(v))
        elif _nk(k) == "fields" and isinstance(v, dict):
            out.update(slug(str(fk)) for fk in v if str(fk).strip())
    return out


@rule("GHL047", "Several workflows write the same contact field", "medium",
      "routing", "reliability", "data")
def multiple_field_writers(acct: Account):
    """Two live workflows both writing one field — the classic GHL race.

    Two workflows both allowed to update the same field, triggered by
    near-simultaneous events, and the value ends up wrong some fraction of
    the time — invisible until a report built on that field is wrong. The
    fix is field-level ownership: one workflow owns each field, everything
    else requests changes through it. Raised as a possible race, never a
    confirmed one — mutually exclusive triggers can make two writers
    legitimate, and that is not decidable from configuration.
    """
    writers: dict = {}
    for wf in acct.published():
        for step in wf.steps:
            if not FIELD_WRITE_TYPE.search(step.type):
                continue
            for field_key in _fields_written(step):
                writers.setdefault(field_key, {})[wf.name] = wf
    for field_key in sorted(writers):
        wfs = [writers[field_key][n] for n in sorted(writers[field_key])]
        if len(wfs) < 2:
            continue
        names = ", ".join(w.name for w in wfs)
        yield Finding(
            rule="GHL047", severity="medium", workflow=wfs[0].name,
            step=", ".join(w.name for w in wfs[1:]), category="routing",
            reach=sum(len(w.outbound) for w in wfs),
            title=f"{len(wfs)} workflows each write the field "
                  f"'{field_key}'",
            symptom=f"Each of these updates the same contact field: {names}. "
                    "When two of them fire close together — a form submit "
                    "and a booking landing in the same minute — the last "
                    "write wins and the field ends up wrong some fraction "
                    "of the time, with nothing logging that it happened. "
                    "Every branch, report and automation keyed on this "
                    "field inherits the error. If the triggers are "
                    "genuinely mutually exclusive this is fine — it is "
                    "raised as a possible race to confirm, not a verdict.",
            fix="Give the field one owner: a single workflow performs every "
                "write, and the others request the change (a tag the owner "
                "listens for) instead of writing directly. One writer per "
                "field turns a race into a queue.",
            cost="The field is intermittently wrong, which is worse than "
                 "always wrong — it passes every spot check and corrupts "
                 "the occasional record where two events raced.")


SCHEDULE_TRIGGER = ("schedule", "cron", "recurring")


@rule("GHL048", "Scheduled workflow with no heartbeat", "medium", "hygiene",
      "reliability", "monitoring")
def scheduled_without_heartbeat(acct: Account):
    """A scheduled run that nothing would miss.

    The failure that beats all error monitoring is the workflow that did not
    run at all: a trigger that stops firing produces zero errors, zero
    failed runs and zero alerts. The guard is a dead-man's switch — every
    scheduled workflow pings a monitor URL on each successful completion,
    and the MONITOR alerts when the ping stops arriving. A scheduled
    workflow with no outbound call anywhere cannot be pinging anything, so
    its silence is undetectable by design. A workflow that does carry a
    webhook call may already be heartbeating — whether that call is a
    monitor is not knowable from the export, so those stay unflagged.
    """
    for wf in acct.published():
        if not any(any(k in t.type.lower() for k in SCHEDULE_TRIGGER)
                   for t in wf.triggers):
            continue
        if wf.steps_of(*WEBHOOK_CALL_TYPES):
            continue
        yield _finding(
            "GHL048", "medium", wf,
            "If this schedule stops running, nothing will ever say so",
            "This workflow runs on a schedule and contains no outbound "
            "call that could ping a monitor. A schedule that silently "
            "stops — the trigger deleted, the workflow unpublished by "
            "accident, the platform skipping it — produces no errors and "
            "no failed executions, so error alerting cannot see it. "
            "Whatever this run maintains (a sweep, a sync, a report) "
            "degrades quietly from the day it stops, and the absence only "
            "surfaces when someone notices stale output downstream.",
            "Add a final webhook step that hits a heartbeat/cron-monitor "
            "URL on every successful run, and have the monitor alert when "
            "a ping misses its window. 'Did it run?' monitoring is "
            "separate from 'did it error?' monitoring, and this is the "
            "cheap end of it.",
            cost="This workflow can be dead for weeks with zero errors "
                 "logged. Whatever it was maintaining rots silently until "
                 "a human notices the output went stale.")


AI_STEP_TYPE = re.compile(
    r"\bai\b|(^|[_-])ai($|[_-])|chatgpt|openai|\bgpt\b|gpt[_-]|claude|"
    r"\bllm\b|anthropic", re.I)

# The keys a step carries when it really does call a model. A name is only ever
# a hint; one of these is the corroboration.
MODEL_CALL_KEYS = {"prompt", "prompts", "systemprompt", "systemmessage",
                   "userprompt", "usermessage", "prompttemplate",
                   "instruction", "instructions", "messages", "model",
                   "temperature", "maxtokens", "agent", "agentid", "botid"}


def _keys_under(node) -> list:
    """Every key name in the structure, at any depth."""
    out: list = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                out.append(str(k))
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out


def _is_send(step: Step) -> bool:
    """Does this step put a message in front of the contact?

    `Step.is_outbound` is the model's list of send types and it does not carry
    `mms` or `send_email`, both of which appear in real exports.
    """
    return step.is_outbound or step.is_sms or step.is_email


def _is_ai_step(step: Step) -> bool:
    """A step that calls a model.

    A matching TYPE is proof — `chatgpt`, `conversation_ai`, `ai_extract` are
    action types, not prose. A matching NAME is only a hint and has to be backed
    by the step carrying a prompt or a model setting, because builders name
    ordinary steps after the thing they route on: "Route by AI score" is an
    If/Else, "Tag as ai-hot" is a tag step, and reading either as a model call
    reports a workflow as having AI steps it does not have.

    A send is never one, however it is named. "Send the AI draft" is an SMS step
    that consumes model output; reading it as the producer would let the send
    satisfy its own guard.
    """
    if _is_send(step):
        return False
    if AI_STEP_TYPE.search(step.type):
        return True
    if not AI_STEP_TYPE.search(step.name):
        return False
    return any(_nk(k) in MODEL_CALL_KEYS for k in _keys_under(step.raw))


ENUM_KEYS = {"enum", "options", "choices", "categories", "allowedvalues",
             "allowed", "labels", "buckets", "intents"}


def _declares_enum(step: Step) -> bool:
    """Does this AI step constrain its output to a fixed set of values?"""
    found = [False]

    def walk(node):
        if found[0]:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if _nk(k) in ENUM_KEYS and isinstance(v, list) and v:
                    found[0] = True
                    return
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(step.config())
    return found[0]


@rule("GHL049", "AI output branched on without an enum constraint", "high",
      "routing", "reliability", "ai")
def ai_branch_without_enum(acct: Account):
    """Routing on model prose instead of a fixed value set.

    The rule: constrain any field you branch on with enums — never accept
    free text for routing or classification. A classifier picks one of N
    known values and its worst case is wrong routing; unconstrained output
    drifts ('interested', 'Interested!', 'seems interested') and the branch
    that matches none of it sends the contact down the else path silently.
    The enum is also prompt-injection mitigation: a model that can only
    emit one of six fixed values has no channel for an injected payload.
    """
    for wf in acct.published():
        for i, step in enumerate(wf.steps):
            if not _is_ai_step(step):
                continue
            if not any(s.is_branch for s in wf.steps[i + 1:]):
                continue
            if _declares_enum(step):
                continue
            yield _finding(
                "GHL049", "high", wf,
                "A branch routes on AI output that nothing constrains",
                "This AI step's output feeds a branch downstream, and the "
                "step declares no fixed set of allowed values. Free-text "
                "output drifts — 'interested', 'Interested!', 'the lead "
                "seems interested' — and every variant the branch does not "
                "literally match falls through to the else path with no "
                "record of why. It is also the injection surface: an "
                "inbound message that manipulates the model can steer "
                "unconstrained output anywhere, where an enum of six known "
                "values gives a payload no channel to travel in.",
                "Constrain the step to a fixed value list (an enum of the "
                "categories the branch actually routes on) and write the "
                "result into a typed custom field the If/Else reads. The "
                "AI decides WHICH path; the paths themselves stay "
                "deterministic.",
                step=step.name or step.type,
                cost="Contacts route to the wrong path — or silently to no "
                     "path — whenever the model phrases its answer a new "
                     "way. The routing is only as stable as the model's "
                     "mood, and nothing logs the misses.")


AI_MERGE = re.compile(
    r"\{\{\s*(ai|chatgpt|gpt|openai|assistant|llm)[._]", re.I)


@rule("GHL050", "AI-generated text sent with no approval gate", "high",
      "routing", "reliability", "ai")
def ai_send_without_approval(acct: Account):
    """Model output going straight to a customer.

    The gate decision runs on irreversibility, blast radius and confidence
    — any two elevated means add a gate. A customer-facing send of
    generated text has the first two elevated by definition: a sent message
    cannot be unsent, and it goes to a real customer. The approval must
    live in the workflow layer — a manual/review step — because an AI that
    decides for itself whether to ask permission has no gate at all. A
    manual send step IS the gate (a human releases it), so only automatic
    sends are flagged.
    """
    for wf in acct.published():
        for step in wf.outbound:
            if step.type.startswith("manual"):
                continue  # a human releases it — that is the gate
            body = step.bodies() or step.text()
            if not AI_MERGE.search(body):
                continue
            yield _finding(
                "GHL050", "high", wf,
                "Generated text reaches the customer with no human gate",
                "This step sends automatically and its body merges an AI "
                "output field — whatever the model produced goes to the "
                "contact verbatim. A generator's worst case is not wrong "
                "routing, it is the company saying something it can never "
                "take back: a hallucinated discount, a made-up policy, a "
                "reply steered by a hostile inbound message. Sending is "
                "irreversible and customer-facing — two elevated risk "
                "factors, which is the threshold where an approval gate "
                "stops being optional.",
                "Route the draft through a human: make the send a manual "
                "step a person releases, or have the AI classify instead "
                "and send a human-written template per category — the AI "
                "decides which path, a human wrote every word that goes "
                "out. Enforce the gate in the workflow layer, not in the "
                "prompt.",
                step=step.name or step.type,
                cost="One hallucinated sentence to one customer — a price, "
                     "a promise, a policy — costs more than the automation "
                     "saves, and it cannot be recalled once sent.")


LEGACY_SIG = "x-wh-signature"
CURRENT_SIG = "x-ghl-signature"


def _sig_mentions(blob: str) -> tuple[bool, bool]:
    low = blob.lower()
    return LEGACY_SIG in low, CURRENT_SIG in low


@rule("GHL051", "Legacy webhook signature — dead on September 1, 2026",
      "critical", "hygiene", "reliability", "webhooks", "deadline")
def legacy_signature_header(acct: Account):
    """An integration still verifying only the RSA X-WH-Signature header.

    GHL marketplace webhooks carry two authentication headers: X-GHL-
    Signature (Ed25519, the current standard) and X-WH-Signature (RSA,
    legacy) — and the legacy header is deprecated September 1, 2026. Any
    integration verifying only X-WH-Signature stops authenticating GHL's
    webhooks on that date: a dated, externally imposed, checkable deadline,
    which makes this the highest-conviction finding an audit can produce.
    Mentions of both headers together read as a migration already in hand
    and are left alone.
    """
    for wf in acct.workflows:
        blob = json.dumps([s.raw for s in wf.steps]
                          + [t.raw for t in wf.triggers])
        legacy, current = _sig_mentions(blob)
        if not legacy or current:
            continue
        yield _finding(
            "GHL051", "critical", wf,
            "References the X-WH-Signature header, which dies Sep 1, 2026",
            "This workflow references the legacy RSA X-WH-Signature webhook "
            "header and nowhere mentions its replacement. HighLevel has "
            "deprecated X-WH-Signature effective September 1, 2026 — after "
            "that date an integration verifying only the legacy header "
            "stops authenticating GHL's webhooks entirely. This is not a "
            "drifting best practice, it is a fixed external deadline: the "
            "break is scheduled, and it lands whether or not anyone is "
            "watching.",
            "Migrate the verification to the X-GHL-Signature header "
            "(Ed25519 — verified with GHL's public key, not a shared "
            "secret) before September 1, 2026, and keep accepting the "
            "legacy header only until the switchover is confirmed working.",
            cost="On September 1, 2026 this integration stops trusting "
                 "every webhook GHL sends it. Scheduled breakage, known "
                 "date, and the fix is cheap now and an outage later.")

    legacy_cvs = [name for name, value in acct.custom_values.items()
                  if LEGACY_SIG in str(value).lower()
                  or LEGACY_SIG in str(name).lower()]
    if legacy_cvs:
        all_blob = json.dumps(acct.custom_values)
        _, current_anywhere = _sig_mentions(all_blob)
        if not current_anywhere:
            yield Finding(
                rule="GHL051", severity="critical", workflow="(custom values)",
                step=", ".join(sorted(legacy_cvs)), category="hygiene",
                reach=2,
                title="Custom value references the X-WH-Signature header, "
                      "which dies Sep 1, 2026",
                symptom="An account custom value references the legacy RSA "
                        "X-WH-Signature webhook header, with no mention of "
                        "the Ed25519 X-GHL-Signature replacement anywhere in "
                        "the custom values. HighLevel deprecates the legacy "
                        "header on September 1, 2026 — whatever integration "
                        "consumes this value stops authenticating GHL "
                        "webhooks on that date.",
                fix="Migrate the consuming integration to X-GHL-Signature "
                    "(Ed25519, verified with GHL's public key) before "
                    "September 1, 2026, then update or retire this value.",
                cost="A scheduled outage with a published date. Cheap to fix "
                     "this week, an incident on the first of September.")


RESPONSE_CODE_KEYS = {"responsecode", "statuscode", "responsestatus",
                      "responsestatuscode", "errorstatuscode",
                      "errorresponsecode", "replystatuscode", "httpstatus"}
NON_2XX = re.compile(r"^\s*[45]\d\d\s*$")


def _declared_response_codes(cfg) -> list:
    """(key, value) pairs anywhere in the config whose key is response-code
    shaped. Exports nest these: n8n's respondToWebhook writes the code at
    `parameters.options.responseCode`, one level below where a flat
    `.items()` scan looks — 41 corpus workflows carried non-2xx codes there
    and were invisible to a top-level-only read."""
    hits: list = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if _nk(k) in RESPONSE_CODE_KEYS \
                        and isinstance(v, (str, int, float)):
                    hits.append((k, v))
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(cfg)
    return hits


@rule("GHL052", "Webhook handler answers a bad record with an error code",
      "medium", "routing", "reliability", "webhooks")
def non_2xx_on_bad_record(acct: Account):
    """A declared 4xx/5xx response — which guarantees redelivery.

    GHL marketplace webhooks retry on ANYTHING that is not 2xx — all 3xx,
    4xx, 5xx, timeouts included — up to 12 times with backoff, and the
    vendor's own instruction is to return 200 even for processing errors,
    reserving error codes for genuine infrastructure failure. A handler
    that answers a malformed record with 500 therefore orders 12 "
    redeliveries of the exact payload that just failed: the poison message
    is retried into every one of them. Ack it, then dead-letter it.
    """
    for wf in acct.published():
        for step in wf.steps:
            cfg = step.config()
            if not isinstance(cfg, dict):
                continue
            for k, v in _declared_response_codes(cfg):
                if not NON_2XX.match(str(v)):
                    continue
                yield _finding(
                    "GHL052", "medium", wf,
                    f"Responds {str(v).strip()} to a bad record — which "
                    "orders it redelivered",
                    "This step answers with a non-2xx status. GHL "
                    "marketplace webhooks redeliver on anything that is "
                    "not 2xx — up to 12 retries with backoff — so an "
                    "error code returned for a bad RECORD (malformed "
                    "payload, missing field) makes the sender redeliver "
                    "that same poison message every time, burning the "
                    "retry budget on a record that can never succeed and "
                    "masking real deliveries behind the noise.",
                    "Return 200 for anything you received and could not "
                    "process, and park the bad record in a failed-events "
                    "table (a DLQ) with the error message for replay "
                    "after the fix. Reserve non-2xx for genuine "
                    "infrastructure failure — the endpoint itself being "
                    "unable to take the request.",
                    step=step.name or step.type,
                    cost="Every malformed record arrives 13 times instead "
                         "of once, and each arrival re-runs whatever side "
                         "effects sit before the failure.")
                break


def run(acct: Account, min_severity: str = "low",
        only: Iterable[str] | None = None) -> list[Finding]:
    """Run the catalog. Returns findings sorted most severe first."""
    return run_all(acct, min_severity=min_severity, only=only)[0]


def run_all(acct: Account, min_severity: str = "low",
            only: Iterable[str] | None = None):
    """(findings, skips). The skips are half the honesty of the report.

    A rule that could not run is not a rule that passed. Callers that only want
    findings use `run()`; the report renderers use this, because a client is
    entitled to know which checks were never actually performed on their
    account.
    """
    cutoff = SEVERITIES.index(min_severity)
    wanted = set(only) if only else None
    findings: list[Finding] = []
    skips: list[Skip] = []
    for r in RULES:
        if wanted and r.id not in wanted:
            continue
        for item in r.check(acct):
            if isinstance(item, Skip):
                skips.append(item)
            elif SEVERITIES.index(item.severity) <= cutoff:
                findings.append(item)
    findings.sort(key=Finding.sort_key)
    skips.sort(key=lambda s: s.rule)
    return findings, skips


# The catalog past GHL052 lives in packs/, one module per failure family. The
# import is last so that `rule`, `_finding` and every helper above are already
# defined when a pack registers against them.
from . import packs  # noqa: E402,F401  (imported for its registration side effect)
