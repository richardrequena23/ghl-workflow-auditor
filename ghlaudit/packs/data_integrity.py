"""Data integrity — the CRM rotting quietly underneath a workflow that runs green.

Every check here is about what a workflow LEAVES BEHIND rather than what it
sends: a phone number stored in a shape nothing can match, an opt-out flag
switched back off, a tag list that grows one entry per contact, a field the
account writes and never reads, a date field holding "ASAP", a value overwritten
before anything could read it. None of these raise an error, none of them appear
in a workflow's execution history, and each is normally found months later when a
report, a filter or a bulk action is wrong and nobody can say since when.

Most of these judge a written VALUE, and three of them infer a field's TYPE from
its NAME. Neither is something an export fully supports, so both are fenced: a
bare merge token is never called wrong, because what it holds is not in the file,
and a name carrying a qualifier — "Budget Range", "SMS Opt In Date", "Phone
Verified At" — is never type-checked, because that word is the field saying it
holds a label rather than the thing the rest of the name promises. Being wrong
about a field costs more than staying quiet about one: the field a data rule
misreports is the first one the client goes and checks.

Boundaries with the rest of the catalog: GHL044 owns create-vs-upsert, GHL047
owns "several workflows write this field" (the cross-workflow race, raised as a
possible one — this pack deliberately does not re-report it), GHL020/GHL023 own
references to fields that do not exist, and GHL014/GHL040 own re-trigger loops.
These six sit everywhere else in that family.
"""

from __future__ import annotations

import json
import re

from ..model import STANDARD_CONTACT_FIELDS, Account, Step, Workflow, slug
from ..rules import Skip, _finding, rule

# A merge token, whatever is inside it. Every rule here that judges a written
# VALUE has to separate the literal half from the half that resolves at send
# time, because only the literal half can be judged from an export.
TOKEN = re.compile(r"\{\{[^}]*\}\}")
# A custom value is ONE string for the whole location, so a token pointing at
# one does not vary per contact. Telling the two apart is the difference
# between "every contact mints its own tag" and "the agency parameterised a
# tag name", which is ordinary snapshot practice.
CUSTOM_VALUE_TOKEN = re.compile(r"\{\{\s*custom_values?\.[^}]*\}\}", re.I)
# Two letters in a row — a word. One letter on its own is a format marker, the
# T and the Z in "{{ date }}T00:00:00Z", and marks nothing wrong.
WORD = re.compile(r"[A-Za-z]{2,}")

FIELD_WRITE_TYPE = re.compile(
    r"(update|set|edit|write)[_ -]?(contact[_ -]?)?(custom[_ -]?)?field|"
    r"(update|edit)[_ -]?contact(?![_ -]?tag)", re.I)

# Keys that hold the NAME of the field being written, and keys that hold the
# value. Deliberately narrow — "name" and "key" also sit on the step itself, and
# a step whose own name was read as a field name writes a field called "Store
# mobile number", which is a finding about nothing.
FIELD_KEY_NAMES = {"field", "fieldkey", "fieldid", "fieldname", "customfield",
                   "customfieldid", "targetfield", "attribute"}
FIELD_VALUE_NAMES = {"value", "newvalue", "fieldvalue", "val", "to", "set",
                     "content"}
# Where the READABLE name sits when the key beside it is an id. GoHighLevel's
# own workflow document writes a contact-field update as
#     {"attributes": {"actionType": "update_field_data",
#                     "fields": [{"field": "tqcNZQbMZYhmYwFdRrLd",
#                                 "value": "Facebook Ads",
#                                 "title": "Attribution Channel",
#                                 "type": "string"}]}}
# so `field` holds the custom field's ID and `title` is the only readable name
# in the step. Reading `field` alone is what made every write in a live account
# invisible to four of these six checks — and made the one finding that did
# survive name a field called 'tqcnzqbmzyhmywfdrrld'.
#
# The `type` beside it is NOT the field's data type and is never read here: the
# builder writes "string" for every custom field whatever its real type, and
# older steps in the same account carry "TEXT" for a field that is also TEXT —
# the key is a UI input hint, so a type check built on it would be a guess.
FIELD_NAME_KEYS = {"title", "fieldname", "fieldlabel", "fieldtitle",
                   "displayname", "fielddisplayname"}


def _nk(key) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


def _looks_like_an_id(value: str) -> bool:
    """True when this key is one of GoHighLevel's opaque object ids.

    A field KEY is lower case with separators ('contact.attribution_channel');
    a display NAME has spaces ('Attribution Channel'); an ID is a long run of
    letters and digits with neither ('tqcNZQbMZYhmYwFdRrLd'). Deliberately
    strict — three capitals or a digit, sixteen characters, no separator — so
    that a camelCase field key is not mistaken for one. Being wrong the strict
    way loses a finding; being wrong the loose way puts a 20-character id in
    front of a client instead of the name of their field.
    """
    v = str(value).strip()
    if len(v) < 16 or not re.fullmatch(r"[A-Za-z0-9]+", v):
        return False
    return bool(re.search(r"\d", v)) or sum(c.isupper() for c in v) >= 3


def _as_text(value) -> str:
    """A written value as the string the CRM would end up holding."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value if isinstance(value, str) else ""


def _field_slug(key) -> str:
    """'contact.lead_temperature' and 'Lead Temperature' are one field."""
    return slug(str(key).split(".")[-1])


def _words(key: str) -> set:
    """The field name's words. Keys arrive slugged, so this is a split."""
    return {w for w in str(key).split("_") if w}


def _word_boundary(key: str) -> str:
    """A search for one field key that will not match inside a longer word."""
    return r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])"


