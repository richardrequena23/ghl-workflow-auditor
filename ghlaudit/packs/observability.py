"""Observability and operational safety — can anyone tell when this broke?

Every check here asks one question about a different part of the build: when
this goes wrong at 2am on a Sunday, does anybody find out, and can they
reconstruct what happened afterwards? GHL041-GHL048 cover whether a failure is
DETECTED at all. These six cover what sits either side of that — the credential
that should never have been in the export in the first place, the call that can
hang or run twice, the destructive step that leaves no trace of what it
touched, and the alert nobody is going to read. Each one is a defect that can
be pointed at in an export, not a preference: a key in a query string, a
missing timeout field, a POST inside a retry ladder with no idempotency key, a
DELETE with no log step anywhere near it, three workflows whose alerts all name
the same person, and an error branch whose only act is to tag the contact.

All six fire on an ABSENCE, which is the easiest kind of check to write badly:
"no idempotency key" and "no idempotency key in the shape I happened to look
at" produce the same finding and only one of them is true. So the helpers below
are deliberately generous about what counts as the thing being present — a
credential REFERENCE is not a credential, a Telegram node is an alert, a Google
Sheets append is an audit trail — and the rules fire only on what is left.
"""

from __future__ import annotations

import json
import re

from ..model import PLACEHOLDER, URL, Account, Step, Workflow
from ..rules import Finding, _finding, rule


