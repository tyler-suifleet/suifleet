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
    elif kind.startswith("object-"):
        operation = "remove" if kind == "object-skip-clear" else "add"
        pattern = (r"(procedure \{:inline 2\} \$2_dynamic_object_field_" + operation
                   + r"'u64_\$51_object_fields_Child_\$51_object_fields_Parent'\(.*?\n\})")
        statements = {
            "object-omit-outer": "    t := $Update'$51_object_fields_Parent'_dynamic_fields'$2_dynamic_object_field_Wrapper'u64'_$2_object_ID'(t, AddTable(t->$dynamic_fields'$2_dynamic_object_field_Wrapper'u64'_$2_object_ID', outer, id));",
            "object-omit-child": "    t := $Update'$51_object_fields_Parent'_dynamic_fields'u64_$51_object_fields_Child'(t, AddTable(t->$dynamic_fields'u64_$51_object_fields_Child', child, v));",
            "object-skip-clear": "    t := $Update'$51_object_fields_Parent'_dynamic_fields'$2_dynamic_object_field_Wrapper'u64'_$2_object_ID'(t, RemoveTable(t->$dynamic_fields'$2_dynamic_object_field_Wrapper'u64'_$2_object_ID', outer));",
            "object-wrong-owner": "AddTable(t->$child_owners, child, $ObjectFieldOwner'u64_$51_object_fields_Parent'(t->$id->$id->$bytes, k))",
            "object-wrong-id": "    id := v->$id->$id;",
        }
        old = statements[kind]
        new = ({"object-wrong-owner": "AddTable(t->$child_owners, child, $ObjectFieldOwner'u64_$51_object_fields_Parent'(t->$id->$id->$bytes, k) + 1)",
                "object-wrong-id": "    id := $2_object_ID(0);"}.get(kind, ""))
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
