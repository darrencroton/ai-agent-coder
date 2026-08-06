"""Protected behaviours: Developer and steer prompt template loading and rendering.

Pins prompts.py's rule that no prompt fragments live anywhere else in the
package. Rendering tests run against the real
`references/developer-prompt.md` (the default reference path) so a template
edit that breaks interpolation fails here; `reference_path` overrides let the
loader tests build their own templates instead.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import PmError
from pm_lib import plan as plan_mod
from pm_lib import prompts

_REAL_REFERENCE_PATH = Path(__file__).resolve().parents[1] / "references" / "developer-prompt.md"


def _make_plan_slice(number: int = 1) -> plan_mod.PlanSlice:
    body = (
        "### Intended Change\nDo the thing.\n\n"
        "### Acceptance Criteria\nIt works.\n\n"
        "### Authorized Surface\n- Files allowed to change:\n  - a.py\n\n"
        "### Explicit Non-Goals\nNothing else.\n\n"
        "### Risk Flags\n- Risky surfaces touched: none.\n"
        "- Approval needed before implementation: no.\n"
        "- Independent audit required: no.\n\n"
        "### Validation Plan\nRun the tests.\n\n"
        "### Rollback Path\ngit revert.\n"
    )
    sections = plan_mod.parse_sections(body)
    return plan_mod.PlanSlice(number=number, title="A title", body=body, sections=sections)


class TestLoadTemplate(unittest.TestCase):
    def test_loads_real_reference_file(self) -> None:
        template = prompts.load_template(_REAL_REFERENCE_PATH)
        self.assertIn("{slice_id}", template)
        self.assertIn("{intended_change}", template)

    def test_no_fence_rejected(self, tmp_path: Path | None = None) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no-fence.md"
            path.write_text("Just some prose, no fenced block here.\n", encoding="utf-8")
            with self.assertRaises(PmError) as ctx:
                prompts.load_template(path)
            self.assertIn(str(path), str(ctx.exception))

    def test_multiple_fences_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "multi-fence.md"
            path.write_text(
                "First:\n\n```md\none\n```\n\nSecond:\n\n```md\ntwo\n```\n",
                encoding="utf-8",
            )
            with self.assertRaises(PmError) as ctx:
                prompts.load_template(path)
            self.assertIn(str(path), str(ctx.exception))

    def test_missing_file_raises_pm_error(self) -> None:
        with self.assertRaises(PmError):
            prompts.load_template(Path("/does/not/exist/reference.md"))

    def test_named_heading_scopes_extraction_around_a_second_section(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "two-section.md"
            path.write_text(
                "# Top\n\n```md\nmain: {slice_id}\n```\n\n"
                "## Second Section\n\nsome prose\n\n```md\nsecond: {correction}\n```\n",
                encoding="utf-8",
            )
            self.assertIn("{slice_id}", prompts.load_template(path))
            self.assertIn("{correction}", prompts.load_template(path, heading="## Second Section"))

    def test_unknown_heading_raises_pm_error(self) -> None:
        with self.assertRaises(PmError):
            prompts.load_template(_REAL_REFERENCE_PATH, heading="## Not A Real Section")


class TestRenderSteerPointer(unittest.TestCase):
    def test_single_line_pointer_names_the_correction_path_and_states_frozen_contract(self) -> None:
        rendered = prompts.render_steer_pointer(Path("/runs/x/slices/slice-001/steer-attempt-2.md"))
        # Must be a single line — sessions.send_line refuses a newline, and
        # the whole point is that the correction stays in the file.
        self.assertNotIn("\n", rendered)
        self.assertIn("/runs/x/slices/slice-001/steer-attempt-2.md", rendered)
        self.assertNotIn("{correction_path}", rendered)
        self.assertIn("frozen slice contract", rendered)
        self.assertIn("never expands your authorized surface", rendered)

    def test_wording_is_sourced_from_the_reference_file_not_hardcoded(self) -> None:
        """A custom reference file with a distinctive marker proves
        `render_steer_pointer` actually reads its wording from disk rather
        than duplicating an equivalent wrapper inline in prompts.py."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom-developer-prompt.md"
            path.write_text(
                "# Top\n\n```md\nmain: {slice_id}\n```\n\n"
                "## Steer Message Template\n\n"
                "```md\nCUSTOM_WRAPPER_MARKER_TEXT_9f3a: {correction_path}\n```\n",
                encoding="utf-8",
            )
            rendered = prompts.render_steer_pointer(Path("/p/steer-attempt-1.md"), reference_path=path)
            self.assertIn("CUSTOM_WRAPPER_MARKER_TEXT_9f3a", rendered)
            self.assertIn("/p/steer-attempt-1.md", rendered)


