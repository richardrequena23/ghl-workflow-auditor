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

MERGE_FIELD = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
CUSTOM_VALUE = re.compile(r"\{\{\s*custom_values\.([a-zA-Z0-9_]+)\s*\}\}")
PLACEHOLDER = re.compile(r"REPLACE[-_ ]?WITH|YOUR[-_ ]?[A-Z]+[-_ ]?HERE|TODO|XXXX", re.I)
# "Replied" / "No reply" branch labels off a wait step — the UI's way of listening.
REPLY_BRANCH = re.compile(r"^\s*(replied|reply|responded|answered)\b", re.I)


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
        for key in ("meta", "data", "config", "settings", "params", "extra"):
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
            if "tag" in t.type.lower():
                out |= t.tag_values()
        return out

    def send_window(self) -> dict | None:
        w = _first(self.settings, "sendingWindow", "sending_window", "window",
                   "quietHours", "quiet_hours")
        return w if isinstance(w, dict) and w else None

    def text(self) -> str:
        return "\n".join(s.text() for s in self.steps)

    def custom_values_used(self) -> set[str]:
        return set(CUSTOM_VALUE.findall(self.text()))


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


@dataclass
class Account:
    """One GoHighLevel location's workflows, plus whatever context we were given."""

    workflows: list[Workflow] = field(default_factory=list)
    custom_values: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, data: Any) -> "Account":
        """Accept a list of workflows, a single workflow, or a bundle dict."""
        custom_values: dict[str, str] = {}
        if isinstance(data, dict):
            raw_cvs = _first(data, "customValues", "custom_values", default={}) or {}
            if isinstance(raw_cvs, list):
                for cv in raw_cvs:
                    if isinstance(cv, dict):
                        key = _first(cv, "name", "key", "fieldKey", default="")
                        custom_values[str(key)] = str(_first(cv, "value", default=""))
            elif isinstance(raw_cvs, dict):
                custom_values = {str(k): str(v) for k, v in raw_cvs.items()}
            workflows = _as_list(_first(data, "workflows", "steps_by_workflow",
                                        default=None) or data)
        else:
            workflows = _as_list(data)

        parsed = [parse_workflow(w) for w in workflows if isinstance(w, dict)]
        return cls(workflows=parsed, custom_values=custom_values)

    @classmethod
    def from_file(cls, path: str) -> "Account":
        with open(path) as fh:
            return cls.load(json.load(fh))

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