def _display_name(obj) -> str:
    """The readable field name sitting beside an id, if this object has one."""
    if not isinstance(obj, dict):
        return ""
    for k, v in obj.items():
        if _nk(k) in FIELD_NAME_KEYS and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _resolve(key: str, holder, allow_title: bool):
    """(readable field key, the id it came from) for one written field.

    Three outcomes. A readable key stands as it is. An opaque id with a
    display name beside it resolves to the name, and the id is carried along
    so a READ of the same field by id can still be recognised. An opaque id
    with no name beside it resolves to nothing at all — the field is genuinely
    unidentifiable from the file, and inventing a key for it is how a data
    rule starts lying about which field it means.
    """
    if not _looks_like_an_id(key):
        return _field_slug(key), ""
    title = _display_name(holder) if allow_title else ""
    if not title:
        return "", ""
    return _field_slug(title), str(key).strip()


def _pairs_from(obj, allow_title: bool = False) -> list:
    """(field key, value, id) out of one settings object, if it carries both.

    `allow_title` is off for the step's own raw body, because a bare `title`
    there is the STEP's title — reading it as a field name writes a field
    called "Store mobile number", which is a finding about nothing.
    """
    if not isinstance(obj, dict):
        return []
    key = value = None
    for k, v in obj.items():
        nk = _nk(k)
        if key is None and nk in FIELD_KEY_NAMES and isinstance(v, (str, int)) \
                and str(v).strip():
            key = str(v)
        elif value is None and nk in FIELD_VALUE_NAMES \
                and isinstance(v, (str, bool, int, float)):
            value = _as_text(v)
    if key is None or value is None:
        return []
    resolved, ident = _resolve(key, obj, allow_title)
    return [(resolved, value, ident)]


def _field_writes(step: Step) -> list:
    """[(field key, written value, field id)] for every field this step sets.

    Four shapes, because four exports write it four ways: the flat
    field/value pair, a {"fields": {...}} map, a list of field/value objects
    (what a real GoHighLevel export writes), and a map whose values are
    themselves {"value": ...} objects.
    """
    cfg = step.config()
    if not isinstance(cfg, dict):
        return []
    # `config()` falls back to the step's own raw body when the export has no
    # settings holder, and a `title` there belongs to the step, not the field.
    nested = cfg is not step.raw
    out = list(_pairs_from(cfg, allow_title=nested))
    for k, v in cfg.items():
        if _nk(k) not in ("fields", "customfields", "contactfields"):
            continue
        if isinstance(v, dict):
            for fk, fv in v.items():
                if isinstance(fv, (str, bool, int, float)):
                    key, ident = _resolve(fk, None, False)
                    out.append((key, _as_text(fv), ident))
                elif isinstance(fv, dict):
                    key, ident = _resolve(fk, fv, True)
                    for vk, vv in fv.items():
                        if _nk(vk) in FIELD_VALUE_NAMES \
                                and isinstance(vv, (str, bool, int, float)):
                            out.append((key, _as_text(vv), ident))
                            break
        elif isinstance(v, list):
            for item in v:
                out.extend(_pairs_from(item, allow_title=True))
    return [row for row in out if row[0]]


def _writes_fields(step: Step) -> bool:
    return bool(FIELD_WRITE_TYPE.search(str(step.type or "")))


# --------------------------------------------------------------------------
# GHL071 — one human, two contact records
# --------------------------------------------------------------------------

PHONE_FIELD = re.compile(
    r"(?:^|_)(phone|phone2|mobile|cell|cellphone|whatsapp|telephone|tel|sms)"
    r"(?:_|$)")
# Words that turn a phone-ish name into a field about something OTHER than the
# number: "SMS Opt In Date" holds a date, "Phone Verified At" a timestamp,
# "WhatsApp Group ID" an identifier. Each of those carries seven-plus digits
# and none of them is E.164 — judging them as phone numbers reported three
# correctly stored fields as broken on the first account this was run against.
NOT_THE_NUMBER = {"date", "time", "at", "id", "ids", "uuid", "type", "carrier",
                  "provider", "status", "consent", "opt", "verified", "valid",
                  "country", "code", "ext", "extension", "count", "credits",
                  "score", "url", "link", "lookup", "reason", "source",
                  "method", "label", "note", "notes"}
# E.164: a plus, a country code that cannot start with zero, then digits. No
# spaces, no punctuation, no extension — that is the whole point of it.
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
# What can legitimately sit in front of a merge field: the plus on its own, or
# the plus and a literal country code. "+{{ country_code }}{{ phone_raw }}" is
# the account ASSEMBLING E.164 out of the two halves it stores, which is the
# fix this rule asks for, not an instance of the defect.
COUNTRY_PREFIX = re.compile(r"^\+\d{0,3}$")
# A channel scheme in front of an E.164 number. Twilio addresses WhatsApp as
# "whatsapp:+15551234567" and a click-to-call field holds "tel:+15551234567";
# both are exact, both match everywhere once the scheme is stripped, and
# neither is the "two strings for one human" problem this rule is about.
CHANNEL_SCHEME = re.compile(r"^(tel|sms|mms|callto|whatsapp|wa)\s*:\s*", re.I)


def _phone_number_field(key: str) -> bool:
    """True when the field's NAME says it holds the number itself."""
    return bool(PHONE_FIELD.search(key)) and not (_words(key) & NOT_THE_NUMBER)


def _phone_problem(value: str) -> str:
    """How this written phone number departs from E.164, or "" if it does not."""
    original = (value or "").strip()
    v = CHANNEL_SCHEME.sub("", original).strip()
    if not v:
        return ""
    if TOKEN.search(v):
        rest = TOKEN.sub("", v).strip()
        if not rest:
            return ""  # a bare merge field — its contents are not in the export
        if COUNTRY_PREFIX.fullmatch(rest):
            return ""  # "+1{{ phone }}" is the fix for this, not an instance of it
        return f"assembled out of pieces as '{original}'"
    if E164.fullmatch(v):
        return ""
    if len(re.sub(r"\D", "", v)) < 7:
        return ""  # not a phone number at all; some other field write
    return f"stored as '{original}'"


