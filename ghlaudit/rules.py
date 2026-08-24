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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .model import Account, PLACEHOLDER, Step, Workflow, slug

SEVERITIES = ("critical", "high", "medium", "low")


@dataclass
class Finding:
    rule: str
    severity: str
    workflow: str
    title: str
    symptom: str
    fix: str
    step: str = ""

    def sort_key(self):
        return (SEVERITIES.index(self.severity), self.workflow, self.rule)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "severity": self.severity, "workflow": self.workflow,
            "step": self.step, "title": self.title, "symptom": self.symptom,
            "fix": self.fix,
        }


@dataclass
class Rule:
    id: str
    title: str
    severity: str
    check: Callable[[Account], Iterable[Finding]]
    tags: tuple = field(default_factory=tuple)


RULES: list[Rule] = []


def rule(rule_id: str, title: str, severity: str, *tags):
    def wrap(fn):
        RULES.append(Rule(rule_id, title, severity, fn, tags))
        return fn
    return wrap


def _finding(r_id, sev, wf, title, symptom, fix, step="") -> Finding:
    return Finding(rule=r_id, severity=sev, workflow=wf.name, title=title,
                   symptom=symptom, fix=fix, step=step)


# --------------------------------------------------------------------------
# Triggers that fire more often than their author thinks
# --------------------------------------------------------------------------

APPOINTMENT_TRIGGERS = ("appointment", "customer_booked_appointment",
                        "appointment_status", "booked_appointment")
# The words that mean "this trigger was narrowed to one appointment status".
APPT_STATUS_WORDS = ("noshow", "no-show", "no_show", "confirmed", "cancelled",
                     "canceled", "showed", "invalid", "status", "booked", "new")


@rule("GHL001", "Appointment trigger is not filtered by status", "critical", "triggers")
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
                    step=trg.name or trg.type)


@rule("GHL002", "Call trigger is not filtered to missed calls", "critical", "triggers")
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
                    step=trg.name or trg.type)


# --------------------------------------------------------------------------
# Workflows that talk but never listen
# --------------------------------------------------------------------------

@rule("GHL003", "Multi-touch sequence never checks for a reply", "high", "replies")
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
                "names this workflow.")
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
            "Prefer the second — one listener can clean up after every sequence.")


@rule("GHL009", "Reply alert has no once-per-conversation guard", "medium", "replies")
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
                "the rep gets exactly one loud ping per conversation per campaign.")


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

TIME_CRITICAL = re.compile(
    r"remind|reminder|starting soon|in 1 hour|in one hour|10 minutes|"
    r"see you at|your call (is|starts)|confirm", re.I)


@rule("GHL004", "Send window applied to time-critical messages", "high", "timing")
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
                step=hits[0].name or hits[0].type)


@rule("GHL013", "Send window with no contact timezone", "low", "timing")
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
                "Set the workflow to use the contact's timezone alongside the window.")


@rule("GHL005", "Bulk campaign has no throttle", "medium", "scale")
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
            "a sane default. Ask the client what they can actually service first.")


# --------------------------------------------------------------------------
# Things that break on handover
# --------------------------------------------------------------------------

@rule("GHL006", "Webhook posts to a hardcoded URL", "medium", "portability")
def hardcoded_endpoint(acct: Account):
    for wf in acct.workflows:
        for step in wf.steps_of("webhook", "http_request", "outbound_webhook"):
            blob = step.text()
            urls = re.findall(r"https?://[^\s\"']+", blob)
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
                    step=step.name or urls[0][:60])


@rule("GHL008", "Unresolved placeholder in a live workflow", "high", "portability")
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
                    "workflows using it are published.")
    for wf in acct.published():
        for step in wf.outbound:
            if PLACEHOLDER.search(step.text()):
                yield _finding(
                    "GHL008", "high", wf,
                    "Outbound message contains placeholder text",
                    "A published message still has REPLACE-WITH / TODO text in the "
                    "body. It will be sent to a customer exactly as written.",
                    "Fill in the copy, or unpublish the workflow until it is ready.",
                    step=step.name or step.type)

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
                fix=f"Create the custom value '{key}', or correct the merge field.")


@rule("GHL007", "Deprecated opportunity action", "low", "maintenance")
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
                step=step.name or step.type)


@rule("GHL011", "Re-enrollment on a workflow that creates records", "medium", "data")
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
                "that no open opportunity already exists for this contact.")


