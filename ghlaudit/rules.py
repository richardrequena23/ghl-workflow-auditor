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


@rule("GHL007", "Deprecated opportunity action", "low", "hygiene", "maintenance")
def deprecated_action(acct: Account):
    for wf in acct.workflows:
        hits = wf.steps_of("create_opportunity", "update_opportunity")
        for step in hits:
            yield _finding(
                "GHL007", "low", wf,
                f"'{step.type}' is deprecated",
                "Existing workflows keep running, so nothing is broken today. But the "
                "action is flagged deprecated in the panel and will not be maintained.",
                f"Swap to internal_{step.type} when you next touch this workflow.",
                step=step.name or step.type,
                cost="Nothing today. It is maintenance debt that comes due on somebody "
                     "else's schedule, not yours.")


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

        # Same trigger AND the same ordered action types is not a collision, it
        # is the same workflow existing twice — what a snapshot re-pushed onto a
        # non-blank sub-account leaves behind. Everything double-sends, exactly.
        shapes = {by_name[n].shape() for n in names}
        cloned = len(shapes) == 1 and len(names) > 1
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
    r"\{\{\s*contact\.(first_name|name|full_name|last_name)\s*\}\}", re.I)


@rule("GHL016", "Greeting merges a contact field with no fallback", "medium", "hygiene", "copy")
def bare_greeting_field(acct: Account):
    for wf in acct.published():
        for step in wf.outbound:
            m = GREETING_FIELD.search(step.text())
            if m:
                yield _finding(
                    "GHL016", "medium", wf,
                    f"Greeting renders as '{m.group(1)} ,' when "
                    f"{m.group(2)} is empty",
                    "Imported lists and form fills routinely leave the name field "
                    "blank, and GoHighLevel renders a missing merge field as empty "
                    "text — the message still sends, opening with 'Hi ,'. That is "
                    "the tell of an automated blast, to exactly the lead you were "
                    "trying to sound personal for.",
                    "Give the merge field a default value, or write the opener so "
                    "it survives an empty field ('Hey there' beats 'Hey ,'). Then "
                    "fix the import so names actually arrive."
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


@rule("GHL018", "Nothing adds the tag this workflow waits for", "low", "dead_weight", "triggers", "hygiene")
def orphan_tag_trigger(acct: Account):
    added: set[str] = set()
    for w in acct.workflows:
        added |= w.tags_added()
    for wf in acct.published():
        if not wf.triggers or not all("tag" in t.type.lower() for t in wf.triggers):
            continue  # another trigger type can still start it
        tags = wf.trigger_tags()
        if not tags or tags & added:
            continue
        missing = ", ".join(sorted(tags))
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

    have_any = any(inv.has(bucket_for[k]) for k in checkable if k in bucket_for)
    if not have_any:
        yield Skip(
            rule="GHL020",
            title="Reference to something that does not exist",
            reason="No account object lists were supplied, so a reference to a "
                   "deleted calendar, user, pipeline or template cannot be told "
                   "apart from one that simply was not in this export.",
            needs="calendars / users / pipelines / forms / surveys / "
                  "emailTemplates in the input bundle",
            category="hygiene")
        return

    misses: dict = {}
    for wf in acct.published():
        for step in wf.steps:
            for kind, ident in step.entity_refs():
                if kind == "workflow":
                    if workflow_ids and ident not in workflow_ids:
                        misses.setdefault((wf.name, kind), []).append(
                            (step.name or step.type, ident))
                    continue
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
        transactional = any(
            any(k in t.canonical_type() for k in
                ("appointment", "order", "payment", "invoice", "form_submitted"))
            for t in wf.triggers) and len(emails) == 1
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