@rule("GHL071", "Phone number written to a contact field without E.164 formatting",
      "high", "hygiene", "data", "duplicates")
def phone_written_unnormalised(acct: Account):
    """A number written in a shape that only matches itself.

    E.164 (+15551234567) is the one form every system agrees on: it is what
    Twilio delivers on the inbound webhook, what the API returns, and what any
    export or spreadsheet built off this account will carry. A number written
    into the record as '(555) 123-4567' — or assembled out of an area code and
    a line number — is a different STRING for the same human, so every lookup
    that compares numbers (a dedupe check, an inbound match, a sync into
    another system) sees two people. Two things are never judged: a bare merge
    field, which could hold anything, and a field whose name says it holds
    something else about the phone rather than the number.
    """
    for wf in acct.published():
        for step in wf.steps:
            if not _writes_fields(step):
                continue
            for key, value, _ident in _field_writes(step):
                if not _phone_number_field(key):
                    continue
                problem = _phone_problem(value)
                if not problem:
                    continue
                yield _finding(
                    "GHL071", "high", wf,
                    f"Phone number written to '{key}' in a shape nothing "
                    "else will match",
                    f"This step writes the contact's number {problem}. E.164 "
                    "(+15551234567) is the only form that is unambiguous — it "
                    "is what arrives on an inbound message, what the API "
                    "returns, and what any spreadsheet or connected system "
                    "reads. A number stored any other way is a different "
                    "string for the same person, so the next lookup that "
                    "compares numbers does not find this contact and creates "
                    "or works a second record instead. The conversation then "
                    "splits across two cards and each one looks like a lead "
                    "who went quiet.",
                    "Write the number in E.164: strip everything that is not "
                    "a digit and prefix the country code, then write the "
                    "single normalised value. Verify by opening the contact "
                    "and confirming the number shows with a leading + and no "
                    "spaces or brackets, then sending it a test message.",
                    step=step.name or step.type,
                    cost="Duplicate contacts, and the half of the "
                         "conversation history that lives on the twin. A rep "
                         "works the record with no notes on it and the "
                         "customer repeats themselves.")


# --------------------------------------------------------------------------
# GHL072 — the record of consent, overwritten
# --------------------------------------------------------------------------

OPT_OUT_FIELD = re.compile(
    r"(?:^|_)(dnd|do_not_disturb|opt_out|opted_out|unsubscribe|unsubscribed)"
    r"(?:_|$)")
OPT_OUT_TYPE = re.compile(r"dnd|do[_ -]?not[_ -]?disturb|opt[_ -]?out|"
                          r"unsubscrib", re.I)
# A step whose TYPE says it removes the flag carries no field/value pair at
# all — the action IS the instruction. Only believed when the type names both
# halves ("remove dnd", "dnd off"), never on a bare "dnd" step, which is just
# as likely to be the action that SETS it.
OPT_OUT_REMOVAL_TYPE = re.compile(
    r"(remove|clear|delete|disable|reset|undo|off|revoke)[_ -]?"
    r"(contact[_ -]?)?(dnd|do[_ -]?not[_ -]?disturb|opt[_ -]?out|unsubscrib\w*)"
    r"|(dnd|do[_ -]?not[_ -]?disturb|opt[_ -]?out)[_ -]?"
    r"(removal|removed|remove|clear\w*|off|disable\w*)", re.I)
# Words that make the name an ATTRIBUTE of the opt-out rather than the flag:
# "Opt Out Reason" = none, "Unsubscribe Link" = the URL, "DND Until" = a date.
# Writing any of those changes nothing about consent.
NOT_THE_FLAG = {"reason", "date", "at", "time", "until", "expires", "source",
                "method", "count", "link", "url", "text", "message", "note",
                "notes", "type", "by", "ip", "id"}
# Everything the exports write for "off". "unsubscribed" is deliberately not
# here: as a VALUE it means the flag is being set, not cleared.
FALSEY = {"false", "no", "off", "0", "n", "disabled", "inactive", "none",
          "null", "remove", "removed", "clear", "cleared", "unset"}


def _opt_out_flag(key: str) -> bool:
    return bool(OPT_OUT_FIELD.search(key)) and not (_words(key) & NOT_THE_FLAG)


def _opt_out_cleared(step: Step):
    """(field, value) when this step switches an opt-out flag back off."""
    for key, value, _ident in _field_writes(step):
        if _opt_out_flag(key) and value.strip().lower() in FALSEY:
            return key, value
    # A dedicated DND action carries no field/value pair — the flag IS the key.
    # Only a step whose TYPE is about DND is read this way. An "Update Contact"
    # action commonly ships the whole contact shape, `"dnd": false` included,
    # and that is the contact's CURRENT state, not an instruction: reading it
    # as one reports every contact-sync step in the account as a consent wipe.
    if not OPT_OUT_TYPE.search(str(step.type or "")):
        return None
    cfg = step.config()
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if not isinstance(v, (str, bool, int)):
                continue
            if _opt_out_flag(slug(k)) \
                    and _as_text(v).strip().lower() in FALSEY:
                return slug(k), _as_text(v)
    # The action that carries no settings at all and says what it does in its
    # own type: "Remove DND", "Clear Unsubscribed".
    t = str(step.type or "")
    if OPT_OUT_REMOVAL_TYPE.search(t):
        low = t.lower()
        flag = "dnd" if "dnd" in low or "disturb" in low else \
            "unsubscribed" if "unsubscrib" in low else "opt_out"
        return flag, ""
    return None


# A person can un-opt-out, and when they do, clearing the flag is the ONLY way
# to message them again — there is no second switch. So a workflow whose
# TRIGGER is the contact asking to be messaged again is doing this correctly.
# Evidence has to come from the trigger, never from the workflow's name: the
# name says what the builder meant, the trigger says who actually enters.
RE_CONSENT_NAME = re.compile(
    r"re[_ -]?subscribe|resubscribe|opt[_ -]?in|opt[_ -]?back[_ -]?in|"
    r"re[_ -]?consent|double[_ -]?opt", re.I)
