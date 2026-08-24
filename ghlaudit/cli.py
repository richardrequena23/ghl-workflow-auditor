"""Command line entry point.

    python -m ghlaudit account.json
    python -m ghlaudit account.json --html audit.html
    python -m ghlaudit account.json --format markdown --out audit.md
    python -m ghlaudit account.json --config client.json --min-severity high
    python -m ghlaudit --list-rules
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import AuditConfig
from .model import Account
from .report import RENDERERS, as_html
from .rules import RULES, SEVERITIES, run_all


def _list_rules() -> str:
    width = max(len(r.title) for r in RULES)
    lines = [f"{len(RULES)} rules", ""]
    for r in sorted(RULES, key=lambda r: (SEVERITIES.index(r.severity), r.id)):
        tags = ",".join(r.tags)
        lines.append(f"  {r.id}  {r.severity:<8}  {r.category:<14}  "
                     f"{r.title:<{width}}  [{tags}]")
    return "\n".join(lines)


def _build_config(args):
    """The caller's config, or None to let the bundle's own block stand.

    Returning an empty AuditConfig here instead of None would silently override
    a `config` block inside the bundle with nothing, and the checks it enables
    would report as skipped on a file that actually contained their input. A
    config file given on the command line does replace the bundle's block —
    the auditor's judgement about the client beats whatever the export shipped
    with — but only when one was actually given.
    """
    if not (args.config or args.owned_domain or args.stats_window):
        return None
    cfg = AuditConfig.from_file(args.config) if args.config else AuditConfig()
    if args.owned_domain:
        cfg.owned_domains = sorted(set(cfg.owned_domains) | {
            d.lower().lstrip("*.") for d in args.owned_domain})
    if args.stats_window:
        cfg.stats_window_days = args.stats_window
    return cfg


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
    ap.add_argument("--html", metavar="PATH", nargs="?", const="ghl-audit.html",
                    help="also write the client-facing HTML report "
                         "(default: ghl-audit.html)")
    ap.add_argument("--config", "-c", metavar="PATH",
                    help="JSON file of account-specific context: owned domains, "
                         "re-entry policy, send-window policy, build manifest")
    ap.add_argument("--owned-domain", action="append", metavar="DOMAIN",
                    help="a domain this account legitimately links to; repeatable")
    ap.add_argument("--stats-window", type=int, metavar="DAYS",
                    help="how many days the supplied enrollment stats cover "
                         "(default 90)")
    ap.add_argument("--account-name", metavar="NAME",
                    help="client name, printed on the HTML report")
    ap.add_argument("--prepared-by", metavar="NAME", default="Richard Requena",
                    help="auditor's name, printed on the HTML report "
                         "(pass an empty string for none)")
    ap.add_argument("--list-rules", action="store_true")
    ap.add_argument("--fail-on", choices=SEVERITIES,
                    help="exit 1 if anything at least this severe is found (for CI)")
    args = ap.parse_args(argv)

    if args.list_rules:
        print(_list_rules())
        return 0
    if not args.path:
        ap.error("a workflow export is required (or --list-rules)")

    config = _build_config(args)
    raw = json.load(sys.stdin) if args.path == "-" else None
    acct = (Account.load(raw, config=config) if raw is not None
            else Account.from_file(args.path, config=config))
    if not acct.workflows:
        print("No workflows found in that file.", file=sys.stderr)
        return 2

    findings, skips = run_all(acct, min_severity=args.min_severity,
                              only=args.rule)
    count = len(acct.workflows)
    if args.format == "html":
        rendered = as_html(findings, count, skips,
                           account_name=args.account_name or "",
                           prepared_by=args.prepared_by)
    else:
        rendered = RENDERERS[args.format](findings, count, skips)

    if args.out:
        with open(args.out, "w") as fh:
            fh.write(rendered + "\n")
        print(f"wrote {args.out} ({len(findings)} findings)")
    else:
        print(rendered)

    # --html is deliberately additive: the terminal report is for you and the
    # HTML is for the client, and an audit usually wants both in one pass.
    if args.html and not (args.format == "html" and args.out):
        page = as_html(findings, count, skips,
                       account_name=args.account_name or "",
                       prepared_by=args.prepared_by)
        with open(args.html, "w") as fh:
            fh.write(page + "\n")
        print(f"wrote {args.html} (client report, {len(findings)} findings)",
              file=sys.stderr if not args.out else sys.stdout)

    if args.fail_on:
        cutoff = SEVERITIES.index(args.fail_on)
        if any(SEVERITIES.index(f.severity) <= cutoff for f in findings):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
