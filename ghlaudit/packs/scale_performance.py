"""Scale and performance — what this account does at ten times the volume.

Every check here is about a build that is fine at one contact an hour and falls
over at a hundred: steps wired in a circle with nothing slowing them down, the
same lookup run three times for one contact, a retry ladder that puts the whole
backlog on a rate-limited endpoint at the same instant, a month-long sequence a
contact can be inside four times at once, two workflows that enroll each other,
and a decision tree with more paths than anybody has ever walked. GHL005 covers
the obvious one, a bulk campaign with no throttle; these are the ones a test
contact cannot show you, because a test contact goes round the loop once, on an
API nobody else is calling, on the day the endpoint is up.
"""

from __future__ import annotations

import re

from ..model import Account, Step, Workflow, _first
from ..rules import Finding, Skip, _finding, rule


def _nk(key) -> str:
    """targetStepId, target_step_id and TARGETSTEPID are one key."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _scalars(node, key=""):
    """Every (key, scalar) pair in a structure, however deeply nested.

    Recursive because the settings that matter here are rarely at the top: n8n
    keeps the method and URL under `parameters`, and GoHighLevel keeps a wait's
    length under a nested `startAfter`.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _scalars(v, k)
    elif isinstance(node, list):
        for item in node:
            yield from _scalars(item, key)
    elif isinstance(node, (str, int, float, bool)):
        yield key, node


def _live(acct: Account):
    """The workflows this pack applies to.

    Published, plus anything from an export that carries no status at all — an
    n8n bundle has no publish state, and reading an absent status as "not live"
    would exempt every n8n workflow from the whole pack. An explicit draft is
    left alone: it is not running, so it is not carrying volume.
    """
    for wf in acct.workflows:
        if wf.published or _nk(wf.status) in ("unknown", ""):
            yield wf


# --------------------------------------------------------------------------
# How long a step parks a contact
# --------------------------------------------------------------------------

# Minutes per unit. Months are the calendar-average 30 days: nothing here turns
# on the difference between 43,200 and 44,640 minutes.
_UNIT = {"second": 1 / 60.0, "sec": 1 / 60.0, "s": 1 / 60.0,
         "minute": 1.0, "min": 1.0, "m": 1.0,
         "hour": 60.0, "hr": 60.0, "h": 60.0,
         "day": 1440.0, "d": 1440.0,
         "week": 10080.0, "wk": 10080.0, "w": 10080.0,
         "month": 43200.0}

_DURATION = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|"
    r"weeks?|months?)\b", re.I)

_NUMBER_KEYS = {"value", "amount", "number", "delay", "duration", "time",
                "interval", "quantity", "qty", "waitfor", "startafter"}
_UNIT_KEYS = {"unit", "units", "delayunit", "waitunit", "durationunit",
              "period", "type", "frequency", "granularity"}


def _unit_of(word) -> float | None:
    return _UNIT.get(_nk(word).rstrip("s") or _nk(word))


def _from_text(text: str) -> float | None:
    match = _DURATION.search(text)
    if not match:
        return None
    return float(match.group(1)) * (_unit_of(match.group(2)) or 0.0)


def _wait_minutes(step: Step) -> float | None:
    """How long this step parks a contact in minutes, or None if unstated.

    Waits are written half a dozen ways across exports: "3 days", {"value": 3,
    "unit": "days"}, {"type": "days", "value": 3} (the hybrid wait's
    startAfter), {"days": 3}. The first parseable one wins, walked in the
    order the export wrote its keys, so the answer is deterministic. None is
    NOT zero — "wait until they reply" has no length, and every rule here
    treats an unknown length as a real pause rather than guessing it away.
    """
    cfg = step.config()
    if not isinstance(cfg, dict):
        return None

    def walk(node):
        if isinstance(node, dict):
            number, unit = None, None
            for k, v in node.items():
                own = _unit_of(k)
                numeric = isinstance(v, (int, float)) \
                    and not isinstance(v, bool)
                if own and numeric:
                    return float(v) * own                  # {"days": 3}
                if _nk(k) in _NUMBER_KEYS:
                    if isinstance(v, str):
                        found = _from_text(v)              # {"delay": "3 days"}
                        if found is not None:
                            return found
                        if v.strip().replace(".", "", 1).isdigit():
                            number = float(v)
                    elif numeric:
                        number = float(v)
                if _nk(k) in _UNIT_KEYS and isinstance(v, str):
                    unit = _unit_of(v) or unit
            if number is not None and unit is not None:
                return number * unit                       # {"value", "unit"}
            for v in node.values():
                found = walk(v)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        elif isinstance(node, str):
            return _from_text(node)
        return None

    return walk(cfg)


# The model knows GoHighLevel's three wait types by name. A namespaced one —
# n8n writes its Wait node as "n8n-nodes-base.wait" — is not in that set and
# would read as a step that does not stop anybody, which is how a correctly
# built retry ladder gets reported as a runaway loop.
PAUSE_TYPE = re.compile(r"wait|delay|sleep|drip|snooze|pause", re.I)


def _is_pause(step: Step) -> bool:
    """Does this step actually stop the contact here?

    A wait whose length cannot be read still counts. Being generous is the
    right way round: the expensive mistake is calling a real wait "no pause",
    and the only thing that disqualifies one is an explicit zero.
    """
    if step.is_wait or PAUSE_TYPE.search(step.type):
        return _wait_minutes(step) != 0
    return False


def _human(minutes: float) -> str:
    for size, unit in ((1440.0, "day"), (60.0, "hour"), (1.0, "minute")):
        if minutes >= size:
            count = minutes / size
            count = int(count) if abs(count - round(count)) < 0.01 \
                else round(count, 1)
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{round(minutes * 60)} seconds"