class TestRenderLaunchPointer(unittest.TestCase):
    def test_single_line_pointer_names_the_contract_path(self) -> None:
        rendered = prompts.render_launch_pointer(Path("/runs/x/slices/slice-001/prompt.md"))
        # Must be a single line — sessions.send_prompt refuses a newline, and
        # the whole point is that the multi-KB contract stays in the file.
        self.assertNotIn("\n", rendered)
        self.assertIn("/runs/x/slices/slice-001/prompt.md", rendered)
        self.assertNotIn("{prompt_path}", rendered)

    def test_wording_is_sourced_from_the_reference_file_not_hardcoded(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom-developer-prompt.md"
            path.write_text(
                "# Top\n\n```md\nmain: {slice_id}\n```\n\n"
                "## Launch Pointer\n\n"
                "```md\nCUSTOM_POINTER_MARKER_4b21 read {prompt_path}\n```\n\n"
                "## Steer Message Template\n\n"
                "```md\nsteer: {correction_path}\n```\n",
                encoding="utf-8",
            )
            rendered = prompts.render_launch_pointer(Path("/p/prompt.md"), reference_path=path)
            self.assertIn("CUSTOM_POINTER_MARKER_4b21", rendered)
            self.assertIn("/p/prompt.md", rendered)


class TestRenderDeveloperPrompt(unittest.TestCase):
    def test_render_against_real_reference_file(self) -> None:
        plan_slice = _make_plan_slice(3)
        rendered = prompts.render_developer_prompt(
            plan_slice,
            plan_path=Path("/repo/plan.md"),
            artifact_dir=Path("/repo/.pm/runs/run-a/slices/slice-003"),
            notes_path=Path("/repo/.pm/runs/run-a/notes.md"),
            result_path=Path("/repo/.pm/runs/run-a/slices/slice-003/result.json"),
        )
        self.assertIn("Slice 3", rendered)
        self.assertIn("A title", rendered)
        self.assertIn("Do the thing.", rendered)
        self.assertIn("It works.", rendered)
        self.assertIn("a.py", rendered)
        self.assertIn("Nothing else.", rendered)
        self.assertIn("Risky surfaces touched: none.", rendered)
        self.assertIn("Run the tests.", rendered)
        self.assertIn("git revert.", rendered)
        self.assertIn("/repo/plan.md", rendered)
        self.assertIn("/repo/.pm/runs/run-a/slices/slice-003", rendered)
        self.assertIn("/repo/.pm/runs/run-a/notes.md", rendered)
        self.assertIn("/repo/.pm/runs/run-a/slices/slice-003/result.json", rendered)
        # The JSON example's escaped {{ }} braces resolve to plain braces,
        # never to a leftover unresolved {placeholder}.
        self.assertNotIn("{{", rendered)
        self.assertNotIn("}}", rendered)
        self.assertNotIn("{slice_id}", rendered)
        self.assertNotIn("{intended_change}", rendered)
        import re

        # No brace-wrapped identifier survives rendering.
        self.assertIsNone(re.search(r"\{[a-z_]+\}", rendered))

    def test_lint_base_is_the_slices_starting_commit(self) -> None:
        # The lint ref must be before_head, not HEAD: once the Developer has
        # committed, `--base HEAD` scopes nothing and lints nothing.
        rendered = prompts.render_developer_prompt(
            _make_plan_slice(3),
            plan_path=Path("/repo/plan.md"),
            artifact_dir=Path("/repo/a"),
            notes_path=Path("/repo/n.md"),
            result_path=Path("/repo/r.json"),
            before_head="d5676715b19d9fa12fa46856ca97a179da19756a",
        )
        self.assertIn("check --base d5676715b19d9fa12fa46856ca97a179da19756a", rendered)
        self.assertNotIn("check --base HEAD", rendered)

    def test_stray_unescaped_brace_raises_pm_error_naming_the_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.md"
            path.write_text(
                "```md\nSlice: {slice_id}\nExample: { not a field }\n```\n",
                encoding="utf-8",
            )
            plan_slice = _make_plan_slice()
            with self.assertRaises(PmError) as ctx:
                prompts.render_developer_prompt(
                    plan_slice,
                    plan_path=Path("/repo/plan.md"),
                    artifact_dir=Path("/repo/.pm/runs/run-a/slices/slice-001"),
                    notes_path=Path("/repo/.pm/runs/run-a/notes.md"),
                    result_path=Path("/repo/.pm/runs/run-a/slices/slice-001/result.json"),
                    reference_path=path,
                )
            self.assertIn(str(path), str(ctx.exception))

    def test_missing_placeholder_field_raises_pm_error(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unknown-field.md"
            path.write_text("```md\nSlice: {slice_id}\nUnknown: {not_a_real_field}\n```\n", encoding="utf-8")
            plan_slice = _make_plan_slice()
            with self.assertRaises(PmError) as ctx:
                prompts.render_developer_prompt(
                    plan_slice,
                    plan_path=Path("/repo/plan.md"),
                    artifact_dir=Path("/repo/.pm/runs/run-a/slices/slice-001"),
                    notes_path=Path("/repo/.pm/runs/run-a/notes.md"),
                    result_path=Path("/repo/.pm/runs/run-a/slices/slice-001/result.json"),
                    reference_path=path,
                )
            self.assertIn(str(path), str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
