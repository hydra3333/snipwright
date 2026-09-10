#!/usr/bin/env python3
"""Chalkline - the native advert detector for Snipwright.

Named for the tool a wright snaps across timber to mark where the cut goes.
That is what this does: it marks where the breaks are, it does not cut
anything.

The aim is a detector that needs no compilation, no per-channel configuration
and no tuning by the user, and which fails by finding nothing rather than by
inventing breaks.  A missed break costs the user a manual pass; an invented
break silently removes programme, which is unrecoverable once exported.

DESIGN
------
Two techniques each propose a *bracket* around a suspected break, and the
instant signals then place the edges precisely inside that bracket.

  Logo      A break is a stretch with no channel logo.  Measured against the
            hand-corrected corpus, the logo-off region reliably contains the
            true break with margin at both ends: 6.5 to 11.6 seconds early at
            the start, 6.0 to 16.0 seconds late at the end.  The overshoot is
            near-constant within a recording (5USA: -11.30, -11.59, -11.36;
            Legend: -8.46, -8.40, -8.09), so the logo is an excellent bracket
            but a poor edge.

  Aspect    A break is a stretch whose picture shape differs from the
            programme's.  Where it applies this is the most accurate signal
            available - start errors of -0.36s three times running on ITV4 and
            -0.90s three times on Sky Mix, which is better than Comskip's own
            output on the same files.

Neither is sufficient alone and neither is a property of the channel:

    5USA     logo only        adverts and programme are both 16:9
    ITV4     both             aspect is the more accurate
    Legend   both             aspect is the more accurate
    Sky Mix  aspect only      no logo Comskip or this detector can find
    U&Dave   logo only
    BBC One  neither          correctly reports nothing

This is why per-channel profiles are the wrong answer.  Sky Mix alone needs
aspect for a 4:3 archive programme and the logo for a 16:9 one, so a "Sky Mix
profile" would be wrong half the time.  The techniques are tried per recording
and whichever fits wins.

A third technique exists purely as a fallback, for a recording neither of
these can say anything about - Grimm, 16:9 throughout with no logo either
technique can find.  It brackets from the density of black/silence/scene
coincidence events inside a candidate span rather than from a picture
property, and it is engaged only when shape and logo both propose nothing at
all, so it can never compete with or distort a recording either of them
already handles.  Checked against the corpus: BBC One and a second
no-advertising recording (BBC Three) both stay silent, and the other four
corpus recordings cannot be reached by this at all since shape or logo
already proposes brackets on every one of them.  See coincidence_brackets()
for the reasoning.

A fourth technique, sample aspect ratio, is not a fallback at all: it is
tried on every recording, the same as shape and logo, because the case it
exists for is different in kind - a recording where the existing
techniques already propose brackets, just wrong ones.  Some broadcasters
change a recording's declared pixel aspect ratio between programme and
advert without changing the decoded picture at all, which shape cannot see
by construction: the analysis frames are decoded at a fixed size that
discards source SAR before shape_track() ever runs.  Single-recording
validation only, prototyped and not yet checked against the rest of the
corpus - see sar_brackets() and CLAUDE.md, "Measured: SAR change...".

A fifth technique, programme anchors, does not propose break brackets at
all - it proposes the opposite, and derives breaks from the gaps.  Every
other technique here assembles a break directly from whatever evidence it
finds and, where more than one candidate overlaps, `build_breaks()` picks
the single highest-evidence one and discards the rest - which fails when a
real break's own evidence arrives in more than one piece, since an advert
break's content is heterogeneous (several different adverts, no signal
consistent throughout all of them) and easily fragments.  This is Comskip's
own strategy, not one invented here - see CLAUDE.md, "Comskip comparison" -
adapted to this file's own shape signal: find long, confident runs where
the picture matches the programme's own dominant shape, and report every
gap between two consecutive runs as one whole break, however fragmented the
evidence inside that gap would otherwise look.  A film's aspect ratio is
consistent throughout, which makes confident programme a genuinely easier
thing to anchor on than the heterogeneous content sitting in between.
Validated across all four ITV1 films in the corpus - see anchor_gaps().

MEMORY
------
The analysis frames are held as uint8 in a disk-backed memmap and reduced in
chunks.  An earlier version converted the whole array to float32 and took two
int16 gradient copies of it, which is over 1.5GB on an hour of video and hung
the machine it was running on.  Nothing here may hold more than one chunk in
float at a time.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import numpy as np

# ---------------------------------------------------------------------------
# Constants, all measured rather than assumed
# ---------------------------------------------------------------------------

# Analysis frames.  Two per second is enough to place a bracket to within half
# a second, which is finer than the edge refinement that follows it.
FPS = 2.0
GW, GH = 160, 90

# Break length bounds, measured across the thirteen hand-corrected breaks in
# the corpus.  They run from 100s (U&Dave) to 342s (5USA).
#
# The figure inherited from the first prototype was "two to four minutes",
# encoded as a 120-240s preference with a 360s ceiling, which peaked below
# almost every real break and could not represent 5USA's 342s break at all.
# A later correction to 180-360s was also wrong: it was derived without
# U&Dave's 100s break and penalised it nearly as hard as the original.
MIN_BREAK = 75.0
MAX_BREAK = 420.0

# The most of a recording a single boundary-exempt bracket may swallow.
#
# A bracket touching the recording's own start or end is exempt from
# MAX_BREAK, because the PVR's padding legitimately runs to ten minutes and
# a genuine change of content at the end has nothing on the far side to have
# overshot into.  That exemption is right and stays - but it was UNBOUNDED,
# and on 5star the corner search proposed "logo absent from 0.00 to 1995.00"
# on a 2581s recording.  It touched time zero, so the ceiling did not apply,
# and the detector reported one 33-minute break that removed all 25.2 minutes
# of programme and kept nothing but tail padding.  Its sibling bracket, the
# same span starting at 295s, was correctly dropped as "over 420s and not at
# an edge" - the exemption was the only thing that let the bad one through.
#
# score() called that detection "found 1/1, false 0": the giant bracket
# overlapped the one real advert, and nothing survived to count as a false
# positive.  A detector that deletes the programme must not be able to report
# a clean sheet, which is why this is a hard floor on the OUTPUT rather than
# another piece of evidence to be weighed.
#
# 0.50 sits in a wide flat band, not on a tuned edge.  Across the ten
# boundary-exempt brackets in run 21 the nine legitimate ones take 5.4% to
# 31.8% of their recording and the 5star one takes 77.0%; any threshold
# between roughly 35% and 70% gives the same answer on all ten.  A figure
# chosen inside a gap that wide is not carrying the result - see
# MASK_POLARITY_MIN for the same reasoning.
#
# This is a guard against catastrophe, not a tuned parameter.  It treats the
# symptom: the root cause on 5star is the corner search preferring a corner
# the logo is on 31.5% of the time to one it is on 71.4% of the time, which
# is a separate question and still open.
MAX_BOUNDARY_SHARE = 0.50

# The floor a break may reach when a remembered logo mask proposed it.
#
# MIN_BREAK is the length below which a bracket is not believed at all, and
# it is set where it is because the corpus contains nothing shorter than
# 100s.  U&Dave's Parks and Recreation runs 40-second breaks, which the
# corpus never contained and which MIN_BREAK therefore discards - the logo
# pass proposed five brackets on one recording and only two survived.
#
# Lowering MIN_BREAK itself would widen every technique's exposure to brief
# logo dropouts mid-programme, and would break sar_brackets()'s coarse sweep,
# which is only safe because nothing shorter than MIN_BREAK can produce a
# bracket (see the 60s sweep there).  So the lower floor applies ONLY to a
# bracket proposed by a remembered *persistent* mask - evidence that had to
# be learned from an edit the user corrected by hand, for this channel
# specifically - and only when both refined edges carry evidence, rather than
# the one end MIN_BREAK-length breaks need.  Every other technique, and every
# channel with no mask, is untouched.
MIN_BREAK_STRONG = 35.0

# Thresholds for the instant signals.  The ffmpeg defaults are far too loose
# on real broadcast material - the default blackdetect called 38% of the 5USA
# recording black.
BLACK_PIC = 0.98
BLACK_PIX = 0.10
BLACK_D = 0.04
SILENCE_DB = -45
SILENCE_D = 0.15
SCENE_TH = 0.40

# A column or row of the analysis frame counts as a black bar below this.
BAR_LEVEL = 24
# Frames over which a bar must persist to count as a bar rather than a dark
# scene.  Twenty-one analysis frames is about ten seconds.
SHAPE_WINDOW = 21
# Gradient magnitude above which an analysis pixel counts as an edge.
EDGE_LEVEL = 18
# Correlation above which two pixels are taken to belong to the same object.
CLUSTER_CORR = 0.6
# A logo is two-dimensional.  A cluster thinner than this, or this much longer
# than it is tall, is a bar edge rather than a logo: on a letterboxed BBC Three
# recording all four corners produced clusters exactly one pixel high - 57x1,
# 57x1, 27x1, 25x1 - as the letterbox boundary appeared and disappeared
# between the programme and the trailers either side of it.  Tracking that as
# a logo produced a break overlapping 219 seconds of the episode on a channel
# that carries no advertising at all.
MIN_CLUSTER_THICKNESS = 2
MAX_CLUSTER_ELONGATION = 10.0
# Persistence levels at which logo brackets are proposed, and how many of
# them must agree before a bracket is believed.  See logo_brackets().
LOGO_LEVELS = (12.0, 20.0, 28.0, 40.0)

# The on-fraction at or above which a corner is a plausible PERSISTENT logo.
#
# A channel DOG is on through the programme and off through the breaks, and
# the programme is most of a recording - so a real persistent logo reads as
# on for roughly two thirds to three quarters of it.
#
# A corner on for only a third is not a logo dropping through breaks.  On
# 5star it was a QR CODE, sitting bottom-right through the 10.6-minute
# infomercial that follows the programme.  The user spotted it in the
# recording; the arithmetic confirms it exactly.  The QR code is present for
# the trailing infomercial and absent for the programme, so the corner
# search - which assumes what it tracks is ON during programme and OFF
# during breaks - read it precisely backwards.  Its "logo absent" bracket,
# 0.00 to 1995.00, is the programme itself: the programme ends at 1943.28,
# a difference of 52 seconds.  Cutting that "break" removed the show.
#
# So the failure is not a marginal logo but an INVERTED one, and the
# on-fraction is what tells them apart: a real DOG is on for the programme,
# an advert's artwork is on for the advert.  Expect more of these - a QR
# code in the corner of an advert is now routine, and nothing about this is
# specific to 5star or to Channel 5.
#
# Used to TIER the corner candidates, not to filter them and not to rank
# within a tier.  Evidence still decides between corners of the same tier,
# because evidence is what separates a logo from picture detail and cluster
# size is known not to - see logo_track() and the Parks and Recreation
# recording, where a 234px cluster of ordinary picture detail beat the real
# 58px logo on size alone.  Nothing here reinstates size as a criterion,
# which matters doubly now: the 5star QR code formed a 157px cluster, the
# largest in the corpus, and the real 5STAR logo only 76px.
#
# 0.50 sits in a wide flat band.  Across the twelve corners chosen over the
# 30-recording corpus, the eleven that produced usable results are on for
# 62.9% to 75.3% of their recording; the one that did not is 5star's BR at
# 31.5%.  Any threshold between roughly 35% and 62% tiers all thirty
# identically, so this figure is not carrying the result.
#
# This is deliberately NOT the 0.30 floor in logo_track(), which answers a
# different question - whether a corner is modulated enough to bracket a
# break at all - and would remove candidates rather than rank them.  A
# recording whose only viable corner sits below this line still uses it.
LOGO_PERSISTENT_ON = 0.50
MIN_LEVEL_SUPPORT = 3

# Fewest coincident pixels that can be a logo.  Measured: Sky Mix formed
# clusters of 18-38 pixels and BBC One, which carries no logo, only 3-6.
MIN_CLUSTER = 10

# Evidence forfeited per second of distance from the bracket edge.
EDGE_DISTANCE_COST = 0.04

# Where learned channel logos are remembered between recordings.
#
# Renamed from ad-logos.json when the detector became Chalkline.  The old file
# is not migrated: a mask is keyed on the channel and cheap to relearn from
# the next edit the user makes on that channel, so carrying stale entries
# across a rename buys nothing.
LOGO_STORE = os.path.expanduser("~/.config/snipwright/chalkline-logos.json")
# Mask contrast above which the logo counts as present in a frame.
MASK_ON = 0.30

# Require the mask to be BRIGHTER than the ring around it before the logo
# counts as present, not merely edgier.
#
# edge_chunk() returns a boolean - (gx + gy) > EDGE_LEVEL - so every technique
# built on it sees "is there an edge here" and never which way the brightness
# goes.  That is blind to the one property separating U&Dave's DOG from the
# Rotorazer infomercial that defeats it: the DOG is white text on dark
# panelling, the advert is dark green text on white.  Measured on u&dave2 and
# u&dave3: programme frames are brighter than their surround 99.7% and 99.6%
# of the time, infomercial frames 0.5% of the time in both, with roughly 65
# luminance levels between the two means.  See "the remaining U&Dave defects"
# in CHALKLINE.md.
#
# ASSUMES A LIGHT LOGO.  Broadcast DOGs are overwhelmingly white or light and
# semi-transparent, but nothing guarantees it, and a channel whose logo is
# darker than its background would have its programme turned into break
# candidates - the unrecoverable direction.  Storing the polarity per mask at
# learn time is the correct fix and needs every mask relearned.
#
# SHIPPED OFF, deliberately, and differently from the tree this came from.
# The measurement that decides it is `--polarity-ab`, which derives the gated
# and ungated brackets from one decode and scores both; until that has been
# read against the whole corpus this stays off, so an ordinary detection run
# behaves exactly as the released version does.  A channel REGRESSING under
# the gate is the signal that polarity must be stored per mask instead.
MASK_POLARITY_GATE = False
# Luminance levels the mask must exceed its ring by.  Zero, deliberately: the
# measured separation is a sign flip with a ~65-level gap, and the brackets
# were identical anywhere from -8 to +4, so this constant is not carrying the
# result.  A figure chosen to sit inside a wide flat band is not the kind of
# invented constant that has cost this file three rounds.
MASK_POLARITY_MIN = 0.0

# How much more often a pixel must be an edge just after a segment starts
# than during the rest of the programme before it is taken to be logo.
MASK_LEARN = 0.35
# A logo is a compact blob.  Its bounding box may span no more than this
# fraction of the picture, and must be at least this densely filled.
MASK_MAX_SPAN = 0.25
MASK_MIN_FILL = 0.25
# Fewest pixels a mask may have, and how decisive it must be at that size.
#
# Not MIN_CLUSTER: that governs the correlation clustering, which is a
# different measurement with different statistics, and borrowing its figure
# here threw away a real logo.  ITV's is small enough that only five pixels of
# a 160x90 analysis frame clear the learning threshold - but they clear it at
# +0.87 contrast, the most decisive of any channel measured, which is far
# better evidence than ten pixels scraping past at +0.36.  Small masks are
# allowed, on condition that they are emphatic.
MIN_MASK_PIXELS = 4
SMALL_MASK_CONTRAST = 0.60

# How far either side of the programme the junction is looked for, when a
# channel has no advert breaks to contrast against.
JUNCTION_WINDOW = 90.0

# Constants for the fallback coincidence technique - see
# coincidence_brackets().  Measured on Grimm; checked afterwards against the
# corpus (BBC One and a second no-advertising recording both stay silent).
#
# "Strong" means a black interval with a silence overlapping it.
# coincidences() scores that 2.0, or 2.5 if a scene change agrees too; 1.9
# catches both and excludes every weaker, single-signal event.
COINCIDENCE_STRONG = 1.9
# Interior evidence density - weighted score per second, strictly between a
# candidate bracket's two edges - a bracket must clear.  On Grimm the three
# real breaks scored 0.076-0.107 and every other candidate a naive pairing
# produces scored 0.063 or below.  0.07 sits in that gap, biased
# toward the real breaks' side of it: a missed break costs a manual pass, an
# invented one costs programme material that cannot be recovered.
COINCIDENCE_DENSITY = 0.07

# How near the end of the file a cut has to reach to count as recording
# padding rather than an advert break.  The PVRs pad by about three minutes at
# the start and ten at the end.
PADDING_TOLERANCE = 5.0


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------
#
# video_signals() and audio_signals() decode a recording once between
# them, and used to print nothing until they finished.  On an HD file that is
# a genuinely long silent gap - long enough that a real run was mistaken for
# a hang and nearly stopped.  ffmpeg's own "-progress" reports periodic
# key=value blocks as it runs; the helpers below turn that into a plain
# "how far through" line, printed unconditionally rather than only in
# verbose mode, because whether the tool is stuck is not a diagnostic
# question - it is the reason this exists.

def _fmt_hms(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class ChalklineError(Exception):
    """Detection failed.  Mirrors ComskipError so callers handle both alike."""


class ChalklineCancelled(ChalklineError):
    """The caller asked to stop.  Raised from inside the ffmpeg read loops."""


def _run_ffmpeg_progress(cmd, duration, label, interval=15.0,
                         progress_cb=None, cancel_cb=None, cwd=None):
    """Run an ffmpeg command, reporting progress against a known duration.

    `-progress pipe:1` gives periodic key=value blocks on stdout, distinct
    from ffmpeg's normal logging - `out_time_ms` against the known
    duration is what turns that into a percentage.  Popen rather than
    subprocess.run(), because a report printed only after the process exits
    is the same silence this exists to fix.

    stdout and stderr are merged and read as one stream, continuously,
    rather than reading stdout in the loop and deferring stderr to the end
    the way an earlier version of this function did.  That earlier version
    deadlocked for real on a recording whose decode produced enough
    repeated warnings to fill the OS pipe buffer on stderr - nothing was
    draining it while the loop waited on stdout, so the child blocked
    trying to write and the loop blocked waiting to read, forever.
    Reproduced directly with a stand-in process that writes 20,000 lines to
    stderr: the old shape hung indefinitely, needing an external `timeout`
    to kill it; reading both streams as one completed the same case in
    0.14s.  `-nostats`/`-loglevel error` on the caller's cmd reduce the
    volume but do not bound it, so they are not a fix on their own -
    genuinely messy broadcast material is exactly the case that produces
    unbounded warnings.

    Returns everything that was not a progress line, which is what
    video_signals() and audio_signals() need to parse for their
    black/scene and silence output.

    `progress_cb(percent, label)` replaces the stderr report when given, so
    the GUI can drive a progress bar off the same figures the command line
    prints.  `cancel_cb()` is polled on every line; returning True terminates
    ffmpeg and raises ChalklineCancelled.  Both default to None, which leaves
    the command-line behaviour exactly as it was.
    """
    proc = subprocess.Popen(
        cmd + ["-progress", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, cwd=cwd)
    start = time.monotonic()
    last = 0.0
    out_time = 0.0
    other_lines = []
    while True:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        if cancel_cb is not None and cancel_cb():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise ChalklineCancelled("Advert detection cancelled.")
        line = line.rstrip("\n")
        if line.startswith("out_time_ms="):
            try:
                out_time = int(line.split("=", 1)[1]) / 1_000_000.0
            except ValueError:
                pass
            continue
        if line.startswith("progress="):
            now = time.monotonic()
            if now - last >= interval or line == "progress=end":
                elapsed = now - start
                speed = out_time / elapsed if elapsed > 0 else 0.0
                pct = (min(100.0, 100.0 * out_time / duration)
                       if duration > 0 else 0.0)
                if progress_cb is not None:
                    progress_cb(pct, label)
                elif duration > 0:
                    print(f"    {label}: {pct:4.1f}% "
                          f"({_fmt_hms(out_time)} / {_fmt_hms(duration)}, "
                          f"{speed:.1f}x realtime)", file=sys.stderr)
                else:
                    print(f"    {label}: {_fmt_hms(out_time)} elapsed, "
                          f"{speed:.1f}x realtime", file=sys.stderr)
                last = now
            continue
        other_lines.append(line)
    proc.wait()
    return "\n".join(other_lines)


# ---------------------------------------------------------------------------
# Probing and signal extraction
# ---------------------------------------------------------------------------

def probe(video):
    """Duration and frame rate of the recording."""
    out = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-select_streams",
         "v:0", "-show_entries", "format=duration:stream=avg_frame_rate",
         "-of", "json", video],
        capture_output=True, text=True).stdout
    try:
        info = json.loads(out)
        duration = float(info["format"]["duration"])
        rate = info["streams"][0]["avg_frame_rate"]
        num, den = (rate.split("/") + ["1"])[:2]
        fps = float(num) / float(den) if float(den) else 25.0
    except Exception:
        duration, fps = 0.0, 25.0
    return duration, fps


# ffmpeg's mpegts muxer writes "Service01" into the SDT when nothing tells it
# otherwise, so a remuxed recording carries a name that identifies no channel
# - and, worse, the SAME name for every channel.  A logo learned under it
# would be looked up for every remuxed recording regardless of what it came
# from, quietly matching Channel 4's mask against ITV.  Rejected for exactly
# the reason program_num 1 is below: it is what a remuxer writes when the real
# identity has been thrown away.
#
# Quick Stream Fix now passes the original name through (see
# repair/stream_fix.py), but files repaired before that, and anything remuxed
# by other tools, still carry it.
_GENERIC_SERVICE = re.compile(r"^service\s*0*\d+$", re.IGNORECASE)


def channel_key(video):
    """A stable identifier for the channel a recording came from.

    Neither PVR preserves the whole picture, and they lose opposite halves of
    it.  Tvheadend remuxes to a single-service stream, so it renumbers the
    program to 1 and the original service id is gone - but it writes an SDT,
    so the channel name survives.  Jellyfin writes no SDT, so the name is
    gone - but it passes the original PAT through, so the DVB service id
    survives as the program number.

    Taking whichever is present gives a key for every recording either
    produces.  A name is not needed for a key: an opaque service id is just
    as good for looking up what was learned about a channel last time, which
    is the only thing a key is for.

    Returns something like "Sky Mix" or "sid:22272", or None if the recording
    carries neither - which is the case for a file that has been through an
    encoder.
    """
    out = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-show_programs",
         "-of", "json", video],
        capture_output=True, text=True).stdout
    try:
        programs = json.loads(out).get("programs", [])
    except Exception:
        return None
    for p in programs:
        name = (p.get("tags") or {}).get("service_name", "").strip()
        if name and not _GENERIC_SERVICE.match(name):
            return name
    for p in programs:
        num = p.get("program_num")
        # Program 1 is what a remuxer writes when it has thrown the real
        # service id away, so it identifies nothing.
        if num not in (None, 0, 1):
            return f"sid:{num}"
    return None


def main_audio_stream(video):
    """Absolute ffprobe stream index of the recording's main audio track.

    A UK broadcast .ts often carries a second audio track - audio
    description - alongside the main mix, and there is no way to be sure
    which is which just from stream index or channel count: on two files
    checked directly, both audio streams were mp2/aac_latm, stereo, tagged
    "eng", with neither carrying the generic "default" disposition ffmpeg
    exposes for other purposes.  What UK DVB streams do flag is audio
    description specifically, via the visual_impaired/descriptions
    disposition - the same signal Snipwright's own export code
    (_usable_audio_tracks() in src/export/exporter.py) already relies on
    to keep from losing AD tracks on export, reused here for the opposite
    reason: to avoid feeding one into a filter meant to read the main
    soundtrack.

    Deep-probed (30M/60M) for the same reason exporter.py's version is: a
    sparse AD track - mostly silence between narration - can read as
    "0 Hz, 0 channels" on a shallow probe.  Confirmed directly on a real
    BBC One recording, whose AD track reads exactly that way even with
    this deep probe; it is still correctly flagged as AD by disposition
    regardless, since that comes from the stream header, not from
    successfully parsing its audio parameters.

    Returns None - "let the caller fall back to the first audio stream" -
    if there are fewer than two audio streams, or if disposition flags
    don't cleanly single out exactly one non-AD stream.  A detector
    guessing wrong here would silently corrupt the very signal
    coincidence_brackets() and refine_edge() both depend on, for every
    recording that has more than one audio track - a worse failure than
    falling back to something simple whenever the disposition data is not
    unambiguous.
    """
    out = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error",
         "-analyzeduration", "30M", "-probesize", "60M",
         "-select_streams", "a",
         "-show_entries",
         "stream=index:stream_disposition=visual_impaired,descriptions",
         "-of", "default=noprint_wrappers=1", video],
        capture_output=True, text=True).stdout

    described = {}
    cur = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("index="):
            cur = line.split("=", 1)[1]
            described.setdefault(cur, False)
        elif cur is not None and (
                line.startswith("DISPOSITION:visual_impaired=1")
                or line.startswith("DISPOSITION:descriptions=1")):
            described[cur] = True

    if len(described) < 2:
        return None
    main = [idx for idx, is_ad in described.items() if not is_ad]
    return main[0] if len(main) == 1 else None


def stream_starts(video):
    """(video_start, audio_start) in container time, or (None, None).

    Needed because the two passes no longer share an ffmpeg run, and so no
    longer share a timebase.  See video_signals().
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,start_time", "-of", "json", video],
            capture_output=True, text=True).stdout
        streams = json.loads(out).get("streams", [])
    except Exception:
        return None, None

    def first(kind):
        for s in streams:
            if s.get("codec_type") == kind:
                try:
                    return float(s.get("start_time"))
                except (TypeError, ValueError):
                    return None
        return None

    return first("video"), first("audio")


