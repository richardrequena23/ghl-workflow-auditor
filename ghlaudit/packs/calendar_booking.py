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
before your appointment", a confirmation quotes a "no-show fee", a win-back
campaign opens "we missed you", an errand promises the forms "in a day or
two", an address says "see you at". Each check below therefore reads copy in
sentences and only after the STRUCTURE of the workflow has established what
kind of lane it is — and a structural read is held to the same standard: a
duration wait a builder happened to name after the appointment is a duration
wait, and a twenty-character word in a booking URL is a slug until its shape
says otherwise. Where the export cannot settle it, these rules stay quiet.
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

# Keys whose VALUE declares what a wait is measured against. A step typed
# `event_start_wait`, or one carrying "waitType": "appointment_time", has said
# out loud that it is anchored to the slot; a duration wait that merely
# mentions an appointment in its label has not.
_ANCHOR_DECLARING_KEYS = ("waittype", "waitfor", "waituntil", "mode", "resumeon",
                          "anchor", "anchoredto", "relativeto", "reference",
                          "referencefield", "basedon", "eventtype")


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


# Keys a wait uses to name its unit. `type` is last-resort and overloaded, but
# it is what GoHighLevel actually writes — see the comment in _offset_from_dict.
_UNIT_KEYS = ("unit", "units", "period", "unittype", "interval", "type")


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
        # `{"hoursBefore": true}` is a flag somebody wrote badly, not one hour.
        # float(True) is 1.0, so without this the export gets a lead time it
        # never stated.
        if m and not isinstance(node[real], bool):
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

    # {"value": 24, "unit": "hours", "direction": "before"} — and the shape a
    # REAL GoHighLevel export uses, which is
    # {"when": "before", "type": "hours", "value": 24, "action_in": 0}.
    #
    # `type` has to be in this list. It is an overloaded key, and leaving it out
    # is precisely what made every hour-based rung of a reminder ladder
    # unreadable. Measured against Richard's live account on Aug-26: "Wait until
    # 24h before" and "Wait until 1h before" both parsed to an offset of None,
    # the ladder collapsed to its one minutes-based rung, and GHL070 reported a
    # textbook 24h/1h/10min ladder as "one reminder, ten minutes out, and
    # nothing earlier" — a false positive on the best-built workflow in the
    # account. Fixture tests never caught it because the fixtures were written
    # in the {"unit": ...} shape the parser already understood.
    #
    # Accepting it is safe because _to_minutes() returns None for anything that
    # is not a real unit, so the far more common {"type": "appointment"} and
    # {"type": "email"} fall through instead of inventing a lead time. We try
    # every candidate rather than the first present one, so a node carrying both
    # {"type": "appointment"} and {"unit": "hours"} still resolves.
    for unit_nk in _UNIT_KEYS:
        if "value" not in keys or unit_nk not in keys:
            continue
        minutes = _to_minutes(node[keys["value"]], node[keys[unit_nk]])
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


def _declares_anchor(step: Step, cfg: dict) -> bool:
    """Does this step SAY it is an appointment wait, or merely mention one?

    The distinction decides whether a configured duration is allowed to
    outrank the word "appointment" appearing somewhere in the step. Builders
    name drip waits for the thing they follow — "Wait 1 day after the
    appointment is booked" is an ordinary duration wait — and reading that
    label as a slot anchor reported a reversed ladder on a correct one.
    """
    nt = _nk(step.type)
    if "eventstartwait" in nt or "appointment" in nt:
        return True
    for key, value in cfg.items():
        if _nk(key) in _ANCHOR_DECLARING_KEYS and isinstance(value, str) \
                and _ANCHOR_WORDS.search(value):
            return True
    return False


