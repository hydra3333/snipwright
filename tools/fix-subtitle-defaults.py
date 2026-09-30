#!/usr/bin/env python3
"""Fix subtitle defaults - a one-off repair for Snipwright's .mkv exports.

Up to Snipwright 2.8.0, every recording exported to .mkv had its subtitle
track marked as a DEFAULT track, which tells a media player to switch the
subtitles on. This tool works through a folder and all its subfolders and
switches that setting off, in place. Nothing is re-encoded or remuxed: only
the one setting in each file's header changes, so it is quick even over a
network.

It is deliberately careful about what it touches, because a folder may hold
more than Snipwright's exports:

  * Only a subtitle track that is currently a default track is changed.
  * Only when the subtitles are in the SAME language as the audio. A
    recording with English audio and English subtitles has them for the hard
    of hearing, and they should start off. A French film with English
    subtitles has them there to be read, and they are left alone.
  * A track marked "forced" is left alone - that flag exists for subtitles
    meant to show whatever the player is set to.
  * An unlabelled language on either side is left alone rather than guessed.
  * Snipwright's working files (anything ending .tmp.mkv) are skipped,
    because an export may still be writing them.

Nothing is changed until you have seen the preview and pressed Apply.

It needs MKVToolNix (mkvmerge and mkvpropedit), which Snipwright already
uses. It looks for them where Snipwright was told to find them, then on the
system path, then in the usual Windows install folder.

This is a stand-alone tool: Snipwright does not use or import it. Run it with
the launcher beside it (fix-subtitle-defaults.bat on Windows,
fix-subtitle-defaults.sh on Linux), or from a terminal:

    python3 fix-subtitle-defaults.py                  # the window
    python3 fix-subtitle-defaults.py --dry-run DIR    # report only, no window
    python3 fix-subtitle-defaults.py --apply DIR      # make the changes
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform.startswith("win")
EXE = ".exe" if WINDOWS else ""

# Snipwright keeps its settings here on every platform.
SNIPWRIGHT_CONFIG = Path.home() / ".config" / "snipwright" / "config.json"

# Where the MKVToolNix installer puts it on Windows, if nothing else finds it.
WINDOWS_MKVTOOLNIX = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "MKVToolNix",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "MKVToolNix",
)

# Keep a console window from flashing up for every file on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------
# Finding MKVToolNix
# --------------------------------------------------------------------------

def _pair_in(folder):
    """mkvmerge and mkvpropedit in `folder`, or None if either is missing."""
    if not folder:
        return None
    folder = Path(folder)
    merge = folder / ("mkvmerge" + EXE)
    prop = folder / ("mkvpropedit" + EXE)
    if merge.is_file() and prop.is_file():
        return str(merge), str(prop)
    return None


def find_mkvtoolnix():
    """(mkvmerge, mkvpropedit, where-found) - or (None, None, reason)."""
    # 1. Wherever Snipwright was pointed at in its own Settings.
    try:
        paths = json.loads(SNIPWRIGHT_CONFIG.read_text(encoding="utf-8"))
        configured = (paths.get("paths", {}).get("mkvmerge_binary") or "")
        if configured.strip():
            found = _pair_in(Path(configured.strip()).parent)
            if found:
                return found[0], found[1], "from Snipwright's settings"
    except Exception:
        pass
    # 2. The system path.
    merge = shutil.which("mkvmerge")
    prop = shutil.which("mkvpropedit")
    if merge and prop:
        return merge, prop, "on the system path"
    # 3. The usual Windows install folders.
    if WINDOWS:
        for folder in WINDOWS_MKVTOOLNIX:
            found = _pair_in(folder)
            if found:
                return found[0], found[1], "in " + str(folder)
    return None, None, "not found"


# --------------------------------------------------------------------------
# Deciding what to change
# --------------------------------------------------------------------------

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=_NO_WINDOW)


def identify(mkvmerge, path):
    """mkvmerge's description of `path`, or None if it is not a Matroska
    file mkvmerge can read."""
    try:
        info = json.loads(_run([mkvmerge, "-J", str(path)]).stdout)
    except Exception:
        return None
    if not info.get("container", {}).get("recognized"):
        return None
    return info


def _language(track):
    lang = (track.get("properties", {}).get("language") or "").strip().lower()
    return "" if lang in ("", "und", "mis", "mul", "zxx") else lang


def decide(info):
    """What to do with each subtitle track in one file.

    Returns a list of (subtitle_number, change, reason), where
    subtitle_number counts subtitle tracks from 1 as mkvpropedit's
    "track:sN" selector does, and `change` says whether to switch its
    default flag off.  Tracks that are not default are not listed: there is
    nothing to do to them.
    """
    tracks = info.get("tracks", [])
    audio = [t for t in tracks if t.get("type") == "audio"]
    reference = next(
        (t for t in audio if t.get("properties", {}).get("default_track")),
        audio[0] if audio else None,
    )
    audio_lang = _language(reference) if reference else ""

    out = []
    subs = [t for t in tracks if t.get("type") == "subtitles"]
    for number, track in enumerate(subs, start=1):
        props = track.get("properties", {})
        if not props.get("default_track"):
            continue
        sub_lang = _language(track)
        if props.get("forced_track"):
            out.append((number, False, "marked forced - meant to show"))
        elif not reference:
            out.append((number, False, "no audio track to compare with"))
        elif not audio_lang:
            out.append((number, False, "audio language not labelled"))
        elif not sub_lang:
            out.append((number, False, "subtitle language not labelled"))
        elif sub_lang != audio_lang:
            out.append((number, False,
                        "subtitles in %s, audio in %s - probably meant to be "
                        "read" % (sub_lang, audio_lang)))
        else:
            out.append((number, True, "same language as the audio (%s)"
                        % sub_lang))
    return out


def mkv_files(folder):
    """Every .mkv beneath `folder`, Snipwright's working files excepted."""
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if name.lower().endswith(".mkv") and \
                    not name.lower().endswith(".tmp.mkv"):
                yield Path(root) / name


