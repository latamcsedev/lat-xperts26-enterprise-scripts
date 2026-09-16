#!/usr/bin/env bash
#
# sync_docs.sh — Mirror the docs/ tree from the main enterprise repo into the two
# published destination repos. By default it only copies files; committing and
# pushing to GitHub are opt-in.
#
#   SOURCE : lat-xperts26-enterprise/docs/            -> destination repo ROOT
#   DEST 1 : enterprise-protection-2026-nola/
#   DEST 2 : enterprise-protection-2026/
#
# Mirror mode: each destination's content is made to match docs/ exactly, so
# files removed from docs/ are deleted from the destination too. Each repo's own
# infrastructure (.git, .github, .gitignore, .gitattributes) is preserved and
# .DS_Store files are excluded.
#
# Usage:
#   ./sync_docs.sh                    # sync files only (default; no git actions)
#   ./sync_docs.sh --git-commit-only  # sync, then git add + commit (no push)
#   ./sync_docs.sh --git-push         # sync, then git add + commit + push
#   ./sync_docs.sh --dry-run          # show what rsync would change; no writes
#
set -euo pipefail

# --- Paths ------------------------------------------------------------------
BASE="/Users/jmartin/GitHub/XPerts26"
SOURCE="$BASE/lat-xperts26-enterprise/docs/"          # trailing slash: copy contents
DESTS=(
  "$BASE/enterprise-protection-2026-nola"
  "$BASE/enterprise-protection-2026"
)

# Paths that live in the destination repos and must never be touched by the mirror.
EXCLUDES=(
  ".git/"
  ".github/"
  ".gitignore"
  ".gitattributes"
  ".DS_Store"
  "nosync/"
  "theme/"
)

# --- Flags ------------------------------------------------------------------
# Default: sync files only. Git actions are opt-in.
DRY_RUN=0
COMMIT=0   # set by --git-commit-only and --git-push
PUSH=0     # set by --git-push
for arg in "$@"; do
  case "$arg" in
    --dry-run)         DRY_RUN=1 ;;
    --git-commit-only) COMMIT=1 ;;
    --git-push)        COMMIT=1; PUSH=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --- Sanity checks ----------------------------------------------------------
if [[ ! -d "$SOURCE" ]]; then
  echo "ERROR: source docs/ not found at $SOURCE" >&2
  exit 1
fi

RSYNC_OPTS=(-a --delete)
for e in "${EXCLUDES[@]}"; do
  RSYNC_OPTS+=(--exclude "$e")
done
if [[ "$DRY_RUN" -eq 1 ]]; then
  RSYNC_OPTS+=(--dry-run --itemize-changes)
fi

TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
COMMIT_MSG="Sync docs from lat-xperts26-enterprise — $TIMESTAMP"

# --- Sync each destination --------------------------------------------------
for DEST in "${DESTS[@]}"; do
  NAME="$(basename "$DEST")"
  echo "=============================================================="
  echo ">> $NAME"
  echo "=============================================================="

  if [[ ! -d "$DEST" ]]; then
    echo "ERROR: $DEST does not exist — skipping." >&2
    continue
  fi

  echo "-- rsync docs/ -> $NAME/"
  rsync "${RSYNC_OPTS[@]}" "$SOURCE" "$DEST/"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "-- dry-run: no writes for $NAME"
    echo
    continue
  fi

  # Default: sync only. Commit/push are opt-in via flags.
  if [[ "$COMMIT" -eq 0 ]]; then
    echo "-- sync only; skipping git (use --git-commit-only or --git-push)."
    echo
    continue
  fi

  if [[ ! -d "$DEST/.git" ]]; then
    echo "ERROR: $DEST is not a git repository — cannot commit; skipping git." >&2
    echo
    continue
  fi

  if [[ -z "$(git -C "$DEST" status --porcelain)" ]]; then
    echo "-- no changes in $NAME; nothing to commit."
    echo
    continue
  fi

  echo "-- committing changes in $NAME"
  git -C "$DEST" add -A
  git -C "$DEST" commit -m "$COMMIT_MSG"

  if [[ "$PUSH" -eq 1 ]]; then
    BRANCH="$(git -C "$DEST" rev-parse --abbrev-ref HEAD)"
    echo "-- pushing $NAME ($BRANCH) to origin"
    git -C "$DEST" push origin "$BRANCH"
  else
    echo "-- committed but not pushed (use --git-push to push)."
  fi
  echo
done

echo "Done."
