# ghl-workflow-auditor

**Static analysis for GoHighLevel workflows.** Point it at an account export and it
finds the failures that only show up once a real customer is inside the sequence —
the ones that do not appear anywhere in the UI, because GoHighLevel considers them
valid configuration.

[![tests](https://github.com/richardrequena23/ghl-workflow-auditor/actions/workflows/ci.yml/badge.svg)](https://github.com/richardrequena23/ghl-workflow-auditor/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-lightgrey)
![rules](https://img.shields.io/badge/rules-27-19D3B0)
![license](https://img.shields.io/badge/license-MIT-green)

```
$ python -m ghlaudit account.json

Account health: 16/100  (F)   Customers are receiving the wrong messages right now.

13 workflows audited. 51 findings: 5 critical, 29 high, 10 medium, 7 low  [27 of 27 checks ran]

  Compliance        51/100  [############............]  9 findings
  Deliverability    86/100  [#####################...]  2 findings
  Routing           27/100  [######..................]  21 findings
  Hygiene           46/100  [###########.............]  13 findings
  Dead weight       85/100  [####################....]  6 findings

Fix in this order — ranked by what each one costs:
  1. [GHL019] Wait for an event with no timeout — 3 messages below it never send
  2. [GHL001] Appointment trigger fires on every status change
  3. [GHL002] Call trigger is not narrowed to missed calls
  4. [GHL014] Tag loop: Hot Lead Alert <-> Long Term Nurture
  5. [GHL020] 1 step points at a calendar that no longer exists
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
```

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
| `phoneNumbers` | SMS-capability context |
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
  "stats_window_days": 90
}
```

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
| GHL005 | medium | deliverability | Reactivation blast with no throttle |
| GHL006 | medium | hygiene | Webhook posting to a hardcoded URL instead of a custom value |
| GHL009 | medium | routing | Reply alerts with no once-per-conversation guard |
| GHL010 | medium | routing | Review/referral ask screened at enrollment but not at send time |
| GHL011 | medium | routing | Re-enrollment creating duplicate opportunities, or drifting from policy |
| GHL016 | medium | hygiene | Greeting that renders as "Hi ," when the name field is empty |
| GHL024 | medium | hygiene | A `\| default:` fallback written into an SMS, where fallbacks do not apply |
| GHL007 | low | hygiene | Deprecated `create_opportunity` / `update_opportunity` |
| GHL012 | low | hygiene | Sandbox or test workflow left published |
| GHL013 | low | compliance | Send window in account time, not the contact's — or wiped from the workflow |
| GHL018 | low | dead_weight | Tag-triggered workflow whose tag nothing in the account adds |
| GHL026 | low | dead_weight | Published workflow that nothing has enrolled in |

Severity means: **critical** — the account is texting customers something wrong right
now. **high** — it will misfire under normal use, not just at an edge. **medium** — it
will bite on scale, on handover, or on a bad day. **low** — correctness is fine,
maintenance or future-proofing is not.

Several rules escalate or downgrade themselves in context: GHL019 drops to `low` when
nothing sits below the wait, GHL021 drops to `low` when the branch is a terminal filter,
GHL015 escalates to `critical` when the two workflows are structurally identical (a
snapshot re-pushed onto a non-blank account), and GHL025 raises its severity only when
it can confirm the account-level unsubscribe default is actually off.

## The health score

```
damage    = 25 per critical + 12 per high + 5 per medium + 2 per low
tolerance = 8 points per published workflow
score     = 100 - (100 × damage / (damage + tolerance))
```

A saturating curve, chosen for three properties:

- **No single finding can fail an account.** One critical on an otherwise healthy
  account is a bad day, not an F. A scoring model that overreacts once gets ignored
  forever after.
- **It never reaches 0.** There is always a worse account, and 0 would claim otherwise.
- **It scales with size.** A sixty-workflow account has proportionally more surface, so
  it absorbs proportionally more findings before the grade moves. Twelve findings on
  sixty workflows is a well-run account; twelve on six is a fire.

Grades: A ≥ 90, B ≥ 80, C ≥ 70, D ≥ 60, F below. The same formula produces the five
category scores — **compliance**, **deliverability**, **routing**, **hygiene**,
**dead weight** — so they are comparable to each other and to the total.

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

Most of these checks would produce nonsense one workflow at a time. Four examples of why
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

## False positives are the point

A rule that fires on everything gets ignored, and then the report is worthless. Every
rule ships with a test that trips it **and** a test that must not trip it — **162 tests**
in [`tests/test_rules.py`](tests/test_rules.py), run against Python 3.9–3.13 on every
push. The shipped example account trips **all 27 rules**, and a test enforces that, so a
rule cannot rot into never firing without the suite noticing.

The calibration shows in the rules themselves: GHL017 (missing opt-out language) exempts
appointment-triggered sequences, because a booking confirmation is a conversation the
contact started. GHL018 (a tag trigger nothing feeds) ships as `low`, because forms,
bulk actions and humans also add tags — the finding asks a question rather than
pretending to certainty the data cannot support. GHL025 reports at `medium` when it
cannot see the account-level unsubscribe default and `high` only when it can confirm the
default is off.

Real false-positive classes found by running this against a live 19-workflow account and
fixed rather than tolerated: reply detection expressed as `Replied` / `No reply` branches
off a wait step (the way the UI actually builds it), and custom values whose display name
differs from their merge key.

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