def audio_signals(video, duration, verbose=False, progress_cb=None,
                  cancel_cb=None):
    """Silences, from an audio-only pass.

    Separate from the video work on purpose, and this is the whole reason the
    decode merge finally works.  An audio output in the same ffmpeg run
    perturbs the video frames: measured on u&dave with an identical filter
    graph, no audio gives frames byte-identical to the two-pass pipeline,
    while adding an audio output gives 601 vs 602 frames and a mean
    difference of 21.09/255 with 22.9% of pixels over 30 - the same signature
    that caused the second decode-merge revert.  Five ways of suppressing it
    were tried and all failed: `-fps_mode passthrough`, `-vsync 0`,
    `-max_interleave_delta 0`, `-copyts`, and routing audio through the
    filter graph.  The coupling cannot be turned off, so the audio is simply
    kept out of the video pass.

    It costs almost nothing on its own - audio decode measured at 0.84s per
    10-minute window of itv1-3, against ~15s for the video decode it lets us
    stop doing twice.  `-vn` is explicit so no video is decoded here at all.

    Audio is mapped via main_audio_stream() rather than left to ffmpeg's own
    default-stream selection, because a UK broadcast recording can carry an
    audio-description track alongside the main mix and there is no guarantee
    which one ffmpeg would pick.  BBC One reports `audio: stream 2` and stays
    correctly silent; itv1-3 reports `audio: stream 1`.
    """
    audio_stream = main_audio_stream(video)
    audio_map = f"0:{audio_stream}" if audio_stream is not None else "0:a:0"
    if verbose and audio_stream is not None:
        print(f"  audio: stream {audio_stream} identified as the main "
              f"track (an audio-description track is also present)",
              file=sys.stderr)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", video,
        "-map", audio_map, "-vn",
        "-af", f"silencedetect=n={SILENCE_DB}dB:d={SILENCE_D}",
        "-f", "null", "-",
    ]
    text = _run_ffmpeg_progress(cmd, duration, "audio signals",
                                progress_cb=progress_cb, cancel_cb=cancel_cb)
    starts = [float(t) for t in re.findall(
        r"silence_start: (-?[\d.]+)", text)]
    ends = [float(t) for t in re.findall(r"silence_end: (-?[\d.]+)", text)]
    return [(s, ends[i] if i < len(ends) else s)
            for i, s in enumerate(starts)]


def video_signals(video, duration, workdir, verbose=False, progress_cb=None,
                  cancel_cb=None):
    """Black frames, scene changes AND the analysis frames, in one decode.

    Previously two full decodes of the same video: instant_signals() for the
    black and scene events, analysis_frames() for the 160x90 frames.  One
    decode now feeds both through a split - blackdetect and scene selection
    on one branch, fps/scale on the other.

    These events do not propose breaks - on their own they fire constantly,
    at every inter-advert gap as much as at a break edge.  They exist to
    place an edge precisely once a technique has said roughly where to look.

    THE DECODE MERGE, REVERTED TWICE BEFORE THIS.  The first attempt mapped
    audio with a bare `-map 0:a`, which maps every audio stream rather than
    one, and broke BBC One.  The second fixed that but produced analysis
    frames that were genuinely misaligned - mean 24/255, a quarter of pixels
    over 30/255 - and moved u&dave's end edge by 36.82s.  That was blamed on
    ffmpeg's fps filter choosing a different sampling phase downstream of a
    split.

    That diagnosis was wrong, and the reason it stood for so long is worth
    remembering: the merged pass was compared against analysis_frames, and
    the two differed in TWO ways at once - the split, and the presence of an
    audio output.  Only the split was suspected.  Isolating them shows the
    split is innocent and the audio output is not; see audio_signals(), which
    is why silencedetect now runs on its own.

    With audio out of the way this is byte-identical to the two-pass output
    on 15 of the 18 corpus recordings.  The three that differ - itv1-2,
    itv1-3, itv1-4 - are damaged streams, all reporting "non-existing PPS 0
    referenced" and "decode_slice_header error", and they are not
    byte-reproducible against *themselves*: running the OLD pipeline twice on
    itv1-2 gives different bytes too.  Multithreaded H.264 error concealment
    is not deterministic, and `-threads 1` makes both paths agree exactly.
    The residual difference is mean 0.00/255, against 23.91/255 for the fault
    that caused the revert, and corpus runs 16, 17 and 18 all produced
    identical scores for those three recordings while it was present.

    Scene events are written to a temp file rather than piped through stdout,
    which is where -progress reports; the two would otherwise interleave into
    one unparseable stream.

    Frames come back as a read-only memmap so nothing here is obliged to hold
    the whole recording in memory.
    """
    # The scene file goes in the workdir under a BARE filename, and ffmpeg is
    # run from there.
    #
    # Its path is embedded inside the filter graph, where ffmpeg treats ':'
    # as an option separator and '\' as an escape.  A Windows temp path is
    # C:\Users\...\addetect-scenes-x.txt, so the colon ends the option and
    # every backslash is eaten:
    #
    #   Unable to parse option value "UsersPaulTempscenes.txt"
    #   Error applying option 'mode' to filter 'metadata'
    #
    # ffmpeg then never starts, no frames are written, and video_signals()
    # returns frames=None - so on Windows Chalkline could not analyse
    # anything at all.  Reported as "no logo clear enough to remember",
    # which named the wrong thing entirely.
    #
    # Escaping the path was tried and is fiddly enough to get wrong twice;
    # a bare filename has no colon and no separator to escape, so there is
    # nothing left to get wrong.  The input path is made absolute because
    # running from the workdir would otherwise break a relative one - which
    # it did, the first time this was written.
    scene_name = "addetect-scenes.txt"
    scene_path = os.path.join(workdir, scene_name)
    path = os.path.join(workdir, "frames.raw")

    graph = (f"[0:v]split=2[a][b];"
             f"[a]blackdetect=d={BLACK_D}:pic_th={BLACK_PIC}:"
             f"pix_th={BLACK_PIX},"
             f"select='gt(scene,{SCENE_TH})',"
             f"metadata=print:file={scene_name}[v1];"
             f"[b]fps={FPS},scale={GW}:{GH}[v2]")
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", os.path.abspath(video),
        "-filter_complex", graph,
        "-map", "[v1]", "-f", "null", "-",
        "-map", "[v2]", "-pix_fmt", "gray", "-f", "rawvideo",
        os.path.abspath(path), "-y",
    ]
    if verbose:
        print("  " + " ".join(cmd[:6]) + " ...", file=sys.stderr)
    text = _run_ffmpeg_progress(cmd, duration, "video signals",
                                progress_cb=progress_cb, cancel_cb=cancel_cb,
                                cwd=workdir)

    # Put these back on the timebase the rest of the detector expects.
    #
    # ffmpeg normalises output timestamps against the EARLIEST mapped stream.
    # The old combined pass mapped video and audio together, and audio always
    # starts first in these recordings, so every black and scene event was
    # referenced to the audio stream's start.  This pass maps video alone, so
    # it references video's start instead - and a transport stream does not
    # begin both at the same PTS.
    #
    # Measured: video minus audio start is +0.468s on u&dave and +0.849s on
    # itv1-2, and those are exactly the amounts by which the refined edges
    # moved (u&dave 0.49s to 0.04s mean, itv1-2 picking up a false positive
    # as its end edges shifted from +0.39 to -0.46).  Start edges did not
    # move at all, because shape brackets are indexed off np.arange(n)/FPS -
    # frame-index time, with no timestamp in it.
    #
    # silencedetect needs no such correction: audio_signals() references the
    # audio stream's start, which is what the old combined pass used too.
    vstart, astart = stream_starts(video)
    offset = 0.0
    if vstart is not None and astart is not None and vstart > astart:
        offset = vstart - astart
        if verbose:
            print(f"  timebase: video starts {offset:.3f}s after audio, "
                  f"shifting black/scene events to match", file=sys.stderr)

    scene_text = ""
    if os.path.isfile(scene_path):
        with open(scene_path, encoding="utf-8", errors="replace") as fh:
            scene_text = fh.read()
        os.unlink(scene_path)

    text = text + scene_text
    blacks = [(float(a) + offset, float(b) + offset) for a, b in re.findall(
        r"black_start:([\d.]+) black_end:([\d.]+)", text)]
    scenes = [float(t) + offset
              for t in re.findall(r"pts_time:([\d.]+)", text)]

    frames = None
    if os.path.isfile(path):
        n = os.path.getsize(path) // (GW * GH)
        if n >= 10:
            frames = np.memmap(path, dtype=np.uint8, mode="r",
                               shape=(n, GH, GW))
    return blacks, scenes, frames


# ---------------------------------------------------------------------------
# Technique one: picture shape
# ---------------------------------------------------------------------------

def rolling_min(values, window=SHAPE_WINDOW):
    """Smallest value in a window centred on each sample.

    Applied to bar widths, this recovers the true bar.  A dark scene makes the
    picture itself read as black at the edges, so a single frame can claim a
    bar that is not there; but over ten seconds at least one frame will carry
    content out to the real picture edge, and the minimum finds it.
    """
    pad = window // 2
    padded = np.pad(values, pad, mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, window)
    return np.min(view, axis=1)


def shape_track(frames, chunk=512):
    """Per-frame width and height of the active picture, in grid units.

    Black bars are found by asking which leading and trailing rows and columns
    are dark across their whole length.  This catches pillarbox (4:3 archive
    material inside a 16:9 transmission) and letterbox alike, and it is
    measured from frames that are being decoded for the logo search anyway.

    Each bar is then passed through a rolling minimum.  Without it the
    measurement is dominated by dark scenes rather than by real bars: on a
    Sky Mix recording the programme's shape fragmented across four different
    keys, which split its share of the running time so badly that the
    *adverts* became the dominant shape and every programme segment was
    reported as a break.  With it, the programme reads as the dominant shape
    for 90-100% of its frames and the breaks for 0.0%.
    """
    n = frames.shape[0]
    left = np.zeros(n, dtype=np.int16)
    right = np.zeros(n, dtype=np.int16)
    top = np.zeros(n, dtype=np.int16)
    bottom = np.zeros(n, dtype=np.int16)
    for i in range(0, n, chunk):
        block = np.asarray(frames[i:i + chunk])
        light_col = block.max(axis=1) >= BAR_LEVEL      # frames x GW
        light_row = block.max(axis=2) >= BAR_LEVEL      # frames x GH
        for j in range(block.shape[0]):
            cs = np.flatnonzero(light_col[j])
            rs = np.flatnonzero(light_row[j])
            if len(cs):
                left[i + j], right[i + j] = cs[0], GW - 1 - cs[-1]
            else:
                left[i + j] = right[i + j] = GW // 2
            if len(rs):
                top[i + j], bottom[i + j] = rs[0], GH - 1 - rs[-1]
            else:
                top[i + j] = bottom[i + j] = GH // 2
        del block, light_col, light_row
    widths = GW - rolling_min(left) - rolling_min(right)
    heights = GH - rolling_min(top) - rolling_min(bottom)
    return widths, heights


