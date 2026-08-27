"""Rule packs — the catalog past GHL052, split so it stays readable.

`rules.py` grew to 166 KB carrying the first fifty-two checks in one file. That
was fine while the catalog was small and one person held all of it in their head;
it is not fine at a hundred. Everything from GHL053 on lives here, one module per
failure family, and every module registers into the same `RULES` list through the
same `@rule` decorator. There is no second engine and no second contract — a pack
rule is indistinguishable from an original one once it is registered.

Each module is imported for its side effect (the decorator appends to RULES), so
discovery is automatic: drop a `.py` in this directory and its rules are in the
catalog. That is deliberate. The alternative — a hand-maintained import list —
is a place to forget a pack, and a forgotten pack is a check that silently never
runs, which is the one failure this tool cannot afford.

Import order is alphabetical and therefore stable, but nothing may depend on it:
`test_rule_ids_are_contiguous_and_correctly_formatted` sorts by id before it
checks, and `run_all` sorts findings by severity. Ordering here is for humans
reading a diff, not for behaviour.
"""

from __future__ import annotations

import importlib
import pkgutil

__all__ = ["loaded"]

#: Module names that were imported, in load order. Handy in a REPL when a rule
#: is registered and you want to know which file put it there.
loaded: list[str] = []

for _finder, _name, _ispkg in sorted(pkgutil.iter_modules(__path__),
                                     key=lambda m: m[1]):
    if _name.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{_name}")
    loaded.append(_name)
