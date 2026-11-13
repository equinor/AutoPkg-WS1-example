---
name: add-ws1-recipe
description: Add a new application from the WorkSpaceOneImporter-recipes repo to this AutoPkg CI/CD setup by generating and registering the local WS1 importer, assigner and pruner overrides with scripts/override_generator/generate_ws1_overrides.py. Use when asked to add, onboard or create overrides for a new WS1 recipe/app.
---

# Add a WS1 recipe (importer + assigner + pruner overrides)

Every application in this repo has three local overrides in `autopkg/overrides/`, each
registered in its own list:

| Kind     | Override file                           | Identifier                     | Registered in                        |
|----------|-----------------------------------------|--------------------------------|--------------------------------------|
| importer | `<Name>.ws1.recipe.yaml`                | `local.ws1.<Name>`             | `autopkg/recipe_list.json`           |
| assigner | `<Name>.ws1-assigner.recipe.yaml`       | `local.ws1-assigner.<Name>`    | `autopkg/recipe_list_assigner.txt`   |
| pruner   | `<Name>.ws1-pruner.recipe.yaml`         | `local.ws1-pruner.<Name>`      | `autopkg/recipe_list_pruner.txt`     |

The upstream parents live in `autopkg/repos/com.github.autopkg.WorkSpaceOneImporter-recipes/ws1-recipes/`
as `<App>.ws1.recipe.yaml`, `<App>.ws1-assigner.recipe.yaml`, `<App>.ws1-pruner.recipe.yaml`.
Do **not** hand-copy an existing override: `ParentRecipeTrustInfo` hashes are recipe-specific
and must be computed by AutoPkg.

## Prerequisites

- macOS with AutoPkg at `/usr/local/bin/autopkg` and `uv` installed.
- Recipe repos cloned and prefs present: run any launcher once
  (`./autopkg_tools_launcher_car.zsh` writes `autopkg/autopkg_prefs.plist` and `repo-add`s
  every URL in `autopkg/repo_list.txt`), or `autopkg repo-add` the repos manually.
- If the upstream `WorkSpaceOneImporter-recipes` checkout has no recipe for the app, stop:
  the recipe must be added upstream first (see that repo's AGENTS.md). Never invent one here.

## Procedure

1. **Preview** (writes nothing, but does run `autopkg make-override` into a temp dir):
   ```zsh
   uv run scripts/override_generator/generate_ws1_overrides.py <App> --dry-run
   ```
   `<App>` is the upstream stem (`PolyStudio`), filename (`PolyStudio.ws1.recipe.yaml`) or
   importer identifier (`com.github.codeskipper.ws1.PolyStudio`).
   Check: all three kinds show `would-create`; no `*_HERE` placeholders remain; `ParentRecipeTrustInfo`
   lists the munki parent chain; list/repo additions look right.

2. **Decide the administrator settings** before writing. The template values are *repo policy
   defaults the generator will write* — they are a starting point, not a recommendation for the
   specific app, so review them with the user if not specified:
   - Assignment smart groups / delays — defaults come from
     `scripts/override_generator/templates/overrides_defaults.local.yaml` (organisation-specific,
     not synced publicly; TESTING 0d → QA 3d → PROD 14d). If that file is absent the generator
     falls back to the generic `overrides_defaults.yaml` (placeholder `GROUP-*` names) and prints a
     NOTICE — in that case create the `.local.yaml` with real smart group names before generating
     for production. Pass `--template <file>` for an app-specific variant, or edit the generated
     file afterwards.
   - Pruner retention — default `5` (`WS1_APP_VERSIONS_TO_KEEP`); existing overrides use 5/8/10
     depending on app. Override with `--set pruner:WS1_APP_VERSIONS_TO_KEEP=<n>`.
     The pruner safeguard is `WS1_PRUNE_SAFEGUARD_HALT_ON_ASSIGNED: 'False'` (the old
     `WS1_SAFEGUARD_HALT_ON_ASSIGNED` name is deprecated upstream — do not reintroduce it).
   - Extra Inputs required by the parent munki recipe (e.g. `PLATFORM_ARCH`) — use
     `--set [kind:]KEY=VALUE`. For per-architecture variants run the generator once per variant with
     `--name <App>-Intel` / `--name <App>-AppleSilicon`.
   - Keep `Input` minimal. `autopkg make-override` copies every inherited Input (`NAME`, `pkginfo`,
     `MUNKI_REPO_SUBDIR`, download URLs…) verbatim; after generating, remove keys whose value equals
     the parent chain's so the override only carries the `WS1_*: none` placeholders and deliberate
     local settings. A pinned copy diverges silently when upstream improves it, and `pkginfo` is a
     dict AutoPkg replaces wholesale rather than merges.

3. **Generate**:
   ```zsh
   uv run scripts/override_generator/generate_ws1_overrides.py <App> [--name ...] [--set ...]
   ```
   Exit codes: `0` complete, `2` partial (an upstream assigner/pruner is missing — report this,
   do not fabricate the missing override; `--allow-partial` acknowledges it), `1` error.
   Existing override files are never overwritten unless `--force` is given. Reruns are idempotent
   for the recipe lists and `repo_list.txt`. If some of the three overrides already exist, the
   generator checks they match the upstream set (Identifier, ParentRecipe, `NAME`) and refuses to
   complete an inconsistent set — investigate rather than `--force` past it. A `WARNING:` that the
   template sets a key the upstream recipe does not define usually means a renamed input: check
   the upstream processor docs and update the template.

4. **Review** the diff (`git diff --stat`, then the three new files). Confirm:
   - WS1 secret inputs are `none` (real values arrive as `AUTOPKG_WS1_*` env vars / secrets).
   - `ws1_app_assignments` names and `distr_delay_days` match what the user wants.
   - `WS1_APP_VERSIONS_TO_KEEP` is a quoted string.
   - `recipe_list.json` / `recipe_list_assigner.txt` / `recipe_list_pruner.txt` each gained exactly one line.

5. **Verify trust** for each generated override:
   ```zsh
   autopkg verify-trust-info --prefs autopkg/autopkg_prefs.plist <App>.ws1.recipe.yaml
   autopkg verify-trust-info --prefs autopkg/autopkg_prefs.plist <App>.ws1-assigner.recipe.yaml
   autopkg verify-trust-info --prefs autopkg/autopkg_prefs.plist <App>.ws1-pruner.recipe.yaml
   ```

6. **Validate tooling** if you changed the generator or template:
   ```zsh
   uv run python -m unittest discover -s scripts/override_generator/tests
   ```

7. Optionally test the importer locally with the launcher:
   `./autopkg_tools_launcher_car.zsh <App>.ws1.recipe.yaml` (requires Keychain-stored WS1 secrets).

## Recovering from a partial run

- Missing upstream assigner/pruner: generated kinds are kept and registered; the missing kind is
  not registered. Add the recipe upstream, `autopkg repo-update WorkSpaceOneImporter-recipes`,
  then rerun the generator (`--kinds pruner` to limit the work).
- Wrong settings: rerun with `--force` and the corrected `--set`/`--template`, or edit the YAML.
  Editing `Input` does not invalidate trust info; changing `ParentRecipe` does.
- To undo: delete the three files and remove their lines from the three recipe lists.

## Never

- Put real WS1 credentials, tenant URLs or group IDs in an override.
- Run the generator against a live tenant — it only invokes `autopkg make-override`/`repo-list`.
- Guess smart group names, delays or retention: derive from the template or ask.