def shape_regions(widths, heights, times, tol=6):
    """Stretches whose picture shape differs from the programme's.

    The dominant shape is whichever occupies most of the recording, which is
    the programme: adverts are a small fraction of the running time.  A run
    that differs from it is a candidate break.
    """
    n = len(widths)
    if n == 0:
        return [], None

    def mode(values):
        vals, counts = np.unique((values // tol) * tol, return_counts=True)
        i = int(np.argmax(counts))
        return int(vals[i]), counts[i] / n

    dom_w, share_w = mode(widths)
    dom_h, share_h = mode(heights)
    # A recording with no settled shape at all cannot use this technique.
    if max(share_w, share_h) < 0.35:
        return [], None

    # Only a picture *larger* than the programme's counts as a change.
    #
    # The comparison has to be one-directional.  A dark scene reads as a
    # smaller picture, because the content itself stops short of the real
    # frame edge and the unlit margin is indistinguishable from a bar - on
    # Sky Mix a seventy-second dark stretch measured 90-96 units wide against
    # the programme's 114 and was bracketed as a break, putting one edge 78
    # seconds out.  Darkness can only ever shrink the measured picture, while
    # a full-frame advert inside a pillarboxed programme is always wider and a
    # 16:9 advert inside a letterboxed film is always taller.  Testing only
    # for growth keeps the real signal and discards the artefact.
    bigger = (widths > dom_w + tol) | (heights > dom_h + tol)
    return runs_of(bigger, times), (dom_w, dom_h)


# ---------------------------------------------------------------------------
# Technique two: channel logo
# ---------------------------------------------------------------------------

def edge_chunk(block):
    """Edge map of one chunk of frames, kept small deliberately.

    The int16 promotion happens per chunk and is dropped immediately.  Doing
    this to a whole recording at once is what previously exhausted memory.
    """
    a = block.astype(np.int16)
    gx = np.abs(np.diff(a, axis=2))[:, :-1, :]
    gy = np.abs(np.diff(a, axis=1))[:, :, :-1]
    return (gx + gy) > EDGE_LEVEL


def corner_boxes(eh, ew):
    """The four corner search boxes, inset from the frame edge.

    Inset because the logo can sit inside the picture rather than on the
    frame edge - on one Sky Mix recording it is 17-23% across, on others it
    sits over the pillarbox bar - and because centre-frame captions such as
    BBC One's "Now on BBC One" must stay outside the search.
    """
    bh, bw = int(eh * 0.30), int(ew * 0.36)
    i = 2
    return {
        "TL": (slice(i, i + bh), slice(i, i + bw)),
        "TR": (slice(i, i + bh), slice(ew - i - bw, ew - i)),
        "BL": (slice(eh - i - bh, eh - i), slice(i, i + bw)),
        "BR": (slice(eh - i - bh, eh - i), slice(ew - i - bw, ew - i)),
    }


def logo_track(frames, times, chunk=512, verbose=False):
    """Find the logo, and say for each frame whether it is present.

    A logo is not merely a persistent edge - a locked-off camera shot is
    persistent too.  What distinguishes a logo is that its pixels switch on
    and off *together*, because they are one object being composited over
    changing pictures.  Selecting by temporal coincidence rather than by
    persistence is what separates a real logo from picture detail: it forms
    clusters of 18-38 pixels on channels that carry one, and only 3-6 on BBC
    One, which carries none.

    Every viable corner is returned rather than just one.  Choosing between
    them here is not possible: the obvious measure is cluster size, and it is
    wrong.  On a Parks and Recreation recording the bottom-left corner formed
    a 234-pixel cluster out of ordinary picture detail while the real logo,
    top-left, was 58 pixels - so the largest cluster won, proposed a
    1460-second "break", and the recording came back with nothing found at
    all.  Which corner carries the logo is settled by the caller, on the
    evidence its brackets turn out to be supported by.
    """
    n = frames.shape[0]
    eh, ew = GH - 1, GW - 1
    boxes = corner_boxes(eh, ew)

    # Accumulate each corner's edge history as bit-packed rows, so the whole
    # recording's edge map for a corner costs about an eighth of a byte per
    # pixel per frame rather than a full byte.
    history = {c: [] for c in boxes}
    for i in range(0, n, chunk):
        e = edge_chunk(np.asarray(frames[i:i + chunk]))
        for c, (ys, xs) in boxes.items():
            history[c].append(np.packbits(
                e[:, ys, xs].reshape(e.shape[0], -1), axis=1))
        del e

    found = []
    for c, (ys, xs) in boxes.items():
        bh = ys.stop - ys.start
        bw = xs.stop - xs.start
        packed = np.concatenate(history[c], axis=0)
        npix = bh * bw
        sub = np.unpackbits(packed, axis=1)[:, :npix].astype(np.float32)
        del packed

        # A logo pixel is on for a good share of the recording but not all of
        # it, since it drops through every break.
        p = sub.mean(axis=0)
        cand = np.flatnonzero((p >= 0.05) & (p <= 0.75))
        if len(cand) < MIN_CLUSTER:
            del sub
            continue

        X = sub[:, cand]
        del sub
        Xc = X - X.mean(axis=0)
        sd = Xc.std(axis=0)
        sd[sd == 0] = 1.0
        corr = (Xc.T @ Xc) / (n * np.outer(sd, sd))
        del Xc
        seed = int(np.argmax((corr >= CLUSTER_CORR).sum(axis=1)))
        clust = np.flatnonzero(corr[seed] >= CLUSTER_CORR)
        del corr
        if len(clust) < MIN_CLUSTER:
            del X
            continue

        # Reject bar edges before they can become logos.
        cy, cx = np.divmod(cand[clust], bw)
        cw = int(cx.max() - cx.min() + 1)
        ch = int(cy.max() - cy.min() + 1)
        if min(cw, ch) < MIN_CLUSTER_THICKNESS or \
                max(cw, ch) > MAX_CLUSTER_ELONGATION * max(min(cw, ch), 1):
            if verbose:
                print(f"    corner {c}: cluster {len(clust)}px is {cw}x{ch} - "
                      f"a line, not a logo", file=sys.stderr)
            del X
            continue

        track = X[:, clust].mean(axis=1)
        del X
        on = track > 0.5
        if verbose:
            print(f"    corner {c}: cluster {len(clust)}px, "
                  f"on {100 * on.mean():.1f}%", file=sys.stderr)
        # A logo that is on almost always or almost never is not being
        # modulated by breaks and cannot bracket one.
        if not (0.30 <= on.mean() <= 0.97):
            continue
        found.append((c, len(clust), on))

    return found


# ---------------------------------------------------------------------------
# Turning tracks into brackets
# ---------------------------------------------------------------------------

def sustained(flag, times, min_len):
    """Drop true-runs shorter than `min_len`.

    Applied to the logo track before its gaps are taken.  A single advert
    carrying the channel's own branding lights the logo detector for a few
    seconds in the middle of a break and splits the logo-off run in two - on
    5USA a 24-second blip at 3062s cut the third break 108 seconds short, and
    Comskip's own log shows it failing on that same break the same way.  A
    genuine programme segment holds its logo for minutes, so requiring the
    logo to persist before it counts removes the blips without touching
    anything real.
    """
    out = np.array(flag, dtype=bool, copy=True)
    idx = np.flatnonzero(out)
    if len(idx) == 0:
        return out
    start = prev = idx[0]
    for i in list(idx[1:]) + [None]:
        if i is None or i != prev + 1:
            if times[prev] - times[start] < min_len:
                out[start:prev + 1] = False
            if i is not None:
                start = i
        if i is not None:
            prev = i
    return out


def logo_brackets(logo_on, times):
    """Propose break brackets from the logo track, with a support count.

    There is no single right value for how long the logo must persist before
    a stretch counts as programme, and pretending otherwise costs real
    detections.  Swept jointly against the two corpus recordings that rely on
    the logo, 5USA needs 26 to 30 seconds - below it a 24-second advert
    carrying channel branding splits its third break and cuts it 108 seconds
    short - while U&Dave needs 12 or less, because above that a genuine
    98-second stretch of flickering logo is erased, the bracket grows past
    the plausible-length ceiling and the break is lost entirely.  No value
    satisfies both, and no combination of gap-closing and persistence gave a
    clean sweep.

    So the threshold is not chosen.  Brackets are proposed at every level and
    each is scored by how many levels agree with it.  A real break is
    bracketed at all four; an artefact of over-filtering appears at only the
    highest one or two.  Requiring agreement from most of the levels keeps
    every true break in both recordings and discards every false one.
    """
    per = {}
    for level in LOGO_LEVELS:
        per[level] = runs_of(~sustained(logo_on, times, level), times)
    candidates = sorted({span for spans in per.values() for span in spans})
    out = []
    for lo, hi in candidates:
        support = sum(
            1 for level in LOGO_LEVELS
            if any(min(hi, d) - max(lo, c) > 0.5 * (hi - lo)
                   for c, d in per[level]))
        if support >= MIN_LEVEL_SUPPORT:
            out.append((lo, hi, support))
    return out


def runs_of(flag, times, min_len=MIN_BREAK, merge_gap=8.0):
    """Contiguous stretches where `flag` is true, in seconds.

    Short interruptions are bridged first.  A single advert carrying the
    channel's own branding, or one frame of the programme's shape appearing
    mid-break, must not split a break in two - that is what caused two of the
    corpus breaks to be bracketed 100 seconds short.
    """
    n = len(flag)
    if n == 0:
        return []
    idx = np.flatnonzero(flag)
    if len(idx) == 0:
        return []
    spans = []
    start = idx[0]
    prev = idx[0]
    for i in idx[1:]:
        if times[i] - times[prev] > merge_gap:
            spans.append((start, prev))
            start = i
        prev = i
    spans.append((start, prev))
    out = []
    for a, b in spans:
        t0, t1 = times[a], times[b]
        if t1 - t0 >= min_len:
            out.append((float(t0), float(t1)))
    return out


# ---------------------------------------------------------------------------
# Edge refinement
# ---------------------------------------------------------------------------

def coincidences(blacks, silences, scenes, window=0.5):
    """Instants where black, silence and a scene change occur together.

    Counted jointly rather than summed per signal.  A single black-and-silent
    frame trips all three detectors, so adding their scores independently
    triple-counts one event and makes a common inter-advert gap look like
    overwhelming evidence.
    """
    events = []
    for a, b in blacks:
        mid = (a + b) / 2.0
        score = 1.0
        if any(s <= mid + window and e >= mid - window for s, e in silences):
            score += 1.0
        if any(abs(t - mid) <= window for t in scenes):
            score += 0.5
        events.append((mid, score))
    for s, e in silences:
        mid = (s + e) / 2.0
        if any(abs(mid - m) <= window for m, _ in events):
            continue
        score = 0.6
        if any(abs(t - mid) <= window for t in scenes):
            score += 0.4
        events.append((mid, score))
    events.sort()
    return events


def refine_edge(t, events, bracket, edge, search=20.0):
    """Move a bracket edge onto the nearest strong instant evidence.

    Searching *inward* from the bracket only.  The bracket produced by either
    technique reliably overshoots - the logo drops before the break begins and
    returns after it ends - so the true edge lies inside, and a symmetric
    search would as readily move the edge further out.
    """
    lo, hi = bracket
    if edge == "start":
        window = [(m, s) for m, s in events if t - 2.0 <= m <= t + search]
    else:
        window = [(m, s) for m, s in events if t - search <= m <= t + 2.0]
    window = [(m, s) for m, s in window if lo - 2.0 <= m <= hi + 2.0]
    if not window:
        return t, 0.0
    # Trade evidence strength off against distance from the bracket edge,
    # rather than taking the strongest and using distance only to break ties.
    #
    # The bracket is a strong prior and the true edge sits near it, so an
    # event further inside needs to be substantially better to be worth
    # moving to.  A hard threshold gets this wrong: at the end of ITV4's
    # first break the true edge is a silence with no black frame, scoring
    # 0.60, while an inter-advert gap eleven seconds inside it has a full
    # black-and-silence coincidence scoring 1.00.  Ranking by strength picked
    # the gap and put the edge 10.9 seconds early; the penalty picks the
    # silence, which is 0.1 seconds from the hand-corrected boundary.
    best = max(window, key=lambda ms: ms[1] - EDGE_DISTANCE_COST * abs(ms[0] - t))
    return best[0], best[1]


def build_breaks(brackets, events, analysis_end, short_ok=(), verbose=False,
                 duration=0.0):
    """Refine every bracket into a break, and choose between the overlaps.

    `short_ok` lists the raw (lo, hi) spans allowed down to
    MIN_BREAK_STRONG rather than MIN_BREAK - in practice the brackets a
    remembered persistent mask proposed.  Those also have to clear a
    stricter evidence test than the rest: both refined edges must show
    something, not just one.  See MIN_BREAK_STRONG.

    Brackets compete rather than merge.  The same break is often proposed
    several times over, by different persistence levels or by both techniques
    at once, and taking the union of overlapping proposals would always yield
    the longest and therefore the loosest answer.  Ranking them by how many
    levels support them and then by the evidence found at their refined edges
    picks the best-attested version instead.

    A bracket whose raw edge sits at (or essentially at) `analysis_end` is
    exempt from MAX_BREAK.  That ceiling exists to catch a runaway false
    candidate - picture noise misread as a change that never actually ends -
    not a genuine, permanent change in content with nothing on the far side
    of it to have overshot into.  Confirmed directly on itv1-4: the film
    ends and something else follows for the rest of the recording - what,
    is not this detector's business to know or assume, see
    programme_anchors() and anchor_gaps() for the same principle applied to
    what counts as a candidate in the first place.  shape found the
    transition correctly, refining to within 0.7s of the true end, but its
    raw bracket ran to the recording's own last analysed frame, making it
    826s long, and was discarded by the length ceiling alone - with nothing
    else to compete with it, a mask candidate 97 seconds later won instead,
    and the whole tail was kept as if it were still the film.

    `analysis_end` deliberately means the last timestamp this detector
    actually looked at (`times[-1]` in detect(), i.e. `(n-1)/FPS` for the n
    analysis frames decoded) - not the recording's probed container
    duration, which is a different number measured a different way and is
    not guaranteed to agree with it.  The first version of this fix used
    the probed duration and silently failed on the very file it was
    written for: shape's raw bracket is bounded by the analysis grid's own
    last sample, but the container duration ffprobe reports can sit more
    than a second beyond that for a real broadcast recording, so the
    at-edge check evaluated false and the fix never engaged.  Confirmed by
    running the real fix against a real corpus batch and watching it not
    change itv1-4's output at all before this was caught.  Compare against
    the same reference the brackets themselves are built from, not a
    second, independent measurement of "how long is this file".

    The 1.0s tolerance is for ordinary frame-boundary rounding, not a real
    design choice - a bracket landing meaningfully short of either edge
    should still be judged the normal way.
    """
    scored = []
    short_ok = {(round(lo, 3), round(hi, 3)) for lo, hi in short_ok}

    # Why each bracket did not become a break.  Every `continue` below is a
    # decision the log used to swallow: a verbose run reported which
    # techniques proposed how many brackets, and then the breaks that came
    # out, with nothing in between.  Working out why a bracket vanished
    # meant inferring it from absences, which cost two rounds of guessing on
    # a U&Dave recording before the answer turned out to be visible in the
    # numbers all along.
    def note(lo, hi, support, reason):
        if verbose:
            print(f"    dropped {lo:8.2f} - {hi:8.2f} ({hi - lo:6.1f}s, "
                  f"support {support}): {reason}", file=sys.stderr)

    for lo, hi, support in brackets:
        # A bracket a remembered mask proposed may be shorter, because the
        # mask is evidence learned from this channel specifically rather
        # than inferred from the picture on the spot.
        is_short_ok = (round(lo, 3), round(hi, 3)) in short_ok
        floor = MIN_BREAK_STRONG if is_short_ok else MIN_BREAK
        if hi - lo < floor:
            note(lo, hi, support, f"shorter than {floor:.0f}s")
            continue
        at_edge = (lo <= 1.0) or \
            (analysis_end > 0 and hi >= analysis_end - 1.0)
        a, sa = refine_edge(lo, events, (lo, hi), "start")
        b, sb = refine_edge(hi, events, (lo, hi), "end")
        if b - a < floor:
            note(lo, hi, support,
                 f"refined to {b - a:.1f}s, under {floor:.0f}s")
            continue
        # The boundary exemption is bounded.  Measured against the analysed
        # span rather than the container duration, for the same reason
        # at_edge is - they are different numbers measured different ways,
        # and mixing them would make the share depend on which one happened
        # to be larger.  See MAX_BOUNDARY_SHARE.
        span = analysis_end if analysis_end > 0 else (duration or 0.0)
        if (at_edge and span > 0
                and (b - a) > MAX_BOUNDARY_SHARE * span):
            note(lo, hi, support,
                 f"refined to {b - a:.1f}s, "
                 f"{100.0 * (b - a) / span:.0f}% of the recording - a bracket "
                 f"at an edge may exceed {MAX_BREAK:.0f}s but not swallow "
                 f"the programme")
            continue
        if b - a > MAX_BREAK and not at_edge:
            note(lo, hi, support,
                 f"refined to {b - a:.1f}s, over {MAX_BREAK:.0f}s "
                 f"and not at an edge")
            continue
        # The recording's own start and end are junctions in their own
        # right: nothing precedes the first frame and nothing follows the
        # last, so an edge that sits on one needs no black or silence to
        # prove it is a boundary.  Without this, a recording that opens
        # mid-advert-break is kept in full whenever one advert cuts
        # straight into the next with no black between - measured on a
        # U&Dave recording where brackets of 161s and 193s both sat at
        # time zero and both were discarded for having no evidence, and
        # 291 seconds of adverts were kept as programme.
        #
        # Deliberately NOT counted towards the evidence score below.  This
        # decides whether a bracket is admitted, not how it ranks against
        # an overlapping one, so no existing competition is reordered and
        # a corpus score can only change by a bracket newly admitted.
        has_start = sa > 0.0 or lo <= 1.0
        has_end = sb > 0.0 or (analysis_end > 0
                               and hi >= analysis_end - 1.0)
        # A break needs evidence at at least one end, or it is a shape or
        # logo artefact rather than a junction.
        if not has_start and not has_end:
            note(lo, hi, support, "no evidence at either refined edge")
            continue
        # A short break has to do better than that: both ends must show
        # something.  A 40-second gap is short enough to be an ordinary dark
        # or quiet passage of programme, and one lucky edge is not enough to
        # tell those apart - the length itself was carrying that weight for
        # every other bracket here.
        if b - a < MIN_BREAK and not (has_start and has_end):
            note(lo, hi, support,
                 f"short break with evidence at one edge only "
                 f"(start {sa:.2f}, end {sb:.2f})")
            continue
        if verbose:
            edge = ""
            if sa <= 0.0 and has_start:
                edge = " [start at recording boundary]"
            elif sb <= 0.0 and has_end:
                edge = " [end at recording boundary]"
            print(f"    kept    {lo:8.2f} - {hi:8.2f} -> {a:8.2f} - {b:8.2f} "
                  f"({b - a:6.1f}s, support {support}, "
                  f"evidence {sa:.2f}+{sb:.2f}){edge}", file=sys.stderr)
        scored.append((support, sa + sb, a, b))
    scored.sort(reverse=True)
    chosen = []
    for support, evidence, a, b in scored:
        clash = next(((c, d) for c, d in chosen
                      if min(b, d) - max(a, c) > 0), None)
        if clash is not None:
            # Not a fault - see the note above about brackets competing
            # rather than merging - but it is the outcome hardest to work
            # out from the outside, because the losing bracket leaves no
            # trace at all in the result.
            if verbose:
                print(f"    lost    {a:8.2f} - {b:8.2f} (support {support}, "
                      f"evidence {evidence:.2f}) overlaps kept "
                      f"{clash[0]:.2f} - {clash[1]:.2f}", file=sys.stderr)
            continue
        chosen.append((a, b))
    return snap_to_edges(sorted(chosen), analysis_end, duration)


# How close to the start or end of the recording a break has to land before
# it is taken to reach it.
#
# A refined edge lands on the nearest strong evidence, which for a break
# running to the end of a recording is the last black frame - and a
# broadcast recording usually carries a fraction of a second beyond that.
# The remainder is then kept as a scene, and a scene of four frames is never
# something anyone wanted: it exports as a broken fragment.  Observed on a
# U&Dave recording, where a break refined to 42:59.16 of a 43:00.03 file and
# left thirteen frames stranded behind it.
#
# Two seconds is well below the shortest real scene and well above the
# fraction of a second at issue.
EDGE_SNAP = 2.0


def snap_to_edges(breaks, analysis_end, duration=0.0):
    """Extend a break that all but reaches the start or end of the recording.

    Only ever lengthens a break, and only into a gap too short to be a scene,
    so it cannot swallow programme.  A break that genuinely stops short of
    the end by any usable amount is left exactly where it was.

    The tail is closed against `duration`, not `analysis_end`.  Those are two
    different numbers - see build_breaks() on why `analysis_end` is the last
    frame this detector looked at rather than the container's probed length -
    and the fragment being removed here lives in the gap between them.  The
    first version snapped to `analysis_end`, which on the recording it was
    written for was already exactly where the break ended, so it did nothing
    whatever and the thirteen-frame scene survived untouched.
    """
    if not breaks:
        return breaks
    out = [list(b) for b in breaks]
    if out[0][0] <= EDGE_SNAP:
        out[0][0] = 0.0
    if analysis_end > 0 and out[-1][1] >= analysis_end - EDGE_SNAP:
        out[-1][1] = max(duration, analysis_end)
    return [tuple(b) for b in out]


# ---------------------------------------------------------------------------
# Technique three: coincidence density (fallback only)
# ---------------------------------------------------------------------------

def coincidence_brackets(events):
    """Propose brackets from coincidence density alone.

    For a recording neither shape nor logo can say anything about.  Grimm is
    16:9 throughout, so the shape technique has no change to find, and no
    usable logo has been found either by the corner search or by a
    remembered mask.  Both established techniques correctly report nothing -
    but nothing is the wrong answer here, because three real breaks exist.

    What is left is the black/silence/scene coincidence events themselves.
    Pairing every "strong" one - a black interval with a silence overlapping
    it, see COINCIDENCE_STRONG - with the next in length bounds finds the
    three real breaks, but also finds spans of ordinary programme that
    happen to sit the right distance apart: 217.77-462.99, and worse,
    1099.53-1516.59 - the gap between two real breaks, which naive pairing
    reads as a candidate in its own right because it is within MIN_BREAK and
    MAX_BREAK exactly as a break would be.

    The distinction is what happens *inside* the candidate.  A real break is
    a string of separate adverts, and the join between one advert and the
    next keeps tripping the same detectors that mark the break's own edges -
    just not quite hard enough to pass the "strong" bar itself.  A stretch of
    programme rarely does this more than once or twice, dialogue pauses
    aside.  Measured on Grimm, the interior score per second is 0.076-0.107
    for the three real breaks and 0.063 or below for every false candidate a
    naive pairing produces - a clean gap.  Checked afterwards against the
    corpus: BBC One and a second no-advertising recording (BBC Three) both
    stay silent, and the other four corpus recordings cannot be reached by
    this at all, since shape or logo already proposes brackets on every one
    of them before this is ever invoked.

    This must never be allowed to compete with shape or logo.  It is invoked
    only when neither of them proposes anything at all - see detect() -
    specifically so it can never distort a recording either technique
    already handles correctly.  Brackets are returned at full support, the
    same as shape: there is no multi-level agreement to score here the way
    logo_brackets() has, so a candidate that has already cleared the density
    gate is not weighed against anything weaker.
    """
    strong = sorted(t for t, s in events if s >= COINCIDENCE_STRONG)
    out = []
    for i, lo in enumerate(strong):
        for hi in strong[i + 1:]:
            length = hi - lo
            if length > MAX_BREAK:
                break  # strong is sorted, so nothing further is shorter
            if length < MIN_BREAK:
                continue
            interior = sum(s for t, s in events if lo < t < hi)
            if interior / length >= COINCIDENCE_DENSITY:
                out.append((lo, hi, len(LOGO_LEVELS)))
    return out


# ---------------------------------------------------------------------------
# Technique four: sample aspect ratio (always tried, not a fallback)
# ---------------------------------------------------------------------------

def _sar_at(video, t):
    """The SAR of a one-second clip extracted fresh at `t`, or None.

    Isolation is load-bearing and must not be optimised away.  Probing the
    source with `ffprobe -read_intervals` gives a *false negative* across a
    real break boundary - a clean, constant reading where the ratio actually
    changes; see CLAUDE.md, "Fixed: a fourth technique".  The value only
    differs once the moment is extracted into its own small file, with no
    seeking involved in the probe itself.  Two processes a sample is the
    price of a reading that can be trusted.

    `N/A` and zero-valued ratios are ffprobe saying it does not know.  They
    are passed through rather than filtered, deliberately.

    Filtering them was tried and reverted.  It looked obviously right - a
    reading that means nothing should not become a SAR *state* - and it did
    remove three spurious brackets sitting in plain programme (itv1
    2390-2470, skymix2 1380-1460, tptv-2 1610-1690, the last of which was a
    scored false positive).  But measured across the corpus it also erodes
    real ones: it deleted legend's 1340-1460 entirely, which sits inside a
    genuine break at 1259-1556, and moved tptv's first bracket from 970 to
    980 against a truth of 972.24, taking that recording from 7.88s to
    11.23s mean.

    The reason appears to be that `N/A` is not noise.  A one-second clip
    spanning a stream discontinuity cannot report a ratio - and
    discontinuities happen at junctions, which is exactly where breaks
    begin and end.  So these readings mark real edges as well as scattering
    through programme, and suppressing them costs signal.  Whether that can
    be exploited deliberately is an open question; suppressing it blindly is
    not the answer.
    """
    clip_fd, clip_path = tempfile.mkstemp(prefix="addetect-sar-", suffix=".ts")
    os.close(clip_fd)
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", str(t), "-i", video, "-t", "1",
             "-c", "copy", clip_path],
            capture_output=True)
        sar = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=sample_aspect_ratio",
             "-of", "default=noprint_wrappers=1:nokey=1", clip_path],
            capture_output=True, text=True).stdout.strip()
    finally:
        if os.path.isfile(clip_path):
            os.unlink(clip_path)
    if not sar:
        return None
    # A clip this short can carry the same value twice, from a
    # program-and-stream listing quirk seen elsewhere in this file's ffprobe
    # output - take the first, they never disagree.
    return sar.splitlines()[0].strip()


