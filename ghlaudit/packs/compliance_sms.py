"""SMS and telecom compliance — the layer the carriers and the courts enforce.

Every other family in this catalog costs a client leads. This one costs them the
channel: a 10DLC campaign that gets filtered stops delivering for every workflow
in the account at once, and a quiet-hours breach is priced per message in
statutory damages rather than per campaign. Six checks, each one provable from a
static read of an export — an SMS campaign aimed at a list with nothing checking
opt-in, a send window whose bounds sit outside the legal hours, an invited reply
keyword the provider does not honour (or one it honours far too well),
carrier-prohibited content in a message body, a first touch that never says who
is texting, and three texts landing on one phone inside a day.

What is deliberately absent matters as much. Whether the account answers HELP is
a provider setting, not a workflow, so no export can show it and this pack does
not guess at it. Cross-workflow frequency — the contact enrolled in four
sequences at once — is not decidable either: two triggers may never both fire
for the same contact, and configuration does not say which. GHL058 checks the
half that IS decidable, inside a single workflow, and says so in its wording.

One structural fact governs three of these rules. GoHighLevel's advanced builder
exports every node of a branching workflow into ONE flat list, in depth-first
order, with the tree recorded in `parentKey`. So position in that list is not
the order a contact experiences: step 11 can be the fourth node of the third
branch, and steps 11, 15 and 19 can be three messages only one of which will
ever send. Reading the flat list as a journey is how a rule about the FIRST
message ends up firing on a mid-sequence nudge, and how a rule about three texts
in a day ends up counting three mutually exclusive ones. `_linear_paths` and
`_trunk` rebuild the real paths from `parentKey` before either rule reads them.
"""

from __future__ import annotations

import json
import re

from ..model import Account, Step, Workflow
from ..rules import Skip, _finding, rule


def _nk(key) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


# --------------------------------------------------------------------------
# Reading a branching workflow as the paths a contact can actually take
# --------------------------------------------------------------------------

# Guards. A path walk over an export nobody has validated is the one place in
# this pack that could hang or blow the stack, and a rule that crashes takes the
# other ninety-nine checks down with it.
PATH_CAP = 64      # distinct root-to-leaf chains explored per workflow
STEP_CAP = 2000    # above this, do not walk the tree at all


def _forks(step: Step) -> bool:
    """Does this step hand the contact to one of several different paths?

    An if/else is the obvious one. `transition` is the other: GoHighLevel's
    multi-path wait writes its 'Replied' / 'No reply' outcomes as transition
    nodes, and those are a fork by any other name.
    """
    return step.is_branch or "transition" in _nk(step.type)


def _wiring(wf: Workflow):
    """(children by parent id, root steps), or None when the export has no tree.

    `parentKey` is written either as the parent's bare id or as
    `<parentId>-<branchName>`, so resolving it is a prefix match, never an
    equality test.
    """
    by_id = {}
    for step in wf.steps:
        sid = step.step_id
        if sid and sid not in by_id:
            by_id[sid] = step
    if not by_id:
        return None
    # `<parentId>-<branchName>` is resolved by trying every id LENGTH present
    # rather than every id: scanning the ids themselves is quadratic, and an
    # export where nothing resolves is exactly the export that would pay for
    # it. Ids in one export are uniform (uuids), so this is one or two probes.
    lengths = sorted({len(sid) for sid in by_id})
    kids: dict = {}
    roots: list = []
    for step in wf.steps:
        pk = step.parent_key
        parent = None
        if pk:
            if pk in by_id:
                parent = pk
            else:
                for size in lengths:
                    if len(pk) > size and pk[size] == "-" and pk[:size] in by_id:
                        parent = pk[:size]
                        break
        if parent is None or parent == step.step_id:
            roots.append(step)
        else:
            kids.setdefault(parent, []).append(step)
    if not roots or not kids:
        return None
    return kids, roots


def _linear_paths(wf: Workflow) -> list:
    """Every route one contact could take through this workflow, in order.

    With wiring, this is the real tree. Without it — a flat export, a
    hand-written fixture — the list is only trustworthy up to the first fork,
    because past that point it interleaves paths that never both run. Cutting
    there loses findings on workflows that branch early; counting past it
    invents findings on workflows that branch at all, and an invented finding
    is the more expensive of the two.
    """
    steps = list(wf.steps)
    if not steps:
        return []
    if len(steps) <= STEP_CAP:
        wired = _wiring(wf)
        if wired:
            kids, roots = wired
            paths: list = []
            # `seen` rides alongside the trail so the cycle check is a set
            # lookup. A malformed export can point a parentKey back up its own
            # chain, and walking that without a guard never returns.
            stack = [(root, (), frozenset()) for root in reversed(roots)]
            while stack and len(paths) < PATH_CAP:
                step, trail, seen = stack.pop()
                if id(step) in seen:
                    paths.append(list(trail))  # cycle: stop before repeating
                    continue
                trail = trail + (step,)
                seen = seen | {id(step)}
                children = kids.get(step.step_id) or []
                if not children:
                    paths.append(list(trail))
                    continue
                for child in reversed(children):
                    stack.append((child, trail, seen))
            if paths:
                return paths
    cut = len(steps)
    for i, step in enumerate(steps):
        if _forks(step):
            cut = i
            break
    return [steps[:cut]]


def _trunk(wf: Workflow) -> list:
    """The steps every contact passes through, before any fork.

    This is the only part of a workflow where "first" means what it sounds
    like. Anything below the first fork is the first message on ONE path, and
    a rule that calls that the opening message is asserting something the file
    does not say.
    """
    paths = _linear_paths(wf)
    if not paths:
        return []
    head = paths[0]
    shared = len(head)
    for path in paths[1:]:
        shared = min(shared, len(path))
        while shared and path[shared - 1] is not head[shared - 1]:
            shared -= 1
        if not shared:
            return []
    return head[:shared]


# --------------------------------------------------------------------------
# GHL053 — consent
# --------------------------------------------------------------------------

# Triggers that ARE the consent record: the contact did something a minute ago
# that produced a written request to be contacted.
CONSENT_TRIGGERS = ("form_submitted", "survey_submitted", "order", "payment",
                    "invoice", "appointment", "inbound_message", "call")

# Enrolment shapes that carry no consent of their own. A tag lands on a contact
# for any reason at all, and a workflow with no trigger is fed by hand from a
# list view or an import.
LIST_TRIGGER = re.compile(r"tag[_ -]?added|added[_ -]?tag|manual|bulk|import|"
                          r"upload|csv|campaign", re.I)

# The vocabulary of a campaign aimed at a LIST rather than at a person. Without
# this second gate the rule fires on every tag-triggered sequence in the
# account, which is most of them — and a check that fires on everything gets the
# whole report ignored. "nurture" is deliberately absent: a nurture sequence is
# normally fed by people who did opt in somewhere upstream. The short words are
# bounded because "list" is inside "waitlist" and "specialist", and neither of
# those is a purchased list.
LIST_NAMES = re.compile(
    r"reactivat|database|\bcold\b|dormant|win[\s-]?back|blast|\bbulk\b|"
    r"purchased|scrape|\bimport(?:ed)?\b|\blists?\b", re.I)
