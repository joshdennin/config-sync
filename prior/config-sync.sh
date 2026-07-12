#!/usr/bin/env bash
# config-sync.sh — sync dotfile configs from git repos into place.
# Bash port of config-sync.lua. See config-plan.md.
#
# Failure philosophy: do only the validation needed to branch correctly. Any
# other unexpected condition is allowed to fail; it is surfaced to the user and
# (for per-entry work) the run continues.
#
# Config format (a sourced bash file, default ~/.config/config-sync/config.sh):
#   entry nvim https://example/nvim.git ~/.config/nvim direct
#   entry tmux https://example/tmux.git ~/.tmux.conf   symlink tmux.conf

set -u

STAGING_ROOT="${HOME}/.local/share/config-sync"

# ------------------------------------------------------------------ helpers

# Expand a leading ~ to $HOME (config values may be quoted, suppressing the
# shell's own tilde expansion).
expand() {
  case "$1" in
    "~")    printf '%s' "$HOME" ;;
    "~/"*)  printf '%s' "$HOME/${1#\~/}" ;;
    *)      printf '%s' "$1" ;;
  esac
}

# Compare repo URLs ignoring surrounding whitespace and a trailing ".git".
norm_url() {
  local u="$1"
  u="$(printf '%s' "$u" | tr -d '[:space:]')"
  printf '%s' "${u%.git}"
}

git_origin() { git -C "$1" remote get-url origin 2>/dev/null; }

# Rename a path aside; echoes the backup path on success.
backup() {
  local path="$1" dest
  dest="${path}.bak.$(date +%Y%m%d-%H%M%S)"
  if mv "$path" "$dest"; then printf '%s' "$dest"; return 0; fi
  return 1
}

# Yes/no prompt with a stated default applied on empty input / EOF.
prompt_yn() {
  local question="$1" default="$2" suffix ans
  if [ "$default" = Y ]; then suffix=" [Y/n] "; else suffix=" [y/N] "; fi
  printf '%s%s' "$question" "$suffix"
  read -r ans || ans=""
  ans="$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [ -z "$ans" ] && { [ "$default" = Y ]; return; }
  [ "$ans" = y ] || [ "$ans" = yes ]
}

# ------------------------------------------------------------ result tracking

declare -a RESULTS=()   # each element: "outcome|program|detail"

record() { RESULTS+=("$1|$2|$3"); }   # outcome program detail

# Surface an error and record a failure; the run continues.
fail() {   # program operation target msg
  printf '[%s] %s failed on %s: %s\n' "$1" "$2" "$3" "$4" >&2
  record failed "$1" "$2 $3"
}

# ---------------------------------------------------------------- bootstrap

# Arch-only: suggest the exact pacman command; never auto-install.
preflight() {
  local dep
  for dep in git; do
    if ! command -v "$dep" >/dev/null 2>&1; then
      printf 'Missing dependency: %s\nInstall it with:\n  sudo pacman -S %s\n' \
        "$dep" "$dep" >&2
      return 1
    fi
  done
}

# ------------------------------------------------------------- known paths

known_paths() {   # program -> newline-separated expanded paths, nonzero if none
  case "$1" in
    nvim) printf '%s\n' "$HOME/.config/nvim" "$HOME/.vim" "$HOME/.vimrc" ;;
    tmux) printf '%s\n' "$HOME/.tmux.conf" "$HOME/.config/tmux" ;;
    *)    return 1 ;;
  esac
}

probe_known_paths() {   # program dest ; return 0 = proceed
  local program="$1" dest="$2" kp_out line p
  local -a kp=() found=()
  if ! kp_out="$(known_paths "$program")"; then
    printf '[%s] no known-paths entry; skipping probe.\n' "$program"
    return 0
  fi
  while IFS= read -r line; do kp+=("$line"); done <<< "$kp_out"
  for p in "${kp[@]}"; do
    [ "$p" != "$dest" ] && [ -e "$p" ] && found+=("$p")
  done
  [ "${#found[@]}" -eq 0 ] && return 0
  printf '[%s] existing config found at:\n' "$program"
  for p in "${found[@]}"; do printf '  %s\n' "$p"; done
  prompt_yn "[$program] proceed with this entry anyway?" N
}

