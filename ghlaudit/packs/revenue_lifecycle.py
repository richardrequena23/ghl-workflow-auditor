"""Revenue and lifecycle — defects that show up in the money rather than the logs.

Every check here is about the COMMERCIAL state of a contact — a new lead, a
booked call, a buyer, a refund — and a workflow that does not know which one it
is looking at. A first response parked behind a wait (GHL095), a selling cadence
that keeps selling after the sale lands (GHL096), a chargeback that stops
nothing (GHL097), a renewal ask that ignores the refund marker the account
itself writes (GHL098), a pipeline that only ever fills (GHL099), and bought
leads arriving with no record of who sold them (GHL100). None of these break a
workflow: all six run green, and what they cost is either a sale or the ability
to see where sales come from — which is why they survive for years in accounts
that are otherwise well built. GHL032 owns the blank opportunity stage, GHL033
the pre-payment confirmation, GHL039/GHL040 pipeline collisions and GHL010 the
review ask sent to an unhappy customer; those lanes are excluded below rather
than re-checked.
"""

from __future__ import annotations

import json
import re

from ..model import Account, Step, Workflow, slug
from ..rules import _finding, rule


def _nk(key) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


# -- durations --------------------------------------------------------------
# Minutes per unit. A month is 30 days: nothing here is calendar arithmetic, it
# is "how long before this lead hears anything", and 28 or 31 does not change
# the answer to that.
_UNITS = {"second": 1 / 60, "sec": 1 / 60, "minute": 1.0, "min": 1.0,
          "hour": 60.0, "hr": 60.0, "day": 1440.0, "week": 10080.0,
          "month": 43200.0}

_DURATION_TEXT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|"
    r"weeks?|months?)", re.I)

_DURATION_KEYS = ("delay", "duration", "wait", "waittime", "waitfor", "amount",
                  "value", "length", "period", "interval", "time")


def _to_minutes(value, unit):
    # `{"delay": true}` is a flag, and float(True) is 1.0 — which would report
    # a one-minute wait that does not exist.
    if isinstance(value, bool):
        return None
    try:
        magnitude = abs(float(value))
    except (TypeError, ValueError):
        return None
    stem = re.sub(r"[^a-z]", "", str(unit).lower())
    if stem.endswith("s") and stem[:-1] in _UNITS:
        stem = stem[:-1]
    factor = _UNITS.get(stem)
    return None if factor is None else magnitude * factor


def _wait_minutes(step: Step):
    """How long this wait holds the contact, in minutes, or None if unreadable.

    None is not zero and no check below may treat it as one. A bare
    `{"delay": 30}` with no unit could be half an hour or a month, and putting
    the wrong one of those in a client's report costs more than missing the
    finding — so an unreadable wait ends the check for that workflow.
    """
    if not step.is_wait:
        return None
    # An event wait ("until they reply") is not a length of time at all. Its own
    # failure mode — no timeout — is GHL019's, and reading a duration out of one
    # would be inventing a number.
    try:
        if step.wait_is_conditional():
            return None
        cfg = step.config()
        cfg = cfg if isinstance(cfg, dict) else {}
        for k, v in cfg.items():
            if _nk(k) not in _DURATION_KEYS:
                continue
            if isinstance(v, dict):
                unit = next((v[u] for u in v if _nk(u) in
                             ("unit", "units", "period")), None)
                for vk in v:
                    if _nk(vk) in ("value", "amount", "duration", "number"):
                        found = _to_minutes(v[vk], unit)
                        if found is not None:
                            return found
            elif isinstance(v, str):
                m = _DURATION_TEXT.search(v)
                if m:
                    return _to_minutes(m.group(1), m.group(2))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                unit = next((cfg[u] for u in cfg if _nk(u) in
                             ("unit", "units", "period")), None)
                found = _to_minutes(v, unit) if unit is not None else None
                if found is not None:
                    return found
        blob = json.dumps(cfg, default=str) + " " + step.name
    except (TypeError, ValueError):
        return None
    m = _DURATION_TEXT.search(blob)
    return _to_minutes(m.group(1), m.group(2)) if m else None


def _human(minutes: float) -> str:
    """'45 minutes', '2 days' — the finding is read by a business owner."""
    for unit, factor in (("day", 1440.0), ("hour", 60.0), ("minute", 1.0)):
        if minutes >= factor:
            count = minutes / factor
            shown = int(count) if abs(count - round(count)) < 0.01 \
                else round(count, 1)
            return f"{shown} {unit}{'s' if shown != 1 else ''}"
    return f"{int(round(minutes * 60))} seconds"


# -- triggers ---------------------------------------------------------------

