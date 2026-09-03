# ghl-workflow-auditor

**Static analysis for GoHighLevel workflows.** Point it at an account export and it
finds the failures that only show up once a real customer is inside the sequence —
the ones that do not appear anywhere in the UI, because GoHighLevel considers them
valid configuration.

[![tests](https://github.com/richardrequena23/ghl-workflow-auditor/actions/workflows/ci.yml/badge.svg)](https://github.com/richardrequena23/ghl-workflow-auditor/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-lightgrey)
![rules](https://img.shields.io/badge/rules-102-19D3B0)
![license](https://img.shields.io/badge/license-MIT-green)

```
$ python -m ghlaudit account.json

Account health: 15/100  (F)   Customers are receiving the wrong messages right now. Stop and fix before the next campaign.
70 workflows audited. 102 root causes showing up in 221 places: 19 critical, 105 high, 69 medium, 28 low  [102 of 102 checks ran]

  Compliance        13/100  [###.....................]  42 findings
  Deliverability    18/100  [####....................]  16 findings
  Routing           16/100  [####....................]  101 findings
  Hygiene           16/100  [####....................]  43 findings
  Dead weight       18/100  [####....................]  19 findings

Fix in this order — ranked by what each one costs:
  1. [GHL019] Wait for an event with no timeout — 3 messages below it never send  (Speed to Lead - 5 Minute Response)
  2. [GHL028] 3 reminders keep sending after a cancel or reschedule  (4 places in 4 workflows)
  3. [GHL001] Appointment trigger fires on every status change  (No Show Recovery)
  4. [GHL002] Call trigger is not narrowed to missed calls  (Missed Call Text Back)
  5. [GHL014] Tag loop: Hot Lead Alert <-> Long Term Nurture  (Hot Lead Alert)
```

That is the real output of `python -m ghlaudit examples/broken-account.json` against
the example account in this repo — not an illustration.

`--html audit.html` writes the same audit as a single self-contained page you can
hand a client. No external assets, no network, readable printed to PDF.

## Why this exists

I have spent the last year running a CRM at volume — 20,000+ leads, 1,400+ booked
appointments — and then a few months building GoHighLevel systems. The bugs that cost
real money are almost never the ones that throw an error. They are configurations
GoHighLevel accepts happily and then executes exactly as written:

- An appointment trigger with no status filter, so **booking a call enrolls the contact
  in no-show recovery** and they get "sorry we missed you" thirty seconds after they
  booked.
- A wait that resumes on a reply, with no timeout. Everyone who does not reply is
  **parked in the workflow forever** — never messaged again, never marked unresponsive,
  and never reported anywhere. This is usually where "the leads that just went quiet"
  actually went.
- A quiet-hours window on a reminder ladder. A send window does not *skip* an action,
  it **holds** it — so the "1 hour before" text arrives the next morning, after the call.
- Six outbound messages and nothing listening, so the lead who replied "yes, call me"
  still receives the day-2 blast and gets tagged no-response on the way out.
- A webhook posting to a hardcoded URL. Clone that account into a client's and it does
  not break — it quietly sends their customer data to your endpoint, which is worse.
- Two workflows that trigger each other through tags. Each looks correct alone; together
  they enroll the same contact **in a loop**, and the builder shows one workflow at a
  time, so nothing on screen ever looks wrong.

Every rule in this tool is one of those. None of them are style opinions.

**The structural reason this has a market:** HighLevel's own in-builder error
highlighting covers two categories — integration issues and missing mandatory fields.
It does not check deleted references, empty branches, missing wait timeouts, or
cross-workflow interactions. And the platform **silently skips** a step it cannot
resolve rather than raising an error. That single decision is why all of this is
invisible, and why a static analyser can find it.

## Install

No dependencies. Python 3.9+.

```bash
git clone https://github.com/richardrequena23/ghl-workflow-auditor
cd ghl-workflow-auditor
python -m ghlaudit examples/broken-account.json
```

## Usage

```bash
python -m ghlaudit account.json                            # terminal report
python -m ghlaudit account.json --html audit.html          # + client-facing HTML
python -m ghlaudit account.json -f markdown -o audit.md    # handover doc
python -m ghlaudit account.json -f json | jq '.score'      # pipe it somewhere
python -m ghlaudit account.json --min-severity high        # only today's problems
python -m ghlaudit account.json --config client.json       # account-specific context
python -m ghlaudit account.json --rule GHL001 --rule GHL019
python -m ghlaudit account.json --fail-on critical         # exit 1 in CI
python -m ghlaudit --list-rules
python -m ghlaudit account.json --html audit.html \
    --account-name "Acme Roofing" --prepared-by "Your Name"
```

The HTML report is the client deliverable: a single self-contained file (inline CSS, no
external requests, print-clean) that opens with the health score, a computed executive
summary — findings tally, the worst category, and the single most expensive defect,
every clause derived from the data — then per-category grades, and the full findings
list ranked by cost. `--prepared-by` puts the auditor's name in the header and footer
(and nothing else brands the page); pass an empty string to omit it.

As a library:

```python
from ghlaudit import Account, AuditConfig, run_all, health, as_html

acct = Account.from_file("account.json", config=AuditConfig.from_file("client.json"))
findings, skips = run_all(acct)
score = health(findings, skips, len(acct.workflows))
print(score.score, score.grade)
open("audit.html", "w").write(as_html(findings, len(acct.workflows), skips))
```

## Input format

Any JSON containing workflows. The parser is deliberately permissive because GoHighLevel
hands back different shapes depending on where the data came from — an account export, an
API response, a snapshot bundle. All of these work:

```jsonc
[ {...}, {...} ]                                   // a bare list of workflows
{ "workflows": [...], "customValues": {...} }      // a bundle, with account context
{ "id": "...", "title": "...", "actions": [...] }  // alternative field names
```

A workflow needs a `name`, a `status`, some `steps`, and ideally its `triggers` and
`settings`. Field aliases (`_id`/`id`, `steps`/`templates`/`actions`/`nodes`,
`type`/`actionType`) are all resolved in [`ghlaudit/model.py`](ghlaudit/model.py).

### Account context unlocks the checks a workflow export cannot answer

Some failures are simply not visible in workflow JSON. A calendar ID that points at
nothing and a calendar ID that just was not exported look identical. So the bundle can
carry the rest of the account, and **any check that does not get what it needs reports
itself as skipped — never as a pass.**

| Bundle key | Unlocks |
|---|---|
| `customValues` | GHL008 placeholder / undefined merge fields, GHL023 empty values |
| `customFields` | GHL023 merge fields referencing a field the account does not have |
| `calendars`, `users`, `pipelines`, `forms`, `surveys`, `emailTemplates` | GHL020 dangling references, deactivated users |
| `emailDomains`, `emailSettings` | GHL025 unauthenticated sending domain, unsubscribe defaults |
| `phoneNumbers` | GHL031 SMS steps with no SMS-capable number, hard-coded from-numbers the location does not own |
| `stats` | GHL026 workflows nothing has enrolled in |
| `config` | the policy checks below (or pass them with `--config`) |

```jsonc
{
  "workflows": [...],
  "customValues": {"booking_link": "https://acme.com/book"},
  "customFields": [{"fieldKey": "contact.service_interest", "name": "Service Interest"}],
  "calendars": [{"id": "cal_1", "name": "Strategy Call"}],
  "users": [{"id": "usr_1", "name": "Dana", "active": true}],
  "emailDomains": [{"domain": "mail.acme.com", "verified": true}],
  "emailSettings": {"default_unsubscribe": true},
  "phoneNumbers": [{"number": "+15550109900", "sms": true}],
  "stats": {"wf_intake": {"enrollments": 0}}
}
```

### The config file — no account-specific values are baked in

Some things are not facts about the export, they are **decisions** someone made about
this account, and both values are legitimate. Re-entry ON is correct for a no-show
recovery (repeat no-shows must re-enroll) and wrong for a speed-to-lead (double-submit
protection). Only the person who designed it knows which. So it is supplied, not
guessed — and drift in either direction is a finding.

```json
{
  "owned_domains": ["acme.com"],
  "reentry_policy": {"Speed to Lead": false, "No Show Recovery": true},
  "send_window_policy": {
    "Long Term Nurture": {"start": "09:00", "end": "20:00", "timezone": "contact"},
    "Speed to Lead": null
  },
  "required_steps": {"Attribution": ["Push to reporting - booked"]},
  "transactional_workflows": ["Receipts"],
  "external_tags": ["job-complete"],
  "stats_window_days": 90
}
```

`external_tags` is the same idea pointed at a tag rather than a workflow. A tag trigger
fires on the tag arriving from anywhere, so "no workflow here adds it" is not proof the
sequence is dead — an ops team may apply it by hand on completion. GHL018 reports a
published, sending workflow behind an unfed tag at **high**, which is right when the
add-tag step was never built and wrong when a human applies it. Naming the tag here says
"we know where it comes from" and the check stops asking.

See [`examples/audit-config.json`](examples/audit-config.json). Every key is optional.
Workflow names match case- and whitespace-insensitively.

## The rule catalog

| Rule | Severity | Category | What it catches |
|---|---|---|---|
| GHL001 | critical | routing | Appointment trigger not filtered by status — booking fires no-show recovery |
| GHL002 | critical | routing | Call trigger not narrowed to missed calls — connected calls get texted back |
| GHL014 | critical | routing | Workflows re-triggering each other through tags — an enrollment loop |
| GHL019 | critical | routing | Wait that resumes on an event with no timeout — contacts park forever |
| GHL020 | critical | hygiene | A step pointing at a calendar, user, pipeline or template that no longer exists |
| GHL028 | critical | routing | Appointment reminders that keep firing after a cancel or reschedule |
| GHL031 | critical | deliverability | SMS steps with no SMS-capable number behind them — sends fail silently |
| GHL051 | critical | hygiene | Integration verifying only the legacy `X-WH-Signature` header — dead on Sep 1, 2026 |
| GHL003 | high | routing | Multi-touch sequence with nothing listening for a reply |
| GHL004 | high | routing | Quiet hours on a reminder ladder — the window *holds* the message |
| GHL008 | high | hygiene | Placeholder custom value, or a merge field with no field behind it |
| GHL015 | high | routing | Two workflows enrolling on an equivalent trigger — or the same workflow twice |
| GHL017 | high | compliance | SMS sequence with no opt-out language — what gets a number A2P-filtered |
| GHL021 | high | routing | An If/Else branch with nothing in it — contacts silently exit there |
| GHL022 | high | routing | A step link pointing at a node that is not in the workflow |
| GHL023 | high | hygiene | A merge field that renders blank: empty custom value, or a field the account lacks |
| GHL025 | high | compliance | Marketing email with no unsubscribe; unauthenticated sending domain |
| GHL027 | high | routing | A step the build manifest requires is missing from the workflow |
| GHL029 | high | compliance | SMS after a wait, with no send window anywhere — the 3am text |
| GHL032 | high | routing | Opportunity created with a pipeline chosen and no stage — files as a brand-new lead |
| GHL035 | high | hygiene | Webhook aimed at webhook.site, ngrok or localhost — or posting over plain http |
| GHL041 | high | routing | External call with no error branch — a failure is silently skipped |
| GHL042 | high | routing | Retry On Fail enabled while On Error is a Continue option — retries never happen |
| GHL045 | high | routing | Inbound webhook processed with no dedupe check — every retry runs it all again |
| GHL046 | high | routing | Go-To retry loop with no attempt counter — a poison record loops forever |
| GHL049 | high | routing | AI output branched on with no enum constraint — routing on model prose |
| GHL050 | high | routing | AI-generated text sent to a customer with no approval gate |
| GHL005 | medium | deliverability | Reactivation blast with no throttle |
| GHL006 | medium | hygiene | Webhook posting to a hardcoded URL instead of a custom value |
| GHL009 | medium | routing | Reply alerts with no once-per-conversation guard |
| GHL010 | medium | routing | Review/referral ask screened at enrollment but not at send time |
| GHL011 | medium | routing | Re-enrollment creating duplicate opportunities, or drifting from policy |
| GHL016 | medium | hygiene | Greeting that renders as "Hi ," when the name field is empty |
| GHL024 | medium | hygiene | A `\| default:` fallback written into an SMS, where fallbacks do not apply |
| GHL030 | medium | routing | Re-entry OFF on an appointment/invoice trigger — the setting HighLevel documents it ignores |
| GHL033 | medium | routing | "Thanks for your purchase" on the pre-payment trigger — declined cards get thanked |
| GHL039 | medium | routing | Several workflows each creating opportunities on one pipeline — duplicate deals |
| GHL040 | medium | routing | Workflows re-triggering each other through pipeline stages — the stage-write loop |
| GHL034 | medium | deliverability | Public link shortener in an SMS — a named driver of carrier filtering |
| GHL036 | medium | routing | Deprecated "Customer Booked Appointment" trigger — manual bookings never enter |
| GHL037 | medium | dead_weight | A finished build sitting in draft — saved is not published |
| GHL043 | medium | routing | n8n workflow with no error workflow attached — failures land in a list nobody reads |
| GHL044 | medium | routing | Create Contact where an upsert belonged — the classic duplicate-contact source |
| GHL047 | medium | routing | Several workflows writing the same contact field — a last-write-wins race |
| GHL048 | medium | hygiene | Scheduled workflow with no heartbeat — if it stops, nothing ever says so |
| GHL052 | medium | routing | Webhook handler answering a bad record with 4xx/5xx — which orders 12 redeliveries |
| GHL007 | low | hygiene | Deprecated `create_opportunity` / `update_opportunity` |
| GHL012 | low | hygiene | Sandbox or test workflow left published |
| GHL013 | low | compliance | Send window in account time, not the contact's — or wiped from the workflow |
| GHL038 | low | routing | Three-plus windowed waits stacked — every boundary drifts the sequence a day |
| GHL018 | low | dead_weight | Tag-triggered workflow whose tag nothing in the account adds |
| GHL026 | low | dead_weight | Published workflow that nothing has enrolled in |

Severity means: **critical** — the account is texting customers something wrong right
now. **high** — it will misfire under normal use, not just at an edge. **medium** — it
will bite on scale, on handover, or on a bad day. **low** — correctness is fine,
maintenance or future-proofing is not.

Several rules escalate or downgrade themselves in context: GHL019 drops to `low` when
nothing sits below the wait, GHL021 drops to `low` when the branch is a terminal filter,
GHL015 escalates to `critical` when the two workflows are structurally identical (a
snapshot re-pushed onto a non-blank account), GHL025 raises its severity only when
it can confirm the account-level unsubscribe default is actually off, and GHL028 drops
to a `low` confirm-this reminder when a dedicated cancellation workflow already cleans
up account-wide — the finding then asks whether this sequence is on its remove list.

## The health score

```
root cause = one rule, everywhere it fires on the account
damage     = for each root cause, severity weight × √(number of sites)
             weights: 25 critical, 12 high, 5 medium, 2 low
tolerance  = 8 points per published workflow, shared out across the categories
score      = 100 - (100 × damage / (damage + tolerance))
```

A saturating curve over root causes, chosen for four properties:

- **A habit is one defect, not N.** An account that never handles a webhook failure has
  made one decision, whether it is wrong in one workflow or in thirteen — and a person
  fixing it has one thing to do. Charging it thirteen times reports thirteen problems,
  and drops an account with one systemic habit below an account with a dozen unrelated
  defects. The repeats are still real, so they are priced at √sites: rising, with
  diminishing returns. The ordering that matters survives it — a high-severity habit
  across thirteen workflows (43) still outweighs one isolated critical (25).
- **No single finding can fail an account.** One critical on an otherwise healthy
  account is a bad day, not an F. A scoring model that overreacts once gets ignored
  forever after.
- **It never reaches 0.** There is always a worse account, and 0 would claim otherwise.
- **It scales with size.** A sixty-workflow account has proportionally more surface, so
  it absorbs proportionally more findings before the grade moves. Twelve findings on
  sixty workflows is a well-run account; twelve on six is a fire.

Grades: A ≥ 90, B ≥ 80, C ≥ 70, D ≥ 60, F below. The five category scores —
**compliance**, **deliverability**, **routing**, **hygiene**, **dead weight** — come off
the same curve and the same budget: each category gets the share of the tolerance that
matches the share of the catalog able to fire in it, so a ten-rule category is not
measured against a fifty-four-rule category's allowance.

That last part is a correction. Until Sep-2026 every category was scored against the
*entire* account's tolerance while the headline was scored against that same figure for
all five categories at once — the same allowance spent five times over. A real
thirteen-workflow account came back with Deliverability at 90 (A) and Hygiene at 50
sitting above a headline of 16 (F). Both numbers were computed correctly and they could
not both be describing the same account. A tool that argues with itself inside one table
does not get believed about either number.

**A category whose every check was skipped reports as "not assessed", never as 100.**
That distinction is the point: a clean report and an unrun report must not look the same.

### Findings are ranked by cost, not by rule number

The report opens with *fix in this order*. The ordering is severity weighted by blast
radius — how many outbound messages sit inside the affected workflow — because a defect
in a six-message sequence burns six times the goodwill of one in a single-touch
workflow. Every finding also carries a one-line `cost`: what it costs in money or lost
leads, written for the person who owns the business rather than the person who will fix
it.

## It reads the account, not just the workflow

Most of these checks would produce nonsense one workflow at a time. Five examples of why
the whole account is parsed first:

**GHL014 builds the tag graph nobody can see.** Workflow A adds a tag that triggers
workflow B; B adds a tag that re-triggers A. Each workflow, opened in the builder, is
correct. The auditor maps every add-tag step against every tag trigger across the
account, walks the graph, and reports each cycle once — with severity `critical` when
re-enrollment is on anywhere inside the loop (a contact cycles forever) and `high` when
it is off (one toggle away from forever).

**GHL003 knows about central reply handlers.** The mature pattern is *one* listener
workflow that pulls a contact out of every running sequence the moment they answer,
rather than bolting reply detection onto each sequence. Judged alone, every sequence in
such an account looks broken. So the auditor looks for a workflow with an inbound-message
trigger and a remove-from-workflow step; if it finds one, GHL003 drops from `high` to a
`low` reminder to confirm this sequence is named in the listener's remove list — which is
the thing people actually forget when they add a sequence later.

**GHL015 compares triggers canonically.** `contact_tag_added` and `contactTagAdded` are
one trigger written two ways, and `{"tag": "vip"}` and `{"field": "tag", "value": "vip"}`
are one filter written two ways. Comparing the raw export text reports a genuine
double-enrollment collision as two unrelated triggers, so the defect goes unreported.
The comparison normalises both sides first.

**GHL010 tracks position, not presence.** A review workflow that checks the complaint tag
once at the top and then waits seven days before asking is not protected. The rule finds
each send that has a wait between it and the last suppression check, which is how it
catches the *second* ask in a two-ask sequence while leaving the first one alone.

**GHL028 looks for the cancellation lane before flagging the reminders.** The correct
build often puts the exit in a *different* workflow — one Cancelled-status listener that
removes contacts from every reminder sequence. Judged alone, every reminder ladder in
such an account looks broken. So the auditor first looks for that cleanup workflow;
found, the finding drops to a `low` "confirm this sequence is on its remove list", which
is the thing people actually forget when they add a calendar later.

## False positives are the point

A rule that fires on everything gets ignored, and then the report is worthless. Every
rule ships with a test that trips it **and** a test that must not trip it — **1,080 tests**
in [`tests/`](tests/), run against Python 3.9–3.13 on every push. The shipped example
account trips **all 102 rules** with **zero checks skipped**, and two tests enforce
exactly that, so a rule cannot rot into never firing without the suite noticing.

Every rule also went through an adversarial pass whose only job was to find a *correct*
configuration it would wrongly flag, and to feed it malformed exports — `steps: null`,
a trigger that is a bare string, a settings value that is a list — because a traceback
mid-audit stops the other 101 checks. What that pass found got fixed and became a
regression test; that is most of why the suite is the size it is.

### The measured rate

A suite proves each rule fires on a fixture built to trip it. The same person wrote the
rule and the fixture, so that is a weak guarantee — it says nothing about how often a
rule misfires on a real export, which is the only number a client cares about.

So the catalog is run against a real 13-workflow account and **every finding is judged by
hand, one at a time, against the raw export**. The ledger is
[`calibration/verdicts.json`](calibration/verdicts.json): each finding keyed by content,
marked `real` or `false_positive`, with a note saying why. `scripts/precision_report.py
--summary` re-derives the number from it.

**Two numbers, and the difference between them is the point.**

| | judged | false positives | rate |
|---|---|---|---|
| **Live** — what the catalog emits today (`--summary --live`) | 92 | **0** | **0.0%** |
| Lifetime — every finding ever recorded, including ones no longer emitted | 188 | 39 | 20.7% |

The lifetime figure is a history of this catalog's mistakes, not a description of it. It
counts GHL007's twenty-two false positives, which were diagnosed, rewritten, and now fire
zero times. For an afternoon the summary rated only that history, so correcting the single
noisiest rule in the catalog moved the headline by exactly nothing — a quality metric that
cannot observe a fix landing is measuring the past. `--summary --live` re-runs the catalog
over each recorded export and rates only what it still emits.

Nothing is unjudged; an unjudged finding is never counted as a pass, for the same reason
the auditor reports a skipped check instead of silently omitting it. Retired findings are
reported rather than dropped, split by whether they were judged false — the narrowing
worked — or judged **real**, which means a rule went quiet on a true problem. That second
bucket is a regression check, and it earned itself: it caught GHL025 exempting a published
cold-chase from CAN-SPAM because a form submission looked transactional.

That number is worse than the one that stood here for an afternoon, and the difference is
the whole argument for measuring. The first pass scored 8.3% because GHL007's twenty-two
findings were marked `real` on the grounds that the *detection* was accurate, with a note
saying the rule's premise had not been checked against GoHighLevel's documentation. A
verdict with that note attached should never have been `real`. Checked properly, the
premise was backwards and all twenty-two were false. **An unverified claim is not a pass,
and recording it as one is how a measured number becomes a comfortable one.**

It moved again, 23.4% to 26.9%, and for a related reason. GHL049 matched its AI pattern
against a step's NAME as well as its type, so an If/Else called "Route by AI score" and
three tag steps called "Tag as ai-hot/warm/cold" were all read as model calls. Five more
findings that were marked `real` because the wording of each one was plausible, on steps
that call no model at all. The `ai_safety` pack had already worked this out — a matching
type is proof, a matching name is a hint needing a prompt or a model setting to back it —
but GHL049 predates that pack and never inherited the discipline. **The same lesson
arriving twice is what a duplicated helper buys you**; there is now one definition of an
AI step, in `rules.py`, and the pack imports it.

All 39 have been narrowed, and a re-run of the same account now produces **zero known
false positives** — 56 findings where there were 81. The five that mattered most, because
each put wrong advice in front of an account owner:

| Rule | What it wrongly claimed | Why it was wrong |
|---|---|---|
| GHL007 ×22 | `create_opportunity` is deprecated — *swap it* | backwards. GoHighLevel split the **combined** Create/Update action into two; `create_opportunity` is the replacement, and there is no `internal_` variant to swap to. The advice would have broken 21 working steps |
| GHL015 | two live campaigns were "identical copies" — *unpublish one* | compared structure and never read the copy; a shared skeleton is good practice |
| GHL003 | a workflow ignored replies through a "day-2 follow-up" | its two sends fired back to back; there was no wait for a reply to land in |
| GHL025 | an appointment confirmation needed an unsubscribe link | Google's sender guidance exempts reservation confirmations |
| GHL029 | an instant reply "goes out three days later, at any hour" | the wait was **one minute**, and the rule never read its duration |

GHL007 is the one worth dwelling on. The rescue tool had *refused* to act on it, because
GHL099 in the same catalog said the opposite — two rules disagreeing, neither checked
against the vendor's own docs, and the disagreement sat there instead of being resolved.
A catalog that contradicts itself in public loses the client's trust in all hundred rules,
not the two.

One account is a start and not a calibration set. The rate above is honest about what it
covers: **two exports of the same 13-workflow location, two days apart, judged by one
person.** That is one account's worth of evidence wearing two labels, and `--summary
--live` now prints that spread and says so rather than letting the clean number speak for
itself. A third export of the same account would not fix it; a different business would.
It is not a population statistic and no copy may present it as one.

**[The full catalog is in `docs/RULES.md`](docs/RULES.md)** — all 102 checks with the
symptom, the fix and what each one costs. It is generated from a real run against the
example account, so it cannot drift from the code.

The calibration shows in the rules themselves: GHL017 (missing opt-out language) exempts
appointment-triggered sequences, because a booking confirmation is a conversation the
contact started. GHL018 (a tag trigger nothing feeds) ships as `low`, because forms,
bulk actions and humans also add tags — the finding asks a question rather than
pretending to certainty the data cannot support. GHL025 reports at `medium` when it
cannot see the account-level unsubscribe default and `high` only when it can confirm the
default is off. GHL032 phrases its finding as "will land in stage *X* — confirm that is
intended", because the first stage may genuinely be the intent and the value of the rule
is in making the default visible. GHL031 checks a hard-coded from-number against the
location's list on **full digit strings** (a last-ten-digits match would equate a UK
mobile with a real US number), and says nothing at all about pool selections like
"default number", which carry no number to check. GHL033 is worded as a risk rather
than a defect, because the vendor's docs do not state whether the pre-payment trigger
fires on a declined card — only that a confirmation should not be built on a maybe.

Real false-positive classes found by running this against a live 19-workflow account and
fixed rather than tolerated: reply detection expressed as `Replied` / `No reply` branches
off a wait step (the way the UI actually builds it), and custom values whose display name
differs from their merge key.

The reliability rules (GHL041–GHL052) are calibrated the same way. GHL042 and GHL043
read settings only n8n-style exports carry, so a GoHighLevel-native workflow — which
declares neither a retry toggle nor an error-workflow slot — is never held to them.
GHL044 and GHL047 are worded as risks to confirm, not verdicts: whether a create
duplicates depends on the location's Allow Duplicate Contacts setting, and two writers
on one field are legitimate when their triggers are provably exclusive — neither is
decidable from the export. GHL048 stays quiet about any scheduled workflow that carries
a webhook call, because whether that call is a heartbeat is not knowable statically.
GHL050 treats a manual send step as its own approval gate — a human releases it — and
flags only automatic sends of AI-merged copy. GHL051 says nothing when both signature
headers appear together, which reads as a migration already in hand.

## Known limits

- **It reads configuration, not history.** It cannot tell you that a workflow *did*
  misfire, only that it will. A tag trigger that never fires because contacts already
  carried the tag looks perfectly correct here — that difference lives in contact
  history, not in the workflow.
- **Branch topology is read linearly** where an export flattens it. Deeply nested
  conditional trees are analysed by step order, which is right for the checks here but
  would not be enough for reachability analysis. GHL021 reads branches only where the
  export carries their children inline; GHL022 needs an export with node ids and links,
  and says so rather than passing when it does not get one.
- **Some real, damaging failures are not statically detectable at all**, and the report
  says so in its own output: async race conditions between a filter and an integration
  write, contacts dropped by restructuring a live workflow, integration token expiry,
  A2P campaign status, DNS-level email authentication, and whether the copy is any good.
- **There is no fetcher included.** Export your workflows however you like and hand it
  JSON — keeping credentials out of this repo entirely is deliberate. Note that
  GoHighLevel's public API `GET /workflows/` returns only metadata (id, name, status,
  version, timestamps) and **no steps**, so node-level JSON has to come from a snapshot,
  an export, or an authenticated session.
- **One documented behaviour is marked unverified in the source.** GHL024 flags a
  `| default:` fallback filter inside an SMS. HighLevel documents fallback values as
  supported in email only, so the safety net the author thinks they have is not there —
  but whether SMS renders the filter literally or silently drops the fallback is not
  documented anywhere I could find and I have not tested it on a live send. The finding
  says exactly that rather than picking one.

## Tests

```bash
python -m unittest discover -s tests -v     # no dependencies
python -m pytest -q                          # if you have it
```

## License

MIT.
