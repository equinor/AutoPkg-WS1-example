#!/usr/bin/env zsh

## Launcher script for autopkg_tool.py for local testing
## stores and retrieves secrets in separate keychain

# BSD-3-Clause
# Copyright (c) Equinor ASA
# Copyright (c) Datamind AS
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# for debugging this script, before starting it, run with a higher number, like 'export AUTOPKG_TOOLS_LAUNCHER_VERBOSE=2'
if ! (( ${+AUTOPKG_TOOLS_LAUNCHER_VERBOSE} )); then
	export AUTOPKG_TOOLS_LAUNCHER_VERBOSE=1
fi
export | grep "AUTOPKG_TOOLS_LAUNCHER_VERBOSE="

## settings for macOS Keychain
launcher_keychain="autopkg_tools_launcher_keychain"

# for WS1 API access with Basic auth:
# export autopkg_tools_secrets=(WS1_API_TOKEN WS1_API_USERNAME WS1_API_PASSWORD WS1_API_URL WS1_CONSOLE_URL WS1_GROUPID)

# for WS1 API access with Oauth
# export autopkg_tools_secrets=(WS1_API_URL WS1_CONSOLE_URL WS1_GROUPID WS1_OAUTH_CLIENT_ID WS1_OAUTH_CLIENT_SECRET WS1_OAUTH_TOKEN_URL SLACK_WEBHOOK_TOKEN)
autopkg_tools_secrets=(WS1_API_URL WS1_CONSOLE_URL WS1_GROUPID WS1_OAUTH_CLIENT_ID WS1_OAUTH_CLIENT_SECRET WS1_OAUTH_TOKEN_URL SLACK_WEBHOOK_TOKEN)

## what arguments autopkg_tools.py should pass to autopkg call as --key="..."
# for WS1 API access with Basic auth:
# export AUTOPKG_CLI_KEYS="WS1_API_TOKEN WS1_API_USERNAME WS1_API_PASSWORD WS1_API_URL WS1_CONSOLE_URL WS1_GROUPID"

# for WS1 API access with Oauth
# export AUTOPKG_CLI_KEYS="WS1_API_URL WS1_CONSOLE_URL WS1_GROUPID WS1_OAUTH_CLIENT_ID WS1_OAUTH_CLIENT_SECRET WS1_OAUTH_TOKEN_URL"

# Get this scripts starting folder
this_script="${0:a}"
script_path="${this_script:h}"

export GITHUB_WORKSPACE="$script_path"

# You need to set these (once) before running this script:
# defaults write com.github.autopkg RECIPE_OVERRIDE_DIRS "$script_path"/overrides
# defaults write com.github.autopkg RECIPE_REPO_DIR "$script_path"/repos
# defaults write com.github.autopkg FAIL_RECIPES_WITHOUT_TRUST_INFO -bool YES

# write Internet timestamp (RFC 3339) + message to stdout
function log() {
	timestamp=$(date +%Y-%m-%d\ %H:%M:%S%z)
	echo "$timestamp [$this_script] $1"
}

log "script_path: $script_path"

log "You are running the launcher script for autopkg_tools.py that stores and retrieves secrets in a dedicated keychain for local testing."
log "Setting out to check dedicated keychain for required secrets called $launcher_keychain"

# Check/ensure autopkg_tool_keychain is present
#autopkg_tools_keychain_present=$(security list-keychains | grep -q "$autopkg_tools_tester_keychain")
#security list-keychains | grep "autopkg_tools_tester_keychain"
if ! security list-keychains | grep -q "${launcher_keychain}"; then
	log "Autopkg_tools launcher keychain not found, proceeding to create one"
	echo "Please enter a password for the new launcher keychain"
	read -rs 'pw?Password: ' </dev/tty
	# create new empty keychain
	# shellcheck disable=SC2154
	security create-keychain -p "${pw}" "${launcher_keychain}"

	# add keychain to user's keychain search list so they can access it
	security list-keychains -d user -s "${launcher_keychain}" "$(security list-keychains -d user | tr -d '"')"

	# removing relock timeout on keychain
	security set-keychain-settings "${launcher_keychain}"
fi