# --------------------------------------------------------------------------
# Outbound calls
# --------------------------------------------------------------------------

CALL_TYPE = re.compile(r"webhook|http|api|request", re.I)

URL_KEYS = ("url", "uri", "endpoint", "webhookurl", "requesturl", "targeturl",
            "hookurl", "apiurl")


def _endpoint(step: Step) -> str:
    """The destination this step calls, exactly as the export wrote it.

    Deliberately not run through a URL regex: a real build points at
    "{{ custom_values.api_base }}/contacts", which is a perfectly good
    destination and matches no URL pattern. Comparing the raw strings is what
    lets two calls to the same merge-built endpoint be recognised as the same
    call.
    """
    for key, value in _scalars(step.raw):
        if _nk(key) in URL_KEYS and isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_call(step: Step) -> bool:
    """An outbound call. The destination is what separates it from a TRIGGER:
    n8n's inbound Webhook node declares a method and a path, never a URL."""
    return bool(CALL_TYPE.search(step.type)) and bool(_endpoint(step))


def _method(step: Step) -> str:
    for key, value in _scalars(step.raw):
        if _nk(key) in ("method", "httpmethod", "requestmethod", "verb") \
                and isinstance(value, str):
            return value.strip().lower()
    return ""


def _destination(step: Step) -> str:
    """The endpoint normalised for comparison — spacing and case only.

    "{{ contact.id }}" and "{{contact.id}}" are the same merge field, and a
    trailing slash is the same resource. Nothing else is touched: a different
    query string is a different call and must not collapse into this one.
    """
    return re.sub(r"\s+", "", _endpoint(step).lower()).rstrip("/")


# --------------------------------------------------------------------------
# The workflow as a graph
# --------------------------------------------------------------------------

# A goto/jump construct IS the whole step type — anything after the "to" is a
# structural suffix, never a product name. Matching "goto" as a substring
# reads n8n's goToWebinar node as a loop.
GOTO_LEAF = re.compile(
    r"^go[-_ ]?to(?:[-_ ]?(?:step|action|node|event|workflow))?$", re.I)

TARGET_KEYS = {"targetstepid", "targetstep", "targetstepname", "targetnodeid",
               "targetnode", "targetid", "targetname", "gotostepid", "gotostep",
               "jumpto", "jumptostep", "destination", "target"}


def _is_goto(step: Step) -> bool:
    leaf = re.split(r"[./:]", str(step.type or ""))[-1].strip()
    return bool(GOTO_LEAF.match(leaf))


def _goto_target(step: Step, by_id: dict, by_name: dict):
    """Which step this jump lands on, by id or by name, or None.

    Both, because both are in the wild: the builder exports a node id, and a
    hand-written or migrated export names the step instead.
    """
    for key, value in _scalars(step.raw):
        if _nk(key) not in TARGET_KEYS or not isinstance(value, str):
            continue
        target = value.strip()
        if target in by_id:
            return by_id[target]
        if target.lower() in by_name:
            return by_name[target.lower()]
    return None


def _graph(wf: Workflow) -> dict:
    """{step index: the steps it can hand the contact to}.

    Two export shapes, one graph. A wired export (ids plus next/parentKey) is
    read as written. A flat ordered list carries no links because it does not
    need any — it runs top to bottom — so the edges are the order itself.
    Mixing the two would be wrong in both directions: inventing sequential
    edges inside a branching export creates loops that are not there, and a
    flat list with a Go-To in it has a genuine loop that only the implicit
    edges can show.

    Indices rather than ids, because half the exports in the wild carry ids on
    some steps and not others, and a graph keyed on ids silently drops the rest.
    """
    steps = wf.steps
    edges: dict = {i: set() for i in range(len(steps))}
    by_id: dict = {}
    by_name: dict = {}
    for i, step in enumerate(steps):
        sid = step.step_id
        if sid and sid not in by_id:
            by_id[sid] = i
        name = (step.name or "").strip().lower()
        if name and name not in by_name:
            by_name[name] = i

    wired = bool(by_id) and any(s.next_ids() or s.parent_key for s in steps)
    if wired:
        for i, step in enumerate(steps):
            for target in step.next_ids():
                if target in by_id:
                    edges[i].add(by_id[target])
            key = step.parent_key
            if not key:
                continue
            # GoHighLevel writes a branch child's parentKey as
            # "<parentId>-<branchName>", so this is a longest-prefix match: with
            # ids "s1" and "s1-yes" both in the file, "s1-yes-2" belongs to the
            # longer one.
            best = ""
            for sid in by_id:
                if key != sid and not key.startswith(sid + "-"):
                    continue
                if len(sid) > len(best):
                    best = sid
            if best:
                edges[by_id[best]].add(i)
    else:
        for i in range(len(steps) - 1):
            edges[i].add(i + 1)

    for i, step in enumerate(steps):
        if _is_goto(step):
            target = _goto_target(step, by_id, by_name)
            if target is not None:
                edges[i].add(target)
    return edges


def _loops(edges: dict) -> list:
    """Every group of steps that can all reach each other — the cycles.

    Tarjan's algorithm, written iteratively: a real export runs to hundreds of
    steps, and a RecursionError in the middle of an audit stops the other
    ninety-nine checks from ever running.
    """
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    found: list = []
    counter = 0

    for root in sorted(edges):
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(sorted(edges[root])))]
        while work:
            node, children = work[-1]
            descended = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                group = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == node:
                        break
                # A single step is only a loop if it points at itself.
                if len(group) > 1 or node in edges.get(node, ()):
                    found.append(sorted(group))
    return found


