from dataclasses import dataclass, field
from fractions import Fraction


@dataclass
class AudioExportSettings:
    codec: str
    channels: str | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    denoise: int = -1

@dataclass
class AudioExportInfo:
    output_tracks: list[AudioExportSettings | None] = field(default_factory=lambda: [])

@dataclass
class CutSegment:
    require_recode: bool
    start_time: Fraction
    end_time: Fraction
    gop_start_dts: int = -1
    gop_end_dts: int = -1
    gop_index: int = -1
    # Clock jumps inside this segment, as (split_time, amount) pairs in
    # seconds on the source clock.  Anything at or after split_time was
    # stamped `amount` seconds later than it really happened.
    #
    # A broadcast recording's clock can leap hours in the middle of a
    # programme with nothing missing (one Channel 4 HD recording jumped
    # 6h34m twenty minutes in).  The GOP that straddles the leap then spans
    # hours on the source clock while holding two seconds of pictures, and
    # every cutter used to advance its output position by end_time -
    # start_time: everything after it was written hours late.  The cutters
    # now take jump_before() and output_length instead, which close
    # the gap.  Empty for every segment of a recording whose clock runs
    # normally, so those behave exactly as before.
    clock_jumps: tuple = ()

    def jump_before(self, source_time: Fraction) -> Fraction:
        """Total clock jump, in seconds, at or before `source_time`."""
        total = Fraction(0)
        for split, amount in self.clock_jumps:
            if source_time >= split:
                total += amount
        return total

    @property
    def jump_total(self) -> Fraction:
        """All the clock jump this segment contains, in seconds."""
        return sum((amount for _split, amount in self.clock_jumps),
                   Fraction(0))

    @property
    def output_length(self) -> Fraction:
        """How much output time this segment really occupies, in seconds."""
        return self.end_time - self.start_time - self.jump_total