# -------------------------------------------------------------- sync models

# mode = direct: the repo IS the config; clone into dest.
sync_direct() {   # program repo dest
  local program="$1" repo="$2" dest="$3" bak

  if [ ! -e "$dest" ]; then
    if ! prompt_yn "[$program] $dest does not exist. Create and clone?" Y; then
      record skipped "$program" "create declined"; return
    fi
    if git clone "$repo" "$dest"; then record synced "$program" "cloned to $dest"
    else fail "$program" "git clone" "$dest" "clone failed"; fi
    return
  fi

  if [ -d "$dest" ] && [ -z "$(ls -A "$dest" 2>/dev/null)" ]; then
    if git clone "$repo" "$dest"; then record synced "$program" "cloned into empty $dest"
    else fail "$program" "git clone" "$dest" "clone failed"; fi
    return
  fi

  if [ -d "$dest" ] && [ "$(norm_url "$(git_origin "$dest")")" = "$(norm_url "$repo")" ]; then
    if git -C "$dest" pull; then record pulled "$program" "pulled $dest"
    else fail "$program" "git pull" "$dest" "pull failed"; fi
    return
  fi

  if ! prompt_yn "[$program] $dest has other content. Back up and overwrite?" N; then
    record skipped "$program" "overwrite declined"; return
  fi
  if ! bak="$(backup "$dest")"; then fail "$program" backup "$dest" "rename failed"; return; fi
  printf '[%s] backed up to %s\n' "$program" "$bak"
  if git clone "$repo" "$dest"; then record synced "$program" "cloned to $dest (backed up old)"
  else fail "$program" "git clone" "$dest" "clone failed"; fi
}

# mode = symlink: clone into staging, then link into place.
sync_symlink() {   # program repo dest source
  local program="$1" repo="$2" dest="$3" source="$4"
  local staging="$STAGING_ROOT/$program" target parent bak

  if [ ! -e "$staging" ]; then
    if ! git clone "$repo" "$staging"; then
      fail "$program" "git clone" "$staging" "clone failed"; return
    fi
  elif [ "$(norm_url "$(git_origin "$staging")")" = "$(norm_url "$repo")" ]; then
    if ! git -C "$staging" pull; then
      fail "$program" "git pull" "$staging" "pull failed"; return
    fi
  else
    fail "$program" staging "$staging" "exists but origin does not match repo"; return
  fi

  if [ -z "$source" ] || [ "$source" = all ]; then target="$staging"
  else target="$staging/$source"; fi

  if [ -L "$dest" ]; then
    if [ "$(readlink "$dest")" = "$target" ]; then
      record unchanged "$program" "symlink already correct"; return
    fi
    if ! rm "$dest"; then fail "$program" "remove symlink" "$dest" "rm failed"; return; fi
    if ln -s "$target" "$dest"; then record synced "$program" "symlink re-created $dest -> $target"
    else fail "$program" "ln -s" "$dest" "symlink failed"; fi
    return
  fi

  if [ ! -e "$dest" ]; then
    parent="${dest%/*}"
    if [ -n "$parent" ] && [ "$parent" != "$dest" ] && ! mkdir -p "$parent"; then
      fail "$program" "mkdir -p" "$parent" "mkdir failed"; return
    fi
    if ln -s "$target" "$dest"; then record synced "$program" "symlink created $dest -> $target"
    else fail "$program" "ln -s" "$dest" "symlink failed"; fi
    return
  fi

  if [ -d "$dest" ] && [ -z "$(ls -A "$dest" 2>/dev/null)" ]; then
    if ! rmdir "$dest"; then fail "$program" "remove empty dir" "$dest" "rmdir failed"; return; fi
    if ln -s "$target" "$dest"; then record synced "$program" "symlink created $dest -> $target"
    else fail "$program" "ln -s" "$dest" "symlink failed"; fi
    return
  fi

  if ! prompt_yn "[$program] $dest has other content. Back up and replace with symlink?" N; then
    record skipped "$program" "overwrite declined"; return
  fi
  if ! bak="$(backup "$dest")"; then fail "$program" backup "$dest" "rename failed"; return; fi
  printf '[%s] backed up to %s\n' "$program" "$bak"
  if ln -s "$target" "$dest"; then record synced "$program" "symlink created $dest -> $target (backed up old)"
  else fail "$program" "ln -s" "$dest" "symlink failed"; fi
}

