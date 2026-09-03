#!/usr/bin/env python3
"""Measure the catalog's false-positive rate against a REAL account.

    python3 scripts/precision_report.py export.json           # run + show the worksheet
    python3 scripts/precision_report.py export.json --record  # persist findings for judging
    python3 scripts/precision_report.py --summary             # the measured rate, from the ledger

WHY THIS EXISTS
---------------
The test suite proves each rule fires on a fixture built to trip it and stays quiet
on one built to be clean. That is a necessary check and a weak one: the same person
wrote the rule and the fixture, so of course they agree. It says nothing about how
often a rule misfires on a real export, which is the only number a client cares
about — a report with junk in it does not get read twice.

The first fifty-two rules earned their calibration the hard way. The history has
commits like "Two false positives down, one false negative, from the first
live-account run" and "Fix two corpus-verified calibration bugs" — real accounts
narrowing real rules. GHL053-GHL100 arrived in one pass and have had none of that.
Nobody should trust the two halves equally until they have been measured the same
way, and "measured" has to mean a number somebody can re-derive, not an impression.

So: this runs the catalog over a real export, writes every finding into a ledger
with an empty verdict, and lets a human mark each one `real` or `false_positive`.
Findings keep their verdict across runs because they are keyed by content, not by
position. Once judged, `--summary` prints the rate per rule, per severity, and
split GHL001-052 vs GHL053-100 — which is the comparison that says whether the
second fifty are as good as the first.

That number belongs in the README when it is real. Until then the README should
not imply it.

THE LEDGER
----------
`calibration/verdicts.json`:

    {"accounts": {"<label>": {"exported": "...", "workflows": 24}},
     "verdicts": {"<key>": {"rule": "GHL071", "account": "...", "workflow": "...",
                            "step": "...", "title": "...",
                            "verdict": null|"real"|"false_positive",
                            "note": "why"}}}

`verdict: null` means nobody has judged it yet. An unjudged finding is never
counted as a pass — it is reported as unjudged, for the same reason the auditor
reports a skipped check instead of silently omitting it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ghlaudit.model import Account  # noqa: E402
from ghlaudit.rules import RULES, SEVERITIES, run_all  # noqa: E402

LEDGER = os.path.join(ROOT, "calibration", "verdicts.json")
# GHL001-052 came out of live account work over fourteen commits; GHL053-100
# arrived in one pass off domain knowledge. The split is the whole comparison.
FIRST_WAVE_MAX = 52


def _num(rule_id: str) -> int:
    try:
        return int(rule_id[3:])
    except (ValueError, TypeError):
        return 0


def wave(rule_id: str) -> str:
    return "GHL001-052" if _num(rule_id) <= FIRST_WAVE_MAX else "GHL053-100"


def key_of(account: str, f) -> str:
    """Stable identity for one finding, so a verdict survives the next run.

    Keyed on content — account, rule, workflow, step, title — and never on
    position in the list. Rules get reordered, workflows get renamed, and a
    verdict that moved to a different finding would be worse than no verdict.
    """
    raw = "|".join([account, f.rule, f.workflow, f.step or "", f.title])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def soft_key_of(account: str, f) -> str:
    """The same identity, minus the title — what the finding is ABOUT.

    `key_of` includes the title, which makes a verdict brittle in one specific
    and very common way: rewording a finding orphans its judgement. GHL018 is
    the worked example. The rule kept firing on exactly the same three
    workflows for exactly the same reason, but its wording went from "No
    workflow in this account adds 'ai-qualify'" to "Published, sends 2
    messages, and the only way in is 'ai-qualify'" — and every verdict
    attached to the old sentence was stranded, so the ledger read three
    genuine findings as having gone silent.

    That is worse than a bookkeeping nuisance. This catalog improves partly by
    rewriting findings until they say something an owner can act on, and a
    ledger that punishes rewording quietly argues against doing it.

    Titles change; a rule firing on a given step of a given workflow is the
    stable fact. This key is deliberately NOT used to store verdicts — only to
    find a stranded one, and only when exactly one candidate matches, because
    two rules can in principle report twice on the same step.
    """
    raw = "|".join([account, f.rule, f.workflow, f.step or ""])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def soft_key_of_row(key_account: str, row: dict) -> str:
    raw = "|".join([key_account, row["rule"], row.get("workflow") or "",
                    row.get("step") or ""])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def load_ledger() -> dict:
    if not os.path.exists(LEDGER):
        return {"accounts": {}, "verdicts": {}}
    with open(LEDGER) as fh:
        data = json.load(fh)
    data.setdefault("accounts", {})
    data.setdefault("verdicts", {})
    return data


def save_ledger(data: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def run(path: str, label: str, record: bool) -> int:
    acct = Account.from_file(path)
    findings, skips = run_all(acct)
    ledger = load_ledger()

    by_rule = defaultdict(list)
    for f in findings:
        by_rule[f.rule].append(f)
    skipped = {s.rule for s in skips}

    new = 0
    for f in findings:
        k = key_of(label, f)
        if k in ledger["verdicts"]:
            continue
        new += 1
        if record:
            ledger["verdicts"][k] = {
                "rule": f.rule, "account": label, "workflow": f.workflow,
                "step": f.step, "title": f.title, "severity": f.severity,
                "verdict": None, "note": "",
            }

    print(f"account: {label}   ({len(acct.workflows)} workflows, from {path})")
    print(f"{len(findings)} findings across {len(by_rule)} rules · "
          f"{len(skipped)} checks skipped · "
          f"{len(RULES) - len(by_rule) - len(skipped)} ran clean\n")

    print(f"{'RULE':8} {'WAVE':12} {'SEV':9} {'HITS':>5}  STATUS")
    print("-" * 76)
    # Judged-ness has to be counted against THIS run's findings. Counting every
    # ledger row for the rule instead printed things like "3/1 judged", a
    # verdict count measured against a different denominator, which reads as a
    # bug in the ledger when it is a bug in the sum.
    for r in sorted(RULES, key=lambda r: r.id):
        mine = by_rule.get(r.id, [])
        hits = len(mine)
        exact = {key_of(label, f) for f in mine}
        soft = {soft_key_of(label, f) for f in mine}
        judged = sum(1 for k, v in ledger["verdicts"].items()
                     if v.get("verdict")
                     and (k in exact
                          or (v["rule"] == r.id
                              and soft_key_of_row(v["account"], v) in soft)))
        judged = min(judged, hits)

        if hits and r.id in skipped:
            # A rule can report on what it could see AND declare a dimension
            # unknown — GHL025 reads every email body, then says it cannot know
            # the account's sending-domain state. Printing that as "SKIPPED"
            # hid seven real findings behind a label that says it did nothing.
            status = (f"PARTIAL — {judged}/{hits} judged, and one dimension "
                      f"needs context this export lacks")
        elif r.id in skipped:
            status = "SKIPPED — needs account context this export lacks"
        elif hits == 0:
            status = "clean"
        else:
            status = f"{judged}/{hits} judged" if judged else "UNJUDGED"
        print(f"{r.id:8} {wave(r.id):12} {r.severity:9} {hits:>5}  {status}")

    if record:
        ledger["accounts"][label] = {"path": os.path.abspath(path),
                                     "workflows": len(acct.workflows),
                                     "findings": len(findings)}
        save_ledger(ledger)
        print(f"\nrecorded {new} new finding(s) into "
              f"{os.path.relpath(LEDGER, ROOT)} with verdict: null")
        print("Judge each one by setting verdict to \"real\" or \"false_positive\", "
              "then run --summary.")
    elif new:
        print(f"\n{new} finding(s) are not in the ledger. Re-run with --record to "
              f"add them for judging.")
    return 0


def _current_keys(ledger: dict):
    """Re-run the catalog over every recorded export, and key what it emits NOW.

    The ledger is a record of judgements. The catalog is a thing that keeps
    changing. Those two drift apart the moment a rule is narrowed, and the
    drift is invisible unless something re-runs the code.

    GHL007 is the worked example. It put 22 false positives into one real
    report — 56% of every false positive ever recorded here — because it had
    the vendor's deprecation backwards. It was rewritten and now fires zero
    times on the same export. The lifetime rate still counted all 22, so the
    headline number could not tell that the worst rule in the catalog had
    been fixed. A quality metric that cannot see a fix land is not measuring
    quality; it is measuring history.

    Returns (live_keys, checked, missing). `missing` is accounts whose export
    is no longer on disk. They are reported, never assumed either way: one of
    the two calibration exports lived in a scratch directory and macOS
    deleted it, and quietly counting its verdicts as still-true — or quietly
    dropping them — would both be lies of a different sign.
    """
    live, soft, checked, missing = set(), set(), [], []
    for label, meta in sorted(ledger.get("accounts", {}).items()):
        path = meta.get("path")
        if not path or not os.path.exists(path):
            missing.append(label)
            continue
        try:
            acct = Account.from_file(path)
            findings, _ = run_all(acct)
        except Exception as exc:                     # a corrupt export is missing
            print(f"⚠️  {label}: could not re-run ({exc})", file=sys.stderr)
            missing.append(label)
            continue
        for f in findings:
            live.add(key_of(label, f))
            soft.add(soft_key_of(label, f))
        checked.append(label)
    return live, soft, checked, missing


def summary(live: bool = False) -> int:
    ledger = load_ledger()
    all_rows = dict(ledger["verdicts"])
    if not all_rows:
        print("No findings recorded yet. Run:\n"
              "  python3 scripts/precision_report.py <real-export.json> --record")
        return 1

    retired, unverifiable, superseded, missing = {}, {}, {}, []
    rows = all_rows
    if live:
        live_keys, live_soft, checked, missing = _current_keys(ledger)
        gone = set(missing)

        def still_emitted(k, r):
            # Exact match first; fall back to the title-independent identity so
            # a reworded finding keeps its verdict instead of reading as a
            # rule that went silent. See soft_key_of.
            return (k in live_keys
                    or soft_key_of_row(r["account"], r) in live_soft)

        unverifiable = {k: r for k, r in all_rows.items()
                        if r["account"] in gone}
        emitted = {k: r for k, r in all_rows.items()
                   if r["account"] not in gone and still_emitted(k, r)}

        # A reworded finding leaves two ledger entries behind — the old
        # sentence and the new one — and both answer to the same soft key.
        # Counting both would inflate the denominator and let one judgement
        # vote twice. Keep the entry the catalog actually emits today.
        rows, superseded = {}, {}
        best = {}
        for k, r in emitted.items():
            sk = soft_key_of_row(r["account"], r)
            current = best.get(sk)
            if current is None or (k in live_keys and current[0] not in live_keys):
                if current is not None:
                    superseded[current[0]] = current[1]
                best[sk] = (k, r)
            else:
                superseded[k] = r
        rows = {k: r for k, r in best.values()}

        retired = {k: r for k, r in all_rows.items()
                   if not still_emitted(k, r) and r["account"] not in gone}
        if not rows:
            print("Nothing in the ledger is still emitted by the current "
                  "catalog, so there is no live rate to report.")
            if retired:
                print(f"{len(retired)} recorded finding(s) are retired — the "
                      f"rules that produced them no longer fire.")
            return 1

    values = list(rows.values())
    judged = [r for r in values if r.get("verdict")]
    unjudged = len(values) - len(judged)

    if live:
        print(f"LIVE — the catalog was re-run over {len(missing) + len(checked)} "
              f"recorded export(s); this rates only what it still emits.\n")
        print(f"{len(values)} of {len(all_rows)} recorded findings are still "
              f"emitted · {len(judged)} judged · {unjudged} still unjudged\n")
    else:
        print(f"{len(values)} findings recorded across "
              f"{len(ledger['accounts'])} account(s) · "
              f"{len(judged)} judged · {unjudged} still unjudged\n")

    if not judged:
        print("Nothing judged yet, so there is no rate to report. An unjudged "
              "finding is not a passing one.")
        return 1

    def rate(subset):
        if not subset:
            return None
        fp = sum(1 for r in subset if r["verdict"] == "false_positive")
        return fp, len(subset), 100.0 * fp / len(subset)

    print("BY WAVE — this is the comparison that matters")
    print(f"  {'wave':14} {'false pos':>10} {'judged':>8} {'rate':>8}")
    for w in ("GHL001-052", "GHL053-100"):
        got = rate([r for r in judged if wave(r["rule"]) == w])
        if got:
            fp, n, pct = got
            print(f"  {w:14} {fp:>10} {n:>8} {pct:>7.1f}%")
        else:
            print(f"  {w:14} {'—':>10} {0:>8} {'n/a':>8}")

    print("\nBY SEVERITY")
    for sev in SEVERITIES:
        got = rate([r for r in judged if r.get("severity") == sev])
        if got:
            fp, n, pct = got
            print(f"  {sev:10} {fp:>4} / {n:<4}  {pct:>5.1f}%")

    worst = defaultdict(lambda: [0, 0])
    for r in judged:
        worst[r["rule"]][1] += 1
        if r["verdict"] == "false_positive":
            worst[r["rule"]][0] += 1
    noisy = sorted(((fp / n, fp, n, rid) for rid, (fp, n) in worst.items() if fp),
                   reverse=True)
    if noisy:
        print("\nRULES TO NARROW — every one of these put junk in a real report")
        for _, fp, n, rid in noisy[:15]:
            print(f"  {rid}  {fp}/{n} false")

    fp, n, pct = rate(judged)
    label = "LIVE" if live else "OVERALL"
    print(f"\n{label}: {fp} false positives in {n} judged findings — {pct:.1f}%")

    # How many accounts is that rate actually standing on? A clean number
    # measured over one account is a statement about one account, and the
    # temptation to quote it as the catalog's rate is exactly what this whole
    # script exists to resist. Two exports OF THE SAME LOCATION two days apart
    # are one account's worth of evidence wearing two labels, and nothing in
    # the ledger can tell that from two genuinely different businesses — so
    # this reports the spread and lets the reader judge it.
    spread = defaultdict(int)
    for r in judged:
        spread[r["account"]] += 1
    print(f"   across {len(spread)} export(s): "
          + ", ".join(f"{a} ({c})" for a, c in sorted(spread.items())))
    if len(spread) < 3:
        print("   ⚠️  This rests on very few exports. It is a statement about "
              "these accounts, not about the catalog. Two exports of the SAME "
              "location are one account's evidence under two labels — check "
              "before quoting this anywhere a client can see it.")

    if live and retired:
        judged_retired = [r for r in retired.values() if r.get("verdict")]
        fixed = [r for r in judged_retired if r["verdict"] == "false_positive"]
        by_rule = defaultdict(int)
        for r in fixed:
            by_rule[r["rule"]] += 1
        print(f"\nRETIRED — {len(retired)} recorded finding(s) are no longer "
              f"emitted by the current catalog.")
        if by_rule:
            print(f"  {len(fixed)} of them were judged false. That is the "
                  f"narrowing work, and it is why the live rate is lower than "
                  f"the lifetime one:")
            for rid, count in sorted(by_rule.items(), key=lambda kv: -kv[1]):
                print(f"    {rid}  {count} false positive(s) no longer fire")
        still_real = [r for r in judged_retired if r["verdict"] == "real"]
        if still_real:
            print(f"  ⚠️  {len(still_real)} were judged REAL and have stopped "
                  f"firing. A rule that went quiet on a true problem is a "
                  f"regression, not a win — check these before celebrating:")
            gone = defaultdict(int)
            for r in still_real:
                gone[r["rule"]] += 1
            for rid, count in sorted(gone.items(), key=lambda kv: -kv[1]):
                print(f"    {rid}  {count} real finding(s) no longer reported")

    if live and superseded:
        print(f"\nSUPERSEDED — {len(superseded)} ledger entry(ies) describe a "
              f"finding the catalog still reports, but under wording it has "
              f"since changed. Their verdicts are held by the current entry "
              f"and are not counted twice. Re-run with --record to fold them "
              f"in.")

    if live and unverifiable:
        print(f"\nUNVERIFIABLE — {len(unverifiable)} recorded finding(s) belong "
              f"to export(s) no longer on disk "
              f"({', '.join(missing)}). They are neither counted nor "
              f"dismissed; re-export the account to bring them back into "
              f"the measurement.")

    if unjudged:
        print(f"⚠️  {unjudged} findings are unjudged. This rate covers only what "
              f"has been judged, and must not be quoted as the catalog's rate "
              f"until they are.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", help="a REAL account export, not the fixture")
    ap.add_argument("--label", help="name for this account in the ledger "
                                    "(default: the filename)")
    ap.add_argument("--record", action="store_true",
                    help="write findings into the ledger for judging")
    ap.add_argument("--summary", action="store_true",
                    help="print the measured rate from the ledger")
    ap.add_argument("--live", action="store_true",
                    help="with --summary: re-run the catalog over the recorded "
                         "exports and rate only the findings it still emits")
    args = ap.parse_args(argv)

    if args.summary:
        return summary(live=args.live)
    if args.live:
        ap.error("--live only means something with --summary")
    if not args.path:
        ap.error("give an export to run, or --summary")
    if os.path.abspath(args.path) == os.path.join(ROOT, "examples",
                                                  "broken-account.json"):
        print("⛔ That is the shipped fixture. Every rule is built to fire on it, "
              "so measuring precision against it is meaningless — point this at a "
              "real account export.", file=sys.stderr)
        return 2
    label = args.label or os.path.splitext(os.path.basename(args.path))[0]
    return run(args.path, label, args.record)


if __name__ == "__main__":
    raise SystemExit(main())
