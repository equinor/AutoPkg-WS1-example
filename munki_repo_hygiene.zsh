#!/bin/zsh
# Cleanup script for munki_repo: prune stale branches, remove untracked files, and clean LFS cache
# Untracked files are checked against every origin ref before removal, so files that exist
# ONLY locally (never pushed) are flagged and require a separate confirmation.
# Usage: ./munki_repo_hygiene.zsh [--gc] [--keep N]
#   --gc      Also run git gc --aggressive for extra compression (~5-10% savings)
#   --keep N  Versions per item kept by the local-only Munki repoclean (default 10; never pushed)

set -euo pipefail

RUN_GC=false
REPOCLEAN_KEEP=10
while (( $# > 0 )); do
    case "$1" in
        --gc) RUN_GC=true ;;
        --keep)
            [[ "${2:-}" == <-> ]] && (( $2 > 0 )) || { echo "Error: --keep needs a positive integer"; exit 1; }
            REPOCLEAN_KEEP=$2; shift ;;
        *) echo "Usage: $0 [--gc] [--keep N]"; exit 1 ;;
    esac
    shift
done

REPOCLEAN=/usr/local/munki/repoclean
MUNKI_PYTHON=/usr/local/munki/munki-python

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MUNKI_REPO="${SCRIPT_DIR}/munki_repo"

if [[ ! -d "$MUNKI_REPO/.git" ]]; then
    echo "Error: $MUNKI_REPO not found or not a git repo"
    exit 1
fi

cd "$MUNKI_REPO"

echo "=== Munki Repo Hygiene ==="
echo "Location: $MUNKI_REPO"
echo

# 1. Fetch and prune (first, so the untracked-file check below sees current origin state)
echo "1. Fetching and pruning remote tracking branches..."
git fetch --prune
echo "   ✓ Done"
echo

# 2. Clean untracked files — but first check whether each one already exists on origin
echo "2. Checking for untracked files..."
UNTRACKED_FILES=("${(@f)$(git ls-files --others --exclude-standard)}")
UNTRACKED_FILES=("${(@)UNTRACKED_FILES:#}")  # drop empty element when there are no files

