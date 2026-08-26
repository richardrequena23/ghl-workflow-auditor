"""Calendar and booking mechanics — the timing layer wrapped around a slot.

Booking is the thing agencies build most often and get subtly wrong most often,
because every defect in it is invisible in the builder and only surfaces weeks
later as a no-show nobody can explain. Four of these six ask the same question:
is this workflow's timing anchored to the APPOINTMENT, or only to the moment
somebody booked it? A reminder released by a fixed wait (GHL065), a ladder whose
appointment offsets run backwards (GHL066), no-show copy sent before the
appointment has happened (GHL069) and a ladder that only speaks inside the last
few hours (GHL070) are four ways of getting that wrong. The other two are about
what the message says: a booking link that lands somewhere this account cannot
see (GHL067) and an appointment time quoted with no timezone (GHL068). All six
produce one customer experience — a message about a meeting that does not match
the meeting. GHL001 owns the unfiltered appointment trigger and GHL028 owns the
missing cancellation exit; neither is repeated here, and the lanes they own are
deliberately excluded below.

Booking copy is also where a careless regex does the most damage, because the
same words appear in correct messages: a cancellation policy says "24 hours
before your appointment", a confirmation quotes a "no-show fee", an address
says "see you at". Each check below therefore reads copy in sentences and only
after the STRUCTURE of the workflow has established what kind of lane it is.
"""

from __future__ import annotations

import json
import re

from ..model import URL, Account, Step, Workflow, slug
from ..rules import Skip, _finding, rule


def _nk(key) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


def _sentences(text: str) -> list:
    """Copy split the way a reader takes it in.

    Every copy check here is sentence-scoped. Matching a phrase in one sentence
    against a qualifier in another is how "a no-show fee applies" and "sorry we
    missed you" end up counted as the same message.
    """
    return [s for s in re.split(r"[.!?\n;]+", str(text)) if s.strip()]


# Minutes per unit. Months are 30 days: nothing here is arithmetic on a real
# calendar, it is a comparison between two offsets, and the ordering is the
# same at 28, 30 or 31.
_UNITS = {"second": 1 / 60, "sec": 1 / 60, "minute": 1.0, "min": 1.0,
          "hour": 60.0, "hr": 60.0, "day": 1440.0, "week": 10080.0,
          "month": 43200.0}

_DURATION_TEXT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|"
    r"weeks?|months?)", re.I)

# "24 hours before", "1 hour prior to the call", "2 days after". A duration
# written with a direction word is only meaningful against a fixed moment, so
# this doubles as evidence that the wait is anchored to the appointment at all.
_RELATIVE_PHRASE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"
    r"[^\d\n]{0,24}?\b(before|prior|ahead|after|following|past|later)\b", re.I)

# What a wait says when it is measured off the slot rather than off the clock.
_ANCHOR_WORDS = re.compile(
    r"appointment|appt|event[_ -]?start|start[_ -]?of[_ -]?event|"
    r"booking[_ -]?time|slot[_ -]?time|calendar[_ -]?event", re.I)

_DIRECTION_KEYS = ("direction", "when", "relation", "offsetdirection",
                   "beforeafter")
_BEFORE_KEYS = ("before", "beforeappointment", "beforestart", "leadtime")
_AFTER_KEYS = ("after", "afterappointment", "afterstart")
_BEFORE_WORDS = ("before", "prior", "ahead")

# Keys a plain "wait N units" carries. Their presence settles what the step is:
# a duration wait counts from enrollment no matter how its label is worded.
_DURATION_KEYS = ("delay", "duration", "waitduration", "timedelay", "waittime",
                  "delayamount", "waitfor")


def _to_minutes(value, unit):
    """Magnitude only — direction is always carried separately and signed on."""
    try:
        magnitude = abs(float(value))
    except (TypeError, ValueError):
        return None
    stem = re.sub(r"[^a-z]", "", str(unit).lower())
    if stem.endswith("s") and stem[:-1] in _UNITS:
        stem = stem[:-1]
    factor = _UNITS.get(stem)
    return None if factor is None else magnitude * factor


def _magnitude(value, container) -> float | None:
    """How long this value is, in minutes, with no opinion on direction.

    A bare number is only readable when something alongside it names the unit.
    `{"before": 24}` with no unit could be hours or days, and picking one is
    the kind of guess that puts a wrong number in a client's report.
    """
    if isinstance(value, dict):
        for key in ("value", "amount", "duration", "delay", "number"):
            for k in value:
                if _nk(k) == key:
                    unit = next((value[u] for u in value
                                 if _nk(u) in ("unit", "units", "period")), None)
                    return _to_minutes(value[k], unit)
        return None
    if isinstance(value, str):
        m = _DURATION_TEXT.search(value)
        return _to_minutes(m.group(1), m.group(2)) if m else None
    if isinstance(value, bool):
        return None  # `{"before": true}` is a flag, not a length of time
    if isinstance(value, (int, float)) and isinstance(container, dict):
        unit = next((container[u] for u in container
                     if _nk(u) in ("unit", "units", "period")), None)
        return _to_minutes(value, unit) if unit is not None else None
    return None


def _offset_from_string(text: str) -> float | None:
    m = _DURATION_TEXT.search(text)
    if not m:
        return None
    minutes = _to_minutes(m.group(1), m.group(2))
    if minutes is None:
        return None
    low = text.lower()
    if re.search(r"\b(before|prior|ahead)\b", low) or re.search(r"-\s*\d", text):
        return -minutes
    if re.search(r"\b(after|following|past|later)\b", low):
        return minutes
    return None  # a duration with no stated direction says nothing


