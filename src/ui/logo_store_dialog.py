"""The remembered-logos manager: what Chalkline has learned, per channel.

Chalkline files each learned logo under the channel key it read from the
recording, and the two PVRs give different kinds of key for the same channel:
Tvheadend keeps the SDT so a service *name* survives, Jellyfin keeps the PAT
so a service *id* survives, and neither recording ever carries both - see
channel_key() in chalkline.py.  Nothing in Snipwright can resolve one to the
other, which is why the pairing is typed in here by hand rather than worked
out.  It is the only route there is.

A row is one learned logo.  The Channel and Service ID columns are both
lookup keys for that same logo, so filling in the empty half of a row makes
the mask work for recordings from the other PVR too.  Typing a key that
already has a row of its own merges the two.

The mask itself is drawn rather than described.  A learned logo looks like a
logo; a mask learned from a poor edit looks like scattered noise or a
fragment of a caption, and no number conveys that as quickly as the shape
does.

All user-facing text uses British English.
"""

import logging
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from repair.chalkline import (
    GH,
    GW,
    LOGO_STORE,
    MASK_HISTORY_MAX,
    active_index,
    load_store,
    mask_history,
    same_mask,
    save_store,
    store_display_name,
    store_service_id,
    write_history,
)

# The thumbnail is drawn as blocks at the mask's own scale, one block per
# analysis pixel.  A logo is 30-70 pixels on a 160x90 analysis frame, so
# there is no detail to smooth and nothing to interpolate - each stored
# pixel is drawn, or it isn't.
_THUMB_W, _THUMB_H = 96, 54
# Breathing room inside the cell, so a mask never touches the row edges.
_PAD = 8
# How far a mask may be magnified.  Without a limit, every mask was scaled to
# fill the thumbnail, so a five-pixel sliver and a seventy-pixel logo were
# drawn the same size - and a mask only one row tall came out as a bold line
# across the whole cell, which flatters it enormously.  Capping the zoom
# keeps the column comparable: a big logo looks big, a scrap looks like a
# scrap.
_MAX_ZOOM = 6
# A one-pixel gap between neighbouring mask pixels.  Drawn solid, adjacent
# pixels merge into a blob and the mask's internal structure disappears;
# drawn with a two-pixel gap it dissolves into scattered dots.  One is the
# balance - the shape still reads, and the pixel grid it is made of stays
# visible, which is honest about how coarse a 160x90 mask really is.
_GUTTER = 1
_SID_PREFIX = "sid:"

log = logging.getLogger("snipwright")


def _mask_thumbnail(entry):
    """Draw a learned mask as a small monochrome picture, to scale.

    The stored pixels are fractions of the recording's active picture, so
    multiplying them by the analysis grid gives the mask's size in grid
    pixels - the same arithmetic mask_to_pixels() does when the mask is
    actually used, which is the reason to copy it rather than invent a
    second scale.  Every row is then drawn at one magnification, so a big
    logo looks big and a scrap looks like a scrap.

    The entry's own `box` is deliberately not used here: it is the mask's
    bounding box as a fraction of the picture, not the picture itself, and
    scaling the pixels by it collapses every mask to a single dot.
    """
    pixels = entry.get("pixels") or []
    if not pixels:
        return None

    # Into grid pixels, so a mask's drawn size means something.
    try:
        xs = [float(p[0]) * (GW - 1) for p in pixels]
        ys = [float(p[1]) * (GH - 1) for p in pixels]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs:
        return None

    left, top = min(xs), min(ys)
    bw = max(1, int(round(max(xs) - left)) + 1)
    bh = max(1, int(round(max(ys) - top)) + 1)

    zoom = min(
        (_THUMB_W - _PAD) / bw,
        (_THUMB_H - _PAD) / bh,
        float(_MAX_ZOOM),
    )
    cell = max(1, int(round(max(zoom, 1.0))))
    block = max(1, cell - _GUTTER)

    pix = QPixmap(_THUMB_W, _THUMB_H)
    pix.fill(QColor(27, 27, 31))
    painter = QPainter(pix)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(230, 230, 235))
    origin_x = (_THUMB_W - bw * cell) // 2
    origin_y = (_THUMB_H - bh * cell) // 2
    # Drawn as blocks rather than scaled from an image: a scaled image has no
    # way to leave the gap between pixels, and nearest-neighbour magnification
    # is what produced the solid blobs this replaces.
    for x, y in zip(xs, ys):
        px = int(round(x - left))
        py = int(round(y - top))
        if 0 <= px < bw and 0 <= py < bh:
            painter.drawRect(
                origin_x + px * cell, origin_y + py * cell, block, block
            )
    painter.end()
    return pix