log "keychain found, checking required secrets"
# shellcheck disable=SC2128
for secret in $autopkg_tools_secrets; do
	log "checking secret for \"$secret\"..."
	if ! the_secret=$(security find-generic-password -a "$secret" -w ${launcher_keychain}); then
		echo "No password found for: \"$secret\" - Please enter a new password to store:"
		read -rs 'pw?Password: ' </dev/tty
		security add-generic-password -a "${secret}" -s "autopkg_tool_launcher" -w "$pw" ${launcher_keychain}
		the_secret="$pw"
	fi
	# shellcheck disable=SC2154
	# shellcheck disable=SC2071
	[[ "$AUTOPKG_TOOLS_LAUNCHER_VERBOSE" > "1" ]] && log "testing... found password for ${secret} : ${the_secret}"
	log "secret \"$secret\" retrieved, passing env var to autopkg_tool.py"
	export "AUTOPKG_$secret"="$the_secret"
done

export AUTOPKG_ws1_slack_webhook_url=$AUTOPKG_SLACK_WEBHOOK_TOKEN

log "The following environment exports are set for Autopkg"
# cloud-autopkg-runner does not recognise a environment variable for verbose level at this time, only a cli argument
# and it overrides the AUTOPKG_verbose with that as well, therefore commenting out this
if ! (( ${+AUTOPKG_verbose} )); then
	export AUTOPKG_verbose=2
fi
export | grep "AUTOPKG_verbose="

if ! (( ${+AUTOPKG_TOOLS_VERBOSE} )); then
	export AUTOPKG_TOOLS_VERBOSE=2
fi
export | grep "AUTOPKG_TOOLS_VERBOSE="

if ! (( ${+AUTOPKG_ws1_force_import} )); then
	export AUTOPKG_ws1_force_import="False"
fi
export | grep "AUTOPKG_ws1_force_import="

if ! (( ${+AUTOPKG_ws1_import_new_only} )); then
	export AUTOPKG_ws1_import_new_only="False"
fi
export | grep "AUTOPKG_ws1_import_new_only="

if ! (( ${+AUTOPKG_ws1_update_assignments} )); then
	export AUTOPKG_ws1_update_assignments="True"
fi
export | grep "AUTOPKG_ws1_update_assignments="

if ! (( ${+AUTOPKG_ws1_app_versions_prune} )); then
	export AUTOPKG_ws1_app_versions_prune="dry_run"
fi
export | grep "AUTOPKG_ws1_app_versions_prune="


read -rs 'user_input?proceed to update Munki repo ([y]/n) ' </dev/tty
# shellcheck disable=SC2154
# shellcheck disable=SC2071
[[ "$AUTOPKG_TOOLS_LAUNCHER_VERBOSE" > "1" ]] && echo "DEBUG: read your answer as: [$user_input]"
echo .
# shellcheck disable=SC2154
if [[ "n" != "$user_input" ]]; then
	cd "$script_path/munki_repo"  || { log "failed to enter munki_repo - aborting";  exit 1; }
	log "changed directory to Munki repo at $PWD"
	git checkout main
	git pull
	# shellcheck disable=SC2181
	if ! [ $? = 0 ]; then
		read -rs 'user_input?overwrite local changes ([y]/n) ' </dev/tty
		echo .
		if [[ "n" == "$user_input" ]]; then
			log "bailing out as directed by user"
			exit 0
		else
			git fetch --all --prune
			git reset --hard origin/main
		fi
	fi
	cd "$script_path" || { log "failed to enter script folder - aborting";  exit 1; }
	# log "script_path: $script_path"
else
	log "skipping Munki repo update as directed by user"
fi


log "running Munki makecatalogs to make sure the repo is ready"
/usr/local/munki/makecatalogs munki_repo  1>/dev/null


# cd ..
log "changed directory back to $PWD"

log "Export AutoPkg preferences for cloud-autopkg-runner (CAR)"
# This ensures the defaults preferences are written out to to file before calling CAR, which is needed because
# CAR does not use macOS native defaults settings.
# When defaults are not passed as expected to CAR, this is known to lead to sporadic
# errors "Could not find parent recipe..."
# We will use the exported binary plist file as an argument to CAR in autopkg_tools_ws1_cloud_cli.py
defaults export com.github.autopkg "autopkg/autopkg_prefs.plist"
log "These are the AutoPkg preferences for CAR/AutoPkg:"
autopkg info --prefs=autopkg/autopkg_prefs.plist