def scan_file(mkvmerge, path):
    """One file's verdict: ("change" | "leave" | "fine" | "unreadable",
    [track decisions])."""
    info = identify(mkvmerge, path)
    if info is None:
        return "unreadable", []
    decisions = decide(info)
    if any(change for _n, change, _r in decisions):
        return "change", decisions
    if decisions:
        return "leave", decisions
    return "fine", []


def apply_file(mkvpropedit, path, decisions):
    """Switch off the default flag on every track marked for it."""
    args = [mkvpropedit, str(path)]
    for number, change, _reason in decisions:
        if change:
            args += ["--edit", "track:s%d" % number, "--set", "flag-default=0"]
    if len(args) == 2:
        return True, ""
    result = _run(args)
    # mkvpropedit: 0 = done, 1 = done with warnings, 2 = error.
    return result.returncode < 2, (result.stdout or result.stderr).strip()


# --------------------------------------------------------------------------
# Without a window
# --------------------------------------------------------------------------

def run_cli(mode, folders):
    mkvmerge, mkvpropedit, where = find_mkvtoolnix()
    if not mkvmerge:
        print("MKVToolNix (mkvmerge and mkvpropedit) could not be found.")
        return 2
    counts = {"change": 0, "leave": 0, "fine": 0, "unreadable": 0,
              "failed": 0}
    for folder in folders:
        for path in mkv_files(folder):
            verdict, decisions = scan_file(mkvmerge, path)
            counts[verdict] += 1
            if verdict == "unreadable":
                print("could not read:   %s" % path)
            elif verdict == "leave":
                reasons = "; ".join(r for _n, _c, r in decisions)
                print("leaving alone:    %s  (%s)" % (path, reasons))
            elif verdict == "change":
                if mode == "dry-run":
                    print("would switch off: %s" % path)
                else:
                    ok, message = apply_file(mkvpropedit, path, decisions)
                    if ok:
                        print("switched off:     %s" % path)
                    else:
                        counts["failed"] += 1
                        print("FAILED:           %s  %s" % (path, message))
    verb = "would change" if mode == "dry-run" else "changed"
    print("--- %d %s, %d left alone on purpose, %d already fine, "
          "%d unreadable%s" % (
              counts["change"] - counts["failed"], verb, counts["leave"],
              counts["fine"], counts["unreadable"],
              ", %d failed" % counts["failed"] if counts["failed"] else ""))
    return 1 if counts["failed"] else 0


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

