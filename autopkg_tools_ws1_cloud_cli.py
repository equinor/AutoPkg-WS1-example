#!/usr/bin/env python3

# BSD-3-Clause
# Copyright (c) Facebook, Inc. and its affiliates.
# Copyright (c) tig <https://6fx.eu/>.
# Copyright (c) Gusto, Inc.
# Copyright (c) Equinor ASA
# Copyright (c) Datamind AS
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the
# following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following
# disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the
# following disclaimer in the documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote
# products derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import inspect
import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime
from optparse import OptionParser
from pathlib import Path

import requests
import yaml

autopkg_tools_type = "Workspace ONE - autopkg-cloud-runner cli"
# This version features download metadata caching ONLY on the runner VM.
# This makes it extremely inefficient to run on ephemeral runners in GitHub cloud as it will cause ALL
# software downloads to be performed on each and every run.
# So suitable really only for self-hosted, on-prem use with persistent storage.

# if you need to use custom CA certificates, e.g. if your runner is behind a proxy or gateway that does packet
# inspection, you can use MasSesh or provide the extra certificates another way, like having all the CA certificates
# available in a file and set the REQUESTS_CA_BUNDLE environment variable to point to the path.
# The environment variable is used instead of macsesh if set.
if "REQUESTS_CA_BUNDLE" in os.environ:
    HAS_REQUESTS_CA_BUNDLE = True
    HAS_MACSESH = False
else:
    HAS_REQUESTS_CA_BUNDLE = False
    try:
        # see if we can import macsesh module
        # because of the deprecation issues cited below, using macsesh currently means you need urllib3 < 2.
        # https://github.com/sheagcraig/MacSesh/issues/7
        # https://github.com/sheagcraig/MacSesh/issues/9
        import macsesh

        HAS_MACSESH = True
    except ImportError:
        HAS_MACSESH = False
    except ModuleNotFoundError:
        HAS_MACSESH = False

# DEBUG = os.environ.get("DEBUG", "False").lower() in ("true", "1", "t")
AUTOPKG_TOOLS_VERBOSE = os.environ.get("AUTOPKG_TOOLS_VERBOSE", "1")
SLACK_WEBHOOK = os.environ.get("AUTOPKG_SLACK_WEBHOOK_TOKEN", None)
MUNKI_REPO = os.path.join(os.getenv("GITHUB_WORKSPACE", "/tmp/"), "munki_repo")
OVERRIDES_DIR = os.path.relpath("autopkg/overrides/")
RECIPE_TO_RUN = os.environ.get("RECIPE_TO_RUN", None)
AUTOPKG_CLI_KEYS = os.environ.get("AUTOPKG_CLI_KEYS", None)


def get_caller_name(level=2):
    """
    Get the name of a caller at a specific level up the stack.
    level=1: direct caller
    level=2: caller's parent
    level=3: caller's grandparent, etc.
    """
    frame = inspect.currentframe()
    try:
        for _ in range(level):
            frame = frame.f_back
            if frame is None:
                return "<module level>"
        return frame.f_code.co_name
    finally:
        # Clean up to avoid reference cycles
        del frame


def output(msg, verbose_level=1):
    """Print a message if verbosity is >= verbose_level"""
    if int(AUTOPKG_TOOLS_VERBOSE) >= verbose_level:
        print(f"autopkg_tools: {get_caller_name(2)}: {msg}")


def get_latest_modified_file(folder_path, pattern="*"):
    folder = Path(folder_path)
    files = list(folder.glob(pattern))
    files = [f for f in files if f.is_file()]
    if not files:
        return None
    latest_file = max(files, key=lambda f: f.stat().st_mtime)
    return latest_file