def sar_track(video, duration, interval=10.0, coarse=60.0, progress_cb=None,
              cancel_cb=None):
    """Sample aspect ratio, sampled at intervals rather than every frame.

    The first version of this read every decoded frame via one linear
    ffprobe pass - measured at 20 minutes on a real 3-hour HD recording,
    which is a full decode of the same file stacked on top of
    video_signals(), already a full decode in its own right.  Frame-level
    ffprobe output requires the same decode work that pass does; there is no
    cheaper way to read it per
    frame.

    But per-frame precision was never needed.  Every real break in the
    corpus runs well over a minute, so a transition sampled every ten
    seconds is still caught with several samples to spare, and the
    resulting edge - off by at most one sampling interval - gets refined
    against real black/silence/scene evidence exactly like every other
    technique's raw bracket, the same way a corner or shape bracket's own
    six-to-twelve-second overshoot already gets corrected. Sampling this
    sparsely turns an unavoidable full decode into a few hundred near-instant
    stream-copy extractions instead.

    Each sample extracts a one-second, stream-copied (not re-encoded) clip
    with ffmpeg's own `-ss` seeking - copying packets costs nothing like
    decoding them - and reads that tiny clip's own declared SAR fresh, the
    same isolated-extraction method that first confirmed this mechanism was
    real and is proven reliable, unlike seeking within an already-open
    probe (see the false negative recorded in CLAUDE.md under "Fixed: a
    fourth technique").

    The schedule is two-phase, because a uniform 10s sweep spends nearly all
    of its effort confirming that nothing changed.  Measured on itv1-3: 1038
    samples, 2076 process spawns, 154s - 21% of that recording's entire
    runtime - to establish that the ratio is 1:1 from beginning to end.  A
    coarse sweep followed by refinement only where the reading differs costs
    about a sixth of that on a recording whose ratio never changes, and the
    brackets are identical, because bracket-forming only ever looks at the
    spacing of non-dominant samples and every one of those ends up inside a
    refined span.
    """
    if duration <= 0:
        return []

    samples = {}
    last_report = 0.0
    if progress_cb is not None:
        progress_cb(0.0, "sample aspect ratio")
    else:
        print(f"    sample aspect ratio:  0.0% (0:00 / {_fmt_hms(duration)})",
              file=sys.stderr)

    # Pass one: a uniform sweep at `coarse`.  Nothing shorter than
    # MIN_BREAK can produce a bracket, so a sweep at 60s cannot miss a
    # region that would have become one - every such region is at least
    # 75s long and so contains at least one coarse sample.
    t = 0.0
    while t < duration:
        # Checked per sample rather than per pass: this loop is thousands of
        # short subprocess pairs and can run for a minute or more, so a
        # cancel checked only between passes would feel unresponsive.
        if cancel_cb is not None and cancel_cb():
            raise ChalklineCancelled("Advert detection cancelled.")
        s = _sar_at(video, t)
        if s:
            samples[round(t, 3)] = s
        if t - last_report >= 15.0:
            pct = min(100.0, 100.0 * t / duration)
            if progress_cb is not None:
                progress_cb(pct, "sample aspect ratio")
            else:
                print(f"    sample aspect ratio: {pct:4.1f}% "
                      f"({_fmt_hms(t)} / {_fmt_hms(duration)})",
                      file=sys.stderr)
            last_report = t
        t += coarse

    # Pass two: refine to `interval` only around readings that differ from
    # the dominant one.  The bracket-forming code only ever looks at the
    # spacing of *non-dominant* samples - dominant ones are simply the gaps
    # between them - so sampling coarsely across unchanging stretches costs
    # nothing in accuracy while costing a great deal less in time.
    #
    # The margin is TWO coarse steps either side, not one.  One is not
    # enough, and Talking Pictures TV proved it: a non-dominant region's
    # readings flicker back to the dominant value now and then - which is
    # why sar_brackets() bridges with merge_gap at all - and a coarse sample
    # landing on a flicker reads as dominant, so the region looks shorter
    # than it is.  On tptv the coarse samples at 2520 and 2640 both landed
    # that way, leaving only the one at 2580 flagged; refinement then ran
    # 2520-2630 and bracketed (2530, 2630) where the uniform sweep gives
    # (2510, 2650).  Two steps recovers it, and three changes nothing
    # further, so this is a plateau rather than a fitted value.
    if samples:
        counts = {}
        for s in samples.values():
            counts[s] = counts.get(s, 0) + 1
        dominant = max(counts, key=counts.get)
        spans = []
        for at in sorted(t for t, s in samples.items() if s != dominant):
            lo, hi = at - 2.0 * coarse, at + 2.0 * coarse
            if spans and lo <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
            else:
                spans.append((lo, hi))
        if spans and progress_cb is None:
            print(f"    sample aspect ratio: refining {len(spans)} "
                  f"span(s) at {interval:.0f}s", file=sys.stderr)
        for lo, hi in spans:
            u = max(0.0, lo)
            stop = min(hi, duration)
            while u < stop:
                if cancel_cb is not None and cancel_cb():
                    raise ChalklineCancelled("Advert detection cancelled.")
                key = round(u, 3)
                if key not in samples:
                    s = _sar_at(video, u)
                    if s:
                        samples[key] = s
                u += interval

    if progress_cb is not None:
        progress_cb(100.0, "sample aspect ratio")
    else:
        print(f"    sample aspect ratio: 100.0% ({_fmt_hms(duration)} / "
              f"{_fmt_hms(duration)}, {len(samples)} samples)",
              file=sys.stderr)
    return sorted(samples.items())


def sar_brackets(track, merge_gap=20.0):
    """Propose brackets from a stretch at a non-dominant SAR.

    Found on Talking Pictures TV, whose advert material is mastered at a
    different sample aspect ratio from its programme - 24:17 (4:3) against
    32:17 (16:9) - on an unchanging pixel grid throughout.  Nothing in the
    decoded picture itself differs: no letterbox or pillarbox bar appears in
    either state, confirmed both by eye and by extracting the actual pixels
    and reading their size.  shape_track() cannot see this by construction,
    since video_signals() decodes at a fixed size that discards source SAR
    before shape_track() ever runs - this is a real blind spot in that
    technique, not a tuning gap.

    Scored on the one recording this exists for: found 3/3, false 0, 6/6
    edges within 5s, mean 1.10s in isolation - against the corner search's
    18.55s on the same file.  `merge_gap` defaults to 20s rather than
    runs_of()'s usual 8s, because sar_track() samples every 10s rather
    than every frame - at the default gap, every consecutive sample of the
    same run would look like its own isolated one-sample span, since the
    ordinary spacing between samples already exceeds it.  This still holds
    now that sar_track() sweeps coarsely and refines: every non-dominant
    sample falls inside a refined span, so the ones this function actually
    looks at are always 10s apart.  20s comfortably
    bridges consecutive same-state samples while staying far short of any
    real break's length, so it cannot bridge across a genuine gap between
    two separate breaks.

    Gated on shape, and on nothing else.  It is *not* gated the way
    coincidence_brackets() is: gating behind "brackets is empty" would never
    reach Talking Pictures TV's own failure, since the corner search
    proposes fifteen brackets there - just badly wrong ones.  The case this
    exists for is a recording where the existing techniques propose
    brackets, only the wrong ones.

    But it is a fallback for *shape* specifically, because shape's blindness
    to it is the whole reason it exists (see above).  When shape proposes
    brackets it is not blind, both techniques are reading the same 4:3/16:9
    switch, and this one adds nothing: measured on itv1, legend, more4,
    rewindtv and skymix, each of which proposes shape brackets, proposes
    exactly one SAR bracket, and scores identically with that bracket
    removed.  All three TPTV recordings propose no shape brackets at all.

    That gate matters because this is the only technique with a decode pass
    of its own - 154s on itv1-3, 21% of that recording's runtime - earning
    its keep on three recordings out of eighteen.

    Single-recording validation only.  Not yet checked against the rest of
    the corpus - see CLAUDE.md, "Measured: SAR change..." - and BBC One is
    the load-bearing test, as it always is for a new technique: a
    recording with no advertising had better show no transitions worth
    pairing.
    """
    if not track:
        return []
    times = np.array([t for t, _s in track])
    sars = [s for _t, s in track]

    # Vote by the time each sample represents, not by sample count.
    # sar_track() no longer samples uniformly - it sweeps coarsely and
    # refines only where the ratio changes - so a plain count would weight
    # the refined stretches several times over and could hand "dominant" to
    # the advert material on a recording with enough of it, inverting the
    # test and bracketing the programme instead.  Each sample stands for the
    # span up to the next one, which makes the vote independent of the
    # schedule; on a uniform track every weight is equal and this is exactly
    # the old count.
    gaps = np.diff(times)
    weights = np.append(gaps, gaps[-1] if len(gaps) else 1.0)
    counts = {}
    for s, w in zip(sars, weights):
        counts[s] = counts.get(s, 0.0) + float(w)
    dominant = max(counts, key=counts.get)
    flag = np.array([s != dominant for s in sars], dtype=bool)
    return runs_of(flag, times, merge_gap=merge_gap)