def _reaches_without_pause(edges: dict, steps: list, src, dst) -> bool:
    """Can the contact get from one step to the other without stopping?

    The walk refuses to pass THROUGH a wait, which is what makes it mean "on
    the same pass". Two identical calls with a wait between them are a poll —
    asking again after a pause is the entire point of one — and a poll must
    never be reported as a duplicate lookup.
    """
    seen = {src}
    frontier = [src]
    while frontier:
        node = frontier.pop()
        for nxt in edges.get(node, ()):
            if nxt == dst:
                return True
            if nxt in seen or nxt >= len(steps) or _is_pause(steps[nxt]):
                continue
            seen.add(nxt)
            frontier.append(nxt)
    return False


def _longest_park(steps: list, edges: dict) -> float:
    """The most wait time on any one path through the workflow, in minutes.

    A path total, not the sum of every wait in the file: two branches with a
    two-day wait each park a contact for two days, not four, and reporting
    four would be the kind of inflated number that gets a whole report
    dismissed. A cycle is counted as a single lap — the graph says a lap can
    repeat, but not how often.
    """
    own = [(_wait_minutes(s) or 0.0) if _is_pause(s) else 0.0 for s in steps]
    memo: dict = {}

    for start in range(len(steps)):
        if start in memo:
            continue
        work = [(start, iter(sorted(edges.get(start, ()))))]
        on_path = {start}
        while work:
            node, children = work[-1]
            nxt = next(children, None)
            if nxt is None:
                work.pop()
                on_path.discard(node)
                downstream = max(
                    (memo[c] for c in edges.get(node, ()) if c in memo),
                    default=0.0)
                memo[node] = own[node] + downstream
                continue
            if nxt in memo or nxt in on_path or nxt >= len(steps):
                continue
            on_path.add(nxt)
            work.append((nxt, iter(sorted(edges.get(nxt, ())))))
    return max(memo.values(), default=0.0)


# --------------------------------------------------------------------------
# GHL089 — a loop with nothing slowing it down
# --------------------------------------------------------------------------

@rule("GHL089", "Loop in the workflow with no pause in it", "critical",
      "routing", "scale", "loops")
def unpaused_loop(acct: Account):
    """Steps wired in a circle with no wait anywhere on the lap.

    This is the graph read properly, not a keyword: the cycle can be a Go-To
    jumping back, a branch child wired to an earlier node, or a step pointing
    at itself, and it is found the same way in all three cases. GHL046 asks a
    different question — whether a Go-To ladder counts its attempts — and a
    ladder that waits between laps is a legitimate build it is right to leave
    alone. This one only fires when there is NO pause on the lap at all, which
    is the difference between a retry every two minutes and a loop that runs
    as fast as the platform will execute it. Nothing about it is visible on a
    test contact who happens to succeed on the first pass.
    """
    for wf in _live(acct):
        edges = _graph(wf)
        for group in _loops(edges):
            steps = [wf.steps[i] for i in group]
            if any(_is_pause(s) for s in steps):
                continue
            sends = [s for s in steps if s.is_outbound]
            names = ", ".join((s.name or s.type) for s in steps[:4])
            yield _finding(
                "GHL089", "critical" if sends else "high", wf,
                f"{len(steps)} steps run in a circle with nothing pausing them",
                f"These steps hand back to each other — {names} — and there is "
                "no wait anywhere on the lap, so a contact who enters goes "
                "round as fast as the platform will run it. Every action "
                "inside repeats on every lap"
                + (f", including {len(sends)} message"
                   f"{'s' if len(sends) != 1 else ''} to the contact, sent "
                   "back to back until somebody notices and pulls them out by "
                   "hand." if sends else
                   ", burning an execution slot the whole time, and nothing "
                   "reports it as an error because each individual lap "
                   "completes normally."),
                "Follow the connections out of "
                f"'{steps[0].name or steps[0].type}' — one of them goes back "
                "up. A loop needs two things this one has neither of: a wait "
                "on the lap, and a way out. Add the wait, then an If/Else "
                "before the jump that checks an attempt counter and leaves at "
                "three. Enroll one test contact afterwards and read the "
                "execution log — it should show a fixed number of laps and "
                "then stop.",
                step=names,
                cost="One contact can take the account's execution capacity on "
                     "its own, and everybody else's messages queue behind it. "
                     "If there is a send on the lap, that person is being "
                     "messaged over and over right now.")


# --------------------------------------------------------------------------
# GHL090 — the same answer fetched twice
# --------------------------------------------------------------------------

# Only a declared read. A call whose method the export does not carry is left
# alone rather than assumed: GET and POST default differently across the two
# platforms, and a repeated POST is a different defect with a different fix
# (GHL085 — the idempotency key), which must not be answered by this rule.
READ_METHODS = ("get", "head")


@rule("GHL090", "The same external lookup runs more than once per contact",
      "medium", "routing", "scale", "api")