# ...and a bounded "list" still is not evidence when the word in front of it
# names an audience that asked to be there. A waiting list, a VIP list and a
# guest list are opt-ins; a price list and a checklist are not audiences at
# all. Only the WORD is removed, so "Client List Reactivation" still trips on
# "reactivat" — this narrows the weakest signal, it does not grant an amnesty.
NOT_A_BOUGHT_LIST = re.compile(
    r"\b(?:wait|waiting|vip|guest|short|price|pricing|check|punch|task|"
    r"to[\s-]?do|client|customer|member|invite|attendee)[\s-]?lists?\b", re.I)
# "campaign" is deliberately NOT here. It is fine as an enrolment shape below,
# but a trigger somebody labelled "Spring Campaign" is not evidence that the
# audience was bought, and treating it as evidence fired this rule on ordinary
# tag-triggered promos whose names said nothing about a list.
LIST_TRIGGER_WORDS = re.compile(
    r"\bbulk\b|\bimport(?:ed)?\b|upload|\bcsv\b|\blists?\b", re.I)

# Steps that push a contact into a DIFFERENT workflow. The target of one of
# these is a sub-workflow: its contacts were admitted by the caller, so its own
# lack of a trigger says nothing about how they got there.
ENROLLING_STEP = re.compile(
    r"add[_ -]?to[_ -]?workflow|add[_ -]?workflow|start[_ -]?workflow|"
    r"enroll|send[_ -]?to[_ -]?workflow", re.I)

# Anything that reads as "we checked whether we are allowed to text this
# person". Deliberately generous — a false positive here accuses a client of
# texting without consent, which is the most expensive wrong thing this tool
# could say.
CONSENT_EVIDENCE = re.compile(
    r"opt[\s_-]?in|opted|consent|subscrib|permission|marketing[\s_-]?ok|"
    r"sms[\s_-]?(?:ok|okay|allowed|approved|yes|consent)|"
    r"do[\s_-]?not[\s_-]?disturb|\bdnd\b|tcpa|agreed", re.I)


def _checks_consent(wf: Workflow) -> bool:
    """Does anything in this workflow look at opt-in state before it sends?

    Only control surfaces are read — trigger filters, branch conditions, step
    types and step names. Message BODIES are excluded on purpose: "reply STOP
    to unsubscribe" contains the word `subscrib`, and reading bodies would let
    the opt-out footer masquerade as an opt-in check on every workflow that
    carries one.
    """
    for trg in wf.triggers:
        try:
            if CONSENT_EVIDENCE.search(json.dumps(trg.raw)):
                return True
        except (TypeError, ValueError):  # pragma: no cover - non-JSON export
            continue
    for step in wf.steps:
        if CONSENT_EVIDENCE.search(f"{step.type} {step.name}"):
            return True
        if step.is_branch or "condition" in _nk(step.type) or \
                "filter" in _nk(step.type):
            try:
                if CONSENT_EVIDENCE.search(json.dumps(step.raw)):
                    return True
            except (TypeError, ValueError):  # pragma: no cover
                continue
    return False


def _sub_workflows(acct: Account) -> set:
    """Workflows some OTHER workflow pushes contacts into, by id and by name.

    A workflow with no trigger is normally somebody adding contacts by hand
    from a list view — which is the shape this rule exists for. But it is also
    how a sub-workflow looks, and a sub-workflow's contacts were vetted by
    whatever admitted them. Only the account as a whole can tell the two
    apart, so the whole account gets read once.
    """
    fed: set = set()
    for wf in acct.workflows:
        for step in wf.steps:
            if not ENROLLING_STEP.search(f"{step.type} {step.name}"):
                continue
            targets = [v for kind, v in step.entity_refs() if kind == "workflow"]
            cfg = step.config()
            if isinstance(cfg, dict):
                targets += [v for k, v in cfg.items()
                            if _nk(k) in ("workflow", "workflowname",
                                          "targetworkflow", "target")
                            and isinstance(v, str)]
            for target in targets:
                key = str(target).strip().lower()
                # A workflow that re-enrolls its OWN contacts has not been fed
                # from anywhere: that is a loop, not an admission gate.
                if key and key not in (wf.id.lower(), wf.name.lower()):
                    fed.add(key)
    return fed


def _list_shaped(wf: Workflow, sub_workflows: set) -> bool:
    """True when contacts arrive here as a list, not as individuals."""
    named = bool(LIST_NAMES.search(NOT_A_BOUGHT_LIST.sub(" ", wf.name)))
    if not wf.triggers:
        # No trigger at all means somebody adds contacts by hand — a bulk
        # action from a list view, or an import. It can also mean this is a
        # sub-workflow another workflow calls, where consent was settled
        # upstream; when the export shows that call, this is not a list.
        if wf.id.lower() in sub_workflows or wf.name.lower() in sub_workflows:
            return False
        return named
    if not all(LIST_TRIGGER.search(f"{t.type} {t.name}") for t in wf.triggers):
        return False
    return named or any(LIST_TRIGGER_WORDS.search(f"{t.type} {t.name}")
                        for t in wf.triggers)


@rule("GHL053", "SMS to a list with no opt-in check", "high", "compliance",
      "compliance", "sms", "consent")
def list_sms_without_consent_check(acct: Account):
    """A texting campaign aimed at a list, with nothing checking opt-in first.

    Consent is the only defence a 10DLC campaign has. Carriers audit it, the
    aggregator asks for the opt-in language at registration, and TCPA damages
    are counted per message — so the campaign that texts a purchased or
    imported list is not a marketing mistake, it is the one defect in this
    catalog that can produce a lawsuit and a dead sending number in the same
    week.

    Detection is deliberately narrow, and the claim is narrower still. Whether
    consent EXISTS is not decidable from a workflow export and this rule never
    says it does not; what is decidable is whether the workflow reads it, and
    that is all the finding asserts. A tag-triggered sequence is the normal way
    to run any campaign in GoHighLevel, so the enrolment shape alone is not
    enough: the workflow must also read as list-aimed by name, nothing anywhere
    in it may check an opt-in field, tag or DND state, and a workflow another
    workflow feeds is exempt because its contacts were admitted elsewhere.
    """
    sub_workflows = _sub_workflows(acct)
    for wf in acct.published():
        if not wf.sms_steps or acct.config.is_transactional(wf.name):
            continue
        if any(any(k in t.canonical_type() for k in CONSENT_TRIGGERS)
               for t in wf.triggers):
            continue
        if not _list_shaped(wf, sub_workflows) or _checks_consent(wf):
            continue
        texts = wf.sms_steps
        yield _finding(
            "GHL053", "high", wf,
            f"{len(texts)} SMS send{'s' if len(texts) != 1 else ''} to a list, "
            "with nothing checking opt-in first",
            "Contacts arrive in this workflow as a list — a tag, an import, a "
            "bulk add — and it starts texting without checking any opt-in "
            "field, tag or DND state on the way in. Consent is the only thing "
            "standing between an SMS campaign and a carrier complaint: it is "
            "what the aggregator was shown at 10DLC registration, and it is "
            "what damages are counted against per message when it is missing. "
            "If the opt-in exists in a form archive somewhere, the workflow "
            "still cannot see it, so nobody can prove it from the account.",
            "Gate the first send on the consent record: an If/Else at the top "
            "that only continues for contacts carrying the opt-in tag or "
            "field, with everything else routed to an email-only path. Then "
            "check the list itself — anyone on it who never opted in to TEXTS "
            "specifically does not belong in an SMS campaign, whatever they "
            "opted into elsewhere.",
            step=texts[0].name or texts[0].type,
            cost="One complaint from a contact who never opted in can cost more "
                 "than the campaign earns, and it arrives with the number "
                 "already filtered. Every text after that is invisible.")