# Keyword replies that mean "start messaging me" and nothing else. "YES" is
# deliberately absent — it is the answer to any question, including one asked
# by the workflow doing the clearing.
RE_CONSENT_KEYWORD = re.compile(
    r"^\s*(start|unstop|resume|subscribe|re-?subscribe|opt[_ -]?in|optin)\b",
    re.I)


def _strings_in(node) -> list:
    out: list[str] = []

    def walk(n):
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out


def _entered_by_re_consenting(wf: Workflow) -> bool:
    """True when the only way into this workflow is asking to be messaged."""
    for trg in wf.triggers:
        if RE_CONSENT_NAME.search(str(trg.name or "")) \
                or RE_CONSENT_NAME.search(str(trg.type or "")):
            return True
        try:
            values = _strings_in(trg.filters())
        except Exception:  # noqa: BLE001 - a filter shape nothing can walk
            values = []
        if any(RE_CONSENT_KEYWORD.match(v) for v in values):
            return True
    return False


@rule("GHL072", "Workflow switches a contact's opt-out back off", "high",
      "hygiene", "data", "compliance")
def opt_out_flag_cleared(acct: Account):
    """A step that erases the one record of someone saying stop.

    DND and unsubscribed are not settings, they are evidence: the field IS the
    record that a person asked you to stop, and the account keeps no second
    copy of it. A workflow that writes the flag back to false re-opens the
    channel and destroys the proof at the same moment — after it runs, nothing
    in the CRM shows the contact ever opted out, so the next audit cannot even
    find who was affected. It gets built for an honest reason (someone set DND
    in bulk by mistake, a "clean up the list" workflow) and then quietly runs
    on everybody who passes through.

    One build is exempt: a workflow a contact enters BY asking to be messaged
    again — a START/UNSTOP keyword, a re-subscribe form. Clearing the flag is
    the only way to honour that, there is no second switch, and reporting it
    would be telling a client their opt-in handler is a violation.
    """
    for wf in acct.published():
        if _entered_by_re_consenting(wf):
            continue  # the contact asked; this is the correct place to do it
        for i, step in enumerate(wf.steps):
            if not _writes_fields(step) \
                    and not OPT_OUT_TYPE.search(str(step.type or "")):
                continue
            cleared = _opt_out_cleared(step)
            if not cleared:
                continue
            key, value = cleared
            below = wf.outbound_after(i)
            yield _finding(
                "GHL072", "critical" if below else "high", wf,
                (f"'{key}' is written back to '{value}' here — messaging "
                 "re-opened for someone who opted out" if value else
                 f"This step switches '{key}' back off — messaging re-opened "
                 "for someone who opted out"),
                "That field is the record of a person telling you to stop, "
                "and this account has no other copy of it. Writing it back to "
                "off does two things at once: it re-opens the channel, and it "
                "erases the evidence, so afterwards nothing in the CRM shows "
                "the contact ever opted out and no later audit can identify "
                "who was affected."
                + (f" This workflow then sends {len(below)} message"
                   f"{'s' if len(below) != 1 else ''} of its own, so the "
                   "first message after the flag is cleared goes out right "
                   "here." if below else "")
                + " One build is not this: a workflow a contact enters BY "
                  "asking to be messaged again (a START reply, a re-subscribe "
                  "form) has to clear the flag, and is not reported. If this "
                  "step is capturing a brand-new lead's stated preference "
                  "rather than reversing an earlier stop, say so in the step "
                  "name — as written it cannot be told from a wipe.",
                "Delete the step. If DND was genuinely set in bulk by "
                "mistake, correct those contacts individually with the reason "
                "written down — never as a step on a live list. Where "
                "somebody really does opt back in, record the new consent "
                "(date, source, wording) in its own field and leave the "
                "original opt-out intact.",
                step=step.name or step.type,
                cost="Messaging people who opted out is what costs you the "
                     "sending number, the domain reputation, or a complaint "
                     "with real money attached — and this step re-opens the "
                     "door for every contact who passes through the workflow.")


# --------------------------------------------------------------------------
# GHL073 — a tag list that grows one entry per contact
# --------------------------------------------------------------------------

# Merge sources that are effectively unique per contact. A tag built on one of
# these mints a tag per person. Anything else — a status, a stage, a plan, a
# source — may hold four values, and four tags is a build somebody meant, so
# the finding on those states the risk instead of asserting the damage.
UNBOUNDED_MERGE = re.compile(
    r"(?:^|[._])(first_?name|last_?name|full_?name|name|email|phone|mobile|"
    r"city|address\d?|full_?address|postal_?code|zip|company|company_?name|"
    r"business_?name|website|url|link|date|time|datetime|birthday|dob|id|"
    r"uuid|message|body|response|answer|note|notes|score|amount|total)"
    r"(?:$|[._])", re.I)


def _merge_sources(tag: str) -> list:
    """The token paths inside a tag name, custom values already removed."""
    return [t.strip("{} ").strip()
            for t in TOKEN.findall(CUSTOM_VALUE_TOKEN.sub("", tag))]


@rule("GHL073", "Tag name built from a merge field", "medium", "hygiene",
      "data", "tags")
