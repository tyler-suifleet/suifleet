#!/usr/bin/env python3
"""Deliberately break one emitted native transition, then execute real Boogie.

Used only for negative controls, never for positive proofs. Exact-match guards
ensure a compiler/model error cannot masquerade as a solver counterexample.
"""
import os
from pathlib import Path
import re
import sys


def mutate(text, kind):
    if kind == "missing-tag-clear":
        pattern = r"(procedure \{:inline 2\} \$2_dynamic_field_remove'u64_u64_\$2_object_UID'\(.*?\n\})"
        old = "RemoveTable(t->$dynamic_tags'u64', enc_k)"
        new = "t->$dynamic_tags'u64'"
    elif kind == "wrong-type-alias":
        pattern = r"(function \{:inline\} \$2_dynamic_field_exists_with_type'u64_bool_\$2_object_UID'\(.*?\n\})"
        old = "t->$dynamic_fields'u64_bool'"
        new = "t->$dynamic_fields'u64_u64'"
    else:
        raise ValueError("Unknown native model control")
    matches = list(re.finditer(pattern, text, re.S))
    if len(matches) != 1 or matches[0].group().count(old) != 1:
        raise ValueError("Native model control did not match exactly one intended transition")
    match = matches[0]
    return text[:match.start()] + match.group().replace(old, new) + text[match.end():]


def main():
    paths = [Path(a) for a in sys.argv[1:] if a.endswith(".bpl")]
    if paths:
        if len(paths) != 1:
            raise SystemExit("Expected one actual solver input")
        path = paths[0]
        path.write_text(mutate(path.read_text(), os.environ["SF050_NATIVE_CONTROL"]))
    os.execv(os.environ["SF050_REAL_BOOGIE"], [os.environ["SF050_REAL_BOOGIE"], *sys.argv[1:]])


if __name__ == "__main__":
    main()