def _trigger_text(trigger) -> str:
    """Type, name and filters as one searchable string.

    All three are read because the export decides where the meaning lands: a
    Stripe chargeback arrives as an inbound webhook whose TYPE says nothing and
    whose NAME says everything.
    """
    parts = [str(getattr(trigger, "type", "")), str(getattr(trigger, "name", ""))]
    try:
        parts.append(trigger.filter_blob())
    except (TypeError, ValueError, AttributeError):
        pass
    return " ".join(parts)


# The events that put a NEW lead in front of the business. Speed matters on
# every one of them and on nothing else — a tag added by another workflow is not
# a lead arriving, it is a lead being moved.
INBOUND_LEAD_TRIGGER = re.compile(
    r"form[_ -]?submit|survey[_ -]?submit|inbound[_ -]?webhook|"
    r"facebook[_ -]?lead|lead[_ -]?ad|lead[_ -]?form|contact[_ -]?created|"
    r"new[_ -]?contact|new[_ -]?lead", re.I)

# The events that mean the contact converted. Used two ways: a workflow that
# STARTS on one is a post-sale lane and not a selling cadence, and an account
# that has one is an account whose pipeline could be advanced automatically.
CONVERSION_TRIGGER = re.compile(
    r"appointment[_ -]?(booked|confirmed|status)|customer[_ -]?booked|"
    r"booked[_ -]?appointment|order[_ -]?(submitted|placed|completed)|"
    r"payment[_ -]?(received|succeeded|success)|invoice[_ -]?paid|purchase|"
    r"subscription[_ -]?(started|created|active)|"
    r"opportunit(?:y|ies)[_ -]?(status|stage|won)", re.I)

# Anything that means this workflow is about a calendar slot. Those lanes belong
# to GHL001/GHL028/GHL065-GHL070 and are excluded from the selling checks here.
APPOINTMENT_CONTEXT = re.compile(r"appointment|appt|calendar|booked|booking",
                                 re.I)

# A refund, a chargeback or a subscription ending. "Cancelled" is deliberately
# never enough on its own — the most common cancellation in a GoHighLevel
# account is an appointment, and matching that would point this rule at the
# rebooking workflow, which is the opposite of the defect.
REFUND_EVENT = re.compile(
    r"refund|charge[_ -]?back|payment[_ -]?(failed|declined|dispute[d]?)|"
    r"dispute[d]?|(?:subscription|membership|plan|order|account|contract|"
    r"policy)[_ -]?(?:cancel|cancell?ed|terminat|churn|lapsed|expired|ended)|"
    r"cancel(?:l?ed)?[_ -]?(?:subscription|membership|plan|order|account)|"
    r"churn", re.I)


def _has(pattern, trigger) -> bool:
    return bool(pattern.search(_trigger_text(trigger)))


# -- markers ----------------------------------------------------------------

# The vocabulary an account uses for "this customer is not one to sell to".
# Whether it is a tag, a field value or a field name, the words are the same.
CHURN_MARKER = re.compile(
    r"refund|charge[_ -]?back|dispute|churn|cancell?ed|lapsed|complain|"
    r"unhappy|dissatisf|do[_ -]?not[_ -]?(?:contact|sell|market)|dnc|"
    r"suppress|bad[_ -]?debt|write[_ -]?off", re.I)

SUPPRESSION_TYPE = re.compile(
    r"dnd|do[_ -]?not[_ -]?disturb|opt[_ -]?out|unsubscrib|suppress|"
    r"remove[_ -]?(?:from[_ -]?)?(?:workflow|campaign|sequence|list)", re.I)

FIELD_WRITE_TYPE = re.compile(
    r"(?:update|set|edit|write)[_ -]?(?:contact[_ -]?)?(?:custom[_ -]?)?field|"
    r"(?:update|edit)[_ -]?contact(?![_ -]?tag)", re.I)

FIELD_KEY_NAMES = {"field", "fieldkey", "fieldid", "fieldname", "customfield",
                   "targetfield", "attribute"}
FIELD_VALUE_NAMES = {"value", "newvalue", "fieldvalue", "val", "to", "content"}


def _field_slug(key) -> str:
    """'contact.lead_source' and 'Lead Source' are the same field."""
    return slug(str(key).split(".")[-1])


