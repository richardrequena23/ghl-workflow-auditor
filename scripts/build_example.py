#!/usr/bin/env python3
"""Rebuild examples/broken-account.json from its parts.

The shipped example is not a sample — it is the proof. `ExampleAccount` asserts
that every rule in the catalog fires on it and that no check skips, so the file
has to grow by hand every time a rule is added. With a hundred rules and several
people adding packs at once, one shared JSON file is a merge conflict on every
branch.

So the example is assembled: `examples/broken-account.json` is the base (the
original twenty-four workflows that carry GHL001-GHL052), and each pack drops a
fragment in `examples/packs/<pack>.json` holding only the workflows its own rules
need. This script merges them into `examples/broken-account.json`.

    python3 scripts/build_example.py            # rebuild
    python3 scripts/build_example.py --check    # fail if the file is stale

A fragment is a normal account bundle. Recognised keys:

    workflows      list, appended
    customValues   dict, merged (a later pack must not redefine a key)
    customFields / calendars / users / pipelines / forms /
    emailTemplates / emailDomains / emailSettings / stats
                   dict merged, list appended, same collision rule

Workflow names must be unique across every fragment: `Account.load` keys some
lookups by name, and two workflows called "Nurture" make a rule's finding point
at whichever one parsed last. The merge refuses rather than let that through.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLES = os.path.join(ROOT, "examples")
BASE = os.path.join(EXAMPLES, "base-account.json")
PACKS = os.path.join(EXAMPLES, "packs")
OUT = os.path.join(EXAMPLES, "broken-account.json")

LIST_KEYS = ("workflows", "emailDomains", "phoneNumbers")


def _merge(dst: dict, src: dict, origin: str) -> None:
    for key, value in src.items():
        if key not in dst:
            dst[key] = json.loads(json.dumps(value))
            continue
        if isinstance(dst[key], list) and isinstance(value, list):
            dst[key].extend(value)
        elif isinstance(dst[key], dict) and isinstance(value, dict):
            clash = sorted(set(dst[key]) & set(value))
            if clash:
                raise SystemExit(
                    f"{origin}: redefines {key} entries already present: {clash}. "
                    "Pick names unique to your pack — a silent overwrite here "
                    "changes what an existing rule sees.")
            dst[key].update(value)
        else:
            raise SystemExit(f"{origin}: cannot merge {key!r} "
                             f"({type(dst[key]).__name__} vs {type(value).__name__})")


def build() -> dict:
    if not os.path.exists(BASE):
        raise SystemExit(
            f"missing {BASE}. Create it by copying the current "
            f"examples/broken-account.json — that file is the generated output "
            f"now, and the base is its first input.")
    with open(BASE) as fh:
        merged = json.load(fh)

    fragments = []
    if os.path.isdir(PACKS):
        fragments = sorted(f for f in os.listdir(PACKS) if f.endswith(".json"))
    for name in fragments:
        path = os.path.join(PACKS, name)
        with open(path) as fh:
            _merge(merged, json.load(fh), origin=f"examples/packs/{name}")

    seen: dict[str, str] = {}
    for wf in merged.get("workflows", []):
        wf_name = wf.get("name") or "(unnamed)"
        if wf_name in seen:
            raise SystemExit(
                f"duplicate workflow name {wf_name!r} in the merged example. "
                f"Account.load keys lookups by name; rename one of them.")
        seen[wf_name] = "base"
    return merged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if broken-account.json is out of date")
    args = ap.parse_args(argv)

    merged = build()
    text = json.dumps(merged, indent=2) + "\n"

    if args.check:
        current = open(OUT).read() if os.path.exists(OUT) else ""
        if current != text:
            print("examples/broken-account.json is STALE — run "
                  "python3 scripts/build_example.py", file=sys.stderr)
            return 1
        print(f"examples/broken-account.json is current "
              f"({len(merged.get('workflows', []))} workflows)")
        return 0

    with open(OUT, "w") as fh:
        fh.write(text)
    print(f"wrote {os.path.relpath(OUT, ROOT)} — "
          f"{len(merged.get('workflows', []))} workflows from "
          f"base + {len(os.listdir(PACKS)) if os.path.isdir(PACKS) else 0} fragments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
