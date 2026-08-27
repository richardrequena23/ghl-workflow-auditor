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
    for r in sorted(RULES, key=lambda r: r.id):
        hits = len(by_rule.get(r.id, []))
        if r.id in skipped:
            status = "SKIPPED — needs account context this export lacks"
        elif hits == 0:
            status = "clean"
        else:
            judged = sum(1 for v in ledger["verdicts"].values()
                         if v["rule"] == r.id and v["account"] == label
                         and v["verdict"])
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


def summary() -> int:
    ledger = load_ledger()
    rows = list(ledger["verdicts"].values())
    if not rows:
        print("No findings recorded yet. Run:\n"
              "  python3 scripts/precision_report.py <real-export.json> --record")
        return 1

    judged = [r for r in rows if r.get("verdict")]
    unjudged = len(rows) - len(judged)

    print(f"{len(rows)} findings recorded across "
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
    print(f"\nOVERALL: {fp} false positives in {n} judged findings — {pct:.1f}%")
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
    args = ap.parse_args(argv)

    if args.summary:
        return summary()
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