class Recipe(object):
    def __init__(self, path):
        self.path = os.path.join(OVERRIDES_DIR, path)
        self.error = False
        self.results = {}
        self.updated = False
        self.ws1_updated = False
        self.ws1_updated_assignments = False
        self.ws1_pruned = False
        self.verified = None

        self._keys = None
        self._has_run = False

        self._name = None

    @property
    def plist(self):
        if self._keys is None:
            with open(self.path, "rb") as f:
                self._keys = plistlib.load(f)

        return self._keys

    @property
    def yaml(self):
        if self._keys is None:
            with open(self.path, "r") as f:
                self._keys = yaml.safe_load(f)

        return self._keys

    @property
    def branch(self):
        return (
            "{}_{}".format(self.name, self.updated_version)
            .strip()
            .replace(" ", "")
            .replace(")", "-")
            .replace("(", "-")
        )

    @property
    def updated_version(self):
        if not self.results or not self.results["imported"]:
            return None

        return self.results["imported"][0]["version"].strip().replace(" ", "")

    @property
    def name(self):
        if self._name is None:
            # Recipe override does not have to include NAME, use autopkg info to retrieve NAME from parent recipe
            cmd = ["/usr/local/bin/autopkg", "info", f'"{self.path}"', "--quiet"]
            cmd = " ".join(cmd)
            output("Running " + str(cmd), verbose_level=2)
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
            )
            result, err = p.communicate()
            p_status = p.wait()
            if p_status == 0:
                # Search for input field NAME in the output, match value without quotes in 2nd group
                pattern = r"\'NAME\': \'(.*?)\',$"
                result_as_text = result.decode("utf-8")
                recipe_name = re.search(pattern, result_as_text, re.MULTILINE).group(1)
            else:
                err = err.decode()
                self.results["message"] = err
                recipe_name = None
            self._name = recipe_name
        return self._name

    def verify_trust_info(self):
        cmd = [
            "/usr/local/bin/autopkg",
            "verify-trust-info",
            "--prefs=autopkg/autopkg_prefs.plist",
            f'"{self.path}"',
            "-vvv",
        ]
        cmd = " ".join(cmd)
        output("Running " + str(cmd), verbose_level=2)
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
        )
        result, err = p.communicate()
        p_status = p.wait()
        if p_status == 0:
            self.verified = True
        else:
            err = err.decode()
            self.results["message"] = err
            self.verified = False
        return self.verified

    def update_trust_info(self):
        cmd = [
            "/usr/local/bin/autopkg",
            "update-trust-info",
            "--prefs=autopkg/autopkg_prefs.plist",
            f'"{self.path}"',
        ]
        cmd = " ".join(cmd)
        output("Running " + str(cmd), verbose_level=2)
        # Fail loudly if this exits 0
        try:
            subprocess.check_call(cmd, shell=True)
        except subprocess.CalledProcessError as e:
            output(e.stderr, verbose_level=1)
            raise e

    def _parse_report(self, report):
        with open(report, "rb") as f:
            report_data = plistlib.load(f)

        failed_items = report_data.get("failures", [])
        imported_items = []
        ws1_results_data = []
        if report_data["summary_results"]:
            # This means something happened
            munki_results = report_data["summary_results"].get(
                "munki_importer_summary_result", {}
            )
            imported_items.extend(munki_results.get("data_rows", []))

            if "ws1_importer_summary_result" in report_data["summary_results"]:
                # meaning ws1 has done something
                ws1_results = report_data["summary_results"].get(
                    "ws1_importer_summary_result", {}
                )
                ws1_results_header = ws1_results.get("header", {})
                if "new_assignment_rules" in ws1_results_header:
                    self.ws1_updated_assignments = True
                if "pruned_versions" in ws1_results_header:
                    self.ws1_pruned = True
                if (
                    ws1_results["summary_text"]
                    and "imported" in ws1_results["summary_text"]
                ):
                    self.ws1_updated = True
                ws1_results_data.extend(ws1_results.get("data_rows", []))
        return {
            "imported": imported_items,
            "failed": failed_items,
            "ws1_results_data": ws1_results_data,
        }

    def run(self):
        if not self.verified:
            self.error = True
            self.results["failed"] = True
            self.results["imported"] = ""
        else:
            # with cloud-autopkg-runner we can't specify the report path, only the folder for report files
            # report = "/tmp/autopkg.plist"
            # if not os.path.isfile(report):
            #     # Letting autopkg create them has led to errors on GitHub runners
            #     Path(report).touch()

            output(f"CLI keys for Autopkg call: {AUTOPKG_CLI_KEYS}", verbose_level=2)
            output(f"CLI keys type: {type(AUTOPKG_CLI_KEYS)}", verbose_level=2)
            # guard against empty env var AUTOPKG_CLI_KEYS
            # if AUTOPKG_CLI_KEYS:
            #     autopkg_cli_key_list = list(AUTOPKG_CLI_KEYS.split())
            # else:
            #     autopkg_cli_key_list = []
            try:
                cmd = [
                    "uv",
                    "run",
                    "cloud-autopkg-runner",
                    "--autopkg-pref-file",
                    "autopkg/autopkg_prefs.plist",
                    "--recipe",
                    f'"{self.path}"',
                    "--cache-file",
                    "metadata_cache.json",
                    "--log-file",
                    "autopkg/logs/autopkg_runner.log",
                    "--report-dir",
                    "autopkg/reports",
                    "--max-concurrency",
                    "1",
                    "--recipe-timeout",
                    "600",
                    "--post-processor",
                    "io.github.hjuutilainen.VirusTotalAnalyzer/VirusTotalAnalyzer",
                    "--post-processor",
                    "com.github.codeskipper.OMNISSA-WorkSpaceOneSlacker/WorkSpaceOneSlacker",
                ]
                # cannot now use env var AUTOPKG_verbose instead of cli arguments
                #
                for i in range(int(AUTOPKG_TOOLS_VERBOSE)):
                    cmd.append("-v")
                # Note - now using env vars instead of cli arguments because cloud-autopkg-runner
                # cli does not support custom keys
                # for key in autopkg_cli_key_list:
                #     cmd.extend(
                #         ["--key", str(key) + '="' + str(os.environ.get(key)) + '"']
                #     )
                cmd = " ".join(cmd)
                output(f"Running {str(cmd)}", verbose_level=2)
                subprocess.check_call(cmd, shell=True)
                # if result.returncode != 0:
                #     output(
                #         f"Error running the command, output:  {result.stderr}",
                #         verbose_level=1,
                #     )
                #     output("Exiting with error.", verbose_level=1)
                #     sys.exit(1)
            except subprocess.CalledProcessError:
                self.error = True
                self.results["failed"] = True
                self.results["imported"] = ""
                output(
                    "Error running the command, exception thrown, exiting with error.",
                    verbose_level=1,
                )
                sys.exit(1)

            self._has_run = True

            # ugly hack to fetch the latest report file when running sequentially instead of concurrently as
            # cloud-autopkg-runner is designed for.
            # Future aim is to fold report parsing (and Slack alerts) into a custom post-processor instead.
            report = get_latest_modified_file("autopkg/reports", pattern="*.plist")
            if not report or report is None:
                self.results["failed"] = True
                self.error = True
                self.results["imported"] = ""
                output("Error: No report found from autopkg run", verbose_level=1)
            output(f"Found latest report file at path [{report}]", verbose_level=2)

            self.results = self._parse_report(report)
            if (
                not self.results["failed"]
                and not self.error
                and self.updated_version
                and not remote_branch_check(self.branch)
            ):
                self.updated = True
            """
            if self.results["ws1_imported"]:
                self.ws1_updated = True
            if self.results["ws1_updated_assignments"]:
                self.ws1_updated_assignments = True
            """

        return self.results


