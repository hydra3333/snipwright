"""Snipwright's own project format (.swproj).

A VideoReDo project (.vprj) records a cut as two times and nothing else: not
which recording it belongs to, not the frame rate it was measured at, not
whether a person or a detector put it there. Snipwright has had to infer all
of that - matching recordings by duration, mapping times back to frames and
hoping they land on the same one, guessing whether a detector's project has
been reviewed. This format stores those things instead, each because
something reads it:

  source      Which recording this is: its path, its path relative to the
              project, size, frame count, frame rate, duration and a
              fingerprint of its first and last few megabytes. Checked when
              the project is opened, and used to find a recording that has
              been moved alongside its project.

  cuts        What to REMOVE, as inclusive frame ranges with the matching
              times alongside. Frames are used whenever the recording is the
              one the project was made from - exact, and immune to a
              broadcast clock that jumps. The times are the fallback when it
              is not, for instance after Quick Stream Fix has rebuilt it.
              Each cut also says whether it is an advert or the recorder's
              padding; nothing sets that yet ("unknown" until the editor can),
              but the field is here so that it can be without a new version.

  markers     Navigation markers, as frames with times alongside.

  provenance  Who found the cuts - a person, or the detector (Chalkline or
              Comskip) whose results Snipwright saved - with which version,
              and whether a person has saved the project since. Snipwright
              always writes the file; Comskip knows nothing about it.

The file is JSON so it can be read and repaired by hand. `format` and
`version` come first; a later version must keep reading this one.

The public functions mirror project/vprj.py - load_swproj(path, index),
save_swproj(path, keep_ranges, markers, source_filename, index, ...) and
read_source_filename(path) - so callers can treat the formats alike.
"""
import datetime
import hashlib
import json
import os

FORMAT = "snipwright-project"
VERSION = 1
EXTENSION = ".swproj"

# How much of each end of the recording the fingerprint reads. Enough to tell
# two recordings apart, including two of the same programme, without reading
# gigabytes over a network share on every save.
FINGERPRINT_BYTES = 4 * 1024 * 1024

# What a cut can be. Only "unknown" is written until the editor can mark it.
CUT_KINDS = ("unknown", "advert", "padding")

# Who found the cuts. Snipwright writes every .swproj itself: "comskip"
# means Snipwright ran Comskip and is saving what it found, not that Comskip
# wrote the file.
MADE_BY = ("person", "chalkline", "comskip", "unknown")


class SwprojError(ValueError):
    """The file is not a Snipwright project this version can read."""


class SwprojData:
    """A loaded project, in the same shape as project.vprj.VprjData plus the
    things only this format knows."""

    def __init__(self, keep_ranges, markers, source_filename, *,
                 source=None, provenance=None, cuts=None,
                 used_frames=False, source_matches=None):
        self.keep_ranges = keep_ranges        # list of (start_frame, end_frame)
        self.markers = markers                # list of frame numbers
        self.source_filename = source_filename
        self.source = source or {}            # as stored
        self.provenance = provenance or {}    # as stored
        self.cuts = cuts or []                # as stored, including "kind"
        # True when the cuts were taken as stored frame numbers, False when
        # they were mapped from their times onto the index given.
        self.used_frames = used_frames
        # True / False once the recording has been compared with what the
        # project was made from; None when there was nothing to compare.
        self.source_matches = source_matches


# --------------------------------------------------------------------------- #
# The recording's identity
# --------------------------------------------------------------------------- #

def fingerprint(path):
    """A hash of the recording's size, first and last few megabytes.

    Cheap enough to take on every save, and enough to tell a moved recording
    from a different one - including a re-recording of the same programme,
    which has the same duration but not the same bytes. Returns "" when the
    file cannot be read.
    """
    try:
        size = os.path.getsize(path)
        digest = hashlib.sha256(str(size).encode("ascii"))
        with open(path, "rb") as fh:
            digest.update(fh.read(FINGERPRINT_BYTES))
            if size > FINGERPRINT_BYTES:
                fh.seek(max(size - FINGERPRINT_BYTES, FINGERPRINT_BYTES))
                digest.update(fh.read(FINGERPRINT_BYTES))
        return "sha256:" + digest.hexdigest()
    except OSError:
        return ""


