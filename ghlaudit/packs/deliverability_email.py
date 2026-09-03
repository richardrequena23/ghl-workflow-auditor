"""Email deliverability past the domain-verification line — GHL059-GHL064.

GHL025 asks the two questions everyone already knows to ask: is there a verified
sending domain, and is there an unsubscribe link. This pack is what still sinks
an email once both are handled — a From address nobody can authenticate, bounces
the account never suppresses, a footer with no postal address, a body that is one
image, a no-reply address swallowing the answers, and a subject line that reads
as spam sent from a domain with no reputation to spend.

None of these show up in GoHighLevel's reporting. The send succeeds, the stats
say delivered, and the message is in a junk folder or was refused at the door —
which is why "our emails just aren't getting opened" is almost never about the
copy.
"""

from __future__ import annotations

import json
import re

from ..model import URL, Account, Step, Workflow
from ..rules import Finding, Skip, _finding, rule


def _nk(key) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


def _under(node, keys: frozenset) -> list[str]:
    """Every string sitting under one of these keys, at any depth.

    Exports disagree about where an email's parts live — `meta.body`,
    `data.html`, `settings.emailBody` — so the key is what identifies the
    field, never its position. Anything that is not a string is ignored: a
    `body` that arrived as a list of blocks is not something this can read,
    and guessing at it is how a content check starts lying.
    """
    out: list[str] = []

    def walk(n, key=""):
        if isinstance(n, str):
            if _nk(key) in keys:
                out.append(n)
        elif isinstance(n, dict):
            for k, v in n.items():
                walk(v, k)
        elif isinstance(n, list):
            for v in n:
                walk(v, key)

    walk(node)
    return out


SUBJECT_KEYS = frozenset({"subject", "emailsubject", "subjectline"})
BODY_KEYS = frozenset({"body", "html", "htmlbody", "emailbody", "message",
                       "messagebody", "content", "text"})
FROM_KEYS = frozenset({"fromemail", "from", "sender", "senderemail",
                       "fromaddress", "fromemailaddress"})
REPLY_KEYS = frozenset({"replyto", "replytoemail", "replytoaddress",
                        "replyaddress"})

ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _addresses(step: Step, keys: frozenset) -> list[str]:
    """The email addresses declared under these keys, lowercased.

    A value holding a merge field is skipped: `{{ custom_values.from_email }}`
    resolves at send time to something this file does not contain, and judging
    a domain we cannot see would be a guess.
    """
    out: list[str] = []
    for value in _under(step.raw, keys):
        if "{{" in value:
            continue
        out.extend(m.group(0).lower() for m in ADDRESS.finditer(value))
    return out


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].strip().strip(".").lower()