# -------------------------------------------------------------- per-entry flow

process_entry() {   # program repo dest_raw mode source
  local program="$1" repo="$2" dest_raw="$3" mode="$4" source="$5" dest

  if [ "$mode" != direct ] && [ "$mode" != symlink ]; then
    fail "$program" validate "$program" "invalid mode: $mode"; return
  fi

  dest="$(expand "$dest_raw")"

  if ! probe_known_paths "$program" "$dest"; then
    record skipped "$program" "known-path declined"; return
  fi

  if [ "$mode" = direct ]; then sync_direct "$program" "$repo" "$dest"
  else sync_symlink "$program" "$repo" "$dest" "$source"; fi
}

# ------------------------------------------------------------------ summary

print_summary() {
  local order=(synced pulled unchanged skipped failed)
  local o r outcome rest program detail flagged=0
  printf '\n=== Summary ===\n'
  for o in "${order[@]}"; do
    for r in "${RESULTS[@]:-}"; do
      [ -n "$r" ] || continue
      outcome="${r%%|*}"; rest="${r#*|}"; program="${rest%%|*}"; detail="${rest#*|}"
      if [ "$outcome" = "$o" ]; then
        if [ -n "$detail" ]; then printf '  %-10s %s  (%s)\n' "$outcome" "$program" "$detail"
        else printf '  %-10s %s\n' "$outcome" "$program"; fi
      fi
    done
  done
  for r in "${RESULTS[@]:-}"; do
    [ -n "$r" ] || continue
    outcome="${r%%|*}"
    { [ "$outcome" = skipped ] || [ "$outcome" = failed ]; } && flagged=$((flagged + 1))
  done
  if [ "$flagged" -gt 0 ]; then
    if [ "$flagged" -eq 1 ]; then
      printf '\n%d entry needs attention (skipped/failed above).\n' "$flagged"
    else
      printf '\n%d entries need attention (skipped/failed above).\n' "$flagged"
    fi
  fi
}

# --------------------------------------------------------------------- main

# Config entry collector — the sourced config calls this once per entry.
declare -a E_PROGRAM=() E_REPO=() E_DEST=() E_MODE=() E_SOURCE=()
entry() {   # program repo dest mode [source]
  E_PROGRAM+=("$1"); E_REPO+=("$2"); E_DEST+=("$3"); E_MODE+=("$4"); E_SOURCE+=("${5:-}")
}

main() {
  preflight || exit 1

  local config_path="${1:-$HOME/.config/config-sync/config.sh}"
  if [ ! -f "$config_path" ]; then
    printf 'Failed to load config (%s): no such file\n' "$config_path" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  if ! source "$config_path"; then
    printf 'Failed to load config (%s): parse error\n' "$config_path" >&2
    exit 1
  fi

  local i
  for i in "${!E_PROGRAM[@]}"; do
    process_entry "${E_PROGRAM[$i]}" "${E_REPO[$i]}" "${E_DEST[$i]}" \
      "${E_MODE[$i]}" "${E_SOURCE[$i]}"
  done

  print_summary
}

main "$@"