def _field_writes(step: Step) -> list:
    """[(field key, written value)] for the contact fields this step sets.

    Two shapes, because those are the two that carry a readable KEY: the flat
    field/value pair and a {"fields": {...}} map. A write addressed only by id
    ({"customFieldId": "a1b2"}) yields nothing — the key is unknowable, and a
    guess at it is how a data check starts lying.
    """
    if not FIELD_WRITE_TYPE.search(str(step.type or "")):
        return []
    cfg = step.config()
    if not isinstance(cfg, dict):
        return []
    out = []
    key = value = None
    for k, v in cfg.items():
        nk = _nk(k)
        if key is None and nk in FIELD_KEY_NAMES and isinstance(v, (str, int)) \
                and str(v).strip():
            key = str(v)
        elif value is None and nk in FIELD_VALUE_NAMES \
                and isinstance(v, (str, bool, int, float)):
            value = str(v)
    if key is not None and value is not None:
        out.append((_field_slug(key), value))
    for k, v in cfg.items():
        if _nk(k) in ("fields", "customfields", "contactfields") \
                and isinstance(v, dict):
            for fk, fv in v.items():
                if isinstance(fv, (str, bool, int, float)):
                    out.append((_field_slug(fk), str(fv)))
    return [(k, v) for k, v in out if k]


def _tags(step: Step) -> set:
    try:
        return step.tags_added()
    except (TypeError, ValueError, AttributeError):
        return set()


def _marks_churn(step: Step) -> bool:
    """Does this step record that a customer went bad?

    A tag matching the churn vocabulary, or a field whose key or value does.
    Appointment cancellations are excluded — 'appointment-cancelled' is a
    scheduling fact, not a commercial one, and treating it as a churn marker
    would make every booking account look like it tracks refunds.
    """
    for tag in _tags(step):
        if CHURN_MARKER.search(tag) and not APPOINTMENT_CONTEXT.search(tag):
            return True
    for key, value in _field_writes(step):
        text = f"{key} {value}"
        if CHURN_MARKER.search(text) and not APPOINTMENT_CONTEXT.search(text):
            return True
    return False


def _account_marks_churn(acct: Account) -> bool:
    return any(_marks_churn(s) for wf in acct.published() for s in wf.steps)


# --------------------------------------------------------------------------
# GHL095 — the first reply, parked behind a wait
# --------------------------------------------------------------------------

# The bar every lead-response process is written against. Under five minutes the
# lead is still on the page they filled the form on, which is the whole
# mechanism: the message arrives while they are still thinking about you, not
# after they have filled in the next three forms. Below this the ordering is a
# judgement call and this stays quiet.
SPEED_TO_LEAD_MINUTES = 5.0

# Past an hour the first message stops being a response and becomes an
# introduction to someone who has already spoken to a competitor. That is the
# line between "slower than it should be" and "will misfire in normal use".
SPEED_TO_LEAD_SERIOUS = 60.0

IMMEDIATE_HANDOFF = re.compile(
    r"internal[_ -]?notification|notification|notify|slack|alert|email[_ -]?"
    r"(?:user|team|staff)|assign|round[_ -]?robin|rotate|create[_ -]?task|"
    r"add[_ -]?task", re.I)


@rule("GHL095", "New lead's first touch is held behind a wait", "high",
      "routing", "speed", "revenue")
def first_touch_behind_a_wait(acct: Account):
    """A form fill, then a wait, then the first message.

    The wait is almost always put there for a good reason — give the rep a
    chance to call first, do not look robotic, let the CRM catch up — and it is
    the single most expensive ordering mistake available in the builder. A lead
    who submitted a form is comparing you against whoever answers first, and the
    comparison is decided in minutes. The workflow reports perfect delivery
    either way, so nothing in the account ever shows what the delay costs.

    A step that hands the lead to a human immediately — a notification, an
    assignment, a task — counts as the first touch and this stays quiet, because
    then somebody WAS told and the delay is a staffing question rather than a
    build defect. And a wait whose length the export does not state is left
    alone: no number is better than the wrong number.
    """
    for wf in acct.published():
        if not any(_has(INBOUND_LEAD_TRIGGER, t) for t in wf.triggers):
            continue
        first = next((i for i, s in enumerate(wf.steps) if s.is_outbound), None)
        if first is None:
            continue
        before = wf.steps[:first]
        if any(IMMEDIATE_HANDOFF.search(f"{s.type} {s.name}") for s in before):
            continue
        waits = [s for s in before if s.is_wait]
        if not waits:
            continue
        minutes = 0.0
        for step in waits:
            held = _wait_minutes(step)
            if held is None:
                minutes = None
                break
            minutes += held
        if minutes is None or minutes <= SPEED_TO_LEAD_MINUTES:
            continue
        send = wf.steps[first]
        serious = minutes >= SPEED_TO_LEAD_SERIOUS
        yield _finding(
            "GHL095", "high" if serious else "medium", wf,
            f"New leads wait {_human(minutes)} before this account says "
            "anything",
            "This workflow starts when a lead arrives and then holds them "
            f"for {_human(minutes)} before the first message goes out. "
            "Nothing before the wait tells a human either, so for that whole "
            "window the lead has contacted you and heard nothing back. They "
            "are not waiting during it — they are on the next tab, filling in "
            "the next form, and the business that answers while they are still "
            "at their desk gets the conversation. Delivery reports show this "
            "workflow performing perfectly, because every message it sends "
            "does arrive.",
            "Move the first message to the top of the workflow, above the "
            "wait, and keep the wait for the second touch. If the delay exists "
            "so a rep can call first, put the internal notification above the "
            "wait as well so somebody is actually told inside the window. Then "
            "check the workflow has no send window on it — a window holds the "
            "first message too, which re-creates this on every lead who "
            "arrives in the evening.",
            step=send.name or send.type,
            cost=f"Every lead this workflow receives is left silent for "
                 f"{_human(minutes)}. The ones who were comparing options are "
                 "gone by then, and they were the ones worth the ad spend.")


