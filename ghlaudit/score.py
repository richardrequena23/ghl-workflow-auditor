"""Turn a list of findings into a number a business owner can act on.

A raw finding count is not a grade. Twelve findings on a sixty-workflow account
is a well-run account; twelve on a six-workflow account is a fire. And "we found
nothing in this category" has to be distinguishable from "we could not check
this category", or the score becomes a way of hiding the gaps.

The formula, stated plainly so it can be argued with:

    damage    = sum of severity weights across the findings
    tolerance = 8 points per published workflow (min 8)
    penalty   = 100 * damage / (damage + tolerance)
    score     = 100 - penalty

That is a saturating curve, chosen for three properties:

  * **No single finding can zero the account.** One critical on a healthy
    account is a bad day, not a failing grade, and a scoring model that says
    otherwise gets ignored the first time it overreacts.
  * **It never reaches 0.** There is always a worse account. A score of 4 and a
    score of 0 should not look the same.
  * **It scales with size.** A large account is allowed proportionally more
    findings before its grade moves, because it has proportionally more surface.

The weights are deliberately far apart — a critical is worth eight mediums —
because in this domain they genuinely are. A workflow texting customers the
wrong thing today is not the same kind of problem as a deprecated action.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .rules import CATEGORIES, RULES, SEVERITIES

WEIGHT = {"critical": 25, "high": 12, "medium": 5, "low": 2}
TOLERANCE_PER_WORKFLOW = 8

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


def _score(damage: float, workflows: int) -> int:
    tolerance = max(TOLERANCE_PER_WORKFLOW, TOLERANCE_PER_WORKFLOW * workflows)
    if damage <= 0:
        return 100
    raw = 100 - (100.0 * damage / (damage + tolerance))
    # The curve approaches 0 but never touches it; rounding would, and a 0 reads
    # as "nothing here works", which is a claim no finite set of findings
    # supports. 1 is the floor, and it still says everything a 0 would.
    return max(1, int(round(raw)))


@dataclass
class CategoryScore:
    key: str
    label: str
    blurb: str
    score: int
    findings: list = field(default_factory=list)
    skips: list = field(default_factory=list)
    assessed: bool = True

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
                "skipped_checks": [s.rule for s in self.skips],
                "counts": self.counts}


@dataclass
class HealthScore:
    score: int
    workflows: int
    findings: list
    skips: list
    categories: list

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
    def coverage(self) -> str:
        ran = len(RULES) - len({s.rule for s in self.skips})
        return f"{ran} of {len(RULES)} checks ran"

    def to_dict(self) -> dict:
        return {
            "score": self.score, "grade": self.grade, "verdict": self.verdict,
            "workflows_audited": self.workflows,
            "counts": self.counts,
            "checks_run": len(RULES) - len({s.rule for s in self.skips}),
            "checks_total": len(RULES),
            "categories": [c.to_dict() for c in self.categories],
        }


def health(findings: list, skips: list, workflows: int) -> HealthScore:
    """Grade an audit. `workflows` is the number audited, not the number broken."""
    workflows = max(0, int(workflows))
    damage = sum(WEIGHT[f.severity] for f in findings)
    overall = _score(damage, workflows)

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
        my_skips = [s for s in skips if s.category == key]
        cat_damage = sum(WEIGHT[f.severity] for f in mine)
        possible = rules_by_cat.get(key, set())
        skipped_ids = {s.rule for s in my_skips}
        assessed = bool(mine) or not (possible and possible <= skipped_ids)
        cats.append(CategoryScore(
            key=key, label=CATEGORY_LABEL[key], blurb=CATEGORY_BLURB[key],
            score=_score(cat_damage, workflows), findings=mine,
            skips=my_skips, assessed=assessed))

    return HealthScore(score=overall, workflows=workflows, findings=findings,
                       skips=skips, categories=cats)
