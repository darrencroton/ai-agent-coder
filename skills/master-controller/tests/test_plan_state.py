"""Plan parsing, eligibility, approval, init, and run-state tests."""

from mc_test_helpers import *  # noqa: F401,F403 — shared fixtures, fake harnesses, and the mc module


class PlanStateTests(McTestCase):
    def test_cli_rejects_unsupported_python_before_parsing(self):
        with mock.patch.object(sys, "version_info", (3, 12)), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(mc.main([]), 1)
        self.assertIn("Python 3.13 or newer is required", err.getvalue())

    def test_check_plan_passes_clean_plan(self):
        report = mc.plan_check_report(self.plan)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["approval_gated"], [])
        self.assertEqual(report["slice_count"], 2)
        args = argparse.Namespace(plan=str(self.plan))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mc.check_plan(args), 0)
        self.assertIn("Result: PASS", out.getvalue())

    def test_check_plan_reports_every_slice_defect_at_once(self):
        bad = self.repo / "bad.md"
        bad.write_text(
            self.plan.read_text(encoding="utf-8")
            .replace("### Rollback Path\n- Revert README.md.", "")
            .replace("Approval needed before implementation: no.\n\n### Validation Plan\n- Commands to run:\n  - git diff --check\n\n### Rollback Path",
                     "Approval needed before implementation: not yet decided.\n\n### Validation Plan\n- Commands to run:\n  - git diff --check\n\n### Rollback Path"),
            encoding="utf-8",
        )
        report = mc.plan_check_report(bad)
        joined = "\n".join(report["errors"])
        self.assertIn("Slice 1", joined)
        self.assertIn("missing required sections: Rollback Path", joined)
        self.assertIn("Slice 2", joined)
        self.assertIn("must be exactly 'yes' or 'no'", joined)
        args = argparse.Namespace(plan=str(bad))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mc.check_plan(args), 1)
        self.assertIn("Result: FAIL", out.getvalue())

    def test_check_plan_reports_approval_gated_without_error(self):
        write_plan(self.plan, approval="yes")
        report = mc.plan_check_report(self.plan)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["approval_gated"], ["Slice 1"])

    def test_check_plan_rejects_malformed_slice_heading_that_would_hide_work(self):
        for malformed in [
            "## Slice 2 - Second Slice",
            "### Slice 2: Second Slice",
            "## slice 2: Second Slice",
            "## Slice2: Second Slice",
            "  ## Slice 2 - Second Slice",
            "## Slice two: Second Slice",
            "## Slice: Second Slice",
            "## Slice Setup",
        ]:
            with self.subTest(malformed=malformed):
                write_plan(self.plan)
                self.plan.write_text(
                    self.plan.read_text(encoding="utf-8").replace("## Slice 2: Second Slice", malformed),
                    encoding="utf-8",
                )

                report = mc.plan_check_report(self.plan)

                self.assertEqual(report["slice_count"], 1)
                self.assertTrue(report["errors"])
                self.assertIn(f"malformed slice heading {malformed!r}", "\n".join(report["errors"]))

        write_plan(self.plan)
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8") + "\n## Slice Batches\n\n## Slice-Level Design\n",
            encoding="utf-8",
        )
        self.assertEqual(mc.plan_check_report(self.plan)["errors"], [])

    def test_check_plan_rejects_authorized_entries_that_match_no_git_path(self):
        for entry in [".", "./", "/", "``", "../outside.md", "docs//file.md", "docs\\file.md"]:
            with self.subTest(entry=entry):
                write_plan(self.plan)
                self.plan.write_text(
                    self.plan.read_text(encoding="utf-8").replace("  - README.md", f"  - {entry}"),
                    encoding="utf-8",
                )

                report = mc.plan_check_report(self.plan)
                runnable, reasons = mc.eligibility(mc.parse_plan(self.plan)[0])

                self.assertIn("invalid authorized surface", "\n".join(report["errors"]))
                self.assertFalse(runnable)
                self.assertIn("invalid authorized surface", "\n".join(reasons))

    def test_check_plan_warns_on_dependency_and_license_surfaces(self):
        text = self.plan.read_text(encoding="utf-8").replace(
            "- Files allowed to change:\n  - CHANGELOG.md",
            "- Files allowed to change:\n  - package.json\n  - LICENSE\n  - poetry.lock",
        )
        self.plan.write_text(text, encoding="utf-8")
        report = mc.plan_check_report(self.plan)
        self.assertEqual(report["errors"], [])
        joined = "\n".join(report["warnings"])
        self.assertIn("'package.json' looks dependency-shaped", joined)
        self.assertIn("'poetry.lock' looks dependency-shaped", joined)
        self.assertIn("'LICENSE' looks license-shaped", joined)

    def test_check_plan_warns_on_whole_repo_surface_and_batches(self):
        text = self.plan.read_text(encoding="utf-8").replace(
            "- Files allowed to change:\n  - CHANGELOG.md",
            "- Files allowed to change:\n  - `**`",
        ) + "\n## Slice Batches\n\n- Batch A: Slices 1-2 — related docs.\n"
        self.plan.write_text(text, encoding="utf-8")
        report = mc.plan_check_report(self.plan)
        self.assertEqual(report["errors"], [])
        joined = "\n".join(report["warnings"])
        self.assertIn("authorizes the entire repository", joined)
        self.assertIn("batches bind in Mode A sessions only", joined)
        self.assertIn("matches top-level paths only", mc.surface_lint("*"))

    def test_check_plan_does_not_lint_an_invalid_authorized_entry(self):
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8").replace("  - README.md", "  - /abs/package.json"),
            encoding="utf-8",
        )

        report = mc.plan_check_report(self.plan)

        self.assertIn("invalid authorized surface", "\n".join(report["errors"]))
        self.assertNotIn("package.json", "\n".join(report["warnings"]))

    def test_check_plan_rejects_unwrapped_annotation_entries(self):
        for entry in ["README.md (new file)", "README.md - new helper"]:
            with self.subTest(entry=entry):
                write_plan(self.plan)
                self.plan.write_text(
                    self.plan.read_text(encoding="utf-8").replace("  - README.md", f"  - {entry}"),
                    encoding="utf-8",
                )

                report = mc.plan_check_report(self.plan)
                runnable, reasons = mc.eligibility(mc.parse_plan(self.plan)[0])

                self.assertIn("unwrapped whitespace", "\n".join(report["errors"]))
                self.assertFalse(runnable)
                self.assertIn("unwrapped whitespace", "\n".join(reasons))

        # Backtick-wrapping stays the escape hatch: annotated entries and paths
        # that genuinely contain spaces remain expressible.
        for entry in ["`README.md` (new file)", "`my notes.md`"]:
            with self.subTest(entry=entry):
                write_plan(self.plan)
                self.plan.write_text(
                    self.plan.read_text(encoding="utf-8").replace("  - README.md", f"  - {entry}"),
                    encoding="utf-8",
                )
                self.assertEqual(mc.plan_check_report(self.plan)["errors"], [])

    def test_check_plan_warns_when_plain_entry_names_existing_directory(self):
        (self.repo / "docs").mkdir()
        for entry, expect_warning in [("docs", True), ("docs/", False), ("docs/**", False), ("missing/", False)]:
            with self.subTest(entry=entry):
                write_plan(self.plan)
                self.plan.write_text(
                    self.plan.read_text(encoding="utf-8").replace("  - README.md", f"  - {entry}"),
                    encoding="utf-8",
                )

                report = mc.plan_check_report(self.plan, repo=self.repo)

                self.assertEqual(report["errors"], [])
                self.assertEqual(
                    "names an existing directory" in "\n".join(report["warnings"]),
                    expect_warning,
                )

        # Without repo context the worktree lint is skipped, not guessed.
        write_plan(self.plan)
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8").replace("  - README.md", "  - docs"),
            encoding="utf-8",
        )
        self.assertNotIn("names an existing directory", "\n".join(mc.plan_check_report(self.plan)["warnings"]))

    def test_init_surfaces_directory_entry_warning(self):
        (self.repo / "docs").mkdir()
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8").replace("  - README.md", "  - docs"),
            encoding="utf-8",
        )
        self.prepare_committed_repo()
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mc.init_run(args), 0)
        self.assertIn("names an existing directory", out.getvalue())

    def test_check_plan_warns_on_batch_heading_at_any_level(self):
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8") + "\n#### Slice Batches\n\n- Batch A: Slices 1-2\n",
            encoding="utf-8",
        )

        report = mc.plan_check_report(self.plan)

        self.assertEqual(report["errors"], [])
        self.assertIn("batches bind in Mode A sessions only", "\n".join(report["warnings"]))

    def test_check_plan_rejects_slice_like_headings_inside_code_fences(self):
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8") + "\n```md\n## Slice 9: Fenced Example\n```\n",
            encoding="utf-8",
        )

        report = mc.plan_check_report(self.plan)

        self.assertIn("sits inside a fenced code block", "\n".join(report["errors"]))

        # A fenced batch heading is documentation, not a batch grouping: no
        # fence error (batch headings are reserved) and no batch warning.
        write_plan(self.plan)
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8") + "\n```md\n## Slice Batches\n```\n",
            encoding="utf-8",
        )
        report = mc.plan_check_report(self.plan)
        self.assertEqual(report["errors"], [])
        self.assertNotIn("batches bind in Mode A sessions only", "\n".join(report["warnings"]))

    def test_check_plan_rejects_unclosed_code_fence(self):
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8") + "\n```\nstray fenced text\n",
            encoding="utf-8",
        )

        report = mc.plan_check_report(self.plan)

        self.assertIn("unclosed code fence", "\n".join(report["errors"]))

    def test_init_fails_closed_on_plan_sanity_errors_in_any_slice(self):
        # The defect is in Slice 2, not the next runnable slice: init must
        # still stop, so plan problems surface before the workflow begins.
        text = self.plan.read_text(encoding="utf-8").replace(
            "## Slice 2: Second Slice\n\n### Intended Change\n- Add more docs.\n",
            "## Slice 2: Second Slice\n\n### Intended Change\n- Add more docs.\n\n",
        ).replace("- Approval needed before implementation: no.\n\n### Validation Plan\n- Commands to run:\n  - git diff --check\n\n### Rollback Path\n- Revert CHANGELOG.md.",
                  "- Approval needed before implementation: none.\n\n### Validation Plan\n- Commands to run:\n  - git diff --check\n\n### Rollback Path\n- Revert CHANGELOG.md.")
        self.plan.write_text(text, encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with self.assertRaisesRegex(mc.McError, "pre-run sanity check"):
            mc.init_run(args)
        self.assertFalse((self.repo / ".ai-mc").exists())

    def test_init_prints_plan_warnings_but_proceeds(self):
        text = self.plan.read_text(encoding="utf-8").replace(
            "- Files allowed to change:\n  - CHANGELOG.md",
            "- Files allowed to change:\n  - requirements.txt",
        )
        self.plan.write_text(text, encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan), harness="codex", worktree_root=None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mc.init_run(args), 0)
        self.assertIn("Plan warning:", out.getvalue())
        self.assertIn("requirements.txt", out.getvalue())

    def test_run_state_creation(self):
        state = self.init_run()
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["repo_path"], str(self.repo.resolve()))
        self.assertEqual(state["plan_path"], str(self.plan.resolve()))
        self.assertEqual(state["harness"]["name"], "codex")
        self.assertEqual(state["plan"]["slice_count"], 2)
        self.assertEqual(state["supervision"]["mode"], "deterministic-batch")
        self.assertIn("rolling_usage_limit", state["supervision"]["pause_policy"])
        self.assertEqual(state["supervision"]["pause_counters"]["cumulative_pause_seconds_run"], 0)
        self.assertEqual(state["operational_events_path"], f".ai-mc/runs/{state['run_id']}/operational-events.jsonl")

    def test_init_can_create_and_switch_to_authorized_branch(self):
        self.prepare_committed_repo()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            branch="mc-trial/pi-calculator",
            create_branch=True,
        )

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)

        self.assertEqual(git(self.repo, "branch", "--show-current"), "mc-trial/pi-calculator")
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["branch"], "mc-trial/pi-calculator")

    def test_init_requires_authorization_to_create_missing_branch(self):
        self.prepare_committed_repo()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            branch="missing-branch",
            create_branch=False,
        )

        with self.assertRaisesRegex(mc.McError, "does not exist"):
            mc.init_run(args)

    def test_init_refuses_branch_switch_from_dirty_worktree(self):
        self.prepare_committed_repo()
        (self.repo / "pi_calculator.py").write_text("dirty\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            branch="mc-trial/pi-calculator",
            create_branch=True,
        )

        with self.assertRaisesRegex(mc.McError, "dirty worktree"):
            mc.init_run(args)

    def test_old_run_state_loads_with_supervision_defaults(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        state.pop("supervision")
        state.pop("operational_events_path")
        run_json.write_text(json.dumps(state), encoding="utf-8")

        loaded = mc.load_run(run_json)

        self.assertEqual(loaded["supervision"]["mode"], "deterministic-batch")
        self.assertEqual(loaded["supervision"]["max_consecutive_pauses_per_slice"], 2)
        self.assertEqual(loaded["operational_events_path"], f".ai-mc/runs/{state['run_id']}/operational-events.jsonl")

    def test_append_operational_event_does_not_rewrite_run_json(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        before = run_json.read_text(encoding="utf-8")

        event = mc.append_operational_event(self.repo, state, {"kind": "manual_note", "status": "recorded"})
        second = mc.append_operational_event(self.repo, state, {"kind": "manual_note", "status": "recorded"})

        self.assertEqual(run_json.read_text(encoding="utf-8"), before)
        self.assertEqual(event["event_id"], "op-0001")
        self.assertEqual(second["event_id"], "op-0002")
        event_path = self.repo / state["operational_events_path"]
        self.assertTrue(event_path.exists())
        records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["event_id"] for record in records], ["op-0001", "op-0002"])
        self.assertEqual(records[0]["kind"], "manual_note")

    def test_current_slice_state_records_before_head_and_pause_slot(self):
        plan_slice = mc.parse_plan(self.plan)[0]
        state = mc.current_slice_state(
            self.repo,
            plan_slice,
            self.repo / ".ai-mc" / "runs" / "test" / "slices" / "slice-001",
            "mc_test_slice-001_a1",
            1,
            "2026-01-01T00:00:00Z",
            "a" * 40,
        )

        self.assertEqual(state["before_head"], "a" * 40)
        self.assertEqual(state["pause"], None)
        self.assertEqual(state["artifact_dir"], ".ai-mc/runs/test/slices/slice-001")

    def test_status_displays_paused_current_slice_fields(self):
        state = self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        state["status"] = "paused"
        state["current_slice"] = {
            "slice_id": "Slice 1",
            "title": "First Slice",
            "artifact_dir": ".ai-mc/runs/test/slices/slice-001",
            "tmux_session": "mc_test_slice-001_a1",
            "attempt": 1,
            "started_at": "2026-01-01T00:00:00Z",
            "before_head": "b" * 40,
            "pause": {"paused_until": "2026-01-01T01:00:00Z", "reason": "rolling usage limit reset"},
        }
        run_json.write_text(json.dumps(state), encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            self.assertEqual(mc.status(argparse.Namespace(repo=str(self.repo), run="current")), 0)

        rendered = output.getvalue()
        self.assertIn("Supervision mode: deterministic-batch", rendered)
        self.assertIn("Current before_head: " + "b" * 40, rendered)
        self.assertIn("Paused until: 2026-01-01T01:00:00Z (rolling usage limit reset)", rendered)

    def test_runnable_slice(self):
        slices = mc.parse_plan(self.plan)
        runnable, reasons = mc.eligibility(slices[0])
        self.assertTrue(runnable)
        self.assertEqual(reasons, [])
        self.assertEqual(slices[0].authorized_files, ["README.md"])

    def test_approval_needed_slice_blocks(self):
        write_plan(self.plan, approval="yes")
        slices = mc.parse_plan(self.plan)
        runnable, reasons = mc.eligibility(slices[0])
        self.assertFalse(runnable)
        self.assertTrue(any(reason.startswith("slice is approval-needed") for reason in reasons), reasons)

    def test_missing_authorized_surface_blocks(self):
        write_plan(self.plan, include_authorized=False)
        slices = mc.parse_plan(self.plan)
        runnable, reasons = mc.eligibility(slices[0])
        self.assertFalse(runnable)
        self.assertIn("authorized surface has no files allowed to change", reasons)

    def test_next_slice_skips_completed_state(self):
        self.init_run()
        run_json = (self.repo / ".ai-mc" / "current").resolve() / "run.json"
        state = json.loads(run_json.read_text(encoding="utf-8"))
        state["slices"].append({"slice_id": "Slice 1", "status": "pass"})
        run_json.write_text(json.dumps(state), encoding="utf-8")
        slices = mc.parse_plan(self.plan)
        self.assertEqual(mc.next_slice(slices, state).slice_id, "Slice 2")

    def test_previous_completed_head_returns_prior_completed_commit(self):
        state = {
            "slices": [
                {
                    "slice_id": "Slice 1",
                    "status": "pass",
                    "commit": {"hash": "a" * 40},
                },
                {
                    "slice_id": "Slice 2",
                    "status": "fail",
                    "commit": {"hash": "b" * 40},
                },
            ],
        }

        self.assertEqual(mc.previous_completed_head(state, "Slice 2"), "a" * 40)

    def test_final_slice_stops_before_future_work(self):
        self.plan.write_text(
            self.plan.read_text(encoding="utf-8")
            + """
## Future Work Outside This Plan

- Do not include this in Slice 2.

## Next Chat Prompt

Continue later.
""",
            encoding="utf-8",
        )
        slices = mc.parse_plan(self.plan)
        self.assertNotIn("Future Work", slices[-1].sections["Rollback Path"])
        self.assertNotIn("Next Chat Prompt", slices[-1].sections["Rollback Path"])

    def test_approval_free_text_blocks(self):
        for value in ["not yet decided", "none", "maybe later"]:
            write_plan(self.plan, approval=value)
            plan_slice = mc.parse_plan(self.plan)[0]
            self.assertIsNone(plan_slice.approval_needed, value)
            runnable, reasons = mc.eligibility(plan_slice)
            self.assertFalse(runnable, value)
            self.assertIn("approval-needed risk flag is missing or unclear", reasons)

    def test_approval_exact_no_runs(self):
        write_plan(self.plan, approval="no")
        self.assertFalse(mc.parse_plan(self.plan)[0].approval_needed)

    def _slice_with_risk_flags(self, risk_flags: str) -> "mc.PlanSlice":
        return mc.PlanSlice(1, "t", "", {"Risk Flags": risk_flags})

    def test_independent_audit_required_exact_yes_arms_gate(self):
        plan_slice = self._slice_with_risk_flags(
            "- Approval needed before implementation: no\n- Independent audit required: yes"
        )
        self.assertTrue(plan_slice.independent_audit_required)

    def test_independent_audit_required_exact_no_leaves_gate_off(self):
        plan_slice = self._slice_with_risk_flags(
            "- Approval needed before implementation: no\n- Independent audit required: no"
        )
        self.assertFalse(plan_slice.independent_audit_required)

    def test_independent_audit_required_absent_defaults_off(self):
        plan_slice = self._slice_with_risk_flags("- Approval needed before implementation: no")
        self.assertFalse(plan_slice.independent_audit_required)

    def test_independent_audit_required_unclear_defaults_off(self):
        # Fails closed to off, unlike approval_needed which blocks on unclear:
        # independence is a degradable preference, so ambiguity means "not armed".
        for value in ["maybe", "not yet", "", "true", "required"]:
            plan_slice = self._slice_with_risk_flags(f"- Independent audit required: {value}")
            self.assertFalse(plan_slice.independent_audit_required, value)

    def test_authorized_files_ignores_stray_bullet(self):
        plan_slice = mc.PlanSlice(
            1,
            "t",
            "",
            {
                "Authorized Surface": (
                    "- Files allowed to change:\n"
                    "  - README.md\n"
                    "- Note: be careful in this area\n"
                    "- Tests allowed or expected to change: none."
                )
            },
        )
        self.assertEqual(plan_slice.authorized_files, ["README.md"])

    def test_is_authorized_path_glob_is_segment_aware(self):
        self.assertTrue(mc.is_authorized_path("a.md", ["*.md"]))
        self.assertFalse(mc.is_authorized_path("deep/a.md", ["*.md"]))
        self.assertTrue(mc.is_authorized_path("deep/a.md", ["**/*.md"]))
        self.assertTrue(mc.is_authorized_path("src/a.py", ["src/*.py"]))
        self.assertFalse(mc.is_authorized_path("src/deep/a.py", ["src/*.py"]))

    def test_normalize_authorized_entry_strips_backtick_with_trailing_annotation(self):
        # Regression: entries like "`file.py` (new file)" were previously
        # normalized to "file.py` (new file)" because str.strip("`") only
        # trims from the very ends of the string, so a closing backtick
        # followed by an annotation was never removed.
        self.assertEqual(mc.normalize_authorized_entry("`nilakantha.py` (new file)"), "nilakantha.py")
        self.assertEqual(
            mc.normalize_authorized_entry(
                "`tests/__init__.py` (new file, only if required for test discovery; must stay empty)"
            ),
            "tests/__init__.py",
        )
        self.assertEqual(mc.normalize_authorized_entry("`*.md`"), "*.md")
        self.assertEqual(mc.normalize_authorized_entry("pi_calculator.py"), "pi_calculator.py")
        self.assertTrue(mc.is_authorized_path("nilakantha.py", ["`nilakantha.py` (new file)"]))

    # --- Review fixes: fail-closed gate ----------------------------------

    def test_init_writes_self_ignoring_gitignore(self):
        self.init_run()
        gitignore = self.repo / ".ai-mc" / ".gitignore"
        self.assertTrue(gitignore.exists())
        self.assertEqual(gitignore.read_text(encoding="utf-8"), "*\n")

    def test_init_records_plan_digest(self):
        state = self.init_run()
        self.assertEqual(state["plan"]["sha256"], mc.plan_digest(self.plan))

    def test_verify_plan_unchanged_stops_on_edit(self):
        state = self.init_run()
        self.plan.write_text(self.plan.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
        with self.assertRaisesRegex(mc.McError, "plan file changed"):
            mc.verify_plan_unchanged(state, self.plan)

    def test_init_rejects_duplicate_slice_numbers(self):
        dup = self.repo / "dup.md"
        dup.write_text("# Plan\n\n## Slice 1: A\n\n## Slice 1: B\n", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), plan=str(dup), harness="codex", worktree_root=None)
        with self.assertRaisesRegex(mc.McError, "duplicate slice numbers"):
            mc.init_run(args)

    def test_slice_entry_records_before_head(self):
        gate = mc.GateDecision("pass", "ok", {"changed_files": []}, ())
        entry = mc.slice_entry_from_gate(self.repo, mc.parse_plan(self.plan)[0], self.repo / "art", "2026-01-01T00:00:00Z", gate, "abc123")
        self.assertEqual(entry["before_head"], "abc123")

    def test_approve_command_clears_explicit_yes_gate(self):
        write_plan(self.plan, approval="yes")
        self.prepare_committed_repo()
        state = self.init_run()
        plan_slice = mc.parse_plan(self.plan)[0]
        runnable, reasons = mc.eligibility(plan_slice, mc.approved_slice_ids(state))
        self.assertFalse(runnable)
        self.assertTrue(any("approve command" in reason for reason in reasons))

        approve_args = argparse.Namespace(repo=str(self.repo), run="current", slice="Slice 1", reason="risk reviewed")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.approve_slice(approve_args), 0)

        updated = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertIn("Slice 1", updated["approvals"])
        self.assertEqual(updated["approvals"]["Slice 1"]["reason"], "risk reviewed")
        runnable, reasons = mc.eligibility(plan_slice, mc.approved_slice_ids(updated))
        self.assertTrue(runnable, reasons)
        events_path = self.repo / updated["operational_events_path"]
        records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(record.get("kind") == "approval" and record.get("slice_id") == "Slice 1" for record in records))

        dry_args = argparse.Namespace(repo=str(self.repo), run="current", dry_run=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.run_next(dry_args), 0)

    def test_approve_rejects_non_gated_slice(self):
        self.prepare_committed_repo()
        self.init_run()
        approve_args = argparse.Namespace(repo=str(self.repo), run="current", slice="Slice 1", reason="")
        with self.assertRaisesRegex(mc.McError, "not approval-gated"):
            mc.approve_slice(approve_args)

    def test_approval_does_not_clear_unclear_flag(self):
        write_plan(self.plan, approval="not yet decided")
        plan_slice = mc.parse_plan(self.plan)[0]
        runnable, reasons = mc.eligibility(plan_slice, {"Slice 1"})
        self.assertFalse(runnable)
        self.assertTrue(any("missing or unclear" in reason for reason in reasons))

    def test_init_assume_complete_adopts_prior_slices(self):
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            assume_complete="Slice 1",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["slices"]), 1)
        entry = state["slices"][0]
        self.assertEqual(entry["slice_id"], "Slice 1")
        self.assertEqual(entry["status"], "assumed-complete")
        self.assertIn("operator attested", entry["gate_reason"])
        candidate = mc.next_slice(mc.parse_plan(self.plan), state)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.slice_id, "Slice 2")

    def test_init_assume_complete_rejects_unknown_slice(self):
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            assume_complete="Slice 99",
        )
        with self.assertRaisesRegex(mc.McError, "not in the plan"):
            mc.init_run(args)

    def test_init_policy_flags_are_recorded(self):
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan),
            harness="codex",
            worktree_root=None,
            max_repair_attempts=1,
            no_commit_required=True,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mc.init_run(args), 0)
        state = json.loads(((self.repo / ".ai-mc" / "current").resolve() / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["policy"]["max_repair_attempts"], 1)
        self.assertFalse(state["policy"]["commit_required"])
        self.assertEqual(state["approvals"], {})

    # --- Gate hardening (validation artifact, worker success) -------------

    def test_event_counter_seeds_from_existing_log(self):
        state = self.init_run()
        event_path = self.repo / state["operational_events_path"]
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text(
            "\n".join(json.dumps({"event_id": f"op-{n:04d}", "kind": "observation"}) for n in (1, 2, 3)) + "\n",
            encoding="utf-8",
        )
        record = mc.append_operational_event(self.repo, state, {"kind": "manual_note", "status": "recorded"})
        self.assertEqual(record["event_id"], "op-0004")
        counter_path = event_path.with_name(event_path.name + ".counter")
        self.assertEqual(counter_path.read_text(encoding="utf-8").strip(), "4")


if __name__ == "__main__":
    unittest.main()
