"""Protected behaviours: Developer prompt template loading and rendering.

Pins prompts.py's rule that there are no inline prompt fragments elsewhere in
the package:

- `load_template` extracts the single fenced ```md block from a reference
  file's leading section (heading=None, the file's content before its
  second heading, or the whole file with at most one heading — this is
  what keeps the developer/reviewer prompt loaders working unchanged now
  that developer-prompt.md carries a second, later section). A reference
  file with no such block, or with more than one, is rejected with a
  `PmError` naming the file. A named `heading` scopes extraction to that
  section instead; an absent heading raises `PmError`.
- `render_steer_message` sources its fixed wrapper from
  developer-prompt.md's "## Steer Message
  Template" section via `load_template(..., heading=...)`, and substitutes
  `{correction}` — braces inside the correction text itself are never
  treated as format fields, since they are a substituted value, not part
  of the template string.
- `render_developer_prompt`, against the real
  `skills/project-manager/references/developer-prompt.md` file (the default
  reference path, resolved relative to the pm_lib package), produces text
  that: contains the slice id, contains every contract section's verbatim
  text (Intended Change, Acceptance Criteria, Authorized Surface, Explicit
  Non-Goals, Risk Flags, Validation Plan, Rollback Path), contains the
  plan/artifact/notes/result paths, and has no unresolved `{placeholder}`
  left over (the JSON example's escaped `{{`/`}}` braces resolve to plain
  `{`/`}` in the output, not to a stray unresolved field).
- A template with a stray, unescaped brace (one that is neither a
  recognized `{placeholder}` nor a doubled `{{`/`}}` escape) raises a
  `PmError` that names the offending template file.
- A reference-path override is honored (tests do not have to touch the
  real references file to exercise the loader/renderer against a
  custom-built template).
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


class TestRenderSteerMessage(unittest.TestCase):
    def test_render_against_real_reference_file_states_frozen_contract_and_includes_correction(self) -> None:
        rendered = prompts.render_steer_message("Please also update the docstring.")
        self.assertIn("Please also update the docstring.", rendered)
        self.assertIn("frozen slice contract", rendered)
        self.assertIn("never expands your authorized surface", rendered)
        self.assertNotIn("{correction}", rendered)

    def test_multiline_correction_survives_verbatim(self) -> None:
        correction = "First line of the correction.\nSecond line with more detail."
        rendered = prompts.render_steer_message(correction)
        self.assertIn(correction, rendered)

    def test_braces_in_correction_are_not_treated_as_format_fields(self) -> None:
        correction = "Use the shape {\"slice\": \"Slice 1\"} exactly."
        rendered = prompts.render_steer_message(correction)
        self.assertIn(correction, rendered)

    def test_wrapper_is_sourced_from_the_reference_file_not_hardcoded(self) -> None:
        """A custom reference file with a distinctive wrapper string proves
        `render_steer_message` actually reads its wording from disk rather
        than duplicating an equivalent wrapper inline in prompts.py."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom-developer-prompt.md"
            path.write_text(
                "# Top\n\n```md\nmain: {slice_id}\n```\n\n"
                "## Steer Message Template\n\n"
                "```md\nCUSTOM_WRAPPER_MARKER_TEXT_9f3a: {correction}\n```\n",
                encoding="utf-8",
            )
            rendered = prompts.render_steer_message("do the thing", reference_path=path)
            self.assertIn("CUSTOM_WRAPPER_MARKER_TEXT_9f3a", rendered)
            self.assertIn("do the thing", rendered)


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
                "```md\nsteer: {correction}\n```\n",
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