# GIT FUNCTIONS
def git_run(cmd):
    cmd = ["git"] + cmd
    hide_cmd_output = True

    if int(AUTOPKG_TOOLS_VERBOSE) >= 2:
        hide_cmd_output = False
    output(f"Running {' '.join(cmd)}", verbose_level=2)
    try:
        result = subprocess.run(
            " ".join(cmd), shell=True, cwd=MUNKI_REPO, capture_output=hide_cmd_output
        )
        output(f"{result}", verbose_level=1)
    except subprocess.CalledProcessError as e:
        output(f"{e.stderr}", verbose_level=1)
        raise e


def current_branch():
    git_run(["rev-parse", "--abbrev-ref", "HEAD"])


def checkout(branch, new=True):
    if current_branch() != "main" and branch != "main":
        checkout("main", new=False)

    gitcmd = ["checkout"]
    if new:
        gitcmd += ["-b"]

    gitcmd.append(branch)
    # Lazy branch exists check
    try:
        git_run(gitcmd)
    except subprocess.CalledProcessError as e:
        if new:
            checkout(branch, new=False)
        else:
            raise e


def remote_branch_check(branch):
    # find out if remote branch already exists for current recipe version (branch)
    cmd = ["git", "branch", "--remotes"]
    try:
        output(
            f"Running check for existing remote git branches: {cmd}", verbose_level=1
        )
        result = subprocess.run(
            " ".join(cmd), shell=True, cwd=MUNKI_REPO, capture_output=True
        ).stdout.decode("utf-8")
        pattern = f"origin/{branch}"
        output(f"Pattern: {pattern} ", verbose_level=2)
        output(f"Result:\n{result}", verbose_level=2)
        match = re.search(pattern, result, re.MULTILINE)
        if match:
            output(f"Found matching remote branch: {match.group(0)}", verbose_level=1)
            return True
        else:
            output("No matching remote git branch found.", verbose_level=1)
            return False
    except subprocess.CalledProcessError as e:
        output(f"{e.stderr}", verbose_level=1)
        raise e


