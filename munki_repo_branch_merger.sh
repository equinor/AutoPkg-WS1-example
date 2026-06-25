#!/bin/bash
# munki_repo_branch_merger.sh
# merge git branches older than 1 month automatically and delete both local and remote

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



# fetch this script dir, name
DIR=$(dirname "${0:a}")
SCRIPT=$(basename "$0")

if [[ "$1" == "--auto-proceed" || "$MUNKI_REPO_BRANCH_MERGER_AUTO_PROCEED" = 'True' ]]; then
    auto_proceed=true
    echo "Enabling auto-proceed per cli/env argument, will proceed to automatically merge each eligible branch"
else
    auto_proceed=false
    echo "Setting to auto_proceed is disabled per default."
fi

if [[ ! "$GITHUB_ACTIONS" = true ]]; then
    GITHUB_ACTIONS=false
fi

function merge_branch() {
    local the_branch=$1

    # merge remote branch, always accept default commit message
    echo "merging remote branch, running: git merge --no-edit origin/$the_branch"
    if ! git merge --no-edit "origin/$the_branch" ; then
        echo "merge of remote branch $the_branch went wrong, bailing out"
        exit 1
    fi

    echo "deleting remote branch, running: git push origin -d $the_branch"
    git push origin -d "$the_branch"

    echo "pushing local merge commits"
    git push
}

echo "Entering munki_repo"
cd "$DIR/munki_repo" || exit

echo "Pulling changes from GitHub"
git pull

echo "Checking out branch [main]"
git checkout main

## get remote branches that are not merged with origin/main, filter on authordate older than e.g. 2 weeks or 1 month ago
if [[ ! "$OSTYPE" == "darwin"* ]]; then
    # date calc on other OS - likely Ubuntu (for GitHub hosted runner)
    # the_date=$(date +'%Y-%m-%d' -d "-2 months")
    the_date=$(date +'%Y-%m-%d' -d "-2 weeks")
else
    # date calc on macOS
    # the_date=$(date -v-2m +'%Y-%m-%d')
    the_date=$(date -v-2w +'%Y-%m-%d')
fi

echo "query git for remote branches older than $the_date"
git for-each-ref --sort=authordate refs/remotes --format='%(authordate:short) %(refname:short)' --no-merged origin/main | awk "\$0 < \"$the_date\"" > "/tmp/$SCRIPT.tmp"
# cat /tmp/$SCRIPT.tmp
while read -r a_line; do
      a_branch=$( awk -F ' origin/' '{print $2}' <<< "$a_line" )
      branch_date=$( awk -F ' origin/' '{print $1}' <<< "$a_line" )
      #echo "Processing: $a_line"
      echo "Processing -  branch[$a_branch] created[$branch_date]"

      if [ "$auto_proceed" = false ]; then
          if [ "$GITHUB_ACTIONS" = false ]; then
              # running in interactive environment
              read -rsp "proceed to merge ([y]/n) ? : " user_input </dev/tty
              # shellcheck disable=SC2154
              if [[ "n" == "$user_input" ]]; then
                    echo "bailing out as directed by user"
                  exit 0
              fi
              echo
          else
              # when running automated in GitHub Action CI pipeline
              merge_branch "$a_branch"
              echo "bailing out after single merge as auto-proceed is disabled."
              exit 0
          fi
      else
          echo "Proceeding to merge automatically per cli argument"
      fi

      merge_branch "$a_branch"

done < "/tmp/$SCRIPT.tmp"


