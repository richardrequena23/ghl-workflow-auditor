# ghl-workflow-auditor

**Static analysis for GoHighLevel workflows.** Point it at an account export and it
finds the failures that only show up once a real customer is inside the sequence —
the ones that do not appear anywhere in the UI, because GoHighLevel considers them
valid configuration.

[![tests](https://github.com/richardrequena23/ghl-workflow-auditor/actions/workflows/ci.yml/badge.svg)](https://github.com/richardrequena23/ghl-workflow-auditor/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)

```
$ python -m ghlaudit account.json --min-severity high

19 workflows audited. 3 findings: 1 critical, 2 high

Hot Lead Alert
--------------
!! [GHL014] Tag loop: Hot Lead Alert <-> Long Term Nurture
     why it matters: Each workflow in this chain adds a tag that enrolls the next one,
     and the chain closes back on itself. Re-enrollment is ON inside the loop, so one
     contact cycles through it forever — messages, opportunities and alerts included —
     until someone notices in the conversation feed.
     fix: Break the cycle at its weakest link: remove the add-tag step, narrow the
     trigger, or have each workflow remove its own trigger tag as its first step so a
     lap cannot restart.
```

## Why this exists

I have spent the last year running a CRM at volume — 20,000+ leads, 1,400+ booked
appointments — and then a few months building GoHighLevel systems. The bugs that cost
real money are almost never the ones that throw an error. They are configurations
GoHighLevel accepts happily and then executes exactly as written:

- An appointment trigger with no status filter, so **booking a call enrolls the contact
  in no-show recovery** and they get "sorry we missed you" thirty seconds after they
  booked.
- A call-status trigger with no filter, so the customer you just spoke to for ten
  minutes receives the missed-call text-back.
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

## Install

No dependencies. Python 3.9+.

```bash
git clone https://github.com/richardrequena23/ghl-workflow-auditor
cd ghl-workflow-auditor
python -m ghlaudit examples/broken-account.json
```

## Usage

```bash
python -m ghlaudit account.json                      # human-readable, grouped by workflow
python -m ghlaudit account.json -f markdown -o audit.md   # a client-ready report
python -m ghlaudit account.json -f json | jq '.counts'    # pipe it somewhere
python -m ghlaudit account.json --min-severity high       # only what needs fixing today
python -m ghlaudit account.json --rule GHL001 --rule GHL004
python -m ghlaudit account.json --fail-on critical        # exit 1 in CI
python -m ghlaudit --list-rules
```

As a library:

```python
from ghlaudit import Account, run, as_markdown

acct = Account.from_file("account.json")
findings = run(acct, min_severity="medium")
print(as_markdown(findings, len(acct.workflows)))
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

Supplying `customValues` unlocks the placeholder and dangling-merge-field checks. Custom
values are matched by slug, so the display name `Integration Webhook URL` correctly
resolves `{{ custom_values.integration_webhook_url }}`.

## The rule catalog

| Rule | Severity | What it catches |
|---|---|---|
| GHL001 | critical | Appointment trigger not filtered by status — booking fires no-show recovery |
| GHL002 | critical | Call trigger not narrowed to missed calls — connected calls get texted back |
| GHL014 | critical | Workflows re-triggering each other through tags — an enrollment loop |
| GHL003 | high | Multi-touch sequence with nothing listening for a reply |
| GHL004 | high | Quiet hours on a reminder ladder — the window *holds* the message |
| GHL008 | high | Placeholder custom value, or a merge field with no field behind it |
| GHL015 | high | Two workflows enrolling on the identical trigger — a double-message collision |
| GHL017 | high | SMS sequence with no opt-out language — what gets a number A2P-filtered |
| GHL005 | medium | Reactivation blast with no throttle |
| GHL006 | medium | Webhook posting to a hardcoded URL instead of a custom value |
| GHL009 | medium | Reply alerts with no once-per-conversation guard |
| GHL010 | medium | Review/referral ask screened at enrollment but not at send time |
| GHL011 | medium | Re-enrollment on a workflow that creates opportunities |
| GHL016 | medium | Greeting that renders as "Hi ," when the name field is empty |
| GHL007 | low | Deprecated `create_opportunity` / `update_opportunity` |
| GHL012 | low | Sandbox or test workflow left published |
| GHL013 | low | Send window evaluated in account time, not the contact's |
| GHL018 | low | Tag-triggered workflow whose tag nothing in the account adds |

Severity means: **critical** — the account is texting customers something wrong right
now. **high** — it will misfire under normal use, not just at an edge. **medium** — it
will bite on scale, on handover, or on a bad day. **low** — correctness is fine,
maintenance or future-proofing is not.

## It reads the account, not just the workflow

Most of these checks would produce nonsense one workflow at a time. Three examples of why
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

**GHL010 tracks position, not presence.** A review workflow that checks the complaint tag
once at the top and then waits seven days before asking is not protected. The rule finds
each send that has a wait between it and the last suppression check, which is how it
catches the *second* ask in a two-ask sequence while leaving the first one alone.

## False positives are the point

A rule that fires on everything gets ignored, and then the report is worthless. Every
rule ships with a test that trips it **and** a test that must not trip it — 61 tests in
[`tests/test_rules.py`](tests/test_rules.py), run against Python 3.9–3.13 on every push.

The calibration shows in the rules themselves: GHL017 (missing opt-out language) exempts
appointment-triggered sequences, because a booking confirmation is a conversation the
contact started. GHL018 (a tag trigger nothing feeds) ships as `low`, because forms,
bulk actions and humans also add tags — the finding asks a question rather than
pretending to certainty the data cannot support.

Two real false-positive classes were found by running this against a live 19-workflow
account and fixed rather than tolerated: reply detection expressed as `Replied` /
`No reply` branches off a wait step (the way the UI actually builds it), and custom
values whose display name differs from their merge key.

## Known limits

- It reads configuration, not history. It cannot tell you that a workflow *did* misfire,
  only that it will.
- Branch topology is read linearly. Deeply nested conditional trees are analysed by step
  order, which is right for the checks here but would not be enough for reachability
  analysis.
- There is no fetcher included. Export your workflows however you like and hand it JSON —
  keeping credentials out of this repo entirely is deliberate.

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT.