def _norm(key) -> str:
    """apiKey, api_key, API-KEY and x-api-key are one key written four ways."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _scalars(node, key=""):
    """Every (key, scalar) pair in a structure, however deeply nested.

    Two shapes have to come out of here the same way. n8n writes a header as
    {"name": "Authorization", "value": "Bearer ..."} and GoHighLevel writes it
    as {"Authorization": "Bearer ..."}, so a dict that carries a name/value
    pair yields its value under the NAME. And the walk has to be recursive:
    n8n keeps the timeout at `parameters.options.timeout`, one level below
    where a flat `.items()` scan looks.
    """
    if isinstance(node, dict):
        label = ""
        for cand in ("name", "key", "header", "parameter", "field"):
            if isinstance(node.get(cand), str) and node[cand].strip():
                label = node[cand]
                break
        for k, v in node.items():
            yield from _scalars(
                v, label if (label and _norm(k) in ("value", "val")) else k)
    elif isinstance(node, list):
        for item in node:
            yield from _scalars(item, key)
    elif isinstance(node, (str, int, float, bool)):
        yield key, node


def _live(acct: Account):
    """The workflows this pack applies to.

    Published, plus anything from an export that carries no status at all: an
    n8n bundle has no publish state, and reading an absent status as "not
    live" would quietly exempt every n8n workflow from the whole pack. An
    explicit draft is left alone — it is not running.
    """
    for wf in acct.workflows:
        if wf.published or _norm(wf.status) in ("unknown", ""):
            yield wf


def _step_keys(step: Step) -> set:
    return {_norm(k) for k in step.raw} if isinstance(step.raw, dict) else set()


def _is_n8n_node(step: Step) -> bool:
    """Does this step carry n8n's node shape rather than GoHighLevel's?

    Same test `rules.py` uses for GHL043: n8n node types are namespaced and
    every node carries a typeVersion. A GoHighLevel action has neither, and
    the two platforms expose different settings — holding one to the other's
    fields produces advice nobody can act on.
    """
    return "n8nnodes" in _norm(step.type) or "typeversion" in _step_keys(step)


URL_KEYS = ("url", "uri", "endpoint", "webhookurl", "requesturl", "targeturl",
            "hookurl")


def _call_url(step: Step) -> str:
    """The http(s) destination this step calls, or ''.

    Doubles as the test for "is this an OUTBOUND call". n8n's inbound Webhook
    node is a TRIGGER that declares an HTTP method and a path, and without a
    destination URL to separate them, it reads as a POST to somewhere.
    """
    for key, value in _scalars(step.raw):
        if _norm(key) in URL_KEYS and isinstance(value, str):
            match = URL.search(value)
            if match:
                return match.group(0)
    return ""


CALL_TYPE = re.compile(r"webhook|http|api|request", re.I)


def _is_outbound_call(step: Step) -> bool:
    return bool(CALL_TYPE.search(step.type)) and bool(_call_url(step))


def _method(step: Step) -> str:
    for key, value in _scalars(step.raw):
        if _norm(key) in ("method", "httpmethod", "requestmethod", "verb") \
                and isinstance(value, str):
            return value.strip().lower()
    return ""


NOTIFY_TYPE = re.compile(
    r"notif|alert|slack|discord|pager|opsgenie|"
    r"internal[-_ ]?(sms|email|call|message)", re.I)

# Node types whose only job in a workflow is to reach the team. An n8n build
# alerts through the Telegram or Slack node, not through an "internal
# notification" action that only GoHighLevel has — and a rule that cannot see
# those channels reports every n8n alerting build as having no alerting.
OPS_CHANNEL_TYPE = re.compile(
    r"slack|discord|telegram|mattermost|msteams|microsoftteams|googlechat|"
    r"pagerduty|opsgenie|pushover|pushbullet|ntfy|gotify|signl4|webex|"
    r"rocketchat|twilio", re.I)

ALERT_SINK = re.compile(
    r"slack|discord|teams|pager|opsgenie|sentry|datadog|healthcheck|"
    r"betteruptime|betterstack|statuspage|cronitor|uptimerobot|telegram|"
    r"pushover|ntfy|gotify|signl4|webex|rocket|chat\.googleapis|mattermost|"
    r"notify|alert|monitor", re.I)


def _is_alert_sink(step: Step) -> bool:
    """An outbound call that is itself the alarm — Slack, PagerDuty, a monitor."""
    return _is_outbound_call(step) and bool(
        ALERT_SINK.search(step.name + " " + _call_url(step)))


def _is_team_alert(step: Step) -> bool:
    """A step whose whole job is to put a message in front of the TEAM."""
    return (bool(NOTIFY_TYPE.search(step.type))
            or bool(OPS_CHANNEL_TYPE.search(step.type))
            or _is_alert_sink(step))


# --------------------------------------------------------------------------
# GHL083 — a credential in the export
# --------------------------------------------------------------------------

# Substrings, matched against the NORMALISED key, so "X-Api-Key", "apiKey"
# and "api_key" are one entry. Kept short on purpose: "secret" also catches
# clientSecret and signingSecret, "token" catches accessToken and authToken.
SECRET_KEY_HINTS = ("apikey", "accesskey", "privatekey", "secret", "token",
                    "password", "passwd", "passphrase", "credential",
                    "authorization", "hmac", "bearer")

# A key that NAMES or POINTS AT a credential is not the credential. Both n8n
# and GoHighLevel keep the secret server-side and write only a pointer into
# the export — {"credentialId": "cred_9f2a1b8c7d6e"}, {"secretName":
# "warehouse_api_key_v2"}. Every one of those is opaque, sits under a
# secret-shaped key, and is not a leak.
REFERENCE_SUFFIXES = ("id", "ids", "name", "names", "type", "types", "ref",
                      "reference", "path", "location", "source", "field")
REFERENCE_KEYS = ("credential", "credentials", "authentication")

# A credential is one unbroken run of key characters. A label, a merge field
# and a sentence all fail this before the entropy test is even reached.
OPAQUE = re.compile(r"^[A-Za-z0-9_\-.=+/]{16,}$")

# The query parameters that mark a pre-signed download link. S3, GCS and
# Azure all sign a URL by putting a credential scope and a signature in the
# query string next to an expiry.
PRESIGNED_PARAM = re.compile(r"^x[-_]?(amz|goog|ms)|^(se|sig|st|sp|sv)$|expir",
                             re.I)


def _is_credential_reference(key) -> bool:
    """True when the key points at a stored credential rather than holding one.

    Telling a client their key is sitting in the file when the file contains
    a pointer to a key is the one mistake this rule cannot make — it is
    unfalsifiable from their side, it costs them a rotation they did not
    need, and it is the finding that makes them doubt the other 99 checks.
    """
    nk = _norm(key)
    return nk in REFERENCE_KEYS or nk.endswith(REFERENCE_SUFFIXES)


def _looks_like_a_credential(value: str) -> bool:
    """An opaque literal, not a label and not a merge field.

    The bar is deliberately high — 16+ characters, no spaces, and letters AND
    digits — because the expensive mistake here is telling a client their key
    leaked when the value was "contact-owner". 'true', 'bearer', 'email' and
    '{{ custom_values.api_key }}' all fail it; anything random enough that
    nobody should be reading it out of an export passes.
    """
    v = str(value).strip()
    for scheme in ("bearer ", "basic ", "token "):
        if v.lower().startswith(scheme):
            v = v[len(scheme):].strip()
            break
    if "{{" in v or not OPAQUE.match(v):
        return False
    if PLACEHOLDER.search(v):  # GHL008 owns "REPLACE_WITH_KEY" — not a leak
        return False
    return any(c.isdigit() for c in v) and any(c.isalpha() for c in v)


def _query_credentials(text: str):
    """(parameter, value) for each credential-shaped query parameter in a URL.

    Pre-signed links are excluded. An S3 or GCS download URL carries a
    credential scope and a signature by design, and both die at the expiry
    sitting beside them — the signature authorises exactly the one request it
    was minted for, so it is neither reusable nor rotatable, and reporting it
    as a leaked key is wrong twice over.
    """
    for match in URL.finditer(text):
        _, _, query = match.group(0).partition("?")
        params = []
        for part in re.split(r"[&;]", query):
            name, sep, value = part.partition("=")
            if sep:
                params.append((name.strip(), value))
        if any(PRESIGNED_PARAM.search(name) for name, _ in params):
            continue
        for name, value in params:
            if any(h in _norm(name) for h in SECRET_KEY_HINTS) \
                    and not _is_credential_reference(name) \
                    and _looks_like_a_credential(value):
                yield name, value


def _redact(value: str) -> str:
    """Never print the key. The audit report is one more place it would live."""
    v = str(value).strip()
    return f"{v[:4]}... ({len(v)} chars)"


@rule("GHL083", "Credential sitting in plain text in a workflow step",
      "critical", "hygiene", "observability", "secrets")
def plaintext_credential(acct: Account):
    """An API key, token or password written into a step instead of a custom value.

    The correct home for a credential is Settings -> Custom Values, referenced
    as {{ custom_values.<name> }}: one place to rotate it, and it is not
    sitting in the builder for every contractor with sub-account access to
    read. A key typed straight into a step is in the workflow, in every
    snapshot taken from it, and in every export mailed to an agency — the
    credential is already outside the account and nobody can say who has it.
    Placement decides severity. In a query string it is worse than in a
    header: URLs are logged by every proxy, execution log and error report on
    the path, so the key is written to disk in systems nobody in this account
    controls. Draft workflows are checked too — an unpublished workflow leaks
    a key exactly as well as a published one.
    """
    for wf in acct.workflows:
        for step in wf.steps:
            in_url, in_config = [], []
            for key, value in _scalars(step.raw):
                if not isinstance(value, str):
                    continue
                in_url.extend(_query_credentials(value))
                if any(h in _norm(key) for h in SECRET_KEY_HINTS) \
                        and not _is_credential_reference(key) \
                        and _looks_like_a_credential(value):
                    in_config.append((str(key), value))
            if not in_url and not in_config:
                continue
            # The URL is the worse placement, so it leads — but the two are
            # named separately. A title that says "in the request URL
            # (Authorization, api_key)" is pointing at two places at once and
            # the reader fixes whichever one they find first.
            primary = in_url or in_config
            named = ", ".join(sorted({k for k, _ in primary}))
            also = sorted({k for k, _ in in_config}) if in_url else []
            sample = _redact(primary[0][1])
            where = "the request URL" if in_url else "the step's settings"
            yield _finding(
                "GHL083", "critical" if in_url else "high", wf,
                f"A credential is sitting in {where}, in plain text ({named})",
                f"This step carries what looks like a real key or token in "
                f"plain text — {named}, value {sample}. Anyone who can open "
                "this workflow can read it and use it as you: a contractor, a "
                "VA, an agency you have since stopped working with, anyone "
                "you ever sent a snapshot or an export to."
                + (" It is in the request URL, which is the worst place for "
                   "it — URLs get written to logs by every proxy, execution "
                   "history and error report the request passes through, so "
                   "copies of this key now exist in systems you do not "
                   "control." if in_url else
                   " There is no way to tell who has already copied it, and "
                   "no way to rotate it without hunting through every "
                   "workflow that hardcoded it.")
                + (f" The same step carries one in its settings as well "
                   f"({', '.join(also)}) — both have to be dealt with."
                   if also else ""),
                "Treat the key as compromised and rotate it at the provider "
                "first — that is the only step that actually undoes this. "
                "Then put the new value in Settings -> Custom Values and "
                "reference it from the step as {{ custom_values.<name> }}, so "
                "the next rotation is one edit in one place."
                + (" Move it out of the URL and into an Authorization header "
                   "while you are there, if the provider accepts one — a "
                   "header is not logged the way a URL is." if in_url else ""),
                step=step.name or step.type,
                cost="Somebody else can send, charge or delete on your "
                     "account and it will look exactly like you did it. "
                     "Rotation is cheap today; a used key is an incident.")


# --------------------------------------------------------------------------
# GHL084 — a call that can hang
# --------------------------------------------------------------------------

def _is_http_node(step: Step) -> bool:
    """An HTTP Request node — a step where a timeout is a real, settable field.

    Scoped on purpose, and scoped twice. GoHighLevel's own webhook action
    exposes no timeout control in the UI, so flagging one would be advice
    nobody can act on — and GHL's action is typed `http_request` in some
    exports, which normalises to the same string as n8n's node. The n8n shape
    (a namespaced type or a typeVersion) is what separates them.
    """
    return "http" in _norm(step.type) and _is_n8n_node(step)


def _declares_timeout(step: Step) -> bool:
    """Is there anything bounding how long this request may hang?

    Zero is not a timeout. Every HTTP client in this family reads 0 as "wait
    forever", which is the exact state this rule exists to find.
    """
    for key, value in _scalars(step.raw):
        if "timeout" not in _norm(key):
            continue
        if value in (None, "", 0, "0", False, "none", "never"):
            continue
        return True
    return False


def _execution_is_bounded(wf: Workflow) -> bool:
    """Does the workflow itself cap how long a run may take?

    n8n's Settings -> Timeout Workflow After is the instance-wide safety net a
    careful builder sets once per workflow. It is a blunter instrument than a
    per-request timeout, but it does bound the hang — and this rule's whole
    claim is that nothing bounds it.
    """
    settings = wf.settings if isinstance(wf.settings, dict) else {}
    for key, value in settings.items():
        if "timeout" in _norm(key) and value not in (
                None, "", 0, "0", False, "none", "never"):
            return True
    return False


@rule("GHL084", "HTTP call with no timeout", "medium", "routing",
      "observability", "reliability")
def http_call_without_a_timeout(acct: Account):
    """An outbound call with nothing bounding how long it may wait.

    A slow endpoint is a different failure from a broken one, and it is the
    one that does not show up in error monitoring: nothing has failed yet, so
    nothing is reported. The request holds its execution slot until something
    else gives up, and on an instance with a concurrency limit a handful of
    hung calls is the whole queue. The number matters less than having one —
    a call that normally answers in 300ms has no business waiting minutes.
    """
    for wf in _live(acct):
        if _execution_is_bounded(wf):
            continue
        for step in wf.steps:
            if not _is_http_node(step) or _declares_timeout(step):
                continue
            yield _finding(
                "GHL084", "medium", wf,
                "This call can hang indefinitely — no timeout is set",
                "Nothing here limits how long this request may wait for the "
                "other end to answer. A dead endpoint fails fast and gets "
                "noticed; a SLOW one just holds on, and while it does, this "
                "run is stuck and the execution slot it is using is not "
                "available to anything else. Nothing is reported as failed, "
                "because nothing has failed yet — which is why a hung "
                "integration is usually found by a client asking why "
                "everything went quiet.",
                "Set Options -> Timeout on the node to a real number — a few "
                "seconds for an API that normally answers instantly, longer "
                "only where the endpoint is genuinely slow — and make sure "
                "the timeout path is handled, not just declared. A timeout "
                "that routes nowhere is the same outage with a shorter wait.",
                step=step.name or step.type,
                cost="One unresponsive endpoint can hold up every run behind "
                     "it, with nothing marked as failed and nothing to alert "
                     "on. The backlog is found by hand, hours later.")


# --------------------------------------------------------------------------
# GHL085 — a POST that can run twice
# --------------------------------------------------------------------------

# Idempotency arrives under a lot of names, and in headers written either as
# a key or as a {"name": ..., "value": ...} pair, so this is matched against
# the step's whole JSON — including its keys, because "Idempotency-Key" is
# usually the key and not the value.
IDEMPOTENT = re.compile(
    r"idempoten|request[-_ ]?id|dedup|deduplicat|client[-_ ]?token|"
    r"transaction[-_ ]?id|correlation[-_ ]?id", re.I)

# An endpoint that says it upserts is telling you it dedupes: the second call
# matches the same key and updates the record the first one created. That is
# idempotency implemented on the receiving side, which is the other correct
# answer to this problem.
UPSERT = re.compile(r"upsert", re.I)

RETRY_FLAG_KEYS = ("retryonfail", "retryenabled", "retriesenabled")
RETRY_COUNT_KEYS = ("maxtries", "maxretries", "retrycount", "retries",
                    "retryattempts")

# A goto/jump construct IS the whole step type — anything after the "to" is a
# structural suffix, never a product name. Matching "goto" as a substring
# reads n8n's goToWebinar node as a loop.
GOTO_LEAF = re.compile(
    r"^go[-_ ]?to(?:[-_ ]?(?:step|action|node|event|workflow))?$", re.I)

# Keys on a Go-To that describe where it came FROM, not where it goes. A
# branch child carries its parent's id, and the parent is by definition the
# step just above — read as a jump target it turns every Go-To into a loop.
WIRING_KEYS = ("id", "parentkey", "parentid", "parent", "previous",
               "previousstepid", "fromstepid", "source", "sourceid")

MONEY = re.compile(
    r"charge|payment|invoice|order|subscription|refund|checkout|"
    r"transaction|billing|purchase|/pay", re.I)


def _step_retries(step: Step) -> bool:
    """Will this step re-send the same request on a failure?

    The explicit flag wins over the count. n8n leaves `maxTries` in the export
    after Retry On Fail is switched back off, so a count on its own proves
    nothing — and "retries are enabled on this step" is a sentence that has to
    be true, because it is the whole reason the finding fires.
    """
    flag = None
    counted = False
    for key, value in _scalars(step.raw):
        nk = _norm(key)
        if nk in RETRY_FLAG_KEYS:
            flag = value not in (None, "", 0, "0", False, "false", "no")
        elif nk in RETRY_COUNT_KEYS and value not in (
                None, "", 0, "0", False, "false"):
            counted = True
    return counted if flag is None else flag


def _loops_back_to(wf: Workflow, index: int) -> bool:
    """Does a Go-To in this workflow jump back to the step at `index` or above?

    A Go-To is not a loop by itself: the builder uses it to skip FORWARD past
    a branch at least as often as it uses it to retry. The finding says "the
    workflow loops back to it", so the target has to resolve to a step at or
    before this one — a jump whose target is unknown is left to GHL020, which
    owns references that do not resolve.
    """
    positions: dict = {}
    for i, step in enumerate(wf.steps):
        sid = step.step_id
        if sid and sid not in positions:
            positions[sid] = i
    if not positions:
        return False
    for step in wf.steps:
        leaf = re.split(r"[./:]", str(step.type or ""))[-1].strip()
        if not GOTO_LEAF.match(leaf):
            continue
        for key, value in _scalars(step.raw):
            if _norm(key) in WIRING_KEYS or not isinstance(value, str):
                continue
            target = positions.get(value.strip())
            if target is not None and target <= index:
                return True
    return False


@rule("GHL085", "Retryable POST with no idempotency key", "high", "routing",
      "observability", "reliability")
def unsafe_retry_of_a_post(acct: Account):
    """A POST on a path that can run it twice, with nothing making that safe.

    A retry is only safe if the receiver can tell the second call is the same
    call. That is what an idempotency key is: the sender picks one value per
    real-world event and sends it on every attempt, and the receiver returns
    the first result instead of doing the work again. Stripe, and most payment
    and order APIs, support this specifically because a retried POST is how
    one customer gets charged twice. This fires only where the same step can
    genuinely run twice for one enrolment — retries declared on the step, or a
    Go-To that jumps back to it. A POST that can only ever run once is left
    alone, and so is one whose endpoint upserts.
    """
    for wf in _live(acct):
        for i, step in enumerate(wf.steps):
            if not _is_outbound_call(step) or _method(step) != "post":
                continue
            url = _call_url(step)
            if IDEMPOTENT.search(json.dumps(step.raw, default=str)) \
                    or UPSERT.search(url):
                continue
            if _step_retries(step):
                why = ("retries are enabled on this step, so a timeout sends "
                       "the same request again")
            elif _loops_back_to(wf, i):
                why = ("the workflow jumps back to it with a Go-To, so a "
                       "contact can pass through it several times")
            else:
                continue
            money = bool(MONEY.search(step.name + " " + step.type + " " + url))
            yield _finding(
                "GHL085", "high" if money else "medium", wf,
                "A POST that can run twice, with nothing making the second "
                "one safe",
                f"This step POSTs to an outside system and {why}. Nothing in "
                "the request tells the receiver that the second attempt is "
                "the same attempt, so it does the work twice — two "
                + ("charges against the same customer's card"
                   if money else "records, two of whatever this creates")
                + ". The first attempt often SUCCEEDED and only its reply was "
                "lost — a timeout is not proof that nothing happened, which "
                "is the part that makes this so easy to get wrong.",
                "Send an idempotency key: pick one stable value per real "
                "event (the order id, the contact id plus the event id — "
                "never a random value generated per attempt, which defeats "
                "the whole mechanism) and send it as an Idempotency-Key "
                "header on every attempt. Check the provider's docs for the "
                "header they expect. Where the endpoint does not support one, "
                "make the workflow check whether the record already exists "
                "before it posts.",
                step=step.name or step.type,
                cost="A retry that should have cost nothing creates a second "
                     + ("charge — the refund, the chargeback and the "
                        "conversation are all more expensive than the sale."
                        if money else
                        "record, and every report and follow-up built on "
                        "that data inherits the duplicate."))


# --------------------------------------------------------------------------
# GHL086 — a destructive step nobody can reconstruct
# --------------------------------------------------------------------------

DESTRUCTIVE_WORDS = ("delete", "destroy", "purge", "wipe", "truncate",
                     "erase", "refund")

TRACE_WORDS = ("note", "log", "audit", "record", "journal", "ledger", "trail")

LOG_SINK = re.compile(
    r"sheet|airtable|notion|log|audit|ledger|journal|trail|archive|backup|"
    r"history|bigquery|database|postgres|mysql|mongo|supabase|table|slack|"
    r"discord|teams|monitor|sentry|datadog|s3", re.I)

# A step that WRITES DOWN a destructive act is not the act. "Log the deletion
# to the audit sheet" contains the word delete and is the trail this rule
# wants to find, not the thing it wants to flag.
RECORDING = re.compile(
    r"\blogs?\b|logging|note|audit|ledger|journal|trail|archive|backup|"
    r"snapshot|history", re.I)


def _records_rather_than_acts(step: Step) -> bool:
    return bool(RECORDING.search(step.name)) or bool(RECORDING.search(step.type))


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _destructive(step: Step) -> str:
    """What makes this step irreversible, in words, or ''.

    The word has to appear somewhere that means the step DOES the thing — its
    type, its operation, its HTTP method, or the endpoint it calls. Reading
    step names alone flags "Tell the team about the refund", which is a
    notification about a destructive act somebody else performed, and firing
    on that is how a report loses a reader. The name and the URL are the
    weakest of those signals, so they are also filtered against the steps that
    only describe the act — an HTTP DELETE stays a delete whatever it is
    called.
    """
    if _is_team_alert(step):
        return ""
    if _method(step) == "delete":
        return "an HTTP DELETE"
    type_words = _norm(step.type)
    for word in DESTRUCTIVE_WORDS:
        if word in type_words:
            return f"{_article(word)} {word} action"
    for key, value in _scalars(step.raw):
        if _norm(key) in ("operation", "action", "mode") \
                and isinstance(value, str):
            for word in DESTRUCTIVE_WORDS:
                if word in _norm(value):
                    return f"{_article(word)} {word} operation"
    if _is_outbound_call(step) and not _records_rather_than_acts(step):
        called = _norm(step.name + " " + _call_url(step))
        for word in DESTRUCTIVE_WORDS:
            if word in called:
                return f"{_article(word)} {word} call"
    return ""


def _leaves_a_trace(wf: Workflow, destructive: Step) -> bool:
    """Does anything else in this workflow record that the step ran?

    Generous on purpose. A tag, a field write, a note, an internal
    notification, a row appended to a sheet or a database, or a second call to
    a log sink all count — any one of them leaves a timestamp somebody can
    work backwards from. The finding is reserved for a workflow where there is
    genuinely nothing.
    """
    for step in wf.steps:
        # A second irreversible step is never the first one's audit trail,
        # however it is worded. "Delete the record" contains the word record;
        # reading it as a log entry hides both deletes at once.
        if step is destructive or _destructive(step):
            continue
        blob = _norm(step.type + " " + step.name)
        if any(word in blob for word in TRACE_WORDS) or "field" in blob:
            return True
        if _is_team_alert(step) or step.tags_added():
            return True
        if LOG_SINK.search(step.type):
            return True
        if _is_outbound_call(step) \
                and LOG_SINK.search(step.name + " " + _call_url(step)):
            return True
    return False


@rule("GHL086", "Irreversible action that leaves no trace", "high", "hygiene",
      "observability", "audit")
def irreversible_action_without_a_trail(acct: Account):
    """A delete or a refund with nothing in the workflow recording it.

    This is not GHL041 asking whether the call worked — it is the question
    that comes next, on the day someone asks what happened to a record. A
    delete that succeeds is the hardest kind of failure to investigate,
    because the evidence is what got deleted. Every mature build writes the
    trail BEFORE the irreversible step, not after: which record, which
    contact, when, and on whose authority. Without it the only account of
    what this workflow did lives in the other system's logs, if it keeps any,
    for as long as it keeps them.
    """
    for wf in _live(acct):
        for step in wf.steps:
            what = _destructive(step)
            if not what or _leaves_a_trace(wf, step):
                continue
            yield _finding(
                "GHL086", "high", wf,
                f"This workflow runs {what} and records nothing anywhere",
                f"This step is irreversible — {what} — and nothing else in "
                "the workflow writes down that it happened: no note, no "
                "field, no tag, no notification, no log row. When someone "
                "asks next month why a record is gone, the honest answer is "
                "that this workflow might have done it, on a date nobody can "
                "narrow down, to records nobody can list. If it ever fires on "
                "the wrong contact, there is nothing to reconstruct the "
                "damage from and nothing to restore.",
                "Write the trail first: add a note (or a row in a log sheet) "
                "immediately BEFORE this step, carrying the record id, the "
                "contact and the reason. Before, not after — if the step "
                "fails halfway you still want the attempt on record. Add an "
                "internal notification too where the action is rare enough "
                "that a human should know it ran at all.",
                step=step.name or step.type,
                cost="An irreversible action with no audit trail. The first "
                     "time it misfires, you cannot say what it touched, how "
                     "many, or when — which turns a small bug into an "
                     "unbounded one.")


# --------------------------------------------------------------------------
# GHL087 — every alert points at one person
# --------------------------------------------------------------------------

RECIPIENT_KEYS = ("to", "recipient", "recipients", "sendto", "email", "emails",
                  "phone", "phonenumber", "userid", "userids", "users",
                  "assignedto", "teammembers", "recipientuserid", "notifyuser",
                  "notifyusers")

# Keys whose value is a destination, never a person: a Slack channel, a chat
# id or an incoming-webhook URL reaches whoever is watching it. "channel" on
# its own is deliberately absent — GoHighLevel writes {"channel": "email"} to
# mean the delivery method, which is not a destination at all.
SHARED_KEYS = ("channelid", "channelname", "slackchannel", "chatid", "roomid",
               "spaceid", "groupid", "teamid", "webhookurl", "hookurl")

GROUP_HINT = re.compile(
    r"team|group|role|everyone|all[-_ ]?users|rotation|round[-_ ]?robin|"
    r"on[-_ ]?call|assigned|owner|^#|@channel|@here", re.I)

# A role mailbox is a room, not a person: ops@ and support@ are read by
# whoever is on shift, which is exactly the arrangement this rule wants
# people to move TO.
SHARED_MAILBOX = re.compile(
    r"^(ops|operations|oncall|support|helpdesk|help|alerts?|admin|it|dev|eng|"
    r"engineering|info|hello|contact|notifications?|monitoring|noc|inbox|"
    r"office|billing|accounts?|sales|team|dl)([._+-]|\d|$)", re.I)


def _is_shared_address(value: str) -> bool:
    local = value.split("@")[0].strip() if "@" in value else ""
    return bool(local) and bool(SHARED_MAILBOX.match(local))


def _alert_addressees(step: Step):
    """(named individuals, whether anything shared or dynamic is addressed).

    A merge field is not a named person — {{ contact.assigned_to.email }}
    routes to whoever owns the record, which is the pattern this rule wants
    people to move TO, so it must never be what trips it.
    """
    people, shared = set(), False
    for key, value in _scalars(step.raw):
        nk = _norm(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if nk in SHARED_KEYS:
            shared = True
            continue
        if nk not in RECIPIENT_KEYS:
            continue
        # "dana@x.com, sam@x.com" in one field is two addressees. Read as one
        # string it looks like a single point of failure and is the opposite.
        for part in re.split(r"[,;]", value):
            v = part.strip()
            if not v:
                continue
            if "{{" in v or GROUP_HINT.search(v) or _is_shared_address(v):
                shared = True
                continue
            people.add(v)
    return people, shared


@rule("GHL087", "Every alert in the account lands on one person", "medium",
      "routing", "observability", "alerting")
def alerts_addressed_to_one_person(acct: Account):
    """Alerting with a bus factor of one.

    A named individual on a notification is not wrong by itself — it is wrong
    when it is the ONLY addressee, in workflow after workflow, because then
    that part of the account's monitoring is one person's inbox. They take a
    week off, change roles, or leave, and nothing announces that the alerts
    stopped being read; the workflows keep firing perfectly into a mailbox
    nobody opens. One or two workflows can be one person genuinely owning one
    area, so the threshold is three — at three it is a habit, not a staffing
    choice. It is worse again when there is no other alert destination
    anywhere in the account: then the whole monitoring layer is that one
    person, which is the severity split below.
    """
    solo: dict = {}
    elsewhere = False
    for wf in _live(acct):
        alerts = [s for s in wf.steps if _is_team_alert(s)]
        if not alerts:
            continue
        people, shared = set(), False
        for step in alerts:
            found, is_shared = _alert_addressees(step)
            people |= found
            shared = shared or is_shared
        if shared or len(people) > 1:
            elsewhere = True
            continue
        if len(people) != 1:
            # Nothing readable is addressed. That is its own question and
            # GHL020/GHL031 own it; it is not evidence either way here.
            continue
        solo.setdefault(people.pop(), []).append(wf)

    users = acct.inventory.users or {}
    for person in sorted(solo):
        wfs = solo[person]
        if len(wfs) < 3:
            continue
        only_one = not elsewhere and len(solo) == 1
        known = users.get(person)
        who = f"{known['name']} ({person})" if isinstance(known, dict) \
            and known.get("name") else person
        names = ", ".join(w.name for w in wfs)
        yield Finding(
            rule="GHL087", severity="high" if only_one else "medium",
            workflow=wfs[0].name, step=", ".join(w.name for w in wfs[1:]),
            category="routing",
            reach=sum(len(w.outbound) for w in wfs),
            title=f"{len(wfs)} workflows send every alert to one person — "
                  f"{who}",
            symptom=f"These all notify {who} and nobody else: {names}. That "
                    "one inbox is the whole monitoring layer for this part of "
                    "the account. A week off, a change of role, a phone on "
                    "silent, or a departure, and the alerts keep sending "
                    "perfectly into a mailbox nobody is reading — nothing "
                    "escalates, nothing bounces back to the team, and the "
                    "first sign of trouble is a client noticing."
                    + (" Nothing else in this account alerts anyone else "
                       "either, so there is no second pair of eyes to fall "
                       "back on." if only_one else
                       " Other workflows here do alert a shared destination, "
                       "which is the pattern to copy onto these."),
            fix="Point these at something that outlives one person: a shared "
                "inbox or a Slack channel the team watches, a user group, or "
                "the round-robin assignee rather than a hardcoded name. Where "
                "an individual genuinely should get it, add a second "
                "recipient as the fallback — one addressee is not a rota.",
            cost="One person's holiday is a silent outage. Everything keeps "
                 "running and reporting healthy, and nobody is reading the "
                 "one channel that would say otherwise.")


# --------------------------------------------------------------------------
# GHL088 — a failure path that raises no alarm
# --------------------------------------------------------------------------

FAILURE_HINT = re.compile(
    r"\berr\b|err[:._-]|error|\bfail\b|failed|failure|failing|exception|"
    r"dead[-_ ]?letter|\bdlq\b|retries?[-_ ]?exhausted|did[-_ ]?not[-_ ]?sync",
    re.I)

TASK_TYPE = re.compile(r"task|todo|ticket", re.I)
ASSIGNEE_KEYS = ("assignedto", "assigneduserid", "assignee", "assignedusers",
                 "userid", "userids", "owner")

# An address with no merge field in it belongs to a colleague. A message to
# the CONTACT is always addressed with a token, so a literal one in a workflow
# is somebody's desk.
LITERAL_EMAIL = re.compile(r"^[^@\s{}]+@[^@\s{}]+\.[a-z]{2,}$", re.I)
EMAIL_TO_KEYS = ("to", "toemail", "recipient", "recipients", "sendto",
                 "email", "emails")


def _assigns_a_human(step: Step) -> bool:
    """A task landing in a named person's list — GoHighLevel notifies them."""
    if not TASK_TYPE.search(step.type) and not TASK_TYPE.search(step.name):
        return False
    return any(_norm(k) in ASSIGNEE_KEYS and str(v).strip()
               for k, v in _scalars(step.raw))


