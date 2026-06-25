#!/usr/bin/env python3
"""AutoPkg tools driver that uses cloud-autopkg-runner (CAR) as a library.

Importer-only variant: runs `WorkSpaceOneImporter` recipe overrides in parallel via
CAR's async API, then commits each new `<AppName>_<Version>` package to a
dedicated git worktree of the separate Munki LFS repo and pushes that branch.

Sibling to ``autopkg_tools_ws1_cloud_cli.py`` (kept untouched as a fallback).

See dev-work/2026 refactor for cloud optimisation/040. REFACTOR_PROMPT ... .md
"""

# BSD-3-Clause
# Copyright (c) Facebook, Inc. and its affiliates.
# Copyright (c) tig <https://6fx.eu/>.
# Copyright (c) Gusto, Inc.
# Copyright (c) Equinor ASA
# Copyright (c) Datamind AS

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from cloud_autopkg_runner import (
    AutoPkgPrefs,
    GitClient,
    Recipe,
    RecipeFinder,
    Settings,
    get_cache_plugin,
    logging_config,
)
from cloud_autopkg_runner.logging_context import recipe_context

# ---------------------------------------------------------------------------
# Constants / environment
# ---------------------------------------------------------------------------

AUTOPKG_TOOLS_TYPE = "Workspace ONE - cloud-autopkg-runner library"
AUTOPKG_TOOLS_VERBOSE = int(os.environ.get("AUTOPKG_TOOLS_VERBOSE", "2"))
SLACK_WEBHOOK = os.environ.get("AUTOPKG_SLACK_WEBHOOK_TOKEN") or None
GITHUB_WORKSPACE = Path(
    os.environ.get("GITHUB_WORKSPACE", os.getcwd())
).resolve()  # fallback to cwd for local runs
MUNKI_REPO = GITHUB_WORKSPACE / "munki_repo"
WORKTREES_DIR = GITHUB_WORKSPACE / "munki_repo_worktrees"
LOGS_DIR = Path("autopkg/logs")
REPORTS_DIR = Path("autopkg/reports")
PREFS_FILE = Path("autopkg/autopkg_prefs.plist")
RECIPE_TO_RUN = (os.environ.get("RECIPE_TO_RUN") or "").strip() or None
MAX_CONCURRENCY = max(1, int(os.environ.get("AUTOPKG_TOOLS_MAX_CONCURRENCY", "4")))
RECIPE_TIMEOUT = int(os.environ.get("AUTOPKG_TOOLS_RECIPE_TIMEOUT", "1200"))
LOG_FORMAT = os.environ.get("AUTOPKG_TOOLS_LOG_FORMAT", "text")

# Munki repo subdirectories the importer can write to.
SNAPSHOT_DIRS = ("pkgs", "pkgsinfo", "icons", "catalogs")

STOP_WORKER: Any = object()

