#!/usr/bin/env zsh

## Launcher script for autopkg_tools_ws1_car.py for local testing.
## Mirrors autopkg_tools_launcher_cloud_cli.zsh but invokes the new
## cloud-autopkg-runner library-based driver instead of the CAR CLI.

# BSD-3-Clause
# Copyright (c) Equinor ASA
# Copyright (c) Datamind AS

if ! (( ${+AUTOPKG_TOOLS_LAUNCHER_VERBOSE} )); then
	export AUTOPKG_TOOLS_LAUNCHER_VERBOSE=1
fi
export | grep "AUTOPKG_TOOLS_LAUNCHER_VERBOSE="

launcher_keychain="autopkg_tools_launcher_keychain"
autopkg_tools_secrets=(WS1_API_URL WS1_CONSOLE_URL WS1_GROUPID WS1_OAUTH_CLIENT_ID WS1_OAUTH_CLIENT_SECRET WS1_OAUTH_TOKEN_URL SLACK_WEBHOOK_TOKEN)

this_script="${0:a}"
script_path="${this_script:h}"
export GITHUB_WORKSPACE="$script_path"

function log() {
	timestamp=$(date +%Y-%m-%d\ %H:%M:%S%z)
	echo "$timestamp [$this_script] $1"
}

log "script_path: $script_path"
log "Launcher for autopkg_tools_ws1_car.py (cloud-autopkg-runner LIBRARY mode)"
log "Checking dedicated keychain for required secrets: $launcher_keychain"

if ! security list-keychains | grep -q "${launcher_keychain}"; then
	log "Autopkg_tools launcher keychain not found, creating one"
	echo "Please enter a password for the new launcher keychain"
	read -rs 'pw?Password: ' </dev/tty
	security create-keychain -p "${pw}" "${launcher_keychain}"
	security list-keychains -d user -s "${launcher_keychain}" "$(security list-keychains -d user | tr -d '"')"
	security set-keychain-settings "${launcher_keychain}"
fi

for secret in $autopkg_tools_secrets; do
	log "checking secret \"$secret\"..."
	if ! the_secret=$(security find-generic-password -a "$secret" -w ${launcher_keychain}); then
		echo "No password found for: \"$secret\" - Please enter a new password to store:"
		read -rs 'pw?Password: ' </dev/tty
		security add-generic-password -a "${secret}" -s "autopkg_tool_launcher" -w "$pw" ${launcher_keychain}
		the_secret="$pw"
	fi
	[[ "$AUTOPKG_TOOLS_LAUNCHER_VERBOSE" > "1" ]] && log "found password for ${secret}"
	export "AUTOPKG_$secret"="$the_secret"
done

log "Setting AutoPkg / tooling environment defaults"
if ! (( ${+AUTOPKG_verbose} )); then export AUTOPKG_verbose=2; fi
export | grep "AUTOPKG_verbose="

if ! (( ${+AUTOPKG_TOOLS_VERBOSE} )); then export AUTOPKG_TOOLS_VERBOSE=2; fi
export | grep "AUTOPKG_TOOLS_VERBOSE="

if ! (( ${+AUTOPKG_TOOLS_MAX_CONCURRENCY} )); then export AUTOPKG_TOOLS_MAX_CONCURRENCY=4; fi
export | grep "AUTOPKG_TOOLS_MAX_CONCURRENCY="

if ! (( ${+AUTOPKG_TOOLS_RECIPE_TIMEOUT} )); then export AUTOPKG_TOOLS_RECIPE_TIMEOUT=1800; fi
export | grep "AUTOPKG_TOOLS_RECIPE_TIMEOUT="

if ! (( ${+AUTOPKG_ws1_force_import} )); then export AUTOPKG_ws1_force_import="False"; fi
export | grep "AUTOPKG_ws1_force_import="

if ! (( ${+AUTOPKG_ws1_import_new_only} )); then export AUTOPKG_ws1_import_new_only="False"; fi
export | grep "AUTOPKG_ws1_import_new_only="

if ! (( ${+AUTOPKG_ws1_update_assignments} )); then export AUTOPKG_ws1_update_assignments="True"; fi
export | grep "AUTOPKG_ws1_update_assignments="

if ! (( ${+AUTOPKG_ws1_app_versions_prune} )); then export AUTOPKG_ws1_app_versions_prune="dry_run"; fi
export | grep "AUTOPKG_ws1_app_versions_prune="

read -rs 'user_input?proceed to update Munki repo ([y]/n) ' </dev/tty
[[ "$AUTOPKG_TOOLS_LAUNCHER_VERBOSE" > "1" ]] && echo "DEBUG: read your answer as: [$user_input]"
echo .
if [[ "n" != "$user_input" ]]; then
	cd "$script_path/munki_repo" || { log "failed to enter munki_repo - aborting"; exit 1; }
	log "changed directory to Munki repo at $PWD"
	git checkout main
	git pull
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
	cd "$script_path" || { log "failed to enter script folder - aborting"; exit 1; }
else
	log "skipping Munki repo update as directed by user"
fi

log "running Munki makecatalogs to ensure repo is ready"
/usr/local/munki/makecatalogs munki_repo 1>/dev/null

log "Export AutoPkg preferences for cloud-autopkg-runner (CAR)"
defaults export com.github.autopkg "autopkg/autopkg_prefs.plist"
log "These are the AutoPkg preferences for CAR/AutoPkg:"
autopkg info --prefs=autopkg/autopkg_prefs.plist

read -rs 'user_input?Proceed to run autopkg_tools_ws1_car (CAR library) for importer recipes ([y]/n) ' </dev/tty
[[ "$AUTOPKG_TOOLS_LAUNCHER_VERBOSE" > "1" ]] && echo "DEBUG: read your answer as: [$user_input]"
if [[ "n" != "$user_input" ]]; then
	log "running uv run python autopkg_tools_ws1_car.py"
	export RECIPE_TO_RUN="$1"
	export AUTOPKG_ws1_slack_webhook_url=$AUTOPKG_SLACK_WEBHOOK_TOKEN
	uv run python autopkg_tools_ws1_car.py -l autopkg/recipe_list.json --no-trust-info-pull-request
else
	log "skipping run of autopkg_tools_ws1_car.py as directed by user"
fi

log "that is all for now - exiting"

