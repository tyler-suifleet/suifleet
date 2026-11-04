"""Keep authority negative controls attached to actual release implementation."""
import importlib.util
from pathlib import Path
import re
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("evaluate_move_prover", ROOT / "scripts/evaluate_move_prover.py")
PROVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROVER)


class AuthorityMutationTests(unittest.TestCase):
    def test_all_controls_mutate_only_the_selected_function(self):
        for name in PROVER.MUTATIONS:
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory:
                package = Path(directory)
                (package / "sources").mkdir()
                module, function, old, new = PROVER.MUTATIONS[name]
                path = package / f"sources/{module}.move"
                shutil.copyfile(ROOT / f"contracts/sources/{module}.move", path)
                # The evaluator renames init before attaching induction specs.
                if name == "init-untyped-identity":
                    path.write_text(path.read_text().replace(
                        "    fun init(ctx: &mut TxContext)",
                        "    fun security_actual_init(ctx: &mut TxContext)"))
                before = path.read_text()
                PROVER.mutate_implementation(package, name)
                after = path.read_text()
                # Insertions may preserve old text; other functions stay intact.
                self.assertEqual(before.count(old) - after.count(old), 1 - new.count(old))
                start = re.search(r"    (?:public(?:\(package\))? )?fun " + function + r"\(", before).start()
                end = before.index("\n    }", start)
                self.assertEqual(after, before[:start] + before[start:end].replace(old, new) + before[end:])
                if old not in new:
                    with self.assertRaisesRegex(SystemExit, "exactly one"):
                        PROVER.mutate_implementation(package, name)


if __name__ == "__main__":
    unittest.main()
