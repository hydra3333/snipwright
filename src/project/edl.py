"""
Read and write EDL (Edit Decision List) files, Comskip's simplest output.

Each line is tab- or space-separated:

    START_SECONDS   END_SECONDS   ACTION

ACTION is an integer; Comskip uses 0 for a cut (a commercial block to remove).
Other EDL dialects use 1=mute, 2=scene-mark, 3=commercial - we treat any line
as a cut region unless the action is clearly a non-cut type, but Comskip's own
output is always 0.

As with .vprj, the regions listed are the parts to REMOVE.  Snipwright works in
KEPT ranges, so loading inverts the cut list against the total frame count and
saving inverts the kept ranges back into cut regions.
"""


def _invert_ranges(removed, total_frames):
    """KEPT ranges filling the gaps between REMOVED (start, end) frame pairs."""
    if total_frames <= 0:
        return []

    removed = sorted(removed)
    kept = []
    cursor = 0

    for start, end in removed:
        start = max(0, start)
        end = min(total_frames - 1, end)
        if start > cursor:
            kept.append((cursor, start - 1))
        cursor = max(cursor, end + 1)

    if cursor <= total_frames - 1:
        kept.append((cursor, total_frames - 1))

    return kept


def parse_edl_cuts(path):
    """Read an EDL file and return a list of (start_seconds, end_seconds) cut
    regions.  Lines that don't parse are skipped."""
    cuts = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                start = float(parts[0])
                end = float(parts[1])
            except ValueError:
                continue
            # Action field (parts[2]) is optional; Comskip uses 0 = cut.  We
            # accept the line as a cut regardless, since Comskip only emits
            # cut regions in its EDL.
            if end < start:
                start, end = end, start
            cuts.append((start, end))
    return cuts


def describe_mismatch(cuts, duration):
    """Say whether a cut list looks like it belongs to a different recording.

    An EDL names no video - it is only a list of times - so nothing stops one
    being applied to the wrong recording, and the result looks plausible
    rather than broken.  There is no way to be certain, but two shapes are
    worth stopping for.  Returns a description of the problem, or None.

    This is deliberately a plain function rather than part of the loader, so
    it can be tested without a video or a running interface.
    """
    if not cuts or duration <= 0:
        return None

    last = max(end for _start, end in cuts)
    span = last - min(start for start, _end in cuts)

    # Certain: the cut list runs past the end of the video.  Nothing
    # legitimate does this - the times came from a longer recording.
    if last > duration + 1.0:
        return (
            "This cut list runs %.1f seconds past the end of the video "
            "(it ends at %.1f seconds, the video is %.1f seconds long)."
            % (last - duration, last, duration)
        )

    # Likely: the video is far longer than anything the cut list covers.  A
    # cut list made for its own recording reaches most of the way through it,
    # because the breaks are spread across the programme and the recording
    # padding is usually cut too.
    #
    # This one is a judgement rather than a certainty: a recording whose only
    # break falls early, with a long unpadded tail, could trip it honestly.
    # It asks rather than refuses for that reason.
    if span > 0 and duration > 2.0 * last:
        return (
            "This cut list only covers the first %.0f%% of the video "
            "(it ends at %.1f seconds, the video is %.1f seconds long)."
            % (100.0 * last / duration, last, duration)
        )

    return None


def load_edl(path, index):
    """Parse an EDL and map it onto the given FrameIndex.

    Returns (keep_ranges, []) - EDL carries no navigation markers, so the
    marker list is always empty (kept for symmetry with the .vprj loader).
    """
    total_frames = index.frame_count

    removed = []
    for start_s, end_s in parse_edl_cuts(path):
        # A cut starting at (or before) zero means "from the very start"; map
        # it to frame 0 explicitly (some broadcast indexes put frame 0 at a
        # slightly negative time, so index_of_seconds(0.0) can land late).
        if start_s <= 0:
            start_f = 0
        else:
            start_f = index.index_of_seconds(start_s)
        end_f = index.index_of_seconds(end_s)
        if end_f < start_f:
            start_f, end_f = end_f, start_f
        removed.append((start_f, end_f))

    keep_ranges = _invert_ranges(removed, total_frames)
    return keep_ranges, []


def save_edl(path, keep_ranges, index):
    """Write an EDL describing the edit.

    keep_ranges are KEPT (green) frame ranges; they are inverted into cut
    regions and written as 'start_seconds<TAB>end_seconds<TAB>0' lines.
    """
    total_frames = index.frame_count
    removed = _invert_ranges(sorted(keep_ranges), total_frames)

    with open(path, "w", encoding="utf-8") as f:
        for start_f, end_f in removed:
            start_s = max(0.0, index.seconds_of(start_f))
            end_s = max(0.0, index.seconds_of(end_f))
            f.write(f"{start_s:.2f}\t{end_s:.2f}\t0\n")