def _org(host) -> str:
    """The registrable part of a hostname — near enough for DMARC alignment.

    Relaxed alignment is DMARC's default, and it only requires the
    organisational domains to match: mail from hello@acme.com signed by
    mail.acme.com aligns and must not be flagged. Without the public suffix
    list that is the last two labels, which over-collapses acme.co.uk to
    co.uk. The error runs in the safe direction — it makes the check miss a
    real defect rather than invent one.
    """
    parts = [p for p in str(host).lower().strip().strip(".").split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else ".".join(parts)


def _body_text(step: Step) -> str:
    return "\n".join(_under(step.raw, BODY_KEYS))


def _subject(step: Step) -> str:
    return " ".join(_under(step.raw, SUBJECT_KEYS)).strip()


# A postal address, in the two forms a footer writes it. The trailing group
# swallows the city/state/ZIP that follows the street line, because the same
# regex is used to REMOVE the address before measuring how much real copy an
# email has — leave the ZIP behind and a correctly built footer looks like body
# text.
STREET = re.compile(
    r"\b\d{1,6}\s+[\w.'-]+(?:\s+[\w.'-]+){0,4}\s+"
    r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|way|"
    r"ct|court|pl|place|pkwy|parkway|hwy|highway|ste|suite|unit|apt|fl|floor)"
    r"\b\.?(?:[^<>\n]{0,60}?\b\d{5}(?:-\d{4})?\b)?", re.I)
PO_BOX = re.compile(r"\bp\.?\s*o\.?\s*box\s*#?\s*\d+", re.I)
# GoHighLevel ships the location's address as a merge field, which is the
# correct way to write this footer — a workflow using it is compliant even
# though no street name appears anywhere in the export.
ADDRESS_MERGE = re.compile(
    r"\{\{\s*(?:location|account|company|business|agency)\.[\w.]*"
    r"(?:address|city|state|postal|zip)|"
    r"\{\{\s*custom_values\.\w*(?:address|mailing|postal)", re.I)

# Triggers whose mail is transactional by nature. CAN-SPAM's address and
# opt-out duties attach to COMMERCIAL mail; a receipt or a booking
# confirmation carries neither.
TRANSACTIONAL_TRIGGERS = ("appointment", "order", "payment", "invoice",
                          "form_submitted")

# Below this much body text the export is carrying a template reference rather
# than the email itself, and a footer nobody was shown cannot be reported
# missing. Sixty characters is roughly one sentence.
MIN_BODY_TEXT = 60


FOOTER_KEYS = frozenset({
    "address", "address1", "addressline1", "street", "city", "state",
    "postalcode", "postcode", "zip", "zipcode", "country", "footer",
    "fulladdress", "businessaddress", "mailingaddress"})


def _carries_footer(settings) -> bool:
    """Does this settings blob even have somewhere for an address to live?

    Keys only, at any depth — the VALUE may legitimately be empty, and an
    account that supplied `"address": ""` really has told us it has none.
    What matters is whether the export went looking.
    """
    found = False

    def walk(node):
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if _nk(k) in FOOTER_KEYS:
                    found = True
                    return
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(settings)
    return found


def _transactional(acct: Account, wf: Workflow, emails: list) -> bool:
    """Exempt from the marketing-only checks in this pack.

    Same read GHL025 makes — a single email off a transactional trigger —
    plus whatever the caller declared in config, which beats any heuristic
    here. A SEQUENCE off a transactional trigger is not transactional: three
    emails after a form fill is a nurture, whatever started it.
    """
    if acct.config.is_transactional(wf.name):
        return True
    if len(emails) != 1:
        return False
    return any(any(k in t.canonical_type() for k in TRANSACTIONAL_TRIGGERS)
               for t in wf.triggers)


@rule("GHL059", "Marketing email with no postal address in the footer",
      "high", "compliance", "email", "canspam")
def marketing_email_without_postal_address(acct: Account):
    """CAN-SPAM's other requirement — the one that gets forgotten.

    Every commercial email has to carry a valid physical postal address for
    the sender: a street address, a PO box, or a registered agent's. The
    unsubscribe link gets all the attention — GHL025 checks that one — and the
    address gets left out, because nothing in GoHighLevel asks for it and
    nothing warns when it is missing. A recipient who cannot tell who sent the
    mail does not go looking; they press the spam button, which costs the
    sending domain far more than the footer would have.

    Fires only when the export actually carries the copy. An email that
    renders from a template this file does not contain has a footer nobody
    here can see, and calling that missing would be a guess.

    And in GoHighLevel that is the NORMAL case, which is why this check skips
    rather than fires when the account's email configuration was not supplied.
    Builders put the postal address in the location-level footer or a shared
    template once, not into the body of every workflow step — so a rule reading
    only step bodies sees no address in an account that is perfectly compliant.
    Measured against a real account on Aug-26 2026, this fired `high` on nine
    of the eleven workflows that send any email: effectively all of them, which
    is the definition of a check nobody reads twice.

    The catalog's contract is that a check which cannot run yields a Skip,
    never a finding. Asserting a federal violation from the absence of data the
    export never carried is the worst possible way to break that contract.
    """
    inv = acct.inventory
    if not (inv.has("email_settings") or inv.has("templates")):
        yield Skip(
            rule="GHL059",
            title="Marketing email with no postal address in the footer",
            reason="The account's email settings and shared templates were not "
                   "in this export, and that is where a GoHighLevel account "
                   "normally carries its postal address. An address missing "
                   "from a workflow's own step bodies is not evidence that it "
                   "is missing from the email that actually goes out.",
            needs="emailSettings / emailTemplates in the input bundle — or "
                  "open Settings → Email Services and confirm the footer by eye",
            category="compliance")
        return

    # The footer was supplied. If the address lives there — which is where it
    # belongs, so that it stays right when the business moves — then every
    # email in the account inherits it and no workflow is in breach.
    account_footer = json.dumps(inv.email_settings, default=str) + " " + \
        json.dumps(inv.templates, default=str)
    if STREET.search(account_footer) or PO_BOX.search(account_footer) \
            or ADDRESS_MERGE.search(account_footer):
        return

    # No address in the supplied footer. Before calling that a violation, ask
    # whether what arrived could have contained one at all.
    #
    # An exporter that ships `emailSettings` holding only sending-domain keys
    # has told us nothing about the footer, but it flips this rule out of its
    # skip and into nine `high` findings on an account whose postal address is
    # sitting in its own location record. That is not hypothetical: it happened
    # the first time this repo's companion exporter learned to send an
    # inventory, and the findings were confidently wrong rather than absent.
    #
    # A half-supplied bucket is more dangerous than a missing one. Missing
    # yields an honest skip; half-supplied yields a confident wrong answer, and
    # this rule asserts a federal violation. So it needs positive evidence that
    # the footer was actually captured — an address-shaped key, or a template
    # with copy in it — and skips when it has neither.
    if not (_carries_footer(inv.email_settings) or inv.templates):
        yield Skip(
            rule="GHL059",
            title="Marketing email with no postal address in the footer",
            reason="An email configuration was supplied but it carries no "
                   "address fields and no template copy, so it cannot show "
                   "where this account's postal address is set. Absence of "
                   "the address from a partial export is not evidence that "
                   "the account has none.",
            needs="the location's address fields (address / city / state / "
                  "postalCode) or the shared email templates, in emailSettings",
            category="compliance")
        return

    for wf in acct.published():
        emails = wf.email_steps
        if not emails or _transactional(acct, wf, emails):
            continue
        body = "\n".join(_body_text(s) for s in emails)
        if len(re.sub(r"\s+", " ", body).strip()) < MIN_BODY_TEXT:
            continue
        if STREET.search(body) or PO_BOX.search(body) \
                or ADDRESS_MERGE.search(body):
            continue
        plural = "s" if len(emails) != 1 else ""
        yield _finding(
            "GHL059", "high", wf,
            f"{len(emails)} marketing email{plural} with no postal address "
            "anywhere in the body",
            "US law requires every commercial email to show the sender's "
            "valid physical postal address — a street address, a PO box, or "
            "a registered agent's address. None of these emails contains "
            "one, and none of them uses the merge field that would render "
            "one. It is a live violation on every send, and it is also the "
            "cheapest thing a recipient looks for when deciding whether the "
            "mail is legitimate: no address, no idea who this is, spam "
            "button.",
            "Add the business's postal address to the footer of each email — "
            "or better, drop in {{location.full_address}} so it stays correct "
            "when the business moves and so every future email inherits it. "
            "Send one to yourself and confirm the address renders rather than "
            "showing the raw merge field.",
            step=emails[0].name or emails[0].type,
            reach=len(emails),
            cost="A regulator-facing violation on every send, and a real "
                 "share of recipients marking mail as spam because they "
                 "cannot tell who it is from. The complaints are what "
                 "actually costs you — they follow the domain into every "
                 "other campaign.")


# Consumer mailbox domains, each with the DMARC policy it published when this
# rule was written (records checked 2026-08-26). The policy sets the severity:
# p=reject means receiving servers are being told to refuse the mail outright,
# p=none means it merely fails alignment and is scored as spoofing-shaped.
FREEMAIL = {
    "yahoo.com": "reject", "ymail.com": "reject", "aol.com": "reject",
    "comcast.net": "reject",
    "icloud.com": "quarantine", "me.com": "quarantine",
    "googlemail.com": "quarantine", "gmx.com": "quarantine",
    "gmail.com": "none", "outlook.com": "none", "hotmail.com": "none",
    "live.com": "none", "msn.com": "none",
}


@rule("GHL060", "Email sends from a domain this account cannot authenticate",
      "critical", "deliverability", "email", "dmarc")
def unauthenticated_from_address(acct: Account):
    """The From header, checked against what the account can actually sign.

    GoHighLevel signs outbound mail with ITS sending domain, so the From
    header decides whether DMARC aligns. Two ways it does not:

    A consumer mailbox in the From — hello@gmail.com, the owner's personal
    yahoo.com — can never align, because nobody can publish DNS for a domain
    they do not own. yahoo.com, ymail.com, aol.com and comcast.net publish
    p=reject: that mail is refused, not junked. icloud.com, me.com and
    googlemail.com publish p=quarantine. gmail.com, outlook.com and hotmail.com
    sit at p=none today, so they are not refused — but they still fail
    alignment on every send and Gmail's bulk-sender rules bar a gmail.com From
    for bulk mail anyway.

    Or a business domain that is simply outside this account's setup: verified
    mail.acme.com, sending as @acmegroup.com. The domain half only runs when at
    least one domain IS verified — on an account with none, GHL025 already says
    so once, account-wide, and repeating it per workflow is noise in the same
    report.
    """
    inv = acct.inventory
    verified = {_org(d.get("domain")) for d in inv.verified_email_domains
                if isinstance(d, dict) and d.get("domain")}
    listed = {_org(d.get("domain")) for d in inv.email_domains
              if isinstance(d, dict) and d.get("domain")}
    unchecked: list[str] = []

    for wf in acct.published():
        emails = wf.email_steps
        for step in emails:
            froms = _addresses(step, FROM_KEYS)
            if not froms:
                continue
            addr = froms[0]
            policy = FREEMAIL.get(_domain(addr))
            if policy:
                refused = policy != "none"
                yield _finding(
                    "GHL060", "critical" if refused else "high", wf,
                    f"Sends as {addr} — a mailbox this account cannot "
                    "authenticate",
                    f"This email goes out with {addr} in the From line. "
                    "GoHighLevel signs the message with its own sending "
                    "domain, so DMARC checks it against "
                    f"{_domain(addr)} — which nobody here can publish DNS "
                    "for. " + (
                        f"{_domain(addr)} publishes a DMARC policy of "
                        f"p={policy}, so receiving servers are instructed to "
                        + ("refuse this mail outright"
                           if policy == "reject" else
                           "drop it straight into the junk folder")
                        + ". Not sometimes — every message, every time."
                        if refused else
                        f"{_domain(addr)} publishes p=none, so the mail is "
                        "not refused outright, but it fails alignment on "
                        "every send and Gmail's own bulk-sender rules bar "
                        "using a consumer address as the From on bulk mail. "
                        "It is scored as spoofing-shaped by everything that "
                        "checks."),
                    "Send from a domain the business owns and has verified in "
                    "this location — a dedicated subdomain such as "
                    "mail.thebusiness.com — and put the personal address in "
                    "Reply-To if the owner wants the answers in that inbox. "
                    "Verify by sending one to a Gmail address and opening "
                    "Show Original: SPF, DKIM and DMARC must all read PASS.",
                    step=step.name or step.type,
                    reach=len(emails),
                    cost="Mail the account is billed for and nobody receives. "
                         "The sending report says delivered, the recipient "
                         "never saw it, and the lead reads as unresponsive.")
                break
            if not inv.has("email_domains"):
                unchecked.append(wf.name)
                break
            if not verified:
                continue  # GHL025 owns "this account has no verified domain"
            org = _org(_domain(addr))
            if org in verified:
                continue
            why = ("is listed in this account but was never verified"
                   if org in listed else
                   "is not on this account's sending-domain list at all")
            yield _finding(
                "GHL060", "high", wf,
                f"Sends as {addr}, and that domain {why}",
                f"This account has authenticated a sending domain, and this "
                f"workflow does not use it: the From address is {addr}, whose "
                f"domain {why}. No DKIM key was ever published for it, so the "
                "mail fails alignment exactly the way an unauthenticated "
                "domain does — while every other workflow in the account "
                "sends clean. That mix is worse than being uniformly "
                "unauthenticated, because the failures are invisible next to "
                "campaigns that work.",
                "Either move this workflow's From address onto the verified "
                "domain, or add and verify this domain in Settings > Email "
                "Services and complete its DNS records. Confirm with Show "
                "Original on a test send that DKIM reports the domain you "
                "expect.",
                step=step.name or step.type,
                reach=len(emails),
                cost="This workflow's mail is the only mail in the account "
                     "that fails authentication, so nobody notices — the "
                     "other campaigns look fine and this one quietly "
                     "underperforms them.")
            break

    if unchecked:
        names = ", ".join(sorted(set(unchecked))[:3])
        yield Skip(
            rule="GHL060",
            title="From address is on a domain of unknown authentication",
            reason=f"{len(set(unchecked))} published workflow(s) declare a "
                   f"From address on a business domain ({names}), and no "
                   "sending-domain list was supplied — whether those domains "
                   "are authenticated in this account lives in the email "
                   "settings and in DNS, not in the workflows. The consumer-"
                   "mailbox half of this check still ran.",
            needs="emailDomains in the input bundle (domain + verified)",
            category="deliverability")


# An email-event trigger, and the actions that make one mean something. A
# workflow that starts on a bounce and only sends a Slack alert has not
# suppressed anything — the address is still in every future audience.
BOUNCE_EVENT = re.compile(
    r"bounce|complain|spam[_ -]?report|abuse[_ -]?report|unsubscrib|"
    r"invalid[_ -]?email|email[_ -]?(?:invalid|error|fail)", re.I)
SUPPRESS_ACTION = re.compile(
    r"dnd|do[_ -]?not[_ -]?disturb|unsubscrib|opt[_ -]?out|tag|"
    r"remove|update[_ -]?contact|edit[_ -]?contact|set[_ -]?(?:field|custom)|"
    r"mark|suppress", re.I)

# One email in an account is a one-off. The third is the point at which the
# same address is being mailed repeatedly, which is when a dead one starts
# costing reputation instead of just failing.
MIN_ACCOUNT_EMAILS = 3


def _suppresses_bad_addresses(wf: Workflow) -> bool:
    listens = any(
        BOUNCE_EVENT.search(t.type) or BOUNCE_EVENT.search(t.name)
        or BOUNCE_EVENT.search(t.filter_blob())
        for t in wf.triggers)
    if not listens:
        return False
    return any(SUPPRESS_ACTION.search(s.type) or SUPPRESS_ACTION.search(s.name)
               for s in wf.steps)


@rule("GHL061", "Nothing in the account suppresses a bounce or a complaint",
      "high", "deliverability", "email", "reputation")
def no_bounce_suppression(acct: Account):
    """Dead addresses stay on the list forever, and the sender pays for it.

    A hard bounce means the mailbox does not exist. GoHighLevel records it and
    stops there — nothing tags the contact, nothing sets email DND, and the
    next campaign mails the same dead address again. Bounce rate never
    improves, because nothing is removing what causes it, and mailbox
    providers read a sender who keeps mailing addresses that do not exist as
    one working from a list nobody maintains. Spam complaints are the same
    shape: Gmail's bulk-sender rules ask senders to keep the reported spam
    rate under 0.3%, and an account that never sees its complaints cannot
    stay under anything.

    This reads the whole account, not one workflow — the suppression can live
    anywhere, and usually should live in exactly one place.
    """
    senders = [w for w in acct.published() if w.email_steps]
    total = sum(len(w.email_steps) for w in senders)
    if total < MIN_ACCOUNT_EMAILS:
        return
    for wf in acct.published():
        if _suppresses_bad_addresses(wf):
            return

    names = sorted(w.name for w in senders)
    shown = ", ".join(names[:3]) + (f", +{len(names) - 3} more"
                                    if len(names) > 3 else "")
    yield Finding(
        rule="GHL061", severity="high", workflow="(account)",
        step=shown, category="deliverability", reach=total,
        title=f"{len(senders)} workflows send email and nothing handles a "
              "bounce or a complaint",
        symptom=f"These workflows send {total} emails between them: {shown}. "
                "No published workflow in this account starts on an email "
                "bounce, a spam complaint or an unsubscribe, and nothing "
                "marks a bad address once one happens. So an address that "
                "hard-bounced in January is mailed again in February, and in "
                "March, and each of those sends tells the receiving provider "
                "that this sender does not clean its list. The account's own "
                "reporting will not show this getting worse — bounce rate "
                "looks like a fact about the list rather than something the "
                "system is choosing not to fix.",
        fix="Build one suppression workflow: trigger on Email Events filtered "
            "to Bounced, plus Complained and Unsubscribed, then tag the "
            "contact email-invalid and switch on DND for the email channel. "
            "Every campaign audience then filters that tag out. Verify by "
            "sending to a deliberately bad address and confirming the tag "
            "lands within a few minutes.",
        cost="Sender reputation decays quietly and takes every campaign in "
             "the account with it — the emails that DO have a live recipient "
             "are the ones that pay for the ones that never did.")


TAG = re.compile(r"<[^>]+>")
ENTITY = re.compile(r"&(?:[a-z]{2,10}|#\d{1,5});", re.I)
IMG = re.compile(r"<img\b[^>]*>", re.I)
ANCHOR = re.compile(r"<a\b[^>]*href", re.I)
ALT_TEXT = re.compile(r"\balt\s*=\s*[\"'][^\"']+[\"']", re.I)
# Footer furniture. It appears in every email and therefore cannot separate an
# image-only blast from a real message, so it comes out before the copy is
# measured — otherwise a correctly built compliance footer hides the very body
# this check exists to find.
BOILERPLATE = re.compile(
    r"unsubscribe|opt[- ]?out|manage\s+(?:your\s+)?preferences|"
    r"view\s+(?:this\s+)?in\s+(?:your\s+)?browser|you\s+(?:are|were)\s+"
    r"receiving\s+this|all\s+rights\s+reserved|privacy\s+policy|"
    r"sent\s+(?:to\s+you\s+)?by|©|\(c\)\s*20\d\d", re.I)

# Shorter than most subject lines. At that length there is nothing for an
# image-blocking client to show, nothing for a text-only client to render, and
# nothing for a content filter to weigh except the link.
MIN_VISIBLE_TEXT = 40


def _visible(body: str) -> str:
    """What a person would actually read, minus the footer furniture."""
    text = TAG.sub(" ", body)
    text = ENTITY.sub(" ", text)
    text = STREET.sub(" ", text)
    text = PO_BOX.sub(" ", text)
    text = BOILERPLATE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


@rule("GHL062", "Email body is an image or a link and almost no text",
      "medium", "deliverability", "email", "content")
def image_or_link_only_email(acct: Account):
    """The designed-in-Canva email: one picture, no words.

    Filters score what they can read, and an image is not text. A body that is
    a single hosted image with a link on it has nothing to score except the
    link, which is the exact shape of the mail filters are built to catch. It
    also fails the reader before it fails the filter: Outlook and Gmail both
    block remote images by default on a first-time sender, so the recipient
    opens a blank rectangle. With no alt text there is not even a caption to
    explain what they are looking at.

    Merge fields are counted as the text they occupy rather than stripped, so
    a body carrying real copy is never mistaken for an empty one.
    """
    for wf in acct.published():
        for step in wf.email_steps:
            body = _body_text(step)
            if not body.strip():
                continue
            visible = _visible(body)
            if len(visible) >= MIN_VISIBLE_TEXT:
                continue
            imgs = IMG.findall(body)
            if not (imgs or ANCHOR.search(body) or URL.search(body)):
                continue  # a short plain-text email is terse, not image-only
            unalt = [i for i in imgs if not ALT_TEXT.search(i)]
            if imgs:
                what = f"{len(imgs)} image{'s' if len(imgs) != 1 else ''}"
                if unalt:
                    what += (" with no alt text on "
                             + ("it" if len(imgs) == 1 else "any of them"))
            else:
                what = "a link"
            yield _finding(
                "GHL062", "medium", wf,
                f"Email body is {what} and {len(visible)} characters of text",
                f"This email is {what}, and almost nothing for anyone "
                "to read. Spam filters score the words in a message, so a "
                "body with no words gives them almost nothing to weigh, and "
                "the shape itself — picture, link, no text — is one they "
                "are specifically built to catch. Before that even matters, "
                "Gmail and Outlook block remote images by default from a "
                "sender the recipient has not corresponded with, so what "
                "opens is a blank rectangle"
                + (" with no caption." if imgs and unalt else "."),
                "Put the offer in real text — a headline and two or three "
                "sentences at minimum — and let the image support it rather "
                "than carry it. Give every image an alt attribute describing "
                "what it shows. Check it by opening the test send with images "
                "switched off: whatever is still readable is what most "
                "first-time recipients get.",
                step=step.name or step.type,
                reach=len(wf.email_steps),
                cost="A share of these land in spam, and of the ones that do "
                     "arrive, the recipients with images off see an empty "
                     "message. Both count as sent in the report.")
            break


NO_REPLY_LOCAL = re.compile(
    r"^(?:no[-._]?reply|do[-._]?not[-._]?reply|donotreply|noreply|"
    r"no[-._]?response|unmonitored|mailer[-._]?daemon)", re.I)
# Only an explicit invitation to answer counts. "Let me know" is a figure of
# speech; "just reply to this email" is an instruction the sender means.
REPLY_ASK = re.compile(
    r"\b(?:hit|click)\s+reply\b|"
    r"\breply\s+(?:to\s+this|to\s+me|back|here|directly|with|and\s+let)|"
    r"\brespond\s+(?:to\s+this|to\s+me|back|here|directly)|"
    r"\b(?:just|simply|please)\s+(?:reply|respond)"
    r"(?:\s+(?:to\s+this(?:\s+email)?|back|here|directly))?\b|"
    r"\bwrite\s+back\b|\bemail\s+me\s+back\b|"
    r"\blet\s+me\s+know\s+by\s+(?:reply|email)|"
    r"\bsend\s+me\s+a\s+(?:quick\s+)?(?:reply|note|email)\b", re.I)
# An SMS-style opt-out instruction pasted into an email is not a request for a
# conversation, and reading it as one would flag the footer of half the
# account.
OPT_OUT_ASK = re.compile(r"\breply\s+(?:with\s+)?[\"']?stop\b", re.I)


@rule("GHL063", "Asks for a reply, sends from an address that cannot take one",
      "high", "deliverability", "email", "replies")
def reply_ask_to_a_no_reply_address(acct: Account):
    """'Just reply to this email' — sent from noreply@.

    The lead does reply. Depending on how the no-reply mailbox was set up, it
    bounces back to them or it is silently discarded; either way the answer
    reaches nobody, and the lead concludes the business ignored them.

    It is a deliverability finding as much as a routing one: a reply is the
    strongest positive engagement signal a mailbox provider can see, and an
    address that cannot receive one throws that signal away on every send —
    while the recipients who try, and get a bounce, learn to treat this
    sender's mail as machine noise.

    From: noreply@ WITH a monitored Reply-To is the correct configuration and
    passes. The check follows where the reply would actually go.
    """
    for wf in acct.published():
        for step in wf.email_steps:
            target = _addresses(step, REPLY_KEYS) or _addresses(step, FROM_KEYS)
            if not target:
                continue
            addr = target[0]
            if not NO_REPLY_LOCAL.match(addr.split("@", 1)[0]):
                continue
            asked = OPT_OUT_ASK.sub(" ", _subject(step) + "\n" + _body_text(step))
            match = REPLY_ASK.search(asked)
            if not match:
                continue
            declared_reply_to = bool(_addresses(step, REPLY_KEYS))
            yield _finding(
                "GHL063", "high", wf,
                f"Asks the reader to reply, and replies go to {addr}",
                f"This email says \"{match.group(0).strip()}\" and the address "
                f"a reply would go to is {addr}"
                + (" (its Reply-To)." if declared_reply_to else
                   " (its From address, with no Reply-To set).")
                + " Anyone who does what the message asked gets a bounce, or "
                "gets nothing at all — the mailbox is not read. To the lead "
                "that is a business that asked a question and then ignored "
                "the answer, and it is the leads who engaged, the warmest "
                "ones in the sequence, who experience it.",
                "Point Reply-To at a mailbox somebody actually reads, and "
                "forward it into the CRM so the reply lands on the contact "
                "record. If the address genuinely cannot take mail, rewrite "
                "the call to action to a link or a booking page instead of a "
                "reply. Verify by replying to a test send and confirming it "
                "arrives.",
                step=step.name or step.type,
                reach=len(wf.email_steps),
                cost="Every lead who answers this email is lost silently. "
                     "They are the ones who were interested, and nothing in "
                     "the CRM will ever show they responded.")
            break


SPAM_PHRASE = re.compile(
    r"\b(?:100%\s*free|free\s+money|risk[- ]free|no\s+obligation|act\s+now|"
    r"limited\s+time\s+only|order\s+now|click\s+here\s+now|buy\s+now|"
    r"double\s+your|guaranteed\s+(?:income|results|approval)|"
    r"(?:make|earn)\s+\$|cash\s+bonus|congratulations,?\s+you|"
    r"you(?:'ve|\s+have)\s+been\s+selected|this\s+is\s+not\s+spam|"
    r"urgent\s+(?:action|response))", re.I)
CAPS_RUN = re.compile(r"\b[A-Z]{4,}\b")
# Words a legitimate subject line shouts without meaning to.
SAFE_CAPS = {"ASAP", "RSVP", "HVAC", "HIPAA", "EMEA", "APAC", "OSHA"}
STACKED_PUNCT = re.compile(r"!!|\?\?|!\?|\?!|\${3}|\*{3}")


def _listed(items: list) -> str:
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _subject_signals(subject: str):
    """(score, reasons). Two independent signals are the bar to fire."""
    score, reasons = 0, []
    letters = re.sub(r"[^A-Za-z]", "", subject)
    shouted = [w for w in CAPS_RUN.findall(subject) if w not in SAFE_CAPS]
    if len(letters) >= 8 and letters.isupper():
        # Nobody types a whole subject line in capitals by accident, so this
        # counts double on its own.
        score += 2
        reasons.append("the whole subject line is in capitals")
    elif shouted:
        score += 1
        reasons.append("shouted word" + ("s" if len(shouted) != 1 else "")
                       + " (" + ", ".join(shouted[:3]) + ")")
    if subject.count("!") >= 2 or STACKED_PUNCT.search(subject):
        score += 1
        reasons.append("stacked punctuation")
    phrase = SPAM_PHRASE.search(subject)
    if phrase:
        score += 1
        reasons.append(f"the phrase \"{phrase.group(0).strip()}\"")
    return score, reasons


def _sender_unauthenticated(acct: Account, step: Step):
    """True / False / None, where None means the account context is missing."""
    inv = acct.inventory
    if not inv.has("email_domains"):
        return None
    verified = {_org(d.get("domain")) for d in inv.verified_email_domains
                if isinstance(d, dict) and d.get("domain")}
    froms = _addresses(step, FROM_KEYS)
    if froms:
        return _org(_domain(froms[0])) not in verified
    return not verified


@rule("GHL064", "Spam-trigger subject line on an unauthenticated domain",
      "medium", "deliverability", "email", "content")
def spam_subject_from_unauthenticated_domain(acct: Account):
    """Content alone rarely decides it any more. Content plus no reputation does.

    A shouty subject from a domain with years of clean history is survivable —
    the sender has reputation to spend. The same subject from a domain the
    receiving server cannot authenticate has nothing behind it, and the
    filter has only the message itself to go on. That is why this fires on
    the COMBINATION and never on the subject alone: a keyword list on its own
    is a false-positive machine, and a report full of "your subject line
    contains the word FREE" is a report nobody finishes reading.

    Two independent signals are the bar — shouting, stacked punctuation, or a
    phrase off the short list. One is a style opinion; two is a pattern.
    """
    unchecked: list[str] = []
    for wf in acct.published():
        for step in wf.email_steps:
            subject = _subject(step)
            if not subject:
                continue
            score, reasons = _subject_signals(subject)
            if score < 2:
                continue
            state = _sender_unauthenticated(acct, step)
            if state is None:
                unchecked.append(wf.name)
                continue
            if not state:
                continue
            shown = (subject if len(subject) <= 60
                     else subject[:57].rstrip() + "...")
            yield _finding(
                "GHL064", "medium", wf,
                f"Subject \"{shown}\" sends from a domain nothing "
                "authenticates",
                "Two things stack here. The subject line carries "
                + _listed(reasons)
                + ", which is the pattern content filters were built on. And "
                "the domain it sends from is not authenticated in this "
                "account, so the receiving server has no sender reputation to "
                "weigh it against — the message is judged on its contents "
                "alone, and its contents look like a promotion nobody asked "
                "for. Either problem alone is usually survivable. Together "
                "they are why a campaign lands at 4% open rate and everyone "
                "blames the list.",
                "Fix the domain first — that is the half with the bigger "
                "effect and it applies to every campaign, not just this one. "
                "Then rewrite the subject as something a person would type: "
                "no capitalised words, one punctuation mark at most, and no "
                "offer language in the subject itself. Test with a small "
                "segment and compare inbox placement before and after.",
                step=step.name or step.type,
                reach=len(wf.email_steps),
                cost="A campaign that arrives in the junk folder still costs "
                     "the send, still shows as delivered, and quietly teaches "
                     "the mailbox provider to route the next one there too.")
            break

    if unchecked:
        names = ", ".join(sorted(set(unchecked))[:3])
        yield Skip(
            rule="GHL064",
            title="Spam-trigger subject line on an unauthenticated domain",
            reason=f"{len(set(unchecked))} published workflow(s) carry a "
                   f"subject line with two or more spam-trigger patterns "
                   f"({names}), and no sending-domain list was supplied — so "
                   "whether the domain behind them is authenticated, which is "
                   "the half that decides whether this matters, could not be "
                   "checked.",
            needs="emailDomains in the input bundle (domain + verified)",
            category="deliverability")