def _emails_a_colleague(step: Step) -> bool:
    for key, value in _scalars(step.raw):
        if _norm(key) in EMAIL_TO_KEYS and isinstance(value, str):
            if any(LITERAL_EMAIL.match(p.strip())
                   for p in re.split(r"[,;]", value)):
                return True
    return False


def _raises_an_alarm(step: Step) -> bool:
    """Could this step reach a human? Deliberately the widest test in the pack.

    The finding says nobody is told, so anything that plausibly tells somebody
    has to clear it — a notification action, an ops channel, a task assigned
    to a named person, an email to a fixed address. Missing a real defect here
    costs one line in a report; claiming a build has no alerting when it
    alerts through a Telegram node costs the reader's trust in the other 99
    checks.
    """
    return _is_team_alert(step) or _assigns_a_human(step) \
        or _emails_a_colleague(step)


def _has_error_workflow(wf: Workflow) -> bool:
    """n8n's shared error workflow — the failure IS routed to an alarm.

    GHL043 recommends exactly this: one Error Trigger workflow attached to
    every workflow on the instance. Flagging a workflow for having taken that
    advice would put two rules in this catalog in direct contradiction.
    """
    settings = wf.settings if isinstance(wf.settings, dict) else {}
    return any("errorworkflow" in _norm(k) and str(v).strip()
               for k, v in settings.items())


