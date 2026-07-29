---
name: commit
description: Use this skill whenever creating or staging a git commit.
---

## Commit Message Format
- Line 1: short imperative summary (under 72 chars)
- Blank line
- Body: rationale + every new/changed/removed file with reason, grouped logically

## Steps
1. Run `git status` and `git diff` to review all changes
2. Keep all staging and commit mechanics with the Developer. Reviewers are read-only and must never perform Git or GitHub mutations.
3. Run the `lint` skill over what is about to land, **before staging**: `check --base HEAD` (or the slice's starting commit). It sees uncommitted and untracked work, so it covers exactly what the commit will contain. Do not commit a new lint finding without either fixing it or stating why it stands. A missing linter is an uncovered gap to report, not a pass — if the repository has no linter for the changed languages, say so and continue.
4. Stage specific files by name — never `git add -A` or `git add .`
5. Commit using a HEREDOC:

```bash
git commit -m "$(cat <<'EOF'
Short summary

Detailed description

Changed files:
- path/to/file: reason
EOF
)"
```

6. Run `git rev-parse HEAD` immediately after a successful commit and record/copy the exact 40-character hash from that command when any workflow asks for the commit hash. Do not infer a full hash from abbreviated `git commit` output.
7. Run `git status` to confirm success
8. Never use `--no-verify` and never amend — if a hook fails, fix the issue and create a new commit