def _offset_from_dict(node: dict) -> float | None:
    """A signed appointment offset declared by one dict, or None."""
    keys = {_nk(k): k for k in node}

    # {"offsetMinutes": -1440} — the unit is in the key and the sign IS the
    # direction. This is how a generated export writes it.
    for nk, real in keys.items():
        m = re.fullmatch(r"offset(second|sec|minute|min|hour|hr|day|week)s?", nk)
        if m and isinstance(node[real], (int, float)) \
                and not isinstance(node[real], bool):
            minutes = _to_minutes(node[real], m.group(1))
            if minutes is not None:
                return -minutes if node[real] < 0 else minutes

    # {"hoursBefore": 24} / {"minutesAfter": 90}
    for nk, real in keys.items():
        m = re.fullmatch(
            r"(second|sec|minute|min|hour|hr|day|week)s?(before|after)", nk)
        if m:
            minutes = _to_minutes(node[real], m.group(1))
            if minutes is not None:
                return -minutes if m.group(2) == "before" else minutes

    # {"before": "24 hours"} — the key names the direction.
    for nk in _BEFORE_KEYS:
        if nk in keys:
            minutes = _magnitude(node[keys[nk]], node)
            if minutes is not None:
                return -minutes
    for nk in _AFTER_KEYS:
        if nk in keys:
            minutes = _magnitude(node[keys[nk]], node)
            if minutes is not None:
                return minutes

    direction = ""
    for nk in _DIRECTION_KEYS:
        if nk in keys and isinstance(node[keys[nk]], str):
            direction = node[keys[nk]].strip().lower()
            break

    # {"value": 24, "unit": "hours", "direction": "before"}
    if "value" in keys and any(u in keys for u in ("unit", "units", "period")):
        unit_key = next(keys[u] for u in ("unit", "units", "period") if u in keys)
        minutes = _to_minutes(node[keys["value"]], node[unit_key])
        if minutes is not None and direction:
            return -minutes if any(w in direction for w in _BEFORE_WORDS) \
                else minutes

    for nk in ("offset", "waitoffset", "relativeoffset", "startoffset",
               "timeoffset", "delay", "startafter"):
        if nk in keys and isinstance(node[keys[nk]], str):
            signed = _offset_from_string(node[keys[nk]])
            if signed is not None:
                return signed
            if direction:
                minutes = _magnitude(node[keys[nk]], node)
                if minutes is not None:
                    return -minutes if any(w in direction for w in _BEFORE_WORDS) \
                        else minutes
    return None