def tag_minted_from_a_merge_field(acct: Account):
    """A tag whose name varies with the contact.

    Tags are a fixed vocabulary — the value of one is that thousands of
    contacts share it, which is what makes a filter, a smart list or a bulk
    action possible. Put a contact's own data in the name and the tag list
    grows with the contact list. How badly depends entirely on how many
    distinct values the merged field holds, and the export does not say: a
    field with four options mints four tags, which is a build somebody meant;
    a name, an email or a city mints one per person. So the severity follows
    the source, and the finding on a bounded source states the risk rather
    than asserting damage. There is a second possibility either way — that
    HighLevel does not resolve a merge field in a tag name at all and every
    contact gets one tag called '{{ contact.city }}'. I have not tested which
    it does; the fix is the same, because varying data belongs in a field.
    """
    for wf in acct.published():
        for step in wf.steps:
            for tag in sorted(step.tags_added()):
                # A custom value is one string for the whole location, so
                # "{{ custom_values.current_promo }}" is a single tag shared by
                # everybody — how an agency snapshot parameterises a tag name,
                # and correct. Only a per-contact token mints a tag per contact.
                if "{{" not in CUSTOM_VALUE_TOKEN.sub("", tag):
                    continue
                sources = _merge_sources(tag)
                unbounded = any(UNBOUNDED_MERGE.search(s) for s in sources)
                yield _finding(
                    "GHL073", "medium" if unbounded else "low", wf,
                    f"Tag name contains a merge field: '{tag}'",
                    "This step applies a tag whose name is built from contact "
                    "data, so the tag is different for every distinct value "
                    "that field holds."
                    + (" It is merging something close to unique per person "
                       "(a name, an email, a town, a date), so the account "
                       "accumulates roughly one tag per contact: after a few "
                       "thousand of them the tag picker, the filters and "
                       "every smart list built on tags stop being usable, and "
                       "nobody can safely delete any of it because no one "
                       "knows what still references what."
                       if unbounded else
                       " If that field holds a handful of options this is a "
                       "small fixed set of tags and it is fine — worth "
                       "confirming, not fixing. If it can hold free text, the "
                       "tag list grows with the contact list and every smart "
                       "list built on tags becomes guesswork.")
                    + " The other possibility is that the merge field is not "
                      "resolved in a tag name at all, and every contact ends "
                      "up with one tag named with the raw template text — in "
                      "which case the segment this was meant to create does "
                      "not exist.",
                    "Open the contact record of somebody who ran through this "
                    "step and read the tag that landed. If it is the raw "
                    "template text, or if the merged field is free text, "
                    "apply a tag from a fixed list instead and write the "
                    "varying value into a custom field. Fields are what "
                    "filters, smart lists and reports are built on; tags are "
                    "for membership, not for storing data.",
                    step=step.name or step.type,
                    cost="Tag lists do not recover. Once thousands of "
                         "one-contact tags exist, every bulk action and smart "
                         "list built on tags is guesswork, and cleaning it up "
                         "costs more than the build did."
                         if unbounded else
                         "Nothing today if the field has few values. If it "
                         "does not, the clean-up costs more than the build "
                         "did — and the segment nobody can build in the "
                         "meantime is the one this step was for.")


# --------------------------------------------------------------------------
# GHL074 — data collected that nothing consumes
# --------------------------------------------------------------------------

# Fields that exist to be evidence. Nothing reading them back is the POINT of
# them: "why they opted out", "when they consented", "which wording they saw"
# are written for a person and an audit, and calling them dead data reads as
# not understanding what they are for.
CONSENT_EVIDENCE = re.compile(r"consent|opt[_ -]?in|gdpr|tcpa|compliance", re.I)
# Fields whose whole job is to be reported on. Marketing attribution is written
# so a human can read it on the record and a dashboard can group by it — no
# workflow ever reads it back, and that is the design, not a defect. The live
# account this pack was calibrated against writes 'Attribution Channel' from
# five branch arms of a workflow called "Lead Source Attribution & Reporting",
# and nothing reads it: reporting that as dead data would have been the pack's
# only finding on a working account, and it would have been wrong.
REPORTING_SURFACE = re.compile(
    r"(?:^|_)(utm|utm_[a-z]+|attribution|referrer|referral_source|campaign|"
    r"ad_click_id|gclid|fbclid|msclkid|landing_page|first_touch|last_touch|"
    r"lead_source|traffic_source|channel|medium)(?:_|$)", re.I)


def _read_blob(acct: Account) -> str:
    """Everything in the account that could be READING a contact field.

    A field-write step naturally contains the name of the field it writes, so
    that key alone is stripped out of the step — counting it would make every
    field its own reader and the check would never fire. The id it was
    resolved from goes with it, for the same reason. The REST of the step
    stays, because a value copying one field into another, or a source-field
    key beside the target, is a read like any other. Draft workflows count as
    readers too: a field a paused build reads is not dead data, it is early.
    """
    parts: list[str] = []
    for wf in acct.workflows:
        for trg in wf.triggers:
            try:
                parts.append(json.dumps(trg.raw))
            except (TypeError, ValueError):  # a shape json cannot dump
                parts.append(str(trg.type))
        for step in wf.steps:
            if not _writes_fields(step):
                parts.append(step.text())
                continue
            written = _field_writes(step)
            text = slug(step.text())
            for key, _value, ident in written:
                for token in (key, slug(ident)):
                    if token:
                        text = re.sub(_word_boundary(token), " ", text)
            parts.append(text)
            parts.extend(value for _, value, _i in written)
    parts.extend(str(v) for v in acct.custom_values.values())
    return slug(" ".join(parts))


@rule("GHL074", "Custom field written that nothing in the account reads", "low",
      "hygiene", "data", "dead_weight")