# Recipe handling
def handle_recipe(recipe, opts):
    if not opts.disable_verification:
        recipe.verify_trust_info()
        if recipe.verified is False:
            output(
                "Recipe override verify trust info FAILED, diff generated for pull request.",
                verbose_level=1,
            )
            if not opts.no_trust_pr:
                recipe.update_trust_info()
            output(f"Skipping autopkg run for [{recipe.path}]", verbose_level=1)
    if recipe.verified in (True, None):
        recipe.run()
        if recipe.results["imported"]:
            if remote_branch_check(recipe.branch):
                output(
                    f"Remote Git branch {recipe.branch} already exists, skipping commit/push and stashing changes.",
                    verbose_level=1,
                )
                return recipe
            output("Imported", verbose_level=1)
            checkout(recipe.branch)
            for imported in recipe.results["imported"]:
                output("Adding files", verbose_level=1)
                git_run(["add", f"'pkgs/{imported['pkg_repo_path']}'"])
                git_run(["add", f"'pkgsinfo/{imported['pkginfo_path']}'"])
            output("Committing changes", verbose_level=1)
            git_run(
                [
                    "commit",
                    "-m",
                    f"'Updated {recipe.name} to {recipe.updated_version}'",
                ]
            )
            output("Pushing changes", verbose_level=1)
            git_run(["push", "--set-upstream", "origin", recipe.branch])
    return recipe


def parse_recipes(recipes):
    recipe_list = []
    # Added this section so that we can run individual recipes
    if RECIPE_TO_RUN:
        for recipe in recipes:
            ext = os.path.splitext(recipe)[1]
            if ext != ".recipe" and ext != ".yaml":
                output(
                    f'Invalid recipe extension ."{ext}" (expected .recipe or .yaml)',
                    verbose_level=1,
                )
                sys.exit(1)
            else:
                recipe_list.append(recipe)
    else:
        ext = os.path.splitext(recipes)[1]
        if ext == ".json":
            parser = json.load
        elif ext == ".plist":
            parser = plistlib.load
        else:
            output(
                f'Invalid run list extension "{ext}" (expected plist or json)',
                verbose_level=1,
            )
            sys.exit(1)

        with open(recipes, "rb") as f:
            recipe_list = parser(f)

    return map(Recipe, recipe_list)


# Icon handling
def import_icons():
    branch_name = "icon_import_{}".format(datetime.now().strftime("%Y-%m-%d"))
    checkout(branch_name)
    subprocess.check_call("/usr/local/munki/iconimporter munki_repo", shell=True)
    git_run(["add", "icons/"])
    git_run(["commit", "-m", "Added new icons"])
    git_run(["push", "--set-upstream", "origin", f"{branch_name}"])