# --------------------------------------------------------------------------
# GHL096 — the cadence that keeps selling after the sale
# --------------------------------------------------------------------------

SELLING_LANE_NAME = re.compile(
    r"nurture|follow[_ -]?up|drip|cadence|sequence|reactivat|re[_ -]?engag|"
    r"outreach|prospect|cold|win[_ -]?back|winback|dormant|promo|offer|"
    r"campaign|blast|pitch", re.I)

# Copy that asks for the sale or the meeting. Kept to phrasings that can only be
# an ask — "let us know" and "get in touch" appear in service messages too, and
# matching them would put every transactional workflow in this check.
SELLING_COPY = re.compile(
    r"book (?:a|your|another) (?:call|time|slot|spot|demo|consult)|"
    r"schedule (?:a|your) (?:call|time|demo|consult)|"
    r"grab (?:a|your) (?:time|slot|spot)|pick a time|find (?:you )?a slot|"
    r"good time to (?:talk|chat|jump on)|calendar link|"
    r"check ?out|buy now|order now|enrol|enroll|sign up (?:now|today)|"
    r"claim your|get started (?:now|today)|\d{1,2}% off|discount code", re.I)

# What a workflow does when it notices the contact converted: pulls them out,
# or branches on the thing that proves it. Notice that a REPLY is not on this
# list and never will be — the whole point is the customer who books through
# the link or buys through the checkout and never texts back. GHL003 owns
# replies; this owns the sale.
CONVERSION_CHECK = re.compile(
    r"appointment|appt|booked|booking|opportunit|deal|purchase|order|paid|"
    r"payment|customer|converted|\bwon\b|sold|checkout|invoice", re.I)


def _checks_for_conversion(wf: Workflow) -> bool:
    if wf.exits():
        return True
    for step in wf.steps:
        try:
            branching = step.is_branch or step.wait_is_conditional()
        except (TypeError, ValueError, AttributeError):
            branching = False
        if not branching:
            continue
        if CONVERSION_CHECK.search(f"{step.name} {step.text()}"):
            return True
    return False


def _conversion_guard(acct: Account):
    """The workflow, if any, that cleans up account-wide when a sale lands.

    The mature build has one: a workflow on the booking or the order that pulls
    the contact out of every selling sequence at once. A per-workflow check
    cannot see it, and flagging every cadence in an account that has one would
    be reporting the correct pattern as a defect.
    """
    for wf in acct.published():
        if any(_has(CONVERSION_TRIGGER, t) for t in wf.triggers) and wf.exits():
            return wf
    return None


@rule("GHL096", "Selling cadence keeps running after the customer buys", "high",
      "routing", "revenue", "lifecycle")
