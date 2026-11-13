# AGENTS.md

## Project Overview

AutoPkg CI/CD pipeline for fetching Mac software installers, processing them through Munki, and uploading to **Workspace ONE UEM (WS1)**. Runs as GitHub Actions on macOS runners. Forked from Facebook/Gusto's `autopkg_tools.py` with WS1-specific extensions.

## Architecture

Two parallel execution modes exist — pick the right files:

| Mode | Python script | Launcher (local) | Workflow (CI) |
|---|---|---|---|
| **Cloud CLI** (active) | `autopkg_tools_ws1_cloud_cli.py` | `autopkg_tools_launcher_cloud_cli.zsh` | `.github/workflows/autopkg_ws1_cloud_cli.yml` |
| **Classic** (on-prem only) | `autopkg_tools_ws1_classic.py` | `autopkg_tools_launcher_classic.zsh` | `workflows/autopkg.yml` |

**Cloud CLI** uses `cloud-autopkg-runner` (PyPI) and is the production path. **Classic** calls `/usr/local/bin/autopkg run` directly — kept for on-prem self-hosted runners only.

**Data flow:** Recipe list (`autopkg/recipe_list.json`) → Python script iterates recipes → AutoPkg runs each recipe override (`autopkg/overrides/*.ws1.recipe.yaml`) → results parsed from plist reports → new packages committed to `munki_repo/` on a per-version branch → pushed to separate Munki Git LFS repo → Slack notification sent.

## Key Directories & Files

- `autopkg/overrides/` — YAML recipe overrides; each defines WS1 assignment rules, parent recipe, and trust info
- `autopkg/recipe_list.json` — JSON array of override filenames to process
- `autopkg/repo_list.txt` — upstream AutoPkg recipe repos to clone
- `autopkg/cache/` — local AutoPkg download cache (per-recipe subdirs)
- `autopkg/reports/` — plist report files generated per run
- `metadata_cache.json` — download metadata cache committed back to repo after cloud CLI runs
- `CA-certificates/` — custom CA bundle for TLS inspection (set via `REQUESTS_CA_BUNDLE`)
- `munki_repo/` — checked-out Munki Git LFS repo (separate GitHub repo)
- `munki_repo_branch_merger.sh` — merges Munki repo branches older than 2 weeks

## Environment Variables

AutoPkg input variables are passed as `AUTOPKG_<key>` env vars (case-sensitive after prefix). Examples:
- `AUTOPKG_verbose`, `AUTOPKG_ws1_force_import`, `AUTOPKG_ws1_update_assignments`, `AUTOPKG_ws1_app_versions_prune`, `AUTOPKG_ws1_prune_safeguard_halt_on_assigned`
- `AUTOPKG_TOOLS_VERBOSE` — controls wrapper script verbosity (1=normal, 2=debug, 3+=suppresses Slack)
- WS1 secrets: `AUTOPKG_WS1_OAUTH_CLIENT_ID`, `AUTOPKG_WS1_OAUTH_CLIENT_SECRET`, `AUTOPKG_WS1_OAUTH_TOKEN_URL`, `AUTOPKG_WS1_API_URL`, `AUTOPKG_WS1_CONSOLE_URL`, `AUTOPKG_WS1_GROUPID`
- `REQUESTS_CA_BUNDLE` — path to CA cert bundle; preferred over legacy MacSesh injection
- `RECIPE_TO_RUN` (cloud CLI) / `RECIPE` (classic) — optional single recipe override

## Conventions