def run_gui():
    from PySide6.QtCore import QObject, QThread, Signal, Qt
    from PySide6.QtWidgets import (
        QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
        QMessageBox, QProgressBar, QPushButton, QSizePolicy, QTreeWidget,
        QTreeWidgetItem, QVBoxLayout, QWidget,
    )

    class Worker(QObject):
        """Scans, or applies, off the interface thread."""
        progress = Signal(int, int, str)
        result = Signal(str, str, list)      # verdict, path, decisions
        finished = Signal()

        def __init__(self, mode, folder, mkvmerge, mkvpropedit, plan=None):
            super().__init__()
            self.mode, self.folder = mode, folder
            self.mkvmerge, self.mkvpropedit = mkvmerge, mkvpropedit
            self.plan = plan or []
            self.stop = False

        def run(self):
            if self.mode == "scan":
                paths = list(mkv_files(self.folder))
                for i, path in enumerate(paths, start=1):
                    if self.stop:
                        break
                    self.progress.emit(i, len(paths), str(path))
                    verdict, decisions = scan_file(self.mkvmerge, path)
                    self.result.emit(verdict, str(path), decisions)
            else:
                for i, (path, decisions) in enumerate(self.plan, start=1):
                    if self.stop:
                        break
                    self.progress.emit(i, len(self.plan), path)
                    ok, message = apply_file(self.mkvpropedit, path,
                                             decisions)
                    self.result.emit("done" if ok else "failed", path,
                                     [message])
            self.finished.emit()

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Fix subtitle defaults - for Snipwright "
                                "exports")
            self.resize(900, 620)
            self.plan = []
            self.thread = None
            self.worker = None

            intro = QLabel(
                "Snipwright up to 2.8.0 marked the subtitles in its .mkv "
                "exports as a default track, so players switch them on. "
                "This switches that off in every .mkv in a folder and its "
                "subfolders - only where the subtitles are in the same "
                "language as the audio. Nothing is changed until you have "
                "seen the list and pressed Apply.")
            intro.setWordWrap(True)

            self.folder = QLineEdit()
            self.folder.setPlaceholderText("The folder your exports are in")
            browse = QPushButton("Browse…")
            browse.clicked.connect(self.choose_folder)

            merge, prop, where = find_mkvtoolnix()
            self.mkvmerge, self.mkvpropedit = merge, prop
            self.tools = QLabel()
            tools_browse = QPushButton("Find MKVToolNix…")
            tools_browse.clicked.connect(self.choose_tools)
            self.show_tools(where)

            self.scan_button = QPushButton("Scan")
            self.scan_button.clicked.connect(self.scan)
            self.apply_button = QPushButton("Apply")
            self.apply_button.setEnabled(False)
            self.apply_button.clicked.connect(self.apply)
            self.stop_button = QPushButton("Stop")
            self.stop_button.setEnabled(False)
            self.stop_button.clicked.connect(self.request_stop)

            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["File", "Why"])
            self.tree.setColumnWidth(0, 560)
            self.groups = {}

            self.bar = QProgressBar()
            self.status = QLabel("Choose a folder, then Scan.")
            # A label asks to be as wide as its text, and this one shows the
            # path of each file as it goes - a deep enough folder made the
            # whole window grow wider mid-scan. Let it be narrower than its
            # text, and shorten long paths in the middle to fit.
            self.status.setSizePolicy(QSizePolicy.Ignored,
                                      QSizePolicy.Preferred)

            row = QHBoxLayout()
            row.addWidget(QLabel("Folder:"))
            row.addWidget(self.folder, 1)
            row.addWidget(browse)
            tools_row = QHBoxLayout()
            tools_row.addWidget(self.tools, 1)
            tools_row.addWidget(tools_browse)
            buttons = QHBoxLayout()
            buttons.addWidget(self.scan_button)
            buttons.addWidget(self.apply_button)
            buttons.addWidget(self.stop_button)
            buttons.addStretch(1)

            layout = QVBoxLayout(self)
            layout.addWidget(intro)
            layout.addLayout(row)
            layout.addLayout(tools_row)
            layout.addLayout(buttons)
            layout.addWidget(self.tree, 1)
            layout.addWidget(self.bar)
            layout.addWidget(self.status)

        # -- MKVToolNix and the folder ------------------------------------

        def show_tools(self, where):
            if self.mkvmerge:
                self.tools.setText("MKVToolNix: found %s (%s)"
                                   % (where, Path(self.mkvmerge).parent))
            else:
                self.tools.setText("MKVToolNix: NOT FOUND - use Find "
                                   "MKVToolNix to point at its folder.")

        def choose_tools(self):
            folder = QFileDialog.getExistingDirectory(
                self, "The folder MKVToolNix is installed in")
            if not folder:
                return
            found = _pair_in(folder)
            if not found:
                QMessageBox.warning(
                    self, "Not MKVToolNix",
                    "That folder does not contain both mkvmerge and "
                    "mkvpropedit.")
                return
            self.mkvmerge, self.mkvpropedit = found
            self.show_tools("where you chose")

        def choose_folder(self):
            folder = QFileDialog.getExistingDirectory(
                self, "The folder your exports are in", self.folder.text())
            if folder:
                self.folder.setText(folder)

        # -- Running a job ------------------------------------------------

        def busy(self, running):
            self.scan_button.setEnabled(not running)
            self.stop_button.setEnabled(running)
            self.apply_button.setEnabled(not running and bool(self.plan))

        def start(self, worker):
            self.worker = worker
            self.thread = QThread()
            worker.moveToThread(self.thread)
            self.thread.started.connect(worker.run)
            worker.progress.connect(self.on_progress)
            worker.result.connect(self.on_result)
            worker.finished.connect(self.thread.quit)
            worker.finished.connect(self.on_finished)
            self.busy(True)
            self.thread.start()

        def request_stop(self):
            if self.worker:
                self.worker.stop = True
                self.status.setText("Stopping after the current file…")

        def on_progress(self, i, total, path):
            self.bar.setMaximum(max(total, 1))
            self.bar.setValue(i)
            text = "%d of %d: %s" % (i, total, path)
            self.status.setText(self.status.fontMetrics().elidedText(
                text, Qt.ElideMiddle, max(self.status.width(), 100)))
            self.status.setToolTip(path)

        def group(self, key, title):
            if key not in self.groups:
                item = QTreeWidgetItem([title, ""])
                self.tree.addTopLevelItem(item)
                self.groups[key] = [item, title, 0]
            entry = self.groups[key]
            entry[2] += 1
            entry[0].setText(0, "%s (%d)" % (entry[1], entry[2]))
            return entry[0]

        # -- Scan -----------------------------------------------------------

        def scan(self):
            folder = self.folder.text().strip()
            if not folder or not os.path.isdir(folder):
                QMessageBox.warning(self, "No folder",
                                    "Choose the folder your exports are in.")
                return
            if not self.mkvmerge:
                QMessageBox.warning(
                    self, "MKVToolNix not found",
                    "Point at MKVToolNix with Find MKVToolNix first.")
                return
            self.tree.clear()
            self.groups = {}
            self.plan = []
            self.mode = "scan"
            self.start(Worker("scan", folder, self.mkvmerge,
                              self.mkvpropedit))

        def on_result(self, verdict, path, decisions):
            if self.mode == "scan":
                if verdict == "change":
                    self.plan.append((path, decisions))
                    parent = self.group("change", "Will switch off")
                    why = "; ".join(r for _n, c, r in decisions if c)
                elif verdict == "leave":
                    parent = self.group("leave", "Leaving alone on purpose")
                    why = "; ".join(r for _n, _c, r in decisions)
                elif verdict == "unreadable":
                    parent = self.group("unreadable", "Could not read")
                    why = "not a Matroska file MKVToolNix can read"
                else:
                    self.group("fine", "Already fine (not listed)")
                    return
            else:
                if verdict == "done":
                    self.group("done", "Switched off")
                    return
                parent = self.group("failed", "FAILED")
                why = decisions[0] if decisions else ""
            QTreeWidgetItem(parent, [path, why])

        def on_finished(self):
            self.busy(False)
            if self.mode == "scan":
                n = len(self.plan)
                self.status.setText(
                    "Scan finished: %d file(s) would be changed. Nothing has "
                    "been changed yet." % n if n else
                    "Scan finished: nothing needs changing.")
                if "change" in self.groups:
                    self.groups["change"][0].setExpanded(True)
            else:
                done = self.groups.get("done", [None, "", 0])[2]
                failed = self.groups.get("failed", [None, "", 0])[2]
                self.status.setText("Finished: %d switched off%s." % (
                    done, ", %d failed" % failed if failed else ""))
                self.plan = []
                self.apply_button.setEnabled(False)

        # -- Apply ----------------------------------------------------------

        def apply(self):
            if not self.plan:
                return
            answer = QMessageBox.question(
                self, "Apply the changes?",
                "Switch the subtitles off by default in %d file(s)?\n\n"
                "Only that one setting changes; the video, audio and "
                "subtitles themselves are untouched." % len(self.plan))
            if answer != QMessageBox.Yes:
                return
            self.tree.clear()
            self.groups = {}
            self.mode = "apply"
            self.start(Worker("apply", "", self.mkvmerge, self.mkvpropedit,
                              list(self.plan)))

        def closeEvent(self, event):
            if self.thread and self.thread.isRunning():
                self.request_stop()
                self.thread.quit()
                self.thread.wait(5000)
            event.accept()

    app = QApplication.instance() or QApplication(sys.argv)
    window = Window()
    window.show()
    run_gui.window = window         # kept so it is not garbage-collected
    return app.exec()


def main(argv):
    if len(argv) >= 3 and argv[1] in ("--dry-run", "--apply"):
        return run_cli(argv[1][2:], argv[2:])
    if len(argv) > 1:
        print(__doc__)
        return 2
    return run_gui()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
