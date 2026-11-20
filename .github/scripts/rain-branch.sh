#!/usr/bin/env bash
# The `rain` branch of the live-rain job (.github/workflows/rain.yml, docs/live-rain.md).
#
#   rain-branch.sh push DIR   commit DIR (index.json + <region>.bin.gz) as one orphan commit and force-push it:
#                             the branch only ever holds the latest files
#   rain-branch.sh age        whole hours since the published index.json was generated (-1: no branch yet)
#
# Remote: $RAIN_REMOTE (a URL or path; local tests), else https://github.com/$GITHUB_REPOSITORY with $GH_TOKEN.
# Branch: $RAIN_BRANCH (default rain). The commit message is index.json's summary (time, ECMWF runs and age).
set -euo pipefail

branch=${RAIN_BRANCH:-rain}
remote=${RAIN_REMOTE:-}
if [ -z "$remote" ]; then
  remote="https://x-access-token:${GH_TOKEN:?set GH_TOKEN or RAIN_REMOTE}@github.com/${GITHUB_REPOSITORY:?}.git"
fi
field() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

case "${1:-}" in
  push)
    dir=${2:?usage: rain-branch.sh push DIR}
    [ -f "$dir/index.json" ] || { echo "no $dir/index.json" >&2; exit 1; }
    msg=$(field "$dir/index.json" summary)
    args=(-m "$msg")
    if [ -n "${RUN_URL:-}" ]; then args+=(-m "Workflow run: $RUN_URL"); fi
    rm -rf "$dir/.git"
    git -C "$dir" init -q -b "$branch"
    git -C "$dir" add -A
    git -C "$dir" -c user.name="github-actions[bot]" \
      -c user.email="41898282+github-actions[bot]@users.noreply.github.com" commit -q "${args[@]}"
    git -C "$dir" push -q --force "$remote" "HEAD:refs/heads/$branch"
    line="$(git -C "$dir" log -1 --format='%h %s') ($(du -sh --exclude=.git "$dir" | cut -f1))"
    echo "pushed $branch: $line"
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "Pushed \`$branch\`: $line" >> "$GITHUB_STEP_SUMMARY"; fi
    ;;
  age)
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    git -C "$tmp" init -q
    if ! git -C "$tmp" fetch -q --depth=1 "$remote" "refs/heads/$branch" 2>/dev/null; then
      echo -1
      exit 0
    fi
    git -C "$tmp" show FETCH_HEAD:index.json > "$tmp/index.json"
    gen=$(field "$tmp/index.json" generated)
    echo $(( ($(date -u +%s) - $(date -u -d "$gen" +%s)) / 3600 ))
    ;;
  *)
    echo "usage: rain-branch.sh push DIR | age" >&2
    exit 2
    ;;
esac