# --------------------------------------------------------------------------
# Asks that go out to the wrong person
# --------------------------------------------------------------------------

SUPPRESSION = ("unhappy", "complaint", "refund", "dispute", "cancel", "churn",
               "do-not-contact", "dnc", "opt-out", "optout")


@rule("GHL010", "Review or referral ask is only screened at enrollment", "medium", "asks")
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
                "re-check them immediately before EACH send, not only at enrollment.")
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
                step=step.name or step.type)


@rule("GHL012", "Sandbox or test workflow is published", "low", "hygiene")
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
                "Unpublish it, or rename it if it is genuinely production.")


# --------------------------------------------------------------------------
# Workflows that trigger each other — nothing in the UI shows this
# --------------------------------------------------------------------------

def _allows_reentry(wf: Workflow) -> bool:
    blob = json.dumps(wf.settings).lower()
    return any(k in blob for k in ("allowmultiple", "allow_multiple", "reentry",
                                   "re_entry", "allowreentry")) and "false" not in blob


@rule("GHL014", "Workflows re-trigger each other through tags", "critical", "triggers", "data")
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
                    "trigger tag as its first step so a lap cannot restart.")


@rule("GHL015", "Two workflows enroll on the identical trigger", "high", "triggers")
def duplicate_enrollment(acct: Account):
    groups: dict[tuple, list[Workflow]] = {}
    for w in acct.published():
        if not w.outbound:
            continue
        for t in w.triggers:
            groups.setdefault((t.type.lower(), t.filter_blob()), []).append(w)
    seen: set[tuple] = set()
    for (ttype, _blob), wfs in sorted(groups.items()):
        names = sorted({w.name for w in wfs})
        if len(names) < 2:
            continue
        key = (ttype, tuple(names))
        if key in seen:
            continue
        seen.add(key)
        yield Finding(
            rule="GHL015", severity="high", workflow=names[0],
            step=", ".join(names[1:]),
            title=f"{len(names)} workflows fire on the same '{ttype}' event "
                  "with identical filters",
            symptom="One event enrolls the contact in all of these at once: "
                    + ", ".join(names) + ". Each one sends its own messages, so "
                    "the lead hears from two sequences in the same afternoon and "
                    "reads it as spam. The builder shows each workflow alone — "
                    "this collision is invisible until a customer complains.",
            fix="Decide which workflow owns this event and narrow or remove the "
                "trigger on the others. If both genuinely must run, make one of "
                "them internal-only (no outbound steps).")


# --------------------------------------------------------------------------
# Message copy that fails quietly
# --------------------------------------------------------------------------

GREETING_FIELD = re.compile(
    r"\b(hi|hey|hello|good\s+(?:morning|afternoon|evening))\s*,?\s*"
    r"\{\{\s*contact\.(first_name|name|full_name|last_name)\s*\}\}", re.I)


@rule("GHL016", "Greeting merges a contact field with no fallback", "medium", "copy")
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
                    "fix the import so names actually arrive.",
                    step=step.name or step.type)
                break  # one finding per workflow is enough to make the point


OPT_OUT = re.compile(
    r"\b(?:reply|text|txt|send)\s+['\"]?stop\b|\bstop\s+to\s+(?:opt|unsub|end)|"
    r"\bopt[- ]?out\b|\bunsubscribe\b", re.I)
SMS_TYPES = ("sms", "manual_sms")
BULK_WORDS = ("reactivat", "database", "blast", "bulk", "list", "dormant", "cold",
              "winback", "win-back", "nurture")


@rule("GHL017", "SMS sequence carries no opt-out language", "high", "compliance")
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
            step=texts[0].name or texts[0].type)


@rule("GHL018", "Nothing adds the tag this workflow waits for", "low", "triggers", "hygiene")
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
            "that in the workflow name so the next auditor does not ask.")


def run(acct: Account, min_severity: str = "low",
        only: Iterable[str] | None = None) -> list[Finding]:
    """Run the catalog. Returns findings sorted most severe first."""
    cutoff = SEVERITIES.index(min_severity)
    wanted = set(only) if only else None
    out: list[Finding] = []
    for r in RULES:
        if wanted and r.id not in wanted:
            continue
        for finding in r.check(acct):
            if SEVERITIES.index(finding.severity) <= cutoff:
                out.append(finding)
    out.sort(key=Finding.sort_key)
    return out