def repeated_identical_lookup(acct: Account):
    """One workflow asking an outside system the same question twice.

    Written one step at a time, this is invisible: each call is individually
    correct, and the contact who needs the customer record in step 2 and again
    in step 7 gets the right answer both times. It shows up as a bill, a rate
    limit, or a run that takes four seconds instead of one, and it multiplies
    with volume — a thousand contacts a day is a thousand wasted calls. The
    fix is the one HighLevel built for it: save the response once and read the
    saved values downstream. A wait between two identical calls means it is a
    POLL, not a duplicate, so the check walks the graph and refuses to pass
    through a pause.
    """
    for wf in _live(acct):
        reads = [(i, s) for i, s in enumerate(wf.steps)
                 if _is_call(s) and _method(s) in READ_METHODS]
        if len(reads) < 2:
            continue
        edges = _graph(wf)
        grouped: dict = {}
        for i, step in reads:
            grouped.setdefault(_destination(step), []).append((i, step))
        for destination in sorted(grouped):
            group = grouped[destination]
            if len(group) < 2:
                continue
            # Same pass only: each repeat has to be reachable from one already
            # counted, without a wait in between.
            chain = [group[0]]
            for candidate in group[1:]:
                if any(_reaches_without_pause(edges, wf.steps, prior,
                                              candidate[0])
                       for prior, _ in chain):
                    chain.append(candidate)
            if len(chain) < 2:
                continue
            shown = _endpoint(chain[0][1])
            yield _finding(
                "GHL090", "high" if len(chain) > 2 else "medium", wf,
                f"The same lookup runs {len(chain)} times for one contact",
                f"This workflow calls {shown} {len(chain)} times on the same "
                "pass, with nothing in between that could change the answer. "
                "Every one of those is a round trip the contact waits through "
                "and a request counted against whatever limit that API has. At "
                "ten contacts an hour nobody notices. At a thousand contacts a "
                f"day it is {(len(chain) - 1) * 1000:,} calls for information "
                "the workflow already had.",
                "Call it once, turn on 'Save response from this Webhook', and "
                "read the saved values in the later steps instead of calling "
                "again. Where the later steps need a different piece of data, "
                "ask for everything in the first call rather than making a "
                "second — one bigger response is cheaper than two round trips, "
                "every time.",
                step=", ".join((s.name or s.type) for _, s in chain[:4]),
                cost=f"{len(chain)}x the API calls this workflow actually "
                     "needs. It is the first thing to break when the provider "
                     "tightens a rate limit, and the call that gets throttled "
                     "is the one a contact is waiting on.")


# --------------------------------------------------------------------------
# GHL091 — every retry comes back at the same instant
# --------------------------------------------------------------------------

BACKOFF_HINT = re.compile(
    r"backoff|back[-_ ]off|jitter|exponential|stagger|randomi[sz]|"
    r"spread|attempt[_ -]?delay|retry[_ -]?delay", re.I)

# An hour, because the point is how many synchronised bursts a day the endpoint
# takes: at 30 minutes it is 48, at 5 minutes it is 288, and every one of them
# is the whole backlog arriving at once. Above an hour the cohort is still in
# lockstep, but the endpoint gets room between waves to actually recover.
LOCKSTEP_MINUTES = 60.0


@rule("GHL091", "Retry ladder that comes back in lockstep", "high", "routing",
      "scale", "retries")
def retry_without_backoff(acct: Account):
    """A retry loop whose wait is the same length every lap.

    The thing that makes this bite is that an API almost never fails for one
    contact — it fails for everybody at once: an outage, an expired token, a
    rate limit. Every contact in the loop at that moment then waits exactly
    the same amount and comes back at the same instant, so the endpoint gets
    the entire backlog as one burst, again a fixed interval later, and again.
    Where the failure WAS a rate limit, the synchronised retries are the
    reason it never clears. Two mechanisms fix it and a mature build has both:
    a delay that grows with the attempt, and a bit of per-contact variation so
    the cohort stops moving as one block.

    This is the other half of GHL089 — that one is a loop with no pause, this
    one is a loop whose pause is the same every time — and it is deliberately
    independent of whether the ladder is bounded, which is GHL046's question. A
    perfectly bounded three-attempt ladder still hammers in lockstep.
    """
    for wf in _live(acct):
        if BACKOFF_HINT.search(wf.text() + " " +
                               " ".join(s.name for s in wf.steps)):
            continue
        edges = _graph(wf)
        for group in _loops(edges):
            steps = [wf.steps[i] for i in group]
            if not any(_is_call(s) for s in steps):
                continue
            waits = [s for s in steps if _is_pause(s)]
            fixed = [w for w in waits
                     if (_wait_minutes(w) or 0) > 0 and "{{" not in w.text()]
            # A wait built from a merge field is a computed delay — that IS
            # backoff, whatever it is named, so it is left alone. So is one
            # whose length the export does not state: unknown is not fixed.
            if not waits or len(fixed) != len(waits):
                continue
            delay = min(_wait_minutes(w) or 0.0 for w in fixed)
            call = next(s for s in steps if _is_call(s))
            tight = delay < LOCKSTEP_MINUTES
            yield _finding(
                "GHL091", "high" if tight else "medium", wf,
                f"Every lap of this loop waits exactly {_human(delay)}",
                "This workflow calls an outside system in a loop — a retry or "
                "a poll — and the wait between attempts is a fixed "
                f"{_human(delay)}. When that endpoint has a bad minute it has "
                "it for everyone at once, so "
                "every contact sitting in this loop comes back at the same "
                f"instant, {_human(delay)} later — the whole backlog as one "
                "burst, then another, then another"
                + (f", {round(1440 / max(delay, 1))} times a day."
                   if tight else ".") +
                " If what failed was a rate limit, these retries are the "
                "reason it stays failed.",
                "Make the delay grow with the attempt: branch on the attempt "
                "counter and wait longer each lap — a minute, then five, then "
                "thirty. Then break the lockstep so the cohort stops arriving "
                "together: route contacts down two or three slightly "
                "different waits using something already on the record (the "
                "last digit of the phone number works), so a hundred retries "
                "are spread across a few minutes instead of landing on one "
                "second.",
                step=call.name or call.type,
                cost="The retries turn one bad minute at the provider into a "
                     "sustained one. Every wave arrives together, gets "
                     "rejected together, and comes back together — which is "
                     "also what gets an API key rate-limited or blocked.")


# --------------------------------------------------------------------------
# GHL092 — contacts stacking up inside one sequence
# --------------------------------------------------------------------------

REENTRY_KEYS = {"allowreentry", "allowmultiple", "allowmultipleentry",
                "reentry", "reenrollment", "allowreenrollment",
                "allowmultipleenrollment"}

TRUE_WORDS = ("true", "yes", "on", "1", "allow", "allowed")

