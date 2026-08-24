"""Everything that is true of ONE account, supplied by the caller.

The first version of this auditor grew out of a tool that audited a single
GoHighLevel location, and it carried that location's opinions in module-level
dicts: which workflows were allowed to re-enroll, what quiet hours each one was
supposed to have, which integration webhooks had to exist. Those maps made the
tool useless on anybody else's account, and worse, silently wrong — a workflow
the map had never heard of simply passed.

So every account-specific fact now arrives from outside, in a JSON config, and a
rule that needs one it did not get **skips loudly**. A skipped check is reported
as a gap in coverage, never as a pass. That distinction is the whole reason this
module exists: the failure mode of an audit tool is not a false positive, it is a
clean report that was never actually run.

    python -m ghlaudit account.json --config client.json

    {
      "owned_domains": ["acme.com", "book.acme.com"],
      "reentry_policy": {"Speed to Lead": false, "No Show Recovery": true},
      "send_window_policy": {
        "Long Term Nurture": {"start": "09:00", "end": "20:00",
                              "timezone": "contact"},
        "Speed to Lead": null
      },
      "required_steps": {"Lead Attribution": ["Push to reporting - booked"]},
      "transactional_workflows": ["Receipt", "Password Reset"],
      "stats_window_days": 90
    }

Every key is optional. An empty config is a valid config — it just means more
checks report as not-assessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


def _str_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _norm_name(name: str) -> str:
    """Workflow names are matched case- and whitespace-insensitively.

    A config written by hand against a screenshot will not reproduce the exact
    spacing of a name someone typed into the builder, and silently failing to
    match is the same failure the hardcoded maps had.
    """
    return " ".join(str(name).split()).strip().lower()


@dataclass
class AuditConfig:
    """Caller-supplied context. Nothing in here is guessed from the export."""

    # Domains this location legitimately sends people to. Used to tell an
    # agency's leftover link apart from a client's real one.
    owned_domains: list = field(default_factory=list)

    # workflow name -> whether re-enrollment SHOULD be on. Both directions are
    # findings: re-entry off on a nurture ignores returning leads, re-entry on a
    # speed-to-lead double-messages. Only the person who designed the account
    # knows which was intended, so it has to be told to us.
    reentry_policy: dict = field(default_factory=dict)

    # workflow name -> {"start", "end", "timezone"} or None for "no window here".
    send_window_policy: dict = field(default_factory=dict)

    # workflow name -> [step names that must exist]. A build manifest: the
    # integration steps a snapshot or a rebuild is supposed to contain.
    required_steps: dict = field(default_factory=dict)

    # Workflows that are transactional by design — receipts, password resets,
    # instant lead responses. Exempt from marketing-only checks.
    transactional_workflows: list = field(default_factory=list)

    # How far back the enrollment stats (if any) reach.
    stats_window_days: int = 90

    # ---------------------------------------------------------------- loading
    @classmethod
    def from_dict(cls, data) -> "AuditConfig":
        if not isinstance(data, dict):
            return cls()
        policy = data.get("send_window_policy") or {}
        windows = {}
        if isinstance(policy, dict):
            for k, v in policy.items():
                windows[_norm_name(k)] = v if isinstance(v, dict) else None
        reentry = {}
        raw_reentry = data.get("reentry_policy") or {}
        if isinstance(raw_reentry, dict):
            for k, v in raw_reentry.items():
                if isinstance(v, bool):
                    reentry[_norm_name(k)] = v
        required = {}
        raw_required = data.get("required_steps") or {}
        if isinstance(raw_required, dict):
            for k, v in raw_required.items():
                names = _str_list(v)
                if names:
                    required[_norm_name(k)] = names
        try:
            window_days = int(data.get("stats_window_days") or 90)
        except (TypeError, ValueError):
            window_days = 90
        return cls(
            owned_domains=[d.lower().lstrip("*.") for d in
                           _str_list(data.get("owned_domains"))],
            reentry_policy=reentry,
            send_window_policy=windows,
            required_steps=required,
            transactional_workflows=[_norm_name(n) for n in
                                     _str_list(data.get("transactional_workflows"))],
            stats_window_days=window_days,
        )

    @classmethod
    def from_file(cls, path: str) -> "AuditConfig":
        with open(path) as fh:
            return cls.from_dict(json.load(fh))

    # ---------------------------------------------------------------- lookups
    def wants_reentry(self, workflow_name: str):
        """True/False if the caller stated a policy for this workflow, else None."""
        return self.reentry_policy.get(_norm_name(workflow_name))

    def wants_window(self, workflow_name: str):
        """(configured, value) — `configured` False means we were told nothing.

        The two-tuple matters: `None` is a real, meaningful policy value ("this
        workflow must NOT have a send window"), so it cannot double as absent.
        """
        key = _norm_name(workflow_name)
        if key not in self.send_window_policy:
            return False, None
        return True, self.send_window_policy[key]

    def required_step_names(self, workflow_name: str) -> list:
        return self.required_steps.get(_norm_name(workflow_name), [])

    def is_transactional(self, workflow_name: str) -> bool:
        return _norm_name(workflow_name) in self.transactional_workflows

    def owns_host(self, host: str) -> bool:
        """Is this hostname one of the client's own?

        Subdomains count: `book.acme.com` is owned when `acme.com` is listed.
        """
        host = (host or "").lower().strip().rstrip(".")
        if not host:
            return False
        for owned in self.owned_domains:
            if host == owned or host.endswith("." + owned):
                return True
        return False

    @property
    def has_owned_domains(self) -> bool:
        return bool(self.owned_domains)