- **Code style:** Black formatter; use **f-strings** for all string formatting and log messages — do not use `%` (printf-style) or `.format()` notation
- **Python:** Runs under `/usr/local/autopkg/python` (AutoPkg's bundled Python 3), not system Python
- **Dependencies:** `pyproject.toml` + `uv.lock` for cloud CLI mode (uses `uv run`); `requirements_all.txt` for AutoPkg's own pip (classic mode). `requirements.txt` is intentionally empty — GHAS workaround
- **Recipe overrides:** Always YAML (`.ws1.recipe.yaml`), identifier pattern `local.ws1.<AppName>`
- **Override Input stays minimal:** keep only keys that *differ* from the parent chain — the `WS1_*: none` placeholders (real values arrive as `AUTOPKG_WS1_*`) plus any deliberate local setting (smart groups, retention, `PLATFORM_ARCH`…). Drop the inherited Inputs that `autopkg make-override` copies verbatim (`NAME`, `pkginfo`, `MUNKI_REPO_SUBDIR`, download URLs…): a pinned copy silently diverges when upstream improves them, and `pkginfo` is replaced wholesale, not merged
- **Git branch naming:** `{AppName}_{Version}` per imported package, pushed to Munki repo
- **GitHub Actions:** Pin actions by SHA1 commit hash, never by tag — see `uses:` lines in workflows
- **Trust info:** Recipe trust verification runs before each recipe; failures generate a PR to update overrides

## Running Locally

```zsh
# CAR library mode (CAR library):
./autopkg_tools_launcher_car.zsh [OptionalRecipe.ws1.recipe.yaml]

# Cloud CLI mode:
./autopkg_tools_launcher_cloud_cli.zsh [OptionalRecipe.ws1.recipe.yaml]

# Classic mode:
./autopkg_tools_launcher_classic.zsh [OptionalRecipe.ws1.recipe.yaml]
```

Launcher scripts store WS1 secrets in a dedicated macOS Keychain (`autopkg_tools_launcher_keychain`) — first run prompts for each secret. Requires AutoPkg and Munki installed at `/usr/local/`.

- **Local AutoPkg preferences:** Prefer registering recipe repos in the global `com.github.autopkg` preferences with `autopkg repo-add <URL>` (without `--prefs`), so plain `autopkg` commands resolve the same recipes. Do not work around missing global repo registration by updating only `autopkg/autopkg_prefs.plist` and validating with `--prefs`. When the project prefs snapshot is needed, refresh it with `defaults export com.github.autopkg autopkg/autopkg_prefs.plist` after updating global preferences, and verify recipe discovery without `--prefs`.

## Branch Hygiene in munki_repo

Run `./munki_repo_hygiene.zsh [--gc] [--keep N]` regularly on the operator Mac. It performs, with confirmation prompts:

1. `git fetch --prune` — drop `origin/*` tracking refs for deleted remote branches
2. Untracked-file cleanup — each file from `git ls-files --others` is checked against **every origin ref** before removal. Files already on `origin/main` or an origin branch are safe to delete (recoverable); files that exist *only* locally (a local AutoPkg run that was never pushed) are flagged ⚠ and need a separate confirmation
3. Delete local branches whose upstream is `[gone]` (already merged by CI and deleted on origin) — but only when all their commits are reachable from an origin ref. A `[gone]` branch with unpushed commits (e.g. an interrupted push) is flagged ⚠ and kept, since deleting it would let step 5 remove its package binary too
4. Optional Munki `repoclean`, **local only** — keeps `N` versions per item (`--keep`, default 10) and shows repoclean's own preview and double prompt. The deletions are never committed or pushed: pruning the shared repo is the job of the weekly `.github/workflows/munki_repoclean.yml` run (keep 10), so a lower local `--keep` cannot shorten its retention. Deleted files show as ` D` in `git status`; leave them uncommitted. They come back on `git reset --hard` (the launchers do this when the pull fails) or `git -C munki_repo checkout -- pkgs pkgsinfo`. This frees the working-tree copy only; the LFS cache keeps everything `main` still references
5. `git lfs prune --verify-remote` — drop cached LFS objects not needed by current refs, after origin confirms it holds each one (the big win: tens of GB reclaimable)
6. With `--gc`: `git gc --aggressive` for a further ~5–10%

**Why:** `git fetch --prune` only removes *tracking* refs, never local branches; and the LFS object cache keeps every version you've ever pulled. The launchers already run `git reset --hard origin/main` for tracked-file deletions, so the script covers only what they don't.

## Adding a New Recipe

Use the generator (see `.github/skills/add-ws1-recipe/SKILL.md` for the full procedure):

```zsh
uv run scripts/override_generator/generate_ws1_overrides.py <App> --dry-run   # preview
uv run scripts/override_generator/generate_ws1_overrides.py <App>             # create + register
```

It wraps `autopkg make-override --format=yaml` for the upstream `<App>.ws1`, `<App>.ws1-assigner` and `<App>.ws1-pruner` recipes, applies `scripts/override_generator/templates/overrides_defaults.local.yaml` (org-specific smart groups, not synced publicly; falls back to the generic `overrides_defaults.yaml` with a NOTICE), and registers the files in `recipe_list.json`, `recipe_list_assigner.txt`, `recipe_list_pruner.txt` (and `repo_list.txt` if needed). Exit 2 means an upstream assigner/pruner recipe is missing — add it upstream, don't fabricate it. Tests: `uv run python -m unittest discover -s scripts/override_generator/tests`.


## Prompt and results summary saving style

- **Prompt and results** are saved in a new, separate Markdown file, with the prompt at the top and results below.
- Folder structure is `dev-work/<theme>/<number>. REFACTOR_PROMPT <short-description>.md`
- `<theme>` is the high-level refactor or diagnosis topic (e.g. "2026 refactor for cloud optimisation")
- `<number>` is a sequential number for each prompt within that theme.

## Scripts generated by AI agents
- are saved in the `dev work/<theme>/refactor and diagnosis prompts, supporting scripts/ ` folder,
- with the same naming convention as above.
- These are not meant to be run directly, but rather serve as a record of the refactor and diagnosis


## workaround for file creation issues in the chat AI
- The tooling must use the workaround for create_file **with a literal (unescaped) path containing spaces.**
- This is needed because create_file with a path containing spaces is currently producing an error in the chat AI, and this workaround allows us to continue saving our prompt and results documents without interruption.
- Also, the use of a Heredoc in this context is failing, so that is not an option until the tooling is fixed.
- Therefore, when generating refactor-prompt Markdown files in `dev work/.../`, always use `create_file` with a literal (unescaped) path.