def _declares_direction(cfg) -> bool:
    """Is a before/after direction written anywhere in these settings?

    A wait that names a direction is measuring against a fixed moment, even
    when the magnitude beside it is in a shape nothing here can read. Only a
    step with no direction anywhere may be demoted to a plain duration.
    """
    found = [False]

    def walk(node):
        if found[0]:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                nk = _nk(key)
                if nk in _DIRECTION_KEYS or nk in _BEFORE_KEYS \
                        or nk in _AFTER_KEYS \
                        or re.fullmatch(r"(?:second|sec|minute|min|hour|hr|day|"
                                        r"week)s?(?:before|after)", nk) \
                        or re.fullmatch(r"offset(?:second|sec|minute|min|hour|"
                                        r"hr|day|week)s?", nk):
                    found[0] = True
                    return
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(cfg)
    return found[0]


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
    # A configured "wait N units" with no direction on it counts from
    # enrollment, whatever the label says about appointments. Demoting it here
    # is what keeps "Wait 1 day after the appointment is booked" out of the
    # reminder ladder — read as +1 day from the SLOT it made every correct
    # ladder underneath it look reversed.
    if anchored and not _declares_anchor(step, cfg) \
            and _has_plain_duration(cfg) and _scan_offset(cfg) is None \
            and not _declares_direction(cfg):
        anchored = False
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


# Statuses that only exist once the appointment is behind the contact. A lane
# entered on one of them has no reminders in it to get wrong and no
# still-future appointment to contradict.
_PAST_STATUS = re.compile(
    r"no[- _]?show|showed|attended|complete|cancel", re.I)


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


def _wait_ancestors(wf: Workflow, parents: dict, step: Step) -> list:
    """The waits that actually hold this step back, on a wired export.

    Where the builder recorded links, the step LIST is not the running order —
    branch children are flattened into it in whatever order they were saved.
    Reading "the waits above it in the file" then credits a send with a wait
    from a branch it never runs through, or misses the one that does hold it.
    Walking the ancestry answers it properly; a flat export has no ancestry to
    walk and falls back to file order.
    """
    by_id: dict = {}
    for other in wf.steps:
        by_id.setdefault(other.step_id, other)
    out: list = []
    seen: set = set()
    current = parents.get(step.step_id)
    while current and current not in seen:
        seen.add(current)
        held = by_id.get(current)
        if held is not None and held.is_wait:
            out.append(held)
        current = parents.get(current)
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
    """'24 hours', '15 minutes' — for a symptom a business owner reads.

    Days only past the two-day mark. Nobody labels a reminder "1 day before";
    they write "24 hours", and a finding the client cannot match to the words
    on their own step is a finding they have to go looking for.
    """
    span = abs(minutes)
    for unit, factor, floor in (("day", 1440.0, 2880.0), ("hour", 60.0, 60.0),
                                ("minute", 1.0, 1.0)):
        if span >= floor:
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


# A sentence that puts the meeting and the time together with a verb between
# them: "your call is tomorrow", "the session starts in 1 hour". This is the
# only shape strong enough to outrank the errand test below.
_APPT_IS_WHEN = re.compile(
    r"\b(?:appointments?|appts?|calls?|sessions?|meetings?|consult\w*|demos?|"
    r"slots?|visits?|bookings?)\b[^.\n]{0,28}?"
    r"\b(?:is|are|'s|starts?|starting|begins?|kicks off|happens|takes place|"
    r"will be)\b[^.\n]{0,20}?"
    r"(?:tomorrow|in (?:an?|\d+) ?(?:hours?|minutes?|mins?|days?)|coming up)",
    re.I)

# Verbs that mean the time claim belongs to a MESSAGE, not to the meeting.
# "We'll email the forms for your session in a day or two" and "reply and
# we'll call you back in 2 hours" both name a meeting and a time and are not
# reminders; the errand verb is what separates them from copy that is.
_ERRAND = re.compile(
    r"\b(?:send|sends|sending|sent|email|emails|emailing|mail|deliver\w*|"
    r"ship\w*|arrives?|arriving|post|hear from|get back|reach out|"
    r"contact you|call (?:you|them|us) back|text (?:you|them) back|"
    r"reply|respond|invoice|charge[ds]?|refund)\b", re.I)