read -rs 'user_input?Proceed to run autopkg_tools_ws1_cloud_cli with CAR for import/upload recipes ([y]/n) ' </dev/tty
# shellcheck disable=SC2154
# shellcheck disable=SC2071
[[ "$AUTOPKG_TOOLS_LAUNCHER_VERBOSE" > "1" ]] &&  echo "DEBUG: read your answer as: [$user_input]"
if [[ "n" != "$user_input" ]]; then
	log "running /usr/local/autopkg/python autopkg_tools_ws1_cloud_cli.py"
	export RECIPE_TO_RUN="$1"
	/usr/local/autopkg/python autopkg_tools_ws1_cloud_cli.py -l autopkg/recipe_list.json --no-trust-info-pull-request
else
	log "skipping run of autopkg_tools_ws1_cloud_cli.py as directed by user"
fi


read -rs 'user_input?Proceed to run Assigner recipes ([y]/n) ' </dev/tty
# shellcheck disable=SC2154
# shellcheck disable=SC2071
[[ "$AUTOPKG_TOOLS_LAUNCHER_VERBOSE" > "1" ]] &&  echo "DEBUG: read your answer as: [$user_input]"
if [[ "n" != "$user_input" ]]; then
	export AUTOPKG_ws1_slack_webhook_url=$AUTOPKG_SLACK_WEBHOOK_TOKEN
	RECIPE_TO_RUN="$1"
	if [[ -z "$RECIPE_TO_RUN" ]]; then
		log "no recipe specified as argument, using default recipe_list_assigner.txt"
		TO_RUN=(--recipe-list autopkg/recipe_list_assigner.txt)  # use zsh array to be able to pass the argument with space in it as intended
	else
		log "using recipe specified as argument: $RECIPE_TO_RUN"
		TO_RUN=("$RECIPE_TO_RUN")
	fi
	log "running \nuv run /usr/local/autopkg/python /Library/AutoPkg/autopkg run ${TO_RUN[*]} \
		--report-plist autopkg/reports/assigner_report.plist \
		--post com.github.codeskipper.OMNISSA-WorkSpaceOneSlacker/WorkSpaceOneSlacker \
		--prefs autopkg/autopkg_prefs.plist"
	uv run /usr/local/autopkg/python /Library/AutoPkg/autopkg run "${TO_RUN[@]}" \
			--report-plist autopkg/reports/assigner_report.plist \
			--post com.github.codeskipper.OMNISSA-WorkSpaceOneSlacker/WorkSpaceOneSlacker \
			--prefs autopkg/autopkg_prefs.plist \
			2>&1 | tee autopkg/logs/autopkg_assigner.log
else
	log "skipping run of Assigner recipes as directed by user"
fi


read -rs 'user_input?Proceed to run Pruner recipes ([y]/n) ' </dev/tty
# shellcheck disable=SC2154
# shellcheck disable=SC2071
[[ "$AUTOPKG_TOOLS_LAUNCHER_VERBOSE" > "1" ]] &&  echo "DEBUG: read your answer as: [$user_input]"
if [[ "n" != "$user_input" ]]; then
	RECIPE_TO_RUN="$1"
	if [[ -z "$RECIPE_TO_RUN" ]]; then
		log "no recipe specified as argument, using default recipe_list_prune.txt"
		TO_RUN=(--recipe-list autopkg/recipe_list_pruner.txt)  # use zsh array to be able to pass the argument with space in it as intended
	else
		log "using recipe specified as argument: $RECIPE_TO_RUN"
		TO_RUN=("$RECIPE_TO_RUN")
	fi
	log "running \nuv run /usr/local/autopkg/python /Library/AutoPkg/autopkg run ${TO_RUN[*]} \
		--report-plist autopkg/reports/prune_report.plist \
		--post com.github.codeskipper.OMNISSA-WorkSpaceOneSlacker/WorkSpaceOneSlacker \
		--prefs autopkg/autopkg_prefs.plist" \
		2>&1 | tee autopkg/logs/autopkg_pruner.log
	uv run /usr/local/autopkg/python /Library/AutoPkg/autopkg run "${TO_RUN[@]}" \
			--report-plist autopkg/reports/prune_report.plist \
			--post com.github.codeskipper.OMNISSA-WorkSpaceOneSlacker/WorkSpaceOneSlacker \
			--prefs autopkg/autopkg_prefs.plist
else
	log "skipping run of Pruner recipes as directed by user"
fi


log "that is all for now - exiting"