# Triggers that fire again for the SAME contact in the ordinary course of
# business. contact_created is deliberately absent: it happens once per person,
# so no amount of re-entry stacks anything up.
REPEATABLE = re.compile(
    r"submit|repl|receiv|inbound|message|tag|stage|status|updat|chang|"
    r"order|purchas|payment|call|abandon", re.I)

# Three days, because that is where the run starts outliving the event that
# began it. A lead who fills the form on Monday and again on Wednesday is
# ordinary behaviour; a two-hour sequence has finished long before they do it
# again, and its overlapping copies would land far enough apart that nobody
# reads them as duplicates.
PARKED_MINUTES = 3 * 1440.0

GUARD_HINT = re.compile(
    r"already|guard|in[-_ ]?progress|enrolled|active|running|dedup|"
    r"currently|has[-_ ]?tag", re.I)


def _reentry_allowed(wf: Workflow) -> bool:
    for key, value in (wf.settings or {}).items():
        if _nk(key) in REENTRY_KEYS:
            return value is True or str(value).strip().lower() in TRUE_WORDS
    return False


def _entry_guard(wf: Workflow) -> bool:
    """Is there anything at the top that stops two copies overlapping?

    Two shapes count, and both are real builds. The first is the documented
    workaround for when the re-entry toggle cannot be trusted: an If/Else on a
    tag the workflow adds on entry. The second is the opposite decision made
    properly — a Remove From Workflow pointing at this same workflow, which
    cancels the run already in flight so the new one is the only one. Neither
    is graded here; the point is not to flag a build that has one.
    """
    first_send = next((i for i, s in enumerate(wf.steps) if s.is_outbound),
                      None)
    if first_send is None:
        return False
    for step in wf.steps[:first_send]:
        if "removefromworkflow" in _nk(step.type) or \
                "removeworkflow" in _nk(step.type):
            targets = [str(v).strip().lower()
                       for kind, v in step.entity_refs() if kind == "workflow"]
            # No target named means "this one" in every export shape seen.
            if not targets or wf.id.lower() in targets \
                    or wf.name.strip().lower() in targets:
                return True
        if not step.is_branch:
            continue
        if GUARD_HINT.search(step.name + " " + step.text()):
            return True
        if step.tags_added() or "tag" in step.text().lower():
            return True
    return False


@rule("GHL092", "Long sequence a contact can re-enter while still inside it",
      "high", "routing", "scale", "concurrency")
def concurrent_enrolment_pileup(acct: Account):
    """Re-entry plus a long wait: the same person, running several times over.

    Re-entry on its own is a design decision, and often the right one — a
    no-show recovery should absolutely re-enroll on the second no-show. It
    turns into a defect when the sequence is long enough that the first run is
    still parked when the second one starts, because then the copies overlap:
    one contact, three simultaneous runs, every message arriving in triplicate
    from the same number. At volume it is also why an account's enrollment
    numbers stop matching its contact numbers.

    Appointment- and invoice-triggered workflows are deliberately excluded.
    HighLevel always allows multiple entry on those regardless of the toggle
    (that is GHL030's finding), and there a second entry is CORRECT — a second
    appointment deserves a second reminder ladder.
    """
    for wf in _live(acct):
        if not wf.triggers or not _reentry_allowed(wf):
            continue
        if any(k in t.type.lower() for t in wf.triggers
               for k in ("appointment", "invoice")):
            continue
        if not any(REPEATABLE.search(t.canonical_type()) for t in wf.triggers):
            continue
        parked = _longest_park(wf.steps, _graph(wf))
        if parked < PARKED_MINUTES:
            continue
        first_wait = next((i for i, s in enumerate(wf.steps) if _is_pause(s)),
                          None)
        after = [s for s in wf.steps[first_wait + 1:] if s.is_outbound] \
            if first_wait is not None else []
        if not after or _entry_guard(wf):
            continue
        yield _finding(
            "GHL092", "high" if len(after) > 1 else "medium", wf,
            f"Contacts sit here for up to {_human(parked)}, and can start a "
            "second copy while they do",
            f"This workflow parks a contact for as long as {_human(parked)} "
            "and re-entry is on, so every time its trigger fires again — "
            "another form submit, another reply, another tag — that person "
            "starts a SECOND run of the sequence while the first one is still "
            f"going. They receive the remaining {len(after)} message"
            f"{'s' if len(after) != 1 else ''} twice, from the same number, a "
            "day or two apart. Nothing reports it: the workflow simply has "
            "more runs in it than the list has people.",
            "Decide which one you want. If a repeat event should restart the "
            "sequence, turn re-entry off and add a Remove From Workflow step "
            "at the top so the old run is cancelled before the new one starts. "
            "If it should not, guard the entry: an If/Else on a tag this "
            "workflow adds on the way in ('in-<name>'), exiting when it is "
            "already there, and a Remove Tag at the end. The tag version is "
            "the one to use on triggers where HighLevel ignores the re-entry "
            "setting.",
            step=wf.steps[first_wait].name or wf.steps[first_wait].type,
            reach=len(after),
            cost="The same person gets the same texts twice, from the same "
                 "number, for as long as the sequence runs. It is the most "
                 "common reason a client says the automation is spamming "
                 "their list — and the reason it keeps happening after "
                 "somebody 'fixed' the copy.")


# --------------------------------------------------------------------------
# GHL093 — workflows that feed each other
# --------------------------------------------------------------------------

ADD_TO_WORKFLOW = re.compile(
    r"add[_ -]?to[_ -]?(?:workflow|campaign)|"
    r"(?:start|trigger|enrol|enroll)[_ -]?(?:in[_ -]?)?workflow", re.I)

WORKFLOW_NAME_KEYS = {"workflow", "workflowname", "targetworkflowname",
                      "campaign", "campaignname"}