def _claims_when_the_appointment_is(text: str) -> bool:
    """Does this copy tell the contact when their appointment is?

    Sentence-scoped, and the sentence has to be ABOUT the meeting rather than
    merely mention it. Without that, "give us 24 hours before your
    appointment" (a cancellation policy), "see you at 4400 Main Street" (an
    address) and "we will email the forms for your session in a day or two"
    (paperwork) all read as reminders — three correct messages reported as a
    critical fault. The errand test is overridden by an explicit "your call is
    tomorrow", so "we'll send you a reminder — your call is tomorrow at 2" is
    still read as what it is.
    """
    for sentence in _sentences(text):
        if _SELF_EVIDENT_CLAIM.search(sentence) or _APPT_IS_WHEN.search(sentence):
            return True
        if not (_TIME_CLAIM.search(sentence) and _APPT_NOUN.search(sentence)):
            continue
        if _ERRAND.search(sentence):
            continue
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
        # A lane entered once the appointment is behind the contact —
        # cancelled, no-showed, showed, completed — contains no reminders to
        # get wrong. GHL028 and GHL069 own those lanes, and a post-appointment
        # check-in labelled "3 days out" is not a reminder ladder.
        if _PAST_STATUS.search(_trigger_blob(appts)):
            continue
        parents = _parent_map(wf) if wf.has_wiring else {}
        waits: list[Step] = []
        for step in wf.steps:
            if step.is_wait:
                waits.append(step)
                continue
            if not step.is_outbound:
                continue
            try:
                body = step.bodies()
            except (TypeError, ValueError):
                continue
            if not (_claims_when_the_appointment_is(body)
                    or _REMINDER_LABEL.search(step.name)):
                continue
            held_by = _wait_ancestors(wf, parents, step) if parents else waits
            if not held_by:
                continue  # nothing delays it, so nothing mis-times it
            if any(_appointment_offset(w)[0] for w in held_by):
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
            # Strictly earlier only. Two waits on the SAME moment — an SMS and
            # an email both timed 24 hours out — release together, which is
            # what that builder wanted; there is no moment in the past for the
            # second one to wait for, and the finding below would describe a
            # sequence that is not there.
            if after >= before:
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

# A HighLevel object id, in the two shapes the platform actually issues:
# mixed-case base62 of twenty-odd characters, or a 24-character hex ObjectId.
# The widget path also accepts a human-readable slug, and a slug cannot be
# checked against a list of ids — calling
# `book.theclient.com/widget/booking/freeconsultationcall` a dead calendar
# because that word is not in the id list would be a guess, and a loud one at
# critical. Length alone does not separate them: "freeconsultationcall" is
# twenty characters. Case does — a hand-typed slug is never mixed-case.
_MIXED_CASE_ID = re.compile(
    r"^(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[a-z])[A-Za-z0-9]{18,}$")
_HEX_OBJECT_ID = re.compile(r"^[0-9a-f]{24}$")

# Third-party schedulers, by host. Only unambiguous ones: a Google Calendar
# link can be a hundred things, an acuityscheduling.com link is exactly one.
_EXTERNAL_SCHEDULER = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*("
    r"calendly\.com|acuityscheduling\.com|cal\.com|savvycal\.com|"
    r"youcanbook\.me|tidycal\.com|oncehub\.com|scheduleonce\.com|"
    r"setmore\.com|simplybook\.me|zcal\.co|appointlet\.com|chilipiper\.com|"
    r"meetings\.hubspot\.com|vcita\.com|booksy\.com)", re.I)

# The same domains serve their own help centre. A link to a support article
# about rescheduling is not a booking link, and reporting it as one is the
# kind of finding that gets the whole section skimmed past.
_DOCS_SUBDOMAIN = re.compile(
    r"^(?:help|support|docs?|blog|status|developer|community|about)\.", re.I)


def _looks_like_an_id(token: str) -> bool:
    return bool(_MIXED_CASE_ID.match(token) or _HEX_OBJECT_ID.match(token))


