#!/usr/bin/env python3
"""Pre-run AutoPkg recipe trust verification for the Linux assigner/pruner workflows.

For each recipe: run ``autopkg verify-trust-info``. Trusted recipes are written to a
filtered recipe list so only those are run. Untrusted recipes get their trust info
updated in the override (``update-trust-info``) and the verbose diff, including links
to the upstream commits, is written to ``pull_request_title`` / ``pull_request_body``
so the workflow can open a single daily trust-info PR (same format as the CAR driver).

Stdlib only, so it runs with the Python of a source-installed AutoPkg venv.
The commit-link helpers are also imported by ``autopkg_tools_ws1_car.py``.

See dev-work/2026-11-05 add-trust-info-handling-to-assigner-workflow.md
"""

# BSD-3-Clause
# Copyright (c) Equinor ASA

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def get_remote_url(repo_path: Path) -> str | None:
    """Read the origin remote URL from a local git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        url = result.stdout.strip()
        # Normalise SSH URLs to HTTPS for clickable links
        if url.startswith("git@github.com:"):
            url = "https://github.com/" + url.removeprefix("git@github.com:")
        return url.removesuffix(".git")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def extract_commit_links(trust_output: str, recipe_repo_dir: Path) -> list[str]:
    """Parse trust-info -vvv output for commit hashes and build GitHub links."""
    links: list[str] = []
    current_repo_path: Path | None = None

    for line in trust_output.splitlines():
        path_match = re.match(r"\s+Path:\s+(.+)", line)
        if path_match:
            full_path = Path(path_match.group(1)).resolve()
            try:
                rel = full_path.relative_to(recipe_repo_dir.resolve())
                repo_dir_name = rel.parts[0]
                current_repo_path = recipe_repo_dir / repo_dir_name
            except (ValueError, IndexError):
                current_repo_path = None

        commit_match = re.match(r"\s*commit ([0-9a-f]{40})", line)
        if commit_match and current_repo_path:
            sha = commit_match.group(1)
            remote_url = get_remote_url(current_repo_path)
            if remote_url:
                links.append(f"{remote_url}/commit/{sha}")

    return links


def _short_name(recipe_name: str) -> str:
    return recipe_name.removesuffix(".yaml").removesuffix(".recipe")


def _read_recipe_list(path: Path) -> list[str]:
    recipes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            recipes.append(line)
    return recipes


def _autopkg(autopkg_cmd: list[str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*autopkg_cmd, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--recipe-list", type=Path, help="Text file, one recipe per line."
    )
    source.add_argument("--recipe", help="Single recipe to check.")
    parser.add_argument(
        "--autopkg-cmd",
        required=True,
        help="AutoPkg command, e.g. '../autopkg/.venv/bin/python3 ../autopkg/Code/autopkg'.",
    )
    parser.add_argument("--prefs", required=True, help="AutoPkg prefs file.")
    parser.add_argument(
        "--recipe-repo-dir",
        type=Path,
        default=Path("autopkg/repos"),
        help="AutoPkg RECIPE_REPO_DIR, used to build upstream commit links.",
    )
    parser.add_argument(
        "--trusted-list-out",
        type=Path,
        required=True,
        help="Output file listing the recipes that passed trust verification.",
    )
    parser.add_argument(
        "--title-suffix",
        default="",
        help="Appended to the PR title, e.g. '(assigner)'.",
    )
    parser.add_argument(
        "--no-trust-info-pull-request",
        dest="no_trust_pr",
        action="store_true",
        help="Do not update trust info or write PR title/body files.",
    )
    opts = parser.parse_args()

    autopkg_cmd = opts.autopkg_cmd.split()
    prefs = f"--prefs={opts.prefs}"
    recipes = _read_recipe_list(opts.recipe_list) if opts.recipe_list else [opts.recipe]

    trusted: list[str] = []
    failures: list[tuple[str, str]] = []
    for recipe in recipes:
        result = _autopkg(autopkg_cmd, "verify-trust-info", recipe, prefs, "-vvv")
        if result.returncode == 0:
            print(f"Trust OK: {recipe}")
            trusted.append(recipe)
            continue

        print(f"::warning::Trust verification FAILED for {recipe} - skipping run")
        output = result.stdout or ""
        print(output)
        commit_links = extract_commit_links(output, opts.recipe_repo_dir)
        if commit_links:
            links_section = "\n".join(f"• {link}" for link in commit_links)
            output += f"\n\nUpstream commit(s):\n{links_section}"
        failures.append((recipe, output))

        if not opts.no_trust_pr:
            update = _autopkg(autopkg_cmd, "update-trust-info", recipe, prefs)
            if update.returncode != 0:
                print(
                    f"::error::update-trust-info failed for {recipe}:\n{update.stdout}"
                )

    opts.trusted_list_out.write_text(
        "".join(f"{r}\n" for r in trusted), encoding="utf-8"
    )
    print(
        f"{len(trusted)} trusted, {len(failures)} untrusted of {len(recipes)} recipes"
    )

    if failures and not opts.no_trust_pr:
        names = " ".join(_short_name(n) for n, _ in failures)
        title = f"Update trust for {names}"
        if opts.title_suffix:
            title += f" {opts.title_suffix}"
        Path("pull_request_title").write_text(title, encoding="utf-8")
        with open("pull_request_body", "w", encoding="utf-8") as body_file:
            for name, msg in failures:
                body_file.write(f"--- {name} ---\n{msg}\n")

    # Always exit 0: trust failures are reported via the PR, trusted recipes still run.
    return 0


if __name__ == "__main__":
    sys.exit(main())