def _source_block(project_path, source_filename, index):
    source = os.path.abspath(source_filename) if source_filename else ""
    block = {"path": source}
    if source:
        try:
            block["relative"] = os.path.relpath(
                source, os.path.dirname(os.path.abspath(project_path)))
        except ValueError:
            # Different drives on Windows: there is no relative path.
            block["relative"] = None
        try:
            block["size"] = os.path.getsize(source)
        except OSError:
            block["size"] = None
        block["fingerprint"] = fingerprint(source)
    frames = getattr(index, "frame_count", 0) or 0
    block["frames"] = frames
    block["fps"] = getattr(index, "fps", None)
    block["duration"] = index.seconds_of(frames - 1) if frames else 0.0
    return block


def resolve_source(project_path, source):
    """The recording a project refers to, found where it now is.

    Tries the stored absolute path, then the stored path relative to the
    project - so a recording moved together with its project is still found -
    then the same file name beside the project. Returns "" if none exists.
    """
    candidates = []
    if source.get("path"):
        candidates.append(source["path"])
    here = os.path.dirname(os.path.abspath(project_path))
    if source.get("relative"):
        candidates.append(os.path.normpath(os.path.join(here, source["relative"])))
    if source.get("path"):
        candidates.append(os.path.join(here, os.path.basename(source["path"])))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SwprojError("%s could not be read: %s" % (path, exc)) from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SwprojError("%s is not a Snipwright project" % path)
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    if version < 1:
        raise SwprojError("%s has no usable format version" % path)
    if version > VERSION:
        raise SwprojError(
            "%s was saved by a newer Snipwright (project format %d; this "
            "version reads up to %d)" % (path, version, VERSION))
    return data


def read(path):
    """The project's stored contents, or SwprojError saying why this version
    cannot read it - not a project, unreadable, or from a newer Snipwright."""
    return _read(path)


def is_swproj(path):
    """Whether `path` is a Snipwright project, judged by its content."""
    try:
        _read(path)
        return True
    except SwprojError:
        return False


def read_source_filename(path):
    """The recording this project refers to, found where it now is - or the
    stored path if it cannot be found, or "" if the file is unreadable.

    Needs no FrameIndex, so the Batch Manager can find and index the
    recording before loading the cuts onto it."""
    try:
        data = _read(path)
    except SwprojError:
        return ""
    source = data.get("source") or {}
    return resolve_source(path, source) or source.get("path", "") or ""


def _invert(removed, total_frames):
    """Kept ranges from removed ones, both inclusive frame ranges."""
    keeps, cursor = [], 0
    for start, end in sorted(removed):
        start, end = max(0, start), min(total_frames - 1, end)
        if end < start:
            continue
        if start > cursor:
            keeps.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= total_frames - 1:
        keeps.append((cursor, total_frames - 1))
    return keeps


