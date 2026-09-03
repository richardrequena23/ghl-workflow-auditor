"""Normalise GoHighLevel workflow exports into something the rules can reason about.

GHL hands back workflow data in more than one shape depending on where you got it
(account export, API response, a snapshot bundle, a hand-written fixture). Rather
than force one schema on the caller, everything funnels through here first, so a
rule never has to ask "which export is this?"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import AuditConfig

MERGE_FIELD = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)")
CUSTOM_VALUE = re.compile(r"\{\{\s*custom_values\.([a-zA-Z0-9_]+)")
PLACEHOLDER = re.compile(
    r"REPLACE[-_ ]?WITH|YOUR[-_ ]?[A-Z]+[-_ ]?HERE|TODO|XXXX|"
    r"\[[A-Z][A-Z0-9 _/'-]{3,}\]|<<[^>]{2,}>>|LOREM IPSUM", re.I)
# "Replied" / "No reply" branch labels off a wait step — the UI's way of listening.
REPLY_BRANCH = re.compile(r"^\s*(replied|reply|responded|answered)\b", re.I)
URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)

# A Liquid-style fallback: {{ contact.first_name | default: "there" }}. HighLevel
# documents fallback values as supported in "email templates, workflow emails,
# campaign emails, and bulk emails" — SMS is not on that list.
FALLBACK_FILTER = re.compile(r"\{\{[^}]*\|\s*(default|fallback)\s*:", re.I)


def slug(name: str) -> str:
    """GHL shows custom values by display name and merges them by key.

    'Integration Webhook URL' is written {{ custom_values.integration_webhook_url }}.
    Comparing the two forms directly reports every correctly wired field as missing.
    """
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")

# Step types that put a message in front of a human.
OUTBOUND = {"sms", "email", "voicemail", "call", "whatsapp", "gmb_message",
            "facebook_message", "instagram_message", "manual_sms", "manual_call"}

# Step types that pause the run. Only some of them can be released by a reply.
WAITING = {"wait", "drip", "event_start_wait"}

# Step types that put a message in front of a human, split by channel, because
# the channels do not behave the same. SMS has no merge-field fallback and is
# carrier-filtered; email has an unsubscribe obligation SMS does not.
SMS_TYPES = ("sms", "manual_sms", "mms")
EMAIL_TYPES = ("email", "manual_email", "send_email")

BRANCHING = ("if_else", "condition", "branch", "conditional", "ifelse", "split")

# A wait that ends on an EVENT rather than after a duration. If one of these has
# no timeout, a contact who never produces the event is parked in the workflow
# permanently — never messaged again, and never shown as an error anywhere.
CONDITIONAL_WAIT = re.compile(
    r"contact[_ -]?repl|customer[_ -]?repl|user[_ -]?repl|contact[_ -]?action|"
    r"specific[_ -]?condition|wait[_ -]?for[_ -]?condition|condition[_ -]?met|"
    r"goal[_ -]?event|email[_ -]?event|until[_ -]?condition", re.I)
TIMEOUT_KEY = re.compile(
    r"^(time_?out|max_?wait|maximum_?wait|wait_?limit|wait_?max|expire|expiry|"
    r"max_?duration|timeout_?after)", re.I)

# Where an ID reference to another account object hides. Keys are normalised to
# letters only before lookup, so calendarId / calendar_id / CalendarID all land
# in the same bucket.
ENTITY_KEYS = {
    "calendar": ("calendarid", "calendarids"),
    "user": ("userid", "userids", "assigneduserid", "assignedto", "assigneduser",
             "users", "teammembers", "recipientuserid"),
    "pipeline": ("pipelineid", "pipelineids"),
    "stage": ("stageid", "pipelinestageid", "stageids"),
    "form": ("formid", "formids"),
    "survey": ("surveyid", "surveyids"),
    "template": ("templateid", "emailtemplateid", "smstemplateid"),
    "workflow": ("workflowid", "workflowids", "targetworkflowid"),
}
_ENTITY_LOOKUP = {k: kind for kind, keys in ENTITY_KEYS.items() for k in keys}

# Steps that decide WHO handles the contact. A round robin with nobody in it
# assigns nobody, and HighLevel documents that a notification with no recipient
# is skipped silently.
ASSIGNMENT = re.compile(r"assign|round[_ -]?robin|rotate|distribut", re.I)
USER_POOL_KEYS = ("users", "userids", "teammembers", "members", "assignees",
                  "roundrobinusers")

# Standard contact fields that exist in every GoHighLevel location. Anything
# outside this set has to be a custom field, so it can be checked against the
# account's custom-field list. Deliberately generous: a name missing from here
# produces a false positive, and a false positive is the expensive mistake.
STANDARD_CONTACT_FIELDS = {
    "id", "contact_id", "first_name", "last_name", "name", "full_name",
    "full_name_lower_case", "email", "phone", "phone_raw", "company_name",
    "address1", "address", "full_address", "city", "state", "postal_code",
    "country", "timezone", "date_of_birth", "birthday", "source", "type",
    "assigned_to", "assigned_user", "owner", "tags", "website", "dnd",
    "date_created", "date_updated", "attribution_source", "attributions",
    "unsubscribe", "unsubscribe_link", "profile_photo", "business_name",
}


def _norm_key(key) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


_MARKUP = re.compile(r"<[^>]+>")


def _message_copy(step) -> str:
    """The words a recipient would read, normalised for comparison.

    Subject plus body, markup removed, whitespace collapsed, lower-cased. The
    same message written as HTML and as plain text has to compare equal, and a
    genuinely different message has to not.
    """
    raw = step.raw if isinstance(step.raw, dict) else {}
    # Exports nest the message under different keys depending on where they
    # came from: `attributes` in a workflow export, `meta` in a snapshot, and
    # occasionally flat on the step. Look in all three, nearest first.
    sources = [raw.get(k) for k in ("attributes", "meta", "data")]
    sources = [d for d in sources if isinstance(d, dict)] + [raw]
    parts = []
    for key in ("subject", "body", "message", "html", "text"):
        val = next((d[key] for d in sources
                    if isinstance(d.get(key), str) and d[key].strip()), None)
        if val:
            parts.append(val)
            # body and html are the same message in two encodings; one is
            # enough, and taking both would let a plain-text-only workflow
            # differ from its own HTML twin.
            if key in ("body", "message"):
                break
    joined = " ".join(parts)
    joined = _MARKUP.sub(" ", joined)
    return re.sub(r"\s+", " ", joined).strip().lower()


def _tag_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, str)]
    return []


def collect_tags(node) -> set[str]:
    """Every string sitting under a tag-ish key, anywhere in the structure.

    Tag names are compared case-insensitively because GHL matches them that way
    in triggers, even though the UI preserves the case you typed.
    """
    out: set[str] = set()

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if re.sub(r"[^a-z]", "", str(k).lower()) in ("tag", "tags", "tagname", "tagid"):
                    out.update(s.strip().lower() for s in _tag_strings(v))
                else:
                    walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return {t for t in out if t}


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # {"steps": [...]} / {"templates": [...]} / {"0": {...}, "1": {...}}
        for k in ("steps", "templates", "actions", "nodes", "triggers"):
            if isinstance(value.get(k), list):
                return value[k]
        if all(str(k).isdigit() for k in value):
            return [value[k] for k in sorted(value, key=lambda x: int(x))]
    return [value]


@dataclass
class Step:
    type: str
    name: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_outbound(self) -> bool:
        return self.type in OUTBOUND

    @property
    def is_wait(self) -> bool:
        return self.type in WAITING

    def config(self) -> dict:
        """The step's settings, wherever this export decided to hide them."""
        for key in ("meta", "data", "config", "settings", "params",
                    "parameters", "extra", "attributes"):
            sub = self.raw.get(key)
            if isinstance(sub, dict):
                return sub
        return self.raw

    def text(self) -> str:
        """Every string in the step, flattened. Used for merge-field scanning."""
        out: list[str] = []

        def walk(node):
            if isinstance(node, str):
                out.append(node)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(self.raw)
        return "\n".join(out)

    def wait_minutes(self):
        """How long this wait holds, in minutes. None when it is unbounded.

        A reply-wait with no duration can hold for days, so None means "assume
        long" — the conservative read for any rule asking whether a send has
        drifted away from the hour its trigger fired in.
        """
        if not self.is_wait:
            return None
        raw = self.raw if isinstance(self.raw, dict) else {}
        # Same nesting problem as the message body: `attributes` in a workflow
        # export, `meta` in a snapshot, sometimes flat on the step.
        sources = [raw.get(k) for k in ("attributes", "meta", "data")]
        sources = [d for d in sources if isinstance(d, dict)] + [raw]
        after = next((d["startAfter"] for d in sources
                      if isinstance(d.get("startAfter"), dict)), None)
        if after is None:
            return None
        try:
            value = float(after.get("value"))
        except (TypeError, ValueError):
            return None
        unit = str(after.get("type", "")).lower()
        per = {"minutes": 1, "minute": 1, "hours": 60, "hour": 60,
               "days": 1440, "day": 1440, "weeks": 10080, "week": 10080}
        if unit not in per:
            return None
        return value * per[unit]

    def tags_added(self) -> set[str]:
        """Tags this step puts ON a contact. Remove-tag steps return nothing."""
        t = self.type.lower()
        if "tag" not in t or "remove" in t or "delete" in t:
            return set()
        return collect_tags(self.raw)

    def wait_releases_on_reply(self) -> bool:
        """True if this wait ends early when the contact answers.

        A wait step that does NOT release on a reply is how a lead who already
        said "yes, call me" still receives the day-2 blast.
        """
        cfg = self.config()
        blob = json.dumps(cfg).lower()
        if _first(cfg, "stopOnResponse", "stop_on_response") is True:
            return True
        return any(marker in blob for marker in (
            "replied", "reply_received", "customer_replied", "contact_replied",
            "inbound_message", "responsereceived", "response_received",
        ))

    # -- wiring ---------------------------------------------------------
    @property
    def step_id(self) -> str:
        return str(_first(self.raw, "id", "_id", "stepId", "nodeId", default=""))

    @property
    def parent_key(self) -> str:
        """The node this one hangs off in the advanced builder.

        GoHighLevel writes it as `<parentId>-<branchName>` on a branch child, so
        it is a prefix match against a step id, never an equality test.
        """
        return str(_first(self.raw, "parentKey", "parent_key", "parentId",
                          "parent", default="") or "")

    def next_ids(self) -> list:
        nxt = _first(self.raw, "next", "nextStep", "nextStepId", "next_steps",
                     default=None)
        if isinstance(nxt, str):
            return [nxt]
        if isinstance(nxt, list):
            return [str(n) for n in nxt if isinstance(n, str)]
        return []

    # -- waits ----------------------------------------------------------
    def wait_is_conditional(self) -> bool:
        """True when this wait resumes on an EVENT, not after a duration.

        Duration waits always end. Event waits end only if the event happens,
        which is why the timeout on them is the difference between a follow-up
        and a lead nobody ever contacts again.
        """
        if not self.is_wait:
            return False
        cfg = self.config()
        declared = _first(cfg, "waitType", "wait_type", "type", "mode",
                          "resumeOn", "resume_on", default="")
        # GoHighLevel's hybrid wait declares itself with a bare word: a wait
        # whose `type` is "reply" resumes when the contact replies. "time" is
        # the duration wait and stays out of this.
        if str(declared).strip().lower() in ("reply", "event", "condition",
                                             "goal"):
            return True
        if declared and CONDITIONAL_WAIT.search(str(declared)):
            return True
        return bool(CONDITIONAL_WAIT.search(json.dumps(cfg)))

    def wait_timeout(self):
        """The wait's maximum duration, whatever this export calls it.

        Returns None when nothing timeout-shaped is set. A zero or an explicit
        'none' counts as absent — those are how the UI writes 'no maximum'.

        GoHighLevel's hybrid reply/event wait does not use a timeout-named key
        at all: the maximum lives in `startAfter` ({"type": "days", "value": 3})
        and the escape path is a `transitions` entry whose condition is
        "timeout"/"wait_timeout". Both are read here — calling a three-day
        reply wait with an explicit timeout branch "unbounded" was this
        auditor's own first false positive on a real account.
        """
        cfg = self.config()
        sa = _first(cfg, "startAfter", "start_after", default=None)
        if isinstance(sa, dict):
            v = sa.get("value")
            if v not in (None, "", 0, "0", False):
                return v
        for tr in (cfg.get("transitions") or []):
            if isinstance(tr, dict) and "timeout" in str(
                    _first(tr, "condition", "name", "type", default="")).lower():
                return "timeout branch"

        found = [None]

        def walk(node):
            if found[0] is not None:
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    if TIMEOUT_KEY.match(_norm_key(k)) or TIMEOUT_KEY.match(str(k)):
                        if v not in (None, "", 0, "0", False, "none", "None",
                                     "never", [], {}):
                            found[0] = v
                            return
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(cfg)
        return found[0]

    # -- branching ------------------------------------------------------
    def branches(self) -> list:
        """[(branch label, [child actions])] for an if/else style step.

        Only the shapes that actually carry their children inline are readable
        here. An export that flattens branch children into the workflow's step
        list with a parentKey is handled by the wiring check instead.
        """
        cfg = self.config()
        out = []
        wired = set(self.next_ids())
        for key in ("branches", "paths", "outcomes", "cases", "children"):
            raw = cfg.get(key) if isinstance(cfg, dict) else None
            if raw is None and isinstance(self.raw, dict):
                raw = self.raw.get(key)
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
                # GoHighLevel's builder wires branch children through the flat
                # step list: the branch objects here carry only the CONDITIONS,
                # and their ids reappear in the step's own `next` links.
                # Emptiness is not knowable from these entries, and calling
                # every such branch empty produced one false "silent exit" per
                # populated branch on a real account. The wiring check owns
                # this shape.
                if wired and any(
                        _first(e, "actions", "steps", "children", "nodes",
                               default=None) is None
                        and str(e.get("id") or "") in wired
                        for e in entries):
                    return []
                for entry in entries:
                    label = str(_first(entry, "name", "label", "title", "branch",
                                       default="(unnamed branch)"))
                    kids = _first(entry, "actions", "steps", "children", "nodes",
                                  default=None)
                    out.append((label, kids if isinstance(kids, list) else []))
                if out:
                    return out
        # An explicit else/none key, which is how the simplest exports write it.
        for key in ("else", "elseBranch", "else_branch", "none", "noneBranch",
                    "otherwise", "default"):
            for holder in (cfg if isinstance(cfg, dict) else {}, self.raw):
                if isinstance(holder, dict) and key in holder:
                    kids = holder[key]
                    out.append((key, kids if isinstance(kids, list) else
                                ([] if kids in (None, {}, "") else [kids])))
                    return out
        return out

    # -- references -----------------------------------------------------
    def entity_refs(self) -> list:
        """[(kind, id)] for every account object this step points at.

        Merge-field tokens are skipped — `{{ custom_values.calendar_id }}` is
        resolved at send time and cannot be checked against a list of IDs.
        """
        out = []

        def add(kind, value):
            if isinstance(value, str):
                v = value.strip()
                if v and "{{" not in v:
                    out.append((kind, v))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        add(kind, item)
                    elif isinstance(item, dict):
                        add(kind, _first(item, "id", "_id", "userId", default=""))

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    kind = _ENTITY_LOOKUP.get(_norm_key(k))
                    if kind:
                        add(kind, v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(self.raw)
        return out

    def user_pool(self):
        """The list of users this assignment step rotates through.

        None means the step does not declare one (a default pool, a calendar's
        own round robin). An empty list means it declares one and it is empty —
        which assigns the lead to nobody, silently.
        """
        if not ASSIGNMENT.search(self.type + " " + self.name):
            return None
        cfg = self.config()
        for holder in (cfg if isinstance(cfg, dict) else {}, self.raw):
            if not isinstance(holder, dict):
                continue
            for k, v in holder.items():
                if _norm_key(k) in USER_POOL_KEYS and isinstance(v, list):
                    return v
        return None

    def bodies(self) -> str:
        """Message text only — no step names, no ids, no settings.

        Scanning the whole step for a merge field finds them in fields the
        contact never sees, which is how a check like this starts lying.
        """
        out: list[str] = []

        def walk(node, key=""):
            if isinstance(node, str):
                if _norm_key(key) in ("body", "message", "text", "subject",
                                      "html", "content", "smsbody", "emailbody",
                                      "value", "note", "footer"):
                    out.append(node)
            elif isinstance(node, dict):
                for k, v in node.items():
                    walk(v, k)
            elif isinstance(node, list):
                for v in node:
                    walk(v, key)

        walk(self.raw)
        return "\n".join(out)

    @property
    def is_sms(self) -> bool:
        return self.type in SMS_TYPES

    @property
    def is_email(self) -> bool:
        return self.type in EMAIL_TYPES

    @property
    def is_branch(self) -> bool:
        return _norm_key(self.type) in {_norm_key(b) for b in BRANCHING}


@dataclass
class Trigger:
    type: str
    name: str = ""
    raw: dict = field(default_factory=dict)

    def filters(self) -> list:
        return _as_list(_first(self.raw, "filters", "conditions", "eventFilters",
                               "filter", default=[]))

    def filter_blob(self) -> str:
        return json.dumps(self.filters()).lower()

    def tag_values(self) -> set[str]:
        """The tag names this trigger listens for, in every export shape seen:
        {"tag": "x"}, {"tags": [...]}, and {"field": "tag", "value": "x"}."""
        tags = collect_tags(self.filters())
        for f in self.filters():
            if isinstance(f, dict) and "tag" in str(f.get("field", "")).lower():
                tags.update(s.strip().lower() for s in _tag_strings(f.get("value")))
        return {t for t in tags if t}

    # -- comparing one trigger against another --------------------------
    def canonical_type(self) -> str:
        """The trigger's type reduced to something comparable across exports.

        `contact_tag_added`, `contactTagAdded` and `ContactTagAdded` are one
        trigger written three ways. Comparing the raw strings reports two
        workflows racing on the same event as two unrelated triggers, which is
        exactly the collision this is supposed to find.
        """
        t = _norm_key(self.type)
        for needle, canon in (
                ("tagadded", "tag_added"), ("addedtag", "tag_added"),
                ("tagremoved", "tag_removed"), ("removedtag", "tag_removed"),
                ("contactcreated", "contact_created"),
                ("newcontact", "contact_created"),
                # Order matters: "orderformsubmit" contains "formsubmit", and
                # collapsing the two would erase the exact distinction GHL033
                # exists to check (pre-payment vs post-payment trigger).
                ("orderformsubmit", "order_form_submitted"),
                ("ordersubmit", "order_submitted"),
                ("formsubmit", "form_submitted"),
                ("surveysubmit", "survey_submitted"),
                ("inboundmessage", "inbound_message"),
                ("customerreplied", "inbound_message"),
                ("messagereceived", "inbound_message"),
                ("appointmentstatus", "appointment_status"),
                ("customerbookedappointment", "appointment_booked"),
                ("bookedappointment", "appointment_booked"),
                ("appointmentbooked", "appointment_booked"),
                ("callstatus", "call_status"),
                ("opportunitystatus", "opportunity_status"),
                ("opportunitystagechanged", "opportunity_stage"),
        ):
            if needle in t:
                return canon
        return t or "unknown"

    def canonical_filters(self) -> tuple:
        """The filter set, order- and spelling-insensitive.

        Two triggers filtered on the same tag are the same trigger whether the
        export wrote `{"tag": "vip"}` or `{"field": "tag", "value": "vip"}`.
        """
        out = set()
        for f in self.filters():
            if isinstance(f, dict):
                pairs = []
                for k, v in sorted(f.items()):
                    nk = _norm_key(k)
                    if nk in ("field", "key", "property", "name", "attribute"):
                        nk = "field"
                    elif nk in ("value", "values", "val", "operatorvalue"):
                        nk = "value"
                    elif nk in ("tag", "tags"):
                        pairs.append(("field", "tag"))
                        nk = "value"
                    if isinstance(v, (list, tuple, set)):
                        v = "|".join(sorted(str(x).strip().lower() for x in v))
                    pairs.append((nk, str(v).strip().lower()))
                out.add(tuple(sorted(set(pairs))))
            else:
                out.add((("value", str(f).strip().lower()),))
        return tuple(sorted(out))

    def signature(self) -> tuple:
        return (self.canonical_type(), self.canonical_filters())


@dataclass
class Workflow:
    id: str
    name: str
    status: str = "unknown"
    steps: list[Step] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    settings: dict = field(default_factory=dict)

    # -- convenience the rules lean on ----------------------------------
    @property
    def published(self) -> bool:
        return str(self.status).lower() in ("published", "active", "live")

    def steps_of(self, *types: str) -> list[Step]:
        want = set(types)
        return [s for s in self.steps if s.type in want]

    @property
    def outbound(self) -> list[Step]:
        return [s for s in self.steps if s.is_outbound]

    def has_reply_release(self) -> bool:
        """Does anything in this workflow notice that the contact answered?

        Three shapes count, because GHL offers three. The third one — a wait
        followed by Replied / No reply branches — is how the builder actually
        does it in the UI, and missing it means flagging every correctly built
        workflow in the account.
        """
        if _first(self.settings, "stopOnResponse", "stop_on_response") is True:
            return True
        for i, step in enumerate(self.steps):
            if not step.is_wait:
                continue
            if step.wait_releases_on_reply():
                return True
            for nxt in self.steps[i + 1:i + 3]:
                if nxt.type in ("transition", "branch", "if_else", "condition") \
                        and REPLY_BRANCH.search(nxt.name):
                    return True
        return False

    def tags_added(self) -> set[str]:
        out: set[str] = set()
        for s in self.steps:
            out |= s.tags_added()
        return out

    def trigger_tags(self) -> set[str]:
        out: set[str] = set()
        for t in self.triggers:
            # "stage" contains "tag" (s-TAG-e), so a bare substring test read
            # every pipeline-stage trigger as a tag trigger and its stage ids
            # as tags — phantom dead-weight and loop findings.
            nk = t.type.lower()
            if "tag" in nk and "stage" not in nk:
                out |= t.tag_values()
        return out

    def send_window(self) -> dict | None:
        w = _first(self.settings, "sendingWindow", "sending_window", "window",
                   "quietHours", "quiet_hours")
        return w if isinstance(w, dict) and w else None

    def text(self) -> str:
        return "\n".join(s.text() for s in self.steps)

    def bodies(self) -> str:
        """Only what a contact could actually read."""
        return "\n".join(s.bodies() for s in self.steps)

    def custom_values_used(self) -> set[str]:
        return set(CUSTOM_VALUE.findall(self.text()))

    # -- shape ----------------------------------------------------------
    @property
    def sms_steps(self) -> list[Step]:
        return [s for s in self.steps if s.is_sms]

    @property
    def email_steps(self) -> list[Step]:
        return [s for s in self.steps if s.is_email]

    def trigger_signatures(self) -> list:
        return [t.signature() for t in self.triggers]

    def sends_across_a_wait(self) -> bool:
        """Is there a send, then a pause, then another send?

        The window a reply can arrive in. Two messages fired back to back — an
        SMS and the same content by email, say — are one touch delivered twice,
        and no reply can land between them, so a rule about ignoring replies
        has nothing to bite on. A real account had a reply-triggered workflow
        whose only two sends went out together flagged for "nothing listening
        for a reply"; the symptom described a day-2 follow-up the workflow did
        not have.
        """
        seen_send = False
        waited = False
        for step in self.steps:
            if step.is_outbound:
                if seen_send and waited:
                    return True
                seen_send = True
            elif step.is_wait and seen_send:
                waited = True
        return False

    def outbound_after(self, index: int) -> list[Step]:
        """Sends that sit below a given step — the size of the leak below it."""
        return [s for s in self.steps[index + 1:] if s.is_outbound]

    def step_ids(self) -> set:
        return {s.step_id for s in self.steps if s.step_id}

    @property
    def has_wiring(self) -> bool:
        """Does this export carry node ids and links at all?

        Flat exports list steps in order and carry no ids. There is nothing
        wrong with them — but a wiring check has nothing to read, and reporting
        'no broken links' on a file that contains no links would be a lie.
        """
        if not self.step_ids():
            return False
        return any(s.next_ids() or s.parent_key for s in self.steps)

    def shape(self) -> tuple:
        """A structural fingerprint: what this workflow does, ignoring names.

        Structure ALONE does not identify a duplicate. Reusing a skeleton is
        good practice — a referral ask and a review ask are the same twenty
        steps in the same order and are not the same workflow — so callers that
        care about duplication must compare `copy_fingerprint()` too. See
        GHL015, which used to escalate on this alone and told the owner of a
        working account to unpublish a live campaign.
        """
        return (tuple(sorted(self.trigger_signatures())),
                tuple(_norm_key(s.type) for s in self.steps))

    def copy_fingerprint(self) -> tuple:
        """What this workflow actually SAYS, normalised.

        Only the message content of the outbound steps: subject line and body,
        stripped of markup, case and whitespace. Deliberately not `Step.text()`
        — that flattens every string in the export including step ids, which
        differ between two copies of the same workflow and would make a real
        duplicate look unique. This has to be the other way round: identical
        for a snapshot re-push, different the moment a human rewrote the copy.
        """
        return tuple(_message_copy(s) for s in self.outbound)

    def exits(self) -> list[Step]:
        """Steps that pull the contact out of this workflow before the end."""
        return [s for s in self.steps
                if "removefromworkflow" in _norm_key(s.type)
                or "removeworkflow" in _norm_key(s.type)
                or "goalevent" in _norm_key(s.type)]


def parse_step(raw: Any) -> Step:
    if not isinstance(raw, dict):
        return Step(type=str(raw))
    return Step(
        type=str(_first(raw, "type", "actionType", "action", "kind", default="unknown")),
        name=str(_first(raw, "name", "label", "title", default="")),
        raw=raw,
    )


def parse_trigger(raw: Any) -> Trigger:
    if not isinstance(raw, dict):
        return Trigger(type=str(raw))
    return Trigger(
        type=str(_first(raw, "type", "eventType", "triggerType", "event", default="unknown")),
        name=str(_first(raw, "name", "label", default="")),
        raw=raw,
    )


def parse_workflow(raw: dict) -> Workflow:
    steps = [parse_step(s) for s in _as_list(_first(
        raw, "steps", "templates", "actions", "nodes", default=[]))]
    triggers = [parse_trigger(t) for t in _as_list(_first(
        raw, "triggers", "trigger", "events", default=[]))]
    settings = _first(raw, "settings", "config", "options", default={}) or {}
    if not isinstance(settings, dict):
        settings = {}
    return Workflow(
        id=str(_first(raw, "_id", "id", "workflowId", default="")),
        name=str(_first(raw, "name", "title", default="(unnamed)")),
        status=str(_first(raw, "status", "state", default="unknown")),
        steps=steps,
        triggers=triggers,
        settings=settings,
    )


def _id_map(raw, name_keys=("name", "title", "label")) -> dict:
    """{id: name} from either a list of objects or an {id: name} dict."""
    out: dict = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = str(v) if not isinstance(v, dict) else \
                str(_first(v, *name_keys, default=k))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                ident = _first(item, "id", "_id", "key", default=None)
                if ident is not None:
                    out[str(ident)] = str(_first(item, *name_keys, default=""))
            elif isinstance(item, str):
                out[item] = item
    return out


@dataclass
class Inventory:
    """What else exists in the location, if the caller could tell us.

    None of this can be inferred from a workflow export — a deleted calendar and
    a calendar that was never in this file look identical. So each bucket
    records whether it was actually SUPPLIED, and a rule that needs a bucket it
    did not get reports itself as skipped rather than passing quietly.
    """

    calendars: dict = field(default_factory=dict)      # id -> name
    # id -> the calendar object as exported. `calendars` keeps only the name,
    # which is all the reference checks need; the settings checks (does the
    # slot auto-confirm, what does the booking screen say) need the record.
    calendar_records: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)          # id -> {"name", "active"}
    pipelines: dict = field(default_factory=dict)
    stages: dict = field(default_factory=dict)         # id -> {"name", "pipeline"}
    forms: dict = field(default_factory=dict)
    surveys: dict = field(default_factory=dict)
    templates: dict = field(default_factory=dict)
    custom_fields: dict = field(default_factory=dict)  # slug -> display name
    tags: set = field(default_factory=set)
    phone_numbers: list = field(default_factory=list)  # [{"number", "sms"}]
    email_domains: list = field(default_factory=list)  # [{"domain", "verified"}]
    email_settings: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)          # workflow key -> enrollments
    provided: set = field(default_factory=set)

    def has(self, *buckets: str) -> bool:
        return all(b in self.provided for b in buckets)

    def missing(self, *buckets: str) -> list:
        return [b for b in buckets if b not in self.provided]

    def known(self, kind: str) -> dict:
        return {"calendar": self.calendars, "user": self.users,
                "pipeline": self.pipelines, "stage": self.stages,
                "form": self.forms, "survey": self.surveys,
                "template": self.templates}.get(kind, {})

    def enrollments(self, workflow: "Workflow"):
        """Enrollment count for a workflow, keyed by id or by name."""
        for key in (workflow.id, workflow.name):
            if not key:
                continue
            if key in self.stats:
                return self.stats[key]
            for k, v in self.stats.items():
                if str(k).strip().lower() == str(key).strip().lower():
                    return v
        return None

    @property
    def sms_capable_numbers(self) -> list:
        return [n for n in self.phone_numbers if n.get("sms")]

    @property
    def verified_email_domains(self) -> list:
        return [d for d in self.email_domains if d.get("verified")]

    @classmethod
    def load(cls, data) -> "Inventory":
        inv = cls()
        if not isinstance(data, dict):
            return inv

        def pick(*keys):
            """The first of these keys carrying a parseable collection.

            A bucket counts as SUPPLIED only when its value is a list or a dict
            — the shapes this can actually read. `"users": "dana"` is a typo,
            and treating it as an empty-but-supplied user list would make every
            userId in the account look like a reference to a deleted user. An
            explicitly empty list is different: that is the account genuinely
            telling us it has none, and it is allowed to mean that.
            """
            for k in keys:
                if k in data and isinstance(data[k], (list, dict)):
                    return k, data[k]
            return None, None

        key, raw = pick("calendars", "calendar")
        if key:
            inv.calendars = _id_map(raw)
            inv.provided.add("calendars")
            items = raw.values() if isinstance(raw, dict) else raw
            for item in items:
                if not isinstance(item, dict):
                    continue
                ident = _first(item, "id", "_id", "key", default=None)
                if ident is not None:
                    inv.calendar_records[str(ident)] = item

        key, raw = pick("users", "staff", "teamMembers")
        if key:
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        ident = _first(item, "id", "_id", "userId", default=None)
                        if ident is None:
                            continue
                        active = item.get("active", item.get("isActive", True))
                        inv.users[str(ident)] = {
                            "name": str(_first(item, "name", "firstName", "email",
                                               default="")),
                            "active": bool(active)}
                    elif isinstance(item, str):
                        inv.users[item] = {"name": item, "active": True}
            elif isinstance(raw, dict):
                for k, v in raw.items():
                    inv.users[str(k)] = {"name": str(v), "active": True} \
                        if not isinstance(v, dict) else {
                            "name": str(_first(v, "name", default=k)),
                            "active": bool(v.get("active", True))}
            inv.provided.add("users")

        key, raw = pick("pipelines", "pipeline")
        if key:
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    pid = str(_first(item, "id", "_id", default=""))
                    if not pid:
                        continue
                    inv.pipelines[pid] = str(_first(item, "name", default=""))
                    for st in _as_list(item.get("stages")):
                        if isinstance(st, dict):
                            sid = str(_first(st, "id", "_id", default=""))
                            if sid:
                                inv.stages[sid] = {
                                    "name": str(_first(st, "name", default="")),
                                    "pipeline": pid}
            else:
                inv.pipelines = _id_map(raw)
            inv.provided.add("pipelines")
            inv.provided.add("stages")

        for bucket, keys in (("forms", ("forms", "form")),
                             ("surveys", ("surveys", "survey"))):
            key, raw = pick(*keys)
            if key:
                setattr(inv, bucket, _id_map(raw))
                inv.provided.add(bucket)

        templates: dict = {}
        got_templates = False
        for keys in (("emailTemplates", "email_templates"),
                     ("smsTemplates", "sms_templates"),
                     ("templates",)):
            key, raw = pick(*keys)
            if key:
                templates.update(_id_map(raw))
                got_templates = True
        if got_templates:
            inv.templates = templates
            inv.provided.add("templates")

        key, raw = pick("customFields", "custom_fields")
        if key:
            entries = raw if isinstance(raw, list) else \
                [{"key": k, "name": v} for k, v in (raw or {}).items()]
            for item in entries:
                if isinstance(item, dict):
                    ident = _first(item, "fieldKey", "key", "name", "id", default="")
                    display = str(_first(item, "name", "fieldKey", "key", default=""))
                    for form in (str(ident), str(ident).split(".")[-1]):
                        if form:
                            inv.custom_fields[slug(form)] = display
                elif isinstance(item, str):
                    inv.custom_fields[slug(item)] = item
                    inv.custom_fields[slug(item.split(".")[-1])] = item
            inv.provided.add("custom_fields")

        key, raw = pick("tags", "tagList")
        if key:
            inv.tags = {str(t).strip().lower() for t in _str_values(raw)}
            inv.provided.add("tags")

        key, raw = pick("phoneNumbers", "phone_numbers", "numbers",
                        "activeNumbers")
        if key:
            for item in _as_list(raw):
                if isinstance(item, dict):
                    caps = item.get("capabilities") or {}
                    sms = caps.get("sms") if isinstance(caps, dict) else None
                    if sms is None:
                        sms = item.get("sms", item.get("smsCapable", True))
                    inv.phone_numbers.append({
                        "number": str(_first(item, "phoneNumber", "number",
                                             default="")),
                        "sms": bool(sms)})
                elif isinstance(item, str):
                    inv.phone_numbers.append({"number": item, "sms": True})
            inv.provided.add("phone_numbers")

        key, raw = pick("emailDomains", "email_domains", "sendingDomains")
        if key:
            for item in _as_list(raw):
                if isinstance(item, dict):
                    verified = item.get("verified", item.get("isVerified",
                                                             item.get("valid")))
                    inv.email_domains.append({
                        "domain": str(_first(item, "domain", "name",
                                             default="")).lower(),
                        "verified": bool(verified)})
                elif isinstance(item, str):
                    inv.email_domains.append({"domain": item.lower(),
                                              "verified": True})
            inv.provided.add("email_domains")

        key, raw = pick("emailSettings", "email_settings")
        if key and isinstance(raw, dict):
            inv.email_settings = raw
            inv.provided.add("email_settings")

        key, raw = pick("stats", "workflowStats", "workflow_stats")
        if key and isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    count = _first(v, "enrollments", "enrolled", "contacts",
                                   "entries", default=None)
                else:
                    count = v
                try:
                    inv.stats[str(k)] = int(count)
                except (TypeError, ValueError):
                    continue
            inv.provided.add("stats")

        return inv