def cadence_with_no_conversion_exit(acct: Account):
    """A multi-touch sales sequence with nothing that notices the sale.

    The lead books through the link on message one. They never text back —
    there is nothing to text back to, they just booked. So the cadence carries
    on: "still interested?" the day before their call, "closing your file" the
    day after they paid. It is the sequence built to win the customer actively
    talking them back out of it, and the client hears about it from the
    customer, not from the system.

    Reply detection (GHL003) does not cover this and cannot: booking a slot and
    completing a checkout are both silent. What is needed is a check on the
    thing that proves conversion — the appointment, the opportunity, the order
    — either inside the cadence or in one workflow that cleans up after all of
    them.
    """
    guard = _conversion_guard(acct)
    for wf in acct.published():
        if len(wf.outbound) < 2 or not any(s.is_wait for s in wf.steps):
            continue
        # A workflow that STARTS on a booking or an order is the post-sale lane
        # — onboarding, reminders, fulfilment. Its contacts have already
        # converted, so there is nothing here for it to stop doing.
        if any(_has(CONVERSION_TRIGGER, t) or _has(APPOINTMENT_CONTEXT, t)
               for t in wf.triggers):
            continue
        if re.search(r"review|referral|testimonial|nps", wf.name, re.I):
            continue  # GHL010's lane
        selling = bool(SELLING_LANE_NAME.search(wf.name)) or \
            bool(SELLING_COPY.search(wf.bodies() or wf.text()))
        if not selling or _checks_for_conversion(wf):
            continue
        if guard is not None and guard.id != wf.id:
            yield _finding(
                "GHL096", "low", wf,
                f"Conversions are handled by '{guard.name}' — confirm this "
                "cadence is in its remove list",
                "This selling sequence has no conversion check of its own, "
                "which is correct when one workflow pulls contacts out of every "
                "cadence the moment they book or buy. But that workflow removes "
                "from a NAMED list of workflows, and a cadence added later is "
                "the easiest thing in the account to leave off it. If this one "
                "is missing, a customer who has already booked keeps receiving "
                "the rest of these messages.",
                f"Open '{guard.name}' and check its remove-from-workflow step "
                "names this cadence.",
                cost="If this cadence was left off the list, every contact who "
                     "converts out of it keeps being sold to — in the one part "
                     "of the account that was supposed to be safe.")
            continue
        yield _finding(
            "GHL096", "high", wf,
            f"{len(wf.outbound)} sales messages, and nothing checks whether "
            "they already bought",
            "This cadence sends until it runs out of messages. Nothing in it "
            "looks at whether the contact has booked an appointment, has an "
            "opportunity, or has paid — so a customer who books through the "
            "link in message one still gets 'still interested?' the day before "
            "their call and 'closing your file' the day after. They booked "
            "silently, which is exactly what a booking link is for, so reply "
            "detection would not have caught it either. The client finds out "
            "when the customer forwards the messages back and asks whether "
            "anyone there is paying attention.",
            "Add a conversion exit: a Remove-From-Workflow, or a branch before "
            "each send that checks for an appointment or an open opportunity. "
            "The better build is one workflow triggered on Appointment Booked "
            "and Order Submitted that removes the contact from every selling "
            "cadence at once — then a cadence added next month is one line to "
            "protect instead of a rebuild. Verify by booking a slot as a test "
            "contact and confirming the next message never lands.",
            cost="The customers this insults are the ones who already said "
                 "yes. Every cancelled booking here is revenue you had won and "
                 "then spent messages talking back out of.")


# --------------------------------------------------------------------------
# GHL097 — the refund that stops nothing
# --------------------------------------------------------------------------

def _suppresses(step: Step) -> bool:
    """Does this step take the contact out of active selling, or mark them?

    Marking counts. A refund workflow that tags the contact 'refunded' has done
    its half of the job — whether the selling workflows then honour that tag is
    a different defect, and GHL098 owns it. Splitting them this way is what
    stops one broken refund path from producing the same finding twice.
    """
    label = f"{step.type} {step.name}"
    if SUPPRESSION_TYPE.search(label):
        return True
    return _marks_churn(step)


@rule("GHL097", "Refund or cancellation that removes the customer from nothing",
      "high", "routing", "revenue", "lifecycle")
def refund_that_suppresses_nothing(acct: Account):
    """The money goes back and the marketing carries on.

    A chargeback or a cancelled subscription is the clearest signal a CRM ever
    receives, and in most accounts it lands in exactly one place: an internal
    notification. Nothing removes the contact from the sequences they are
    already inside, nothing marks the record, so the upsell that was scheduled
    for day 30 goes out on day 30 as though nothing happened. The customer who
    just disputed a charge gets asked to buy again — and with a chargeback
    already open, that is the message their bank reads.

    The lane is established from the TRIGGER, never from the workflow's name
    alone, and appointment cancellations are excluded: the most common thing
    cancelled in a GoHighLevel account is a booking, and that lane belongs to
    GHL028.
    """
    others = [w for w in acct.published() if w.outbound]
    for wf in acct.published():
        lane = [t for t in wf.triggers
                if _has(REFUND_EVENT, t) and not _has(APPOINTMENT_CONTEXT, t)]
        if not lane:
            continue
        # No other workflow sends anything, so there is nothing to be pulled
        # out of and no message left running. Underclaiming beats a finding
        # whose fix is "remove them from the zero sequences you have".
        if not [w for w in others if w.id != wf.id]:
            continue
        if any(_suppresses(s) for s in wf.steps):
            continue
        trg = lane[0]
        yield _finding(
            "GHL097", "high", wf,
            "Refund lane records nothing and removes the customer from nothing",
            "This workflow runs on a refund, chargeback or cancellation — the "
            "strongest commercial signal the account ever gets — and it does "
            "not act on it. Nothing removes the contact from the sequences "
            "they are currently inside, nothing switches off messaging, and "
            "nothing writes the fact down anywhere the rest of the account can "
            "read. So every campaign, upsell and follow-up already scheduled "
            "for this person keeps running exactly as though they were still a "
            "happy customer, and the next one asks them to buy again.",
            "Add two steps here: remove the contact from the active selling "
            "workflows, and tag them (refunded / cancelled) so every future "
            "audience can exclude them. Tag first — the tag is the part that "
            "protects campaigns built after today. Verify by refunding a test "
            "order and confirming the tag lands and the next scheduled message "
            "never sends.",
            step=trg.name or trg.type,
            cost="Refunded and disputing customers keep receiving sales "
                 "messages. On an open chargeback that is evidence against you, "
                 "and it is the single fastest way to turn one refund into a "
                 "review.")