def _enrolment_targets(step: Step) -> list:
    """The workflows this step puts the contact into — ids and names both.

    A merge-built target is skipped: "{{ custom_values.next_workflow }}" is
    resolved at run time and cannot be matched against anything here, and
    guessing which workflow it means is how a loop gets reported that does not
    exist.
    """
    if not ADD_TO_WORKFLOW.search(step.type + " " + step.name):
        return []
    out = [value for kind, value in step.entity_refs() if kind == "workflow"]
    for key, value in _scalars(step.raw):
        if _nk(key) in WORKFLOW_NAME_KEYS and isinstance(value, str) \
                and value.strip() and "{{" not in value:
            out.append(value.strip())
    return out


@rule("GHL093", "Workflows that enroll each other in a circle", "critical",
      "routing", "scale", "loops")
def cross_workflow_enrolment_loop(acct: Account):
    """A adds the contact to B, and B adds them back to A.

    GHL014 finds this when the mechanism is a tag and GHL040 when it is a
    pipeline stage. Neither can see it when the mechanism is an explicit Add
    To Workflow step, which is the version that reads as completely deliberate
    on the canvas: each workflow looks like it simply hands off to the next
    one. Nothing in the builder draws the circle, because no single workflow
    contains it.

    Each lap is a fresh enrollment in both workflows, so the runs multiply
    rather than repeat, and everything inside either one — messages,
    opportunities, alerts, webhook calls — happens again on every lap. Only
    published targets count: an add pointing at a draft workflow does nothing,
    and calling that a runaway would be a false alarm.
    """
    live = list(_live(acct))
    by_id = {wf.id: i for i, wf in enumerate(live) if wf.id}
    by_name: dict = {}
    for i, wf in enumerate(live):
        by_name.setdefault(wf.name.strip().lower(), i)

    edges: dict = {i: set() for i in range(len(live))}
    for i, wf in enumerate(live):
        for step in wf.steps:
            for target in _enrolment_targets(step):
                j = by_id.get(target)
                if j is None:
                    j = by_name.get(str(target).strip().lower())
                if j is not None:
                    edges[i].add(j)

    for group in _loops(edges):
        wfs = [live[i] for i in group]
        sends = sum(len(w.outbound) for w in wfs)
        reenters = any(_reentry_allowed(w) for w in wfs)
        names = " -> ".join(w.name for w in wfs) + f" -> {wfs[0].name}"
        if len(wfs) == 1:
            title = f"'{wfs[0].name}' enrolls contacts back into itself"
            opening = ("This workflow's own Add To Workflow step points at "
                       "itself, so finishing it starts it again.")
        else:
            title = f"{len(wfs)} workflows enroll each other in a circle"
            opening = ("Each of these adds the contact to the next one, and "
                       f"the chain closes back on itself: {names}.")
        yield Finding(
            rule="GHL093", severity="critical" if sends else "high",
            workflow=wfs[0].name, step=names, title=title, category="routing",
            reach=sends,
            symptom=opening + " Every lap is a brand-new enrollment rather "
                    "than a repeat, so the runs multiply and everything "
                    "inside happens again"
                    + (f" — including {sends} message"
                       f"{'s' if sends != 1 else ''} to the contact." if sends
                       else ", including every webhook call and every record "
                            "these workflows write.")
                    + (" Re-entry is on somewhere in the circle, so nothing "
                       "stops it: the contact laps it until a person notices."
                       if reenters else
                       " Re-entry is off, which caps it at one lap today — "
                       "but the first person to toggle that on turns this "
                       "into a runaway, and nothing in the builder will warn "
                       "them."),
            fix="Decide which direction is the real hand-off and delete the "
                "other Add To Workflow step. If both directions are genuinely "
                "needed, guard the return leg: the second workflow should add "
                "the contact back only behind an If/Else on a tag the first "
                "one sets, and the first must remove that tag on its way out "
                "so a lap cannot restart. Check the enrollment counts on both "
                "before and after — if they are already higher than the "
                "number of contacts, this has been running.",
            cost="Enrollment counts climb on their own and sends multiply "
                 "with them. The account's execution capacity goes to a "
                 "handful of contacts going round in a circle, and every "
                 "other workflow queues behind them.")


# --------------------------------------------------------------------------
# GHL094 — more paths than anybody has walked
# --------------------------------------------------------------------------

# Four, because of what the arithmetic does: each condition doubles the number
# of distinct routes, so four nested conditions is up to sixteen and six is
# sixty-four. A build is normally tested on the happy path and one exception.
# Sixteen is the point where the untested majority is the workflow.
NESTING_LIMIT = 4


def _branch_depth(wf: Workflow) -> tuple:
    """(how many conditions deep the deepest path goes, the chain of them).

    Two export shapes carry nesting. The builder flattens branch children into
    the step list and links each to its parent with a parentKey, so depth is
    the number of branch ancestors above a step. Simpler exports nest the
    children inline under the branch itself. Both are read; a flat ordered
    list carries neither and cannot be measured at all.
    """
    steps = wf.steps
    best = (0, [])

    by_id: dict = {}
    for i, step in enumerate(steps):
        if step.step_id and step.step_id not in by_id:
            by_id[step.step_id] = i
    parent: dict = {}
    for i, step in enumerate(steps):
        key = step.parent_key
        if not key:
            continue
        match = ""
        for sid in by_id:
            if key != sid and not key.startswith(sid + "-"):
                continue
            if len(sid) > len(match):
                match = sid
        if match and by_id[match] != i:
            parent[i] = by_id[match]

    for i in range(len(steps)):
        chain: list = []
        node = i
        seen: set = set()
        while node is not None and node not in seen:
            seen.add(node)
            if steps[node].is_branch:
                chain.append(steps[node].name or steps[node].type)
            node = parent.get(node)
        if len(chain) > best[0]:
            best = (len(chain), list(reversed(chain)))

    def inline(step: Step, depth: int, chain: list):
        nonlocal best
        if step.is_branch:
            depth += 1
            chain = chain + [step.name or step.type]
            if depth > best[0]:
                best = (depth, chain)
        for _label, kids in step.branches():
            for kid in kids:
                if isinstance(kid, dict):
                    inline(Step(type=str(kid.get("type") or "unknown"),
                                name=str(kid.get("name") or ""), raw=kid),
                           depth, chain)

    for step in steps:
        if not step.parent_key:
            inline(step, 0, [])
    return best


