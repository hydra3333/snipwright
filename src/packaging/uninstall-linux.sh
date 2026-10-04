#!/usr/bin/env bash
#
# Remove Snipwright from this computer (Linux).
#
#   ./uninstall-linux.sh
#
# Undoes what install-linux.sh and install-desktop-entries.sh set up, and
# tidies away what Snipwright itself creates while it runs:
#   - the "Snipwright" and "Snipwright Watcher" application-menu entries;
#   - the .vprj and .swproj file types, their icons, and Snipwright as the
#     program that opens .swproj files;
#   - the Watcher's start-on-login entry;
#   - Snipwright's cache, and the small scratch files it leaves in the
#     temporary folder;
#   - the Python environment (.venv) the installer made;
#   - only if you say so: Quick Stream Fix working copies, and your settings.
#
# It never touches recordings, exported videos, project files or logs, and it
# leaves FFmpeg and MKVToolNix installed - they are separate programs that
# other software may use.  Every question is asked before anything is
# removed, so stopping part-way through the questions changes nothing.
#
# The menu entries, file types and start-on-login entry belong to whichever
# copy of Snipwright installed them last.  If they point at a different copy
# than this one, they are left alone, so removing an old copy cannot break
# the one you still use.
#
# A script cannot sensibly delete the folder it is running from, so the last
# step - deleting the Snipwright folder itself - is yours; it says which.
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"                    # the src/ directory
ROOT="$(cd "$SRC/.." && pwd)"                    # the project root (holds src/)
VENV="$ROOT/.venv"

APPS="$HOME/.local/share/applications"
MIME="$HOME/.local/share/mime"
ICON_ROOT="$HOME/.local/share/icons/hicolor"
ICONS="$ICON_ROOT/scalable/mimetypes"
AUTOSTART="$HOME/.config/autostart"
CONF="$HOME/.config/snipwright"
LEGACY_CONF="$HOME/.config/vrd-next"            # settings from the old name
CACHE="$HOME/.cache/snipwright"
TMP="${TMPDIR:-/tmp}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }    # bold heading
info() { printf '  %s\n' "$*"; }

# ask "question" default(y|n) - returns 0 for yes.  Anything other than a
# clear yes or no takes the default shown in capitals.
ask() {
    local prompt="$1" default="$2" hint ans
    if [ "$default" = "y" ]; then hint="[Y/n]"; else hint="[y/N]"; fi
    printf '  %s %s ' "$prompt" "$hint"
    if ! read -r ans; then ans=""; fi
    ans="$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]')"
    case "$ans" in
        y|yes) return 0 ;;
        n|no)  return 1 ;;
        *)     [ "$default" = "y" ] ;;
    esac
}

removed=()
note_removed() { removed+=("$1"); }

rm_file() {
    if [ -e "$1" ] || [ -L "$1" ]; then
        if rm -f -- "$1"; then note_removed "$1"; else info "Could not remove $1"; fi
    fi
}

# Which installation does an entry point at?  Echoes "ours", "other" or
# "none".  The entries embed absolute paths, so a match on this copy's src/
# folder is unambiguous.
owner_of() {
    local file="$1" target="$2"
    if [ ! -f "$file" ]; then echo none
    elif grep -qF -- "$SRC/$target" "$file"; then echo ours
    else echo other
    fi
}

# The folder another installation's entry points at, for telling the user.
other_path() {
    sed -n 's/^Exec=.*"\(.*\)\/\(main\|watcher\)\.py".*/\1/p' "$1" | head -n 1 \
        | sed 's#/src$##'
}

if [ "$(id -u)" -eq 0 ]; then
    info "Please run this as your normal user, not as root or with sudo."
    info "Snipwright is installed per user, so as root this would look in the"
    info "wrong home folder."
    exit 1
fi

# --- 1. what is here ------------------------------------------------------
say "Uninstall Snipwright"
info "This removes the copy of Snipwright in:"
info "    $ROOT"
info "Your recordings, exported videos, project files and logs are not touched."
echo
if ! ask "Continue?" n; then
    info "Nothing has been changed."
    exit 0
fi

# --- 2. it must not be running --------------------------------------------
# Removing the Python environment under a running editor or Watcher would
# pull it out from under them.  Matched on this copy's own paths, so another
# copy of Snipwright running elsewhere does not count.
running() {
    # Python running one of this copy's two programs - not, say, a text
    # editor that happens to have watcher.py open.
    pgrep -u "$(id -u)" -a -f 'main\.py|watcher\.py' 2>/dev/null \
        | awk -v m="$SRC/main.py" -v w="$SRC/watcher.py" '
            { cmd = $0; sub(/^[0-9]+ /, "", cmd) }
            cmd ~ /^[^ ]*python[0-9.]* / && (index(cmd, m) || index(cmd, w)) { found = 1; print }
            END { exit !found }'
}
while running >/dev/null; do
    say "Snipwright is still running"
    info "Close Snipwright, and quit the Watcher (right-click its icon in the"
    info "system tray and choose Quit).  Then press Enter to check again, or"
    printf '  type q and press Enter to stop without changing anything: '
    read -r ans || ans="q"
    case "$ans" in q|Q) info "Nothing has been changed."; exit 0 ;; esac
