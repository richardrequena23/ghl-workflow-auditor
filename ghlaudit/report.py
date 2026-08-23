"""Render findings.

Three audiences, three formats:
  text      you, in a terminal, mid-audit
  markdown  the client, pasted into an email or a handover doc
  json      whatever you are piping this into next
"""

from __future__ import annotations

import json
from collections import Counter

from .rules import Finding, SEVERITIES

ICON = {"critical": "!!", "high": "! ", "medium": "~ ", "low": "· "}


def _counts(findings: list[Finding]) -> Counter:
    return Counter(f.severity for f in findings)


def summary_line(findings: list[Finding], workflows: int) -> str:
    if not findings:
        return f"{workflows} workflows audited. Nothing found."
    c = _counts(findings)
    parts = [f"{c[s]} {s}" for s in SEVERITIES if c[s]]
    return f"{workflows} workflows audited. {len(findings)} findings: " + ", ".join(parts)


def group_by_workflow(findings: list[Finding]) -> list[tuple[str, list[Finding]]]:
    """Workflows ordered by their worst finding; findings within, most severe first.

    Sorting purely by severity scatters one workflow's problems across the whole
    report, which is useless when you are fixing them one workflow at a time.
    """
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        groups.setdefault(f.workflow, []).append(f)
    for items in groups.values():
        items.sort(key=Finding.sort_key)
    return sorted(groups.items(),
                  key=lambda kv: (SEVERITIES.index(kv[1][0].severity), kv[0]))


def as_text(findings: list[Finding], workflows: int) -> str:
    out = [summary_line(findings, workflows)]
    for name, items in group_by_workflow(findings):
        out += ["", name, "-" * len(name)]
        for f in items:
            head = f"{ICON[f.severity]} [{f.rule}] {f.title}"
            if f.step:
                head += f"  ({f.step})"
            out.append(head)
            out.append(f"     why it matters: {f.symptom}")
            out.append(f"     fix: {f.fix}")
    return "\n".join(out)


def as_markdown(findings: list[Finding], workflows: int) -> str:
    out = ["# GoHighLevel account audit", "", summary_line(findings, workflows), ""]
    if not findings:
        out.append("No findings. Every published workflow passed the catalog.")
        return "\n".join(out)

    out += ["| Severity | Rule | Workflow | Finding |", "|---|---|---|---|"]
    for f in findings:
        out.append(f"| {f.severity} | {f.rule} | {f.workflow} | {f.title} |")
    out.append("")

    for name, items in group_by_workflow(findings):
        out += ["", f"## {name}"]
        for f in items:
            out += [
                "", f"### {f.rule} — {f.title}  \n**Severity:** {f.severity}"
                + (f"  ·  **Step:** {f.step}" if f.step else ""),
                "", f"**What the customer sees.** {f.symptom}",
                "", f"**Fix.** {f.fix}",
            ]
    return "\n".join(out)


def as_json(findings: list[Finding], workflows: int) -> str:
    return json.dumps({
        "workflows_audited": workflows,
        "counts": dict(_counts(findings)),
        "findings": [f.to_dict() for f in findings],
    }, indent=2)


RENDERERS = {"text": as_text, "markdown": as_markdown, "json": as_json}
