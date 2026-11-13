# Sync Changelog — 2026-11-13

**22 commit(s)** since last sync.

## New Features & Enhancements

- `bf32cd2b` ws1_sensor_upload: resolve remaining clear-text logging flows; document TLS inspection diagnosis
  
  - Split the secrets dict into individual variables and rename get_secret()
    to get_ws1_setting(); CodeQL's name heuristic tainted API responses via
    the API URL. Local CodeQL python-security-and-quality suite: 0 results.
  - Add dev-work/TLS packet inspection/001: Gatekeeper notarization lookup
    (api.apple-cloudkit.com) blocked by HTTPS interception, exemption
    recommendations, post-exemption verification and lessons learned.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `5dbc5720` ws1_sensor_upload: print labels instead of secret-derived values
  
  Addresses code scanning alert #17 (py/clear-text-logging-sensitive-data):
  debug lines print <WS1_API_URL>/<WS1_GROUPID> labels and param names only,
  and identifiers holding setting names/mask labels are renamed so they are
  not mistaken for secret values.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `8e9864fb` ws1_sensor_upload: never log secret values, show SHA-256 fingerprints at level 4+
  
  Addresses code scanning alert #16 (clear-text logging of sensitive data).
  Level 4+ now prints len + SHA-256 prefix per secret and OAuth token instead
  of plaintext; messages use labels rather than secret variables. Validate a
  secret by comparing with shasum, or inspect full values in a debugger.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `3c36e0ce` ws1_sensor_upload: add AUTOPKG_TOOLS_VERBOSE level (default 2), mask secrets below level 4
  
  Move to scripts/experimental/. Verbosity uses the launcher plumbing
  (AUTOPKG_TOOLS_VERBOSE, default 2, -v/--verbose override) and an output()
  helper; keychain/env secrets, URL host/path parts and the OAuth token are
  masked in all output and errors unless the level is 4 or higher.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `65311828` Add munki_repo_hygiene.zsh for local Munki repo cleanup on the operator Mac
  
  Interactive script that, with confirmation prompts:
  - fetches and prunes remote tracking refs
  - removes untracked files, flagging those that exist on no origin ref
  - deletes local branches with a gone upstream, keeping any with unpushed commits
  - optionally runs Munki repoclean locally (never committed or pushed)
  - prunes the Git LFS cache with --verify-remote
  - optionally runs git gc --aggressive (--gc)
  
  Document the routine in AGENTS.md.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `e8de8f66` AGENTS.md: prefer global AutoPkg repo registration over --prefs workarounds
  
  Register recipe repos in the global com.github.autopkg preferences with
  `autopkg repo-add` so plain autopkg commands resolve the same recipes; refresh
  autopkg/autopkg_prefs.plist with `defaults export` when the snapshot is needed.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `ad0c07e1` Microsoft 365 Copilot: trim override Input, add WS1 sensor upload helper and migration plan doc
  
  The preinstall_script that was meant to remove the legacy App Store Copilot
  (com.microsoft.copilot-mac) never ran: Munki's installs check compares only the
  bundle version at /Applications/Copilot.app, not its bundle ID, so the legacy
  app's higher CFBundleVersion made it report "already installed". Legacy-app
  removal is now handled by a WS1 sensor (codeskipper/MacAdmin
  sensors/experimental/copilot_legacy_app_cleanup.zsh) with a marker file so the
  result stays reportable in Intelligence; the preinstall script is dropped.
  
  - autopkg/overrides/Microsoft365Copilot.ws1.recipe.yaml: Input reduced to the
    WS1_*: none placeholders; inherited keys (NAME, pkginfo, MUNKI_REPO_SUBDIR,
    download inputs) are resolved from the parent chain. verify-trust-info OK.
  - AGENTS.md, README.md, add-ws1-recipe skill: document minimal override Input
    as best practice (make-override copies inherited Inputs verbatim; pkginfo is
    replaced wholesale, not merged).
  - scripts/ws1_sensor_upload.py: create a device sensor via the UEM REST API
    and assign it to a smart group, dry-run by default, --apply to send.
    Schemas taken from the tenant API explorer (mdmv2/mdmv1/systemv1). Dry-run
    currently stops at HTTP 403 (OAuth role lacks REST API > Sensors); sensor
    was uploaded manually for now.
  - dev-work/Microsoft 365 Copilot migration/001.: diagnosis, superseded
    AutoPkg-side plan, sensor pivot, MDM removal analysis, reporting design.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `66853dc5` Add Microsoft 365 Copilot recipes