def _scan_offset(node) -> float | None:
    if isinstance(node, dict):
        found = _offset_from_dict(node)
        if found is not None:
            return found
        for value in node.values():
            found = _scan_offset(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _scan_offset(value)
            if found is not None:
                return found
    return None


def _has_plain_duration(cfg: dict) -> bool:
    """Does this wait carry a configured "wait N units" setting?

    It settles an argument the step's label cannot. "Wait 1 day after booking"
    is an ordinary drip wait wearing a descriptive name, and reading that name
    as an appointment offset turns a correct sequence into a reversed reminder
    ladder — the exact false report this pack exists to avoid. A configured
    duration with no direction on it is proof the step counts forward from
    enrollment.
    """
    if not isinstance(cfg, dict):
        return False
    for key, value in cfg.items():
        if _nk(key) not in _DURATION_KEYS:
            continue
        if isinstance(value, dict):
            inner = {_nk(k) for k in value}
            if inner & {"value", "amount", "number", "duration"} and \
                    not inner & set(_DIRECTION_KEYS):
                return True
        elif isinstance(value, str):
            if _DURATION_TEXT.search(value) and \
                    not re.search(r"before|prior|ahead|after", value, re.I):
                return True
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False


def _appointment_offset(step: Step):
    """(anchored, signed minutes) for a wait measured against the appointment.

    Negative is before the start, positive after it. Minutes of None means the
    wait is clearly anchored to the slot but the export does not say by how
    much — which is not the same as zero, and no check below may treat it as
    one.
    """
    try:
        if not step.is_wait and "wait" not in _nk(step.type):
            return False, None
        cfg = step.config()
        cfg = cfg if isinstance(cfg, dict) else {}
        blob = json.dumps(cfg, default=str) + " " + step.name + " " + step.type
    except (TypeError, ValueError):
        return False, None

    anchored = bool(_ANCHOR_WORDS.search(blob)) \
        or "eventstartwait" in _nk(step.type)
    phrase = _RELATIVE_PHRASE.search(step.name) or _RELATIVE_PHRASE.search(blob)
    if phrase and not anchored:
        # A label is only proof of anchoring when it counts BACKWARDS. Nothing
        # can wait until "24 hours before" a moment unless it knows the moment,
        # so that phrase can only mean the appointment. "2 days after" is how
        # every ordinary drip wait is described, and treating it as an
        # appointment offset is what made a plain "Wait 1 day after booking"
        # read as a rung of the reminder ladder.
        if phrase.group(3).lower() in _BEFORE_WORDS and not _has_plain_duration(cfg):
            anchored = True
        else:
            phrase = None
    if not anchored:
        return False, None

    minutes = _scan_offset(cfg)
    if minutes is None and phrase:
        magnitude = _to_minutes(phrase.group(1), phrase.group(2))
        if magnitude is not None:
            minutes = -magnitude if phrase.group(3).lower() in _BEFORE_WORDS \
                else magnitude
    return True, minutes


def _appointment_triggers(wf: Workflow) -> list:
    return [t for t in wf.triggers
            if re.search(r"appointment|appt|booked", t.type, re.I)]


def _is_booking_lane(wf: Workflow) -> bool:
    """Is this workflow about a booked slot at all?

    Copy alone cannot answer it. "Sorry we missed your call" is no-show copy in
    a reminder ladder and correct speed-to-lead copy in a missed-call text-back
    — the sentence is identical and only the lane tells them apart. So the lane
    has to be established from structure: an appointment trigger, a wait
    anchored to the slot, or a step that names a calendar.
    """
    if _appointment_triggers(wf):
        return True
    for step in wf.steps:
        if _appointment_offset(step)[0]:
            return True
        try:
            if any(kind == "calendar" for kind, _ in step.entity_refs()):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _trigger_blob(triggers) -> str:
    try:
        return " ".join(t.filter_blob() for t in triggers)
    except (TypeError, ValueError):
        return ""


def _trigger_evidence(wf: Workflow) -> str:
    """Everything a trigger says about itself: type, name, filters, tags.

    The filter blob alone misses the commonest no-show pattern in the wild,
    where one workflow tags the contact on the no-show status and a second
    workflow is triggered by that tag. The tag name is the gate, and it is not
    a filter value on an appointment trigger.
    """
    parts = []
    for trg in wf.triggers:
        try:
            parts.append(f"{trg.type} {trg.name} {trg.filter_blob()}")
            parts.extend(trg.tag_values())
        except (TypeError, ValueError):
            continue
    return " ".join(parts)


def _parent_map(wf: Workflow) -> dict:
    """child step id -> parent step id, from whichever wiring the export used.

    GoHighLevel's advanced builder flattens branch children into the same step
    list as everything else and records the branch in `parentKey` as
    `<parentId>-<branchName>`, so the longest step id that prefixes the key is
    the real parent.
    """
    ids = sorted({s.step_id for s in wf.steps if s.step_id}, key=len,
                 reverse=True)
    out: dict = {}
    for step in wf.steps:
        key = step.parent_key
        if not key:
            continue
        for sid in ids:
            if key == sid or key.startswith(sid):
                if sid != step.step_id:
                    out[step.step_id] = sid
                break
    for step in wf.steps:
        for nxt in step.next_ids():
            if nxt != step.step_id:
                out.setdefault(nxt, step.step_id)
    return out


def _runs_after(parents: dict, later: Step, earlier: Step) -> bool:
    seen = set()
    current = parents.get(later.step_id)
    while current and current not in seen:
        if current == earlier.step_id:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def _provably_sequential(wf: Workflow, parents: dict, first_index: int,
                         second_index: int, first: Step, second: Step) -> bool:
    """Do these two waits sit on ONE path through the workflow?

    Branch children are flattened into the step list, so two waits that read as
    consecutive in the export are often the two halves of an If/Else — one for
    same-day bookings, one for everything else. Those never run in sequence,
    and comparing them reports a reversal that does not exist. Where the export
    carries wiring the ancestry answers it; where it does not, a branch step
    anywhere between them is enough to make the order unknowable, and unknown
    has to mean quiet.
    """
    if parents:
        return _runs_after(parents, second, first)
    return not any(s.is_branch for s in wf.steps[first_index + 1:second_index])


def _human(minutes: float) -> str:
    """'24 hours', '15 minutes' — for a symptom a business owner reads."""
    span = abs(minutes)
    for unit, factor in (("day", 1440.0), ("hour", 60.0), ("minute", 1.0)):
        if span >= factor:
            count = span / factor
            shown = int(count) if abs(count - round(count)) < 0.01 else round(count, 1)
            return f"{shown} {unit}{'s' if shown != 1 else ''}"
    return f"{round(span * 60)} seconds"


# --------------------------------------------------------------------------
# GHL065 — the reminder that is not measured against the appointment
# --------------------------------------------------------------------------

# The nouns a message uses when it is talking about the booked slot. A time
# claim only means "your appointment is then" if the sentence is about the
# appointment; otherwise it is about paperwork, delivery, or office hours.
_APPT_NOUN = re.compile(
    r"\b(appointments?|appts?|calls?|sessions?|meetings?|consult\w*|demos?|"
    r"slots?|visits?|bookings?|zoom)\b", re.I)

# Copy that states WHEN, relative to reading the message. "today" is
# deliberately absent: "sorry we missed you today" is no-show copy, not a
# reminder. So is a bare "N hours before" — that is the cancellation policy
# every confirmation carries, not a statement of when the call is.
_TIME_CLAIM = re.compile(
    r"\btomorrow\b|\bstarts? in\b|\bstarting in\b|\bcoming up in\b|"
    r"\bin (?:an?|\d+) ?(?:hours?|minutes?|mins?|days?)\b|"
    r"\b\d+\s*(?:hours?|minutes?|days?)\s*from now\b", re.I)

# Claims that need no noun beside them, because there is only one thing they
# can be about.
_SELF_EVIDENT_CLAIM = re.compile(
    r"\bsee you tomorrow\b|\btalk (?:to you )?tomorrow\b|"
    r"\bsee you in (?:an?|\d+) ?(?:hours?|minutes?|mins?|days?)\b", re.I)

# A step LABEL that names a lead time. A builder only writes "24 hours before"
# on a step they meant to fire relative to the appointment, and unlike message
# copy a label is never a cancellation policy.
_REMINDER_LABEL = re.compile(
    r"\b\d+\s*(?:hours?|hrs?|minutes?|mins?|days?)\s*"
    r"(?:before|prior|ahead|out)\b", re.I)


def _claims_when_the_appointment_is(text: str) -> bool:
    """Does this copy tell the contact when their appointment is?

    Sentence-scoped, and the sentence has to name the meeting. Without that,
    "give us 24 hours before your appointment" (a cancellation policy), "see
    you at 4400 Main Street" (an address) and "we will email the forms in a day
    or two" (paperwork) all read as reminders — three correct messages reported
    as a critical fault.
    """
    for sentence in _sentences(text):
        if _SELF_EVIDENT_CLAIM.search(sentence):
            return True
        if _TIME_CLAIM.search(sentence) and _APPT_NOUN.search(sentence):
            return True
    return False


@rule("GHL065", "Appointment reminder timed from the booking, not the "
      "appointment", "critical", "routing", "appointments", "timing")
def reminder_timed_off_the_booking(acct: Account):
    """A "1 hour reminder" released by a fixed-duration wait.

    HighLevel's Wait action can hold a contact until a set time relative to the
    APPOINTMENT. A plain duration wait cannot: it counts from the moment the
    contact entered, which is the moment they booked. Book three weeks out and
    a ladder written as "wait 1 day, then send the 24-hour reminder" delivers
    that reminder twenty days early — and the copy names a time it is nowhere
    near. It looks correct in the builder because the builder tested it with a
    booking for tomorrow.
    """
    for wf in acct.published():
        appts = _appointment_triggers(wf)
        if not appts:
            continue
        blob = _trigger_blob(appts)
        # The cancellation and no-show lanes are not reminder ladders — their
        # appointment is already behind them. GHL028 and GHL069 own those.
        if re.search(r"cancel|no[- _]?show", blob):
            continue
        waits: list[Step] = []
        for step in wf.steps:
            if step.is_wait:
                waits.append(step)
                continue
            if not step.is_outbound or not waits:
                continue
            try:
                body = step.bodies()
            except (TypeError, ValueError):
                continue
            if not (_claims_when_the_appointment_is(body)
                    or _REMINDER_LABEL.search(step.name)):
                continue
            if any(_appointment_offset(w)[0] for w in waits):
                continue
            yield _finding(
                "GHL065", "critical", wf,
                "A reminder counted from the booking, not from the appointment",
                "This message tells the contact when their appointment is, and "
                "the only thing holding it back is a fixed wait — which starts "
                "counting the moment they booked, not the appointment. Someone "
                "who books three weeks out gets the reminder the day after "
                "booking, saying their call is tomorrow. It reads correct in "
                "the builder because the builder was tested with a booking for "
                "tomorrow, and it is wrong for every contact who books further "
                "ahead than the wait is long.",
                "Change the wait to the appointment-relative kind — hold until "
                "a set time BEFORE the appointment start — instead of a "
                "duration. Then book a slot two weeks out and confirm the "
                "reminder does not arrive until the day before it.",
                step=step.name or step.type,
                cost="Reminders arrive days early quoting a time that has not "
                     "come, and nothing reminds anyone on the actual day. You "
                     "pay for the no-show and for the message that caused it.")
            break


# --------------------------------------------------------------------------
# GHL066 — a ladder whose rungs are in the wrong order
# --------------------------------------------------------------------------

@rule("GHL066", "Reminder ladder's appointment offsets run backwards", "high",
      "routing", "appointments", "timing")
def reminder_offsets_run_backwards(acct: Account):
    """Two appointment-relative waits where the second targets an earlier moment.

    Waits anchored to a slot have to march toward it: 24 hours before, then 1
    hour before, then after. Reorder two steps in the builder — which is one
    drag — and the ladder asks to wait until 24 hours before an appointment the
    contact has already been held to within an hour of. That moment is gone by
    the time they reach it, so the step either releases immediately (both
    reminders land seconds apart) or never releases at all. Either way the
    sequence does not do what its labels say, and nothing in the builder draws
    attention to it.
    """
    for wf in acct.published():
        ladder = []
        for i, step in enumerate(wf.steps):
            anchored, minutes = _appointment_offset(step)
            if anchored and minutes is not None:
                ladder.append((i, step, minutes))
        if len(ladder) < 2:
            continue
        parents = _parent_map(wf) if wf.has_wiring else {}
        for (pi, prev, before), (i, step, after) in zip(ladder, ladder[1:]):
            if after > before:
                continue
            if not _provably_sequential(wf, parents, pi, i, prev, step):
                continue
            yield _finding(
                "GHL066", "high", wf,
                f"'{step.name or step.type}' waits for a moment that has "
                "already passed",
                f"This wait targets {_human(after)} "
                f"{'before' if after < 0 else 'after'} the appointment, and the "
                f"wait above it already held the contact to {_human(before)} "
                f"{'before' if before < 0 else 'after'} it. The second target "
                "is in the past by the time anyone reaches it, so the step "
                "either releases at once — two reminders in the same minute, "
                "the later one quoting the wrong lead time — or holds the "
                "contact for an appointment moment that will never come round "
                "again. Both are wrong, and the ladder still reads correctly "
                "in the builder.",
                "Put the waits back in order, furthest from the appointment "
                "first: 24 hours before, then 1 hour before, then anything "
                "after. Then run one real booking through and check the "
                "timestamps on the two messages.",
                step=step.name or step.type,
                reach=len(wf.outbound_after(i)),
                cost="The reminder ladder collapses into one burst or stalls "
                     "entirely. Either the contact is told twice in a minute "
                     "or they are never reminded at all — and the no-show "
                     "looks like their fault.")
            break


# --------------------------------------------------------------------------
# GHL067 — booking links that leave the account
# --------------------------------------------------------------------------

# HighLevel's booking widget path. The host varies — api.leadconnectorhq.com,
# link.msgsndr.com, or the client's own white-labelled domain — but the
# /widget/booking/<calendarId> shape does not.
_GHL_BOOKING_WIDGET = re.compile(
    r"https?://[^\s/]+/widget/(?:booking|bookings|appointment)/"
    r"([A-Za-z0-9_-]{4,})", re.I)

# A HighLevel object id: base62, no separators, twenty-odd characters. The
# widget path also accepts a human-readable slug, and a slug cannot be checked
# against a list of ids — calling `book.theclient.com/widget/booking/discovery`
# a dead calendar because "discovery" is not an id would be a guess, and a
# loud one. Only id-shaped tokens are judged.
_GHL_OBJECT_ID = re.compile(r"^[A-Za-z0-9]{18,}$")

# Third-party schedulers, by host. Only unambiguous ones: a Google Calendar
# link can be a hundred things, an acuityscheduling.com link is exactly one.
_EXTERNAL_SCHEDULER = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*("
    r"calendly\.com|acuityscheduling\.com|cal\.com|savvycal\.com|"
    r"youcanbook\.me|tidycal\.com|oncehub\.com|scheduleonce\.com|"
    r"setmore\.com|simplybook\.me|zcal\.co|appointlet\.com|chilipiper\.com|"
    r"meetings\.hubspot\.com|vcita\.com|booksy\.com)", re.I)


@rule("GHL067", "Booking link bypasses this account's calendar", "high",
      "hygiene", "appointments", "references")
def booking_link_bypasses_the_calendar(acct: Account):
    """A link that books somewhere the account's automation cannot see.

    Two shapes, one consequence. A third-party scheduler link means the
    appointment is created outside HighLevel: no appointment trigger fires, no
    confirmation, no reminder ladder, no no-show recovery, and the calendar
    reporting the client reads is missing those bookings entirely. A HighLevel
    widget link carrying a calendar id this location does not have is the same
    outcome by a different route — usually a snapshot or a copy-pasted message
    still pointing at the calendar it was built against.
    """
    inv = acct.inventory
    widget_hits: list = []
    seen_hosts: set = set()

    for wf in acct.published():
        for step in wf.outbound:
            try:
                body = step.bodies()
            except (TypeError, ValueError):
                continue
            for link in URL.findall(body):
                widget = _GHL_BOOKING_WIDGET.search(link)
                if widget:
                    if _GHL_OBJECT_ID.match(widget.group(1)):
                        widget_hits.append((wf, step, widget.group(1)))
                    continue
                external = _EXTERNAL_SCHEDULER.search(link)
                if not external:
                    continue
                # The same booking link appears three times in a normal email —
                # button, body copy, footer. One defect, one finding.
                host = external.group(1).lower()
                key = (wf.name, step.name or step.type, host)
                if key in seen_hosts:
                    continue
                seen_hosts.add(key)
                yield _finding(
                    "GHL067", "high", wf,
                    f"Booking link sends contacts to {host}, outside this "
                    "account",
                    "This message hands the contact a third-party booking "
                    "link, so the appointment is created outside HighLevel. "
                    "Nothing in this account ever learns about it: no "
                    "appointment trigger fires, no confirmation goes out, the "
                    "reminder ladder never runs, no-show recovery never runs, "
                    "and the booking does not appear in any report the client "
                    "reads. The bookings still happen — they are just "
                    "invisible to every automation built to protect them.",
                    "Point the link at this location's calendar (the booking "
                    "widget link from Calendars, held in a custom value so it "
                    "is changed in one place), and keep the third-party "
                    "calendar only as a synced source of busy time.",
                    step=step.name or step.type,
                    cost="Every appointment booked through this link runs with "
                         "no reminders and no follow-up, and never shows up in "
                         "the numbers. The no-show rate on them is whatever it "
                         "was before you had automation at all.")

    if not widget_hits:
        return
    if not inv.has("calendars"):
        yield Skip(
            rule="GHL067",
            title="Booking link bypasses this account's calendar",
            reason="Booking-widget links were found in message bodies, but the "
                   "location's calendar list was not supplied — so a link "
                   "pointing at a deleted calendar cannot be told apart from "
                   "one pointing at a calendar that simply was not exported.",
            needs="calendars in the input bundle (id + name)",
            category="hygiene")
        return

    known = set()
    for cal_id, cal_name in inv.calendars.items():
        known.add(str(cal_id))
        known.add(str(cal_id).lower())
        known.add(slug(cal_name))

    reported: set = set()
    for wf, step, calendar_id in widget_hits:
        if calendar_id in known or calendar_id.lower() in known \
                or slug(calendar_id) in known:
            continue
        key = (wf.name, calendar_id)
        if key in reported:
            continue
        reported.add(key)
        yield _finding(
            "GHL067", "critical", wf,
            f"Booking link points at calendar '{calendar_id}', which is not "
            "in this account",
            "This message contains a HighLevel booking link whose calendar id "
            "does not exist in this location. A contact who taps it does not "
            "reach a broken page they will report — they reach a page that "
            "cannot take their booking, and they leave. This is the classic "
            "leftover from a snapshot or a message copied out of the account "
            "it was built in: the link still looks right, and it has been "
            "losing bookings quietly ever since.",
            "Replace the id with this location's calendar (Calendars → the "
            "calendar → its booking widget link) and store it in a custom "
            "value so the next copy-paste cannot drift. If this is a calendar "
            "GROUP link rather than a single calendar, confirm the group id "
            "instead — that is the one benign reason an id would not be in "
            "the calendar list.",
            step=step.name or step.type,
            cost="Every contact who taps this link fails to book and does not "
                 "tell you. It is the most expensive kind of silent failure: "
                 "the lead was ready, and the last step took the offer away.")


# --------------------------------------------------------------------------
# GHL068 — an appointment time with no zone attached
# --------------------------------------------------------------------------

_APPT_TIME_MERGE = re.compile(
    r"\{\{\s*(?:appointment|appt|event)[._-]?"
    r"(?:start(?:_?(?:time|date|at))?|time|date|datetime|slot)\b", re.I)

_ZONE_TEXT = re.compile(r"time ?zone|\byour time\b|\blocal time\b", re.I)
# Uppercase only, deliberately: lowercased, "ct" and "mt" appear inside
# ordinary words and every reminder in the account would look compliant.
_ZONE_ABBR = re.compile(
    r"\b(?:E[SD]T|C[SD]T|M[SD]T|P[SD]T|AK[SD]T|HST|UTC|GMT|BST|"
    r"CES?T|AEST|IST|ET|CT|MT|PT)\b")
# The zone written out in words — "2pm Eastern Time", "(Pacific)". Plenty of
# well-built accounts spell it rather than abbreviate it, and reading those as
# missing a zone puts a finding on copy that is already correct.
_ZONE_NAME = re.compile(
    r"\b(?:eastern|central|mountain|pacific|atlantic|alaska|hawaii|"
    r"newfoundland|greenwich|british)\b[^.\n]{0,14}?"
    r"\b(?:time|standard|daylight|summer)\b", re.I)
_ZONE_PARENTHETICAL = re.compile(
    r"\(\s*(?:eastern|central|mountain|pacific|atlantic|alaska|hawaii|"
    r"e[sd]t|c[sd]t|m[sd]t|p[sd]t|utc|gmt)\s*\)", re.I)
_ZONE_IANA_IN_COPY = re.compile(
    r"\b(?:America|Europe|Asia|Africa|Australia|Pacific|Atlantic|Indian|US|"
    r"Etc)/[A-Za-z_]+")

_IANA_ZONE = re.compile(r"^[A-Za-z]+(?:_[A-Za-z]+)*/[A-Za-z0-9_+-]+$")
_FIXED_OFFSET = re.compile(r"^(?:UTC|GMT)\s*[+-]\d{1,2}(?::\d{2})?$", re.I)
_CONTACT_ZONE = ("contact", "contacttimezone", "contactstimezone", "lead",
                 "recipient", "user")


def _names_a_zone(body: str) -> bool:
    return bool(_ZONE_TEXT.search(body) or _ZONE_ABBR.search(body)
                or _ZONE_NAME.search(body) or _ZONE_PARENTHETICAL.search(body)
                or _ZONE_IANA_IN_COPY.search(body))


def _pinned_timezone(wf: Workflow) -> str:
    """A literal zone nailed to the workflow, rather than the contact's own."""
    settings = wf.settings if isinstance(wf.settings, dict) else {}
    for key, value in settings.items():
        if _nk(key) not in ("timezone", "timezonesource", "tz", "sendtimezone"):
            continue
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if _nk(candidate) in _CONTACT_ZONE:
            return ""
        if _IANA_ZONE.match(candidate) or _FIXED_OFFSET.match(candidate):
            return candidate
    return ""


@rule("GHL068", "Appointment time sent with no timezone", "high", "routing",
      "appointments", "copy")
def appointment_time_without_a_timezone(acct: Account):
    """"Your call is at 2:00pm" — whose 2pm?

    An appointment merge field renders an hour, not an hour and a zone. Anyone
    reading it assumes their own, and the moment the account and the contact
    are in different zones half the confirmations are silently wrong by one to
    three hours. The contact is not confused, which is the problem: they are
    confident, and they turn up at the wrong time. Every booking build that
    survives contact with a second timezone puts the zone in the copy.
    """
    for wf in acct.published():
        pinned = _pinned_timezone(wf)
        offenders = []
        for step in wf.outbound:
            try:
                body = step.bodies()
            except (TypeError, ValueError):
                continue
            if not _APPT_TIME_MERGE.search(body):
                continue
            if _names_a_zone(body):
                continue
            offenders.append(step)
        if not offenders:
            continue
        names = ", ".join(s.name or s.type for s in offenders)
        count = len(offenders)
        plural, verb = ("s", "quote") if count != 1 else ("", "quotes")
        # A finding that says "these messages" about one message reads as a
        # check that did not look at the account it is describing.
        many = count != 1
        merges = "These messages merge" if many else "This message merges"
        says = "say" if many else "says"
        if pinned:
            yield _finding(
                "GHL068", "high", wf,
                f"{count} message{plural} {verb} an appointment time with no "
                f"zone, on a workflow pinned to {pinned}",
                f"{merges} the appointment time and never {says} "
                f"which zone it is in, and the workflow itself is pinned to "
                f"{pinned} rather than following the contact. Whichever zone "
                "the platform renders that time in, the contact has no way to "
                "tell — and anyone outside "
                f"{pinned} reads an hour that is not theirs and believes it. "
                "They do not ask; they arrive at the wrong time.",
                "Add the zone to the copy right next to the time (the "
                "appointment timezone merge field, or the words spelled out), "
                "and set the workflow to the contact's timezone rather than a "
                "fixed one. Then send yourself one confirmation with your own "
                "contact record set to a different zone and read what arrives.",
                step=names, reach=count,
                cost="Out-of-zone clients turn up hours early or hours late "
                     "and count as no-shows. The closer loses the slot, and "
                     "the client is certain they did nothing wrong.")
            continue
        yield _finding(
            "GHL068", "medium", wf,
            f"{count} message{plural} {verb} an appointment time with no "
            "timezone",
            f"{merges} the appointment time and never {'name' if many else 'names'} "
            "the zone. Every contact reads it as their own local time. That is "
            "harmless while every contact shares one zone and starts costing "
            "appointments the first day one does not — and nothing about the "
            "workflow changes on that day, so nobody connects the two.",
            "Put the zone beside the time in the copy — the appointment "
            "timezone merge field, or the words spelled out — in the "
            "confirmation and in every reminder that repeats the time.",
            step=names, reach=count,
            cost="One misread hour is one missed appointment plus the slot it "
                 "occupied. It reads as a flaky client until it happens often "
                 "enough to be read as a flaky business.")


# --------------------------------------------------------------------------
# GHL069 — apologising for a meeting that has not happened yet
# --------------------------------------------------------------------------

_NOSHOW_COPY = re.compile(
    r"we missed you|sorry we missed|missed (?:you|your (?:call|appointment|"
    r"session|slot))|you missed|\bno[- ]?show\b|did ?n'?t (?:make it|show|"
    r"attend)|could ?n'?t make it|we did ?n'?t connect|"
    r"were ?n'?t able to (?:make|attend)", re.I)

# The same words, used as terms rather than as an apology. Every second
# confirmation message in a service business carries a no-show fee line, and it
# is not recovery copy — it is the policy, and it belongs in the confirmation.
_POLICY_CONTEXT = re.compile(
    r"\bfees?\b|charge[ds]?\b|\bcharging\b|polic(?:y|ies)|penalt|billed|"
    r"invoice|forfeit|deposit|\bcosts?\b|\brate\b", re.I)

# Statuses that only exist once the appointment is behind the contact.
_PAST_STATUS = re.compile(
    r"no[- _]?show|showed|attended|complete|cancel", re.I)
_PASSED_GATE = re.compile(
    r"no[- _]?show|showed|attend|appointment[_ ]?status|did[_ ]?they", re.I)
# An unambiguous "this contact already missed one" marker, safe to read on ANY
# trigger — a tag, a trigger name, a filter value.
_MISS_MARKER = re.compile(
    r"no[- _]?show|missed[- _]?appointment|did[- _]?not[- _]?attend|"
    r"didnt[- _]?attend", re.I)

# A workflow the phone system starts. "Sorry we missed your call" is the whole
# point of a missed-call text-back, and plenty of them also carry a booking
# link or a Book Appointment step — which is enough to make the workflow look
# like a booking lane. The trigger settles it, and GHL002 owns whether a call
# trigger is filtered correctly.
_CALL_TRIGGER = re.compile(r"call|voicemail", re.I)


def _reads_as_a_missed_appointment(text: str) -> bool:
    """Is this copy apologising for a miss, or quoting the no-show policy?"""
    for sentence in _sentences(text):
        if not _NOSHOW_COPY.search(sentence):
            continue
        if _POLICY_CONTEXT.search(sentence):
            continue
        return True
    return False


@rule("GHL069", "No-show follow-up with nothing proving the appointment "
      "passed", "critical", "routing", "appointments")
def noshow_copy_with_no_proof_the_appointment_passed(acct: Account):
    """"Sorry we missed you" on a lane that starts when the booking is made.

    Recovery copy belongs behind one of two gates: a trigger that only fires
    once the contact has missed something, or a wait that holds until after the
    appointment start. Built into the same lane as the confirmation and
    released by a plain duration wait, it goes to people whose appointment is
    still ahead of them — and it goes at the worst possible moment, to the
    warmest lead in the account. Only booking lanes are read, and never a lane
    the phone system starts — "sorry we missed your call" is the correct copy
    there, and a text-back that also offers a booking link would otherwise look
    like a booking lane. Workflows whose appointment trigger carries no filter
    at all are left to GHL001, which owns that shape.
    """
    for wf in acct.published():
        if not _is_booking_lane(wf):
            continue
        if any(_CALL_TRIGGER.search(t.type) for t in wf.triggers):
            continue
        appts = _appointment_triggers(wf)
        # An unfiltered appointment trigger is GHL001's finding, and reporting
        # the same workflow twice for one root cause dilutes both.
        if appts and any(not t.filters() for t in appts):
            continue
        if _PAST_STATUS.search(_trigger_blob(appts)):
            continue
        # The two-workflow pattern: one lane tags the contact on the no-show
        # status, a second is triggered by that tag. The tag IS the proof, and
        # it never appears as a filter on an appointment trigger.
        if _MISS_MARKER.search(_trigger_evidence(wf)):
            continue
        for i, step in enumerate(wf.steps):
            if not step.is_outbound:
                continue
            try:
                body = step.bodies()
            except (TypeError, ValueError):
                continue
            if not _reads_as_a_missed_appointment(body + "\n" + step.name):
                continue
            if _proves_the_appointment_passed(wf, i):
                break
            yield _finding(
                "GHL069", "critical", wf,
                "No-show copy sent with nothing checking the appointment "
                "happened",
                "This message apologises for a missed appointment, and nothing "
                "above it establishes that the appointment is over. The "
                "workflow is entered when the booking is made and released by "
                "a plain wait, so a contact who books further ahead than the "
                "wait is long is told they were a no-show while their call is "
                "still in the future. They are your warmest lead, they read it "
                "as being written off, and a good share of them cancel rather "
                "than argue.",
                "Gate the recovery copy: either move it to its own workflow "
                "triggered on Appointment Status = no-show, or hold it behind "
                "a wait that runs until after the appointment start and an "
                "If/Else on the appointment status. Confirmations and "
                "recovery should not share one lane.",
                step=step.name or step.type,
                cost="The lead who booked is told they did not show up, "
                     "before their call. That message costs the appointment "
                     "it was written to recover.")
            break


def _proves_the_appointment_passed(wf: Workflow, index: int) -> bool:
    """Is there anything above this step that only clears once the slot is over?"""
    for step in wf.steps[:index]:
        anchored, minutes = _appointment_offset(step)
        if anchored and (minutes is None or minutes >= 0):
            # Anchored with no readable offset still means the builder tied
            # this wait to the slot, and calling that ungated would be the
            # guess. Anchored and positive is the real gate.
            return True
        if step.is_branch and _PASSED_GATE.search(step.name + " " + step.text()):
            return True
    return False


# --------------------------------------------------------------------------
# GHL070 — reminders that arrive too late to act on
# --------------------------------------------------------------------------

# Twelve hours = the night before. A reminder that lands inside the same
# working day cannot be acted on: the contact is already committed to the day
# they have, so the ladder can only document the no-show, not prevent it. The
# reminder that actually recovers a booking is the one that arrives while
# rescheduling is still easier than cancelling.
_RESCHEDULE_WINDOW_MINUTES = 720
# One touch inside two hours is not a ladder, it is a ping.
_LAST_MINUTE_MINUTES = 120


@rule("GHL070", "Reminder ladder gives no time to reschedule", "medium",
      "routing", "appointments", "timing")
def reminders_leave_no_time_to_reschedule(acct: Account):
    """Every reminder in the ladder lands inside the last few hours.

    A reminder has one job: convert a contact who has forgotten, or whose day
    has changed, into a reschedule instead of an empty slot. That only works
    while there is still time to move the call. A ladder whose earliest touch
    is fifteen minutes out has no effect on attendance at all — it arrives
    after the decision, and its only real output is a record that the person
    was told. Advisory rather than alarming: a same-day booking flow can be
    built this way deliberately, and the day-before touch may be sitting in
    another workflow or on the calendar's own notification settings, neither of
    which a workflow export can see.
    """
    for wf in acct.published():
        reminders = []
        for i, step in enumerate(wf.steps):
            anchored, minutes = _appointment_offset(step)
            if not anchored or minutes is None or minutes >= 0:
                continue
            if not wf.outbound_after(i):
                continue  # a wait with nothing below it reminds nobody
            reminders.append((i, step, minutes))
        if not reminders:
            continue
        earliest = min(m for _, _, m in reminders)
        if -earliest >= _RESCHEDULE_WINDOW_MINUTES:
            continue
        names = ", ".join(s.name or s.type for _, s, _ in reminders)
        first_index = min(i for i, _, m in reminders if m == earliest)
        # "Nothing earlier" has to be literally true before it is said. A
        # workflow that sends the confirmation at booking time and nudges an
        # hour out has spoken to the contact twice, and calling that a single
        # last-minute reminder reads as a check that did not look.
        nothing_earlier = not any(s.is_outbound for s in wf.steps[:first_index])
        if len(reminders) == 1 and -earliest <= _LAST_MINUTE_MINUTES \
                and nothing_earlier:
            yield _finding(
                "GHL070", "high", wf,
                f"One reminder, {_human(earliest)} before the appointment — "
                "and nothing earlier",
                f"Nothing in this booking workflow reaches the contact until "
                f"{_human(earliest)} before the appointment. By then the "
                "contact's day is already decided: if they have forgotten, "
                "double-booked, or need a different time, there is nothing "
                "they can do with the message except not turn up. A reminder "
                "this late does not change attendance, it only records that "
                "someone was told.",
                "Add a touch the day before — 24 hours out is the one that "
                "recovers bookings, because a reschedule is still easier than "
                "a cancellation at that point — and keep this one as the "
                "final nudge. Give both a reply path so a reschedule can "
                "actually happen. If a day-before reminder already goes out "
                "from another workflow or from the calendar's own "
                "notification settings, note that and move on.",
                step=names, reach=len(wf.outbound),
                cost="This ladder does not reduce no-shows, it documents "
                     "them. Every slot lost here was recoverable a day "
                     "earlier for the price of one text.")
            continue
        count = len(reminders)
        yield _finding(
            "GHL070", "medium", wf,
            f"Every reminder lands within {_human(earliest)} of the "
            "appointment",
            f"{count} appointment-timed reminder{'s' if count != 1 else ''} in "
            f"this workflow, and the earliest is {_human(earliest)} before the "
            "start. Reminders bunched in the final stretch reach the contact "
            "after the point where a reschedule is easier than a no-show, so "
            "they raise the message count without moving attendance. If this "
            "is a same-day booking flow, or the day-before touch lives in "
            "another workflow, that is a reasonable design — which is why this "
            "is advisory.",
            "Move the first touch out to roughly 24 hours before and leave one "
            "close to the start. Two touches spread that way outperform three "
            "in the last hour. Check the calendar's own reminder settings "
            "before adding a second one, so the contact is not told twice.",
            step=names, reach=len(wf.outbound),
            cost="No-shows this ladder could have caught still happen, and the "
                 "extra messages land where they cannot change anything.")