def _external_scheduler(link: str) -> str:
    """The scheduler host this link books on, or "" if it books nothing."""
    match = _EXTERNAL_SCHEDULER.search(link)
    if not match:
        return ""
    host = match.group(0).split("//", 1)[-1].split("/", 1)[0]
    if _DOCS_SUBDOMAIN.match(host):
        return ""
    return match.group(1).lower()


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

    for wf in acct.published():
        # One scheduler is one defect in this workflow, however many messages
        # carry the link. A normal email holds it three times — button, body,
        # footer — and three identical findings is how a report loses a reader.
        external: dict = {}
        for step in wf.outbound:
            try:
                body = step.bodies()
            except (TypeError, ValueError):
                continue
            for link in URL.findall(body):
                widget = _GHL_BOOKING_WIDGET.search(link)
                if widget:
                    if _looks_like_an_id(widget.group(1)):
                        widget_hits.append((wf, step, widget.group(1)))
                    continue
                host = _external_scheduler(link)
                if not host:
                    continue
                label = step.name or step.type
                named = external.setdefault(host, [])
                if label not in named:
                    named.append(label)
        for host, named in external.items():
            many = len(named) != 1
            yield _finding(
                "GHL067", "high", wf,
                f"Booking link sends contacts to {host}, outside this "
                "account",
                (f"{len(named)} messages in this workflow hand"
                 if many else "This message hands")
                + " the contact a third-party booking "
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
                step=", ".join(named), reach=len(named),
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

# Only merge fields that render an HOUR. A date-only field ("{{
# appointment.start_date }}") names a day, and a day needs no zone beside it —
# flagging one puts a finding on a message that is already correct. Any
# message that quotes the day also quotes the time, in a field this matches.
_APPT_TIME_MERGE = re.compile(
    r"\{\{\s*(?:appointment|appt|event)[._-]?"
    r"(?:start(?:_?(?:time|at|date_?time))?|time|date_?time|slot)\b", re.I)

_ZONE_TEXT = re.compile(r"time ?zone|\byour time\b|\blocal time\b", re.I)
# Uppercase only, deliberately: lowercased, "ct" and "mt" appear inside
# ordinary words and every reminder in the account would look compliant.
_ZONE_ABBR = re.compile(
    r"\b(?:E[SD]T|C[SD]T|M[SD]T|P[SD]T|AK[SD]T|A[SD]T|HST|UTC|GMT|BST|"
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
# The zone as a bare word, but only where it sits right after the time it
# qualifies: "{{ appointment.start_time }} Eastern", "2:00 PM Central". Plenty
# of accounts write it that way and mean it; "our central location" is the
# reason the word alone is not enough.
_ZONE_AFTER_TIME = re.compile(
    r"(?:\}\}|\d(?::\d{2})?\s*(?:am|pm)?)[\s,(–—-]*"
    r"\b(?:eastern|central|mountain|pacific|atlantic|alaska|hawaii|"
    r"newfoundland)\b", re.I)

_IANA_ZONE = re.compile(r"^[A-Za-z]+(?:_[A-Za-z]+)*/[A-Za-z0-9_+-]+$")
_FIXED_OFFSET = re.compile(r"^(?:UTC|GMT)\s*[+-]\d{1,2}(?::\d{2})?$", re.I)
_CONTACT_ZONE = ("contact", "contacttimezone", "contactstimezone", "lead",
                 "recipient", "user")


def _names_a_zone(body: str) -> bool:
    return bool(_ZONE_TEXT.search(body) or _ZONE_ABBR.search(body)
                or _ZONE_NAME.search(body) or _ZONE_PARENTHETICAL.search(body)
                or _ZONE_IANA_IN_COPY.search(body)
                or _ZONE_AFTER_TIME.search(body))


def _pinned_timezone(wf: Workflow) -> str:
    """A literal zone nailed to the workflow, rather than the contact's own.

    A workflow set to follow the contact frequently ALSO carries the account's
    zone in the same settings block, as the fallback. Reading that as a pin
    raises a medium finding to high on a workflow that is configured correctly,
    so the source is settled first and the literal is only used if nothing said
    "contact".
    """
    settings = wf.settings if isinstance(wf.settings, dict) else {}
    relevant = [(key, value) for key, value in settings.items()
                if _nk(key) in ("timezone", "timezonesource", "tz",
                                "sendtimezone")
                and isinstance(value, str)]
    if any(_nk(value) in _CONTACT_ZONE for _, value in relevant):
        return ""
    for _, value in relevant:
        candidate = value.strip()
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

# Copy that can only be about a missed MEETING. The noun is inside the phrase,
# so no context is needed to read it.
_NOSHOW_UNAMBIGUOUS = re.compile(
    r"\bno[- ]?shows?\b|"
    r"missed (?:your|the|our|today'?s) (?:appointment|appt|call|session|"
    r"meeting|consult\w*|demo|slot|visit|booking)|"
    r"did ?n'?t (?:make it to|show up for|attend) (?:your|the|our)", re.I)

# The same apology with the noun left out. "We missed you!" is no-show copy in
# a booking lane and the opening line of every win-back campaign ever built,
# so it only counts where something else in the message is about a meeting.
_NOSHOW_AMBIGUOUS = re.compile(
    r"we missed you|sorry we missed|you missed|did ?n'?t (?:make it|show|"
    r"attend)|could ?n'?t make it|we did ?n'?t connect|"
    r"were ?n'?t able to (?:make|attend)", re.I)

# What makes an apology about a meeting rather than about a lapsed customer:
# the meeting itself, or the offer to move it.
_MEETING_REFERENCE = re.compile(
    r"\b(?:appointments?|appts?|calls?|sessions?|meetings?|consult\w*|demos?|"
    r"slots?|visits?|bookings?)\b|re-?book|resched|another (?:time|slot|day)|"
    r"new time|back on the (?:calendar|books)", re.I)

# The same words, used as terms rather than as an apology. Every second
# confirmation message in a service business carries a no-show fee line, and it
# is not recovery copy — it is the policy, and it belongs in the confirmation.
_POLICY_CONTEXT = re.compile(
    r"\bfees?\b|charge[ds]?\b|\bcharging\b|polic(?:y|ies)|penalt|billed|"
    r"invoice|forfeit|deposit|\bcosts?\b|\brate\b", re.I)

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

# Evidence that the contact enters this lane when the booking is MADE: an
# appointment trigger filtered to a live status, or a tag/trigger named for
# the booking. Anything vaguer — an opaque tag, a stage id — cannot be read,
# and a critical finding may not rest on a tag name nobody can decode.
_BOOKING_ENTRY = re.compile(
    r"book|schedul|confirm|appointment[- _]?set|new[- _]?appointment", re.I)


# An apology for a missed PHONE attempt, which is not a no-show: "sorry we
# missed you on the phone earlier — your call is still on for tomorrow" is a
# correct message and names a meeting, so nothing else here separates it out.
_PHONE_MISS = re.compile(
    r"on the phone|tried (?:to call|calling|you)|could ?n'?t reach you|"
    r"rang you|gave you a (?:call|ring)|left (?:you )?a (?:voicemail|message)|"
    r"when we called", re.I)


def _reads_as_a_missed_appointment(text: str) -> bool:
    """Is this copy apologising for a missed meeting?

    Four ways to be wrong here, and each of them puts a critical finding on a
    correct message: the no-show FEE line every confirmation carries, the
    win-back campaign that opens "we missed you", the cold-outreach nudge that
    opens "sorry we didn't connect", and the apology for a phone call nobody
    picked up. So those sentences are dropped first, and an apology with no
    noun in it only counts when the message elsewhere is about a meeting.
    """
    meeting = bool(_MEETING_REFERENCE.search(text))
    for sentence in _sentences(text):
        if _POLICY_CONTEXT.search(sentence) or _PHONE_MISS.search(sentence):
            continue
        if _NOSHOW_UNAMBIGUOUS.search(sentence):
            return True
        if meeting and _NOSHOW_AMBIGUOUS.search(sentence):
            return True
    return False


def _entered_before_the_appointment(wf: Workflow) -> bool:
    """Does this workflow demonstrably start while the appointment is ahead?

    That is the whole claim GHL069 makes, so it has to be provable rather than
    inferred. An appointment trigger answers it outright — and answers it
    alone, because a cancellation lane whose trigger someone named "Booking
    cancelled" must not be read as a booking lane on the strength of the word.
    Failing that, a trigger named for the booking counts. A lane entered on an
    opaque tag proves nothing — the tag may well be the no-show marker another
    workflow set — and guessing there would put "you were told you missed a
    call you have not had yet" in a report about a correct recovery lane.
    """
    appts = _appointment_triggers(wf)
    if appts:
        if not any(t.filters() for t in appts):
            return False  # unfiltered: GHL001's finding, not this one
        return not _PAST_STATUS.search(_trigger_blob(appts))
    return bool(_BOOKING_ENTRY.search(_trigger_evidence(wf)))


def _sends_before_the_appointment(wf: Workflow, index: int) -> bool:
    """Is this send held by a wait that targets a moment BEFORE the start?

    The other way to prove the appointment has not happened: the builder said
    so themselves, with a wait anchored to the slot and pointed backwards.
    """
    for step in wf.steps[:index]:
        anchored, minutes = _appointment_offset(step)
        if anchored and minutes is not None and minutes < 0:
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
    warmest lead in the account.

    The bar for reading it is deliberately high, because every cheap version of
    this check reports correct work. Only a workflow that provably starts
    BEFORE the appointment is read; never a lane the phone system starts, where
    "sorry we missed your call" is the correct copy; and never one whose
    appointment trigger carries no filter at all, which is GHL001's finding.
    """
    for wf in acct.published():
        if any(_CALL_TRIGGER.search(t.type) for t in wf.triggers):
            continue
        # The two-workflow pattern: one lane tags the contact on the no-show
        # status, a second is triggered by that tag. The tag IS the proof, and
        # it never appears as a filter on an appointment trigger.
        if _MISS_MARKER.search(_trigger_evidence(wf)):
            continue
        entered_early = _entered_before_the_appointment(wf)
        for i, step in enumerate(wf.steps):
            if not step.is_outbound:
                continue
            try:
                body = step.bodies()
            except (TypeError, ValueError):
                continue
            if not _reads_as_a_missed_appointment(body + "\n" + step.name):
                continue
            # A later send may still be held by a backwards-anchored wait even
            # when this one is not, so keep reading rather than giving up.
            if not (entered_early or _sends_before_the_appointment(wf, i)):
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


def _warns_in_good_time(wf: Workflow) -> bool:
    """Does this workflow reach the contact while a reschedule is still easy?"""
    for i, step in enumerate(wf.steps):
        anchored, minutes = _appointment_offset(step)
        if anchored and minutes is not None \
                and -minutes >= _RESCHEDULE_WINDOW_MINUTES \
                and wf.outbound_after(i):
            return True
    return False


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
    another workflow or on the calendar's own notification settings, the second
    of which a workflow export cannot see. The first of them it CAN see, so it
    reads the whole account before calling a lane the contact's only warning.
    """
    # Day-of and day-before reminders are often split across two workflows, and
    # the day-of one alone looks exactly like a ladder with no room to move.
    # Read once, not once per finding.
    in_good_time = [w for w in acct.published() if _warns_in_good_time(w)]
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
        # Splitting the day-before touch and the day-of touch across two
        # workflows is a normal build, not a defect, so when the account has
        # the earlier one this drops to the same advisory note GHL003 and
        # GHL028 give a delegated responsibility: worth one line, not a rank.
        elsewhere = next((w for w in in_good_time if w is not wf), None)
        if elsewhere is not None:
            yield _finding(
                "GHL070", "low", wf,
                f"Reminders here all land within {_human(earliest)} of the "
                f"appointment — the earlier touch is in '{elsewhere.name}'",
                "Nothing in this workflow reaches the contact until "
                f"{_human(earliest)} before the appointment, which is after "
                "the point where a reschedule is still easier than not "
                f"turning up. '{elsewhere.name}' does send an earlier "
                "reminder, so the contact is probably warned in time — but "
                "only if both workflows cover the same calendars and the same "
                "contacts. Where they do not, the bookings in this lane get a "
                "last-minute warning and nothing else.",
                f"Check that '{elsewhere.name}' enrolls the same bookings this "
                "one does — same calendar, same trigger filters. If it does, "
                "this is fine as the final nudge. If it does not, add a "
                "24-hour touch here.",
                step=names, reach=len(wf.outbound),
                cost="Nothing, if the two workflows cover the same bookings. "
                     "Every no-show in the gap between them, if they do not.")
            continue
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
                "someone was told. No other workflow in this account sends an "
                "earlier one either.",
                "Add a touch the day before — 24 hours out is the one that "
                "recovers bookings, because a reschedule is still easier than "
                "a cancellation at that point — and keep this one as the "
                "final nudge. Give both a reply path so a reschedule can "
                "actually happen. If a day-before reminder already goes out "
                "from the calendar's own notification settings, note that and "
                "move on.",
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


# --------------------------------------------------------------------------
# GHL103 — the booking screen and the confirmation text disagree
# --------------------------------------------------------------------------

# What a booking screen says when the slot is only a REQUEST. Sentence-scoped,
# and a short closed list: every phrase here asserts that confirmation has not
# happened yet, which is the one claim that cannot be true on a calendar whose
# own setting confirms the slot the moment it is picked.
_REQUEST_COPY = re.compile(
    r"appointment request"
    r"|will (contact|call|reach out to|be in touch with|get back to) you\b"
    r".{0,40}\bconfirm"
    r"|pending (our )?confirmation"
    r"|once (we|it)('s| is| has been)? confirm"
    r"|request (has been |was )?(received|submitted)"
    r"|to confirm your (request|booking|appointment)",
    re.I)

# What the account's own workflow tells the same person a moment later.
_BOOKED_COPY = re.compile(
    r"you'?re booked|you are booked|is confirmed|has been confirmed"
    r"|you'?re on the calendar|you are on the calendar|see you (on|at)\b",
    re.I)

# Thank-you types under which the message is actually shown. A redirect never
# displays it, so its text cannot contradict anything.
_MESSAGE_SHOWN = ("thankyoumessage", "message", "thank_you_message",
                  "thankyou", "thank_you")


def _thanks_message(rec: dict) -> str:
    for key in ("formSubmitThanksMessage", "thankYouMessage",
                "thanksMessage", "formSubmitMessage"):
        val = rec.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _shows_thanks_message(rec: dict) -> bool:
    kind = rec.get("formSubmitType", rec.get("thankYouType"))
    if kind in (None, ""):
        return True  # the platform default is the message
    return _nk(kind) in _MESSAGE_SHOWN


def _confirms_bookings_on(acct: Account, cal_id: str):
    """The published workflow that greets a CONFIRMED booking on this calendar
    with copy saying it is booked, and the step that says so."""
    want = str(cal_id).lower()
    for wf in acct.published():
        first = next((s for s in wf.steps if s.is_outbound), None)
        if first is None or not _BOOKED_COPY.search(first.bodies()):
            continue
        for trg in wf.triggers:
            if "appointment" not in trg.canonical_type():
                continue
            blob = trg.filter_blob()
            if "confirmed" in blob and want in blob:
                return wf, first
    return None, None


@rule("GHL103", "Booking screen says 'we'll confirm' on a calendar that "
      "already did", "medium", "routing", "appointments", "copy")
def thank_you_screen_contradicts_auto_confirm(acct: Account):
    """The calendar auto-confirms, the screen says it does not, the text says it does.

    GoHighLevel ships every new calendar with the same thank-you text: "Thank
    you for your appointment request. We will contact you shortly to confirm
    your request." That sentence is true on a calendar that hand-confirms and
    false on one set to auto-confirm — and auto-confirm is the setting that
    fires the account's own "you're booked" workflow. So the last thing the
    contact reads on the site says the slot is provisional, and the first thing
    that lands on their phone says it is not.

    Standalone stock copy on an auto-confirm calendar is left alone: a business
    that confirms by hand afterwards anyway is not wrong, only slow, and a
    workflow export cannot see its office. This fires only when the account
    contradicts ITSELF — its own published workflow, triggered on the confirmed
    status of this very calendar, opens with copy asserting the booking. Both
    halves are stated outright in the export: `autoConfirm` is a boolean, the
    trigger names the calendar and the status, and the copy is quoted.
    """
    inv = acct.inventory
    if not inv.has("calendars"):
        # Nothing to contradict unless some workflow confirms bookings at all.
        # The calendar list has to be asked for by id though, so this reads
        # every appointment trigger for the shape and reports the gap.
        for wf in acct.published():
            first = next((s for s in wf.steps if s.is_outbound), None)
            if first is None or not _BOOKED_COPY.search(first.bodies()):
                continue
            if any("appointment" in t.canonical_type()
                   and "confirmed" in t.filter_blob() for t in wf.triggers):
                yield Skip(
                    rule="GHL103",
                    title="Booking screen says 'we'll confirm' on a calendar "
                          "that already did",
                    reason="A workflow greets confirmed bookings with "
                           "'you're booked', but the location's calendars "
                           "were not supplied — so whether the calendar "
                           "auto-confirms and what its booking screen says "
                           "cannot be read. The finding is the disagreement "
                           "between those two, and neither is in a workflow "
                           "export.",
                    needs="calendars exported as full objects, each carrying "
                          "autoConfirm, formSubmitType and "
                          "formSubmitThanksMessage",
                    category="routing")
                return
        return

    records = inv.calendar_records
    with_settings = [r for r in records.values() if "autoConfirm" in r]
    if not with_settings:
        # Ids and names only. Only a hole if there is something to check.
        for cal_id in records:
            wf, _ = _confirms_bookings_on(acct, cal_id)
            if wf is not None:
                yield Skip(
                    rule="GHL103",
                    title="Booking screen says 'we'll confirm' on a calendar "
                          "that already did",
                    reason="The calendar list in this bundle carries ids and "
                           "names only, not the calendars' own settings. "
                           "Whether a booking auto-confirms, and what the "
                           "screen says to the person who just booked, are "
                           "both calendar-level settings — and the finding is "
                           "the disagreement between those two.",
                    needs="calendars exported as full objects, each carrying "
                          "autoConfirm, formSubmitType and "
                          "formSubmitThanksMessage",
                    category="routing")
                return
        return

    for cal_id, rec in records.items():
        if rec.get("autoConfirm") is not True:
            continue  # a real request calendar: the copy is true
        if not _shows_thanks_message(rec):
            continue  # redirected away; the message is never seen
        message = _thanks_message(rec)
        hits = [s.strip() for s in _sentences(message)
                if _REQUEST_COPY.search(s)]
        if not hits:
            continue
        # Quote the sentence that makes the promise, when there is one.
        hit = next((h for h in hits if re.search(r"confirm", h, re.I)),
                   hits[0])
        wf, step = _confirms_bookings_on(acct, cal_id)
        if wf is None:
            continue  # bad copy alone is not a contradiction this can prove
        cal_name = str(rec.get("name") or inv.calendars.get(cal_id) or cal_id)
        yield _finding(
            "GHL103", "medium", wf,
            f"The '{cal_name}' booking screen says \"we'll confirm\" — the "
            f"calendar already did, and '{step.name or step.type}' says so",
            f"The '{cal_name}' calendar is set to auto-confirm, so a booking "
            f"is confirmed the instant someone picks a slot — that is the "
            f"status that starts '{wf.name}'. But the screen they land on "
            f"still says \"{hit}.\" Moments later '{step.name or step.type}' "
            f"arrives telling them they are booked. The last thing they read "
            f"on the site and the first thing that reaches their phone say "
            f"opposite things, and the site's version is GoHighLevel's stock "
            f"text, not a decision anyone made.",
            f"Rewrite the calendar's thank-you message to match what the "
            f"calendar does: confirm the booking, restate the time, say what "
            f"happens next (Calendars → {cal_name} → Forms & Payment). If the "
            f"intent really is to hand-confirm each slot, switch auto-confirm "
            f"off instead — and then move '{wf.name}' off the confirmed-status "
            f"trigger, or it will keep congratulating people on a slot nobody "
            f"has accepted.",
            step=f"calendar '{cal_name}'",
            cost="A contact who believes the screen treats the slot as "
                 "provisional and leaves it out of their diary, then reads the "
                 "confirmation text as a mismatch. It surfaces weeks later as "
                 "a no-show nobody can explain, and the closer's hour is paid "
                 "for either way.",
            reach=len(wf.outbound))
