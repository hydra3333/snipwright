"""Single-instance coordination for Snipwright.

Two separate jobs live here.

The **Watcher** uses `watcher_lock_path()` / `watcher_is_running()` to refuse
starting a second copy (two tray icons and two background scanners would be a
mess), and the editor's "Launch Snipwright Watcher" menu item uses the same
check to say it is already running rather than spawning a duplicate.

The **editor** uses `EditorInstance` to make sure only one copy runs at a time.
A second launch - almost always a file manager opening a `.vprj` that has been
associated with Snipwright - hands its file to the copy already running and
quits, rather than opening a second window.

That hand-off matters more than the refusal.  The second process exists
BECAUSE the user double-clicked a project; a version that merely exits would
leave the project unopened and the file association looking broken.

**Why single instance at all.**  Two copies share the batch queue, the settings
file, the logo store and the staging folder, and two of those lose data under
two writers.  Worse, two Batch Managers running at once will both work through
the same queue with nothing marking a job as taken: measured on 2026-09-11,
both copies exported the same two recordings to the same output paths five
seconds apart.  Making that safe needs per-job claiming that survives a crash;
preventing it costs a lock and a socket.  See HANDOVER.md item 1h.
"""

import os
import tempfile


def watcher_lock_path():
    """Return the per-user lock-file path in the system temp directory.

    The owning user's id is folded into the name so two people sharing a
    machine don't block one another.
    """
    if hasattr(os, "getuid"):
        name = "snipwright-watcher-%d.lock" % os.getuid()
    else:
        name = "snipwright-watcher.lock"

    return os.path.join(tempfile.gettempdir(), name)


def watcher_is_running():
    """Best-effort check: True if a live Watcher currently holds the lock.

    Uses QLockFile, which treats a lock left behind by a dead process as stale,
    so a Watcher that crashed won't be mistaken for a running one.  Never
    raises - any problem is reported as "not running" so the caller can simply
    try to start it.
    """
    try:
        from PySide6.QtCore import QLockFile

        probe = QLockFile(watcher_lock_path())
        probe.setStaleLockTime(0)

        if probe.tryLock(50):
            # Nothing was holding it - release immediately so the real Watcher
            # can take it when it starts.
            probe.unlock()
            return False

        return True
    except Exception:
        return False


def editor_lock_path():
    """Lock file for the editor, alongside the Watcher's and named the same way."""
    if hasattr(os, "getuid"):
        name = "snipwright-editor-%d.lock" % os.getuid()
    else:
        name = "snipwright-editor.lock"

    return os.path.join(tempfile.gettempdir(), name)


def editor_socket_name():
    """Local-socket name the running editor listens on.

    Per-user for the same reason the lock file is, and deliberately NOT a path
    under the temp directory: QLocalServer treats this as a name and picks the
    right mechanism per platform (a named pipe on Windows, a socket file on
    Unix).  Handing it a Windows path would put a colon in a name that has to
    survive being used as one.
    """
    if hasattr(os, "getuid"):
        return "snipwright-editor-%d" % os.getuid()
    return "snipwright-editor"


class EditorInstance:
    """Holds the editor's single-instance lock and listens for hand-offs.

    Usage at startup, before building the main window:

        inst = EditorInstance()
        if not inst.acquire():
            inst.hand_off(path_or_none)     # tell the running copy
            sys.exit(0)
        ...
        inst.listen(on_open)                # call once the window exists

    `acquire()` is the only thing that decides whether this process continues.
    Everything else is best-effort: if the socket cannot be created the copy
    already running simply will not hear about the file, which is no worse than
    the behaviour before any of this existed.
    """

    def __init__(self):
        self._lock = None
        self._server = None

    # -- the decision ----------------------------------------------------- #

    def acquire(self):
        """True if this process may run; False if another editor holds the lock.

        A lock left behind by a process that died is treated as stale by
        QLockFile, so a crash cannot lock the user out of their own editor -
        which is the failure mode that would matter most here.

        Any error acquiring returns True: better two windows than an editor
        that will not start.
        """
        try:
            from PySide6.QtCore import QLockFile

            self._lock = QLockFile(editor_lock_path())
            # 30s: long enough that a busy start-up is not mistaken for a dead
            # process, short enough that a real crash does not strand the user.
            self._lock.setStaleLockTime(30000)
            if self._lock.tryLock(100):
                return True
            self._lock = None
            return False
        except Exception:
            self._lock = None
            return True

    # -- second process: hand over and go -------------------------------- #

    def hand_off(self, path=None):
        """Ask the running editor to come forward, and open `path` if given.

        Returns True if the running copy acknowledged.  A False here means the
        lock was held but nothing answered the socket - a half-dead instance -
        and the caller may reasonably carry on and start anyway rather than
        leaving the user with nothing.
        """
        try:
            from PySide6.QtNetwork import QLocalSocket

            sock = QLocalSocket()
            sock.connectToServer(editor_socket_name())
            if not sock.waitForConnected(1000):
                return False
            payload = (path or "").encode("utf-8")
            sock.write(payload + b"\n")
            sock.flush()
            sock.waitForBytesWritten(1000)
            # Wait for the acknowledgement before quitting, so the running copy
            # has actually taken the path rather than finding the socket closed
            # underneath it.
            sock.waitForReadyRead(2000)
            sock.disconnectFromServer()
            return True
        except Exception:
            return False

    # -- first process: listen for the next one --------------------------- #

    def listen(self, on_open):
        """Start listening.  `on_open(path)` is called with '' for no path.

        Never raises: if the server cannot be created, a later launch will not
        be able to hand over and will start its own window instead.
        """
        try:
            from PySide6.QtNetwork import QLocalServer

            name = editor_socket_name()
            # A socket left by a process that died would otherwise refuse the
            # name forever.  We hold the lock at this point, so nothing else
            # can legitimately be listening.
            QLocalServer.removeServer(name)

            self._server = QLocalServer()
            if not self._server.listen(name):
                self._server = None
                return False

            def _incoming():
                conn = self._server.nextPendingConnection()
                if conn is None:
                    return

                def _ready():
                    try:
                        raw = bytes(conn.readAll()).decode("utf-8", "replace")
                        conn.write(b"ok\n")
                        conn.flush()
                        path = raw.strip()
                        on_open(path)
                    except Exception:
                        pass
                    finally:
                        conn.disconnectFromServer()

                conn.readyRead.connect(_ready)

            self._server.newConnection.connect(_incoming)
            return True
        except Exception:
            self._server = None
            return False

    def release(self):
        """Stop listening and drop the lock.  Safe to call more than once."""
        try:
            if self._server is not None:
                self._server.close()
                self._server = None
        except Exception:
            pass
        try:
            if self._lock is not None:
                self._lock.unlock()
                self._lock = None
        except Exception:
            pass
