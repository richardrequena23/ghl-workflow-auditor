"""Render findings.

Four audiences, four formats:
  text      you, in a terminal, mid-audit
  markdown  a handover doc, or a paste into an email
  json      whatever you are piping this into next
  html      the client — a single self-contained file, no external assets,
            readable printed to PDF

Everything client-facing is ordered by what a finding COSTS, not by rule id. A
rule catalog is organised for whoever maintains it. A report has to be organised
for whoever pays for the fixes, and those are two different orders.
"""

from __future__ import annotations

import datetime
import html
import json
from collections import Counter

from .rules import Finding, RULES, SEVERITIES
from .score import CATEGORY_LABEL, HealthScore, health

ICON = {"critical": "!!", "high": "! ", "medium": "~ ", "low": "· "}
SEVERITY_LABEL = {"critical": "Critical", "high": "High", "medium": "Medium",
                  "low": "Low"}


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


def _scored(findings, workflows, skips=None) -> HealthScore:
    return health(list(findings), list(skips or []), workflows)


# --------------------------------------------------------------------------
# terminal
# --------------------------------------------------------------------------

def as_text(findings: list[Finding], workflows: int, skips=None) -> str:
    hs = _scored(findings, workflows, skips)
    bar_w = 24
    out = [
        f"Account health: {hs.score}/100  ({hs.grade})   {hs.verdict}",
        summary_line(findings, workflows) + f"  [{hs.coverage}]",
        "",
    ]
    for cat in hs.categories:
        if not cat.assessed:
            out.append(f"  {cat.label:<16} not assessed  "
                       f"(needs: {', '.join(s.rule for s in cat.skips)})")
            continue
        filled = int(round(cat.score / 100.0 * bar_w))
        bar = "#" * filled + "." * (bar_w - filled)
        out.append(f"  {cat.label:<16} {cat.score:>3}/100  [{bar}]  "
                   f"{len(cat.findings)} finding"
                   f"{'s' if len(cat.findings) != 1 else ''}")

    if hs.ranked:
        out += ["", "Fix in this order — ranked by what each one costs:"]
        for i, f in enumerate(hs.ranked[:5], 1):
            out.append(f"  {i}. [{f.rule}] {f.title}  ({f.workflow})")

    for name, items in group_by_workflow(findings):
        out += ["", name, "-" * len(name)]
        for f in items:
            head = f"{ICON[f.severity]} [{f.rule}] {f.title}"
            if f.step:
                head += f"  ({f.step})"
            out.append(head)
            if f.cost:
                out.append(f"     what it costs: {f.cost}")
            out.append(f"     why it matters: {f.symptom}")
            out.append(f"     fix: {f.fix}")

    if hs.skips:
        out += ["", "Checks that could not run", "-------------------------"]
        for s in hs.skips:
            out.append(f"?  [{s.rule}] {s.title}")
            out.append(f"     {s.reason}")
            if s.needs:
                out.append(f"     supply: {s.needs}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------

def as_markdown(findings: list[Finding], workflows: int, skips=None) -> str:
    hs = _scored(findings, workflows, skips)
    out = ["# GoHighLevel account audit", "",
           f"**Account health: {hs.score}/100 ({hs.grade})** — {hs.verdict}", "",
           summary_line(findings, workflows) + f" ({hs.coverage}.)", ""]

    out += ["| Category | Score | Findings |", "|---|---|---|"]
    for cat in hs.categories:
        score = f"{cat.score}/100 ({cat.grade})" if cat.assessed else "not assessed"
        out.append(f"| {cat.label} | {score} | {len(cat.findings)} |")
    out.append("")

    if not findings:
        out.append("No findings. Every published workflow passed the catalog.")
        if hs.skips:
            out += ["", "Note: not every check ran — see below."]
        else:
            return "\n".join(out)

    if findings:
        out += ["## Fix in this order", "",
                "Ranked by what each finding costs, not by rule number.", "",
                "| # | Severity | Rule | Workflow | Finding |",
                "|---|---|---|---|---|"]
        for i, f in enumerate(hs.ranked, 1):
            out.append(f"| {i} | {f.severity} | {f.rule} | {f.workflow} | "
                       f"{f.title} |")
        out.append("")

        for name, items in group_by_workflow(findings):
            out += ["", f"## {name}"]
            for f in items:
                out += [
                    "", f"### {f.rule} — {f.title}  \n**Severity:** {f.severity}"
                    + f"  ·  **Category:** {CATEGORY_LABEL[f.category]}"
                    + (f"  ·  **Step:** {f.step}" if f.step else ""),
                ]
                if f.cost:
                    out += ["", f"**What it costs.** {f.cost}"]
                out += ["", f"**What the customer sees.** {f.symptom}",
                        "", f"**Fix.** {f.fix}"]

    if hs.skips:
        out += ["", "## Checks that could not run", "",
                "These are gaps in the audit, not passes.", ""]
        for s in hs.skips:
            out += [f"- **{s.rule} — {s.title}.** {s.reason}"
                    + (f" _Supply: {s.needs}_" if s.needs else "")]
    return "\n".join(out)


# --------------------------------------------------------------------------
# json
# --------------------------------------------------------------------------

def as_json(findings: list[Finding], workflows: int, skips=None) -> str:
    hs = _scored(findings, workflows, skips)
    payload = hs.to_dict()
    payload["findings"] = [f.to_dict() for f in hs.ranked]
    payload["skipped"] = [s.to_dict() for s in hs.skips]
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------
# html — the deliverable
# --------------------------------------------------------------------------

CSS = """
:root{
  --ground:#0F1C27; --panel:#152632; --line:#223947;
  --ink:#F3F7FA; --muted:#93A6B4; --accent:#19D3B0; --danger:#F2637E;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font-family:Poppins,"Helvetica Neue",Helvetica,Arial,sans-serif;
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto;padding:56px 28px 96px}
h1{font-size:30px;line-height:1.2;margin:0 0 6px;font-weight:600;
  letter-spacing:-.02em}
h2{font-size:19px;margin:52px 0 16px;font-weight:600;letter-spacing:-.01em;
  padding-bottom:10px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:0 0 8px;font-weight:600;letter-spacing:-.01em}
p{margin:0 0 12px}
a{color:var(--accent)}
.sub{color:var(--muted);font-size:13.5px;margin:0}
.rule{height:1px;background:var(--line);border:0;margin:34px 0}

.hero{display:flex;gap:32px;align-items:center;margin:36px 0 8px;
  background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:28px 32px}
.dial{flex:0 0 132px;text-align:center}
.dial .n{font-size:52px;font-weight:600;line-height:1;letter-spacing:-.03em;
  color:var(--accent)}
.dial .n.bad{color:var(--danger)}
.dial .of{color:var(--muted);font-size:12px;letter-spacing:.14em;
  text-transform:uppercase;margin-top:8px}
.dial .g{display:inline-block;margin-top:10px;padding:2px 12px;border-radius:20px;
  border:1px solid var(--line);font-size:12px;letter-spacing:.1em;
  color:var(--muted)}
.verdict{font-size:17px;font-weight:500;margin:0 0 10px;letter-spacing:-.01em}
.tally{color:var(--muted);font-size:13.5px}
.tally b{color:var(--ink);font-weight:600}

.cat{padding:14px 0;border-bottom:1px solid var(--line);page-break-inside:avoid;
  break-inside:avoid}
.cat:last-child{border-bottom:0}
.chead{display:flex;align-items:baseline;justify-content:space-between;gap:16px;
  margin-bottom:7px}
.clabel{font-weight:500;font-size:15px}
.cnum{color:var(--muted);font-size:13px;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.cnum b{color:var(--ink);font-weight:600}
.track{height:7px;background:#0B151D;border-radius:4px;overflow:hidden;
  border:1px solid var(--line)}
.fill{height:100%;background:var(--accent);border-radius:4px}
.fill.bad{background:var(--danger)}
.na{color:var(--muted);font-size:13px;font-style:italic}
.blurb{color:var(--muted);font-size:12.5px;margin:7px 0 0;max-width:74ch}

.f{background:var(--panel);border:1px solid var(--line);border-left:3px solid
  var(--line);border-radius:10px;padding:20px 24px;margin:0 0 14px;
  page-break-inside:avoid;break-inside:avoid}
.f.critical{border-left-color:var(--danger)}
.f.high{border-left-color:var(--danger);border-left-style:dashed}
.f.medium{border-left-color:var(--accent)}
.f.low{border-left-color:var(--line)}
.fhead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  margin-bottom:10px}
.n{color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
.chip{font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
  padding:3px 9px;border-radius:4px;border:1px solid var(--line);
  color:var(--muted);white-space:nowrap}
.chip.critical{color:var(--danger);border-color:var(--danger)}
.chip.high{color:var(--danger);border-color:var(--line)}
.chip.medium{color:var(--accent);border-color:var(--line)}
.meta{color:var(--muted);font-size:12.5px;margin:0 0 14px;
  word-break:break-word}
.meta code{background:#0B151D;border:1px solid var(--line);border-radius:3px;
  padding:1px 5px;font-size:11.5px}
.cost{border-left:2px solid var(--accent);padding:2px 0 2px 14px;margin:0 0 14px;
  font-size:14.5px}
.lbl{display:block;font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);margin-bottom:3px}
.body p:last-child{margin-bottom:0}
.fix{background:#0B151D;border:1px solid var(--line);border-radius:8px;
  padding:14px 16px;margin-top:14px}

.gap{border:1px dashed var(--line);border-radius:10px;padding:16px 20px;
  margin:0 0 12px;page-break-inside:avoid;break-inside:avoid}
.gap h3{font-size:14.5px}
footer{margin-top:56px;padding-top:22px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12.5px}
footer p{margin:0 0 8px}

@media (max-width:640px){
  .wrap{padding:32px 18px 64px}
  .hero{flex-direction:column;gap:20px;text-align:center;padding:24px 20px}

  .chead{flex-direction:column;gap:2px;align-items:flex-start}
}

/* Printed, this becomes a document. A dark page either eats a cartridge or
   renders as white-on-white when background printing is off — both make the
   deliverable useless, so print gets its own light treatment. */
@media print{
  :root{--ground:#fff;--panel:#fff;--line:#D6DEE4;--ink:#10202B;
        --muted:#5C6E7C;--accent:#0E8F79;--danger:#C2314F}
  body{font-size:11pt}
  .wrap{max-width:none;padding:0}
  .hero,.f,.fix,.gap{border:1px solid var(--line);background:#fff}
  .track{background:#EDF1F4}
  h2{page-break-after:avoid;break-after:avoid}
  a{color:var(--ink);text-decoration:none}
}
"""


def _esc(text) -> str:
    return html.escape(str(text), quote=False)


def _bar(score: int) -> str:
    cls = "fill bad" if score < 70 else "fill"
    return (f'<div class="track"><div class="{cls}" '
            f'style="width:{max(0, min(100, score))}%"></div></div>')


def as_html(findings: list[Finding], workflows: int, skips=None,
            account_name: str = "", generated: str = "") -> str:
    """A single self-contained page. No external requests, inline CSS only.

    Poppins is named first and falls back to Helvetica/Arial rather than being
    fetched, because a report that needs the network is a report that renders
    wrong in an email client, on a plane, and in six months.
    """
    hs = _scored(findings, workflows, skips)
    stamp = generated or datetime.date.today().isoformat()
    title = _esc(account_name) + " — " if account_name else ""
    c = hs.counts

    p: list[str] = []
    p.append("<!doctype html><html lang=\"en\"><head>")
    p.append("<meta charset=\"utf-8\">")
    p.append("<meta name=\"viewport\" content=\"width=device-width,"
             "initial-scale=1\">")
    p.append(f"<title>{title}GoHighLevel account audit</title>")
    p.append(f"<style>{CSS}</style></head><body><div class=\"wrap\">")

    p.append("<h1>GoHighLevel account audit</h1>")
    p.append(f"<p class=\"sub\">{title}{workflows} published workflows reviewed"
             f" &middot; {hs.coverage} &middot; {_esc(stamp)}</p>")

    dial_cls = "n bad" if hs.score < 70 else "n"
    tally = " &middot; ".join(
        f"<b>{c[s]}</b> {SEVERITY_LABEL[s].lower()}" for s in SEVERITIES if c[s]
    ) or "<b>no</b> findings"
    p.append(
        '<div class="hero"><div class="dial">'
        f'<div class="{dial_cls}">{hs.score}</div>'
        '<div class="of">out of 100</div>'
        f'<div class="g">Grade {hs.grade}</div></div><div>'
        f'<p class="verdict">{_esc(hs.verdict)}</p>'
        f'<p class="tally">{tally}</p></div></div>')

    # ---- categories
    p.append("<h2>Where the damage is</h2>")
    for cat in hs.categories:
        p.append('<div class="cat">')
        n = len(cat.findings)
        if cat.assessed:
            right = (f"<b>{cat.score}</b>/100 &nbsp;&middot;&nbsp; grade "
                     f"{cat.grade} &nbsp;&middot;&nbsp; {n} finding"
                     f"{'s' if n != 1 else ''}")
        else:
            right = "not assessed"
        p.append(f'<div class="chead"><span class="clabel">{_esc(cat.label)}'
                 f'</span><span class="cnum">{right}</span></div>')
        if cat.assessed:
            p.append(_bar(cat.score))
        else:
            need = ", ".join(s.rule for s in cat.skips)
            p.append(f'<p class="na">Nothing was supplied that would let these '
                     f'checks run ({_esc(need)}), so this category has no score '
                     f'— not a clean one.</p>')
        p.append(f'<p class="blurb">{_esc(cat.blurb)}</p>')
        p.append("</div>")

    # ---- findings, ranked by cost
    if hs.ranked:
        p.append("<h2>Fix in this order</h2>")
        p.append('<p class="sub">Ranked by what each one costs you, not by '
                 'check number. The first item is the one losing you the most.</p>')
        p.append('<hr class="rule">')
        for i, f in enumerate(hs.ranked, 1):
            p.append(f'<div class="f {f.severity}">')
            p.append('<div class="fhead">'
                     f'<span class="n">{i:02d}</span>'
                     f'<span class="chip {f.severity}">'
                     f'{SEVERITY_LABEL[f.severity]}</span>'
                     f'<span class="chip">{_esc(CATEGORY_LABEL[f.category])}'
                     '</span>'
                     f'<span class="chip">{_esc(f.rule)}</span></div>')
            p.append(f"<h3>{_esc(f.title)}</h3>")
            meta = f"Workflow: <code>{_esc(f.workflow)}</code>"
            if f.step:
                meta += f" &nbsp;&middot;&nbsp; Step: <code>{_esc(f.step)}</code>"
            p.append(f'<p class="meta">{meta}</p>')
            if f.cost:
                p.append('<div class="cost"><span class="lbl">What it costs'
                         f'</span>{_esc(f.cost)}</div>')
            p.append(f'<div class="body"><span class="lbl">What is happening'
                     f'</span><p>{_esc(f.symptom)}</p></div>')
            p.append('<div class="fix"><span class="lbl">How to fix it</span>'
                     f'{_esc(f.fix)}</div>')
            p.append("</div>")
    else:
        p.append("<h2>Findings</h2><p>Nothing found. Every published workflow "
                 "passed every check that ran.</p>")

    # ---- coverage gaps
    if hs.skips:
        p.append("<h2>What this audit could not check</h2>")
        p.append('<p class="sub">These are gaps in the audit, not clean bills '
                 'of health. Each one names exactly what would close it.</p>')
        for s in hs.skips:
            p.append('<div class="gap">')
            p.append(f'<h3>{_esc(s.title)} '
                     f'<span class="chip">{_esc(s.rule)}</span></h3>')
            p.append(f"<p>{_esc(s.reason)}</p>")
            if s.needs:
                p.append('<p class="meta">To run this check, supply: '
                         f"<code>{_esc(s.needs)}</code></p>")
            p.append("</div>")

    p.append("<h2>How to read this</h2>")
    p.append(
        "<p><b>The score.</b> Severity-weighted damage measured against the "
        "size of the account: a critical counts for 25, a high 12, a medium 5, "
        "a low 2, and every published workflow buys 8 points of tolerance. No "
        "single finding can fail an account on its own, and the scale means a "
        "large account is not punished for being large.</p>")
    p.append(
        "<p><b>Severity.</b> <i>Critical</i> — the account is sending customers "
        "something wrong right now. <i>High</i> — it will misfire under normal "
        "use, not just at an edge. <i>Medium</i> — it bites at scale, on "
        "handover, or on a bad day. <i>Low</i> — correctness is fine, "
        "maintenance is not.</p>")
    p.append(
        "<p><b>What this cannot see.</b> This is static analysis: it reads how "
        "the account is configured, not what it has done. It cannot tell you a "
        "workflow <i>did</i> misfire, only that it will. It cannot see contact "
        "history, so a tag trigger that never fires because contacts already "
        "carried the tag looks correct here. It cannot see execution logs, DNS, "
        "or carrier registration state. A clean report means the configuration "
        "is sound — not that nothing is wrong.</p>")

    p.append("<footer>")
    p.append(f"<p>Generated {_esc(stamp)} by ghlaudit — {len(RULES)} checks, "
             "open source. Every rule is readable before you trust its "
             "output.</p>")
    p.append("</footer>")
    p.append("</div></body></html>")
    return "\n".join(p)


RENDERERS = {"text": as_text, "markdown": as_markdown, "json": as_json,
             "html": as_html}