if (( ${#UNTRACKED_FILES} == 0 )); then
    echo "   ✓ No untracked files"
else
    echo "   Found ${#UNTRACKED_FILES} untracked files — checking origin for each path..."
    # Build a path → "ref ref ..." index over every origin ref (path-based: Munki paths embed the version)
    typeset -A ORIGIN_PATHS
    for REF in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin | grep -v '/HEAD$'); do
        while IFS= read -r P; do
            ORIGIN_PATHS[$P]+="${REF#origin/} "
        done < <(git ls-tree -r --name-only "$REF")
    done

    typeset -a SAFE_FILES UNSAFE_FILES
    for F in "${UNTRACKED_FILES[@]}"; do
        REFS="${ORIGIN_PATHS[$F]:-}"
        if [[ -z "$REFS" ]]; then
            UNSAFE_FILES+=("$F")
            echo "     ⚠  NOT on origin : $F"
        elif [[ " $REFS" == *" main "* ]]; then
            SAFE_FILES+=("$F")
            echo "     ✓  on origin/main: $F"
        else
            SAFE_FILES+=("$F")
            echo "     ✓  on origin branch (${REFS% }): $F"
        fi
    done
    echo

    if (( ${#UNSAFE_FILES} > 0 )); then
        echo "   ⚠  ${#UNSAFE_FILES} file(s) exist ONLY locally — deleting them loses the only copy."
        echo "      Commit/push them to a branch first (or re-run the recipe later) if they matter."
        echo
    fi

    if (( ${#SAFE_FILES} > 0 )); then
        read -q "CLEAN_SAFE?   Remove the ${#SAFE_FILES} file(s) that exist on origin? (y/n) " || CLEAN_SAFE="n"
        echo
        if [[ "$CLEAN_SAFE" == "y" ]]; then
            printf '%s\0' "${SAFE_FILES[@]}" | xargs -0 rm -f --
            echo "   ✓ Removed files present on origin"
        else
            echo "   (skipped)"
        fi
    fi

    if (( ${#UNSAFE_FILES} > 0 )); then
        read -q "CLEAN_UNSAFE?   Also remove the ${#UNSAFE_FILES} file(s) NOT on origin? (y/n) " || CLEAN_UNSAFE="n"
        echo
        if [[ "$CLEAN_UNSAFE" == "y" ]]; then
            printf '%s\0' "${UNSAFE_FILES[@]}" | xargs -0 rm -f --
            echo "   ✓ Removed local-only files"
        else
            echo "   (kept)"
        fi
    fi

    # Sweep any directories left empty by the removals
    find pkgs pkgsinfo -type d -empty -delete 2>/dev/null || true
fi
echo

# 3. Find stale local branches
echo "3. Checking for local branches with deleted remotes..."
CURRENT_BRANCH=$(git symbolic-ref --quiet --short HEAD || echo "")
typeset -a PUSHED_BRANCHES UNPUSHED_BRANCHES
while read -r B TRACK; do
    [[ "$TRACK" == "[gone]" ]] || continue
    if [[ "$B" == "$CURRENT_BRANCH" ]]; then
        echo "   (skipping $B: currently checked out)"
        continue
    fi
    # Only branches whose commits are all reachable from an origin ref are safe to delete
    UNPUSHED=$(git rev-list --count "$B" --not --remotes=origin)
    if (( UNPUSHED > 0 )); then
        UNPUSHED_BRANCHES+=("$B")
        echo "     ⚠  KEPT, $UNPUSHED commit(s) not on origin: $B"
    else
        PUSHED_BRANCHES+=("$B")
        echo "     ✓  fully on origin: $B"
    fi
done < <(git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads)

if (( ${#UNPUSHED_BRANCHES} > 0 )); then
    echo
    echo "   ⚠  ${#UNPUSHED_BRANCHES} branch(es) hold commits that exist ONLY locally and are kept."
    echo "      Push them (git push -u origin <branch>) or delete them by hand if obsolete."
fi

if (( ${#PUSHED_BRANCHES} == 0 )); then
    echo "   ✓ No stale branches to delete"
else
    echo
    read -q "DELETE_BRANCHES?   Delete the ${#PUSHED_BRANCHES} branch(es) fully on origin? (y/n) " || DELETE_BRANCHES="n"
    echo
    if [[ "$DELETE_BRANCHES" == "y" ]]; then
        # -D is needed because local main may lag origin/main; safety is the rev-list check above
        git branch -D -- "${PUSHED_BRANCHES[@]}"
        echo "   ✓ Deleted"
    else
        echo "   (skipped)"
    fi
fi
echo

# 4. Munki repoclean, LOCAL ONLY: frees working-tree space on this Mac and is never committed or pushed.
#    Pruning the shared repo is the job of the weekly CI workflow (munki_repoclean.yml).
echo "4. Munki repoclean, local only (keep $REPOCLEAN_KEEP versions per item)..."
count_deleted() { git ls-files --deleted -- pkgs pkgsinfo | wc -l | tr -d ' '; }
run_repoclean() {
    if [[ ! -x "$REPOCLEAN" ]]; then
        echo "   ⚠  $REPOCLEAN not found, skipping (install Munki tools)"
        return 0
    fi
    read -q "RUN_RC?   Run repoclean now? It previews deletions and asks again before deleting. (y/n) " || RUN_RC="n"
    echo
    [[ "$RUN_RC" == "y" ]] || { echo "   (skipped)"; return 0; }

    # repoclean also deletes pkgs no pkginfo refers to; untracked ones would be lost for good
    local LEFTOVER
    LEFTOVER=$(git ls-files --others --exclude-standard -- pkgs pkgsinfo)
    if [[ -n "$LEFTOVER" ]]; then
        echo "   ⚠  Untracked files remain under pkgs/ or pkgsinfo/; repoclean may delete them as orphans:"
        echo "$LEFTOVER" | sed 's/^/     - /'
        read -q "GO_ON?   Continue anyway? (y/n) " || GO_ON="n"
        echo
        [[ "$GO_ON" == "y" ]] || { echo "   (skipped)"; return 0; }
    fi

    local BEFORE_DEL AFTER_DEL
    BEFORE_DEL=$(count_deleted)
    if ! "$MUNKI_PYTHON" "$REPOCLEAN" --keep="$REPOCLEAN_KEEP" "$MUNKI_REPO"; then
        echo "   ⚠  repoclean exited with an error"
    fi
    AFTER_DEL=$(count_deleted)

    echo
    if (( AFTER_DEL == BEFORE_DEL )); then
        echo "   ✓ Nothing deleted"
    else
        echo "   ✓ Removed $(( AFTER_DEL - BEFORE_DEL )) tracked file(s) from the working tree."
    fi
    if (( AFTER_DEL > 0 )); then
        echo "   $AFTER_DEL tracked file(s) are now deleted locally and show as 'D' in git status."
        echo "   Leave them uncommitted (no 'git add -A' / 'git commit -a' in munki_repo)."
        echo "   They return on 'git reset --hard' (launchers do this when the pull fails) or with:"
        echo "     git -C \"$MUNKI_REPO\" checkout -- pkgs pkgsinfo"
    fi
}
run_repoclean
echo

# 5. LFS prune (--verify-remote: only drop objects that origin confirms it has)
echo "5. Pruning LFS object cache (verifying each object on origin first)..."
BEFORE=$(du -sh .git/lfs/objects 2>/dev/null | awk '{print $1}' || echo "unknown")
if git lfs prune --verify-remote; then
    AFTER=$(du -sh .git/lfs/objects 2>/dev/null | awk '{print $1}' || echo "unknown")
    echo "   LFS cache: $BEFORE → $AFTER"
    echo "   ✓ Done"
else
    echo "   ⚠  git lfs prune stopped: origin is missing some objects, nothing was pruned."
    echo "      Push the affected branch(es) and re-run."
fi
echo

# 6. Optional git gc
if $RUN_GC; then
    echo "6. Running git gc --aggressive (this may take a minute)..."
    git gc --aggressive
    echo "   ✓ Done"
    echo
fi

echo "=== Cleanup Complete ==="
git -C "$MUNKI_REPO" count-objects -vH | grep "size-pack"