# Attach to the cloud_autopkg_runner logger tree so our messages share the
# handlers initialised by ``logging_config.initialize_logger`` (recipe context
# filter, file handler, colour console, etc.).
logger = logging.getLogger("cloud_autopkg_runner.tools_car")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_cache_paths(cache_file: Path) -> None:
    """Convert any absolute file_path values in the metadata cache to relative paths.

    The CAR library writes absolute paths which break portability between local
    runs and GitHub Actions.  This rewrites them relative to GITHUB_WORKSPACE.
    """
    if not cache_file.exists():
        return

    data = json.loads(cache_file.read_text(encoding="utf-8"))
    changed = False

    for _recipe_name, entry in data.items():
        for item in entry.get("metadata", []):
            fp = item.get("file_path", "")
            if not fp:
                continue
            p = Path(fp)
            if p.is_absolute():
                try:
                    rel = p.relative_to(GITHUB_WORKSPACE)
                    item["file_path"] = str(rel)
                    changed = True
                except ValueError:
                    logger.warning(
                        f"metadata_cache: absolute path not under GITHUB_WORKSPACE, "
                        f"leaving unchanged: {fp}"
                    )

    if changed:
        cache_file.write_text(
            json.dumps(data, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(f"Normalised absolute paths in {cache_file.name} to relative")


def _verbose_to_settings_level(v: int) -> int:
    """Map AUTOPKG_TOOLS_VERBOSE to CAR Settings.verbosity_level.

    Direct pass-through: 0=ERROR, 1=WARNING, 2=INFO, 3=DEBUG.
    """
    return min(v, 3)


def _short_name(recipe_name: str) -> str:
    """Strip recipe file extensions for use as a log/branch token."""
    return recipe_name.removesuffix(".yaml").removesuffix(".recipe")


def _branch_name(app_name: str, version: str) -> str:
    """Sanitise an ``<AppName>_<Version>`` branch name.

    Must match the convention used by ``autopkg_tools_ws1_cloud_cli.py`` so
    that ``munki_repo_branch_merger.sh`` keeps working.
    """
    raw = f"{app_name}_{version}".strip()
    return raw.replace(" ", "").replace("(", "-").replace(")", "-")


# ---------------------------------------------------------------------------
# Disk-space diagnostics
# ---------------------------------------------------------------------------


def _get_free_bytes() -> int | None:
    """Return free bytes on / or None on failure."""
    try:
        return shutil.disk_usage("/").free
    except OSError:
        return None


def _log_disk_usage(label: str, *, prev_free: int | None = None) -> None:
    """Log current free disk space and sizes of key directories.

    Writes a structured summary at INFO level tagged with ``label`` so the
    progression through the workflow can be correlated with storage consumption.
    Only active when AUTOPKG_TOOLS_VERBOSE >= 4.

    If ``prev_free`` is provided, also logs the delta (space consumed since then).
    """
    if AUTOPKG_TOOLS_VERBOSE < 4:
        return
    try:
        usage = shutil.disk_usage("/")
        free_gb = usage.free / (1024**3)
        used_gb = usage.used / (1024**3)
        total_gb = usage.total / (1024**3)
    except OSError:
        logger.warning("[disk:%s] Could not read disk_usage('/')", label)
        return

    lines = [
        f"[disk:{label}] Free: {free_gb:.2f} GB | Used: {used_gb:.2f} GB | Total: {total_gb:.2f} GB"
    ]

    if prev_free is not None:
        delta_mb = (prev_free - usage.free) / (1024**2)
        lines.append(f"  Delta since last checkpoint: {delta_mb:+.1f} MB consumed")

    # Key directories to track — sizes computed via du for accuracy with LFS
    dirs_to_check = [
        ("munki_repo", MUNKI_REPO),
        ("munki_repo/.git", MUNKI_REPO / ".git"),
        ("munki_repo_worktrees", WORKTREES_DIR),
        ("autopkg/cache", GITHUB_WORKSPACE / "autopkg" / "cache"),
        ("autopkg/repos", GITHUB_WORKSPACE / "autopkg" / "repos"),
    ]

    for tag, dirpath in dirs_to_check:
        if not dirpath.exists():
            continue
        try:
            result = subprocess.run(
                ["du", "-sh", str(dirpath)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            size_str = result.stdout.split("\t")[0].strip() if result.stdout else "?"
        except Exception:  # noqa: BLE001
            size_str = "ERR"
        lines.append(f"  {tag}: {size_str}")

    logger.info("\n".join(lines))


def _log_disk_if_low(
    label: str, threshold_gb: float = 5.0, *, prev_free: int | None = None
) -> None:
    """Log full disk diagnostics if free space drops below threshold.

    Only active when AUTOPKG_TOOLS_VERBOSE >= 2.
    """
    if AUTOPKG_TOOLS_VERBOSE < 2:
        return
    try:
        usage = shutil.disk_usage("/")
        free_gb = usage.free / (1024**3)
    except OSError:
        return
    if free_gb < threshold_gb:
        logger.warning(
            "[disk:%s] FREE SPACE LOW: %.2f GB remaining (threshold: %.1f GB)",
            label,
            free_gb,
            threshold_gb,
        )
        _log_disk_usage(f"{label}_LOW_SPACE", prev_free=prev_free)


# ---------------------------------------------------------------------------
# Munki-repo snapshot / diff
# ---------------------------------------------------------------------------


def _snapshot_munki_repo() -> dict[str, tuple[int, float]]:
    """Take a (size, mtime) snapshot of files under SNAPSHOT_DIRS in MUNKI_REPO."""
    snap: dict[str, tuple[int, float]] = {}
    for sub in SNAPSHOT_DIRS:
        base = MUNKI_REPO / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(MUNKI_REPO)
            snap[str(rel)] = (st.st_size, st.st_mtime)
    return snap


def _diff_munki_repo(pre: dict[str, tuple[int, float]]) -> list[Path]:
    """Return relative paths that are new or changed vs ``pre`` snapshot."""
    after = _snapshot_munki_repo()
    diff: list[Path] = []
    for key, sig in after.items():
        if pre.get(key) != sig:
            diff.append(Path(key))
    return diff


# ---------------------------------------------------------------------------
# Per-recipe logging
# ---------------------------------------------------------------------------


class _RecipeOnlyFilter(logging.Filter):
    """Only let records through whose recipe-context tag matches ``recipe_short``."""

    def __init__(self, recipe_short: str) -> None:
        super().__init__()
        self.recipe_short = recipe_short

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        return getattr(record, "recipe", None) == self.recipe_short


def _attach_recipe_log(recipe_name: str, timestamp: str) -> logging.FileHandler:
    """Attach a per-recipe FileHandler to the CAR logger, filtered by recipe ctx."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    short = _short_name(recipe_name)
    path = LOGS_DIR / f"{short}.{timestamp}.log"
    handler = logging.FileHandler(path, mode="w")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(recipe)-30s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    handler.addFilter(_RecipeOnlyFilter(short))
    logging.getLogger("cloud_autopkg_runner").addHandler(handler)
    return handler


def _detach_recipe_log(handler: logging.FileHandler) -> None:
    """Detach a per-recipe FileHandler previously attached above."""
    logging.getLogger("cloud_autopkg_runner").removeHandler(handler)
    try:
        handler.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Slack notifications (importer-only payload)
# ---------------------------------------------------------------------------


def _slack(title: str, color: str, text: str) -> None:
    """Post a Slack message; suppressed at AUTOPKG_TOOLS_VERBOSE>=3 or no webhook."""
    if AUTOPKG_TOOLS_VERBOSE >= 3:
        logger.debug("Skipping Slack (verbose>=3): %s", title)
        return
    if not SLACK_WEBHOOK:
        logger.debug("Skipping Slack (no webhook): %s", title)
        return
    payload = {
        "attachments": [
            {
                "username": "Autopkg",
                "as_user": True,
                "title": title,
                "color": color,
                "text": text,
                "mrkdwn_in": ["text"],
            }
        ]
    }
    try:
        resp = requests.post(
            SLACK_WEBHOOK,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Slack returned %s: %s", resp.status_code, resp.text)
    except Exception:  # noqa: BLE001
        logger.exception("Slack POST failed")


def _slack_for_recipe(
    recipe_name: str,
    importer_row: dict[str, Any] | None,
    munki_row: dict[str, Any] | None,
    failure: dict[str, Any] | None,
    trust_failed_msg: str | None,
    suppress_non_trust_failures: bool = False,
) -> None:
    """Build and send the per-recipe Slack message (importer-only schema).

    When ``suppress_non_trust_failures`` is True, only trust verification failures
    are sent; other notifications (success, failure, etc.) are suppressed. This is
    used when the WorkSpaceOneSlacker post-processor handles those notifications.
    """
    short = _short_name(recipe_name)

    if trust_failed_msg:
        _slack(
            f"{short} failed trust verification (CICD-Slack)",
            "warning",
            trust_failed_msg,
        )
        return

    # Suppress non-trust-failure notifications if post-processor is handling them
    if suppress_non_trust_failures:
        return

    if failure:
        msg = (
            f"*Error:* {failure.get('message', '')}\n"
            f"*Traceback:* `{(failure.get('traceback', '') or '')[:1500]}`"
        )
        if "No releases found for repo" in msg:
            return
        _slack(f"Failed to import {short} (CICD-Slack)", "danger", msg)
        return

    if importer_row:
        version = importer_row.get("version", "")
        title = f"WS1 UEM imported {short} {version} (CICD-Slack)"
        text = f"App: `{short}`\nVersion: `{version}`\n"
        if importer_row.get("console_location"):
            text += f"<{importer_row['console_location']}|*console location*>\n"
        if munki_row:
            text += (
                "*Munki*\n"
                f"*Catalogs:* {munki_row.get('catalogs', '')}\n"
                f"*Package Path:* `{munki_row.get('pkg_repo_path', '')}`\n"
                f"*Pkginfo Path:* `{munki_row.get('pkginfo_path', '')}`\n"
            )
        _slack(title, "good", text)
        return

    if munki_row:
        version = munki_row.get("version", "")
        title = f"Munki (NOT WS1 UEM!) imported {short} {version} (CICD-Slack)"
        text = (
            f"*Catalogs:* {munki_row.get('catalogs', '')}\n"
            f"*Package Path:* `{munki_row.get('pkg_repo_path', '')}`\n"
            f"*Pkginfo Path:* `{munki_row.get('pkginfo_path', '')}`\n"
        )
        _slack(title, "good", text)


# ---------------------------------------------------------------------------
# Remote branch existence (small async git wrapper to keep GitClient pure)
# ---------------------------------------------------------------------------


async def _remote_branch_exists(branch: str) -> bool:
    """Return True if ``origin/<branch>`` already exists on the Munki remote."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        "--heads",
        "origin",
        branch,
        cwd=str(MUNKI_REPO),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return bool(stdout.strip())


# ---------------------------------------------------------------------------
# Recipe processing
# ---------------------------------------------------------------------------


async def _process_recipe(
    name: str,
    *,
    prefs: AutoPkgPrefs,
    finder: RecipeFinder,
    munki_git: GitClient,
    munki_lock: asyncio.Lock,
    default_branch: str,
    opts: argparse.Namespace,
    trust_failures: list[tuple[str, str]],
    suppress_non_trust_failures: bool = False,
) -> None:
    """Trust-check, run, snapshot/diff, then commit + push for a single recipe.

    When ``suppress_non_trust_failures`` is True, only trust verification failures
    are reported via Slack; other recipe outcomes are suppressed (handled by
    WorkSpaceOneSlacker post-processor).
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = recipe_context.set(_short_name(name))
    handler = _attach_recipe_log(name, timestamp)
    try:
        logger.info("Starting recipe %s", name)
        _recipe_free_start = _get_free_bytes()
        _log_disk_if_low(f"pre_{_short_name(name)}")

        try:
            path = await finder.find_recipe(name)
        except Exception:  # noqa: BLE001
            logger.exception("Recipe %s not found", name)
            return

        try:
            recipe = Recipe(path, REPORTS_DIR, prefs)
        except Exception:  # noqa: BLE001
            logger.exception("Could not construct Recipe object for %s", name)
            return

        # --- Trust info ---------------------------------------------------
        if not opts.disable_verification:
            trusted = await recipe.verify_trust_info()
            if not trusted:
                trust_out = await recipe.get_trust_output()
                logger.warning("Trust verification FAILED for %s", name)
                if not opts.no_trust_pr:
                    try:
                        await recipe.update_trust_info()
                    except Exception:  # noqa: BLE001
                        logger.exception("update_trust_info failed for %s", name)
                trust_failures.append((name, trust_out or ""))
                _slack_for_recipe(
                    name,
                    None,
                    None,
                    None,
                    (trust_out or "")[:1500],
                    suppress_non_trust_failures,
                )
                return

        # --- Run (serialised against the shared MUNKI_REPO) ---------------
        async with munki_lock:
            pre = _snapshot_munki_repo()
            try:
                await asyncio.wait_for(recipe.run(), timeout=RECIPE_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                logger.error("Recipe %s timed out after %ds", name, RECIPE_TIMEOUT)
                _slack_for_recipe(
                    name,
                    None,
                    None,
                    {"message": f"timed out after {RECIPE_TIMEOUT}s", "traceback": ""},
                    None,
                    suppress_non_trust_failures,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Recipe %s raised during run()", name)
                _slack_for_recipe(
                    name,
                    None,
                    None,
                    {"message": str(exc), "traceback": ""},
                    None,
                    suppress_non_trust_failures,
                )
                return
            new_files = _diff_munki_repo(pre)

        _log_disk_if_low(f"post_run_{_short_name(name)}", prev_free=_recipe_free_start)

        # --- Parse report (importer-only) --------------------------------
        try:
            recipe._result.refresh_contents()  # noqa: SLF001
            failed_items = recipe._result.failures  # noqa: SLF001
            summary = recipe._result.summary_results  # noqa: SLF001
        except Exception:  # noqa: BLE001
            logger.exception("Could not parse report for %s", name)
            failed_items = []
            summary = {}

        if failed_items:
            _slack_for_recipe(
                name,
                None,
                None,
                failed_items[0],
                None,
                suppress_non_trust_failures,
            )
            return

        importer_rows = (summary.get("ws1_importer_summary_result") or {}).get(
            "data_rows", []
        )
        munki_rows = (summary.get("munki_importer_summary_result") or {}).get(
            "data_rows", []
        )
        importer_row = importer_rows[0] if importer_rows else None
        munki_row = munki_rows[0] if munki_rows else None

        if not (importer_row or munki_row):
            logger.info("Nothing new imported for %s", name)
            return

        primary = importer_row or munki_row
        app_name = primary.get("name") or recipe.input.get("NAME") or _short_name(name)
        version = str(primary.get("version") or "").strip()
        if not version:
            logger.info("No version reported for %s, skipping git push", name)
            _slack_for_recipe(
                name,
                importer_row,
                munki_row,
                None,
                None,
                suppress_non_trust_failures,
            )
            return

        branch = _branch_name(app_name, version)

        # --- Remote branch existence check -------------------------------
        async with munki_lock:
            try:
                await munki_git.fetch("origin", prune=True)
            except Exception:  # noqa: BLE001
                logger.exception("git fetch failed (continuing)")
        if await _remote_branch_exists(branch):
            logger.info("Remote branch %s already exists, skipping push", branch)
            _slack_for_recipe(
                name,
                importer_row,
                munki_row,
                None,
                None,
                suppress_non_trust_failures,
            )
            return

        if not new_files:
            logger.warning(
                "Importer reported new pkg %s/%s but no files diffed under "
                "%s -- skipping git push",
                app_name,
                version,
                SNAPSHOT_DIRS,
            )
            _slack_for_recipe(
                name,
                importer_row,
                munki_row,
                None,
                None,
                suppress_non_trust_failures,
            )
            return

        # --- Worktree-based commit + push --------------------------------
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        wt_path = WORKTREES_DIR / f"{branch}_{timestamp}"

        _wt_free_before = _get_free_bytes()
        _log_disk_usage(
            f"pre_worktree_{_short_name(name)}", prev_free=_recipe_free_start
        )
        async with munki_lock:
            await munki_git.add_worktree(
                wt_path,
                f"origin/{default_branch}",
                checkout_options=["-b", branch],
                force=True,
            )
        _log_disk_if_low(
            f"post_worktree_{_short_name(name)}", prev_free=_wt_free_before
        )

        try:
            # Copy new/changed files into the isolated worktree at matching paths.
            for rel in new_files:
                src = MUNKI_REPO / rel
                if not src.is_file():
                    continue
                dst = wt_path / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

            wt_git = GitClient(wt_path)
            await wt_git.add(".")
            await wt_git.commit(f"Updated {app_name} to {version}")
            await wt_git.push("origin", branch, set_upstream=True)
            logger.info("Pushed branch %s", branch)
            _slack_for_recipe(
                name,
                importer_row,
                munki_row,
                None,
                None,
                suppress_non_trust_failures,
            )
        finally:
            async with munki_lock:
                try:
                    await munki_git.remove_worktree(wt_path, force=True)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to remove worktree %s", wt_path)
                try:
                    await munki_git.prune_worktrees()
                except Exception:  # noqa: BLE001
                    logger.debug("prune_worktrees failed", exc_info=True)
    finally:
        _detach_recipe_log(handler)
        recipe_context.reset(token)


# ---------------------------------------------------------------------------
# Worker / queue
# ---------------------------------------------------------------------------


async def _worker(
    queue: asyncio.Queue,
    *,
    prefs: AutoPkgPrefs,
    finder: RecipeFinder,
    munki_git: GitClient,
    munki_lock: asyncio.Lock,
    default_branch: str,
    opts: argparse.Namespace,
    trust_failures: list[tuple[str, str]],
    suppress_non_trust_failures: bool = False,
) -> None:
    """Pull recipes off the queue until the STOP sentinel is reached."""
    while True:
        item = await queue.get()
        try:
            if item is STOP_WORKER:
                return
            try:
                await _process_recipe(
                    item,
                    prefs=prefs,
                    finder=finder,
                    munki_git=munki_git,
                    munki_lock=munki_lock,
                    default_branch=default_branch,
                    opts=opts,
                    trust_failures=trust_failures,
                    suppress_non_trust_failures=suppress_non_trust_failures,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Worker failed on recipe %s", item)
        finally:
            queue.task_done()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_recipe_list(opts: argparse.Namespace) -> list[str]:
    """Resolve recipes to run from ``RECIPE_TO_RUN`` or ``--list`` file."""
    if RECIPE_TO_RUN:
        return [r.strip() for r in RECIPE_TO_RUN.split(",") if r.strip()]
    if not opts.list:
        logger.error("Neither --list nor RECIPE_TO_RUN provided")
        sys.exit(1)
    p = Path(opts.list)
    if p.suffix == ".json":
        data = json.loads(p.read_text())
    elif p.suffix == ".plist":
        data = plistlib.loads(p.read_bytes())
    else:
        logger.error("Unsupported recipe list extension: %s", p.suffix)
        sys.exit(1)
    if not isinstance(data, list):
        logger.error("Recipe list file must contain a top-level list")
        sys.exit(1)
    return [str(x) for x in data]


async def _async_main(opts: argparse.Namespace, recipes: list[str]) -> None:
    """Configure CAR Settings, spin up workers, run all recipes."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    settings.autopkg_pref_file = PREFS_FILE
    settings.report_dir = REPORTS_DIR
    settings.cache_plugin = "json"
    settings.cache_file = "metadata_cache.json"
    settings.max_concurrency = MAX_CONCURRENCY
    settings.recipe_timeout = RECIPE_TIMEOUT
    settings.verbosity_level = _verbose_to_settings_level(AUTOPKG_TOOLS_VERBOSE)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    settings.log_file = LOGS_DIR / f"autopkg_tools_car.{timestamp}.log"
    settings.log_format = LOG_FORMAT
    settings.post_processors = [
        "com.github.codeskipper.OMNISSA-WorkSpaceOneSlacker/WorkSpaceOneSlacker",
    ]

    logging_config.initialize_logger(
        settings.verbosity_level, settings.log_file, settings.log_format
    )

    logger.info("Starting Autopkg tools session, type: [%s]", AUTOPKG_TOOLS_TYPE)
    logger.info(
        "max_concurrency=%d  recipe_timeout=%ds  recipes=%d",
        MAX_CONCURRENCY,
        RECIPE_TIMEOUT,
        len(recipes),
    )
    _session_free_start = _get_free_bytes()
    _log_disk_usage("session_start")

    if "REQUESTS_CA_BUNDLE" in os.environ:
        logger.info("Using REQUESTS_CA_BUNDLE=%s", os.environ["REQUESTS_CA_BUNDLE"])

    prefs = AutoPkgPrefs(settings.autopkg_pref_file)
    finder = RecipeFinder(prefs)

    if not MUNKI_REPO.is_dir():
        logger.error("Munki repo not found at %s", MUNKI_REPO)
        sys.exit(1)
    munki_git = GitClient(MUNKI_REPO)
    munki_lock = asyncio.Lock()

    try:
        default_branch = await munki_git.get_default_branch("origin")
    except Exception:  # noqa: BLE001
        logger.warning("Could not determine default branch from origin, using 'main'")
        default_branch = "main"
    logger.info("Munki repo default branch: %s", default_branch)

    trust_failures: list[tuple[str, str]] = []

    # Detect if WorkSpaceOneSlacker post-processor is configured; if so,
    # suppress non-trust-failure Slack notifications from this script since
    # the post-processor will handle them.
    suppress_non_trust_failures = (
        "com.github.codeskipper.OMNISSA-WorkSpaceOneSlacker/WorkSpaceOneSlacker"
        in settings.post_processors
    )
    if suppress_non_trust_failures:
        logger.info(
            "WorkSpaceOneSlacker post-processor detected; suppressing non-trust-failure "
            "Slack notifications (handled by post-processor)"
        )

    queue: asyncio.Queue = asyncio.Queue()
    for r in recipes:
        queue.put_nowait(r)
    num_workers = min(MAX_CONCURRENCY, max(1, len(recipes)))
    for _ in range(num_workers):
        queue.put_nowait(STOP_WORKER)

    # Pre-run: normalise any absolute paths left from a previous environment.
    _normalise_cache_paths(GITHUB_WORKSPACE / settings.cache_file)

    async with get_cache_plugin():
        workers = [
            asyncio.create_task(
                _worker(
                    queue,
                    prefs=prefs,
                    finder=finder,
                    munki_git=munki_git,
                    munki_lock=munki_lock,
                    default_branch=default_branch,
                    opts=opts,
                    trust_failures=trust_failures,
                    suppress_non_trust_failures=suppress_non_trust_failures,
                )
            )
            for _ in range(num_workers)
        ]
        await queue.join()
        await asyncio.gather(*workers, return_exceptions=True)

    # Post-run: normalise paths the CAR library may have written as absolute.
    _normalise_cache_paths(GITHUB_WORKSPACE / settings.cache_file)

    if trust_failures and not opts.disable_verification and not opts.no_trust_pr:
        names = " ".join(_short_name(n) for n, _ in trust_failures)
        with open("pull_request_title", "a+") as title_file:
            title_file.write(f"Update trust for {names}")
        with open("pull_request_body", "a+") as body_file:
            for n, msg in trust_failures:
                body_file.write(f"--- {n} ---\n{msg}\n")

    _log_disk_usage("session_end", prev_free=_session_free_start)
    logger.info("Autopkg tools session finished")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Drive cloud-autopkg-runner as a library to run WorkSpaceOneImporter "
            "recipes in parallel, committing each new package to a git worktree "
            "branch of the Munki repo."
        )
    )
    parser.add_argument(
        "-l", "--list", help="Path to a JSON or plist list of recipe names."
    )
    parser.add_argument(
        "-v",
        "--disable_verification",
        action="store_true",
        help="Disable recipe trust-info verification.",
    )
    parser.add_argument(
        "-n",
        "--no-trust-info-pull-request",
        dest="no_trust_pr",
        action="store_true",
        default=False,
        help="Do NOT generate a Pull Request to update trust info on failure.",
    )
    opts = parser.parse_args()

    recipes = _parse_recipe_list(opts)
    start = time.time()
    try:
        asyncio.run(_async_main(opts, recipes))
    finally:
        logger.info("Session duration: %.1f seconds", time.time() - start)


if __name__ == "__main__":
    main()