def slack_alert(recipe, opts):
    if int(AUTOPKG_TOOLS_VERBOSE) >= 3:
        output(
            "Skipping Slack notification - verbose level ≥3 is set.", verbose_level=3
        )
        return
    if not SLACK_WEBHOOK:
        output("Skipping slack notification - webhook is missing!", verbose_level=1)
        return

    if not recipe.verified:
        task_title = f"{recipe.name} failed trust verification (CICD-Slack)"
        task_description = recipe.results["message"]
    elif recipe.error:
        task_title = f"Failed to import {recipe.name} (CICD-Slack)"
        if not recipe.results["failed"]:
            task_description = "Unknown error"
        else:
            task_description = ("Error: {} \n" "Traceback: {} \n").format(
                recipe.results["failed"][0]["message"],
                recipe.results["failed"][0]["traceback"],
            )

            if "No releases found for repo" in task_description:
                # Just no updates
                return
    elif recipe.updated and not recipe.ws1_updated:
        task_title = f"Munki (NOT WS1 UEM!) imported {recipe.name} {str(recipe.updated_version)} (CICD-Slack)"
        task_description = (
            "*Catalogs:* %s \n" % recipe.results["imported"][0]["catalogs"]
            + "*Package Path:* `%s` \n" % recipe.results["imported"][0]["pkg_repo_path"]
            + "*Pkginfo Path:* `%s` \n" % recipe.results["imported"][0]["pkginfo_path"]
        )
    elif recipe.updated and recipe.ws1_updated:
        task_title = "WS1 UEM and Munki - Imported (CICD-Slack)"
        task_description = (
            "*WS1 UEM* \n"
            f"App:       `{recipe.name}` \n"
            f"Version: `{recipe.results['ws1_results_data'][0]['version']}` \n"
        )
        if recipe.results["ws1_results_data"][0].get("new_assignment_rules"):
            task_description += (
                "*Assignment rules:* "
                f"`{recipe.results['ws1_results_data'][0]['new_assignment_rules']}` \n"
            )
        if recipe.results["ws1_results_data"][0].get("console_location"):
            task_description += f"<{recipe.results['ws1_results_data'][0]['console_location']}|*console location*> \n\n"
        task_description += (
            "*Munki* \n"
            f"*Catalogs:* {recipe.results['imported'][0]['catalogs']} \n"
            f"*Package Path:* `{recipe.results['imported'][0]['pkg_repo_path']}` \n"
            f"*Pkginfo Path:* `{recipe.results['imported'][0]['pkginfo_path']}` \n"
        )
        if recipe.ws1_pruned:
            task_description += (
                f"*Pruned versions:* `{recipe.results['ws1_results_data'][0].get('pruned_versions')}` \n\n"
                f"*Number of versions pruned:* `{recipe.results['ws1_results_data'][0].get('pruned_versions_num')}` \n"
            )
    elif recipe.ws1_updated:
        task_title = "WS1 UEM - Imported (CICD-Slack)"
        task_description = (
            f"App:       `{recipe.name}` \n"
            f"Version: `{recipe.results['ws1_results_data'][0]['version']}` \n"
        )
        if recipe.results["ws1_results_data"][0].get("new_assignment_rules"):
            task_description += (
                "*Assignment rules:* "
                f"`{recipe.results['ws1_results_data'][0]['new_assignment_rules']}` \n"
            )
        if recipe.results["ws1_results_data"][0].get("console_location"):
            task_description += f"<{recipe.results['ws1_results_data'][0]['console_location']}|*console location*> \n"
        if recipe.ws1_pruned:
            task_description += (
                f"*Pruned versions:* `{recipe.results['ws1_results_data'][0].get('pruned_versions')}` \n\n"
                f"*Number of versions pruned:* `{recipe.results['ws1_results_data'][0].get('pruned_versions_num')}` \n"
            )
    elif recipe.ws1_updated_assignments:
        task_title = "WS1 UEM - New Assignment Rules (CICD-Slack)"
        task_description = (
            f"App:       `{recipe.name}` \n"
            f"Version: `{recipe.results['ws1_results_data'][0]['version']}` \n"
            f"*New Assignment rules:* `{recipe.results['ws1_results_data'][0]['new_assignment_rules']}` \n"
        )
        if recipe.results["ws1_results_data"][0].get("console_location"):
            task_description += f"<{recipe.results['ws1_results_data'][0]['console_location']}|*console location*> \n"
        if recipe.ws1_pruned:
            task_description += (
                f"*Pruned versions:* `{recipe.results['ws1_results_data'][0].get('pruned_versions')}` \n\n"
                f"*Number of versions pruned:* `{recipe.results['ws1_results_data'][0].get('pruned_versions_num')}` \n"
            )
    elif recipe.ws1_pruned:
        task_title = "WS1 UEM - old app versions pruned (CICD-Slack)"
        task_description = (
            f"App:       `{recipe.name}` \n"
            f"*Pruned versions:* `{recipe.results['ws1_results_data'][0].get('pruned_versions')}` \n"
            f"*Number of versions pruned:* `{recipe.results['ws1_results_data'][0].get('pruned_versions_num')}` \n"
        )
    else:
        # Fall through if no updates
        return

    response = requests.post(
        SLACK_WEBHOOK,
        data=json.dumps(
            {
                "attachments": [
                    {
                        "username": "Autopkg",
                        "as_user": True,
                        "title": task_title,
                        "color": (
                            "warning"
                            if not recipe.verified
                            else "good" if not recipe.error else "danger"
                        ),
                        "text": task_description,
                        "mrkdwn_in": ["text"],
                    }
                ]
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    if response.status_code != 200:
        raise ValueError(
            "Request to slack returned an error %s, the response is:\n%s"
            % (response.status_code, response.text)
        )


def main():
    start_time = datetime.now()

    parser = OptionParser(description="Wrap AutoPkg with git support.")
    parser.add_option(
        "-l", "--list", help="Path to a plist or JSON list of recipe names."
    )
    parser.add_option(
        "-g",
        "--gitrepo",
        help="Path to git repo. Defaults to MUNKI_REPO from Autopkg preferences.",
        default=MUNKI_REPO,
    )
    # parser.add_option(
    #     "-d",
    #     "--debug",
    #     action="store_true",
    #     help="Disables sending Slack alerts and adds more verbosity to output.",
    # )
    parser.add_option(
        "-v",
        "--disable_verification",
        action="store_true",
        help="Disables recipe verification.",
    )
    parser.add_option(
        "-n",
        "--no-trust-info-pull-request",
        dest="no_trust_pr",
        action="store_true",
        default=False,
        help="In case recipe trust info fails, no NOT generate a git Pull Request to update the trust info, "
        "default=False",
    )
    parser.add_option(
        "-i",
        "--icons",
        action="store_true",
        help="Run iconimporter against git munki repo.",
    )

    opts, _ = parser.parse_args()

    # global DEBUG
    # DEBUG = bool(DEBUG or opts.debug)

    output(
        f"Starting Autopkg tools session, type: [{autopkg_tools_type}].",
        verbose_level=1,
    )

    if HAS_REQUESTS_CA_BUNDLE:
        output(
            f"Found environment variable REQUESTS_CA_BUNDLE is set to: [{os.getenv('REQUESTS_CA_BUNDLE')}] "
            "so using that for CA-certificates instead of MacSesh module.",
            verbose_level=2,
        )
    else:
        if HAS_MACSESH:
            # Init the MacSesh so we can use the trusted certs in macOS Keychains to verify SSL.
            # Needed especially in networks with TLS packet inspection and custom certificates.
            macsesh.inject_into_requests()
            output("MacSesh is installed, imported, and injected.", verbose_level=2)
        else:
            output(
                "MacSesh was NOT found installed. If you need to use custom certificates for TLS packet inspection, "
                "you must either install it or provide the certs another way, like having the CA certificates "
                "available in a file and set the REQUESTS_CA_BUNDLE environment variable to point to the path.",
                verbose_level=2,
            )

    failures = []

    recipes = (
        RECIPE_TO_RUN.split(", ") if RECIPE_TO_RUN else opts.list if opts.list else None
    )
    if recipes is None:
        output("Recipe --list or RECIPE_TO_RUN not provided!", verbose_level=1)
        sys.exit(1)
    recipes = parse_recipes(recipes)
    for recipe in recipes:
        handle_recipe(recipe, opts)
        slack_alert(recipe, opts)
        if not opts.disable_verification:
            if not recipe.verified:
                failures.append(recipe)
    if not opts.disable_verification and not opts.no_trust_pr:
        if failures:
            title = " ".join([f"{recipe.name}" for recipe in failures])
            lines = [f"{recipe.results['message']}\n" for recipe in failures]
            with open("pull_request_title", "a+") as title_file:
                title_file.write(f"Update trust for {title}")
            with open("pull_request_body", "a+") as body_file:
                body_file.writelines(lines)

    if opts.icons:
        import_icons()

    end_time = datetime.now()
    duration = end_time - start_time
    output(
        f"Autopkg tools session, type: [{autopkg_tools_type}] duration:  [{duration.total_seconds()}] seconds",
        verbose_level=1,
    )


if __name__ == "__main__":
    main()