class LogoStoreDialog(QDialog):
    """Show and edit the logos Chalkline has learned."""

    COL_MASK, COL_NAME, COL_SID, COL_LOGO, COL_CONTRAST = range(5)

    def __init__(self, parent=None, store_path=LOGO_STORE):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Remembered logos"))
        self.setMinimumSize(660, 420)
        self._store_path = store_path
        self._filling = False
        # An edit in flight, and the edit waiting for the event loop to
        # settle before it is applied.  See _on_item_changed.
        self._editing = False
        self._pending_edit = None
        # Keys the user has forgotten in this session.  Without these, an
        # additive reload would fetch a forgotten channel straight back off
        # the disk it has not yet been removed from.
        self._removed_keys = set()
        self._store = load_store(store_path)

        layout = QVBoxLayout(self)

        intro = QLabel(self.tr(
            "Chalkline learns each channel's logo when you correct a "
            "detection and save the project. A recorder that keeps the "
            "channel name gives a name here; one that keeps the service "
            "number gives a number. They are the same channel, but Snipwright "
            "cannot tell - fill in the missing half of a row and the logo "
            "will be used for recordings from both."
        ))
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray;")
        layout.addWidget(intro)

        body = QHBoxLayout()
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            self.tr("Logo"), self.tr("Channel"), self.tr("Service ID"),
            self.tr("Mask"), self.tr("Contrast"),
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_NAME, QHeaderView.Stretch)
        for c in (self.COL_MASK, self.COL_SID, self.COL_LOGO,
                  self.COL_CONTRAST):
            header.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        body.addWidget(self.table, 1)

        side = QVBoxLayout()
        self._forget_btn = QPushButton(self.tr("Forget"))
        self._forget_btn.clicked.connect(self._forget)
        side.addWidget(self._forget_btn)
        side.addStretch(1)
        body.addLayout(side)
        layout.addLayout(body)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: gray;")
        layout.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).clicked.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill_table()

    # -- the store can change under us -------------------------------------

    def event(self, ev):
        """Re-read the store when the window is activated.

        Learning runs on a background thread while the editor is in use, so a
        logo can be learned while this dialog sits open.  Without this, the
        table would show a stale picture and Save would write it back,
        deleting whatever had been learned in the meantime.
        """
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.WindowActivate:
            self._reload_if_changed()
        return super().event(ev)

    def _reload_if_changed(self):
        """Take in anything learned since this opened, without losing edits.

        Additive only.  The first version of this replaced the whole store
        whenever the file differed from what was loaded - which is true of
        every unsaved edit in this dialog, so joining two rows was
        immediately undone by the window regaining focus, and the status line
        blamed a background learn that had never happened.

        Learning can only ever *add* a channel, so a row on disk that this
        dialog knows nothing about is new and worth taking.  Everything else
        on disk is either already here, possibly edited, or was deliberately
        forgotten - and in both cases what is on screen is the newer truth.
        """
        if self._editing or self._pending_edit is not None:
            # An edit is part-way through and is holding entries out of the
            # current store.  Swapping the store now would orphan them, and
            # the edit would silently do nothing.
            return
        try:
            on_disk = load_store(self._store_path)
        except Exception:
            return

        channels = self._channels()
        known = {k for e in channels for k in e.get("keys", [])}
        known |= self._removed_keys

        added = [
            dict(e) for e in on_disk.get("channels", [])
            if e.get("keys") and not (set(e["keys"]) & known)
        ]
        if not added:
            return

        channels.extend(added)
        self._fill_table()
        names = ", ".join(
            store_display_name(e) or store_service_id(e) for e in added
        )
        self._status.setText(self.tr(
            "Chalkline learned a logo while this window was open, and it has "
            "been added to the list: %s"
        ) % names)

    # -- table --------------------------------------------------------------

    def _channels(self):
        return self._store.setdefault("channels", [])

    def _fill_table(self):
        self._filling = True
        channels = self._channels()
        self.table.setRowCount(len(channels))
        self.table.verticalHeader().setDefaultSectionSize(_THUMB_H + 6)

        for i, entry in enumerate(channels):
            thumb = QTableWidgetItem()
            thumb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            pix = _mask_thumbnail(entry)
            if pix is not None:
                thumb.setData(Qt.DecorationRole, pix)
            self.table.setItem(i, self.COL_MASK, thumb)

            name = QTableWidgetItem(store_display_name(entry))
            self.table.setItem(i, self.COL_NAME, name)

            sid = QTableWidgetItem(store_service_id(entry))
            self.table.setItem(i, self.COL_SID, sid)

            kind = entry.get("kind") or ""
            history = mask_history(entry)
            text = (self.tr("%(count)dpx %(kind)s")
                    % {"count": int(entry.get("count", 0)), "kind": kind})
            if len(history) > 1:
                text = (self.tr("%(logo)s, best of %(held)d")
                        % {"logo": text, "held": len(history)})
            logo = QTableWidgetItem(text)
            logo.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            # What each logo this channel holds has done on the projects the
            # user corrected - the evidence the choice above rests on.
            lines = []
            for item in history:
                record = item["record"].get("on", {})
                line = self.tr(
                    "%(count)dpx: %(seen)d project(s), "
                    "%(false)d invented break(s)"
                ) % {
                    "count": int(item["mask"].get("count", 0)),
                    "seen": len(record),
                    "false": sum(r.get("false", 0) for r in record.values()),
                }
                if same_mask(item["mask"], entry):
                    line += self.tr(" - in use")
                lines.append(line)
            logo.setToolTip("\n".join(lines))
            self.table.setItem(i, self.COL_LOGO, logo)

            contrast = QTableWidgetItem("%.2f" % float(entry.get("contrast", 0)))
            contrast.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            contrast.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, self.COL_CONTRAST, contrast)

            # On every cell of the row, not just the one that happens to name
            # the mask.  The user went looking for this on the logo picture -
            # the column actually HEADED "Logo" - and found nothing there.
            for col in range(self.table.columnCount()):
                cell = self.table.item(i, col)
                if cell is not None:
                    cell.setToolTip("\n".join(lines))

        self._filling = False
        if not channels:
            self._status.setText(self.tr(
                "Nothing learned yet. Correct a detection and save the "
                "project, and the channel's logo will appear here."
            ))

    def _on_item_changed(self, item):
        """Note the edit and handle it once the cell editor has closed.

        Nothing is done here directly.  Committing an edit emits this while
        the table is still finishing with its editor widget, and opening a
        modal question from inside that runs a nested event loop - during
        which the dialog is deactivated and reactivated, `_reload_if_changed`
        fires, and `self._store` can be replaced underneath the very entries
        this handler is holding.  The merge then edits an orphaned list and
        the table, refilled from the reloaded store, shows no change at all.

        Deferring to the next turn of the event loop costs nothing and means
        every path below runs with the table settled and the store still the
        one the row indices refer to.
        """
        if self._filling:
            return
        col = item.column()
        if col not in (self.COL_NAME, self.COL_SID):
            return
        self._pending_edit = (item.row(), col, item.text().strip())
        QTimer.singleShot(0, self._apply_pending_edit)

    def _apply_pending_edit(self):
        pending = self._pending_edit
        self._pending_edit = None
        if pending is None:
            return
        row, col, typed = pending

        # Held across the whole edit, modal question included, so a reload
        # cannot swap the store out from under the entries below.
        self._editing = True
        try:
            self._apply_edit(row, col, typed)
        except Exception:
            # An exception in a Qt slot goes to the terminal and the handler
            # simply stops, which looks exactly like nothing having happened.
            # Put it in the log where it will be found.
            log.exception("Editing the remembered-logo list failed")
            self._fill_table()
        finally:
            self._editing = False

    def _apply_edit(self, row, col, typed):
        channels = self._channels()
        if not (0 <= row < len(channels)):
            return

        entry = channels[row]
        old = (store_display_name(entry) if col == self.COL_NAME
               else store_service_id(entry))

        if col == self.COL_SID and typed and not typed.startswith(_SID_PREFIX):
            # Accept a bare number: the Tvheadend UI shows service ids without
            # the prefix, so requiring it would mean retyping what was copied.
            typed = _SID_PREFIX + typed

        if typed == old:
            return

        if not typed:
            # Clearing a key is allowed, but never the last one - an entry
            # with no keys can never be found again and would sit in the file
            # unreachable.
            keys = [k for k in entry.get("keys", []) if k != old]
            if not keys:
                self._warn(self.tr(
                    "A logo needs at least one of Channel or Service ID. Use "
                    "Forget to remove it entirely."
                ))
                self._fill_table()
                return
            entry["keys"] = keys
            self._fill_table()
            return

        clash = None
        for other in channels:
            if other is not entry and typed in other.get("keys", []):
                clash = other
                break

        if clash is not None:
            if not self._confirm_merge(entry, clash, typed):
                self._fill_table()
                return
            self._merge(entry, clash, typed, old)
            self._fill_table()
            return

        entry["keys"] = [k for k in entry.get("keys", []) if k != old]
        entry["keys"].append(typed)
        self._fill_table()

    # -- merging ------------------------------------------------------------

    def _confirm_merge(self, entry, clash, typed):
        return QMessageBox.question(
            self,
            self.tr("Remembered logos"),
            self.tr(
                "\u201c%s\u201d already has a logo of its own. Joining them "
                "keeps both logos under the one name, and Snipwright uses "
                "whichever does better on the projects you correct."
                "\n\nJoin them?"
            ) % typed,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes

    def _merge(self, entry, clash, typed, old=None):
        """Join two rows, keeping BOTH masks under one entry.

        This used to keep whichever mask had the higher contrast and throw
        the other away, on the reasoning that contrast is an objective
        measure of a template.  It is not a comparable one: contrast is
        measured on the recording each mask was learned from, and the corpus
        has twice shown the higher-contrast mask to be the worse one - ITV1's
        5px mask (0.812) beat its 10px mask (0.721) in a join and then read
        the SD recording's logo 0% of the time, and in 2.6.19 a fuller
        Channel 4 mask gave one more false positive than the sparser one.

        So nothing is thrown away.  The joined entry holds both masks, and
        the projects the user corrects decide between them - see
        mask_history() and pick_active() in chalkline.py.  The mask in use
        stays the one the typed name was already being detected with, until
        the other beats it on a recording they have both been scored against.
        """
        # The name being typed over goes, exactly as it does on a rename
        # that does not collide: the user is saying this entry is "typed",
        # not what it was called.  Its OTHER keys - a service id - stay, and
        # so do the clash's.  Keeping the old name here was how one entry
        # ended up answering to both "ITV1" and "ITV1 HD", two different
        # pictures, with one mask between them.
        keys = list(dict.fromkeys(
            [k for k in entry.get("keys", []) if k != old]
            + list(clash.get("keys", []))
        ))

        channels = self._channels()
        # Located by identity, not by value.  list.index() and "in" both
        # compare with ==, and two entries can compare equal while being
        # different rows - most easily right after a reload, when the list
        # holds fresh copies of the same dictionaries.  Matching the wrong
        # one silently merges the wrong pair.
        at = next((i for i, e in enumerate(channels) if e is entry), -1)
        clash_at = next((i for i, e in enumerate(channels) if e is clash), -1)
        if at < 0 or clash_at < 0:
            log.warning(
                "Could not join the logo entries: the list changed underneath "
                "the edit (entry %s, clash %s).", keys, clash.get("keys"),
            )
            self._status.setText(self.tr(
                "The entries could not be joined - the list had changed. "
                "Try again."
            ))
            return

        # Both histories, the clash's first: the typed name is already being
        # detected with its mask, so that is the incumbent and it keeps its
        # place until the evidence says otherwise.
        history = mask_history(clash)
        incumbent = active_index(clash, history)
        for item in mask_history(entry):
            if not any(same_mask(item["mask"], h["mask"]) for h in history):
                history.append(item)
        history = history[:MASK_HISTORY_MAX]
        merged = write_history(dict(clash), history, incumbent)
        merged["keys"] = keys
        # The merged entry takes the EDITED row's place and the clash row is
        # the one removed, whichever mask ends up in use.  An earlier version
        # removed whichever entry lost the contrast comparison, which did
        # nothing at all when that was the edited row - it had already been
        # replaced on the line above and was no longer in the list.
        channels[at] = merged
        clash_at = next((i for i, e in enumerate(channels) if e is clash), -1)
        if clash_at >= 0:
            del channels[clash_at]

        self._status.setText(self.tr(
            "Joined into one entry holding %(held)d logo(s), using the "
            "%(use)dpx one for now. Correct a detection on this channel and "
            "save it, and the better logo will be chosen on the evidence."
        ) % {
            "held": len(mask_history(merged)),
            "use": int(merged.get("count", 0)),
        })

    # -- actions ------------------------------------------------------------

    def _warn(self, text):
        QMessageBox.information(self, self.tr("Remembered logos"), text)

    def _forget(self):
        row = self.table.currentRow()
        channels = self._channels()
        if not (0 <= row < len(channels)):
            return
        entry = channels[row]
        label = (store_display_name(entry) or store_service_id(entry)
                 or self.tr("this channel"))
        if QMessageBox.question(
            self,
            self.tr("Forget logo"),
            self.tr(
                "Forget the logo remembered for \u201c%s\u201d?\n\nChalkline "
                "will learn it again the next time you correct a detection "
                "for that channel and save the project."
            ) % label,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        del channels[row]
        self._removed_keys |= set(entry.get("keys", []))
        self._fill_table()
        self._status.setText(self.tr("Forgotten: %s") % label)

    def _save(self):
        """Write the table back, merged against whatever is on disk now.

        Not a straight overwrite: a background learn may have added a channel
        since this dialog opened, and writing the loaded snapshot back would
        delete it.  Only the entries this dialog actually touched are
        applied.
        """
        try:
            on_disk = load_store(self._store_path)
        except Exception:
            on_disk = {"channels": []}

        mine = self._channels()
        my_keys = {k for e in mine for k in e.get("keys", [])}
        # Forgotten keys count as "mine" too.  They are absent from the table
        # by definition, so without this the row would be read back off disk
        # as an untouched entry and written straight out again - Forget would
        # appear to work and then quietly undo itself on Save.
        my_keys |= self._removed_keys

        kept = [
            e for e in on_disk.get("channels", [])
            if not (set(e.get("keys", [])) & my_keys)
        ]
        merged = {"channels": kept + [dict(e) for e in mine]}

        try:
            save_store(merged, self._store_path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                self.tr("Remembered logos"),
                self.tr("The logo list could not be saved:\n\n%s") % exc,
            )
            return
        self.accept()