def load_swproj(path, index):
    """Read a project and put its cuts onto `index`.

    When the recording is the one the project was made from - the same frame
    count and, where both are known, the same fingerprint - the stored frame
    numbers are used as they are. Otherwise each cut's times are mapped onto
    the index, exactly as a .vprj is.
    """
    data = _read(path)
    source = data.get("source") or {}
    total = index.frame_count

    matches = None
    if source.get("frames"):
        matches = source["frames"] == total
        if matches and source.get("fingerprint"):
            located = resolve_source(path, source)
            if located:
                matches = fingerprint(located) == source["fingerprint"]
    use_frames = bool(matches)

    removed, cuts = [], []
    for cut in data.get("cuts") or []:
        if not isinstance(cut, dict):
            continue
        cuts.append(cut)
        if use_frames and "start_frame" in cut and "end_frame" in cut:
            start, end = int(cut["start_frame"]), int(cut["end_frame"])
        else:
            start_s = float(cut.get("start", 0))
            start = 0 if start_s <= 0 else index.index_of_seconds(start_s)
            end = index.index_of_seconds(float(cut.get("end", 0)))
        if end < start:
            start, end = end, start
        removed.append((start, end))

    markers = set()
    for marker in data.get("markers") or []:
        if not isinstance(marker, dict):
            continue
        if use_frames and "frame" in marker:
            frame = int(marker["frame"])
        else:
            frame = index.index_of_seconds(float(marker.get("time", 0)))
        if 0 <= frame < total:
            markers.add(frame)

    return SwprojData(
        _invert(removed, total), sorted(markers),
        resolve_source(path, source) or source.get("path", ""),
        source=source, provenance=data.get("provenance") or {}, cuts=cuts,
        used_frames=use_frames, source_matches=matches)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def save_swproj_from_cuts(path, source_filename, cut_ranges_seconds,
                          duration_seconds=0.0, *, made_by="unknown",
                          made_with="", app_version=""):
    """Write a .swproj from cut times alone, with no FrameIndex.

    The Watcher's path: a detector hands back cut regions in seconds, and
    decoding the recording just to number its frames would double the work.
    The cuts are written with times only and no frame numbers, and the
    recording's frame count is left unknown - so load_swproj() always maps
    these times onto the real index when the project is opened, exactly as
    for a .vprj. What it still records is the recording's identity and that a
    detector found the cuts and no person has reviewed them yet.
    """
    if made_by not in MADE_BY:
        made_by = "unknown"
    source = os.path.abspath(source_filename) if source_filename else ""
    block = {"path": source}
    if source:
        try:
            block["relative"] = os.path.relpath(
                source, os.path.dirname(os.path.abspath(path)))
        except ValueError:
            block["relative"] = None
        try:
            block["size"] = os.path.getsize(source)
        except OSError:
            block["size"] = None
        block["fingerprint"] = fingerprint(source)
    block["frames"] = None
    block["fps"] = None
    block["duration"] = float(duration_seconds or 0.0)

    cuts = []
    for start, end in sorted(cut_ranges_seconds or []):
        start, end = float(start), float(end)
        if end < start:
            start, end = end, start
        cuts.append({"start": round(max(0.0, start), 6),
                     "end": round(end, 6), "kind": "unknown"})

    if not app_version:
        try:
            from version import VERSION_STRING
            app_version = VERSION_STRING
        except Exception:
            app_version = "Snipwright"

    doc = {
        "format": FORMAT,
        "version": VERSION,
        "saved_by": app_version,
        "saved_at": datetime.datetime.now(datetime.timezone.utc)
                    .replace(microsecond=0).isoformat(),
        "source": block,
        "cuts": cuts,
        "markers": [],
        "provenance": {
            "made_by": made_by,
            "made_with": made_with or "",
            "reviewed": False,
        },
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return doc


def save_swproj(path, keep_ranges, markers, source_filename, index, *,
                made_by="person", made_with="", reviewed=None,
                app_version=""):
    """Write the edit as a .swproj.

    `made_by` is who found the cuts: "person" for a project saved from the
    editor, or the detector ("chalkline", "comskip") whose results Snipwright
    is saving. `reviewed`
    says whether a person has saved it since the cuts were made; it defaults
    to True for a person's save and False for a detector's. The write is
    atomic - a temporary file renamed over the target - so a crash part-way
    never leaves a half-written project.
    """
    if made_by not in MADE_BY:
        made_by = "unknown"
    if reviewed is None:
        reviewed = made_by == "person"
    total = index.frame_count
    removed = _invert(sorted(keep_ranges), total)
    cuts = [{
        "start_frame": start,
        "end_frame": end,
        "start": round(index.seconds_of(start), 6),
        "end": round(index.seconds_of(end), 6),
        "kind": "unknown",
    } for start, end in removed]
    marks = [{"frame": f, "time": round(index.seconds_of(f), 6)}
             for f in sorted(set(markers)) if 0 <= f < total]

    if not app_version:
        try:
            from version import VERSION_STRING
            app_version = VERSION_STRING
        except Exception:
            app_version = "Snipwright"

    doc = {
        "format": FORMAT,
        "version": VERSION,
        "saved_by": app_version,
        "saved_at": datetime.datetime.now(datetime.timezone.utc)
                    .replace(microsecond=0).isoformat(),
        "source": _source_block(path, source_filename, index),
        "cuts": cuts,
        "markers": marks,
        "provenance": {
            "made_by": made_by,
            "made_with": made_with or "",
            "reviewed": bool(reviewed),
        },
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return doc
