#!/usr/bin/env bash
# Install/remove the gitlab-ci-monitor skill as symlinks to this repository's canonical copy.
# Never uses sudo, never touches credentials or agent settings, never overwrites foreign paths.
# Exit status is nonzero unless every requested action actually succeeded.
set -u

SKILL=gitlab-ci-monitor
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CANON="$HERE/skills/$SKILL"
DRY=0; ACTION=install; AGENTS="claude,codex"

usage() {
  cat <<USAGE
Usage: install.sh [--dry-run] [--uninstall] [--agents LIST]
  --agents LIST  comma list of: claude, codex, opencode   (default: claude,codex)
  --dry-run      preflight and show what would happen; change nothing
  --uninstall    remove only symlinks whose target is exactly $CANON

Targets (under \$HOME): claude ~/.claude/skills/$SKILL ; codex ~/.agents/skills/$SKILL
OpenCode discovers both of those directories itself (~/.agents/skills takes precedence over
~/.claude/skills), so it never gets a third copy. 'opencode' ensures the effective definition is
ours: it refuses if a foreign ~/.agents/skills/$SKILL would override ours, and otherwise adds the
~/.agents/skills link when no canonical link exists yet. Those links are SHARED with Codex/Claude, so
'--uninstall --agents opencode' removes nothing.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --uninstall) ACTION=uninstall ;;
    --agents) shift; AGENTS="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ -f "$CANON/SKILL.md" ] || { echo "canonical skill not found: $CANON" >&2; exit 1; }
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || { echo "HOME is not set to a directory" >&2; exit 1; }
for a in ${AGENTS//,/ }; do
  case "$a" in claude|codex|opencode) ;; *) echo "unknown agent: $a" >&2; exit 2 ;; esac
done
[ -n "$AGENTS" ] || { echo "--agents needs a value" >&2; exit 2; }
has() { case ",$AGENTS," in *,"$1",*) return 0 ;; esac; return 1; }

CLAUDE_DIR="$HOME/.claude/skills"; AGENTS_DIR="$HOME/.agents/skills"

# state DIR -> ok | absent | conflict:<reason>
state() {
  local t="$1/$SKILL"
  if [ -L "$t" ]; then
    local dest; dest="$(readlink "$t")"
    if [ "$dest" = "$CANON" ]; then
      [ -f "$t/SKILL.md" ] && echo ok || echo "conflict:link to canonical path is broken"
    elif [ -e "$t" ]; then echo "conflict:foreign symlink -> $dest"
    else echo "conflict:foreign broken symlink -> $dest"; fi
  elif [ -e "$t" ]; then echo "conflict:existing non-symlink path"
  else echo absent; fi
}

# parent_problem DIR -> prints a reason if DIR (or its nearest existing ancestor) is unusable
parent_problem() {
  local p="$1"
  while [ ! -e "$p" ] && [ ! -L "$p" ]; do p="$(dirname "$p")"; done
  if [ -L "$p" ] && [ ! -d "$p" ]; then echo "broken symlink at $p"; return; fi
  if [ ! -d "$p" ]; then echo "$p exists but is not a directory"; return; fi
  [ -w "$p" ] || echo "$p is not writable"
}

# ---- plan ---------------------------------------------------------------
want=()   # directories that must contain our link
has claude && want+=("$CLAUDE_DIR")
has codex && want+=("$AGENTS_DIR")
if has opencode && [ "$ACTION" = install ]; then
  sa="$(state "$AGENTS_DIR")"; sc="$(state "$CLAUDE_DIR")"
  case "$sa" in
    conflict:*) echo "CONFLICT $AGENTS_DIR/$SKILL (${sa#conflict:}): OpenCode would load that foreign definition; refusing" >&2; exit 1 ;;
    ok) ;;
    absent) if [ "$sc" != ok ] && ! has claude && ! has codex; then
        want+=("$AGENTS_DIR")
        [ "$sc" = absent ] || echo "note: ~/.claude/skills/$SKILL is foreign; the ~/.agents/skills link takes precedence in OpenCode" >&2
      fi ;;
  esac
fi

fail=0
if [ "$ACTION" = install ]; then
  for d in "${want[@]}"; do
    s="$(state "$d")"
    case "$s" in conflict:*) echo "CONFLICT $d/$SKILL (${s#conflict:}); refusing" >&2; fail=1 ;; esac
    if [ "$s" = absent ]; then
      pp="$(parent_problem "$d")"
      [ -z "$pp" ] || { echo "CONFLICT cannot create $d: $pp" >&2; fail=1; }
    fi
  done
  [ "$fail" -eq 0 ] || { echo "no changes made" >&2; exit 1; }
fi

if [ "$ACTION" = uninstall ] && [ "${#want[@]}" -eq 0 ]; then
  echo "nothing removed: the OpenCode link is shared with Claude/Codex; use --uninstall --agents claude,codex"
  exit 0
fi

# ---- execute ------------------------------------------------------------
created=()
for d in "${want[@]}"; do
  t="$d/$SKILL"
  if [ "$ACTION" = install ]; then
    s="$(state "$d")"
    if [ "$s" = ok ]; then echo "already installed: $t -> $CANON"; continue; fi
    if [ "$DRY" -eq 1 ]; then echo "would link: $t -> $CANON"; continue; fi
    if mkdir -p "$d" && ln -s -T "$CANON" "$t" && [ "$(readlink "$t")" = "$CANON" ] && [ -f "$t/SKILL.md" ]; then
      echo "installed: $t -> $CANON"; created+=("$t")
    else
      echo "FAILED to install $t" >&2; fail=1; break
    fi
  else
    if [ -L "$t" ] && [ "$(readlink "$t")" = "$CANON" ]; then
      if [ "$DRY" -eq 1 ]; then echo "would remove: $t"
      elif rm -- "$t" && [ ! -L "$t" ]; then echo "removed: $t"
      else echo "FAILED to remove $t" >&2; fail=1; fi
    elif [ -e "$t" ] || [ -L "$t" ]; then
      echo "skipped (not an installer-created link, left untouched): $t" >&2
    else
      echo "not installed: $t"
    fi
  fi
done

if [ "$fail" -ne 0 ]; then
  for t in ${created[@]+"${created[@]}"}; do rm -- "$t" 2>/dev/null && echo "rolled back: $t" >&2; done
  echo "installer finished with errors" >&2
  exit 1
fi
exit 0