done

# --- 3. look before deleting anything -------------------------------------
MENU_STATE="$(owner_of "$APPS/snipwright.desktop" main.py)"
AUTO_STATE="$(owner_of "$AUTOSTART/snipwright-watcher.desktop" watcher.py)"

# Quick Stream Fix working copies, and where the logs are.  Read straight from
# the settings file rather than through Snipwright's own loader, which would
# recreate the settings folder if it were missing.  The test for "is this a
# working copy" IS Snipwright's own, so the uninstaller cannot disagree with
# the application about what is safe to delete.
PYI=""
if [ -x "$VENV/bin/python" ]; then PYI="$VENV/bin/python"
elif command -v python3 >/dev/null 2>&1; then PYI="$(command -v python3)"
fi
QSF_DIR=""; LOG_DIR=""; qsf_paths=(); qsf_bytes=0; qsf_checked=0
if [ -n "$PYI" ]; then
    while IFS=$'\t' read -r kind a b; do
        case "$kind" in
            QSFDIR) QSF_DIR="$a"; qsf_checked=1 ;;
            QSF)    qsf_bytes=$((qsf_bytes + a)); qsf_paths+=("$b") ;;
            LOGDIR) LOG_DIR="$a" ;;
        esac
    done < <("$PYI" - "$SRC" 2>/dev/null <<'PY'
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
settings, paths = {}, {}
try:
    data = json.loads((Path.home() / ".config" / "snipwright" / "config.json")
                      .read_text(encoding="utf-8"))
    settings = data.get("settings", {}) or {}
    paths = data.get("paths", {}) or {}
except Exception:
    pass
qsf = os.path.expanduser(str(settings.get("qsf_temp_dir", "") or "").strip())
if not (qsf and os.path.isdir(qsf)):
    qsf = tempfile.gettempdir()
from utils.qsf_temp import list_working_copies
print("QSFDIR\t" + qsf)
for path, size, _mtime in list_working_copies(qsf):
    print("QSF\t%d\t%s" % (size, path))
print("LOGDIR\t" + os.path.expanduser(str(paths.get("log_folder", "") or "").strip()))
PY
)
fi

delete_qsf=0
if [ "${#qsf_paths[@]}" -gt 0 ]; then
    say "Quick Stream Fix working copies"
    size="$(awk -v b="$qsf_bytes" 'BEGIN {
        if (b >= 1073741824) printf "%.1f GB", b / 1073741824
        else printf "%.0f MB", b / 1048576 }')"
    if [ "${#qsf_paths[@]}" -eq 1 ]; then what="1 working copy"
    else what="${#qsf_paths[@]} working copies"; fi
    info "Found $what in $QSF_DIR, taking $size."
    info "Snipwright makes these temporary repaired copies while editing -"
    info "not your recordings, which are where you left them:"
    for p in "${qsf_paths[@]:0:10}"; do info "    $(basename "$p")"; done
    [ "${#qsf_paths[@]}" -gt 10 ] && info "    ...and $(( ${#qsf_paths[@]} - 10 )) more"
    echo
    if ask "Delete them?" y; then delete_qsf=1; fi
elif [ "$qsf_checked" -eq 0 ]; then
    info "(Could not check for Quick Stream Fix working copies - no Python found.)"
fi

delete_settings=0
if [ -d "$CONF" ] || [ -d "$LEGACY_CONF" ]; then
    say "Your settings"
    info "Snipwright keeps your settings, output profiles, learned channel logos"
    info "and break idents, the batch queue and the Watcher's settings in:"
    [ -d "$CONF" ] && info "    $CONF"
    [ -d "$LEGACY_CONF" ] && info "    $LEGACY_CONF   (from Snipwright's earlier name)"
    info "Keep them if you might reinstall - Snipwright picks them up again, so"
    info "it will not have to relearn your channels.  Any log files in there"
    info "are kept either way."
    if [ "$MENU_STATE" = "other" ]; then
        info "Another copy of Snipwright is installed, in"
        info "    $(other_path "$APPS/snipwright.desktop")"
        info "and it uses these settings too."
    fi
    echo
    if ask "Delete your settings?" n; then delete_settings=1; fi
fi

# --- 4. remove ------------------------------------------------------------
say "Removing Snipwright"

if [ "$MENU_STATE" = "other" ]; then
    info "Leaving the menu entries and file types alone: they belong to the copy in"
    info "    $(other_path "$APPS/snipwright.desktop")"
