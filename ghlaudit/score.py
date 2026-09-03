"""Turn a list of findings into a number a business owner can act on.

A raw finding count is not a grade. Twelve findings on a sixty-workflow account
is a well-run account; twelve on a six-workflow account is a fire. And "we found
nothing in this category" has to be distinguishable from "we could not check
this category", or the score becomes a way of hiding the gaps.

The formula, stated plainly so it can be argued with:

    root cause = one rule, everywhere it fires on the account
    damage     = for each root cause, severity weight * sqrt(number of sites)
    tolerance  = 8 points per published workflow (min 8), split across the
                 categories in proportion to the rules that can fire in each
    penalty    = 100 * damage / (damage + tolerance)
    score      = 100 - penalty

That is a saturating curve over root causes, chosen for four properties:

  * **A habit is one defect, not N.** An account that never handles a webhook
    failure has made one decision, whether it is wrong in one workflow or in
    thirteen. Charging it thirteen times reports thirteen problems where a
    person has one thing to fix, and grades an account with a single systemic
    habit below an account with a dozen unrelated defects. The repeats are
    still real - thirteen workflows burn more leads than one - so they are
    priced at sqrt(sites): rising, but with diminishing returns. The ordering
    that matters survives it, a high-severity habit across 13 workflows (43)
    still outweighing one isolated critical (25).
  * **No single finding can zero the account.** One critical on a healthy
    account is a bad day, not a failing grade, and a scoring model that says
    otherwise gets ignored the first time it overreacts.
  * **It never reaches 0.** There is always a worse account. A score of 4 and a
    score of 0 should not look the same.
  * **It scales with size.** A large account is allowed proportionally more
    findings before its grade moves, because it has proportionally more surface.

The category scores and the headline are drawn on one budget. They used to be
drawn on five: every category was scored against the *whole* account's
tolerance while the headline was scored against that same figure for all five
categories at once, so the same allowance was spent five times over and a
report could show two categories at A sitting above a headline F. A tool that
argues with itself in the same table does not get believed about either number.

The weights are deliberately far apart — a critical is worth eight mediums —
because in this domain they genuinely are. A workflow texting customers the
wrong thing today is not the same kind of problem as a deprecated action.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .rules import CATEGORIES, RULES, SEVERITIES

WEIGHT = {"critical": 25, "high": 12, "medium": 5, "low": 2}

# Four points of damage per published workflow before the grade starts moving.
#
# This was eight, and eight was not derived from anything. Four is, so here is
# the derivation, which is the part that can be argued with:
#
#   An A claims "well built - what is left is maintenance, not repair." That
#   describes an account with a couple of low-severity notes and nothing else:
#   call it three lows, six points of damage, on a thirteen-workflow account.
#   For six damage to still score 90 the tolerance has to be 54, or 4.2 per
#   workflow.
#
#   A B claims "sound, with real defects, none of them on fire" - a handful of
#   mediums, say four, twenty points. For twenty damage to score 80 the
#   tolerance has to be 80, or 6.2 per workflow.
#
# The two anchors disagree, so the stricter one wins: a scale is only as
# honest as its most demanding claim. Four per workflow, rounded down from 4.2.
TOLERANCE_PER_WORKFLOW = 4

# How much of the catalog can fire in each category. This is the share of the
# tolerance budget the category gets, so a 10-rule category is not measured
# against the same allowance as a 54-rule one.
RULES_PER_CATEGORY = {c: sum(1 for r in RULES if r.category == c)
                      for c in CATEGORIES}

CATEGORY_LABEL = {
    "compliance": "Compliance",
    "deliverability": "Deliverability",
    "routing": "Routing",
    "hygiene": "Hygiene",
    "dead_weight": "Dead weight",
}

CATEGORY_BLURB = {
    "compliance": "What the law and the carriers require of you: opt-out "
                  "language, unsubscribe links, quiet hours.",
    "deliverability": "Whether the message physically arrives: sending-domain "
                      "authentication, throttling, carrier filtering risk.",
    "routing": "Whether the right person gets the right message at the right "
               "moment: triggers, branches, exits, waits, wiring.",
    "hygiene": "Content and maintainability: placeholder text, merge fields "
               "that render blank, hardcoded endpoints, dead references.",
    "dead_weight": "Things that exist and do nothing: workflows nothing enrolls "
                   "in, steps nothing can reach, triggers nothing fires.",
}

GRADES = ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F"))

# Ceilings that no amount of good elsewhere can lift. Each one exists because
# a grade is a claim, and there are claims the arithmetic must not be allowed
# to make on the account's behalf.
#
# This is NOT the "no single finding can zero the account" principle being
# reversed. That principle is about the raw curve, and it still holds - one
# critical still leaves the other ninety-nine checks visible in the number. A
# ceiling is a different statement: not "you scored badly" but "you cannot be
# described this way while this is true."
CRITICAL_CEILING = 59   # top of F
HIGH_CEILING = 89       # top of B

VERDICT = {
    "A": "Well built. What is left is maintenance, not repair.",
    "B": "Sound, with real defects. None of them are on fire.",
    "C": "Working, but leaking. Fix the criticals before adding anything new.",
    "D": "This account is costing money in ways nobody has attributed to it yet.",
    "F": "Customers are receiving the wrong messages right now. Stop and fix "
         "before the next campaign.",
}


def grade_for(score: int) -> str:
    for floor, letter in GRADES:
        if score >= floor:
            return letter
    return "F"  # pragma: no cover - unreachable, GRADES ends at 0


def _tolerance(workflows: int, category: str = "") -> float:
    """How much damage an account this size absorbs before the grade moves.

    One budget for the account, divided among the categories by how much of the
    rule catalog can fire in each. Passing no category asks for the whole
    budget, which is what the headline is scored against.
    """
    total = max(TOLERANCE_PER_WORKFLOW, TOLERANCE_PER_WORKFLOW * workflows)
    if not category:
        return total
    share = RULES_PER_CATEGORY.get(category, 0)
    if not share:
        return total
    return total * share / len(RULES)


def _score(damage: float, tolerance: float) -> int:
    if damage <= 0:
        return 100
    raw = 100 - (100.0 * damage / (damage + tolerance))
    # The curve approaches 0 but never touches it; rounding would, and a 0 reads
    # as "nothing here works", which is a claim no finite set of findings
    # supports. 1 is the floor, and it still says everything a 0 would.
    return max(1, int(round(raw)))


@dataclass
class Ceiling:
    """A cap on the grade, and the sentence that says why.

    Reported, never silent. A score that has been held down without saying so
    is a score nobody can act on — and the reason is usually the most useful
    line in the whole report, because it names the one thing standing between
    this account and a better grade.
    """

    limit: int
    reason: str
    lift: str

    def to_dict(self) -> dict:
        return {"limit": self.limit, "reason": self.reason, "lift": self.lift}


def ceilings(findings: list, skips: list) -> list:
    """Every ceiling that applies, worst first.

    Three of them, and each answers a question the raw curve cannot:

    **Coverage.** A check that could not run costs nothing today, which means
    an account can be graded A while a tenth of the audit never examined
    anything. "We found nothing" and "we could not look" have to be different
    numbers, or the grade is partly a measure of how little was supplied. The
    category scores already refuse to report an unassessed category as a clean
    one; this is the same rule, finally applied to the headline.

    **A live critical.** The F verdict reads "customers are receiving the wrong
    messages right now." An account doing that cannot also be passing, however
    tidy the rest of it is.

    **A live high.** The A verdict reads "what is left is maintenance, not
    repair." A high-severity finding is repair by definition — the catalog
    defines high as "it will misfire under normal use, not just at an edge."
    """
    out = []
    counts: dict = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    ran = len(RULES) - len({s.rule for s in skips})
    if ran < len(RULES):
        missing = len(RULES) - ran
        needs = sorted({s.needs for s in skips if getattr(s, "needs", "")})
        out.append(Ceiling(
            limit=int(round(100.0 * ran / len(RULES))),
            reason=f"{missing} of {len(RULES)} checks could not run, so this "
                   f"account has not been fully examined. A grade cannot claim "
                   f"more certainty than the audit had.",
            lift="Supply what the skipped checks need and re-run — "
                 + ("; ".join(needs[:3]) + ("; …" if len(needs) > 3 else "")
                    if needs else "see the coverage section.")))

    if counts.get("critical"):
        n = counts["critical"]
        out.append(Ceiling(
            limit=CRITICAL_CEILING,
            reason=f"{n} critical finding{'s' if n != 1 else ''} still open. "
                   "A critical means customers are receiving the wrong "
                   "messages right now; an account doing that is not passing, "
                   "whatever the rest of it looks like.",
            lift="Clear the critical findings. Nothing else moves this."))
    elif counts.get("high"):
        n = counts["high"]
        out.append(Ceiling(
            limit=HIGH_CEILING,
            reason=f"{n} high-severity finding{'s' if n != 1 else ''} still "
                   "open. An A says what is left is maintenance rather than "
                   "repair, and a high-severity defect is repair — it "
                   "misfires under normal use, not only at an edge.",
            lift="Clear the high-severity findings to make an A reachable."))

    return sorted(out, key=lambda c: c.limit)


@dataclass
class RootCause:
    """One rule, and every place on the account where it fires.

    This is the unit a person actually fixes. "You never handle a webhook
    failure" is one decision to make whether it is true in one workflow or in
    thirteen, and a report that lists it thirteen times is thirteen lines the
    reader has to collapse in their head before they can act.
    """

    rule: str
    severity: str
    category: str
    findings: list = field(default_factory=list)

    @property
    def sites(self) -> int:
        return len(self.findings)

    @property
    def damage(self) -> float:
        return WEIGHT[self.severity] * math.sqrt(self.sites)

    @property
    def title(self) -> str:
        return self.findings[0].title if self.findings else self.rule

    @property
    def workflows(self) -> list:
        """The distinct workflows this fires in, first seen first."""
        out = []
        for f in self.findings:
            if f.workflow and f.workflow not in out:
                out.append(f.workflow)
        return out

    def to_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity,
                "category": self.category, "title": self.title,
                "sites": self.sites, "workflows": self.workflows,
                "damage": round(self.damage, 1)}


def root_causes(findings: list) -> list:
    """Collapse findings onto the rule that produced them, worst first."""
    groups: dict = {}
    for f in findings:
        g = groups.get(f.rule)
        if g is None:
            g = groups[f.rule] = RootCause(rule=f.rule, severity=f.severity,
                                           category=f.category)
        g.findings.append(f)
    return sorted(groups.values(),
                  key=lambda g: (-g.damage, SEVERITIES.index(g.severity),
                                 g.rule))


@dataclass
class CategoryScore:
    key: str
    label: str
    blurb: str
    score: int
    findings: list = field(default_factory=list)
    skips: list = field(default_factory=list)
    assessed: bool = True
    roots: list = field(default_factory=list)

    @property
    def grade(self) -> str:
        return grade_for(self.score)

    @property
    def counts(self) -> dict:
        out = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            out[f.severity] += 1
        return out

    def to_dict(self) -> dict:
        return {"category": self.key, "label": self.label,
                "score": self.score if self.assessed else None,
                "grade": self.grade if self.assessed else None,
                "assessed": self.assessed, "findings": len(self.findings),
                "root_causes": len(self.roots),
                "skipped_checks": [s.rule for s in self.skips],
                "counts": self.counts}


@dataclass
class HealthScore:
    score: int
    workflows: int
    findings: list
    skips: list
    categories: list
    roots: list = field(default_factory=list)
    caps: list = field(default_factory=list)
    uncapped: int = 0

    @property
    def grade(self) -> str:
        return grade_for(self.score)

    @property
    def verdict(self) -> str:
        return VERDICT[self.grade]

    @property
    def counts(self) -> dict:
        out = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            out[f.severity] += 1
        return out

    @property
    def ranked(self) -> list:
        """Findings ordered by what they cost, not by rule id.

        This is the ordering the client reads. A rule catalog is organised for
        whoever maintains it; a report has to be organised for whoever pays for
        the fixes.
        """
        return sorted(self.findings, key=lambda f: f.cost_key())

    @property
    def fix_order(self) -> list:
        """The ranked list, one entry per root cause.

        `ranked` is every finding, worst first, and the detail sections need
        that. The *fix in this order* list is a to-do list, and a to-do list
        that says the same job four times is a worse to-do list. Each entry is
        the worst-costing finding of its root cause, so the wording stays
        concrete about a real workflow rather than going abstract.
        """
        best: dict = {}
        for f in self.ranked:
            best.setdefault(f.rule, f)
        by_rule = {g.rule: g for g in self.roots}
        return [(f, by_rule[f.rule].sites) for f in best.values()]

    @property
    def coverage(self) -> str:
        ran = len(RULES) - len({s.rule for s in self.skips})
        return f"{ran} of {len(RULES)} checks ran"

    @property
    def capped(self) -> bool:
        """Is the reported score lower than the curve alone produced?"""
        return bool(self.caps) and self.score < self.uncapped

    @property
    def binding_cap(self):
        """The ceiling actually holding the score down, if any."""
        return next((c for c in self.caps if c.limit <= self.score), None)

    def to_dict(self) -> dict:
        return {
            "score": self.score, "grade": self.grade, "verdict": self.verdict,
            "uncapped_score": self.uncapped,
            "capped": self.capped,
            "ceilings": [c.to_dict() for c in self.caps],
            "workflows_audited": self.workflows,
            "counts": self.counts,
            "root_causes": len(self.roots),
            "root_cause_detail": [g.to_dict() for g in self.roots],
            "checks_run": len(RULES) - len({s.rule for s in self.skips}),
            "checks_total": len(RULES),
            "categories": [c.to_dict() for c in self.categories],
        }


def health(findings: list, skips: list, workflows: int) -> HealthScore:
    """Grade an audit. `workflows` is the number audited, not the number broken."""
    workflows = max(0, int(workflows))
    roots = root_causes(findings)
    damage = sum(g.damage for g in roots)
    uncapped = _score(damage, _tolerance(workflows))
    caps = ceilings(findings, skips)
    overall = min([uncapped] + [c.limit for c in caps])

    # Which rules could have contributed to each category at all. Needed so a
    # category whose every check was skipped reports as not assessed instead of
    # as a perfect score — the difference between "clean" and "unexamined".
    rules_by_cat: dict = {c: set() for c in CATEGORIES}
    for r in RULES:
        rules_by_cat[r.category].add(r.id)
    for s in skips:
        rules_by_cat.setdefault(s.category, set()).add(s.rule)

    cats = []
    for key in CATEGORIES:
        mine = [f for f in findings if f.category == key]
        my_roots = [g for g in roots if g.category == key]
        my_skips = [s for s in skips if s.category == key]
        cat_damage = sum(g.damage for g in my_roots)
        possible = rules_by_cat.get(key, set())
        skipped_ids = {s.rule for s in my_skips}
        assessed = bool(mine) or not (possible and possible <= skipped_ids)
        cats.append(CategoryScore(
            key=key, label=CATEGORY_LABEL[key], blurb=CATEGORY_BLURB[key],
            score=_score(cat_damage, _tolerance(workflows, key)),
            findings=mine, skips=my_skips, assessed=assessed,
            roots=my_roots))

    return HealthScore(score=overall, workflows=workflows, findings=findings,
                       skips=skips, categories=cats, roots=roots,
                       caps=caps, uncapped=uncapped)