# --------------------------------------------------------------------------
# GHL098 — the renewal ask that ignores the account's own marker
# --------------------------------------------------------------------------

# Asks aimed at somebody who has already paid once. Deliberately narrower than
# GHL096's selling vocabulary: this is about the SECOND sale, and the mistake
# only exists where the first one happened. Expiry only counts when it is a
# PLAN expiring — a quote expires too, and chasing a quote is the first sale,
# which is a different lane with a different customer in it.
UPSELL_LANE = re.compile(
    r"renew|renewal|upsell|up[_ -]?sell|upgrade|re[_ -]?order|reorder|"
    r"repurchase|re[_ -]?subscribe|resubscribe|top[_ -]?up|add[_ -]?on|"
    r"cross[_ -]?sell|loyalty|next (?:package|tier|plan)|"
    r"your (?:plan|subscription|membership|contract)|"
    r"(?:plan|subscription|membership|contract|cover|policy|licen[cs]e)"
    r"[^.\n]{0,24}expir", re.I)


def _screens_for_churn(wf: Workflow) -> bool:
    """Does anything here look at the refund/cancel marker before it asks?

    Both places count: a trigger filtered to exclude the tag (screened on the
    way in) and a branch before the send (screened at send time). The second is
    the better build — GHL010 makes that case for review asks — but either one
    means somebody thought about it.
    """
    for trg in wf.triggers:
        if CHURN_MARKER.search(_trigger_text(trg)) \
                and not APPOINTMENT_CONTEXT.search(_trigger_text(trg)):
            return True
    for step in wf.steps:
        try:
            branching = step.is_branch or step.wait_is_conditional()
        except (TypeError, ValueError, AttributeError):
            branching = False
        if not branching:
            continue
        if CHURN_MARKER.search(f"{step.name} {step.text()}"):
            return True
    return False


@rule("GHL098", "Renewal or upsell ask ignores the account's own churn marker",
      "high", "routing", "revenue", "lifecycle")
def upsell_with_no_churn_screen(acct: Account):
    """Asking for the second sale from somebody who reversed the first.

    This only fires where the account already labels these people: some
    workflow writes a refunded / cancelled / complaint marker, so the contacts
    exist, they are identified, and the data needed to exclude them is sitting
    right there. The upsell simply does not look at it. That is what makes this
    worth writing up rather than theorising about — the fix is one branch, and
    the evidence that it is needed is already in the account.

    GHL010 covers the same mistake for review and referral asks, so those names
    are excluded here; the two never report the same workflow twice.
    """
    if not _account_marks_churn(acct):
        return
    for wf in acct.published():
        if not wf.outbound:
            continue
        if re.search(r"review|referral|testimonial|nps", wf.name, re.I):
            continue  # GHL010's lane
        if not UPSELL_LANE.search(f"{wf.name}\n{wf.bodies() or wf.text()}"):
            continue
        if _screens_for_churn(wf):
            continue
        yield _finding(
            "GHL098", "high", wf,
            "Upsell goes to refunded and cancelled customers too",
            "This workflow asks for more money — a renewal, an upgrade, "
            "another order — and nothing in it checks the marker this account "
            "already writes when a customer refunds, disputes or cancels. "
            "Those contacts are labelled, they are findable, and this ask goes "
            "to them anyway: 'ready to renew?' to somebody who cancelled last "
            "week, an upgrade offer to somebody whose payment is in dispute. "
            "It reads as a business that does not know who its own customers "
            "are, and it is the most likely message in the account to be "
            "screenshotted.",
            "Put a condition immediately before each send: if the refund / "
            "cancelled / complaint marker is present, exit. Check it before "
            "EACH send rather than only at enrollment — this workflow waits, "
            "and somebody who was fine on day 1 can have cancelled by day 30. "
            "Verify by tagging a test contact and confirming they drop out.",
            cost="One upsell to a disputing customer costs more than the "
                 "campaign earns from the rest — it is the message that turns "
                 "a quiet refund into a public complaint.")


