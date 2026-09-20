"""
Run Chalkline on a video to detect advert breaks, on a worker thread.

Chalkline decodes the whole file, which takes a few minutes on an HD
recording, so it runs in the background with progress.  The detected breaks
are written to an EDL in a temporary directory and that path is handed back.

The contract deliberately matches comskip.py's exactly - same three signals,
same "emit a path to an EDL" result - so the caller that already parses
Comskip's output needs no second code path, and choosing between the two
detectors is a matter of which worker gets constructed.  Chalkline produces
its breaks directly rather than via a file, so writing an EDL is a small
detour; it is worth it to keep one parsing path rather than two.

Qt lives here and not in chalkline.py.  The detector itself imports nothing
beyond the standard library and numpy, which keeps it runnable from the
command line for corpus testing without pulling in a GUI toolkit - and lets
the Watcher, which is deliberately Qt-free so it can be tested headlessly,
call run_chalkline() directly.  That function therefore lives in
chalkline.py; only the QThread around it is here.
"""

import shutil
import tempfile

from PySide6.QtCore import QThread, Signal

from repair.chalkline import (
    LOGO_STORE,
    ChalklineCancelled,
    learn_from_project,
    run_chalkline,
)


class ChalklineWorker(QThread):

    finished_ok = Signal(str)     # emits the EDL path
    failed = Signal(str)
    progress = Signal(int, int)   # percent, pass number

    def __init__(self, source_path, parent=None, store_path=LOGO_STORE,
                 channel=None):
        super().__init__(parent)
        self.source_path = source_path
        self.store_path = store_path
        self.channel = channel
        # Populated before finished_ok is emitted, so a caller that wants to
        # report which technique fired can read it from the worker.
        self.info = {}
        self._cancel = False
        self._out_dir = tempfile.mkdtemp(prefix="snipwright-chalkline-")

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            edl, info = run_chalkline(
                self.source_path,
                self._out_dir,
                progress_cb=self.progress.emit,
                cancel_cb=lambda: self._cancel,
                store_path=self.store_path,
                channel=self.channel,
            )
            self.info = info
            if not self._cancel:
                self.finished_ok.emit(edl)
        except ChalklineCancelled as exc:
            # Cancelling is not failing - the same distinction stream_fix and
            # the exporter already draw.  Reported so the caller can tell the
            # two apart, since a cancelled run and a broken one need
            # different messages.
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))

    def cleanup(self):
        """Remove the temporary output directory."""
        try:
            shutil.rmtree(self._out_dir, ignore_errors=True)
        except Exception:
            pass


class ChalklineLearnWorker(QThread):
    """Learn a channel's logo from a saved project, in the background.

    Deliberately quieter than ChalklineWorker: there is no progress dialog
    and no cancel button, because the user did not ask for this - they asked
    to save a project, and this is Snipwright taking the opportunity.  It
    must therefore never interrupt them, and never report a failure as
    anything more than a log line.

    It reports through one signal carrying the info dict, whatever happened.
    `learned` in that dict means a logo was stored; `skipped` means nothing
    was, with the reason; `error` means it broke.  The caller decides
    whether any of that is worth a word in the status bar.

    The cheap checks - is the edit plausible, is the channel known, is a
    logo already remembered - happen first and cost an ffprobe.  Only a
    recording that passes them reaches the decode, which is the expensive
    part.  So the common case, saving a project for a channel already
    learned, costs almost nothing.
    """

    finished_learning = Signal(dict)
    started_learning = Signal(str)   # emits the channel key, decode starting

    def __init__(self, video_path, vprj_path, parent=None,
                 store_path=LOGO_STORE, channel=None, corrected=True):
        super().__init__(parent)
        self.video_path = video_path
        self.vprj_path = vprj_path
        self.store_path = store_path
        self.channel = channel
        # False when the saved cuts are the detector's own, unchanged: a
        # settled channel learns nothing from being told it was right, and
        # the decode is minutes long.
        self.corrected = corrected
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            info = learn_from_project(
                self.video_path,
                self.vprj_path,
                store_path=self.store_path,
                channel=self.channel,
                corrected=self.corrected,
                cancel_cb=lambda: self._cancel,
                on_start=self.started_learning.emit,
            )
        except ChalklineCancelled as exc:
            info = {"skipped": str(exc) or "cancelled"}
        except Exception as exc:
            info = {"error": str(exc)}
        self.finished_learning.emit(info)