# --------------------------------------------------------------------------
# GHL054 — quiet hours, as bounds rather than as a checkbox
# --------------------------------------------------------------------------

# Federal TCPA: no telemarketing call or text before 8am or after 9pm in the
# CALLED PARTY's local time. Around ten states are stricter and stop at 8pm —
# Florida's FTSA and Oklahoma's Telephone Solicitation Act are the two usually
# cited — so a window that runs to 9pm is federally legal and still exposed on
# a nationwide list. That gap is why this rule reports in two registers, and
# why the two must never be confused with one another; see the branches below.
TCPA_OPEN = 8 * 60
TCPA_CLOSE = 21 * 60
STATE_CLOSE = 20 * 60
DAY_MINUTES = 24 * 60

CLOCK = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?\s*$", re.I)
WINDOW_START_KEYS = ("start", "starttime", "startat", "from", "fromtime",
                     "begin", "opens", "open", "earliest", "after")
WINDOW_END_KEYS = ("end", "endtime", "endat", "to", "totime", "finish",
                   "closes", "close", "latest", "before")
WINDOW_HOLDER_KEYS = ("window", "sendingwindow", "sendwindow", "quiethours",
                      "resumewindow", "deliverywindow")
# Toggling a send window off in the builder does not clear the times it was
# set to. An export therefore carries the old bounds next to an explicit
# disable flag, and reading those bounds as live accuses a client of texting
# at hours the workflow is not sending at at all.
WINDOW_OFF_KEYS = ("enabled", "isenabled", "active", "isactive", "on",
                   "enable", "applywindow", "usewindow")
WINDOW_OFF_WORDS = ("false", "0", "no", "off", "disabled", "none")


