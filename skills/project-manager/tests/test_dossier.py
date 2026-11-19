"""`pm.py dossier`: numbering, idempotence and the one-cell head rewrite."""

from __future__ import annotations

import json
import re
import subprocess
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


COUNTED_WORK = WORK.replace("| change \\| evidence |", "| evidence |")


def _write_run(run_dir: Path, status: str, stop: str | None, attempts: list[int], stamps: list[str | None]) -> None:
    """The `run.json` and `events.jsonl` of an archived child run; a None stamp is an event with no `ts`."""
    run_dir.mkdir(parents=True)
    slices = [{"id": f"s{n}", "title": "a | b", "attempts": count} for n, count in enumerate(attempts, 1)]
    (run_dir / "run.json").write_text(json.dumps({"status": status, "stop_reason": stop, "slices": slices}))
    events = [{"event": "e"} if stamp is None else {"event": "e", "ts": stamp} for stamp in stamps]
    (run_dir / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))


class CountsTests(DossierTestCase):
    def build(self) -> None:
        """Node rows evidence and change; node a with two timed review rounds (90 s, 60 s) and one without a
        status.json, b with one timed (10 s) and one whose finished_at is null (interrupted); child runs a/r1 (3 steers, 1h 2m 3s), a/r3 (no timestamped event)
        and b/r2 (0 steers, 45 s); from a the open question and two archived ones."""
        self.write_work(COUNTED_WORK)
        for node, number, started, finished in (
            ("a", 1, "2026-01-01T09:00:00Z", "2026-01-01T09:01:30Z"),
            ("a", 2, "2026-01-01T09:05:00Z", "2026-01-01T09:06:00Z"),
            ("a", 3, None, None),
            ("b", 1, "2026-01-01T09:00:00Z", "2026-01-01T09:00:10Z"),
            ("b", 2, "2026-01-01T09:10:00Z", None),
        ):
            rnd = self.dossier / "nodes" / node / "reviews" / f"round-{number}"
            rnd.mkdir(parents=True)
            if started:
                (rnd / "status.json").write_text(json.dumps({"started_at": started, "finished_at": finished}))
        validation = self.dossier / "validation"
        _write_run(
            validation / "a/state/r1",
            "stopped",
            "plan stop rule",
            [2, 1],
            ["2026-01-01T09:00:00Z", None, "2026-01-01T10:02:03Z"],
        )
        _write_run(validation / "a/state/r3", "stopped", "owner stop", [], [None])
        _write_run(validation / "b/state/r2", "complete", None, [0], ["2026-01-01T09:00:00Z", "2026-01-01T09:00:45Z"])
        (validation / "a/state/current").write_text("r3\n")
        (validation / "a" / "campaign").mkdir()
        for name in ("cm-question.md", "cm-question-1.md", "cm-question-2.md", "cm-answer-1.md", "cm-question-x.md"):
            (validation / "a" / "campaign" / name).write_text("q\n")

    def product_repo(self) -> Path:
        """A repository whose BASE..HEAD range changes a.py (+2 -1), adds bé.py (+3) and Makefile (+1), and a binary."""
        repo = Path(self._tmp.name) / "product"
        repo.mkdir()

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
                check=True,
                capture_output=True,
            )

        git("init", "-q", "-b", "main")
        (repo / "a.py").write_text("1\n2\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        (repo / "a.py").write_text("1\nA\nB\n")
        (repo / "bé.py").write_text("x\ny\nz\n")
        (repo / "Makefile").write_text("all:\n")
        (repo / "img.bin").write_bytes(b"\x00\x01\x02")
        git("add", "-A")
        git("commit", "-q", "-m", "change")
        return repo

    def test_every_section_counts_what_the_fixture_holds(self) -> None:
        self.build()
        repo = self.product_repo()
        code, out, err = self.run_cli(
            ["dossier", "counts", "--dossier", str(self.dossier), "--repo", str(repo), "--diff", "HEAD~1..HEAD"]
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out,
            "nodes total 2\n"
            "nodes kind change 1\n"
            "nodes kind evidence 1\n"
            "review rounds a 3 seconds 150 = 0h 2m 30s untimed 1\n"
            "review rounds b 2 seconds 10 = 0h 0m 10s untimed 1\n"
            "review rounds total 5 seconds 160 = 0h 2m 40s untimed 2\n"
            "run a r1 status stopped steers/relaunches 3 seconds 3723 = 1h 2m 3s stop plan stop rule\n"
            "run a r3 status stopped steers/relaunches 0 untimed stop owner stop\n"
            "run b r2 status complete steers/relaunches 0 seconds 45 = 0h 0m 45s stop None\n"
            "runs total 3 steers/relaunches 3 seconds 3768 = 1h 2m 48s untimed 1\n"
            "runs status complete 1\n"
            "runs status stopped 2\n"
            "cm questions a 3\n"
            "cm questions total 3\n"
            "diff files 3 added 6 deleted 1\n"
            "diff extension (none) files 1 added 1 deleted 0\n"
            "diff extension .py files 2 added 5 deleted 1\n"
            "diff binary files 1\n",
        )

    def test_a_diff_that_is_not_a_two_revision_range_is_refused(self) -> None:
        self.build()
        repo = self.product_repo()
        for diff in ("HEAD", "--output=x..HEAD"):
            with self.subTest(diff=diff), self.assertRaisesRegex(PmError, "BASE..HEAD"):
                dossier.counts(self.dossier, repo=repo, diff=diff)
        self.assertFalse((repo / "x").exists())

    def test_a_malformed_input_is_refused_naming_its_file(self) -> None:
        self.write_work(COUNTED_WORK)
        status = self.dossier / "nodes" / "a" / "reviews" / "round-1" / "status.json"
        status.parent.mkdir(parents=True)
        status.write_text(json.dumps({"started_at": "yesterday", "finished_at": "2026-01-01T09:00:10Z"}))
        code, out, err = self.run_cli(["dossier", "counts", "--dossier", str(self.dossier)])
        self.assertEqual((code, out), (2, ""))
        self.assertIn(str(status), err)


if __name__ == "__main__":
    unittest.main()