@rule("GHL088", "Failure path that raises no alarm", "high", "routing",
      "observability", "alerting")
def error_path_nobody_hears(acct: Account):
    """The failure is caught, recorded, and nobody is ever told.

    This is the build one step past GHL041: somebody DID handle the error —
    there is a branch for it, or a tag like 'err:sync-failed' — and then the
    handling stops there. A tag is a record, not an alarm. Nothing polls it,
    so the count climbs quietly and the integration can be down for a month
    with a perfectly tidy error path.

    The one architecture this must not flag is the good one: a workflow that
    tags the failure and a separate listener that triggers on that tag and
    alerts. So the whole account is read first, and a workflow is only flagged
    when no listener anywhere watches the tag it writes — which is also the
    sharpest version of this finding, because "you have an alert listener and
    this tag is not on it" is a defect someone believes they already fixed.
    Scoped to workflows that actually call an outside system, so a dunning
    sequence with a "payment failed" branch is not mistaken for one.
    """
    listeners: dict = {}
    for wf in _live(acct):
        if not any(_raises_an_alarm(s) for s in wf.steps):
            continue
        for tag in wf.trigger_tags():
            listeners.setdefault(tag, wf.name)

    for wf in _live(acct):
        if any(_raises_an_alarm(s) for s in wf.steps) or _has_error_workflow(wf):
            continue
        if not any(_is_outbound_call(s) for s in wf.steps):
            continue
        handlers = [s for s in wf.steps
                    if FAILURE_HINT.search(s.name)
                    or any(FAILURE_HINT.search(t) for t in s.tags_added())]
        if not handlers:
            continue
        error_tags = {t for s in wf.steps for t in s.tags_added()
                      if FAILURE_HINT.search(t)}
        if error_tags & set(listeners):
            continue
        # Only a listener watching a FAILURE tag makes the sharper claim
        # true. An account whose one alert workflow watches 'hot-lead' has
        # not built an error-alerting layer, and telling its owner their
        # error tag is "not on the list" points at a list that does not
        # exist.
        watching = sorted(t for t in listeners if FAILURE_HINT.search(t))
        if watching and error_tags:
            watched = ", ".join(watching[:3])
            tagged = ", ".join(sorted(error_tags))
            yield _finding(
                "GHL088", "high", wf,
                f"The failure tag this workflow writes ({tagged}) is not one "
                "any alert listens for",
                f"When this workflow's integration fails it tags the contact "
                f"'{tagged}' and stops. There IS alerting in this account — "
                f"workflows listening for {watched} — and this tag is not on "
                "that list, so the tag is applied, the contact sits there, "
                "and no alert ever fires. This is worse than having no error "
                "handling at all, because from the outside it looks handled: "
                "somebody built the error path, and it ends in a dead end.",
                f"Either add '{tagged}' to the trigger of the alert workflow "
                "that already exists, or point this workflow at the tag that "
                "listener is watching. Then break the integration on purpose "
                "and confirm the alert actually arrives — a failure path is "
                "not finished until it has been fired once on purpose.",
                step=handlers[0].name or handlers[0].type,
                cost="The integration can be down for weeks with a perfectly "
                     "tidy error path. The tag count climbs and nothing tells "
                     "a human to look at it.")
            continue
        yield _finding(
            "GHL088", "high", wf,
            "This workflow handles its failures and tells nobody",
            "There is an error path here — something catches the failure — "
            "and nothing anywhere in it notifies a person: no internal "
            "notification, no Slack or Telegram message, no task for anyone, "
            "no call to a monitor. Recording a failure and raising an alarm "
            "are two different jobs, and only the first one is done. The "
            "integration this workflow depends on can be broken for weeks "
            "while every run completes 'successfully' down the error branch, "
            "and the first person to find out is the client asking where "
            "their data went.",
            "Put a notification on the failure branch — an internal "
            "notification or a Slack post naming the workflow, the contact "
            "and what failed. Better still, have every workflow tag its "
            "failures the same way ('err:<system>-failed') and build ONE "
            "listener workflow on that tag that alerts, so the next "
            "integration is covered the day it is added.",
            step=handlers[0].name or handlers[0].type,
            cost="Silent failure. Everything reports as fine, the error "
                 "branch absorbs every broken run, and the outage is "
                 "measured in weeks instead of minutes.")