# --------------------------------------------------------------------------
# GHL099 — the pipeline that only ever fills
# --------------------------------------------------------------------------

def _opportunity_action(step: Step) -> str:
    """"create", "advance" or "" for what this step does to an opportunity."""
    nk = _nk(step.type)
    if "opportunit" not in nk and "deal" not in nk:
        return ""
    if any(k in nk for k in ("update", "edit", "move", "change", "stage",
                             "status", "advance", "won", "win", "lost",
                             "close")):
        return "advance"
    if any(k in nk for k in ("create", "add", "new")):
        return "create"
    return ""


def _pipeline_of(step: Step):
    cfg = step.config()
    if not isinstance(cfg, dict):
        return None
    for k, v in cfg.items():
        if _nk(k) in ("pipelineid", "pipeline") and isinstance(v, str) \
                and v.strip() and "{{" not in v:
            return v.strip()
    return None


@rule("GHL099", "Pipeline that workflows fill and never advance", "medium",
      "dead_weight", "revenue", "pipeline", "reporting")
def pipeline_only_ever_fills(acct: Account):
    """Opportunities created automatically, moved by nobody.

    Creating the opportunity is the easy half and the half every build does.
    Advancing it is the half that makes the pipeline mean anything, and it is
    routinely left to somebody remembering to drag a card. Meanwhile the events
    that PROVE the deal moved — the booking, the order, the payment — are
    already flowing through this account as triggers; they are just not being
    written to the deal. So the pipeline grows a stage-one column that only
    ever gets longer, conversion rate reads as near zero, and every number the
    owner is shown about their own sales is wrong in the same direction.

    Only pipelines this account both names and never touches are reported, and
    an update step that does not say which pipeline it targets stops the check
    entirely — that step could be moving any deal in the account, and a finding
    that ignores it would be a guess.
    """
    if not any(_has(CONVERSION_TRIGGER, t)
               for wf in acct.published() for t in wf.triggers):
        return

    creators: dict = {}
    advanced: set = set()
    for wf in acct.published():
        for step in wf.steps:
            action = _opportunity_action(step)
            if not action:
                continue
            pipe = _pipeline_of(step)
            if action == "advance":
                if pipe is None:
                    return  # unattributable; see the docstring
                advanced.add(pipe)
            elif pipe is not None:
                creators.setdefault(pipe, {})[wf.name] = wf

    names = acct.inventory.pipelines
    for pipe in sorted(creators):
        if pipe in advanced:
            continue
        wfs = [creators[pipe][n] for n in sorted(creators[pipe])]
        label = names.get(str(pipe)) or pipe
        yield _finding(
            "GHL099", "medium", wfs[0],
            f"'{label}' is filled by automation and moved by nobody",
            f"{len(wfs)} published workflow"
            f"{'s create' if len(wfs) != 1 else ' creates'} opportunities on "
            f"'{label}', and no workflow in this account ever moves, closes or "
            "updates one. The deals pile up in whatever stage they were "
            "created in, so the pipeline view shows a growing first column and "
            "a conversion rate that reads close to zero — while the events "
            "that would advance those deals (a booking, an order, a payment) "
            "are already arriving here as triggers and simply are not written "
            "to the record. If the team genuinely moves every card by hand "
            "this is a reporting note rather than a defect, and worth writing "
            "into the build docs so the next person does not rebuild it.",
            "Add the stage moves to the workflows that already own those "
            "events: the booking workflow moves the deal to Booked, the order "
            "workflow marks it Won. Then compare the pipeline against the "
            "calendar for last month — the gap between them is how wrong the "
            "reporting has been.",
            step=", ".join(w.name for w in wfs[1:]),
            reach=sum(len(w.outbound) for w in wfs),
            cost="Every sales number the owner looks at is understated, and "
                 "nobody can tell which channel or which campaign produced the "
                 "revenue — the pipeline holds the deals but not the outcome.")


# --------------------------------------------------------------------------
# GHL100 — leads bought and arriving anonymous
# --------------------------------------------------------------------------

