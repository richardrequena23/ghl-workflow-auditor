"""Command line entry point.

    python -m ghlaudit account.json
    python -m ghlaudit account.json --format markdown --out audit.md
    python -m ghlaudit account.json --min-severity high
    python -m ghlaudit --list-rules
"""

from __future__ import annotations

import argparse
import json
import sys

from .model import Account
from .report import RENDERERS
from .rules import RULES, SEVERITIES, run


def _list_rules() -> str:
    width = max(len(r.title) for r in RULES)
    lines = [f"{len(RULES)} rules", ""]
    for r in sorted(RULES, key=lambda r: (SEVERITIES.index(r.severity), r.id)):
        tags = ",".join(r.tags)
        lines.append(f"  {r.id}  {r.severity:<8}  {r.title:<{width}}  [{tags}]")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ghlaudit",
        description="Static analysis for GoHighLevel workflows. Finds the failures "
                    "that only show up once a real customer is in the sequence.")
    ap.add_argument("path", nargs="?", help="workflow export (JSON), or - for stdin")
    ap.add_argument("--format", "-f", default="text", choices=sorted(RENDERERS))
    ap.add_argument("--min-severity", "-s", default="low", choices=SEVERITIES,
                    help="only report findings at least this severe")
    ap.add_argument("--rule", "-r", action="append",
                    help="run only this rule id; repeatable")
    ap.add_argument("--out", "-o", help="write to a file instead of stdout")
    ap.add_argument("--list-rules", action="store_true")
    ap.add_argument("--fail-on", choices=SEVERITIES,
                    help="exit 1 if anything at least this severe is found (for CI)")
    args = ap.parse_args(argv)

    if args.list_rules:
        print(_list_rules())
        return 0
    if not args.path:
        ap.error("a workflow export is required (or --list-rules)")

    raw = json.load(sys.stdin) if args.path == "-" else None
    acct = Account.load(raw) if raw is not None else Account.from_file(args.path)
    if not acct.workflows:
        print("No workflows found in that file.", file=sys.stderr)
        return 2

    findings = run(acct, min_severity=args.min_severity, only=args.rule)
    rendered = RENDERERS[args.format](findings, len(acct.workflows))

    if args.out:
        with open(args.out, "w") as fh:
            fh.write(rendered + "\n")
        print(f"wrote {args.out} ({len(findings)} findings)")
    else:
        print(rendered)

    if args.fail_on:
        cutoff = SEVERITIES.index(args.fail_on)
        if any(SEVERITIES.index(f.severity) <= cutoff for f in findings):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
