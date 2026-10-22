"""Protected behaviours: the plan parser, check-plan report, and risk derivation.

One class per contract area, each test naming the single frozen scenario it
pins. Two conventions run throughout: check-plan reports every defect it
finds rather than stopping at the first, and the risk flags fail in opposite
directions on purpose — an unclear approval flag blocks, while an unclear
`Independent audit required:` leaves the opt-in gate unarmed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import unittest
from pathlib import Path

# PlanTestCase, not PmTestCase: every test in this module treats `repo` as a
# plain directory to write a plan into. None of them read git state, and the
# git fixture cost ~165ms per test — more than the parsing they exist to test.
from pm_test_helpers import PlanTestCase, render_slice

from pm_lib import PmError
from pm_lib import plan as plan_mod


class TestCheckPlanCleanAndMultiDefect(PlanTestCase):
    def test_clean_plan_passes_check_plan(self) -> None:
        path = self.write_plan(slices=[{}, {}])
        report = plan_mod.plan_check_report(path)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["slice_count"], 2)

    def test_multi_defect_plan_reports_every_error_at_once(self) -> None:
        text = (
            "# Plan\n\n"
            "## Slice 1: Missing sections\n\n"
            "### Intended Change\nDo it.\n\n"
            "### Authorized Surface\n"
            "- Files allowed to change:\n  - a.py\n\n"
            "## Slice 2: Duplicate\n\n"
            + render_slice(2, files=None)
            + "## Slice 2: Duplicate again\n\n"
            + render_slice(2)
        )
        path = self.repo / "plan.md"
        path.write_text(text, encoding="utf-8")
        report = plan_mod.plan_check_report(path)
        joined = "\n".join(report["errors"])
        self.assertIn("missing required sections", joined)
        self.assertIn("duplicate slice numbers", joined)
        self.assertIn("authorized surface has no files allowed to change", joined)
        # At least three independent defects reported together, not just the first.
        self.assertGreaterEqual(len(report["errors"]), 3)

    def test_approval_gated_listed_without_error(self) -> None:
        path = self.write_plan(slices=[{"approval": "yes"}, {"approval": "no"}])
        report = plan_mod.plan_check_report(path)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["approval_gated"], ["Slice 1"])


class TestHeadingDefects(PlanTestCase):
    def test_malformed_slice_heading_rejected(self) -> None:
        text = "# Plan\n\n" + render_slice(1) + "# Slice 2: Wrong Level\n\n" + render_slice(2)
        path = self.repo / "plan.md"
        path.write_text(text, encoding="utf-8")
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("malformed slice heading" in error for error in report["errors"]))

    def test_slice_like_heading_in_fence_rejected(self) -> None:
        text = "# Plan\n\n" + render_slice(1) + "```markdown\n## Slice 2: Example inside a fence\n```\n\n"
        path = self.repo / "plan.md"
        path.write_text(text, encoding="utf-8")
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("fenced code block" in error for error in report["errors"]))

    def test_unclosed_fence_rejected(self) -> None:
        text = "# Plan\n\n" + render_slice(1) + "```\nunterminated example\n"
        path = self.repo / "plan.md"
        path.write_text(text, encoding="utf-8")
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("unclosed code fence" in error for error in report["errors"]))

    def test_batch_heading_warning_at_any_level(self) -> None:
        for level in ("##", "###", "####"):
            with self.subTest(level=level):
                text = "# Plan\n\n" + render_slice(1) + f"{level} Slice Batches\n\nSome text.\n"
                path = self.repo / "plan.md"
                path.write_text(text, encoding="utf-8")
                report = plan_mod.plan_check_report(path)
                self.assertEqual(report["errors"], [])
                self.assertTrue(any("ignores batch groupings" in warning for warning in report["warnings"]))

    def test_batch_bullet_warning(self) -> None:
        text = "# Plan\n\n" + render_slice(1) + "- Batch 1: Slices 1-2\n"
        path = self.repo / "plan.md"
        path.write_text(text, encoding="utf-8")
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("ignores batch groupings" in warning for warning in report["warnings"]))

    def test_duplicate_slice_numbers_rejected(self) -> None:
        text = "# Plan\n\n" + render_slice(1) + render_slice(1, title="Same number again")
        path = self.repo / "plan.md"
        path.write_text(text, encoding="utf-8")
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("duplicate slice numbers" in error for error in report["errors"]))


class TestSectionsAndSurfaceDefects(PlanTestCase):
    def test_missing_sections_rejected(self) -> None:
        text = (
            "# Plan\n\n## Slice 1: Bare\n\n### Intended Change\nDo it.\n\n"
            "### Authorized Surface\n- Files allowed to change:\n  - a.py\n\n"
        )
        path = self.repo / "plan.md"
        path.write_text(text, encoding="utf-8")
        report = plan_mod.plan_check_report(path)
        [error] = [e for e in report["errors"] if "missing required sections" in e]
        for section in ("Acceptance Criteria", "Explicit Non-Goals", "Risk Flags", "Validation Plan", "Rollback Path"):
            self.assertIn(section, error)

    def test_empty_surface_rejected(self) -> None:
        path = self.write_plan(slices=[{"files": None}])
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("no files allowed to change" in error for error in report["errors"]))

    def test_stray_sibling_bullet_ignored(self) -> None:
        slices = plan_mod.parse_plan(self.write_plan())
        self.assertEqual(slices[0].authorized_files, ["a.py"])
        # The renderer's sibling bullets ("Functions/... none.", "Tests... none.")
        # must never leak into authorized_files.
        for entry in slices[0].authorized_files:
            self.assertNotIn("Functions", entry)
            self.assertNotIn("Tests", entry)

    def test_none_dot_bullet_filtered(self) -> None:
        slices = plan_mod.parse_plan(self.write_plan(slices=[{"files": None}]))
        self.assertEqual(slices[0].authorized_files, [])


class TestAuthorizedEntryValidity(PlanTestCase):
    def test_invalid_entries_rejected(self) -> None:
        cases = {
            "/abs/path.py": "absolute",
            "./rel.py": "redundant",
            "a\\b.py": "backslash",
            "a//b.py": "empty path segment",
            "../escape.py": "'.' or '..'",
            "README.md (new file)": "unwrapped whitespace",
        }
        for entry, expected_fragment in cases.items():
            with self.subTest(entry=entry):
                error = plan_mod.authorized_entry_error(entry)
                self.assertIsNotNone(error)
                self.assertIn(expected_fragment, error)

    def test_backtick_annotation_normalizes_and_authorizes(self) -> None:
        from pm_lib.git_ops import is_authorized_path

        entry = "`nilakantha.py` (new file)"
        self.assertIsNone(plan_mod.authorized_entry_error(entry))
        self.assertTrue(is_authorized_path("nilakantha.py", [entry]))
        self.assertFalse(is_authorized_path("nilakantha.py (new file)", [entry]))

    def test_invalid_entry_not_double_linted(self) -> None:
        path = self.write_plan(slices=[{"files": ["/abs/path.py"]}])
        report = plan_mod.plan_check_report(path)
        errors = [e for e in report["errors"] if "invalid authorized surface" in e]
        self.assertEqual(len(errors), 1)
        self.assertEqual(report["warnings"], [])


class TestApprovalFlag(PlanTestCase):
    def test_approval_exact_no_runs(self) -> None:
        slices = plan_mod.parse_plan(self.write_plan(slices=[{"approval": "no"}]))
        self.assertIs(slices[0].approval_needed, False)
        report = plan_mod.plan_check_report(self.write_plan(slices=[{"approval": "no"}]))
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["approval_gated"], [])

    def test_approval_exact_yes_blocks_until_approved(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"approval": "yes"}]))[0]
        self.assertIs(plan_slice.approval_needed, True)
        ok, reasons = plan_mod.eligibility(plan_slice)
        self.assertFalse(ok)
        self.assertTrue(any("approval-needed" in r for r in reasons))
        ok, reasons = plan_mod.eligibility(plan_slice, approved_slice_ids={"Slice 1"})
        self.assertTrue(ok)

    def test_approval_free_text_is_planning_defect_not_approvable(self) -> None:
        for value in ("not yet decided", "none"):
            with self.subTest(value=value):
                path = self.write_plan(slices=[{"approval": value}])
                plan_slice = plan_mod.parse_plan(path)[0]
                self.assertIsNone(plan_slice.approval_needed)
                ok, reasons = plan_mod.eligibility(plan_slice, approved_slice_ids={"Slice 1"})
                self.assertFalse(ok)
                self.assertTrue(any("missing or unclear" in r for r in reasons))
                report = plan_mod.plan_check_report(path)
                self.assertTrue(any("must be exactly 'yes' or 'no'" in e for e in report["errors"]))

    def test_approval_blank_value_is_planning_defect_not_approvable(self) -> None:
        text = render_slice(1).replace(
            "- Approval needed before implementation: no.\n",
            "- Approval needed before implementation:\n",
        )
        path = self.repo / "plan.md"
        path.write_text("# Plan\n\n" + text, encoding="utf-8")
        plan_slice = plan_mod.parse_plan(path)[0]
        self.assertIsNone(plan_slice.approval_needed)
        ok, reasons = plan_mod.eligibility(plan_slice, approved_slice_ids={"Slice 1"})
        self.assertFalse(ok)
        self.assertTrue(any("missing or unclear" in r for r in reasons))
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("must be exactly 'yes' or 'no'" in e for e in report["errors"]))


class TestPlanCovariates(PlanTestCase):
    def _write(self, text: str) -> Path:
        path = self.repo / "plan.md"
        path.write_text("# Plan\n\n" + text, encoding="utf-8")
        return path

    def _errors(self, text: str) -> list[str]:
        return plan_mod.plan_check_report(self._write(text))["errors"]

    def test_missing_difficulty_is_plan_error_naming_slice(self) -> None:
        path = self._write(render_slice(1).replace("- Difficulty: moderate.\n", ""))
        self.assertIsNone(plan_mod.parse_plan(path)[0].difficulty)
        errors = plan_mod.plan_check_report(path)["errors"]
        self.assertTrue(any(e.startswith("Slice 1 (") and "'Difficulty:' must be exactly one of" in e for e in errors))

    def test_unrecognised_difficulty_is_plan_error(self) -> None:
        for value in ("easy | moderate | hard", "medium"):
            with self.subTest(value=value):
                errors = self._errors(render_slice(1, difficulty=value))
                self.assertTrue(any("'Difficulty:' must be exactly one of" in e for e in errors))

    def test_blank_difficulty_is_plan_error(self) -> None:
        errors = self._errors(render_slice(1).replace("- Difficulty: moderate.\n", "- Difficulty:\n"))
        self.assertTrue(any("'Difficulty:' must be exactly one of" in e for e in errors))

    def test_valid_difficulty_and_checkboxes_are_counted(self) -> None:
        text = render_slice(1, difficulty="Hard", acceptance="- Inputs: none\n- [ ] One.\n  - [x] Two.\n- [X] Three.")
        path = self._write(text)
        plan_slice = plan_mod.parse_plan(path)[0]
        self.assertEqual(plan_slice.difficulty, "hard")
        self.assertEqual(plan_slice.criteria_total, 3)
        self.assertEqual(plan_mod.plan_check_report(path)["errors"], [])

    def test_zero_checkbox_criteria_is_plan_error(self) -> None:
        errors = self._errors(render_slice(1, acceptance="- Inputs: none\n- Behaviour that must not change: x"))
        self.assertTrue(
            any(e.startswith("Slice 1 (") and "at least one '- [ ]' checkbox criterion" in e for e in errors)
        )

    def test_difficulty_quoted_in_another_flag_is_not_read_as_the_difficulty(self) -> None:
        text = render_slice(1, difficulty="easy").replace(
            "- Difficulty: easy.\n",
            "- Recommended Developer: a strong model (Difficulty: hard) at high effort.\n- Difficulty: easy.\n",
        )
        self.assertEqual(plan_mod.parse_plan(self._write(text))[0].difficulty, "easy")

    def test_checkbox_inside_a_fenced_block_is_not_counted(self) -> None:
        acceptance = "- Inputs: none\n- [ ] One.\n\n```markdown\n- [ ] An example, not a criterion.\n```"
        plan_slice = plan_mod.parse_plan(self._write(render_slice(1, acceptance=acceptance)))[0]
        self.assertEqual(plan_slice.criteria_total, 1)


class TestIndependentAudit(PlanTestCase):
    def test_independent_audit_exact_yes_arms_gate(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"audit": "yes"}]))[0]
        self.assertTrue(plan_slice.independent_audit_required)

    def test_independent_audit_exact_no_leaves_off(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"audit": "no"}]))[0]
        self.assertFalse(plan_slice.independent_audit_required)

    def test_independent_audit_absent_defaults_off(self) -> None:
        text = render_slice(1).replace("- Independent audit required: no.\n", "")
        path = self.repo / "plan.md"
        path.write_text("# Plan\n\n" + text, encoding="utf-8")
        plan_slice = plan_mod.parse_plan(path)[0]
        self.assertFalse(plan_slice.independent_audit_required)

    def test_independent_audit_line_counts_without_a_dash_bullet(self) -> None:
        """The label is read at the start of its line whatever the bullet marker, so a hand-written
        `Independent audit required: yes` (the template has no such line) still arms the gate."""
        for prefix in ("", "* "):
            with self.subTest(prefix=prefix or "none"):
                text = render_slice(1).replace(
                    "- Independent audit required: no.\n", f"{prefix}Independent audit required: yes\n"
                )
                path = self.repo / "plan.md"
                path.write_text("# Plan\n\n" + text, encoding="utf-8")
                self.assertTrue(plan_mod.parse_plan(path)[0].independent_audit_required)

    def test_independent_audit_unclear_defaults_off(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"audit": "maybe"}]))[0]
        self.assertFalse(plan_slice.independent_audit_required)


class TestRiskySurfacesAndPlanRisk(PlanTestCase):
    def test_risky_surfaces_exact_none_is_clear(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"risky": "none"}]))[0]
        self.assertTrue(plan_slice.risky_surfaces_clear)
        self.assertEqual(plan_slice.plan_risk, "standard")

    def test_risky_surfaces_none_without_trailing_period_is_clear(self) -> None:
        # render_slice always appends a period ("{risky}." -> "none."); write the
        # bare "none" (no trailing period) form directly to pin that case too.
        text = render_slice(1, risky="none").replace(
            "- Risky surfaces touched: none.\n", "- Risky surfaces touched: none\n"
        )
        path = self.repo / "plan.md"
        path.write_text("# Plan\n\n" + text, encoding="utf-8")
        plan_slice = plan_mod.parse_plan(path)[0]
        self.assertTrue(plan_slice.risky_surfaces_clear)

    def test_risky_surfaces_other_value_is_elevated(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"risky": "auth module"}]))[0]
        self.assertFalse(plan_slice.risky_surfaces_clear)
        self.assertEqual(plan_slice.plan_risk, "elevated")

    def test_risky_surfaces_missing_line_is_elevated(self) -> None:
        text = render_slice(1).split("\n")
        text = "\n".join(line for line in text if "Risky surfaces touched" not in line)
        path = self.repo / "plan.md"
        path.write_text("# Plan\n\n" + text, encoding="utf-8")
        plan_slice = plan_mod.parse_plan(path)[0]
        self.assertFalse(plan_slice.risky_surfaces_clear)
        self.assertEqual(plan_slice.plan_risk, "elevated")

    def test_plan_risk_elevated_via_approval(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"approval": "yes", "audit": "no", "risky": "none"}]))[
            0
        ]
        self.assertEqual(plan_slice.plan_risk, "elevated")

    def test_plan_risk_elevated_via_independent_audit(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"approval": "no", "audit": "yes", "risky": "none"}]))[
            0
        ]
        self.assertEqual(plan_slice.plan_risk, "elevated")

    def test_plan_risk_elevated_via_risky_surfaces(self) -> None:
        plan_slice = plan_mod.parse_plan(
            self.write_plan(slices=[{"approval": "no", "audit": "no", "risky": "database schema"}])
        )[0]
        self.assertEqual(plan_slice.plan_risk, "elevated")

    def test_plan_risk_standard_when_all_clear(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"approval": "no", "audit": "no", "risky": "none"}]))[
            0
        ]
        self.assertEqual(plan_slice.plan_risk, "standard")


class TestSurfaceLintWarnings(PlanTestCase):
    def test_dependency_lint_warning(self) -> None:
        path = self.write_plan(slices=[{"files": ["package.json"]}])
        report = plan_mod.plan_check_report(path)
        self.assertEqual(report["errors"], [])
        self.assertTrue(any("dependency-shaped" in w for w in report["warnings"]))

    def test_license_lint_warning(self) -> None:
        path = self.write_plan(slices=[{"files": ["LICENSE"]}])
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("license-shaped" in w for w in report["warnings"]))

    def test_whole_repo_lint_warning(self) -> None:
        path = self.write_plan(slices=[{"files": ["**"]}])
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("authorizes the entire repository" in w for w in report["warnings"]))

    def test_top_level_only_lint_warning(self) -> None:
        path = self.write_plan(slices=[{"files": ["*"]}])
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("top-level paths only" in w for w in report["warnings"]))

    def test_directory_entry_lint_with_repo_context(self) -> None:
        (self.repo / "subdir").mkdir()
        (self.repo / "subdir" / "keep.py").write_text("x = 1\n", encoding="utf-8")
        path = self.write_plan(slices=[{"files": ["subdir"]}])
        report = plan_mod.plan_check_report(path, repo=self.repo)
        self.assertTrue(any("names an existing directory" in w for w in report["warnings"]))
        # Without repo context, the same plan gets no directory-lint warning.
        report_no_repo = plan_mod.plan_check_report(path)
        self.assertFalse(any("names an existing directory" in w for w in report_no_repo["warnings"]))


class TestDigestFreeze(PlanTestCase):
    def test_verify_plan_unchanged_passes_untouched(self) -> None:
        path = self.write_plan()
        state = {"plan": {"sha256": plan_mod.plan_digest(path)}}
        plan_mod.verify_plan_unchanged(state, path)  # must not raise

    def test_verify_plan_unchanged_raises_on_edit(self) -> None:
        path = self.write_plan()
        state = {"plan": {"sha256": plan_mod.plan_digest(path)}}
        path.write_text(path.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")
        with self.assertRaises(PmError) as ctx:
            plan_mod.verify_plan_unchanged(state, path)
        message = str(ctx.exception)
        self.assertIn("start a new run", message)


class TestNextSliceAndEligibility(PlanTestCase):
    def test_next_slice_skips_accepted_and_attested_and_orders_by_number(self) -> None:
        path = self.write_plan(slices=[{}, {}, {}])
        slices = plan_mod.parse_plan(path)
        state = {
            "slices": [
                {"id": "Slice 2", "status": "accepted"},
                {"id": "Slice 1", "status": "attested"},
            ]
        }
        result = plan_mod.next_slice(slices, state)
        self.assertEqual(result.slice_id, "Slice 3")

    def test_next_slice_none_when_all_done(self) -> None:
        path = self.write_plan(slices=[{}, {}])
        slices = plan_mod.parse_plan(path)
        state = {"slices": [{"id": "Slice 1", "status": "accepted"}, {"id": "Slice 2", "status": "attested"}]}
        self.assertIsNone(plan_mod.next_slice(slices, state))

    def test_eligibility_reports_missing_sections(self) -> None:
        text = "## Slice 1: Bare\n\n### Intended Change\nDo it.\n"
        path = self.repo / "plan.md"
        path.write_text("# Plan\n\n" + text, encoding="utf-8")
        plan_slice = plan_mod.parse_plan(path)[0]
        ok, reasons = plan_mod.eligibility(plan_slice)
        self.assertFalse(ok)
        self.assertTrue(any("missing required sections" in r for r in reasons))

    def test_eligibility_reports_empty_surface(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"files": None}]))[0]
        ok, reasons = plan_mod.eligibility(plan_slice)
        self.assertFalse(ok)
        self.assertTrue(any("no files allowed to change" in r for r in reasons))

    def test_eligibility_reports_unapproved_gate(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"approval": "yes"}]))[0]
        ok, reasons = plan_mod.eligibility(plan_slice)
        self.assertFalse(ok)
        self.assertTrue(any("approval-needed" in r for r in reasons))

    def test_eligibility_reports_unclear_approval(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"approval": "unsure"}]))[0]
        ok, reasons = plan_mod.eligibility(plan_slice, approved_slice_ids={"Slice 1"})
        self.assertFalse(ok)
        self.assertTrue(any("missing or unclear" in r for r in reasons))

    def test_eligibility_passes_when_clear(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan())[0]
        ok, reasons = plan_mod.eligibility(plan_slice)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_plan_slice_by_id(self) -> None:
        slices = plan_mod.parse_plan(self.write_plan(slices=[{}, {}]))
        found = plan_mod.plan_slice_by_id(slices, "Slice 2")
        self.assertIsNotNone(found)
        self.assertEqual(found.number, 2)
        self.assertIsNone(plan_mod.plan_slice_by_id(slices, "Slice 99"))


class TestSurfaceGrants(PlanTestCase):
    """PM surface grants: `slice_grants` reads them out of state,
    `effective_authorized_files` folds them onto the frozen plan surface."""

    def test_slice_grants_returns_recorded_grants_and_empty_for_absent_slice(self) -> None:
        grants = [{"path": "b.py", "evidence": "needed", "at": "2026-01-01T00:00:00Z"}]
        state = {"slices": [{"id": "Slice 1", "grants": grants}]}
        self.assertEqual(plan_mod.slice_grants(state, "Slice 1"), grants)
        self.assertEqual(plan_mod.slice_grants(state, "Slice 2"), [])

    def test_effective_authorized_files_orders_plan_surface_before_grants(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"files": ["a.py"]}]))[0]
        state = {"slices": [{"id": "Slice 1", "grants": [{"path": "b.py", "evidence": "needed"}]}]}
        # Plan entries must come first: grants only ever widen, never reorder
        # the frozen surface ahead of what a mid-run grant adds.
        self.assertEqual(plan_mod.effective_authorized_files(plan_slice, state), ["a.py", "b.py"])


class TestLoadPolicy(PlanTestCase):
    """`load_policy` reads untrusted operator input: every malformed shape is
    a `PmError` naming the file, and a policy inside the repository protects
    itself."""

    def write_policy(self, content: object, path: Path | None = None) -> Path:
        """Write `content` as JSON (or verbatim when it is a str) outside the repo by default."""
        path = path or self.repo.parent / "policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text, encoding="utf-8")
        return path

    def test_happy_path_returns_exactly_path_digest_and_entries(self) -> None:
        path = self.write_policy({"schema": 1, "owner": "ignored", "protected_paths": ["instruments/", "data.csv"]})
        policy = plan_mod.load_policy(path)
        self.assertEqual(set(policy), {"path", "sha256", "protected_paths"})
        self.assertEqual(policy["path"], str(path.resolve()))
        self.assertTrue(Path(policy["path"]).is_absolute())
        self.assertEqual(policy["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(policy["protected_paths"], ["instruments/", "data.csv"])

    def test_absent_protected_paths_is_empty_and_unknown_keys_ignored(self) -> None:
        path = self.write_policy({"schema": "not even a number", "notes": [1, 2]})
        self.assertEqual(plan_mod.load_policy(path)["protected_paths"], [])

    def test_malformed_policy_raises_naming_the_file(self) -> None:
        cases = {
            "invalid JSON": "{not json",
            "top-level list": [],
            "protected_paths string": {"protected_paths": "instruments/"},
            "protected_paths null": {"protected_paths": None},
            "non-string item": {"protected_paths": ["ok/", 7]},
            "empty-string item": {"protected_paths": [""]},
            "absolute entry": {"protected_paths": ["/etc/passwd"]},
            "dot-dot entry": {"protected_paths": ["../outside/"]},
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                path = self.write_policy(content)
                with self.assertRaises(PmError) as ctx:
                    plan_mod.load_policy(path)
                self.assertIn(str(path.resolve()), str(ctx.exception))

    def test_unreadable_policy_raises_naming_the_file(self) -> None:
        missing = self.repo.parent / "missing.json"
        with self.assertRaises(PmError) as ctx:
            plan_mod.load_policy(missing)
        self.assertIn(str(missing.resolve()), str(ctx.exception))
        self.assertIn("cannot be read", str(ctx.exception))

    def test_entry_with_empty_literal_prefix_refused_naming_the_entry(self) -> None:
        for entry in ("**/*.py", "*.lock"):
            with self.subTest(entry=entry):
                path = self.write_policy({"protected_paths": ["instruments/", entry]})
                with self.assertRaises(PmError) as ctx:
                    plan_mod.load_policy(path)
                self.assertIn(repr(entry), str(ctx.exception))
                self.assertIn(str(path.resolve()), str(ctx.exception))

    def test_entries_normalized_deduplicated_first_seen_order(self) -> None:
        path = self.write_policy({"protected_paths": ["b/", "`a.py` (the instrument)", " b/ ", "a.py", "c/*.json"]})
        self.assertEqual(plan_mod.load_policy(path)["protected_paths"], ["b/", "a.py", "c/*.json"])

    def test_policy_inside_repo_appends_its_relative_path_once(self) -> None:
        path = self.write_policy({"protected_paths": ["instruments/"]}, self.repo / "config" / "policy.json")
        # An unresolved repo spelling must still yield a repo-relative path.
        policy = plan_mod.load_policy(path, repo=self.repo / "config" / "..")
        self.assertEqual(policy["protected_paths"], ["instruments/", "config/policy.json"])

    def test_policy_inside_repo_not_appended_when_an_entry_covers_it(self) -> None:
        for entries in (["config/"], ["config/policy.json"], ["config/*.json"]):
            with self.subTest(entries=entries):
                path = self.write_policy({"protected_paths": entries}, self.repo / "config" / "policy.json")
                self.assertEqual(plan_mod.load_policy(path, repo=self.repo)["protected_paths"], entries)

    def test_policy_outside_repo_or_without_repo_appends_nothing(self) -> None:
        outside = self.write_policy({"protected_paths": ["instruments/"]})
        self.assertEqual(plan_mod.load_policy(outside, repo=self.repo)["protected_paths"], ["instruments/"])
        inside = self.write_policy({"protected_paths": ["instruments/"]}, self.repo / "policy.json")
        self.assertEqual(plan_mod.load_policy(inside)["protected_paths"], ["instruments/"])


class TestProtectedOverlap(unittest.TestCase):
    """The plan-time overlap rule compares literal prefixes segment-wise."""

    def test_literal_prefix_table(self) -> None:
        cases = {
            "a/b.py": "a/b.py",
            "dir/": "dir",
            "a/b/": "a/b",
            "a/b/*.py": "a/b",
            "src/**/x.py": "src",
            "a/b?/c.py": "a",
            "a/[xy]/c.py": "a",
            "**/*.py": "",
            "*.lock": "",
        }
        for entry, expected in cases.items():
            with self.subTest(entry=entry):
                self.assertEqual(plan_mod.literal_prefix(entry), expected)

    def test_overlaps_reported(self) -> None:
        cases = [
            ("plain file inside a protected subtree", "a/b/c.py", ["a/b/"]),
            ("subtree containing a protected file", "a/", ["a/b/c.py"]),
            ("glob whose prefix lies inside a protected subtree", "a/b/*.py", ["a/"]),
            ("empty-prefix glob against any protected entry", "**/*.py", ["x/y.json"]),
            ("equal prefixes", "a/b", ["a/b"]),
        ]
        for name, entry, protected in cases:
            with self.subTest(case=name):
                self.assertEqual(plan_mod.protected_overlap(entry, protected), protected[0])

    def test_first_overlapping_protected_entry_returned(self) -> None:
        self.assertEqual(plan_mod.protected_overlap("a/b/c.py", ["x/", "a/", "a/b/"]), "a/")

    def test_no_overlap_for_sibling_name_or_disjoint_pair(self) -> None:
        self.assertIsNone(plan_mod.protected_overlap("a/b", ["a/bc/"]))
        self.assertIsNone(plan_mod.protected_overlap("src/x.py", ["docs/", "instruments/check.py"]))
        self.assertIsNone(plan_mod.protected_overlap("src/x.py", []))


class TestCheckPlanPolicy(PlanTestCase):
    """`check-plan --policy` refuses a surface reaching a protected path and
    still reports every other defect the plan carries."""

    def write_policy(self, entries: list[str], path: Path | None = None) -> Path:
        path = path or self.repo.parent / "policy.json"
        path.write_text(json.dumps({"schema": 1, "protected_paths": entries}), encoding="utf-8")
        return path

    def test_report_emits_overlap_error_beside_other_errors_and_warnings(self) -> None:
        path = self.write_plan(
            slices=[
                {"title": "Touches instruments", "files": ["instruments/check.py", "package.json"]},
                {"difficulty": "trivial"},
            ]
        )
        report = plan_mod.plan_check_report(path, protected=["instruments/"])
        self.assertIn(
            "Slice 1 (Touches instruments): authorized entry 'instruments/check.py' reaches protected path "
            "'instruments/'; give it a literal prefix disjoint from every protected path",
            report["errors"],
        )
        self.assertTrue(any("Slice 2" in e and "'Difficulty:'" in e for e in report["errors"]))
        self.assertTrue(any("dependency-shaped" in w for w in report["warnings"]))
        self.assertEqual(report["protected_count"], 1)

    def test_overlap_error_names_the_normalized_entry(self) -> None:
        path = self.write_plan(slices=[{"files": ["`instruments/new.py` (new file)"]}])
        report = plan_mod.plan_check_report(path, protected=["instruments/"])
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("authorized entry 'instruments/new.py' reaches", report["errors"][0])

    def test_report_without_protected_is_unchanged(self) -> None:
        path = self.write_plan(slices=[{"files": ["instruments/check.py", "package.json"]}, {"difficulty": "x"}])
        baseline = plan_mod.plan_check_report(path)
        self.assertEqual(baseline["protected_count"], 0)
        for protected in (None, []):
            with self.subTest(protected=protected):
                report = plan_mod.plan_check_report(path, protected=protected)
                self.assertEqual(report, baseline)

    def test_cli_exits_2_on_overlap_and_prints_protected_line(self) -> None:
        plan_path = self.write_plan(slices=[{"files": ["instruments/check.py"]}])
        policy_path = self.write_policy(["instruments/"])
        code, out, _err = self.run_cli(["check-plan", "--plan", str(plan_path), "--policy", str(policy_path)])
        self.assertEqual(code, 2)
        self.assertIn("ERROR: Slice 1 (Slice 1 title): authorized entry 'instruments/check.py' reaches", out)
        self.assertIn(f"protected paths: 1 (from {policy_path.resolve()})", out)

    def test_cli_exits_0_on_clean_plan_and_prints_protected_line(self) -> None:
        plan_path = self.write_plan(slices=[{"files": ["src/a.py"]}])
        policy_path = self.write_policy(["instruments/"])
        code, out, _err = self.run_cli(["check-plan", "--plan", str(plan_path), "--policy", str(policy_path)])
        self.assertEqual(code, 0)
        self.assertNotIn("ERROR:", out)
        self.assertIn(f"protected paths: 1 (from {policy_path.resolve()})", out)

    def test_cli_counts_self_protected_policy_inside_repo(self) -> None:
        plan_path = self.write_plan(slices=[{"files": ["src/a.py"]}])
        policy_path = self.write_policy(["instruments/"], self.repo / "policy.json")
        argv = ["check-plan", "--plan", str(plan_path), "--repo", str(self.repo), "--policy", str(policy_path)]
        code, out, _err = self.run_cli(argv)
        self.assertEqual(code, 0)
        self.assertIn(f"protected paths: 2 (from {policy_path.resolve()})", out)

    def test_cli_malformed_policy_exits_2_naming_the_file(self) -> None:
        plan_path = self.write_plan()
        policy_path = self.repo.parent / "policy.json"
        policy_path.write_text('{"protected_paths": "instruments/"}', encoding="utf-8")
        code, out, err = self.run_cli(["check-plan", "--plan", str(plan_path), "--policy", str(policy_path)])
        self.assertEqual(code, 2)
        self.assertIn("pm: error:", err)
        self.assertIn(str(policy_path.resolve()), err)
        self.assertNotIn("slice(s); approval-gated", out)

    def test_cli_without_policy_prints_no_protected_line(self) -> None:
        plan_path = self.write_plan(slices=[{"files": ["instruments/check.py"]}])
        code, out, _err = self.run_cli(["check-plan", "--plan", str(plan_path)])
        self.assertEqual(code, 0)
        self.assertEqual(out, "1 slice(s); approval-gated: none\n")


class TestImportHygiene(unittest.TestCase):
    """pm_lib may only import stdlib modules plus pm_lib internals."""

    def test_pm_lib_imports_only_stdlib_and_internal(self) -> None:
        import sys

        pm_lib_dir = Path(__file__).resolve().parents[1] / "scripts" / "pm_lib"
        stdlib_names = set(sys.stdlib_module_names)
        offenders: list[str] = []
        for source_file in sorted(pm_lib_dir.glob("*.py")):
            tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_level = alias.name.split(".")[0]
                        if top_level not in stdlib_names and top_level != "pm_lib":
                            offenders.append(f"{source_file.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        continue  # relative import within pm_lib itself
                    module = node.module or ""
                    top_level = module.split(".")[0]
                    if top_level not in stdlib_names and top_level != "pm_lib" and top_level != "__future__":
                        offenders.append(f"{source_file.name}: from {module} import ...")
        self.assertEqual(offenders, [], f"non-stdlib/non-pm_lib imports found: {offenders}")


if __name__ == "__main__":
    unittest.main()