# Fields that record where a lead came from. Matched on the slug with underscore
# boundaries, so "lead_source" and "utm_campaign" are caught and a field called
# "resource_pack" is not.
SOURCE_FIELD = re.compile(
    r"(?:^|_)(?:source|utm|utm_source|utm_medium|utm_campaign|referrer|"
    r"referral|attribution|channel|campaign|vendor|partner|origin|"
    r"how_did_you_hear|hdyh)(?:_|$)")
SOURCE_TAG = re.compile(
    r"source|utm|referr|vendor|partner|campaign|channel|origin|attribution",
    re.I)

# Triggers that mean a lead arrived from OUTSIDE the platform's own capture. A
# form or funnel submission already carries HighLevel's attribution; a contact
# pushed in over a webhook carries whatever the sender chose to put in the
# payload, which for most lead vendors and most Zaps is nothing at all.
PUSHED_IN_TRIGGER = re.compile(r"inbound[_ -]?webhook|webhook[_ -]?received|"
                               r"incoming[_ -]?webhook", re.I)

# Webhooks carry everything, not just leads: a Stripe chargeback, an order from
# the store, a calendar sync. A payload that names a transaction or an
# appointment is about somebody who is already in the database, and asking where
# THAT lead came from is a question about the wrong event.
NOT_A_LEAD_EVENT = re.compile(
    r"refund|charge|dispute|payment|invoice|order|purchase|subscription|"
    r"renewal|appointment|appt|booking|calendar|cancel|unsubscrib|bounce|"
    r"complain|fulfil|fulfill|shipment|delivery", re.I)

WORKS_THE_LEAD = re.compile(
    r"internal[_ -]?notification|notification|notify|slack|alert|assign|"
    r"round[_ -]?robin|create[_ -]?task", re.I)


def _records_a_source(wf: Workflow) -> bool:
    for step in wf.steps:
        for key, _value in _field_writes(step):
            if SOURCE_FIELD.search(key):
                return True
        if any(SOURCE_TAG.search(t) for t in _tags(step)):
            return True
    return False


def _works_the_lead(wf: Workflow) -> bool:
    if wf.outbound:
        return True
    for step in wf.steps:
        if _opportunity_action(step):
            return True
        if WORKS_THE_LEAD.search(f"{step.type} {step.name}"):
            return True
    return False


@rule("GHL100", "Leads pushed in by webhook with no source recorded", "medium",
      "hygiene", "revenue", "attribution")
def webhook_intake_with_no_attribution(acct: Account):
    """A lead arrives from outside, and nothing writes down where from.

    A contact created by a form or a funnel arrives carrying HighLevel's own
    attribution. A contact pushed in over an inbound webhook — a lead vendor, a
    Zap, an ad platform, a partner's site — carries whatever the sender put in
    the payload, and the sender is usually another business with no reason to
    care. Unless this workflow stamps the source itself, every one of those
    leads lands identical to every other, and from that point on the account
    cannot answer the only question the owner actually asks: which of these is
    worth paying for. It is invisible for months, because nothing is broken —
    the leads work fine, they just cannot be told apart, and by the time
    somebody wants the answer the history is unrecoverable.

    If the sending system already sets the contact's Source on create, this is
    a note rather than a defect — worth confirming once and writing down.
    """
    for wf in acct.published():
        intake = [t for t in wf.triggers if _has(PUSHED_IN_TRIGGER, t)
                  and not _has(NOT_A_LEAD_EVENT, t)]
        if not intake:
            continue
        if not _works_the_lead(wf):
            continue
        if _records_a_source(wf):
            continue
        yield _finding(
            "GHL100", "medium", wf,
            "Webhook leads arrive with nothing recording where they came from",
            "Leads enter this workflow over a webhook, which means they were "
            "created by something outside this account — a lead vendor, an "
            "automation, a partner site — and nothing here writes down which. "
            "No source field, no source tag. Once they are in the database "
            "they are indistinguishable from every other contact, so nothing "
            "in the account can compare one supplier against another, work out "
            "a cost per booked call, or tell the owner which spend to stop. "
            "The leads themselves are fine, which is why this runs for a year "
            "before anyone notices — and the months already lost cannot be "
            "reconstructed afterwards.",
            "Add a field write at the top of this workflow that stamps the "
            "source, and a tag if you segment on it. Map it from the payload "
            "where the sender provides one; where they do not, hardcode the "
            "source per webhook and give each supplier its own inbound "
            "webhook, which is the version that keeps working when a second "
            "vendor is added. Verify on the next real lead by opening the "
            "contact and checking the field is populated.",
            step=intake[0].name or intake[0].type,
            cost="Ad and lead-vendor spend cannot be judged. The account keeps "
                 "buying from whoever invoices most confidently, because there "
                 "is no number in it that says otherwise.")