else
    rm_file "$APPS/snipwright.desktop"
    # The Watcher's menu entry is written alongside the editor's, so it
    # follows the same ownership.
    rm_file "$APPS/snipwright-watcher.desktop"
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$APPS" >/dev/null 2>&1

    rm_file "$MIME/packages/snipwright.xml"
    command -v update-mime-database >/dev/null 2>&1 \
        && update-mime-database "$MIME" >/dev/null 2>&1

    rm_file "$ICONS/application-x-vrd-project.svg"
    rm_file "$ICONS/application-x-snipwright-project.svg"
    if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -d "$ICON_ROOT" ]; then
        gtk-update-icon-cache -f -t "$ICON_ROOT" >/dev/null 2>&1
    fi

    # The installer made Snipwright the program for .swproj files with
    # `xdg-mime default`, which writes a line to mimeapps.list.  Remove that
    # one line only - the file holds the user's other choices too.
    for list in "$HOME/.config/mimeapps.list" "$APPS/mimeapps.list"; do
        if [ -f "$list" ] && grep -qE '^application/x-snipwright-project=snipwright\.desktop;?$' "$list"; then
            if sed -i '/^application\/x-snipwright-project=snipwright\.desktop;\{0,1\}$/d' "$list"; then
                note_removed "Snipwright as the program for .swproj files ($list)"
            fi
        fi
    done
fi

if [ "$AUTO_STATE" = "other" ]; then
    info "Leaving the Watcher's start-on-login entry alone: it starts the copy in"
    info "    $(other_path "$AUTOSTART/snipwright-watcher.desktop")"
else
    rm_file "$AUTOSTART/snipwright-watcher.desktop"
fi
# Left by the Watcher under Snipwright's earlier name.  It can only start an
# installation that no longer exists, which is why the Watcher removes it too.
rm_file "$AUTOSTART/vrd-next-watcher.desktop"

if [ -d "$CACHE" ]; then
    if rm -rf -- "$CACHE"; then note_removed "$CACHE (cache)"; fi
fi

# Snipwright's own scratch files in the temporary folder: the lock files that
# stop a second editor or Watcher starting, and the Joiner's editing project.
# These are shared by every copy of Snipwright you have, so a lock is removed
# only when the process that took it has gone - deleting a live one would let
# another copy start a second Watcher beside the first.  Qt writes the owner's
# process ID on the lock file's first line.
lock_is_live() {
    local pid
    [ -f "$1" ] || return 1
    pid="$(head -n 1 "$1" 2>/dev/null)"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}
uid="$(id -u)"
lock_is_live "$TMP/snipwright-watcher-$uid.lock" || rm_file "$TMP/snipwright-watcher-$uid.lock"
if ! lock_is_live "$TMP/snipwright-editor-$uid.lock"; then
    rm_file "$TMP/snipwright-editor-$uid.lock"
    # Only an editor can be using this, so it goes when no editor is open.
    rm_file "$TMP/snipwright-joiner-edit.swproj"
fi

if [ "$delete_qsf" -eq 1 ]; then
    gone=0
    for p in "${qsf_paths[@]}"; do
        if rm -f -- "$p"; then gone=$((gone + 1)); fi
    done
    if [ "$gone" -eq 1 ]; then note_removed "1 Quick Stream Fix working copy"
    else note_removed "$gone Quick Stream Fix working copies"; fi
fi

kept_logs=""
if [ "$delete_settings" -eq 1 ]; then
    for dir in "$CONF" "$LEGACY_CONF"; do
        [ -d "$dir" ] || continue
        # Everything except log files, which are kept as promised.
        find "$dir" -mindepth 1 -depth ! -name '*.log' -delete 2>/dev/null
        if [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
            rmdir "$dir" 2>/dev/null && note_removed "$dir (settings)"
        else
            note_removed "your settings in $dir"
            kept_logs="$dir"
        fi
    done
fi

# Only a folder that really is a Python environment is removed.
if [ -d "$VENV" ] && [ -f "$VENV/pyvenv.cfg" ]; then
    if rm -rf -- "$VENV"; then note_removed "$VENV (Python environment)"
    else info "Could not remove $VENV completely."
    fi
fi

# --- 5. report ------------------------------------------------------------
say "Done"
if [ "${#removed[@]}" -eq 0 ]; then
    info "There was nothing left to remove."
else
    info "Removed:"
    for r in "${removed[@]}"; do info "    $r"; done
fi
if [ "$delete_settings" -eq 0 ] && { [ -d "$CONF" ] || [ -d "$LEGACY_CONF" ]; }; then
    echo
    info "Your settings were kept, in $CONF."
fi
if [ -n "$kept_logs" ]; then
    echo
    info "Your log files were kept, in $kept_logs."
    info "Delete that folder too if you don't want them."
fi
if [ -n "$LOG_DIR" ] && [ -d "$LOG_DIR" ]; then
    echo
    info "Logs in your chosen log folder were left as they are:"
    info "    $LOG_DIR"
fi
echo
info "Last step - delete the Snipwright folder itself:"
info "    $ROOT"
info "for example with:"
info "    rm -rf \"$ROOT\""
echo
info "FFmpeg and MKVToolNix are still installed: they are separate programs that"
info "other software may use.  Remove them with your package manager if you"
info "know nothing else needs them."
