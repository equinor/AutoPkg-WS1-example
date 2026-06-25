[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

This repository is a setup for CI/CD runners to orchestrate running of Autopkg recipes that fetch Mac software
installers, make adjustments for distribution, and upload to Workspace ONE UEM (WS1).

The [WorkSpaceOneImporter processor and recipes](https://github.com/autopkg/WorkSpaceOneImporter-recipes) are used.  These build on Munki recipes to upload to WS1 and to add
WS1-specific features to schedule staging assignments, and to prune old versions of software.

This setup is adapted from, and builds on the example setup shared by Gusto Inc.

## Credits
[autopkg_tools.py](https://github.com/facebook/IT-CPE/tree/master/legacy/autopkg_tools) from Facebook under a BSD 3-clause license with modifications from [tig](https://6fx.eu).

[Autopkg CICD example by Gusto Inc](https://github.com/Gusto/it-cpe-opensource/tree/main/autopkg).


### Adaptations (latest listed first)
- Assigner and Pruner processors and recipes have been split out from the main Importer processor to support running in CAR
- optimise for running in the cloud using [cloud-autopkg-runner](https://pypi.org/project/cloud-autopkg-runner/) a.k.a. CAR library
- support WS1 specific settings, can be passed as env vars
- report WS1 specific results in a Slack channel, richer Slack messages
- support for custom TLS handling (REQUESTS_CA_BUNDLE) and optional MacSesh injection
- scheduled workflow to merge branches in Munki repo
- remote branch existence checking for Munki repo to skip pushes when branch already exists
- script to run the Autopkg workflow locally on an admin Mac to help with processor development and with recipe testing
- new CLI option for skipping trust-info pull-request generation (-n / --no-trust-info-pull-request)
- verbose level setting in autopkg_tools.py and in autopkg_tools_launcher.zsh


### How it works
We've supplied example overrides for Firefox and for Palo Alto GlobalProtect.

* `autopkg_ws1-car.yml` - Checks out the latest version of your autopkg overrides, installs Munki and autopkg, then clones all the upstream recipe repos. We forked Facebook's `autopkg_tools.py` script, which iterates over a list of recipes, and successful builds are pushed into a separate Git LFS repo. The build results are posted to a Slack channel so we can fix any recipe trust issues with a pull request. This also runs hjuutilainen's VirusTotalAnalyzer post-processor.
* `autopkg_ws1-assigner.yml` - performs automated staging by assigning the latest version of each package to a smart group in WS1 UEM. This is done by running the Assigner processor on the latest imported packages.  The settings in the recipe can be overridden to assign to different smart groups, or to assign to multiple smart groups.  And las but not least, you can set the number of days to wait before assigning a package to a smart group, so you can stage the latest version for testing in different groups before rolling it out to all Mac devices.
* `autopkg_ws1-pruner.yml` - performs automated pruning of old versions of packages in WS1 UEM.  This is done by running the Pruner processor on the software title.  The settings in the recipe override can be changed to keep a specified number of versions.
* `munki_repoclean.yml` - Pares your Munki repo down to the five newest versions of each package.
* `munki_repo_branch_merge.yml` merge git branches older than 1 month automatically and delete both local and remote


## Github Actions specifics
GitHub releases can be changed after publishing, which can make your build environment change without any indication. If an action’s repo get compromised a tag could point to malicious code. We pin the SHA1 commit hash for actions instead, since Git and GitHub have robust protections against SHA1 collisions.


### Setting up your local machine
Because of how AutoPkg handles relative paths, the directory paths on your machine must match the ones on the AutoPkg server for recipes to run properly.
You can see an example of the paths and preferences we use in `autopkg_ws1-car.yml`.
Following the runner setup steps will help prepare your local Mac.



### Using this repo
1. Create an empty GitHub repo with Actions enabled
1. Copy `workflows/` to `.github/` in this repo
1. Create an override for the ws1 importer: `autopkg make-override  --format=yaml recipename.ws1.recipe.yaml`. Be sure to place overrides in `autopkg/overrides/`
1. Add the recipe filename to `recipe_list.json`
1. Add the repo's needed to `repo_list.txt`
2. Create an override for the ws1 assigner: `autopkg make-override  --format=yaml recipename.ws1-assigner.recipe.yaml`
2. Add the recipe to `recipe_list_assigner.txt`
3. Create an override for the ws1 pruner: `autopkg make-override  --format=yaml recipename.ws1-pruner.recipe.yaml`
4. Add the recipe to `recipe_list_pruner.txt`
1. Create another empty GitHub repo with Actions enabled. This will be your munki repo.
1. In your Munki repo, create a [GitHub deploy key](https://docs.github.com/en/developers/overview/managing-deploy-keys#setup-2) with read/write access to repo.
1. Copy the name of your Munki git repo to the `Checkout your munki LFS repo` step in `autopkg.yml`
1. Add the private key for your Munki repo deploy key as a GitHub Actions secret named `CPE_MUNKI_LFS_DEPLOY_KEY` in your AutoPkg repo.
1. Add the sensitive [WS1 specific input variables](https://github.com/autopkg/WorkSpaceOneImporter-recipes#available-input-variables) you need as GitHub Actions secrets
1. Optionally, if using Slack, add Github Actions secrets for `SLACK_WEBHOOK_URL` (a URL to post AutoPkg results in Slack) and `SLACK_TOKEN`.


### YAML support
With YAML becoming more popular for AutoPkg recipes and overrides, this runner includes YAML support. Simply ensure the recipe list uses the full file name including a .yaml extension.
```
[
  "Firefox.ws1.recipe.yaml",
  "SuspiciousPackage.ws1.recipe.yaml"
]
```


### a note about security
We've used the MacSesh library for the first couple of years for convenience to provide our corporate internal CA-certificates to Autopkg Python. These certificates are needed when running on self-hosted GitHub runners because our internet firewall performs packet inspection.
When reviewing the dependencies, we found the need to keep urllib3 < 2 due to issues with supporting features being deprecated.  Details are [here](https://github.com/sheagcraig/MacSesh/issues/7) and [here](https://github.com/sheagcraig/MacSesh/issues/9).

Note that GitHub Advanced Security (GHAS) will generate both a couple of high severity alerts and a medium one for vulnerabilities in this urllib3 version, which is why we've now moved to using a REQUESTS_CA_BUNDLE in production instead of MacSesh, this allows us to use the latest urllib3.
These are the vulnerabilities reported in urllib3 < 2.6.0:
- [urllib3 streaming API improperly handles highly compressed data](https://github.com/advisories/GHSA-2xpw-w6gg-jr37)
- [urllib3 allows an unbounded number of links in the decompression chain](https://github.com/advisories/GHSA-gm62-xv2j-4w53)
- [urllib3 redirects are not disabled when retries are disabled on PoolManager instantiation](https://github.com/advisories/GHSA-pq67-6m6q-mj2v)

The new dependencies are kept in `requirements_all.txt` which is used by `autopkg.yml` workflow, and the MacSesh ones are in `requirements_macsesh.txt`.
The `requirements_all.txt` name was used to avoid issues with a GHAS runner, details are in the `requirements.*.txt` files.
the dependency of MacSesh on the outdated urllib3 version < 2.6.0 is addressed by moving to REQUESTS_CA_BUNDLE instead.
The old dependencies, listed in requirements_macsesh.txt,  are kept around only as reference, not used in production anymore.


#### Recent changes
The scripts and workflows marked `classic` do NOT feature caching of Autopkg (meta)data across GitHub runners.
This means when starting a AutoPkg session on a runner on another Mac, all (or most) recipes will start from  scratch,
and the downloads recipes will fetch everything again.

Since this would be highly inefficient in the cloud with no caching support and with ephemeral runners that start from
scratch each run, we consider this setup really only suitable for on-prem use.

In concert with the processors and recipes being refactored to support running in CAR, we have adapted the workflows and scripts in this repo to support caching of Autopkg (meta)data across GitHub runners, and to support running in the cloud.

The `cloud_cli` versions use the `cloud-autopkg-runner` CLI for better caching and cloud compatibility, while the `classic` versions will continue to use the existing GitHub Actions workflows without caching.

The Assigner and Pruner processors have been split out from the main Importer processor to support running in CAR, and to separate concerns. The scripts and workflows have been separated out into `classic` and `cloud_cli` versions to support both the existing on-prem runner setup and the new CAR-based cloud setup.

The `autopkg_tools_launcher_cloud_cli.zsh` script supports running the new recipe lists for assigner and pruner workflows.

We're now running the WS1 Assigner and Pruner processors independently in separate workflows on Linux runners to optimise further for cost.


#### `car` mode — CAR as a library, parallel importer with git worktrees
The `car` mode (`autopkg_tools_ws1_car.py`, launcher `autopkg_tools_launcher_car.zsh`, workflow `.github/workflows/autopkg_ws1_car.yml`) drives [`cloud-autopkg-runner`](https://pypi.org/project/cloud-autopkg-runner/) as a **library** (not via its CLI).

Key differences vs `cloud_cli` mode:
- Recipes run **in parallel** via CAR's async API (`asyncio.Queue` + N workers, bounded by `AUTOPKG_TOOLS_MAX_CONCURRENCY`, default 4).
- **Importer-only.** Result parsing reads only `ws1_importer_summary_result` / `munki_importer_summary_result` — Assigner and Pruner are handled by their own dedicated workflows.
- Each new `<AppName>_<Version>` is committed to the Munki repo via a **dedicated git worktree** (`cloud_autopkg_runner.GitClient.add_worktree`), so concurrent commits never clash. The `AUTOPKG_MUNKI_REPO` itself is fixed for the run and the actual `recipe.run()` call is serialised with an `asyncio.Lock` (see "Known Limitations" in prompt `040`).
- **Per-recipe log files** at `autopkg/logs/<recipe>.<timestamp>.log` plus a script-wide log at `autopkg/logs/autopkg_tools_car.<timestamp>.log`. The workflow uploads `autopkg/logs/` and `autopkg/reports/` as an artefact on every run (`if: always()`), so logs are available even on failure.
- `metadata_cache.json` is round-tripped via CAR's `get_cache_plugin()` context manager (no manual handling).


## Findings and future plans
- Using `uv run autopkg` on a developer Mac has been seen to greatly improve speed already, especially when verifying trust info.
- Installing AutoPkg as a `uv` managed tool may also help with caching support.
- managing the custom packages needed for WS1 may be easier using `uv`