def field_written_but_never_read(acct: Account):
    """The mirror of GHL018: data collected that nothing consumes.

    A field write is cheap, so these accumulate — a question added to a form
    for a campaign that ended, a field a previous build branched on, a value
    written "so we have it". The cost is not the write, it is the belief: the
    team assumes the answer is being used, so nobody notices that the
    follow-up it was supposed to drive was never built. Only fields the
    account actually owns are considered — a write to a key that is not in the
    field list is a dangling reference, which is GHL020/GHL023's job.

    Three families are never called dead, because nothing reading them back is
    what they are FOR: the opt-out flag, consent evidence, and marketing
    attribution. And the rest is reported at "low" with the question attached,
    because an export carries workflows and nothing else — a smart list, a
    dashboard column, a human reading the record, or an outside system pulling
    the contact are all consumers this file cannot see.
    """
    inv = acct.inventory
    if not inv.has("custom_fields"):
        yield Skip(
            rule="GHL074",
            title="Custom field written that nothing in the account reads",
            reason="The account's custom-field list was not supplied, so a "
                   "field key written by a workflow cannot be told apart from "
                   "a typo or a field that only existed in the account this "
                   "build was copied from.",
            needs="customFields in the input bundle",
            category="hygiene")
        return

    writers: dict = {}
    for wf in acct.published():
        for step in wf.steps:
            if not _writes_fields(step):
                continue
            for key, _value, ident in _field_writes(step):
                writers.setdefault(key, (wf, step, ident))

    blob = _read_blob(acct)
    for key in sorted(writers):
        if key in STANDARD_CONTACT_FIELDS or key not in inv.custom_fields:
            continue
        if OPT_OUT_FIELD.search(key) or CONSENT_EVIDENCE.search(key) \
                or REPORTING_SURFACE.search(key):
            continue
        # Three characters is not enough to search for: a key like "id" or
        # "mrr" matches inside unrelated words and the answer stops meaning
        # anything. Missing a short-named field is the safe direction.
        if len(key) < 4:
            continue
        wf, step, ident = writers[key]
        # A real export references a custom field by ID, not by key: the
        # branch that reads this field back carries 'tqcNZQbMZYhmYwFdRrLd',
        # not 'attribution_channel'. Checking only the key called a field with
        # three live readers dead.
        if re.search(_word_boundary(key), blob):
            continue
        if ident and re.search(_word_boundary(slug(ident)), blob):
            continue
        display = inv.custom_fields.get(key) or key
        yield _finding(
            "GHL074", "low", wf,
            f"'{display}' is written here and read nowhere",
            f"This workflow writes the custom field '{display}' and nothing "
            "else in the account — no message, no trigger filter, no "
            "condition, no other field — ever reads it back. If a smart list, "
            "a report or an outside system consumes it, this is fine and "
            "worth noting in the build docs. If not, the field is data "
            "collection nobody acts on: the real cost is that the team "
            "believes the answer is being used, so nobody notices the "
            "follow-up it was meant to drive does not exist.",
            "Confirm who reads it. If the answer is nobody, delete the write "
            "step and the question that feeds it; if it feeds a report or an "
            "external sync, say so in the field's description so the next "
            "person does not delete it.",
            step=step.name or step.type,
            cost="Nothing directly — it is the question you make customers "
                 "answer and then ignore, and the follow-up everyone assumes "
                 "is running off it.")


# --------------------------------------------------------------------------
# GHL075 — a value the field's type cannot hold
# --------------------------------------------------------------------------

DATE_FIELD = re.compile(
    r"(?:^|_)(date|dob|birthday|birthdate|anniversary|expiry|expiration|"
    r"expires|deadline|due)(?:_|$)")
# "budget" is deliberately absent. On a lead form it is nearly always a
# dropdown of written bands — "under $5k", "not sure" — so a field called
# Budget holding text is the normal build, not a defect.
#
# "score" and "rating" came out for the same reason, and this one was measured:
# the live account this pack was calibrated against has 'AI Lead Score' and
# 'RF Lead Score', and BOTH are declared TEXT in its custom-field list, because
# what a qualifier writes into them is "hot" / "warm" / "cold". A name-inferred
# number check would have called two correctly built fields broken.
NUMBER_FIELD = re.compile(
    r"(?:^|_)(amount|total|price|revenue|qty|quantity|count|"
    r"age|mrr)(?:_|$)")
# Words that mean the field holds a LABEL for the thing, not the thing: an
# "Age Group" is a band, a "Date Preference" is a written answer, a "Score
# Label" is a letter grade. Every one of them is correctly a text field, and
# type-checking their contents flags a working build.
NOT_THE_VALUE = {"range", "band", "bracket", "tier", "group", "segment",
                 "category", "type", "label", "grade", "level", "class",
                 "text", "note", "notes", "description", "comment",
                 "comments", "reason", "status", "preference", "preferences",
                 "option", "options", "choice", "answer", "question", "id",
                 "url", "link", "name", "source", "method"}
# Standard fields that hold text. A date or number field fed one of these is
# the mis-picked merge field — the picker inserts whatever was highlighted, and
# first_name sits next to the date fields in the list.
TEXT_TOKEN_FIELDS = {"first_name", "last_name", "name", "full_name", "email",
                     "phone", "company_name", "business_name", "city", "state",
                     "country", "address1", "address", "full_address", "source",
                     "tags", "website", "notes", "timezone"}
DATE_LITERAL = re.compile(
    r"^\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}"      # 2026-01-05, and ISO timestamps
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"        # 05/01/2026
    r"|\d{1,2}\s+\w{3,9}\.?,?\s+\d{2,4}"     # 5 January 2026
    r"|\w{3,9}\.?\s+\d{1,2},?\s+\d{2,4}"     # January 5, 2026
    r"|\d{9,13})")                           # epoch seconds or milliseconds
# Builders do write these, and whether the platform resolves them is not
# something I have tested. The unambiguous defect is free text like "ASAP".
RELATIVE_DATE = re.compile(r"^(today|now|tomorrow|yesterday|current[_ ]?date)$",
                           re.I)
# The words that join a date token to a time token. "{{ start_date }} at
# {{ start_time }}" is one datetime assembled out of the two halves GHL stores
# it in, not a date field with prose in it.
DATE_JOINERS = {"at", "on", "to", "and", "am", "pm", "st", "nd", "rd", "th"}


def _declared_kind(key: str) -> str:
    """The type this field's NAME claims, once its qualifiers are read."""
    if _words(key) & NOT_THE_VALUE:
        return ""
    if DATE_FIELD.search(key):
        return "date"
    if NUMBER_FIELD.search(key):
        return "number"
    return ""


