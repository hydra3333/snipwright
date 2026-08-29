"""Advert detection page: which detector finds the breaks, and its settings.

Its own page rather than a section of External tools, because only one of the
two detectors *is* an external tool.  Chalkline ships with Snipwright and
needs no setup at all, so filing it under programs the user has to go and
install would be misleading - and the choice between the two belongs beside
both of them rather than above one.

The Comskip program and .ini live here too.  They are external, but they are
settings *for detecting adverts* and nothing else uses them, so grouping them
by what they are for beats grouping them by what they happen to be.

All user-facing text uses British English.
"""

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QWidget,
)

from ui.settings_pages import SettingsPage
from ui.settings_widgets import FileRow, hint
from PySide6.QtCore import QT_TRANSLATE_NOOP


def _divider():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet("color: #3a3d42;")
    return line


class AdDetectionPage(SettingsPage):
    TITLE = QT_TRANSLATE_NOOP("Settings", "Advert detection")

    def build(self):
        s = self._settings()
        p = self._paths()

        # One choice, used everywhere - the editor and the Watcher both.
        # Offering it per-context would mean a recording could be detected one
        # way unattended and another way by hand, which is a confusing thing
        # to explain and a worse thing to debug.
        #
        # Chalkline is first, and is what a new installation gets: it is
        # already present and needs nothing installed or configured, whereas
        # choosing Comskip first would mean every new user meeting an error
        # about a missing program before they could detect anything.
        self._detector = QComboBox()
        self._detector.addItem(self.tr("Chalkline (built in)"), "chalkline")
        self._detector.addItem(self.tr("Comskip (external program)"), "comskip")
        current = str(s.get("ad_detector", "chalkline")).lower()
        index = self._detector.findData(current)
        self._detector.setCurrentIndex(index if index >= 0 else 0)
        self.add(self._detector)
        self.add(hint(
            self.tr("Which detector finds the advert breaks, in the editor and the "
            "Watcher alike.")
        ))

        self.add(_divider())

        chalkline_label = QLabel(self.tr("Chalkline"))
        chalkline_label.setStyleSheet("font-weight: bold;")
        self.add(chalkline_label)
        self.add(hint(
            self.tr("Built into Snipwright, so there is nothing to install or "
            "configure. It reads the channel logo, the picture shape and the "
            "aspect ratio, and reports nothing at all when it cannot tell - "
            "rather than guessing and removing part of the programme.")
        ))

        self._chalkline_learn = QCheckBox(
            self.tr("Learn channel logos from my edits")
        )
        self._chalkline_learn.setChecked(bool(s.get("chalkline_learn", True)))
        self.add(self._chalkline_learn)
        self.add(hint(
            self.tr("After you correct a detection and save the project, Chalkline "
            "remembers what that channel's logo looks like and finds the breaks "
            "better on the next recording from it. It only ever learns from a "
            "project you have edited yourself, never from its own unattended "
            "results.")
        ))

        self._logos_btn = QPushButton(self.tr("Remembered logos…"))
        self._logos_btn.clicked.connect(self._open_logo_store)
        row = QHBoxLayout()
        row.addWidget(self._logos_btn)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        self.add(holder)
        self.add(hint(
            self.tr("See what Chalkline has learned, pair a channel name with the "
            "service number the other recorder gives it, or forget a logo so "
            "it is learned afresh.")
        ))

        self.add(_divider())

        comskip_label = QLabel(self.tr("Comskip"))
        comskip_label.setStyleSheet("font-weight: bold;")
        self.add(comskip_label)

        self._comskip_bin_row = FileRow(
            self.tr("Comskip program:"),
            p.get("comskip_binary", ""),
            self.tr("(path to the comskip executable)"),
            "All files (*)",
        )
        self.add(self._comskip_bin_row)

        self._comskip_ini_row = FileRow(
            self.tr("Comskip .ini:"),
            p.get("comskip_ini", ""),
            self.tr("(optional: path to comskip.ini)"),
            "INI files (*.ini);;All files (*)",
        )
        self.add(self._comskip_ini_row)
        self.add(hint(
            self.tr("Comskip is a separate program you install yourself. The .ini is "
            "optional - leave it blank to use Comskip's built-in defaults.")
        ))

        self._comskip_by_channel = QCheckBox(
            self.tr("Pick the .ini by channel name in the filename")
        )
        self._comskip_by_channel.setChecked(
            bool(p.get("comskip_ini_by_channel", False))
        )
        self.add(self._comskip_by_channel)
        self.add(hint(
            self.tr("For recorders that write the channel into the filename (e.g. "
            "Tvheadend). Put per-channel files named Comskip_<channel>.ini in "
            "the same folder as the .ini above. See the user guide for details.")
        ))

        # Wired up last, once every control it touches exists.
        self._detector.currentIndexChanged.connect(self._sync_detector)
        self._sync_detector()

    def _open_logo_store(self):
        """Open the remembered-logos manager.

        Its own dialog with its own Save, because the logo store is a
        separate file from config.json and cannot ride along on the settings
        write.  Having this dialog's changes undone by Cancel in the parent -
        after they had already been written elsewhere - would be worse than
        not offering the choice.
        """
        from ui.logo_store_dialog import LogoStoreDialog
        LogoStoreDialog(self).exec()

    def _sync_detector(self):
        """Grey out whichever detector's settings are not in use.

        Left visible rather than hidden: a user who has set a Comskip path
        should be able to see it is still there after switching away, rather
        than wondering whether it was lost.
        """
        chalkline = self._detector.currentData() == "chalkline"
        self._chalkline_learn.setEnabled(chalkline)
        # The logo list stays reachable whichever detector is chosen: what
        # Chalkline has learned is still worth seeing, and still worth
        # tidying, while Comskip is doing the detecting.
        for w in (self._comskip_bin_row, self._comskip_ini_row,
                  self._comskip_by_channel):
            w.setEnabled(not chalkline)

    def save(self, config):
        settings = config.setdefault("settings", {})
        settings["ad_detector"] = self._detector.currentData()
        settings["chalkline_learn"] = self._chalkline_learn.isChecked()

        paths = config.setdefault("paths", {})
        paths["comskip_binary"] = self._comskip_bin_row.value()
        paths["comskip_ini"] = self._comskip_ini_row.value()
        paths["comskip_ini_by_channel"] = self._comskip_by_channel.isChecked()