# ---------------------------------------------------------------------------
# Technique five: programme anchors (always tried, not a fallback)
# ---------------------------------------------------------------------------

def programme_anchors(widths, heights, times, dominant, tol=6):
    """Confident programme runs - the inverse of shape_regions()'s own
    "bigger than dominant" test, using the exact same tolerance.

    Comskip's own strategy for its aspect-ratio signal, reverse-engineered
    from its logs on itv1-3 and adapted here to this file's shape signal -
    see CLAUDE.md, "Comskip comparison".  A long, settled run where the
    picture matches the programme's own dominant shape is confident
    evidence that the whole run is programme, not that any particular
    moment inside it is - the same distinction shape_regions() draws when
    it looks for growth beyond the dominant shape rather than any
    departure from it.
    """
    dom_w, dom_h = dominant
    bigger = (widths > dom_w + tol) | (heights > dom_h + tol)
    return runs_of(~bigger, times)


def anchor_gaps(anchors):
    """Every gap between two consecutive programme anchors is one whole
    break candidate, however fragmented the evidence inside it would
    otherwise look to the other techniques.

    This is what lets a break survive being internally heterogeneous.
    `build_breaks()` still refines each gap's edges against real
    black/silence/scene evidence and still competes it against whatever
    shape, logo, mask and SAR propose directly for the same span - this
    only changes what gets offered into that competition, not how the
    competition itself is judged.

    Validated standalone against all four ITV1 films in the corpus before
    being wired into `detect()` at all:

        itv1     found 6/6, false 0, mean  1.84s (was 5/6, false 2, 33.19s)
        itv1-2   found 5/5, false 0, mean  1.33s (was 5/5, false 0,  8.91s)
        itv1-3   found 5/5, false 0, mean  1.62s (was 4/5, false 3, 28.65s)
        itv1-4   found 5/5, false 0, mean  1.20s (was 5/5, false 0, 12.69s)

    That validation was not enough on its own.  Wired in as a plain peer
    bracket source with full support, itv1-2/3/4 came back **completely
    unchanged** from the figures above the "was" - the anchor brackets were
    computed correctly and simply lost `build_breaks()`'s evidence
    competition every time they overlapped a shape bracket for the same
    span, because a narrow, wrong shape fragment's own edges often coincide
    with locally stronger black/silence/scene evidence than a wide, correct
    anchor's edges do.  Confirmed directly with a bracket trace on itv1-3:
    the anchor's refined edges were the accurate ones in both problem
    windows and lost anyway, 3.10 evidence against a wrong fragment's 3.50,
    2.60 against 3.00.

    The actual fix lives in `detect()`, not here: any shape bracket an
    anchor overlaps is suppressed before the competing pool is built,
    since it is derived from the same shape_track() measurement and is a
    fragment of the same signal the anchor already resolved completely,
    not independent evidence.  With that in place, real, in-pipeline
    results:

        itv1-2   found 5/5, false 0, mean 1.33s  (matches standalone)
        itv1-3   found 5/5, false 0, mean 1.62s  (matches standalone)
        itv1-4   found 5/5, false 0, mean 8.44s  (improved from 12.69s,
                                                   short of standalone's
                                                   1.20s at first - one
                                                   edge still unresolved)

    That itv1-4 gap was chased down, not left open.  `corner None` in the
    verbose log meant this file leans entirely on a remembered mask (ITV1's
    own 5px persistent one) via the gap-filling path, and the shape-vs-
    anchor suppression above never touches mask brackets at all.  A trace
    on the specific window showed the same mechanism again, one technique
    over: the mask's bracket was truncated 72s short of the true end by a
    brief false "logo present" reading - an unrelated advert's own logo
    happened to sit in the same screen position ITV1's mask tracks - and
    the truncated, wrong bracket (evidence 2.00) still beat the anchor's
    accurate, complete one (evidence 1.60).  Suppressing mask brackets an
    anchor overlaps too, the same principle applied to a second technique,
    closed it exactly: itv1-4 now reaches `found 5/5, false 0, 10/10 edges
    within 5s, mean 1.20s`, matching the standalone figure precisely.
    Confirmed harmless on the other two mask-using channels (5USA, U&Dave)
    with a full corpus batch - neither forms any anchors at all, so neither
    could have been touched by this regardless.

    Confirmed harmless everywhere shape currently contributes nothing at
    all (5USA, Channel 4, U&Dave, Talking Pictures TV: zero anchor
    brackets) and byte-identical everywhere shape already worked well
    alone (ITV4 1.11s, Legend 0.51s, Sky Mix 0.39s - unchanged to two
    decimal places, the safety check that mattered most since this is
    exactly where shape and anchor overlap). BBC One - the load-bearing
    test, as always - forms one giant anchor spanning the entire recording
    and proposes nothing.  See CLAUDE.md, "Fixed: a fifth technique -
    programme anchors", for the full story including both mistakes.
    """
    gaps = []
    for i in range(len(anchors) - 1):
        lo, hi = anchors[i][1], anchors[i + 1][0]
        if hi > lo:
            gaps.append((lo, hi))
    return gaps


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect(video, verbose=False, keep=None, learn=None,
           store_path=LOGO_STORE, channel_override=None,
           progress_cb=None, cancel_cb=None, polarity_ab=False):
    """Find the advert breaks in one recording.

    Returns (breaks, info), where breaks is a list of (start, end) in
    seconds - the same REMOVE regions save_vprj_from_cuts() and write_edl()
    both expect.

    `polarity_ab` additionally derives the breaks the MASK_POLARITY_GATE
    setting would have produced had it been the other way, from the same
    decode, and returns them as info["polarity_alt"].  The reported breaks
    are unaffected: the live arm is whatever MASK_POLARITY_GATE says, so a
    measurement run and an ordinary run agree on the answer.  See
    MASK_POLARITY_GATE.

    `progress_cb(percent, label)` and `cancel_cb()` are for the GUI: the
    three passes report against their own labels, and cancelling raises
    ChalklineCancelled from wherever it is caught.  Both default to None,
    which prints to stderr and never cancels, exactly as the command line
    has always behaved.
    """
    duration, fps = probe(video)
    # An explicit channel overrides what the recording says about itself.
    #
    # The two PVRs preserve opposite halves of the DVB identity and never
    # both - see channel_key() - so the same channel keys as its name from
    # one and as "sid:NNNNN" from the other.  Nothing in either file can
    # bridge that, and this was measured across the whole corpus rather than
    # assumed: every SDT-named recording carries program number 1, and every
    # sid-keyed one carries no service name at all.  The two key spaces do
    # not overlap anywhere.
    #
    # A household running one PVR never notices.  One running two has each
    # channel silently split into two identities in the logo store, so a mask
    # learned from one recording of a channel cannot be found by another
    # recording of the same channel - which is why neither U&Dave recording
    # could see a mask, and why Sky Mix's 69px mask was never once tried
    # against the DS9 recording it was supposed to help.
    #
    # This is the escape hatch.  It takes a name rather than guessing at an
    # alias table, because the mapping is not derivable from anything in the
    # files and inventing one would be inventing data.
    channel = channel_override or channel_key(video)
    if verbose:
        origin = " (given)" if channel_override else ""
        print(f"  {os.path.basename(video)}: {duration:.1f}s at {fps:.3f}fps"
              f"  channel {channel or 'unknown'}{origin}", file=sys.stderr)

    info = {}
    # Audio first and on its own - it must not share an ffmpeg run with the
    # video work, see audio_signals().
    silences = audio_signals(video, duration, verbose,
                             progress_cb=progress_cb, cancel_cb=cancel_cb)

    workdir = keep or tempfile.mkdtemp(prefix="addetect-")
    if keep:
        # tempfile.mkdtemp() always creates its own directory, but a --keep
        # path is the caller's to have prepared - and hadn't been. ffmpeg
        # then fails to open frames.raw with no explanation beyond "No such
        # file or directory", which reads like a decode fault rather than a
        # missing folder.
        os.makedirs(workdir, exist_ok=True)
    try:
        blacks, scenes, frames = video_signals(video, duration, workdir,
                                               verbose,
                                               progress_cb=progress_cb,
                                               cancel_cb=cancel_cb)
        if verbose:
            print(f"  black {len(blacks)}  silence {len(silences)}  "
                  f"scene {len(scenes)}", file=sys.stderr)

        events = coincidences(blacks, silences, scenes)

        if frames is None:
            return [], {"reason": "no analysis frames"}
        n = frames.shape[0]
        times = np.arange(n) / FPS
        # How much of the recording the decode actually produced frames for.
        # A short decode is silent otherwise: every later measurement simply
        # sees a shorter recording and reports weak contrast rather than a
        # truncated one, which is indistinguishable from a channel with a
        # faint logo.  A user reported learning finishing in 30 seconds on a
        # three-hour recording that takes 6m45s here; without this figure
        # there is no way to tell a fast machine from a decode that stopped
        # early.
        info["analysis_frames"] = int(n)
        info["analysis_expected"] = int(round(duration * FPS)) if duration else 0

        widths, heights = shape_track(frames)
        shape_br, dominant = shape_regions(widths, heights, times)

        # SAR runs only when shape proposed nothing, because it exists to
        # cover shape's blind spot and nothing else.  See sar_brackets():
        # video_signals() decodes at a fixed size that discards source
        # SAR, so shape cannot see an aspect change that leaves no letterbox
        # or pillarbox in the picture.  When shape *does* see a change,
        # both techniques are reading the same 4:3/16:9 switch and SAR adds
        # nothing - measured on itv1, legend, more4, rewindtv and skymix,
        # every one of which proposes shape brackets, proposes exactly one
        # SAR bracket, and scores identically with that bracket removed.
        #
        # This is its own pass, and the only technique that has one: 154s on
        # itv1-3, 21% of that recording's entire runtime, for a technique
        # that earns its keep on three recordings out of eighteen.  Gating
        # it here skips it on nine, including all three HD ITV1 films.
        #
        # The gate must be on *shape*, not on whether any breaks were built.
        # sar_brackets() rejects the latter for good reason: Talking
        # Pictures TV is the recording this technique exists for, and its
        # corner search proposes fifteen brackets - wrong ones - so a
        # break-based gate would never open where it is needed most.  All
        # three TPTV recordings propose no shape brackets at all, which is
        # exactly why they are the ones that need this.
        sar_br = []
        if shape_br:
            if verbose:
                print(f"  sar skipped: shape proposed {len(shape_br)} "
                      f"brackets", file=sys.stderr)
        else:
            sar_br = sar_brackets(sar_track(video, duration,
                                            progress_cb=progress_cb,
                                            cancel_cb=cancel_cb))
            if verbose:
                print(f"  sar {len(sar_br)} candidate brackets",
                      file=sys.stderr)

        # Reuses the same widths/heights/dominant shape_regions() already
        # computed - no extra decode work, just a different question asked
        # of the same measurement.  See anchor_gaps().
        anchor_br = []
        if dominant is not None:
            anchors = programme_anchors(widths, heights, times, dominant)
            anchor_br = anchor_gaps(anchors)
        if verbose:
            print(f"  programme anchors: {len(anchor_br)} candidate "
                  f"brackets", file=sys.stderr)

        corners = logo_track(frames, times, verbose=verbose)
        mask_br = None
        # The other arm of the polarity A/B - the brackets the gate would
        # have produced had it been set the other way.  Stays None outside
        # --polarity-ab, so nothing about an ordinary run touches it.
        mask_br_alt = None
        # Spans a remembered mask proposed that survive to become brackets,
        # and so may use the shorter length floor.  Empty unless a mask
        # actually contributes something - see MIN_BREAK_STRONG.
        mask_short_ok = []

        # Learn or apply a remembered logo mask.  This never changes which
        # breaks are reported - the obvious way of using it, pairing each
        # mark with an earlier black-and-silence event, invents breaks on
        # programme content, so the marks are reported and left for the
        # caller to judge.
        box = picture_box(widths, heights)
        marks = []
        if learn is not None:
            # Built here rather than by the caller: only now is the real
            # frame count known, and deriving it from duration x FPS is off
            # by one often enough to matter.
            programme = np.ones(n, dtype=bool)
            for a, b in learn["cuts"]:
                programme &= ~((times >= a) & (times < b))
            breaks_mask = np.zeros(n, dtype=bool)
            for a, b in learn["breaks"]:
                breaks_mask |= (times >= a) & (times < b)
            if not breaks_mask.any():
                breaks_mask = junction_mask(programme, times)
            report = []
            entry = learn_mask(frames, times, learn["starts"], programme,
                               breaks_mask, box, report)
            info["learn_report"] = report
            if entry and channel:
                store = load_store(store_path)
                store_set(store, channel, entry)
                save_store(store, store_path)
                info["learned"] = entry["count"]
                info["learned_kind"] = entry["kind"]
                info["learned_contrast"] = entry["contrast"]
            elif entry:
                info["learned_but_unkeyed"] = entry["count"]
        elif channel:
            entry = store_get(load_store(store_path), channel)
            if entry:
                mask = mask_to_pixels(entry, box)
                track = mask_track(frames, mask)
                if track is not None:
                    ungated = track > MASK_ON

                    # A "logo present" reading taken while the picture is a
                    # different SHAPE from the rest of the recording is not
                    # the channel's logo.
                    #
                    # Talking Pictures shows 4:3 films and switches to 16:9
                    # for the breaks, and the DOG goes on the first frame of
                    # the break.  Adverts park their own artwork in the same
                    # corner, so the mask reads present for a few seconds at
                    # a time DURING the break - and each of those readings
                    # splits one break into two candidates.  Measured over
                    # run 23: the 240s break at 1610-1850 on tptv contained
                    # SEVEN such readings, and the three recordings between
                    # them produced fourteen false positives.
                    #
                    # Scoped to spans the shape techniques have ALREADY
                    # proposed as breaks, not to any frame off the dominant
                    # shape.  That keeps it to the case where two techniques
                    # disagree - sar says "the picture changed, this is a
                    # break", the mask says "logo present, it is not" - and
                    # settles it in favour of the one that cannot be fooled
                    # by an advert's own corner artwork.  It cannot invent a
                    # break on its own: with no shape change there is nothing
                    # to suppress.  Measured across the whole corpus, only
                    # the three TPTV recordings change at all.
                    if sar_br and entry.get("kind") == "persistent":
                        off_shape = np.zeros(len(times), dtype=bool)
                        for _lo, _hi in sar_br:
                            off_shape |= (times >= _lo) & (times <= _hi)
                        dropped = int((ungated & off_shape).sum())
                        if dropped:
                            ungated = merge_absence(ungated,
                                                    ungated & ~off_shape)
                            info["mask_offshape_dropped"] = dropped
                            if verbose:
                                print(f"    mask: ignored {dropped} present "
                                      f"frame(s) inside {len(sar_br)} span(s) "
                                      f"where the picture shape had changed",
                                      file=sys.stderr)

                    # The polarity gate, and the whole reason both arms can
                    # be had for one decode.  It is purely subtractive -
                    # `present &= lum > MASK_POLARITY_MIN` - and works on the
                    # same `frames` mask_track() has just read, so deriving
                    # the second arm costs one luminance pass and array
                    # arithmetic rather than another decode.  A corpus run is
                    # an hour of the user's machine; two runs would be two,
                    # and would additionally be comparing two passes that may
                    # have drifted in some other respect.
                    #
                    # This only ever removes presence, which for a persistent
                    # mask means MORE absence and so more and longer break
                    # candidates.  That is the direction that eats programme,
                    # not the safe one: a gate firing wrongly during real
                    # programme manufactures a break there.  What makes it
                    # worth measuring is the margin - 99.7% of programme
                    # frames brighter than their surround against 0.5% of the
                    # advert's, ~65 levels apart - not the direction.
                    gated = None
                    if MASK_POLARITY_GATE or polarity_ab:
                        lum = mask_lum_track(frames, mask)
                        if lum is not None:
                            gated = ungated & (lum > MASK_POLARITY_MIN)
                            info["mask_polarity_dropped"] = (
                                int(ungated.sum()) - int(gated.sum()))
                            if verbose:
                                print(f"    mask polarity: "
                                      f"{int(ungated.sum())} present -> "
                                      f"{int(gated.sum())} after requiring "
                                      f"brighter than surround",
                                      file=sys.stderr)

                    info["mask_pixels"] = entry["count"]
                    info["mask_kind"] = entry.get("kind")

                    def _mask_arm(present):
                        """Marks and brackets for one presence track.

                        A *persistent* logo is on through the programme and
                        off through the break, so the stretches where the
                        known mask is absent are the breaks themselves.  An
                        intermittent logo is absent for most of the
                        programme too, so its gaps mean nothing and only the
                        marks are reported.
                        """
                        arm_marks = runs_of(present, times, min_len=2.0,
                                            merge_gap=4.0)
                        arm_br = None
                        if entry.get("kind") == "persistent":
                            # Built down to MIN_BREAK_STRONG rather than
                            # MIN_BREAK: runs_of() is the first of the two
                            # length filters, so leaving it at MIN_BREAK
                            # would discard a short break before
                            # build_breaks() ever got the chance to judge
                            # its edges.
                            arm_br = runs_of(~present, times,
                                             min_len=MIN_BREAK_STRONG)
                        return arm_marks, arm_br

                    # The gate is applied to the live arm only when it is
                    # actually switched on.  In --polarity-ab the shipped
                    # behaviour stays the baseline and the gated reading is
                    # carried alongside it, so the A/B never changes what the
                    # detector would have reported.
                    live = gated if (MASK_POLARITY_GATE and gated is not None) \
                        else ungated
                    marks, mask_br = _mask_arm(live)
                    if mask_br is not None:
                        info["mask_brackets"] = len(mask_br)

                    if polarity_ab and gated is not None:
                        other = ungated if MASK_POLARITY_GATE else gated
                        alt_marks, mask_br_alt = _mask_arm(other)
                        info["polarity_alt_marks"] = len(alt_marks)

        del frames
    finally:
        if keep is None:
            raw = os.path.join(workdir, "frames.raw")
            if os.path.isfile(raw):
                os.unlink(raw)
            try:
                os.rmdir(workdir)
            except OSError:
                pass

    # Pick the corner whose brackets the recording actually supports.
    #
    # Cluster size cannot decide this - see logo_track().  What separates the
    # logo from picture detail is that breaking on it produces edges where
    # black frames and silence agree: on the Parks and Recreation recording
    # the top-left logo yielded one break worth 5.00 in edge evidence, while
    # the two larger clusters yielded no surviving break and no evidence at
    # all.
    #
    # Evidence alone is not quite enough, though, and 5star is why: BR, on
    # 31.5% of the time, beat TL on 71.4% because BR's brackets scored 1.00
    # against TL's 0.00.  BR was a QR code in the trailing infomercial, so
    # its "absence" was the programme and breaking on it cost the whole
    # show.  Candidates are therefore TIERED by whether they look like a
    # persistent logo at all (see LOGO_PERSISTENT_ON) before evidence
    # decides between them.  Evidence still decides WITHIN a tier, so
    # nothing about the Parks and Recreation reasoning is weakened - both of
    # its clusters would sit in the same tier and be separated by evidence
    # exactly as before.
    corner, strength, logo_br = None, None, []
    best = (-1, -1.0)

    # The mask gets shape suppression (see the mask branch above); the corner
    # search DELIBERATELY does not.  It was tried, measured over two corpus
    # runs, and reverted.
    #
    # The idea was sound and half of it works: a corner reading taken inside
    # a span the shape techniques already call a break cannot be trusted,
    # because an advert parks its own artwork in the same corner.  Applied to
    # the corner search it bought one false positive on tptv3 (7 -> 6, mean
    # 56.9s -> 17.3s) and cost 46 seconds of PROGRAMME on tptv: the second
    # advert's bracket started at 1626.2 against a true 1672.6, because that
    # recording's shape span begins 62.6 seconds before its break does.
    #
    # merge_absence() was written to stop exactly that, and it helped without
    # being enough - it pulled the proposed edges from 1572 back to 1631, but
    # the good 1672.5 candidate comes from a higher sustained() persistence
    # level applied AFTER this point, and clamping to the raw track cannot
    # reach it.  Run 29 scored identically to run 28.
    #
    # A false positive is recoverable and removed programme is not, so the
    # trade was refused.  Anyone reopening this needs to clamp against the
    # sustained() output per level rather than the raw presence track, and
    # should read runs 27-29 first: three corpus runs bought one clear
    # improvement, and it came from the mask, not from here.
    for name, size, on in corners:
        on_frac = float(on.mean())
        brackets = logo_brackets(on, times)
        evidence = 0.0
        for lo, hi, _support in brackets:
            _a, sa = refine_edge(lo, events, (lo, hi), "start")
            _b, sb = refine_edge(hi, events, (lo, hi), "end")
            if MIN_BREAK <= _b - _a <= MAX_BREAK:
                evidence += sa + sb
        tier = 1 if on_frac >= LOGO_PERSISTENT_ON else 0
        if verbose:
            print(f"    corner {name}: {len(brackets)} brackets, "
                  f"evidence {evidence:.2f}, on {100 * on_frac:.1f}% "
                  f"({'persistent' if tier else 'too intermittent to lead'})",
                  file=sys.stderr)
        if (tier, evidence) > best:
            corner, strength, logo_br = name, size, brackets
            best = (tier, evidence)

    # A remembered mask only fills spans the corner search left uncovered,
    # rather than competing with it everywhere it also has a bracket.
    #
    # An earlier version of this comment said the mask "replaces the corner
    # search rather than competing with it" - that stopped being true when
    # letting mask brackets compete was shipped (see "Ideas already killed by
    # measurement" in CLAUDE.md), and the comment was never corrected. Full
    # competition has a real fault: both are given identical full support
    # (see below), so where they overlap, build_breaks() decides purely on
    # whichever refined edge scores fractionally higher - and on 5USA's first
    # break the corner's edge was the better one and lost anyway, by ten
    # seconds.  Full replacement was tried before that and rejected too - it
    # regressed U&Dave, whose corner route is more accurate than its mask's
    # looser bracket.
    #
    # Restricting the mask to spans the corner has nothing to say about keeps
    # its genuine value intact.  On a Channel 4 recording the corner route
    # splits a single 343-second break into two either side of a
    # thirty-second hole, where the mask sees one absence - but the mask's
    # bracket there overlaps both of the corner's, so it is filtered out here
    # just as it already lost the competition before; this does not fix that
    # split, only stops the mask winning a tie it never earned.
    def _assemble(arm_mask_br, arm_verbose):
        """Everything downstream of the mask, for one presence arm.

        Split out so the polarity A/B can run it twice against one decode.
        Nothing in here touches `frames` - they have already been released -
        so a second call costs bracket arithmetic and edge refinement only,
        which is seconds against the hour a corpus run takes.

        `logo_br` is REBOUND below rather than mutated, and that is the whole
        reason this takes a local copy: the first arm used to extend the
        enclosing list, so the second arm would have started from brackets
        the first had already added to it.
        """
        logo_br_arm = list(logo_br)
        mask_short_ok_arm = []
        if arm_mask_br is not None:
            uncovered = [
                (lo, hi) for lo, hi in arm_mask_br
                if not any(min(hi, d) - max(lo, c) > 0
                           for c, d, _s in logo_br_arm)
            ]
            # An anchor covering this span is a more complete reading than a
            # mask fragment, the same principle as the shape suppression
            # below - not because the mask shares any measurement with the
            # anchor the way shape does, but because a fragment is a
            # fragment regardless of which technique produced it.  Confirmed
            # directly on itv1-4: an unrelated advert's own logo, positioned
            # within ITV1's tracked mask footprint, gave a brief false "logo
            # present" reading that truncated the mask's bracket 72 seconds
            # short of the true end (support 4, evidence 2.00, refined to
            # (2465.14, 2687.65)) - and it then outranked the anchor's
            # accurate, complete span (evidence 1.60, refined to (2465.14,
            # 2760.06), within a few seconds of truth) purely because its
            # wrong edge happened to coincide with stronger local evidence.
            # See CLAUDE.md for the full trace.
            uncovered = [
                (lo, hi) for lo, hi in uncovered
                if not any(min(hi, d) - max(lo, c) > 0 for c, d in anchor_br)
            ]
            logo_br_arm = logo_br_arm + [
                (lo, hi, len(LOGO_LEVELS)) for lo, hi in uncovered]
            # Only these spans earn the shorter floor.  Recorded after the
            # overlap filtering above, so a mask bracket that lost to the
            # corner search or to an anchor does not smuggle the relaxation
            # in with it.
            mask_short_ok_arm = list(uncovered)

    # Shape brackets carry full support: they have no persistence level to
    # disagree about, and where both techniques apply the shape edges are
    # measurably the more accurate - sub-second and consistent against the
    # corrected corpus, where the logo overshoots by six to twelve seconds.
    # SAR and programme-anchor brackets get the same treatment and for the
    # same reason - see sar_brackets() and anchor_gaps().
    #
    # A shape bracket an anchor overlaps is suppressed first, though, rather
    # than left to compete with it.  Both are derived from the same
    # shape_track() measurement - the anchor technique exists specifically
    # because that measurement can fragment into more than one shape_br
    # candidate instead of one clean bracket - so an overlapping shape
    # bracket is not independent evidence, it is a fragment of the same
    # signal the anchor already resolved completely.  Confirmed directly on
    # itv1-3's breaks 4 and 5: the anchor's own refined edges landed within
    # a few seconds of truth on both, but lost build_breaks()'s evidence
    # competition to a narrower shape fragment whose own edges happened to
    # coincide with stronger local black/silence/scene evidence despite
    # covering barely a third of the real break.  This is the same
    # principle as the mask/corner gap-filling fix above, applied in the
    # other direction - there the older technique (corner) was the more
    # complete reading and the newer one (mask) had to defer to it; here
    # the newer technique is the more complete reading, so it is shape that
    # defers.
        shape_for_brackets = [
            (lo, hi) for lo, hi in shape_br
            if not any(min(hi, d) - max(lo, c) > 0 for c, d in anchor_br)
        ]
        brackets = [
            (lo, hi, len(LOGO_LEVELS)) for lo, hi in shape_for_brackets]
        brackets += logo_br_arm
        brackets += [(lo, hi, len(LOGO_LEVELS)) for lo, hi in sar_br]
        brackets += [(lo, hi, len(LOGO_LEVELS)) for lo, hi in anchor_br]

        if arm_verbose:
            # Every candidate, with its span and which technique proposed it.
            # The old log gave counts only, so a bracket that existed and a
            # bracket that never did looked identical from the outside - and
            # the counts are printed after the fact, by which point the spans
            # have been discarded.
            named = (
                [("shape", lo, hi) for lo, hi in shape_for_brackets]
                + [("logo", lo, hi) for lo, hi, _s in logo_br_arm]
                + [("sar", lo, hi) for lo, hi in sar_br]
                + [("anchor", lo, hi) for lo, hi in anchor_br]
            )
            print(f"  {len(named)} candidate bracket(s) into build_breaks:",
                  file=sys.stderr)
            for who, lo, hi in sorted(named, key=lambda n: n[1]):
                short = " [short floor]" if (lo, hi) in set(
                    mask_short_ok_arm or []) else ""
                print(f"    {who:7} {lo:8.2f} - {hi:8.2f} "
                      f"({hi - lo:6.1f}s){short}", file=sys.stderr)

    # Fallback only.  BBC One carries no logo and does not change shape, and
    # correctly proposing nothing is the right answer for it - which is
    # exactly why this cannot run unconditionally.  It can never compete with
    # or override another technique, because it is only consulted once every
    # bracket the others proposed has already failed to produce a break.  See
    # coincidence_brackets() for the design reasoning and the corpus check -
    # BBC One, part of the corpus below, and a second no-advertising
    # recording (BBC Three) both stay silent with this in place.
    #
    # The gate is "build_breaks() returned nothing", not "no bracket was
    # proposed".  Those meant the same thing when this was written and
    # stopped meaning the same thing when SAR was added: a proposed bracket
    # is not a break, and build_breaks() has five separate ways to discard
    # one (raw length, refined length against either bound, and no evidence
    # at either refined edge).  On the Grimm recording - the file this
    # technique was written for, and which it scored 3/3 at 0.76s mean - SAR
    # later began proposing exactly one bracket, which build_breaks() then
    # discarded.  That was enough to close the old gate, so the fallback was
    # never reached and the recording came back completely empty: found 0/3.
    # Ask what survived, not what was offered.
        analysis_end = times[-1] if len(times) else 0.0
        arm_breaks = build_breaks(
            brackets, events, analysis_end,
            short_ok=mask_short_ok_arm, verbose=arm_verbose,
            duration=duration,
        ) if brackets else []

        arm_coincidence = []
        if not arm_breaks:
            arm_coincidence = coincidence_brackets(events)
            if arm_coincidence:
                arm_breaks = build_breaks(
                    brackets + arm_coincidence, events, analysis_end,
                    verbose=arm_verbose, duration=duration)

        # If nothing distinguishes anything, report nothing.  BBC One is what
        # this keeps correctly empty rather than inventing a break from black
        # frames alone - which is precisely what Comskip does on it.
        #
        # The two reasons are reported separately because they are genuinely
        # different failures and reading one as the other cost a diagnosis: a
        # recording whose brackets were all discarded looks identical in the
        # log to one where no technique had anything to say, and the second
        # reading sends you looking at the techniques instead of at
        # build_breaks().
        arm_reason = None
        if not arm_breaks:
            if not brackets and not arm_coincidence:
                arm_reason = "no technique proposed a break"
            else:
                arm_reason = "no proposed bracket survived refinement"

        return arm_breaks, {
            "logo_brackets": len(logo_br_arm),
            "coincidence_brackets": len(arm_coincidence),
            "reason": arm_reason,
        }

    breaks, arm = _assemble(mask_br, verbose)

    info.update({
        "channel": channel,
        "duration": duration,
        "fps": fps,
        "logo_corner": corner,
        "logo_cluster": strength,
        "shape_brackets": len(shape_br),
        "logo_brackets": arm["logo_brackets"],
        "sar_brackets": len(sar_br),
        "anchor_brackets": len(anchor_br),
        "coincidence_brackets": arm["coincidence_brackets"],
        "events": len(events),
        "marks": marks,
    })
    if arm["reason"]:
        info["reason"] = arm["reason"]

    # The other arm of the polarity A/B.  Run second and kept entirely out of
    # `info`'s own keys, so the reported result is the live arm's and reads
    # identically to a run without --polarity-ab.  Silent regardless of
    # verbosity: interleaving two runs' bracket traces makes both unreadable,
    # and the scores are what this mode is for.
    if polarity_ab and mask_br_alt is not None:
        alt_breaks, alt_arm = _assemble(mask_br_alt, False)
        info["polarity_alt"] = alt_breaks
        info["polarity_alt_reason"] = alt_arm["reason"]
        info["polarity_gate_live"] = bool(MASK_POLARITY_GATE)
    elif polarity_ab:
        # No mask, or no persistent mask, so the gate has nothing to act on
        # and both arms are the same run.  Said explicitly rather than left
        # absent, because "no difference" and "not measured" are different
        # answers and the corpus log has to be able to tell them apart.
        info["polarity_alt"] = None
        info["polarity_gate_live"] = bool(MASK_POLARITY_GATE)

    return breaks, info