def _parses_as_number(value: str) -> bool:
    try:
        float(re.sub(r"[,\s$£€%]", "", value))
        return True
    except ValueError:
        return False


def _unparseable(kind: str, value: str):
    """(what is wrong with this value, is it wrong whatever the type is).

    The second half is the honest half. The field's type is inferred from its
    NAME here, so most of these are only defects if the field really is typed —
    a plain text field called "Follow Up Date" holds "ASAP" perfectly well.
    Two of them are wrong either way: a first name merged into a date field is
    the wrong datum whatever the column is, and two numeric tokens run
    together produce a number that is neither of them.
    """
    v = (value or "").strip()
    if not v:
        return "", False
    tokens = TOKEN.findall(v)
    if tokens:
        for token in tokens:
            name = re.sub(r"[^a-z0-9_.]", "", token.strip("{} ").lower())
            if name.split(".")[-1] in TEXT_TOKEN_FIELDS:
                return f"merges {{{{ {name} }}}}, which holds text", True
        rest = TOKEN.sub("", v).strip()
        # Only WORDS beside a token prove the result cannot parse. Punctuation,
        # digits and lone letters around one are how a date and a time, or a
        # currency symbol and an amount, get assembled deliberately — and so
        # are the two or three words that join a date to a time ("{{ date }}
        # at {{ time }}"), which is a composition somebody meant. That reprieve
        # needs BOTH halves to be merge fields: "{{ day }} at 4pm" is a literal
        # time typed into a date field, and still cannot parse.
        words = [w.lower() for w in WORD.findall(rest)]
        if kind == "date" and len(tokens) > 1 and words \
                and all(w in DATE_JOINERS for w in words):
            words = []
        if rest and words:
            return f"glues a merge field to literal text — '{v}'", False
        # Two tokens run together can only ever produce one number after
        # another, which is not the quantity either of them holds. A date
        # composed of a date token and a time token is a build somebody meant,
        # so the same shape is left alone there.
        if kind == "number" and len(tokens) > 1:
            return f"joins {len(tokens)} merge fields together — '{v}'", True
        return "", False  # what a bare token holds is not in the export
    if kind == "date":
        if RELATIVE_DATE.match(v) or DATE_LITERAL.match(v):
            return "", False
        return f"is the text '{v}', which is not a date", False
    if not _parses_as_number(v):
        return f"is the text '{v}', which is not a number", False
    return "", False


@rule("GHL075", "Field written with a value its type cannot hold", "high",
      "hygiene", "data")
def value_does_not_fit_the_field(acct: Account):
    """A date field fed "ASAP", a number field fed a name.

    A typed field rejects what it cannot parse — and rejects it silently: no
    error, no failed step, the field simply ends up empty or holding text
    nothing can compare. Everything downstream then behaves as though the
    contact has no value at all: the reminder timed "3 days before" never
    fires, the filter never matches, the report shows a blank. The two classic
    sources are a free-text form answer wired straight into a date field, and
    the merge picker inserting the token that was highlighted rather than the
    one that was wanted.

    The fence around it: a GoHighLevel export does not carry a field's data
    type where the rules can read it, so the type here is inferred from the
    NAME and the finding says so out loud. A name carrying a qualifier
    ("Budget Range", "Date Preference") is not judged at all, because that
    word is the field telling you it holds a label rather than a value. And
    the severity splits on whether the type matters: a first name merged into
    a date field, or two numeric tokens run together, is the wrong datum
    whatever the column is (high); free text in a field the NAME says is a
    date is only a defect if the field really is a date (medium).
    """
    for wf in acct.published():
        for step in wf.steps:
            if not _writes_fields(step):
                continue
            for key, value, _ident in _field_writes(step):
                if _phone_number_field(key):
                    continue  # numbers that are not quantities — GHL071's job
                kind = _declared_kind(key)
                if not kind:
                    continue
                problem, certain = _unparseable(kind, value)
                if not problem:
                    continue
                yield _finding(
                    "GHL075", "high" if certain else "medium", wf,
                    f"'{key}' is written a value that is not a {kind}",
                    f"The field's name says it holds a {kind}, and this step "
                    f"writes a value that {problem}. A typed field refuses "
                    "what it cannot parse and refuses it quietly — no error, "
                    "no failed step — so the field ends up empty or holding "
                    "text nothing can compare. Every later step that reads "
                    f"it then behaves as if the contact has no {kind} at all: "
                    + ("the reminder timed off it never fires"
                       if kind == "date" else
                       "the branch comparing it against a threshold never "
                       "matches")
                    + ", the filter finds nobody, the report cell is blank. "
                    + ("The type is read from the field's NAME — an export "
                       "does not carry it — so check the field in Settings "
                       "-> Custom Fields first. If it is a plain text field "
                       "the value is stored and nothing breaks today; it "
                       "simply cannot be sorted, filtered or used to time "
                       "anything."
                       if not certain else
                       "This one is the wrong value whatever the field's "
                       "type turns out to be."),
                    f"Write a real {kind}: "
                    + ("map the free-text answer onto a date the workflow "
                       "calculates (booking date plus N days), or capture it "
                       "with a date picker rather than a text question."
                       if kind == "date" else
                       "send the numeric value on its own, with no currency "
                       "symbol, units or words attached.")
                    + " Then open a contact that ran through this step and "
                      "confirm the field is populated, not blank.",
                    step=step.name or step.type,
                    cost="Everything keyed off this field silently does "
                         "nothing for these contacts, and the gap looks like "
                         "'they just never got the follow-up'.")


# --------------------------------------------------------------------------
# GHL076 — a value overwritten before anything could read it
# --------------------------------------------------------------------------

# Anything that forks the run. A branch means the two writes may sit on arms
# that never both execute, and then neither one is dead.
BRANCHY = re.compile(r"branch|condition|split|goal|filter|switch|case|"
                     r"if[_ -]?else|ifelse", re.I)