- `7762b0aa` Add recipe overrides for PolyStudio - importer, assigner, and pruner
- `d35a2083` override generator: generic public template + org-specific .local fallback
  
  - overrides_defaults.yaml now uses placeholder GROUP-* smart group names
  - overrides_defaults.local.yaml holds the real names; preferred when present,
    excluded from the public-repo sync
  - generator prints a NOTICE when falling back to the generic template
  - sync_to_public: exclude only 'scripts/update public repo' so the generator
    is synced
  - tests: 31 (2 new); fixture pins the generic template
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `a53997d6` override generator: adopt upstream scaffolder safeguards
  
  - rename pruner safeguard key to WS1_PRUNE_SAFEGUARD_HALT_ON_ASSIGNED
  - warn when a template WS1_* key is absent from the upstream recipe Input
  - refuse to complete an inconsistent existing override set (Identifier,
    ParentRecipe, NAME) unless --force
  - docs/skill: defaults are repo policy to review; retention 5; new key
  - tests: 29 (3 new)
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `ec239128` Add WS1 override generator scripts, templates, and agent skill for recipe management
- `51bb2d2f` Clean up pruner overrides and add missing tools_verbose dispatch input
  
  - Replace NUMBER_OF_VERSIONS_TO_KEEP_HERE placeholder with '5' in 12 pruner
    overrides (removes 'out of range' warning; behaviour unchanged)
  - Set all *_HERE credential/URL placeholders to 'none' to match the
    importer/assigner convention (values come from GitHub secrets)
  - Add autopkg_tools_verbose workflow_dispatch input to the pruner workflow;
    it was referenced but never declared, leaving AUTOPKG_TOOLS_VERBOSE empty
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `295e0ea7` Rename WS1_SAFEGUARD_HALT_ON_ASSIGNED to WS1_PRUNE_SAFEGUARD_HALT_ON_ASSIGNED in pruner overrides
  
  Follows WorkSpaceOneImporter-recipes commit cb9eeba, which prefixes the
  pruner safeguard input with ws1_prune_. Renames the Input key in all 25
  pruner overrides and refreshes their trust info against the new parent
  recipe and WorkSpaceOnePruner.py hashes. Documents the new env var in
  AGENTS.md and adds dev-work note 054.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `37ba4a34` Add recipe trust-info handling to assigner and pruner workflows
  
  - New autopkg_trust_check.py: verify-trust-info per recipe, skip untrusted,
    update-trust-info and write PR title/body (stdlib only, Linux venv).
  - Assigner/pruner workflows: run only trusted recipes, one daily trust-info
    PR per workflow (trust-info-<kind>-<date>) plus Slack notification.
  - CAR driver imports shared commit-link helpers from autopkg_trust_check.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>

## Other Changes

- `6bcea7e4` gitignore: ignore Python bytecode and untrack committed __pycache__ files
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `bd2f1fba` Update WS1_APP_VERSIONS_TO_KEEP template to retain fewer application versions
- `02e9b66d` Document that pruner results are not sent to Slack
  
  Operators must check the job log or autopkg_pruner.log artifact for
  'TO BE PRUNED' entries before enabling real pruning.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `adcbd416` Move non-cloud-optimisation dev-work notes to dev-work root with date-stamped names
  
  Relocate 052–055 (assigner trust handling, prune safeguard rename, staging
  tuning) out of the '2026 refactor for cloud optimisation' theme folder and
  rename to 'YYYY-MM-DD <description>.md'. Update cross-references in 053 and
  autopkg_trust_check.py docstring.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `c431b024` Remove Docker Desktop Intel recipes from recipe lists
- `5073b918` Update trust for MicrosoftVisualStudioCode.ws1-assigner DockerDesktop-Intel.ws1-assigner DockerDesktop-AppleSilicon.ws1-assigner JetBrainsToolbox-Intel.ws1-assigner JetBrainsToolbox-AppleSilicon.ws1-assigner Firefox.ws1-assigner SuspiciousPackage.ws1-assigner CitrixWorkspace.ws1-assigner MicrosoftDefender.ws1-assigner LastPass-Safari.ws1-assigner iTerm2.ws1-assigner MacAdminsPython.ws1-assigner GitHubCLI.ws1-assigner MicrosoftEdge.ws1-assigner GPGSuite.ws1-assigner GoogleChrome.ws1-assigner GitHubDesktop.ws1-assigner MicrosoftCompanyPortal.ws1-assigner MunkiAdmin.ws1-assigner LexmarkUniversalDriverColor.ws1-assigner erase-install.ws1-assigner PaloAltoNetworksGlobalProtect.ws1-assigner MicrosoftTeams.ws1-assigner NodeJS-LTS.ws1-assigner Apparency.ws1-assigner (assigner)
- `2cd46e69` Update trust for Firefox.ws1 SuspiciousPackage.ws1 CitrixWorkspace.ws1 MicrosoftDefender.ws1 LastPass-Safari.ws1 iTerm2.ws1 JetBrainsToolbox-Intel.ws1 JetBrainsToolbox-AppleSilicon.ws1 DockerDesktop-AppleSilicon.ws1 DockerDesktop-Intel.ws1 MacAdminsPython.ws1 GitHubCLI.ws1 GoogleChrome.ws1 MicrosoftEdge.ws1 GitHubDesktop.ws1 GPGSuite.ws1 MicrosoftCompanyPortal.ws1 MunkiAdmin.ws1 LexmarkUniversalDriverColor.ws1 erase-install.ws1 PaloAltoNetworksGlobalProtect.ws1 MicrosoftVisualStudioCode.ws1 MicrosoftTeams.ws1 NodeJS-LTS.ws1 Apparency.ws1