# ---------------------------------------------------------------------------
# Remembering a channel's logo
# ---------------------------------------------------------------------------
#
# The logo on some channels is not a presence signal at all.  On Sky Mix it
# appears for a few seconds at the start of each programme segment and then
# goes, so "logo absent" describes most of the programme as well as all of the
# break, and bracketing on it is meaningless.  What it does mark, very
# precisely, is where a break ends: on one recording the four segment starts
# were caught within 0.06, 0.18, 0.20 and 0.28 seconds.
#
# Finding it per-recording is the hard part, because a plain "are there edges
# in this corner" test cannot tell a logo from an advert's caption - measured
# on one advert, the corner lit up to 0.49 while the logo's own pixels only
# reached 0.16.  Matching the logo's *shape* separates them completely: the
# same test against a known mask gives 0.99 for the logo and 0.16 for the
# advert.
#
# So the mask is worth remembering.  It is stored against the channel, in
# coordinates relative to the active picture rather than to the frame,
# because the same logo sits in different places depending on what shape the
# picture is: on Sky Mix's 16:9 recordings it is at x 7-17 of the frame, and
# on its pillarboxed 4:3 ones at x 21-36, having been pushed across by the
# bar.  A mask pinned to frame coordinates would be a mask for one channel in
# one aspect ratio, which is not what it claims to be.