def _minutes(value):
    """A clock time as minutes past midnight, or None if it is not one.

    Exports write a bound as "9:00", "09:00", "9am", or a bare hour. Anything
    else — a merge field, a list, a cron string — is unreadable, and an
    unreadable bound must never become a finding: this rule tells a client they
    are breaking a federal statute, so it only speaks when it can read the
    number it is accusing them over.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        hour = int(value)
        return hour * 60 if 0 <= hour <= 24 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "{{" in text:
        return None
    meridiem = CLOCK.match(text)
    if meridiem:
        hour = int(meridiem.group(1)) % 12
        minute = int(meridiem.group(2) or 0)
        if meridiem.group(3).lower() == "p":
            hour += 12
        return hour * 60 + minute if minute < 60 else None
    plain = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$", text)
    if not plain:
        return None
    hour, minute = int(plain.group(1)), int(plain.group(2) or 0)
    if hour > 24 or minute > 59:
        return None
    return hour * 60 + minute


def _window_is_off(window) -> bool:
    """True when the window carries an explicit 'switched off' flag."""
    if not isinstance(window, dict):
        return False
    for key, value in window.items():
        if _nk(key) not in WINDOW_OFF_KEYS:
            continue
        if isinstance(value, str):
            if value.strip().lower() in WINDOW_OFF_WORDS:
                return True
        elif value is False or value is None:
            return True
        elif isinstance(value, (int, float)) and value == 0:
            return True
    return False


def _bounds(window):
    """(open, close) in minutes past midnight for a window dict."""
    if not isinstance(window, dict) or _window_is_off(window):
        return None, None
    start = end = None
    for key, value in window.items():
        nk = _nk(key)
        if start is None and nk in WINDOW_START_KEYS:
            start = _minutes(value)
        elif end is None and nk in WINDOW_END_KEYS:
            end = _minutes(value)
    return start, end


def _windows(wf: Workflow):
    """(label, window) for every send window this workflow imposes.

    The workflow-level one is the common case, but a wait step can carry its
    own "resume inside these hours" window, and a step-level window is subject
    to exactly the same statute as the workflow-level one.
    """
    top = wf.send_window()
    if top:
        yield "", top
    for step in wf.steps:
        cfg = step.config()
        if not isinstance(cfg, dict):
            continue
        for key, value in cfg.items():
            if _nk(key) in WINDOW_HOLDER_KEYS and isinstance(value, dict) and value:
                yield step.name or step.type, value


def _clock(mins) -> str:
    total = int(mins) % DAY_MINUTES
    hour, minute = divmod(total, 60)
    if total == 0:
        return "midnight"
    return f"{hour % 12 or 12}:{minute:02d}{'am' if hour < 12 else 'pm'}"


@rule("GHL054", "Send window bounds sit outside safe texting hours",
      "critical", "compliance", "compliance", "timing", "sms")
def quiet_hours_bounds_are_illegal(acct: Account):
    """The window exists — and this rule reads the NUMBERS in it.

    GHL013 asks whether a window is evaluated in the contact's timezone and
    GHL029 asks whether there is a window at all. Both can pass on a workflow
    that is still breaking the law, because neither reads the numbers. A
    7am-10pm window is a window, in the contact's timezone, and it texts people
    an hour before and an hour after the hours the TCPA allows — which is the
    single most litigated fact pattern in SMS marketing, priced per message.

    Two registers, never confused:

      critical — the bounds are outside 8am-9pm, so the workflow is configured
                 to send at hours federal law does not allow. This is the
                 accusation, and it only ever appears when the numbers support
                 it exactly.
      medium   — the bounds are INSIDE the federal hours and the window closes
                 after 8pm, which is legal everywhere federally and exposed in
                 the states that stop at 8pm.

    The rule title covers both because "safe texting hours" is not the same
    set as "legal hours". An earlier version titled itself "outside the legal
    hours" and then fired its state branch on 08:00-21:00 — the textbook
    federally-correct window — so the finding contradicted the rule it came
    from and read as a legal accusation against an account that had done
    everything right. The state branch is worth keeping: closing at 8pm costs
    nothing in replies and removes the exposure. It is worth keeping only if it
    never masquerades as the federal one, which is what the split title, the
    medium severity and the finding's own wording are all for.

    Only workflows that place a phone call or send a text are checked: quiet
    hours are a telephone rule, and an email window outside these bounds is a
    preference, not a violation.
    """
    for wf in acct.published():
        phone = [s for s in wf.outbound
                 if s.is_sms or s.type in ("call", "manual_call", "voicemail")]
        if not phone:
            continue
        for label, window in _windows(wf):
            start, end = _bounds(window)
            if start is None and end is None:
                continue  # unreadable or switched off: say nothing rather than guess
            step = label or (phone[0].name or phone[0].type)

            if start is not None and end is not None and end <= start:
                yield _finding(
                    "GHL054", "critical", wf,
                    f"Send window runs overnight ({_clock(start)} to "
                    f"{_clock(end)})",
                    "This window closes earlier in the day than it opens, so "
                    "it covers the hours THROUGH the night rather than the "
                    "hours inside the day. Every message held by it is "
                    "released into the middle of the night, which is both the "
                    "worst possible first impression and a straight breach of "
                    "the 8am-9pm quiet-hours rule.",
                    "Set the window to open in the morning and close in the "
                    "evening (9am to 8pm in the contact's timezone is the safe "
                    "nationwide default). If overnight delivery is genuinely "
                    "wanted for an internal alert, move that step to a "
                    "workflow with no window instead.",
                    step=step, reach=len(phone),
                    cost="Texts at 2am, to every contact this workflow holds. "
                         "Quiet-hours damages are counted per message, and the "
                         "opt-outs arrive with them.")
                break

            early = start is not None and start < TCPA_OPEN
            late = end is not None and end > TCPA_CLOSE
            if early or late:
                breaches = []
                if early:
                    breaches.append(f"opens at {_clock(start)} (before the 8am floor)")
                if late:
                    breaches.append(f"closes at {_clock(end)} (after the 9pm ceiling)")
                yield _finding(
                    "GHL054", "critical", wf,
                    "Send window " + " and ".join(breaches),
                    "The TCPA bans telemarketing calls and texts before 8am "
                    "and after 9pm in the recipient's own local time, and this "
                    "window is set outside those hours. The workflow will do "
                    "exactly what it was configured to do — hold each message "
                    "until the window opens, then send it at an hour that is "
                    "not lawful to send at. Damages are statutory and counted "
                    "per message, so the exposure scales with the size of the "
                    "list, not with the size of the mistake.",
                    "Narrow the window to 8am-9pm at the widest, and 9am-8pm "
                    "if the list is nationwide — Florida, Oklahoma and several "
                    "other states stop at 8pm, and the strictest state on your "
                    "list sets the real limit. Confirm the same screen has the "
                    "window running in the CONTACT's timezone, not the "
                    "account's: correct bounds on the wrong clock breach the "
                    "statute just as reliably.",
                    step=step, reach=len(phone),
                    cost="Every message sent outside these hours is its own "
                         "claim, at statutory damages per text, on top of the "
                         "opt-outs and complaints that get the number filtered.")
                break

            if end is not None and STATE_CLOSE < end <= TCPA_CLOSE:
                yield _finding(
                    "GHL054", "medium", wf,
                    f"Send window closes at {_clock(end)} — inside the federal "
                    "hours, outside the strictest state ones",
                    f"Nothing here is a federal breach: this window closes at "
                    f"{_clock(end)}, which is inside the 8am-9pm the TCPA "
                    "allows. It is a tightening worth making anyway. Around ten "
                    "states stop telemarketing texts at 8pm rather than 9pm — "
                    "Florida and Oklahoma are the two usually cited — so on a "
                    "nationwide list this window is compliant for most of the "
                    "contacts in it and not for the rest, and which contacts "
                    "those are depends on where they live rather than on "
                    "anything visible in the account.",
                    "Close the window at 8pm. The last hour of the evening is "
                    "the worst-performing send hour in the day anyway, so this "
                    "costs nothing in results and removes the exposure "
                    "entirely. If the list is known to be single-state and that "
                    "state follows the federal hours, leave it — this one is a "
                    "risk posture, not a defect.",
                    step=step, reach=len(phone),
                    cost="A late-evening send is worth almost nothing in "
                         "replies and carries the whole of the state-law risk "
                         "on the list.")
                break


# --------------------------------------------------------------------------
# GHL055 — the reply keyword the message promises
# --------------------------------------------------------------------------

# The keyword set the messaging provider answers by itself, before the message
# ever reaches a workflow. Twilio — which is what LC Phone runs on — handles
# STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, REVOKE and OPTOUT as opt-outs
# and reserves HELP/INFO for the help reply. Any other word is just a word in a
# text message: nothing in the stack does anything with it.
HONOURED = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit",
            "revoke", "optout", "opt-out", "help", "info"}

# One of those words offered as a keyword anywhere in the same message. Matched
# case-SENSITIVELY because uppercase is the keyword convention: a lowercase
# "cancel" is ordinary prose ("we'll cancel your slot") and reading it as a
# working opt-out would silence the rule on most of the texts it exists for.
HONOURED_IN_BODY = re.compile(
    r"\b(?:STOP|STOPALL|UNSUBSCRIBE|CANCEL|END|QUIT|REVOKE|OPTOUT)\b")

OPT_OUT_INTENT = (r"stop|opt\s*-?\s*out|unsubscribe|be\s+removed|remove\s+you|"
                  r"removed|taken\s+off|off\s+(?:this|the)\s+list|no\s+more|"
                  r"cancel|end\s+these|quit")

# The invited word itself, and it must be in CAPS. Everything around it is read
# case-insensitively; this one group is not, via a scoped flag.
#
# Without that, the capture group takes whatever word follows "reply" and the
# rule reports it as a promised keyword — so "Reply to this message to opt out"
# became "tells people to reply TO and nothing listens for it", and "Just reply
# and we'll take you off the list" became AND. Four of the five most natural
# ways to write a correct opt-out instruction produced a high-severity finding
# naming an English stopword. Uppercase is what an actual keyword looks like in
# SMS copy, and it is the same convention HONOURED_IN_BODY already relies on.
# The cost is a missed lowercase "reply remove" — which is prose as often as it
# is an instruction, and a missed finding is the cheap direction here.
KEYWORD = r"(?-i:([A-Z][A-Z0-9-]{1,11}))"

# "Reply REMOVE to be taken off this list."
INVITE_THEN_INTENT = re.compile(
    r"\b(?:reply|text|txt|send|respond)\s+(?:back\s+|with\s+)?[\"'“]?"
    + KEYWORD + r"[\"'”]?[^.!?\n]{0,25}?\b(?:" + OPT_OUT_INTENT + r")\b", re.I)

# "To opt out, reply OFF."
INTENT_THEN_INVITE = re.compile(
    r"\b(?:" + OPT_OUT_INTENT + r")\b[^.!?\n]{0,25}?\b"
    r"(?:reply|text|txt|send|respond)\s+(?:back\s+|with\s+)?[\"'“]?"
    + KEYWORD, re.I)

# The opposite mistake: a standard opt-out word offered for something that is
# not an opt-out. Anyone who follows the instruction is unsubscribed from every
# message the account will ever send them. Case-INSENSITIVE here, and that is
# not an oversight: Twilio matches opt-out keywords case-insensitively, so a
# lowercase "reply cancel" opts the contact out exactly as thoroughly.
HIJACKED_KEYWORD = re.compile(
    r"\b(?:reply|text|txt|send|respond)\s+(?:back\s+|with\s+)?[\"'“]?"
    r"(stop|stopall|cancel|end|quit|unsubscribe|revoke)[\"'”]?\s*"
    r"(?:to\s+|if\s+you\s+(?:want\s+to\s+|need\s+to\s+)?)"
    r"(cancel|reschedul\w*|change|move|confirm|skip|pause|end)\b"
    # ...unless the thing being cancelled IS the messaging, which is what the
    # keyword is for. "Reply END to end these texts" is correct usage.
    r"(?![^.!?\n]{0,30}\b(?:messages?|texts?|texting|subscription|receiving|"
    r"these|updates?|alerts?|reminders?|lists?|out|unsubscrib\w*)\b)", re.I)


def _keyword_candidates(body: str) -> list:
    out = []
    for pattern in (INVITE_THEN_INTENT, INTENT_THEN_INVITE):
        for match in pattern.finditer(body):
            word = match.group(1).strip().lower()
            if word and word not in out:
                out.append(word)
    return out


def _listened_for(acct: Account) -> str:
    """Every string sitting in a trigger anywhere in the account, lowercased.

    A workflow that catches a custom keyword does it with an inbound-message
    trigger filtered on the word, so the word is in that trigger's own config.
    Draft workflows are read too: a listener sitting in draft is a different
    defect from no listener at all, and reading generously keeps this rule
    quiet wherever somebody has clearly thought about the keyword.
    """
    try:
        return json.dumps([t.raw for w in acct.workflows
                           for t in w.triggers]).lower()
    except (TypeError, ValueError):  # pragma: no cover - non-JSON export
        return ""


@rule("GHL055", "Reply keyword does not do what the message says", "high",
      "compliance", "compliance", "sms", "opt-out")
def keyword_does_not_match_the_promise(acct: Account):
    """Two halves of one defect: a dead opt-out word, and a hijacked live one.

    The provider honours a fixed set of opt-out keywords and nothing else. Tell
    a contact to "reply REMOVE" and nobody is listening: they believe they
    opted out, the sequence keeps sending, and the next thing they press is
    report-as-spam — which is the complaint rate carriers actually filter on.

    The mirror image is worse and far more common: "reply CANCEL to cancel your
    appointment". CANCEL is a standard opt-out keyword, so the contact who
    follows the instruction is marked do-not-disturb across the whole account
    and can never be texted again — no error, no warning, and the appointment
    is not cancelled either.

    This is a check on a SPECIFIC promise the copy makes, which is what keeps
    it distinct from GHL017. GHL017 asks whether the sequence carries opt-out
    language at all; this asks whether the word it offers is the word the
    provider answers.
    """
    listened = _listened_for(acct)
    for wf in acct.published():
        for step in wf.sms_steps:
            body = step.bodies()
            if not body:
                continue

            hijack = HIJACKED_KEYWORD.search(body)
            if hijack:
                word, purpose = hijack.group(1).upper(), hijack.group(2).lower()
                yield _finding(
                    "GHL055", "high", wf,
                    f"'{word}' is offered as the way to {purpose} — it opts "
                    "the contact out of everything",
                    f"This text invites the contact to reply {word}, and "
                    f"{word} is one of the standard opt-out keywords the "
                    "messaging provider handles before any workflow sees it. "
                    "Anyone who follows the instruction is marked "
                    "do-not-disturb for the entire account: no text from any "
                    "workflow reaches them again, nothing in the interface "
                    "says why, and whatever they were actually trying to do "
                    "did not happen either.",
                    f"Pick a word the provider does not own — 'reply RESCHEDULE'"
                    f" or a booking link — and keep {word} for opting out. Then "
                    "check the DND list for contacts who went quiet after this "
                    "text: they did not lose interest, they were unsubscribed "
                    "by the instruction.",
                    step=step.name or step.type,
                    cost="Every contact who does what the text asks is silently "
                         "lost from the SMS channel forever, and they are the "
                         "engaged ones — they replied.")
                continue

            if HONOURED_IN_BODY.search(body):
                # This message already offers a word that works. Whatever else
                # it invites — "reply BOOK to grab a time, or STOP to opt out"
                # — the contact has a working way out, and reading the other
                # keyword as a broken opt-out would be an invented finding.
                continue
            for word in _keyword_candidates(body):
                if word in HONOURED:
                    continue
                if re.search(rf"\b{re.escape(word)}\b", listened):
                    continue  # something in the account is catching it
                yield _finding(
                    "GHL055", "high", wf,
                    f"Text tells people to reply '{word.upper()}' and nothing "
                    "listens for it",
                    f"This message offers {word.upper()} as the way out. The "
                    "provider only honours the standard set — STOP, STOPALL, "
                    "UNSUBSCRIBE, CANCEL, END, QUIT, REVOKE, OPTOUT — and no "
                    "trigger in this account is filtered on the word either. "
                    f"So a contact who replies {word.upper()} is not "
                    "unsubscribed, is not tagged, and keeps receiving the rest "
                    "of the sequence after asking twice to be left alone. The "
                    "next thing they press is report-as-spam, and complaint "
                    "rate is the number carriers actually filter on.",
                    "Use 'Reply STOP to opt out' in the copy — it is the "
                    "wording the provider honours automatically and the one "
                    "declared at 10DLC registration. If the custom word has to "
                    "stay, build the listener: an inbound-message trigger "
                    f"filtered on '{word}' that sets DND and removes the "
                    "contact from every sequence.",
                    step=step.name or step.type,
                    cost="A contact who asked to stop and kept receiving texts "
                         "is the highest-probability spam complaint you can "
                         "manufacture, and complaints are what get a number "
                         "filtered for every workflow at once.")
                break


# --------------------------------------------------------------------------
# GHL056 — content the carriers will not carry
# --------------------------------------------------------------------------

# SHAFT (Sex, Hate, Alcohol, Firearms, Tobacco) plus the categories carriers
# added around it on 10DLC: cannabis and CBD, gambling, and the loan/debt/credit
# family. Hate speech is not attempted here — it cannot be detected from a word
# list without being wrong about it, and being wrong about THAT accusation in a
# client's report is unrecoverable.
#
# Every pattern is a concrete noun with a word boundary. The tempting generic
# words are left out on purpose: "bar", "shot", "high" and "weed" all appear in
# ordinary copy from restaurants, clinics and lawn-care companies, and a lawn
# company reading "weed control — flagged as cannabis" in its audit stops
# trusting the whole document. The same test retired four more on this pass:
# "jackpot" ("you hit the jackpot — 20% off"), bare "betting" ("I'm betting
# you'll love it"), "xxx" (a masked phone number, reported as ADULT CONTENT to a
# roofing company) and bare "escort" ("our team will escort you from the car
# park"). An accusation of adult content is the one this report can least
# afford to get wrong, so both of those now need the full phrase.
RESTRICTED = (
    ("alcohol", re.compile(
        r"\b(?:alcohol|beers?|wines?|whisk(?:e)?y|vodka|tequila|bourbon|rum|"
        r"liquor|happy hour|brewery|distillery|open bar)\b|"
        # "cocktail attire" and "cocktail dress" are dress codes, not drinks.
        r"\bcocktails?\b(?!\s+(?:attire|dress))", re.I)),
    ("cannabis or CBD", re.compile(
        # CBD is read as cannabidiol. It also abbreviates "central business
        # district" outside the US — but the filtering this rule predicts is
        # US carrier filtering, so the US reading is the one that matters.
        r"\b(?:cannabis|marijuana|thc|cbd|dispensar(?:y|ies)|delta[\s-]?8)\b",
        re.I)),
    ("tobacco or vape", re.compile(
        r"\b(?:tobacco|cigarettes?|cigars?|vapes?|vaping|e-?cigs?|nicotine|"
        r"hookah)\b", re.I)),
    ("firearms", re.compile(
        r"\b(?:firearms?|handguns?|rifles?|shotguns?|pistols?|ammunition|ammo|"
        r"ar-?15|silencers?|gun (?:range|show|shop))\b", re.I)),
    ("gambling", re.compile(
        r"\b(?:casinos?|sportsbook|poker|lottery|free spins|wagers?|"
        r"sports\s?betting|betting\s+(?:app|site|odds|slip|line)s?|"
        r"place a bet|bet now)\b", re.I)),
    ("loans or debt relief", re.compile(
        r"\b(?:payday loans?|debt (?:relief|consolidation|settlement)|"
        r"credit repair|loan forgiveness|fix your credit|get rich quick)\b",
        re.I)),
    ("adult content", re.compile(
        r"\b(?:adult (?:content|entertainment)|escort (?:service|agency)|"
        r"onlyfans|strip club|nsfw)\b", re.I)),
)


@rule("GHL056", "Carrier-restricted content in a text", "high",
      "deliverability", "compliance", "sms", "content")
def restricted_content_in_sms(acct: Account):
    """SHAFT and its 10DLC neighbours, sitting in a message body.

    Carriers filter these categories on the content of the message itself,
    independently of the campaign's registration, and the filtering is silent:
    the send succeeds in GoHighLevel, the delivery report says nothing useful,
    and the message is dropped somewhere between the aggregator and the
    handset. Repeat matches escalate from filtering to campaign suspension and
    carrier violation fees.

    Only message bodies are read. Scanning step names would flag a workflow
    called "Wine Club Reminders" whose copy is perfectly clean, and the
    carriers read the body, not the label.
    """
    for wf in acct.published():
        hits = []
        for step in wf.sms_steps:
            body = step.bodies()
            if not body:
                continue
            for label, pattern in RESTRICTED:
                match = pattern.search(body)
                if match:
                    hits.append((step, label, match.group(0)))
                    break
        if not hits:
            continue
        step, label, phrase = hits[0]
        categories = sorted({h[1] for h in hits})
        yield _finding(
            "GHL056", "high", wf,
            f"{len(hits)} text{'s' if len(hits) != 1 else ''} "
            f"carr{'y' if len(hits) != 1 else 'ies'} "
            f"{', '.join(categories)} content ('{phrase}')",
            "US carriers restrict this category on 10DLC traffic and filter on "
            "the words in the message body, whatever the campaign was "
            "registered as. Nothing about that failure is visible from inside "
            "the account: the workflow reports the message as sent, and it is "
            "dropped between the aggregator and the handset. Sustained matches "
            "escalate — first this campaign is filtered, then the number is, "
            "and carrier violation fees land on the account behind it.",
            "Take the restricted wording out of the SMS and move the detail to "
            "the landing page the text links to — the page is not carrier "
            "filtered. If this is genuinely an age-restricted business, it "
            "needs its own compliant campaign registration with age gating and "
            "explicit opt-in, not a general marketing campaign, and that is a "
            "conversation with the aggregator before the next send.",
            step=step.name or step.type, reach=len(hits),
            cost="Filtered messages are billed and never delivered, and nothing "
                 "reports them as missing. The campaign looks like it is "
                 "running right up to the point the number is suspended.")


# --------------------------------------------------------------------------
# GHL057 — who is texting
# --------------------------------------------------------------------------

# Enrolments where the contact reached out seconds ago and is expecting the
# reply. Identification still helps there, but the recipient is not wondering
# who this is, and a compliance report that says otherwise is padding.
REPLYING_TRIGGERS = ("form_submitted", "survey_submitted", "order", "payment",
                     "invoice", "appointment", "inbound_message", "call")

IDENTIFIES_PHRASE = re.compile(
    r"\bthis is\b|\bmy name is\b|\bcalling from\b|\bon behalf of\b|"
    r"\bteam at\b|\bhere at\b|"
    r"\{\{\s*(?:location|account|business|company)[._]|"
    r"\{\{\s*custom_values\.[a-z0-9_]*"
    r"(?:business|company|brand|location|practice|clinic|shop|studio|agency|"
    r"name)", re.I)

# A capitalised word after from/with/at/by, or one immediately before "here" —
# how a business name actually appears in a text ("from Northgate Roofing",
# "at the Northgate Clinic", "Northgate here"). Case-sensitive on purpose:
# lowercasing this turns every "at 3pm" and "with you" into a business name and
# the rule stops firing at all.
IDENTIFIES_BRAND = re.compile(
    r"\b(?:from|with|at|by)\s+(?:the\s+)?[A-Z][\w&'’.-]+|"
    r"\b[A-Z][\w&'’.-]{2,}\s+here\b")

# "Northgate Roofing: your quote is ready" — the brand led with, before the
# message. A common and perfectly compliant opening shape that neither pattern
# above sees, because there is no from/with/at/by and no "here". Only a colon
# or a dash counts as the separator, and a greeting in front disqualifies it:
# a plain hyphen after any capitalised word would read "Hey Jen - still
# interested?" as an identified sender, which is the exact text this rule
# exists to catch.
IDENTIFIES_LEAD = re.compile(
    r"^\s*(?!(?:Hi|Hey|Hello|Good|Quick|Just|Thanks|Thank|Reminder|Your|You|"
    r"It|We|This|That|Still|Sorry|Congrats|Happy|Update|Heads)\b)"
    r"[A-Z][\w&'’.-]{2,}(?:\s+[A-Z][\w&'’.-]+){0,3}\s*[:–—]\s")


@rule("GHL057", "First text never says who is sending it", "medium",
      "compliance", "compliance", "sms", "copy")
def first_touch_does_not_identify(acct: Account):
    """The opening message of a sequence with no business name in it.

    Carriers expect the sender identified at the start of a conversation, and
    the recipient's phone shows a ten-digit number it has never seen. A text
    that opens "Hey Jen, still interested?" from an unknown number is
    indistinguishable from a scam, and the two things people do with it are
    ignore it and report it — the second of which is the complaint rate that
    gets the number filtered.

    Two things narrow it, and both are about not overclaiming. The contact must
    not have just contacted you: a reply to a form submitted a minute ago is a
    conversation the contact started. And the message must sit on the TRUNK —
    the steps every enrolled contact passes through before the workflow forks.
    A text below a fork is the first message on one branch of several, not the
    first message of the conversation, and this rule's whole argument is that
    the recipient has never seen the number. On a real account it fired on a
    booking nudge eleven nodes deep inside the HOT branch of an AI-routing
    workflow, whose symptom line — "this is the first message the contact
    receives" — was simply false about that step. A true observation attached
    to a false premise is still a false positive.
    """
    for wf in acct.published():
        trunk = _trunk(wf)
        first = next((s for s in trunk if s.is_outbound), None)
        if first is None or not first.is_sms:
            continue
        if any(any(k in t.canonical_type() for k in REPLYING_TRIGGERS)
               for t in wf.triggers):
            continue
        body = first.bodies()
        if not body.strip():
            continue
        if IDENTIFIES_PHRASE.search(body) or IDENTIFIES_BRAND.search(body) \
                or IDENTIFIES_LEAD.search(body):
            continue
        yield _finding(
            "GHL057", "medium", wf,
            "Opening text arrives from an unknown number with no business name "
            "in it",
            "This is the first message the contact receives from this "
            "workflow, and it never says who is texting. On their phone it is "
            "a ten-digit number they have never seen, saying something "
            "familiar — which is exactly the shape of every scam text they get "
            "in a week. Carriers expect the sender identified at the start of "
            "a conversation, and the ones that are not get reported rather "
            "than answered.",
            "Put the business name in the first sentence — 'Hi {{contact."
            "first_name}}, it's Dana from Northgate Roofing' — and keep it out "
            "of the later messages in the sequence, where it reads as "
            "automated. Merge it from a custom value so a rebuild cannot lose "
            "it.",
            step=first.name or first.type,
            cost="An unidentified first text gets ignored, and the share that "
                 "gets reported as spam instead is what pushes the number over "
                 "the complaint threshold carriers filter on.")


# --------------------------------------------------------------------------
# GHL058 — how many texts land in one day
# --------------------------------------------------------------------------

DURATION_UNITS = {"second": 1 / 60, "seconds": 1 / 60, "sec": 1 / 60,
                  "secs": 1 / 60, "minute": 1, "minutes": 1, "min": 1,
                  "mins": 1, "m": 1, "hour": 60, "hours": 60, "hr": 60,
                  "hrs": 60, "h": 60, "day": 1440, "days": 1440, "d": 1440,
                  "week": 10080, "weeks": 10080, "w": 10080, "month": 43200,
                  "months": 43200}
DURATION_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)", re.I)
DELAY_KEYS = ("delay", "duration", "wait", "waitfor", "waittime", "time",
              "period", "startafter", "delayfor")
UNIT_KEYS = ("unit", "units", "type", "period", "interval", "frequency")
AMOUNT_KEYS = ("value", "amount", "duration", "delay", "number", "qty",
               "count", "quantity")

# Three is where a sequence stops reading as attentive and starts reading as
# broken. Two texts in a day is a deliberate and defensible pattern — a message
# and a nudge for the people who did not answer. The third one inside the same
# day is what produces the STOP reply and the spam report, and complaint rate is
# the number the carriers grade a sending number on.
BURST_LIMIT = 3


def _duration_from_text(text: str):
    """Minutes described by a string like "2 days" or "1 hour 30 minutes"."""
    total = None
    for amount, unit in DURATION_TEXT.findall(text):
        factor = DURATION_UNITS.get(unit.lower())
        if factor is None:
            continue
        try:
            total = (total or 0) + float(amount) * factor
        except ValueError:  # pragma: no cover - regex guarantees a number
            continue
    return total


def _value_unit(cfg: dict):
    """Minutes from the {"value": 3, "unit": "hours"} shape, in its variants."""
    unit = None
    for key, value in cfg.items():
        if _nk(key) in UNIT_KEYS and isinstance(value, str):
            unit = DURATION_UNITS.get(value.strip().lower())
            if unit is not None:
                break
    if unit is None:
        return None
    for key, value in cfg.items():
        if _nk(key) in AMOUNT_KEYS and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            return float(value) * unit
    return None


def _wait_minutes(step: Step):
    """How long this wait holds a contact, or None when the export does not say.

    Nothing is inferred from the step's NAME. "Wait 4 hours" as a label with an
    empty config is extremely common in exports where the delay lives somewhere
    this reader cannot see, and a rule that counts messages per day cannot base
    that count on what somebody typed in a text box.
    """
    cfg = step.config()
    if not isinstance(cfg, dict):
        return None
    for key, value in cfg.items():
        if _nk(key) not in DELAY_KEYS:
            continue
        if isinstance(value, str):
            found = _duration_from_text(value)
            if found is not None:
                return found
        elif isinstance(value, dict):
            found = _value_unit(value)
            if found is not None:
                return found
    found = _value_unit(cfg)
    if found is not None:
        return found
    # {"hours": 2, "minutes": 30} is one delay written in two keys. Returning
    # on the first of them under-reads the wait, and every minute this reader
    # loses makes the messages look closer together than they are.
    total = None
    for key, value in cfg.items():
        factor = DURATION_UNITS.get(_nk(key))
        if factor and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            total = (total or 0) + float(value) * factor
    return total


def _span(minutes: float) -> str:
    if minutes < 60:
        return "under an hour"
    if minutes < 120:
        return "an hour"
    if minutes < DAY_MINUTES:
        return f"{int(round(minutes / 60))} hours"
    return "a day"  # pragma: no cover - the caller only spans under a day


@rule("GHL058", "Three texts land on the same phone inside a day", "medium",
      "deliverability", "compliance", "sms", "frequency")
def texts_stacked_inside_one_day(acct: Account):
    """A sequence whose declared delays put three or more texts in 24 hours.

    Nothing in GoHighLevel caps how many messages one contact receives, so the
    cap is whatever the sequences add up to — and the recipient does not
    experience it per workflow, they experience it as their phone. Three texts
    from a business in a day reads as a malfunction, and the reply it earns is
    STOP. Complaint and opt-out rate is the number carriers grade a sending
    number on, so this is a deliverability defect before it is a taste one.

    Read conservatively on purpose, in three ways. The clock only advances on
    waits whose duration the export actually states, and it stops entirely at a
    wait that ends on an event — a reply wait can release in a second or never,
    and guessing which would put a finding in a client's report that the file
    does not support. The count runs per PATH, not down the flat step list: an
    advanced-builder export writes every branch into one list, so three texts
    sitting in three mutually exclusive branches look like a burst and are
    actually one message. And appointment-triggered ladders are exempt: their
    sends are timed off a slot the contact chose, and three reminders before a
    booking is the correct build.
    """
    for wf in acct.published():
        if acct.config.is_transactional(wf.name):
            continue
        if any(any(k in t.canonical_type() for k in
                   ("appointment", "invoice", "order", "payment"))
               for t in wf.triggers):
            continue

        worst, span, opener = 0, 0.0, None
        for path in _linear_paths(wf):
            clock = 0.0
            sent: list = []
            texts: list = []
            for step in path:
                if step.is_wait:
                    if step.type == "drip" or step.wait_is_conditional():
                        break  # unknowable duration — keep what is already known
                    minutes = _wait_minutes(step)
                    if minutes is None:
                        break
                    clock += minutes
                elif step.is_sms:
                    sent.append(clock)
                    texts.append(step)
            if len(sent) < BURST_LIMIT:
                continue
            for i, start in enumerate(sent):
                inside = [t for t in sent[i:] if t - start < DAY_MINUTES]
                if len(inside) > worst:
                    worst, span, opener = (len(inside), inside[-1] - start,
                                           texts[i])
        if worst < BURST_LIMIT or opener is None:
            continue

        yield _finding(
            "GHL058", "medium", wf,
            f"{worst} texts to the same contact within {_span(span)}",
            f"The delays in this workflow put {worst} text messages on one "
            f"phone inside {_span(span)}. The contact does not see a sequence, "
            "they see a business that will not stop texting them — and the "
            "reply that earns is STOP, from someone who was interested enough "
            "to be in the workflow in the first place. Opt-out and complaint "
            "rate is what carriers grade a sending number on, so a stacked "
            "sequence degrades delivery for every other workflow in the "
            "account as well.",
            "Spread the sequence: at most two texts in any 24 hours, with the "
            "later touches a day or more apart, and move the middle message to "
            "email — the same content, on a channel nobody counts against you. "
            "If the speed is genuinely worth it, keep the fast pair and push "
            "the third touch to the following day.",
            step=opener.name or opener.type, reach=worst,
            cost="The contacts this burns are the ones who opted in. An opt-out "
                 "is permanent, and the complaint rate behind it is what gets "
                 "the whole account's texts filtered.")


SUPPRESSION_TAG = re.compile(
    r"(do[\s_-]*not[\s_-]*(contact|call|text|email|disturb)"
    r"|(^|[\s_-])dnc([\s_-]|$)"
    r"|opt(ed)?[\s_-]*out"
    r"|unsubscrib"
    r"|blacklist"
    r"|suppress)", re.I)

# Step types that GATE a contact's path. A tag only enforces anything if
# something branches on it or filters entry on it. Deliberately excludes note
# bodies and message copy: "we tagged them do-not-contact" written into a note
# is a RECORD of the opt-out, not a thing that stops the next send.
#
# ⛔ Matched precisely, NOT by substring. "internal_notification" contains
# "if" — not-IF-ication — so a substring test read every rep-alert step as a
# branch condition, and the alert that says "opt-out to honour" then counted as
# the account reading its own opt-out tag. The rule went silent on the exact
# account it was written from. Same family of bug as "stage" containing "tag".
GATING_TYPES = frozenset({
    "if_else", "ifelse", "if", "condition", "conditional", "branch",
    "split", "filter", "decision", "goal", "wait_condition"})


def _is_gate(step_type: str) -> bool:
    t = (step_type or "").lower()
    return (t in GATING_TYPES
            or t.startswith("if_")
            or t.startswith("condition")
            or t.endswith("_condition")
            or t.endswith("_branch"))

# Keys GoHighLevel uses for the native Do-Not-Disturb switch. An account that
# flips native DND on an opt-out really has enforced it account-wide, and the
# tag beside it is then just a label — so this rule stays quiet.
DND_KEY = re.compile(r"\b(dnd|do_?not_?disturb)\b", re.I)


def _sets_native_dnd(acct: Account) -> bool:
    for wf in acct.workflows:
        for step in wf.steps:
            if DND_KEY.search(step.type):
                return True
            for key, value in (step.config() or {}).items():
                if DND_KEY.search(str(key)) and value not in (None, "", False, "false"):
                    return True
    return False


@rule("GHL101", "An opt-out is recorded and never enforced", "high",
      "compliance", "sms", "opt-out")
def optout_recorded_never_enforced(acct: Account):
    """A suppression tag that nothing branches on.

    The account writes the opt-out down and then keeps texting. This is not the
    same check as GHL017 (does the SMS carry opt-out language) or GHL072 (does
    something switch an opt-out back off) — here the contact asked to stop, the
    account correctly recorded it, and no workflow ever reads the record.

    Found on a real account, and the shape is worth stating because it looks
    handled from the inside. The reply handler tagged the contact
    `do-not-contact`, wrote a compliance note, emailed the rep "opt-out to
    honour", and pulled them from three named workflows. Ten other published
    workflows never checked the tag, nothing removed it, and no step set the
    native DND switch — so honouring the opt-out came down to a person reading
    an email. The tag existed in exactly one place in the whole export: the
    step that created it.

    A tag is only a gate if something branches on it. This looks at branch
    conditions and trigger filters, and at nothing else — a note body naming
    the tag is a record of the opt-out, not an enforcement of it, and counting
    it would let the most common way of getting this wrong pass silently.
    """
    if _sets_native_dnd(acct):
        return  # enforced account-wide at the platform level; the tag is a label

    written: dict = {}
    for wf in acct.workflows:
        for step in wf.steps:
            for tag in step.tags_added():
                key = str(tag).strip().lower()
                if key and SUPPRESSION_TAG.search(key):
                    written.setdefault(key, []).append((wf, step))
    if not written:
        return

    gates = []
    for wf in acct.workflows:
        for step in wf.steps:
            if _is_gate(step.type):
                gates.append(step.text())
        for trg in wf.triggers:
            gates.append(trg.filter_blob())

    if not gates:
        yield Skip(
            rule="GHL101",
            title="An opt-out is recorded and never enforced",
            reason="This export carries no branch conditions and no trigger "
                   "filters, so whether anything reads the opt-out tag cannot "
                   "be determined. An unread tag and a tag read by a condition "
                   "this file does not contain look identical.",
            needs="workflow branch conditions (if/else steps) and trigger "
                  "filters in the export",
            category="compliance")
        return

    blob = " ".join(gates).lower()
    senders = [w for w in acct.published() if w.sms_steps or w.email_steps]

    for tag, sites in sorted(written.items()):
        if tag in blob:
            continue
        wf, step = sites[0]
        where = f"{len(sites)} step{'s' if len(sites) != 1 else ''}"
        yield _finding(
            "GHL101", "high", wf,
            f"'{tag}' is written when someone opts out, and no workflow ever "
            f"reads it",
            f"{where} in this account put the '{tag}' tag on a contact, and "
            f"not one branch condition or trigger filter anywhere in the "
            f"account tests for it. {len(senders)} published workflows send "
            f"messages and none of them check it before sending. Nothing sets "
            f"the native Do-Not-Disturb switch either, so the record of the "
            f"opt-out exists and nothing acts on it — the contact who asked to "
            f"stop keeps receiving everything except the specific workflows "
            f"that were named by hand at the moment they asked.",
            f"Put a check for '{tag}' at the top of every workflow that sends, "
            f"or on each send step — or, better, have the opt-out set the "
            f"contact's Do-Not-Disturb switch, which stops the whole account "
            f"at once and cannot be forgotten in the next workflow somebody "
            f"builds. Then test it: tag a contact and confirm nothing sends.",
            step=step.name or step.type,
            category="compliance",
            cost="Every message to someone who asked to stop is its own "
                 "statutory claim, and the tag is a written record that the "
                 "account knew. It is the one compliance failure that is "
                 "harder to defend for having been detected correctly.")