def _str_values(raw) -> list:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(str(_first(item, "name", "tag", "label", default="")))
        return [o for o in out if o]
    if isinstance(raw, dict):
        return [str(v) for v in raw.values()]
    return []


@dataclass
class Account:
    """One GoHighLevel location's workflows, plus whatever context we were given."""

    workflows: list[Workflow] = field(default_factory=list)
    custom_values: dict[str, str] = field(default_factory=dict)
    inventory: Inventory = field(default_factory=Inventory)
    config: AuditConfig = field(default_factory=AuditConfig)

    @classmethod
    def load(cls, data: Any, config: AuditConfig = None) -> "Account":
        """Accept a list of workflows, a single workflow, or a bundle dict."""
        custom_values: dict[str, str] = {}
        inventory = Inventory()
        if isinstance(data, dict):
            raw_cvs = _first(data, "customValues", "custom_values", default={}) or {}
            if isinstance(raw_cvs, list):
                for cv in raw_cvs:
                    if isinstance(cv, dict):
                        key = _first(cv, "name", "key", "fieldKey", default="")
                        custom_values[str(key)] = str(_first(cv, "value", default=""))
            elif isinstance(raw_cvs, dict):
                custom_values = {str(k): str(v) for k, v in raw_cvs.items()}
            inventory = Inventory.load(data)
            # A bundle may carry its own config block, so one file can hold
            # everything about one account. An explicit --config still wins:
            # policy the auditor decided beats policy the export shipped with.
            if config is None and isinstance(data.get("config"), dict):
                config = AuditConfig.from_dict(data["config"])
            workflows = _as_list(_first(data, "workflows", "steps_by_workflow",
                                        default=None) or data)
        else:
            workflows = _as_list(data)

        parsed = [parse_workflow(w) for w in workflows if isinstance(w, dict)]
        return cls(workflows=parsed, custom_values=custom_values,
                   inventory=inventory, config=config or AuditConfig())

    @classmethod
    def from_file(cls, path: str, config: AuditConfig = None) -> "Account":
        with open(path) as fh:
            return cls.load(json.load(fh), config=config)

    def custom_value_slugs(self) -> dict:
        """{slug: (display name, value)} — the form merge fields actually use."""
        return {slug(k): (k, v) for k, v in self.custom_values.items()}

    def published(self) -> Iterable[Workflow]:
        return (w for w in self.workflows if w.published)

    def reply_handler(self) -> Workflow | None:
        """The workflow, if any, that listens for replies on behalf of the others.

        The mature pattern is one listener that pulls a contact out of every running
        sequence the moment they answer, instead of bolting reply detection onto each
        sequence separately. A per-workflow check cannot see it, so the whole account
        gets read once and the result is handed to the rules.
        """
        for wf in self.published():
            listens = any(
                any(k in t.type.lower() for k in
                    ("inbound", "reply", "message_received", "customer_replied"))
                for t in wf.triggers)
            removes = bool(wf.steps_of("remove_from_workflow", "remove_workflow"))
            if listens and removes:
                return wf
        return None