def picture_box(widths, heights):
    """The recording's usual active picture, as (left, top, width, height)."""
    def mode(v):
        vals, counts = np.unique(v, return_counts=True)
        return int(vals[int(np.argmax(counts))])
    w, h = mode(widths), mode(heights)
    return ((GW - w) // 2, (GH - h) // 2, max(w, 1), max(h, 1))


def _clean_mask(mask, box, ew, eh, why=None):
    """Drop bar edges, then reject anything that is not compact.

    A pillarbox bar edge is a hard vertical line that comes and goes with the
    content, so it scores like a logo and gets learned as one - it is what
    smeared a Sky Mix mask down 47% of the picture height when the real logo
    is 11%.  Trimming the picture's own edges removes most of it; requiring
    the result to be small and solid removes the rest.  A logo is a compact
    blob, so a mask that is not one is not a logo however well it scored.
    """
    left, top, w, h = box
    for x in range(ew):
        if abs(x - left) <= 2 or abs(x - (left + w - 1)) <= 2:
            mask[:, x] = False
    for y in range(eh):
        if abs(y - top) <= 2 or abs(y - (top + h - 1)) <= 2:
            mask[y, :] = False
    if mask.sum() < MIN_MASK_PIXELS:
        if why is not None:
            why.append(f"only {int(mask.sum())}px above threshold "
                       f"(need {MIN_MASK_PIXELS})")
        return None
    ys, xs = np.nonzero(mask)
    bw = xs.max() - xs.min() + 1
    bh = ys.max() - ys.min() + 1
    if bw > MASK_MAX_SPAN * w or bh > MASK_MAX_SPAN * h:
        if why is not None:
            why.append(f"not compact: spans {bw / w:.0%}x{bh / h:.0%} of the "
                       f"picture (limit {MASK_MAX_SPAN:.0%})")
        return None
    fill = mask.sum() / float(bw * bh)
    if fill < MASK_MIN_FILL:
        if why is not None:
            why.append(f"too sparse: fills {fill:.0%} of its own box "
                       f"(need {MASK_MIN_FILL:.0%})")
        return None
    return mask


def junction_mask(programme, times):
    """The cut material immediately either side of the programme.

    A channel that carries no advertising has no breaks to contrast a logo
    against, and contrasting against everything the project cuts does not
    work: most of that is the neighbouring programmes, which carry the logo
    just as the kept one does.  Measured on a BBC Three recording, only 114
    seconds out of 870 lacked the logo, and the contrast came out at +0.27 -
    too weak to learn from - when the junction alone would have been
    decisive.

    What does lack the logo is the junction: the minute or so of trailers
    between one programme and the next, which sits directly against the
    programme boundary.  That is what this selects.
    """
    edges = np.flatnonzero(np.diff(programme.astype(np.int8)) != 0)
    out = np.zeros_like(programme)
    for i in edges:
        t0 = times[i]
        out |= (times >= t0 - JUNCTION_WINDOW) & (times <= t0 + JUNCTION_WINDOW)
    return out & ~programme


def learn_mask(frames, times, starts, programme, breaks, box, report=None):
    """Work out which pixels are the logo.

    Two kinds of logo need two different questions, and asking only one of
    them is why this failed on half the first recordings it met.

    A *persistent* logo - Channel 4's, ITV4's, 5USA's - is on throughout the
    programme and gone during the break, so it is found by comparing
    programme against break.

    An *intermittent* logo - Sky Mix's - appears for a few seconds when a
    segment resumes and then goes.  It is absent for most of the programme
    too, so programme-against-break barely sees it: measured on one recording
    that comparison peaked at +0.10 across the whole frame.  Comparing the
    seconds after a segment starts against the rest of the programme finds it
    at +0.56.

    Both are tried and the better compact result wins.
    """
    n = frames.shape[0]
    eh, ew = GH - 1, GW - 1

    after = np.zeros(n, dtype=bool)
    for st in starts:
        after |= (times >= st) & (times < st + 25.0)
    rest = programme & ~after

    trials = []
    if programme.sum() >= 100 and breaks.sum() >= 100:
        trials.append(("persistent", programme, breaks))
    elif programme.sum() >= 100 and (~programme).sum() >= 100:
        # Last resort, when even the junction is too short to measure.  This
        # comparison is badly diluted - most cut material is the neighbouring
        # programmes, which carry the logo too - and is expected to fail more
        # often than not.  See junction_mask().
        trials.append(("persistent", programme, ~programme))
    if after.sum() >= 10 and rest.sum() >= 100:
        trials.append(("intermittent", after, rest))
    if not trials:
        if report is not None:
            report.append("not enough programme or break material to compare")
        return None

    # One pass over the frames, accumulating every window each trial needs.
    windows = {}
    for _name, a, b in trials:
        windows[id(a)] = np.zeros((eh, ew))
        windows[id(b)] = np.zeros((eh, ew))
    for i in range(0, n, 512):
        e = edge_chunk(np.asarray(frames[i:i + 512]))
        sl = slice(i, min(i + 512, n))
        for _name, a, b in trials:
            windows[id(a)] += e[a[sl]].sum(axis=0)
            windows[id(b)] += e[b[sl]].sum(axis=0)
        del e

    best = None
    for name, a, b in trials:
        raw = windows[id(a)] / a.sum() - windows[id(b)] / b.sum()

        # Measure each pixel against the REST OF THE FRAME, not against zero.
        #
        # `raw` is "how much more often is this pixel an edge during the
        # programme than during the breaks".  A logo gives a few pixels a
        # large positive figure.  But so does content: a programme full of
        # stone walls and rubble is edgier everywhere than the smooth graphics
        # in its adverts, which lifts the WHOLE frame above the threshold.
        # Measured on a Channel 4 recording of a renovation series - peak
        # contrast +0.61 against a 0.35 threshold, and the resulting mask
        # spanned 94% x 91% of the picture, so it was rejected as not compact
        # and the channel's logo was never learned even though it is plainly
        # visible.
        #
        # Subtracting the median removes that whole-frame shift and leaves
        # what actually stands out.  Only a POSITIVE median is subtracted: if
        # the breaks are the edgier material the shift runs the other way,
        # and subtracting it would lower the bar rather than raise it.  So
        # this can only ever make the test harder, never easier - it cannot
        # invent a logo, only decline to see one in a frame-wide difference.
        baseline = max(0.0, float(np.median(raw)))
        contrast = raw - baseline

        why = []
        mask = _clean_mask(contrast > MASK_LEARN, box, ew, eh, why)
        if mask is not None and mask.sum() < MIN_CLUSTER:
            # A small mask has to be emphatic to be believed.
            if float(contrast[mask].mean()) < SMALL_MASK_CONTRAST:
                why.append(f"{int(mask.sum())}px is small and only "
                           f"{float(contrast[mask].mean()):+.2f} contrast "
                           f"(need {SMALL_MASK_CONTRAST:+.2f} below "
                           f"{MIN_CLUSTER}px)")
                mask = None
        if mask is None:
            if report is not None:
                reason = why[0] if why else "nothing above threshold"
                extra = (f", frame-wide shift {baseline:+.2f} removed"
                         if baseline > 0.01 else "")
                report.append(f"{name}: peak contrast "
                              f"{contrast.max():+.2f} (need "
                              f"{MASK_LEARN:+.2f}){extra} - {reason}")
            continue
        score = float(contrast[mask].mean())
        if best is None or score > best[0]:
            best = (score, name, mask)

    if best is None:
        return None
    score, kind, mask = best
    left, top, w, h = box
    ys, xs = np.nonzero(mask)
    return {
        "pixels": [[(x - left) / w, (y - top) / h] for x, y in zip(xs, ys)],
        "count": int(mask.sum()),
        "kind": kind,
        "contrast": round(score, 3),
        "box": [(xs.min() - left) / w, (ys.min() - top) / h,
                (xs.max() - xs.min() + 1) / w, (ys.max() - ys.min() + 1) / h],
    }


def merge_absence(on_original, on_suppressed):
    """Let suppression JOIN absences without moving their outer edges.

    Blanking a presence reading inside a shape change fixes the case it was
    built for - an advert's corner artwork splitting one break into pieces -
    but it also lets a bracket's OUTER edge slide to the edge of the shape
    span.  That is only safe if the span is tight around the break, and it is
    not always: on tptv the span for the second break starts at 1610.0 while
    the break starts at 1672.6, so suppressing the whole span moved the
    bracket 46 seconds into the programme.  Cutting programme is the
    unrecoverable direction, and one recording losing 46 seconds is a worse
    outcome than the false positives the suppression removes.

    So each merged absence is clamped back to the first and last frame that
    was absent WITHOUT suppression.  Interior fragments still join up - which
    is the whole point - but an edge can only ever move inward, never out
    into material the original reading called present.
    """
    absent_orig = ~on_original
    absent_merged = ~on_suppressed
    out = np.zeros(len(absent_merged), dtype=bool)
    n = len(absent_merged)
    i = 0
    while i < n:
        if not absent_merged[i]:
            i += 1
            continue
        j = i
        while j < n and absent_merged[j]:
            j += 1
        # The run is [i, j).  Keep only as far as the original agreed.
        idx = np.nonzero(absent_orig[i:j])[0]
        if len(idx):
            out[i + idx[0]:i + idx[-1] + 1] = True
        i = j
    return ~out


def mask_to_pixels(entry, box):
    """Map a stored mask onto this recording's active picture."""
    left, top, w, h = box
    eh, ew = GH - 1, GW - 1
    mask = np.zeros((eh, ew), dtype=bool)
    for fx, fy in entry["pixels"]:
        x = int(round(left + fx * w))
        y = int(round(top + fy * h))
        if 0 <= x < ew and 0 <= y < eh:
            mask[y, x] = True
    return mask


def mask_track(frames, mask, pad=6):
    """Per-frame contrast between the logo's own pixels and those around it.

    The surround is what makes this work.  An advert caption fills the corner
    and lights the mask's neighbourhood as much as the mask itself, so the
    difference collapses; the logo lights the mask alone.
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    eh, ew = GH - 1, GW - 1
    y0, y1 = max(0, ys.min() - pad), min(eh, ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(ew, xs.max() + pad + 1)
    around = np.zeros((eh, ew), dtype=bool)
    around[y0:y1, x0:x1] = True
    around &= ~mask
    if around.sum() == 0:
        return None

    n = frames.shape[0]
    out = np.zeros(n)
    for i in range(0, n, 512):
        e = edge_chunk(np.asarray(frames[i:i + 512]))
        sl = slice(i, min(i + 512, n))
        out[sl] = e[:, mask].mean(axis=1) - e[:, around].mean(axis=1)
        del e
    return out


def mask_lum_track(frames, mask, pad=6):
    """Signed luminance: mean inside the mask minus mean in the ring around it.

    mask_track()'s companion, and the same geometry - the difference is that
    this keeps the sign.  edge_chunk() thresholds into a boolean, so a mask
    full of dark text on white reads identically to one full of white text on
    dark.  A channel DOG is light; the advert artwork that defeats the mask on
    U&Dave is dark.  See MASK_POLARITY_GATE.

    Raw 0-255 levels, not normalised, so a value reads as "this many levels
    brighter than its surround".

    Costs one pass over the frames but no edge detection, so it is cheaper
    than mask_track() and can be computed beside it without a second decode.
    That is what lets the polarity gate be scored both ways from a single
    corpus run - see --polarity-ab.
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    eh, ew = GH - 1, GW - 1
    y0, y1 = max(0, ys.min() - pad), min(eh, ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(ew, xs.max() + pad + 1)
    around = np.zeros((eh, ew), dtype=bool)
    around[y0:y1, x0:x1] = True
    around &= ~mask
    if around.sum() == 0:
        return None

    n = frames.shape[0]
    out = np.zeros(n)
    for i in range(0, n, 512):
        # edge_chunk() drops the last row and column, so the mask grid is
        # (GH-1, GW-1).  Take the same corner of the raw frames to line up -
        # indexing the full frame with this mask would raise, and slicing the
        # wrong corner would silently offset every measurement by a pixel.
        block = np.asarray(frames[i:i + 512, :eh, :ew]).astype(np.float32)
        sl = slice(i, min(i + 512, n))
        out[sl] = block[:, mask].mean(axis=1) - block[:, around].mean(axis=1)
        del block
    return out


STORE_VERSION = 2


def _migrate_store(raw):
    """Bring any store on disk up to the current shape.

    Version 1 was a flat `{channel_key: entry}` map, which cannot express the
    thing the two PVRs force on us: the same channel arrives under a service
    *name* from Tvheadend and a service *id* from Jellyfin, and neither
    recording carries both - see channel_key().  So a mask learned from one
    was invisible to the other, and nothing in the file could say they were
    the same channel.

    Version 2 gives each entry a list of keys instead.  Any of them finds it,
    so a channel can be taught its own aliases once and both PVRs benefit.
    The display name is simply whichever key is not a `sid:` - no separate
    name field, because a name that was not also a lookup key would be
    decoration.

    Migration is one-way and silent: a v1 entry becomes a v2 entry with a
    single key.  Nothing is lost and nothing needs relearning.
    """
    if not isinstance(raw, dict):
        return {"version": STORE_VERSION, "channels": []}

    if raw.get("version") == STORE_VERSION and isinstance(
            raw.get("channels"), list):
        channels = []
        for entry in raw["channels"]:
            if isinstance(entry, dict) and entry.get("keys"):
                entry = dict(entry)
                entry["keys"] = [str(k) for k in entry["keys"] if str(k).strip()]
                if entry["keys"]:
                    channels.append(entry)
        return {"version": STORE_VERSION, "channels": channels}

    # Version 1: a flat map of key -> entry.
    channels = []
    for key, entry in raw.items():
        if key == "version" or not isinstance(entry, dict):
            continue
        entry = dict(entry)
        entry["keys"] = [str(key)]
        channels.append(entry)
    return {"version": STORE_VERSION, "channels": channels}


def load_store(path=LOGO_STORE):
    """Read the logo store, migrating an older one on the way through.

    Always returns the current shape, so nothing downstream has to know which
    version was on disk.  A missing or unreadable file is an empty store -
    the detector then falls back to the corner search, which is the correct
    behaviour and not an error.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return {"version": STORE_VERSION, "channels": []}
    return _migrate_store(raw)


def store_get(store, key):
    """The entry a channel key belongs to, or None.  Matching is exact."""
    if not key:
        return None
    for entry in store.get("channels", []):
        if key in entry.get("keys", []):
            return entry
    return None


def store_set(store, key, entry):
    """File `entry` under `key`, replacing whatever that key held before.

    An entry already carrying the key keeps its other keys: relearning a
    channel must not throw away the alias someone paired with it by hand.
    """
    channels = store.setdefault("channels", [])
    entry = dict(entry)
    for i, existing in enumerate(channels):
        if key in existing.get("keys", []):
            entry["keys"] = list(existing["keys"])
            channels[i] = entry
            return store
    entry["keys"] = [key]
    channels.append(entry)
    return store


def store_display_name(entry):
    """The human-readable key, or "" when only a service id is known."""
    for key in entry.get("keys", []):
        if not str(key).startswith("sid:"):
            return str(key)
    return ""


def store_service_id(entry):
    """The `sid:` key, or "" when only a name is known."""
    for key in entry.get("keys", []):
        if str(key).startswith("sid:"):
            return str(key)
    return ""


def save_store(store, path=LOGO_STORE):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    store.setdefault("version", STORE_VERSION)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=1)


# ---------------------------------------------------------------------------
# Scoring against a hand-corrected project
# ---------------------------------------------------------------------------

def parse_vprj(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    cuts = []
    for m in re.finditer(r"<[Cc]ut\b[^>]*>(.*?)</[Cc]ut>", text, re.S):
        a = re.search(r"CutTimeStart>(\d+)", m.group(1))
        b = re.search(r"CutTimeEnd>(\d+)", m.group(1))
        if a and b:
            cuts.append((int(a.group(1)) / 1e7, int(b.group(1)) / 1e7))
    m = re.search(r"<Duration>(\d+)</Duration>", text)
    duration = int(m.group(1)) / 1e7 if m else 0.0
    return sorted(cuts), duration


def split_truth(cuts, duration):
    """Separate real advert breaks from the recording padding.

    The PVRs pad by about three minutes at the start and ten at the end, so
    the first and last cuts are almost always that padding rather than
    adverts.  Counting them as missed detections makes any detector look
    terrible, which has already cost one round of work.
    """
    if not cuts:
        return [], []
    duration = max(duration, max(b for _a, b in cuts))
    padding, adverts = [], []
    for a, b in cuts:
        if a <= PADDING_TOLERANCE or b >= duration - PADDING_TOLERANCE:
            padding.append((a, b))
        else:
            adverts.append((a, b))
    return adverts, padding


def _padding_edges(found, padding, duration):
    """How accurately the head and tail trims were placed.

    `score()` deliberately excludes padding from found/false - counting the
    PVR's three-minute head and ten-minute tail as missed breaks makes any
    detector look terrible, and that exclusion is load-bearing.  But it also
    meant trim accuracy was never measured AT ALL, on any recording, in any
    batch run.  A whole class of defect was invisible: the U&Dave infomercial
    that two rounds of work have targeted lives entirely in the head trim, and
    the polarity gate's one clear success - taking u&dave2 from 121 seconds
    short to 0.26 - did not move a single number in the summary.

    Only the INNER edge of each padding region is scored.  A head trim runs
    from 0, and a tail trim to the end of the recording; those outer edges are
    right by construction and scoring them would dilute the figure with two
    guaranteed zeros per recording.  What is worth knowing is where the trim
    STOPS at the head, and where it STARTS at the tail.

    A padding region with no bracket OVERLAPPING it counts as missed rather
    than as an error of zero, because "trimmed nothing" and "trimmed
    perfectly" are opposite outcomes and averaging them together would hide
    the worse one.

    Candidates are found by OVERLAP with the padding region, not by starting
    within PADDING_TOLERANCE of the recording's edge.  The first version of
    this did the latter and was badly wrong: refine_edge() snaps a bracket's
    outer edge to the nearest black frame or silence, which on a real
    recording is typically several seconds in - itv1-3 refined to 6.59,
    itv1-4 to 11.91, more4 to 14.83, itv4 to 19.58.  All four had their head
    trimmed correctly and all four were scored "never cut", which made the
    detector look as though it ignored padding on three quarters of the
    corpus when it does nothing of the kind.  The same tolerance was cutting
    off tails ending 7 to 35 seconds short.

    Overlap also keeps the case that matters most.  A bracket running from
    the recording's start straight through the padding and into the
    programme - 5star's 0.00 to 1988.74 against a true head of 389.20 - is
    not contained in the padding and would vanish under a containment test,
    taking the +1599.54 reading with it.  That reading is the only thing that
    showed the programme being destroyed, so it has to survive.
    """
    errors, missed = [], 0
    for a, b in padding:
        head = a <= PADDING_TOLERANCE
        tail = b >= duration - PADDING_TOLERANCE
        if head and tail:
            # One cut covering the whole recording - nothing was kept, so
            # there is no meaningful inner edge to measure.
            continue
        # Anything overlapping this padding region at all.
        over = [d for d in found if min(d[1], b) - max(d[0], a) > 0]
        if not over:
            missed += 1
            continue
        if head:
            # The earliest overlapping bracket is the head trim; its END is
            # the edge being measured.  Earliest rather than furthest-reaching
            # because a trim split into fragments should report where the
            # FIRST one stops - that is where kept padding starts again.
            errors.append(min(over, key=lambda d: d[0])[1] - b)
        else:
            # Mirror image: the latest overlapping bracket is the tail trim
            # and its START is the edge.
            errors.append(max(over, key=lambda d: d[1])[0] - a)
    return errors, missed


def score(found, vprj):
    truth, duration = parse_vprj(vprj)
    adverts, padding = split_truth(truth, duration)
    real = [d for d in found
            if not any(p[0] - PADDING_TOLERANCE <= d[0]
                       and d[1] <= p[1] + PADDING_TOLERANCE for p in padding)]
    matched, errors = 0, []
    used = set()
    for a, b in adverts:
        best, bi = None, None
        for i, (c, d) in enumerate(real):
            if i in used:
                continue
            overlap = min(b, d) - max(a, c)
            if overlap > 0.5 * (b - a) and (best is None or overlap > best):
                best, bi = overlap, i
        if bi is not None:
            used.add(bi)
            matched += 1
            errors += [real[bi][0] - a, real[bi][1] - b]
    within5 = sum(1 for e in errors if abs(e) <= 5.0)
    mean = sum(abs(e) for e in errors) / len(errors) if errors else 0.0
    # Measured against the FULL detected list, not `real`: the head and tail
    # brackets are precisely the ones `real` has just thrown away.
    pad_errors, pad_missed = _padding_edges(
        found, padding, max(duration, max((b for _a, b in truth), default=0.0)))
    pad_mean = (sum(abs(e) for e in pad_errors) / len(pad_errors)
                if pad_errors else 0.0)
    return {
        "breaks": len(adverts), "found": matched,
        "false": len(real) - matched,
        "edges": len(errors), "within5": within5, "mean_error": mean,
        "errors": errors,
        "pad_edges": len(pad_errors),
        "pad_within5": sum(1 for e in pad_errors if abs(e) <= 5.0),
        "pad_mean_error": pad_mean,
        "pad_missed": pad_missed,
        "pad_errors": pad_errors,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_edl(breaks, path):
    with open(path, "w", encoding="utf-8") as fh:
        for a, b in breaks:
            fh.write(f"{a:.2f}\t{b:.2f}\t0\n")


def _write_vprj(path, video, breaks, duration, fps):
    """Write a genuine Snipwright/VideoReDo project selecting the detected
    breaks as cuts.

    Reuses save_vprj_from_cuts() from the application's own project code
    (src/project/vprj.py) rather than duplicating it - this is the same
    function the Watcher already calls with Comskip's output, and `breaks`
    is already exactly the (start, end) REMOVE regions in seconds it
    expects, no inversion needed.

    A recording with no detected breaks writes an empty <CutList>, which is
    not a special case: an empty cut list already means "the whole
    recording is one kept range" to load_vprj()'s own inversion, so nothing
    extra is needed here to get that behaviour - confirmed by reading
    _invert_ranges() directly rather than assumed.

    The path walks up TWO levels, not one.  This module used to sit in the
    repository root, where `dirname(__file__)/src` was correct; it now lives
    in src/repair/, where that would resolve to src/repair/src and the
    import would fail with nothing to explain why.  Harmless when Snipwright
    imports this normally - src is already on the path by then - and needed
    only when the file is run directly from the command line.
    """
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from project.vprj import save_vprj_from_cuts
    save_vprj_from_cuts(path, video, breaks, duration, fps)


# ---------------------------------------------------------------------------
# Running Chalkline from the application
# ---------------------------------------------------------------------------

# Chalkline's passes, in the order they run, so a per-pass percentage can be
# reported the way ComskipWorker reports Comskip's re-scans.
#
# A recording reports two passes or three, not always three: the sample
# aspect ratio pass only runs when the shape technique proposed nothing -
# see sar_brackets() - because it is the only technique with a decode pass of
# its own and it earns that on three recordings in eighteen.  Comskip is
# open-ended in the same way for its own reasons, so the caller already
# copes with not knowing the total in advance.
_PASS_NUMBERS = {
    "audio signals": 1,
    "video signals": 2,
    "sample aspect ratio": 3,
}


def run_chalkline(source_path, out_dir, progress_cb=None, cancel_cb=None,
                  store_path=LOGO_STORE, channel=None):
    """Detect breaks in source_path, writing an EDL into out_dir.

    Returns (edl_path, info).  `info` carries what the detector worked out
    along the way - which technique fired, whether a remembered logo was
    used, and `reason` when nothing was found - so the caller can say
    something more useful than "no breaks" if it wants to.

    progress_cb(percent, pass_number) is called as detection proceeds.  The
    percentage restarts at each pass, which is why the pass number is given
    alongside it rather than the caller being left to wonder why the bar
    went backwards.

    This lives here rather than in chalkline_worker.py because the Watcher
    calls it too, and watch/engine.py is deliberately free of Qt so it can
    be tested headlessly.  The worker adds the QThread around it; this
    function is the detection itself and imports nothing beyond the standard
    library and numpy, the same as the rest of this file.
    """
    if not os.path.isfile(source_path):
        raise ChalklineError("The recording could not be found.")

    last_pass = [1]

    def on_progress(pct, label):
        if progress_cb is None:
            return
        pass_no = _PASS_NUMBERS.get(label, last_pass[0])
        last_pass[0] = pass_no
        # Held below 100 so the bar only completes when the whole detection
        # does, not when the first of three passes finishes.
        progress_cb(int(max(0, min(99, pct))), pass_no)

    try:
        breaks, info = detect(
            source_path,
            verbose=False,
            store_path=store_path,
            channel_override=channel,
            progress_cb=on_progress,
            cancel_cb=cancel_cb,
        )
    except ChalklineError:
        raise
    except Exception as exc:
        # Anything else is a genuine fault rather than a cancellation, and
        # the caller should see it as a detector failure rather than a
        # traceback.
        raise ChalklineError(f"Advert detection failed:\n{exc}")

    base = os.path.splitext(os.path.basename(source_path))[0]
    edl_path = os.path.join(out_dir, base + ".edl")
    try:
        write_edl(breaks, edl_path)
    except OSError as exc:
        raise ChalklineError(f"Could not write the detection result:\n{exc}")

    # An empty EDL is a real result, not a failure: it means the recording
    # has no advert breaks, which is the right answer for a BBC recording and
    # the one this detector is built to give rather than inventing something.
    # The caller's existing parser reads it as no cuts, which is correct.
    if progress_cb:
        progress_cb(100, last_pass[0])
    return edl_path, info


# ---------------------------------------------------------------------------
# Learning from a corrected project
# ---------------------------------------------------------------------------

# An edit has to look like a real one before anything is learned from it.
# The store is keyed on the channel, so one bad lesson poisons every future
# recording from that channel until it is deleted by hand - which makes a
# refusal to learn much cheaper than a wrong thing learned.
#
# The cuts are not trusted just because a human saved them.  A project saved
# halfway through an edit, or one where the user was trimming a single scene
# rather than removing adverts, is a perfectly ordinary thing to save and a
# useless thing to learn from.
MAX_ADVERT_FRACTION = 0.5


def check_learnable(cuts, duration):
    """Decide whether a saved project is fit to learn a logo from.

    Returns (breaks, reason).  `breaks` is the advert breaks with the
    recording's padding removed, or None when the project should not be
    learned from at all - in which case `reason` says why, in a form fit for
    a log line.
    """
    if not cuts:
        return None, "the project has no cuts"

    adverts, padding = split_truth(cuts, duration)
    if not adverts:
        return None, "the only cuts are the recording's own padding"

    # Every break must be a plausible length.  One bad break is enough to
    # reject the project: a break of the wrong length means the boundaries
    # are somewhere other than where this thinks they are, and learning
    # compares logo presence either side of exactly those boundaries.
    #
    # MIN_BREAK_STRONG, not MIN_BREAK: a channel that genuinely runs short
    # breaks - U&Dave's are 40 seconds - would otherwise have every
    # corrected project refused, and so could never improve the very mask
    # that lets those breaks be found in the first place.
    for a, b in adverts:
        length = b - a
        if length < MIN_BREAK_STRONG or length > MAX_BREAK:
            return None, (f"a {length:.0f}s cut is outside the "
                          f"{MIN_BREAK_STRONG:.0f}-{MAX_BREAK:.0f}s range a "
                          f"real advert break falls in")

    # A recording that is more advert than programme is not a recording, it
    # is a mistake - most likely an edit made against a different video, or
    # one where the kept and removed regions have ended up the wrong way
    # round.
    if duration > 0:
        removed = sum(b - a for a, b in adverts)
        if removed > duration * MAX_ADVERT_FRACTION:
            return None, (f"the cuts remove {removed / duration:.0%} of the "
                          f"recording, which is too much to be adverts")

    # Padding at one end at least.  Both PVRs pad every recording, so its
    # absence means either the file has already been cut - in which case the
    # break boundaries are no longer where the logo changes - or the edit is
    # only part done.
    if not padding:
        return None, ("neither end of the recording is padded, so this looks "
                      "like an already-cut file rather than a raw recording")

    return adverts, ""


def learn_from_project(video, vprj_path, store_path=LOGO_STORE, channel=None,
                       only_if_unknown=True, progress_cb=None,
                       cancel_cb=None, on_start=None):
    """Learn this channel's logo from a project the user has corrected.

    This is the same work `--learn-logo` does, in a form the editor can call
    when a project is saved.  The saved project is the ground truth: it says
    where the breaks really are, and the logo is learned by comparing what
    the corner looks like inside them against outside them.

    Only ever call this for an edit a person made.  Learning from an
    unattended detection would teach Chalkline from its own guesses, and a
    wrong mask is worse than no mask - the corner search that runs without
    one is at least honest about knowing nothing.

    Returns an info dict.  `learned` is the pixel count when a logo was
    stored; `skipped` says why nothing was learned when it wasn't, and is
    the ordinary outcome rather than an error - most projects cannot teach
    it anything, and that is fine.  Raises ChalklineError only for a genuine
    fault.

    `on_start(channel)` is called once every cheap guard has passed and the
    decode is about to begin - the only point at which this is known to be
    about to take minutes rather than milliseconds.  Everything before it
    costs an ffprobe, so a caller that announced the work any earlier would
    be announcing it for every save.
    """
    info = {}
    if not os.path.isfile(video):
        raise ChalklineError("The recording could not be found.")
    if not os.path.isfile(vprj_path):
        raise ChalklineError("The saved project could not be found.")

    try:
        cuts, duration = parse_vprj(vprj_path)
    except OSError as exc:
        raise ChalklineError(f"The saved project could not be read:\n{exc}")

    adverts, reason = check_learnable(cuts, duration)
    if adverts is None:
        info["skipped"] = reason
        return info

    key = channel or channel_key(video)
    if not key:
        # A file that has been through an encoder carries neither an SDT nor
        # a usable service id, so there is nothing to file the mask under.
        # See channel_key().
        info["skipped"] = "the recording does not say which channel it is from"
        return info
    info["channel"] = key

    if only_if_unknown and store_get(load_store(store_path), key) is not None:
        # The cautious first version of this: never replace a mask that is
        # already working.  Relearning on every save would mean one poor edit
        # quietly undoing a good mask, and there is no way for the user to
        # tell that had happened.  Deleting the entry by hand is the way to
        # force a fresh one for now.
        info["skipped"] = f"a logo is already remembered for {key}"
        return info

    # A programme segment starts where the head padding ends and where each
    # break ends - those are the moments the logo appears, which is what an
    # intermittent logo is found by.  Same derivation as the command line's.
    _adverts, padding = split_truth(cuts, duration)
    starts = [b for _a, b in adverts]
    starts += [b for a, b in padding if a <= PADDING_TOLERANCE]
    learn = {"starts": sorted(starts), "cuts": cuts, "breaks": adverts}

    # Past every cheap check now, so this is about to be the expensive part.
    if on_start is not None:
        on_start(key)

    started = time.monotonic()
    try:
        _breaks, detected = detect(
            video,
            verbose=False,
            learn=learn,
            store_path=store_path,
            channel_override=key,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
    except ChalklineError:
        raise
    except Exception as exc:
        raise ChalklineError(f"Learning the channel logo failed:\n{exc}")

    # What the pass actually cost and what it looked at.  Learning runs the
    # whole of detect() - ffmpeg over the audio, a full decode, shape_track
    # and logo_track - before it ever reaches the mask, so a figure that
    # looks too fast for the recording's length means something was skipped
    # and is worth knowing about.  A user's log reporting 30 seconds for a
    # three-hour HD recording is what prompted this: without the length and
    # the elapsed time side by side there was no way to tell an unexpectedly
    # quick machine from a pass that never really ran.
    info["elapsed"] = time.monotonic() - started
    info["duration"] = detected.get("duration")
    info["fps"] = detected.get("fps")
    info["analysis_frames"] = detected.get("analysis_frames")
    info["analysis_expected"] = detected.get("analysis_expected")

    info["report"] = detected.get("learn_report", [])
    if detected.get("learned"):
        info["learned"] = detected["learned"]
        info["kind"] = detected.get("learned_kind")
        info["contrast"] = detected.get("learned_contrast")
    else:
        # learn_mask() rejects a mask it cannot believe in, which is most of
        # them.  Not a failure: the corner search carries on as before.
        #
        # But detect() may have given up long before reaching the mask - if
        # the decode produced no frames, for instance - and it says so in
        # `reason`.  That was thrown away here, so a user whose analysis had
        # not run at all was told the logo was not clear enough, which sent
        # two people looking at the logo instead of at the decode.  Report
        # what actually happened when detect() knows.
        info["skipped"] = (detected.get("reason")
                           or "no logo clear enough to remember")
    return info


def main():
    ap = argparse.ArgumentParser(
        description="Detect advert breaks in a UK broadcast recording.")
    ap.add_argument("video")
    ap.add_argument("--score", help="hand-corrected .VPrj to score against")
    ap.add_argument("--edl", help="write the result as an EDL")
    ap.add_argument("--vprj", metavar="PATH",
                    help="write a Snipwright/VideoReDo project selecting "
                         "the detected breaks as cuts; a recording with no "
                         "breaks found correctly selects the whole video, "
                         "not an empty project")
    ap.add_argument("--keep", help="keep analysis frames in this directory")
    ap.add_argument("--learn-logo", metavar="VPRJ",
                    help="learn this channel's logo from a hand-corrected "
                         "project and remember it")
    ap.add_argument("--logo-store", default=LOGO_STORE,
                    help=f"where remembered logos live (default {LOGO_STORE})")
    ap.add_argument("--channel", metavar="NAME",
                    help="use this channel name instead of the one read from "
                         "the recording, for both learning and lookup; needed "
                         "when two PVRs key the same channel differently - "
                         "see channel_key()")
    ap.add_argument("--polarity-ab", action="store_true",
                    help="also report what the MASK_POLARITY_GATE setting "
                         "would have produced the other way round, derived "
                         "from the same decode; with --score both are "
                         "scored.  The reported breaks are unaffected - see "
                         "MASK_POLARITY_GATE")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    learn = None
    if args.learn_logo:
        cuts, vdur = parse_vprj(args.learn_logo)
        adverts, padding = split_truth(cuts, vdur)
        # A programme segment starts where the head padding ends and where
        # each break ends - those are the moments the logo appears.
        starts = [b for _a, b in adverts]
        starts += [b for a, b in padding if a <= PADDING_TOLERANCE]
        learn = {"starts": sorted(starts), "cuts": cuts,
                 "breaks": adverts}

    breaks, info = detect(args.video, args.verbose, args.keep, learn,
                          args.logo_store, args.channel,
                          polarity_ab=args.polarity_ab)

    if info.get("learned"):
        print(f"learned {info.get('learned_kind')} logo for "
              f"{info.get('channel')}: {info['learned']} pixels, "
              f"contrast {info.get('learned_contrast')}, "
              f"saved to {args.logo_store}")
    elif args.learn_logo:
        print("no logo could be learned from this recording")
        for line in info.get("learn_report", []):
            print(f"  {line}")

    if info.get("mask_brackets"):
        print(f"remembered {info.get('mask_pixels')}px {info.get('mask_kind')} "
              f"mask supplied {info['mask_brackets']} brackets")
    if info.get("marks"):
        print(f"logo marks ({info.get('mask_pixels')}px mask, "
              f"{len(info['marks'])} appearances):")
        for a, b in info["marks"]:
            print(f"  logo {a:8.2f} - {b:8.2f}  ({b - a:5.1f}s)")
    print(f"technique: logo corner {info.get('logo_corner')} "
          f"cluster {info.get('logo_cluster')} "
          f"({info.get('logo_brackets')} brackets), "
          f"shape ({info.get('shape_brackets')} brackets), "
          f"sar ({info.get('sar_brackets', 0)} brackets), "
          f"anchors ({info.get('anchor_brackets', 0)} brackets), "
          f"coincidence ({info.get('coincidence_brackets', 0)} brackets)")
    if info.get("reason"):
        print(f"no breaks reported: {info['reason']}")
    for a, b in breaks:
        print(f"  break {a:8.2f} - {b:8.2f}  ({b - a:6.1f}s)")

    if args.edl:
        write_edl(breaks, args.edl)

    if args.vprj:
        try:
            _write_vprj(args.vprj, args.video, breaks,
                        info.get("duration", 0.0), info.get("fps", 25.0))
            print(f"wrote project: {args.vprj}")
        except Exception as e:
            print(f"could not write .vprj: {e}", file=sys.stderr)

    def _report_score(found, label=None):
        r = score(found, args.score)
        tag = f"[{label}] " if label else ""
        print(f"  {tag}found {r['found']}/{r['breaks']}, "
              f"false {r['false']}, "
              f"{r['within5']}/{r['edges']} edges within 5s, "
              f"mean {r['mean_error']:.2f}s")
        if r["errors"]:
            print(f"  {tag}errors: " +
                  "  ".join(f"{e:+.2f}" for e in r["errors"]))
        # Head and tail trims, on their own line and never folded into the
        # figures above.  They are a different question - how much padding was
        # removed, not which breaks were found - and averaging the two would
        # let a good trim mask a missed break or the reverse.
        if r["pad_edges"] or r["pad_missed"]:
            miss = (f", {r['pad_missed']} not trimmed"
                    if r["pad_missed"] else "")
            print(f"  {tag}padding {r['pad_within5']}/{r['pad_edges']} "
                  f"edges within 5s, mean {r['pad_mean_error']:.2f}s{miss}")
            if r["pad_errors"]:
                print(f"  {tag}padding errors: " +
                      "  ".join(f"{e:+.2f}" for e in r["pad_errors"]))
        return r

    if args.polarity_ab:
        # Name the arms by what they ARE rather than by "live" and "other",
        # so a log stays readable if the shipped default is ever flipped.
        live_name = "gate on" if info.get("polarity_gate_live") else "gate off"
        alt_name = "gate off" if info.get("polarity_gate_live") else "gate on"
        alt = info.get("polarity_alt")
        if alt is None:
            print("polarity A/B: no persistent mask applied - "
                  "both arms are the same run")
        else:
            dropped = info.get("mask_polarity_dropped")
            print(f"polarity A/B: {live_name} reported {len(breaks)} break(s), "
                  f"{alt_name} reported {len(alt)}"
                  + (f"; gate removed {dropped} present frame(s)"
                     if dropped is not None else ""))
            for a, b in alt:
                print(f"  [{alt_name}] break {a:8.2f} - {b:8.2f} "
                      f"({b - a:6.1f}s)")
            if info.get("polarity_alt_reason"):
                print(f"  [{alt_name}] no breaks reported: "
                      f"{info['polarity_alt_reason']}")

    if args.score:
        if args.polarity_ab and info.get("polarity_alt") is not None:
            live_name = "gate on" if info.get(
                "polarity_gate_live") else "gate off"
            alt_name = "gate off" if info.get(
                "polarity_gate_live") else "gate on"
            _report_score(breaks, live_name)
            _report_score(info["polarity_alt"], alt_name)
        else:
            _report_score(breaks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