@rule("GHL094", "Branching nested deeper than anyone can test", "medium",
      "routing", "scale", "maintainability")
def untestable_branch_depth(acct: Account):
    """A decision tree whose paths outnumber the ones anybody has walked.

    Not a style opinion — an arithmetic one. Every nested condition doubles
    the routes through the workflow, and testing is done by enrolling a
    contact and watching where they land, one route at a time. At four deep
    there are up to sixteen destinations and a build was checked on two of
    them; the other fourteen are discovered by a contact ending up somewhere
    strange months later, by which point nobody remembers what the third
    condition was for. It is also the shape that makes every later edit
    dangerous, because no one can hold the whole tree in their head to see
    what a change breaks.
    """
    readable = [wf for wf in _live(acct)
                if wf.has_wiring
                or any(kids for s in wf.steps if s.is_branch
                       for _label, kids in s.branches())]
    if not readable:
        yield Skip(
            rule="GHL094",
            title="Branching nested deeper than anyone can test",
            reason="No workflow in this export carries the structure that "
                   "would show nesting — no step ids and links, and no branch "
                   "children written inline. A flat list of steps in order "
                   "cannot say which condition sits inside which.",
            needs="an export that includes each step's id and its "
                  "next/parentKey, or branches with their child actions",
            category="routing")
        return

    for wf in readable:
        depth, chain = _branch_depth(wf)
        if depth < NESTING_LIMIT:
            continue
        yield _finding(
            "GHL094", "high" if depth >= NESTING_LIMIT + 2 else "medium", wf,
            f"Conditions nested {depth} deep — up to {2 ** depth} different "
            "paths",
            f"The deepest route through this workflow passes {depth} "
            f"conditions one inside the next, which is up to {2 ** depth} "
            "different places a contact can end up. Nobody has walked "
            f"{2 ** depth} routes. The ones that were tested are the ones "
            "somebody thought of at the time; the rest get found when a "
            "contact ends up somewhere strange and the person looking at it "
            "cannot tell whether that was intended. Every edit from here is a "
            "guess, because no one can hold the whole tree well enough to say "
            "what a change breaks.",
            "Flatten it. Do the routing ONCE — a single condition on one "
            "field that the steps above have already worked out — and give "
            "each outcome its own workflow. Three shallow workflows can be "
            "tested one at a time by a person who has never seen the account; "
            "one four-deep tree cannot be tested at all. Where the nesting "
            "exists because several conditions must all be true, combine them "
            "into one If/Else with multiple AND conditions instead of "
            "stacking separate steps.",
            step=" > ".join(chain[:4]),
            cost="Most of the routes through this workflow have never had a "
                 "contact on them. The bill arrives as a lead who got the "
                 "wrong sequence, months after the branch that did it was "
                 "built, and as an hour of somebody's time every time it "
                 "needs an edit.")


# --------------------------------------------------------------------------
# GHL104 — one event, two conversations, through a tag chain
# --------------------------------------------------------------------------

# Copy that opens a conversation: an acknowledgement AND a callback ask. Both
# are required. "Thanks for reaching out" on its own appears mid-sequence
# constantly, and a single signal is how this check would start reading a
# nurture drip as an introduction.
_OPENER_ACK = re.compile(
    r"thanks for (reaching out|getting in touch|your (enquiry|inquiry))"
    r"|(just )?got your details|received your (enquiry|inquiry|request|details)",
    re.I)
_OPENER_ASK = re.compile(
    r"what.?s (the )?best time to (reach|call) you|when.?s a good time"
    r"|can i (give you a )?call|good time to (call|chat)",
    re.I)
# Copy that closes: a booking widget AND a pick-a-slot imperative.
_CLOSER_LINK = re.compile(r"/widget/(booking|bookings|appointment)/", re.I)
_CLOSER_ASK = re.compile(
    r"grab (a|whichever|any) (time|slot)|pick a (time|slot)|book a (time|slot)"
    r"|choose a (time|slot)|lock in a (time|slot)",
    re.I)

# Two first touches closer together than this are one moment to the contact.
_SAME_MOMENT_MINUTES = 60.0


def _first_touch(wf: Workflow):
    """The first outbound step and the declared minutes before it.

    Only stated waits count. A reply-wait or a wait whose length is not in
    the export makes the delay unknowable and the workflow is dropped from
    the comparison — an estimate here would be the finding's whole basis.
    """
    minutes = 0.0
    for step in wf.steps:
        if step.is_outbound:
            return step, minutes
        if step.is_wait:
            held = _wait_minutes(step)
            if held is None:
                return None, None
            minutes += held
    return None, None


def _tag_hop_is_immediate(wf: Workflow, tag: str) -> bool:
    """Does this workflow add the tag before any pause?"""
    for step in wf.steps:
        if tag in {str(t).strip().lower() for t in step.tags_added()}:
            return True
        if step.is_wait:
            return False
    return False