def _single_path(wf: Workflow) -> bool:
    """True when this FLAT export shows one path, in the order it is written.

    A branch step means the two writes may be on arms that never both run, so
    "the first value is dead" stops being provable and this check declines
    rather than guesses — a data rule that is wrong about which of two steps
    matters is worse than one that stays quiet on the exports it cannot read.
    Only asked of exports with no wiring; a wired one is read link by link.
    """
    for step in wf.steps:
        if step.is_branch or BRANCHY.search(str(step.type or "")):
            return False
    return True


def _next_step(wf: Workflow, step: Step, by_id: dict):
    """The step that ALWAYS runs immediately after this one, or None.

    A real GoHighLevel export is a linked list: every node carries `next`, and
    every node carries the `parentKey` of the node above it. A fork writes
    `next` as a LIST of branch ids, and its arms hang off it by parentKey — so
    "exactly one successor, counting both directions" is the whole test for
    "these two steps are adjacent on one path". Reading the flat file ORDER
    instead is what made this check refuse every real export: the five arms of
    one if/else all write the same field, and in file order they look like
    five overwrites of each other.
    """
    ids = set(step.next_ids())
    sid = step.step_id
    if sid:
        ids |= {s.step_id for s in wf.steps
                if s.step_id and s.parent_key == sid}
    ids = {i for i in ids if i in by_id and i != sid}
    return by_id[ids.pop()] if len(ids) == 1 else None


def _write_runs(wf: Workflow) -> list:
    """Stretches of consecutive field writes with nothing between them.

    Each run is a list of steps that provably run back to back, so a value
    written in one and overwritten in a later one was never visible to
    anything. Any other step — a wait, a message, a branch, a task — ends the
    run, because any of them is something that could have read the value.
    """
    if not wf.has_wiring:
        if not _single_path(wf):
            return []
        runs, current = [], []
        for step in wf.steps:
            if _writes_fields(step):
                current.append(step)
            else:
                runs.append(current)
                current = []
        runs.append(current)
        return [r for r in runs if len(r) > 1]

    by_id = {s.step_id: s for s in wf.steps if s.step_id}
    linked: dict = {}
    for step in wf.steps:
        if not _writes_fields(step):
            continue
        nxt = _next_step(wf, step, by_id)
        if nxt is not None and _writes_fields(nxt) and nxt.step_id:
            linked[nxt.step_id] = step
    runs = []
    for step in wf.steps:
        if not _writes_fields(step) \
                or (step.step_id and step.step_id in linked):
            continue  # not the head of a run
        run, cur, seen = [], step, set()
        while cur is not None and _writes_fields(cur) \
                and cur.step_id not in seen:
            seen.add(cur.step_id)
            run.append(cur)
            cur = _next_step(wf, cur, by_id)
        if len(run) > 1:
            runs.append(run)
    return runs


@rule("GHL076", "Field set twice in a row, so the first value never exists",
      "medium", "dead_weight", "data", "waste")
def field_overwritten_before_anything_reads_it(acct: Account):
    """Two writes to one field with nothing in between them.

    The interesting part is what does NOT count. A field set to "In Sequence",
    then to "No Reply" three days later, is a design: the first value is live
    for the whole wait, and anything that reads the contact meanwhile sees it.
    The defect is the pair with nothing between them at all — no wait, no
    message, no branch, nothing that could observe the first value — because
    then the first write is a step that changes nothing that ever happens. It
    arrives two ways: an action duplicated during an edit, and a branch that
    was deleted with both of its arms left behind in a straight line.

    GHL047 owns the other half of this family, two WORKFLOWS writing one field.
    This one stays strictly inside a single workflow so the same field is never
    reported twice.
    """
    for wf in acct.published():
        for run in _write_runs(wf):
            yield from _scan_run(wf, run)


def _scan_run(wf: Workflow, run: list):
    """Findings for one stretch of back-to-back field writes."""
    # field key -> (step that wrote it, value)
    pending: dict = {}
    for step in run:
        for key, value, _ident in _field_writes(step):
            v = (value or "").strip()
            # An empty write is a deliberate clear, and a merge token is
            # not a value this file knows — neither can be compared, and
            # both mean the earlier value's fate is no longer readable.
            if not v or TOKEN.search(v):
                pending.pop(key, None)
                continue
            earlier = pending.get(key)
            pending[key] = (step, v)
            # Two assignments inside ONE action are not evidence of order:
            # a field list is a set of assignments and the file does not
            # say which the platform applies last. Same value twice is a
            # duplicated step, not a contradiction, and costs nothing.
            if not earlier or earlier[0] is step \
                    or earlier[1].lower() == v.lower():
                continue
            first_step, first = earlier
            yield _finding(
                "GHL076", "medium", wf,
                f"'{key}' is set to '{first}' and then '{v}' with "
                "nothing in between",
                f"Two steps write the same field one after the other — "
                f"'{first_step.name or first_step.type}' sets it to "
                f"'{first}', then '{step.name or step.type}' sets it to "
                f"'{v}' — with nothing in between: no wait, no message, "
                f"no branch. Nothing can read a value in that gap, so "
                f"'{first}' never reaches anybody. Every message that "
                "merges this field, every filter, and every person who "
                f"opens the contact sees '{v}'. One of the two steps is "
                "normally a leftover — an action duplicated during an "
                "edit, or half of a branch that was deleted — and either "
                f"way the build says contacts pass through '{first}' and "
                "none of them do.",
                "Decide which value is right and delete the other write. "
                "If the first one was meant to hold while something "
                "happened, put that step back between them — the wait, "
                "the call task, the message that was supposed to read it. "
                "Verify by running a test contact through and watching "
                "the field on the contact record: it should change once, "
                "not twice.",
                step=step.name or step.type,
                cost="Nothing goes out wrongly today; the bill arrives "
                     "later. Anything built on the value that never "
                     "lands — a smart list, a report, a second workflow "
                     "watching for it — matches nobody, and the person "
                     "who builds it will not see why.")
