"""Protected behaviours: the plan parser, check-plan report, and risk derivation.

Pins the parser's proven behaviour plus the mechanical `plan_risk`
derivation. Each test names one frozen scenario:

- A clean plan passes check-plan with no errors; a plan with several
  independent defects reports every one of them at once (multi-error
  reporting), never stopping at the first.
- Slice headings must be exactly "## Slice <N>: <name>"; any other
  heading that merely looks like one (any level, case-insensitive, "Slice"
  followed by space/colon/digit/EOL) is a malformed-heading error, except
  the "Slice Batches" heading, which is a warning (PM ignores batches) at
  any heading level, as is a "- Batch X: Slices..." bullet.
- A slice-like heading sitting inside a fenced code block is an error
  (the parser reads headings literally, so a fenced example could silently
  change the slice set); an unclosed fence is itself an error, since it
  makes heading detection ambiguous.
- Duplicate slice numbers and missing required sections are errors.
- The authorized surface is parsed from indented sub-bullets under
  "Files allowed to change:"; a sibling column-0 bullet is never captured;
  "- none."/"- none" values are filtered out, leaving an empty (and
  therefore invalid) surface. An empty or invalid surface is an error.
  Invalid entries (absolute, "./"-prefixed, backslash, "//", "."/".."
  segments, unwrapped trailing-annotation whitespace) are each rejected
  with an actionable message; a backtick-wrapped path with a trailing
  annotation normalizes to the wrapped path and is accepted; an invalid
  entry is never also lint-warned (it is reported once, as an error).
- "Approval needed before implementation:" must be exactly "yes" or "no"
  (case/whitespace/trailing-period insensitive); anything else (free text,
  blank) is a planning defect — a check-plan error, and never approvable
  at runtime, unlike an explicit "yes" which merely blocks until approved.
- "Independent audit required:" arms only on an exact "yes"; absent,
  blank, or unclear defaults off (fails closed to *off*, the opposite
  direction from approval).
- "Risky surfaces touched:" must be exactly "none" (or "none.") to read
  as clear; anything else, or a missing line, reads as not-clear.
- `plan_risk` is "elevated" iff approval is required, independent audit
  is required, or risky surfaces are not clear (each trigger tested in
  isolation); "standard" only when all three are clear.
- check-plan warns (never errors) on dependency-shaped, license-shaped,
  whole-repository, and top-level-only surface entries, and on a plain
  entry naming an existing directory when repo context is given.
- `verify_plan_unchanged` passes silently against an untouched plan file
  and raises PmError naming both digests after an edit.
- `next_slice` returns slices in plan-number order, skips any slice whose
  state entry is "accepted" or "attested", and returns None once every
  slice is done.
- `eligibility` reports one reason per defect: missing sections, an empty
  or invalid surface, an approval-required slice without a recorded
  approval, and an unclear approval flag (which blocks even with an
  approval recorded, since it is not an approvable condition).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from pm_test_helpers import PmTestCase, render_slice

from pm_lib import PmError
from pm_lib import plan as plan_mod


class TestCheckPlanCleanAndMultiDefect(PmTestCase):
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
            "## Slice 2: Duplicate\n\n" + render_slice(2, files=None) + "## Slice 2: Duplicate again\n\n"
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


class TestHeadingDefects(PmTestCase):
    def test_malformed_slice_heading_rejected(self) -> None:
        text = "# Plan\n\n" + render_slice(1) + "# Slice 2: Wrong Level\n\n" + render_slice(2)
        path = self.repo / "plan.md"
        path.write_text(text, encoding="utf-8")
        report = plan_mod.plan_check_report(path)
        self.assertTrue(any("malformed slice heading" in error for error in report["errors"]))

    def test_slice_like_heading_in_fence_rejected(self) -> None:
        text = (
            "# Plan\n\n"
            + render_slice(1)
            + "```markdown\n## Slice 2: Example inside a fence\n```\n\n"
        )
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


class TestSectionsAndSurfaceDefects(PmTestCase):
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


class TestAuthorizedEntryValidity(PmTestCase):
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


class TestApprovalFlag(PmTestCase):
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


class TestIndependentAudit(PmTestCase):
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

    def test_independent_audit_unclear_defaults_off(self) -> None:
        plan_slice = plan_mod.parse_plan(self.write_plan(slices=[{"audit": "maybe"}]))[0]
        self.assertFalse(plan_slice.independent_audit_required)


class TestRiskySurfacesAndPlanRisk(PmTestCase):
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
        plan_slice = plan_mod.parse_plan(
            self.write_plan(slices=[{"approval": "yes", "audit": "no", "risky": "none"}])
        )[0]
        self.assertEqual(plan_slice.plan_risk, "elevated")

    def test_plan_risk_elevated_via_independent_audit(self) -> None:
        plan_slice = plan_mod.parse_plan(
            self.write_plan(slices=[{"approval": "no", "audit": "yes", "risky": "none"}])
        )[0]
        self.assertEqual(plan_slice.plan_risk, "elevated")

    def test_plan_risk_elevated_via_risky_surfaces(self) -> None:
        plan_slice = plan_mod.parse_plan(
            self.write_plan(slices=[{"approval": "no", "audit": "no", "risky": "database schema"}])
        )[0]
        self.assertEqual(plan_slice.plan_risk, "elevated")

    def test_plan_risk_standard_when_all_clear(self) -> None:
        plan_slice = plan_mod.parse_plan(
            self.write_plan(slices=[{"approval": "no", "audit": "no", "risky": "none"}])
        )[0]
        self.assertEqual(plan_slice.plan_risk, "standard")


class TestSurfaceLintWarnings(PmTestCase):
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


class TestDigestFreeze(PmTestCase):
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


class TestNextSliceAndEligibility(PmTestCase):
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


class TestImportHygiene(unittest.TestCase):
    """pm_lib may only import stdlib modules plus pm_lib internals (blueprint §4, §6)."""

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