def _coordinated(a: Workflow, b: Workflow) -> bool:
    """Does either workflow pull contacts out of the other by id or name?"""
    for src, dst in ((a, b), (b, a)):
        for step in src.steps:
            if "remove" not in _nk(step.type) or "workflow" not in _nk(step.type):
                continue
            blob = step.text().lower()
            if (dst.id and str(dst.id).lower() in blob) \
                    or (dst.name and dst.name.lower() in blob):
                return True
    return False


def _window_hours(wf: Workflow) -> str:
    """'09:00-20:00' from a send window, or '' when it states no hours."""
    win = wf.send_window() or {}
    start = _first(win, "start", "startTime", "from", default="")
    end = _first(win, "end", "endTime", "to", default="")
    return f"{start}-{end}" if start and end else ""


def _is_opener(step: Step) -> bool:
    body = step.bodies()
    return bool(_OPENER_ACK.search(body) and _OPENER_ASK.search(body))


def _is_closer(step: Step) -> bool:
    body = step.bodies()
    return bool(_CLOSER_LINK.search(body) and _CLOSER_ASK.search(body))


@rule("GHL104", "One event starts two conversations through a tag chain",
      "high", "routing", "triggers", "speed", "copy")
def chained_enrollment_collision(acct: Account):
    """Two workflows reach one lead from one event, and only one is visible.

    GHL015 catches two workflows on the same trigger. This is the version that
    hides from it: workflow A fires on the event and sends nothing, but adds a
    tag; workflow B fires on that tag and sends. Workflow C fires on the same
    event directly and sends too. In the builder B and C share no trigger, so
    nothing looks like a collision — and the contact gets two first messages,
    from one number, minutes apart.

    The hop has to be immediate (no pause in A before the tag lands) and both
    first touches have to have a declared delay, so the comparison is between
    two numbers the export states. Waits of unknown length drop the pair; an
    estimate is exactly what this must not be built on. A pair where either
    workflow removes contacts from the other is coordinated by design and is
    left alone.

    The copy decides the severity. When the message on the chained path is a
    close ("grab a time" plus a booking widget) and the direct path opens
    ("thanks for reaching out - what's the best time to call you?"), the
    ordering matters and the finding says so: a close that lands before the
    introduction, followed by a request to name a callback time, is what
    earns a STOP from the lead the account just scored as ready. Send windows
    are quoted as evidence and kept out of the arithmetic; they need an
    arrival time the export does not have.
    """
    live = [w for w in acct.published()]
    seen: set = set()
    for a in live:
        a_sigs = set(a.trigger_signatures())
        if not a_sigs:
            continue
        for tag in sorted({str(t).strip().lower() for t in a.tags_added()}):
            if not tag or not _tag_hop_is_immediate(a, tag):
                continue
            for b in live:
                if b is a or not b.outbound or tag not in b.trigger_tags():
                    continue
                b_step, b_delay = _first_touch(b)
                if b_step is None:
                    continue
                for c in live:
                    if c is a or c is b or not c.outbound:
                        continue
                    shared = a_sigs & set(c.trigger_signatures())
                    if not shared:
                        continue
                    c_step, c_delay = _first_touch(c)
                    if c_step is None:
                        continue
                    if abs(b_delay - c_delay) >= _SAME_MOMENT_MINUTES:
                        continue
                    if _coordinated(b, c):
                        continue
                    key = (b.name, c.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    event = sorted(shared)[0][0]

                    backwards = _is_closer(b_step) and _is_opener(c_step) \
                        and b_delay <= c_delay
                    order = ""
                    if backwards:
                        order = (
                            f" And the order is wrong: '{b_step.name or b_step.type}' "
                            f"in '{b.name}' asks them to pick a time and carries the "
                            f"booking link, while '{c_step.name or c_step.type}' in "
                            f"'{c.name}' is the introduction — it asks what time to "
                            f"call them. Going by the declared waits, the close goes "
                            f"out first and the introduction follows it, asking for "
                            f"a callback slot after the booking link has already "
                            f"been sent.")
                    win_note = ""
                    b_win, c_win = _window_hours(b), _window_hours(c)
                    if b_win and c_win and b_win != c_win:
                        win_note = (
                            f" The two send windows differ ('{b.name}' {b_win}, "
                            f"'{c.name}' {c_win}), so for a lead arriving in an hour "
                            f"one window is open and the other is not, the gap "
                            f"stretches to however long that hour lasts.")

                    yield _finding(
                        "GHL104", "high" if backwards else "medium", c,
                        f"One '{event}' event starts '{c.name}' and, through "
                        f"'{a.name}' tagging '{tag}', '{b.name}' too",
                        f"A '{event}' event enrolls '{c.name}' directly; its first "
                        f"message ('{c_step.name or c_step.type}') goes out after "
                        f"{_human(c_delay)}. The same event enrolls '{a.name}', "
                        f"which adds the tag '{tag}' with no pause before it, and that "
                        f"tag starts '{b.name}', whose first message "
                        f"('{b_step.name or b_step.type}') goes out after "
                        f"{_human(b_delay)}. Two conversations open from one number "
                        f"inside {_human(abs(b_delay - c_delay) or 1)} of each other, "
                        f"and the builder shows neither one next to the other — they "
                        f"share no trigger.{order}{win_note}",
                        f"Decide which workflow owns the first message. Either hold "
                        f"'{b.name}' behind a check that '{c.name}' has already sent "
                        f"(a tag it adds on the way out), or move the message into "
                        f"'{c.name}' and let '{b.name}' stay internal — score, tag, "
                        f"alert, and send nothing.",
                        step=f"{b.name} via '{a.name}' → '{tag}'",
                        cost="It lands hardest on the leads the chain was built to "
                             "prioritise: two different asks from one number in one "
                             "minute is the pattern that earns a STOP, or silence, from "
                             "someone who was about to book.",
                        reach=len(b.outbound) + len(c.outbound))
