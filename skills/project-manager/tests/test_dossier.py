"""`pm.py dossier`: numbering, idempotence and the one-cell head rewrite."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pm_test_helpers import PlanTestCase

from pm_lib import PmError
from pm_lib import dossier

SHA_A = "a" * 40
SHA_B = "b" * 40

WORK = """# Work: demo

## Now
- In flight: none

## Nodes
| Id | Kind | Objective | Target | Status | Head SHA |
|---|---|---|---|---|---|
| N1 | change \\| evidence | first, with a `\\|` in code | T1 |   open   | <SHA> |
| N2 | change | second | T1 | open | %(a)s |

## Later
| Id | Head SHA |
|---|---|
| N1 | untouched |
""" % {"a": SHA_A}

DECISION = dict(title="Launch N1", actor="CM", decision="launch N1", grounds="E1", permission="Budgets")
EVIDENCE = dict(title="Finding one", finding="it leaks", source="a.py:3", sha=SHA_A, uncertainty="none")


class DossierTestCase(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dossier = Path(self._tmp.name) / "dossier"
        self.dossier.mkdir()

    def write_work(self, text: str = WORK) -> Path:
        path = self.dossier / "work.md"
        path.write_bytes(text.encode("utf-8"))
        return path


class AddEntryTests(DossierTestCase):
    def test_numbering_from_an_empty_file(self) -> None:
        self.assertEqual(dossier.add_decision(self.dossier, **DECISION), (1, True))
        text = (self.dossier / "decisions.md").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"\A## D1: Launch N1\n- At: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n- Actor: CM\n- Decision: launch N1\n"
            r"- Grounds: E1\n- Permission used: Budgets\n\Z",
        )

    def test_numbering_after_existing_entries_with_one_blank_line_between(self) -> None:
        existing = "# Decisions\n\n## D1: old\n- Actor: CM\n\n## D7: older\n- Actor: owner\n\n\n"
        (self.dossier / "decisions.md").write_text(existing, encoding="utf-8")
        number, new = dossier.add_decision(self.dossier, **DECISION)
        self.assertEqual((number, new), (8, True))
        text = (self.dossier / "decisions.md").read_text(encoding="utf-8")
        self.assertTrue(
            text.startswith(
                "# Decisions\n\n## D1: old\n- Actor: CM\n\n## D7: older\n- Actor: owner\n\n## D8: Launch N1\n"
            )
        )

    def test_evidence_uses_its_own_numbers_and_fields(self) -> None:
        self.assertEqual(dossier.add_evidence(self.dossier, **EVIDENCE), (1, True))
        self.assertEqual(dossier.add_evidence(self.dossier, **{**EVIDENCE, "title": "Two"}), (2, True))
        text = (self.dossier / "evidence.md").read_text(encoding="utf-8")
        self.assertIn(f"- Finding: it leaks\n- Source: a.py:3\n- SHA read at: {SHA_A}\n- Uncertainty: none\n", text)
        self.assertFalse((self.dossier / "decisions.md").exists())

    def test_identical_repeat_is_a_byte_identical_no_op(self) -> None:
        dossier.add_decision(self.dossier, **DECISION)
        before = (self.dossier / "decisions.md").read_bytes()
        self.assertEqual(dossier.add_decision(self.dossier, **DECISION), (1, False))
        self.assertEqual((self.dossier / "decisions.md").read_bytes(), before)
        dossier.add_evidence(self.dossier, **EVIDENCE)
        before = (self.dossier / "evidence.md").read_bytes()
        self.assertEqual(dossier.add_evidence(self.dossier, **EVIDENCE), (1, False))
        self.assertEqual((self.dossier / "evidence.md").read_bytes(), before)

    def test_repeat_finds_an_earlier_entry_not_only_the_last(self) -> None:
        dossier.add_decision(self.dossier, **DECISION)
        dossier.add_decision(self.dossier, **{**DECISION, "title": "Other"})
        self.assertEqual(dossier.add_decision(self.dossier, **DECISION), (1, False))

    def test_a_differing_field_or_title_gets_a_new_number(self) -> None:
        dossier.add_decision(self.dossier, **DECISION)
        for change in ({"grounds": "E2"}, {"actor": "owner"}, {"title": "Launch N2"}, {"permission": "Other"}):
            with self.subTest(change=change):
                number, new = dossier.add_decision(self.dossier, **{**DECISION, **change})
                self.assertTrue(new)
                self.assertGreater(number, 1)

    def test_bad_actor_blank_and_multiline_values_are_refused(self) -> None:
        with self.assertRaisesRegex(PmError, "actor"):
            dossier.add_decision(self.dossier, **{**DECISION, "actor": "pm"})
        with self.assertRaisesRegex(PmError, "blank"):
            dossier.add_decision(self.dossier, **{**DECISION, "grounds": "  "})
        with self.assertRaisesRegex(PmError, "single line"):
            dossier.add_evidence(self.dossier, **{**EVIDENCE, "finding": "a\n## E9: forged"})
        self.assertFalse((self.dossier / "decisions.md").exists())
        self.assertFalse((self.dossier / "evidence.md").exists())

    def test_missing_dossier_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(PmError, "does not exist"):
                dossier.add_decision(Path(tmp) / "nope", **DECISION)


class SetHeadTests(DossierTestCase):
    def test_changes_only_the_one_cell(self) -> None:
        path = self.write_work()
        self.assertTrue(dossier.set_head(self.dossier, node="N1", head=SHA_B))
        after = path.read_text(encoding="utf-8")
        self.assertEqual(after, WORK.replace("|   open   | <SHA> |", f"|   open   | {SHA_B} |"))

    def test_second_row_and_idempotence(self) -> None:
        path = self.write_work()
        self.assertTrue(dossier.set_head(self.dossier, node="N2", head=SHA_B))
        once = path.read_bytes()
        self.assertFalse(dossier.set_head(self.dossier, node="N2", head=SHA_B))
        self.assertEqual(path.read_bytes(), once)
        self.assertEqual(once.decode("utf-8"), WORK.replace(SHA_A, SHA_B))

    def test_crlf_line_endings_survive(self) -> None:
        path = self.write_work(WORK.replace("\n", "\r\n"))
        dossier.set_head(self.dossier, node="N1", head=SHA_B)
        self.assertEqual(path.read_bytes().decode("utf-8"), WORK.replace("<SHA>", SHA_B).replace("\n", "\r\n"))

    def test_missing_row_column_table_and_heading_are_refused(self) -> None:
        self.write_work()
        with self.assertRaisesRegex(PmError, "no row with Id 'N9'"):
            dossier.set_head(self.dossier, node="N9", head=SHA_B)
        self.write_work(
            WORK.replace("| Head SHA |\n|---|---|---|---|---|---|", "| Tip |\n|---|---|---|---|---|---|", 1)
        )
        with self.assertRaisesRegex(PmError, "no 'Head SHA' column"):
            dossier.set_head(self.dossier, node="N1", head=SHA_B)
        self.write_work(re.sub(r"(?m)^\|.*\n", "", WORK.split("## Later")[0]))
        with self.assertRaisesRegex(PmError, "no Markdown table"):
            dossier.set_head(self.dossier, node="N1", head=SHA_B)
        self.write_work("# Work\n")
        with self.assertRaisesRegex(PmError, "no '## Nodes' heading"):
            dossier.set_head(self.dossier, node="N1", head=SHA_B)
        (self.dossier / "work.md").unlink()
        with self.assertRaisesRegex(PmError, "no work.md"):
            dossier.set_head(self.dossier, node="N1", head=SHA_B)

    def test_short_or_non_hex_head_is_refused(self) -> None:
        path = self.write_work()
        before = path.read_bytes()
        for head in ("abc1234", "A" * 40, "g" * 40, ""):
            with self.subTest(head=head), self.assertRaisesRegex(PmError, "40-hex"):
                dossier.set_head(self.dossier, node="N1", head=head)
        self.assertEqual(path.read_bytes(), before)

    def test_duplicate_id_is_refused(self) -> None:
        self.write_work(WORK.replace("| N2 |", "| N1 |", 1))
        with self.assertRaisesRegex(PmError, "2 rows"):
            dossier.set_head(self.dossier, node="N1", head=SHA_B)


class DossierCliTests(DossierTestCase):
    def test_round_trip_through_main(self) -> None:
        argv = [
            "dossier", "add-decision", "--dossier", str(self.dossier), "--title", "Launch N1", "--actor", "CM",
            "--decision", "launch N1", "--grounds", "E1", "--permission", "Budgets",
        ]  # fmt: skip
        self.assertEqual(self.run_cli(argv), (0, "D1: recorded\n", ""))
        self.assertEqual(self.run_cli(argv), (0, "D1: already recorded\n", ""))

        self.write_work()
        head = ["dossier", "set-head", "--dossier", str(self.dossier), "--node", "N1", "--head", SHA_B]
        self.assertEqual(self.run_cli(head), (0, "head SHA set for N1\n", ""))
        self.assertEqual(self.run_cli(head), (0, f"head SHA already {SHA_B} for N1\n", ""))

        code, out, err = self.run_cli(head[:-1] + ["abc"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("pm: error:", err)


if __name__ == "__main__":
    unittest.main()
