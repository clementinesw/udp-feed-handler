"""Testbench for feed_handler.sv.

Tests are grouped by what they check, so one group can be run on its own:
    make test GROUP=reset      outputs stay low with no traffic
    make test GROUP=counter    beat counter
    make test GROUP=filter     header checks
    make test GROUP=decode     message decode + tick
    make test GROUP=fcs        bad-FCS handling (cut-through safety)
    make test GROUP=perf       latency and back-to-back frames at line rate
    make test GROUP=random     randomised traffic vs the reference model
    make test                  everything

How cocotb works: the simulator runs the design and keeps time. Each test is a
Python function that pauses on `await` until something happens (a clock edge,
signals settling), then drives inputs or checks outputs through `dut`.
"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from axis import AxisSource, Monitor
from frames import FEED_IP, FEED_PORT, build_frame, ip_to_int, model, quote, random_frame

CLK_PERIOD_NS = 6.4    # 156.25 MHz: 64 bits x 156.25 MHz = 10 Gb/s


async def setup(dut, gap_prob=0.0, seed=1):
    """Start the clock, configure the feed, reset. Returns (source, monitor)."""
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())         #toggles clock forever every 3.2ns (fnt in cocotb), run in the background
    dut.cfg_dst_ip.value = ip_to_int(FEED_IP)       # "239.1.1.1" -> 0xEF010101
    dut.cfg_dst_port.value = FEED_PORT
    src = AxisSource(dut, gap_prob=gap_prob, seed=seed)   # the fake MAC; fixed seed = same gaps every run
    src.idle()                                      # tvalid, tlast, tuser, tkeep, tdata all 0
    dut.rst.value = 1
    await ClockCycles(dut.clk, 4)                   # registers see rst on 4 edges
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    mon = Monitor(dut)                              # records every tick and cancel from now on
    mon.start()
    return src, mon                                 # tests that don't need one write `src, _ = ...`


async def drain(dut, cycles=10):
    """Wait a few cycles so the last outputs (tick, cancel) have time to appear."""
    await ClockCycles(dut.clk, cycles)


def watch(dut, condition, signal):
    """Every cycle where condition(dut) holds, record int(signal). Returns the list.

    The list comes back empty straight away and keeps filling in the background
    until the test ends. The caller passes the condition as a lambda.
    """
    seen = []

    async def run():
        while True:
            await ReadOnly()          # sample first, so the beat already on the bus is seen
            if condition(dut):
                seen.append(int(signal.value))
            await RisingEdge(dut.clk)

    cocotb.start_soon(run())
    return seen


def with_byte(frame: bytes, offset: int, value: int) -> bytes:
    """Copy of frame with one byte overwritten (bytes can't be changed, bytearray can)."""
    b = bytearray(frame)
    b[offset] = value
    return bytes(b)


# Frames the filter must reject, one per header check: (label for error messages,
# function that builds the frame). Tests loop over it with `for label, make in REJECTS`
# and call make() to get each frame. Used by the filter and decode tests.
REJECTS = [
    # IHL says 24-byte IP header, so the real UDP header is 4 bytes later - even
    # though the bytes at our fixed offsets still look like a valid feed packet.
    ("IHL = 6, fixed offsets look valid", lambda: with_byte(build_frame(quote()), 14, 0x46)),
    ("wrong UDP port", lambda: build_frame(quote(), dport=FEED_PORT + 1)),
    ("wrong group, low half of IP", lambda: build_frame(quote(), dst_ip="239.1.1.2")),    # caught on beat 4
    ("wrong group, high half of IP", lambda: build_frame(quote(), dst_ip="239.2.1.1")),   # caught on beat 3
    ("VLAN tag (EtherType 0x8100)", lambda: build_frame(quote(), vlan=100)),
    ("IP options (IHL = 6)", lambda: build_frame(quote(), ip_options=True)),
    ("IP fragment (MF set)", lambda: build_frame(quote(), fragment=True)),
    ("IP last fragment (MF = 0, offset != 0)", lambda: build_frame(quote(), frag_offset=185)),
    ("TCP, not UDP", lambda: build_frame(quote(), proto=6)),
    ("UDP length too short", lambda: build_frame(quote()[:10])),   # only 10 payload bytes, padding fills 42-55
]


# ----------------------------------------------------------------------------- reset
@cocotb.test()
async def test_reset_idle(dut):
    """After reset, with no traffic, the outputs stay low."""
    await setup(dut)
    for _ in range(20):                             # 20 cycles x 2 checks
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert dut.tick_valid.value == 0, "tick_valid should be 0 with no traffic"
        assert dut.tick_cancel.value == 0, "tick_cancel should be 0 with no traffic"


# ----------------------------------------------------------------------------- beat counter
@cocotb.test()
async def test_counter_counts_and_saturates(dut):
    """beat_cnt = index of the beat on the bus: counts up, saturates at 7, restarts after tlast."""
    src, _ = await setup(dut)
    # record beat_cnt on every valid beat (internal signals are visible through dut)
    seen = watch(dut, lambda d: d.s_axis_tvalid.value == 1, dut.beat_cnt)

    # frames of zeros: only the number of beats matters here, not the contents
    await src.send(bytes(60))            # 8 beats
    await src.send(bytes(100))           # 13 beats: must stick at 7
    await src.send(bytes(60), ifg=3)     # 3 idle cycles after this one
    await drain(dut)

    assert seen[0:8] == [0, 1, 2, 3, 4, 5, 6, 7], f"60-byte frame: got {seen[0:8]}"
    assert seen[8:21] == [0, 1, 2, 3, 4, 5, 6] + [7] * 6, \
        f"100-byte frame should saturate at 7: got {seen[8:21]}"
    assert seen[21:29] == [0, 1, 2, 3, 4, 5, 6, 7], f"third frame should restart at 0: got {seen[21:29]}"


@cocotb.test()
async def test_counter_holds_in_gaps(dut):
    """When tvalid is low mid-frame, the counter must hold its value."""
    src, _ = await setup(dut, gap_prob=0.5)          # 50% chance of an idle cycle before each beat
    seen = watch(dut, lambda d: d.s_axis_tvalid.value == 1, dut.beat_cnt)
    await src.send(bytes(60))
    await src.send(bytes(60))
    await drain(dut)
    # only valid beats are recorded, so gaps must not show up as skipped numbers
    assert seen == [0, 1, 2, 3, 4, 5, 6, 7] * 2, f"with idle gaps: got {seen}"


# ----------------------------------------------------------------------------- header filter
def header_verdict_watch(dut):
    """match_q seen while beat 5 is on the bus = verdict of the header checks on beats 1-4.

    match_q is a register, so during beat 5 it holds the result of beats 1-4 but
    not yet beat 5's own check (message type and side).
    """
    return watch(dut, lambda d: d.s_axis_tvalid.value == 1 and d.beat_cnt.value == 5, dut.match_q)


@cocotb.test()
async def test_filter_accepts_feed_packet(dut):
    """A UDP packet to our group and port passes every header check."""
    src, _ = await setup(dut)
    verdicts = header_verdict_watch(dut)
    await src.send(build_frame(quote()))
    await src.send(build_frame(quote(), dont_fragment=True))   # DF set is fine, it's not a fragment
    await drain(dut)
    assert verdicts, "never saw beat 5 - is the beat counter working?"   # empty list counts as False
    assert verdicts[0] == 1, "feed packet should match (match_q = 1 at beat 5)"
    assert verdicts == [1, 1], f"feed packet with DF set should also match, got {verdicts}"


@cocotb.test()
async def test_filter_rejects_other_traffic(dut):
    """Every kind of non-feed traffic fails the header checks."""
    src, _ = await setup(dut)
    verdicts = header_verdict_watch(dut)
    for label, make in REJECTS:
        before = len(verdicts)
        await src.send(make())
        await drain(dut, 2)
        assert len(verdicts) == before + 1, f"{label}: never saw beat 5"
        assert verdicts[-1] == 0, f"{label}: should be rejected (match_q = 0 at beat 5)"   # [-1] = last item


@cocotb.test()
async def test_filter_resets_each_frame(dut):
    """A rejected frame must not poison the next one: match_q restarts at 1 after tlast."""
    src, _ = await setup(dut)
    verdicts = header_verdict_watch(dut)
    await src.send(build_frame(quote(), dport=1234))
    await src.send(build_frame(quote()))
    await drain(dut)
    assert verdicts == [0, 1], f"expected [reject, accept], got {verdicts}"


# ----------------------------------------------------------------------------- message decode
@cocotb.test()
async def test_decode_tick_fields(dut):
    """Each feed packet produces one tick with the right side, instrument, price and qty."""
    src, mon = await setup(dut)
    # values with all-different bytes catch fields in the wrong byte order; 0xFFFFFFFF = largest value
    msgs = [("B", 42, 1_015_000, 300), ("S", 42, 1_015_100, 50),
            ("B", 0xDEADBEEF, 0x01020304, 0x0A0B0C0D), ("S", 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)]
    for side, inst, price, qty in msgs:
        await src.send(build_frame(quote(side, inst, price, qty)))
    await drain(dut)

    got = [t.fields() for t in mon.ticks]                          # what the design output
    want = [(1 if s == "S" else 0, i, p, q) for s, i, p, q in msgs]   # side: 'B' = 0, 'S' = 1
    assert len(got) == len(want), f"expected {len(want)} ticks, got {len(got)}"
    for n, (g, w) in enumerate(zip(got, want)):                    # zip pairs them up, n says which one
        assert g == w, f"tick {n}: got (side, inst, price, qty) = {g}, want {w}"


@cocotb.test()
async def test_decode_no_tick_for_other_traffic(dut):
    """Rejected headers, unknown message types and bad side codes produce no tick."""
    src, mon = await setup(dut)
    for _, make in REJECTS:
        await src.send(make())
    await src.send(build_frame(quote(msg_type="X")))   # good headers, only the beat 5 check catches these
    await src.send(build_frame(quote(side="Z")))
    await drain(dut)
    assert mon.ticks == [], f"expected no ticks, got {len(mon.ticks)}: {mon.ticks[:3]}"


@cocotb.test()
async def test_decode_long_frame_one_tick(dut):
    """A long frame gives exactly one tick - the beat counter must not wrap around."""
    src, mon = await setup(dut)
    msg = quote("S", 7, 99, 1)
    # Hide a complete feed frame inside the payload, starting at byte 64 = beat 8
    # (the 8 zero bytes push it there). A counter that wraps from 7 back to 0 would
    # see its headers exactly where a new frame's would be and fire a bogus second tick.
    decoy = build_frame(quote("B", 666, 666, 666), pad=False)
    await src.send(build_frame(msg + bytes(8) + decoy + bytes(100)))
    await drain(dut)
    assert len(mon.ticks) == 1, f"expected 1 tick, got {len(mon.ticks)} - does the counter wrap?"
    assert mon.ticks[0].fields() == (1, 7, 99, 1)


# ----------------------------------------------------------------------------- bad FCS / cut-through
@cocotb.test()
async def test_fcs_bad_frame_cancels(dut):
    """Cut-through: the tick goes out before the FCS is known, then gets cancelled."""
    src, mon = await setup(dut)
    await src.send(build_frame(quote()), bad_fcs=True)   # 60 bytes, bad FCS flagged on beat 7
    await drain(dut)
    assert len(mon.ticks) == 1, "the tick must still go out before the end of the frame"
    assert mon.cancels == [0], f"expected a cancel for frame 0, got {mon.cancels}"


@cocotb.test()
async def test_fcs_bad_on_message_beat_suppressed(dut):
    """If the frame ends (corrupt) on the message beat itself, never send the tick."""
    src, mon = await setup(dut)
    await src.send(build_frame(quote(), pad=False), bad_fcs=True)   # 56 bytes: ends on beat 6
    await drain(dut)
    assert mon.ticks == [], "tick should be suppressed, the bad FCS was known in time"
    assert mon.cancels == [], "nothing to cancel if nothing was sent"


@cocotb.test()
async def test_fcs_good_frame_ending_on_message_beat(dut):
    """A good frame that ends exactly on beat 6 still gives a tick and no cancel."""
    src, mon = await setup(dut)
    await src.send(build_frame(quote("B", 5, 6, 7), pad=False))
    await drain(dut)
    # comparing whole lists checks the number of ticks (exactly 1) and their values together
    assert [t.fields() for t in mon.ticks] == [(0, 5, 6, 7)]
    assert mon.cancels == []


@cocotb.test()
async def test_fcs_truncated_message(dut):
    """A frame cut off in the middle of the message (tkeep not full) gives no tick."""
    src, mon = await setup(dut)
    await src.send(build_frame(quote(), pad=False)[:52])   # first 52 bytes: beat 6 only has 4 lanes
    await drain(dut)
    assert mon.ticks == [], "message is incomplete, there must be no tick"


@cocotb.test()
async def test_fcs_cancel_only_bad_frames(dut):
    """Only frames that sent a tick AND turned out bad are cancelled."""
    src, mon = await setup(dut)
    await src.send(build_frame(quote()))                              # 0: good
    await src.send(build_frame(quote()), bad_fcs=True)                # 1: bad  -> cancel
    await src.send(build_frame(quote(), dport=999), bad_fcs=True)     # 2: bad, not ours -> no cancel
    await src.send(build_frame(quote()))                              # 3: good
    await src.send(build_frame(quote()), bad_fcs=True)                # 4: bad  -> cancel
    await drain(dut)
    assert [t.frame for t in mon.ticks] == [0, 1, 3, 4]               # frame number of each tick
    assert mon.cancels == [1, 4], f"expected cancels for frames [1, 4], got {mon.cancels}"


# ----------------------------------------------------------------------------- latency and line rate
@cocotb.test()
async def test_perf_latency(dut):
    """No gaps: tick 7 cycles after the first beat, 1 cycle after the last message byte.

    With no gaps the two numbers say the same thing (7 = 6 + 1). With gaps only the
    second one is fixed, which is why the random test checks after_last_byte.
    """
    src, mon = await setup(dut)
    await src.send(build_frame(quote()))
    await drain(dut)
    assert len(mon.ticks) == 1
    t = mon.ticks[0]
    dut._log.info("latency: %d cycles from first beat = %.1f ns; %d cycle after last message byte",
                  t.latency, t.latency * CLK_PERIOD_NS, t.after_last_byte)
    assert t.after_last_byte == 1, f"tick should be 1 cycle after beat 6, got {t.after_last_byte}"
    assert t.latency == 7, f"tick should be 7 cycles after beat 0, got {t.latency}"


@cocotb.test()
async def test_perf_line_rate(dut):
    """500 feed packets with no gaps at all: every one must produce a tick."""
    src, mon = await setup(dut)
    for n in range(500):                 # each frame's beat 0 comes straight after the last frame's tlast
        await src.send(build_frame(quote("B", n, n * 3, n * 7)))   # different values so each tick matches its frame
    await drain(dut)
    assert len(mon.ticks) == 500, f"dropped ticks at line rate: got {len(mon.ticks)} of 500"
    assert all(t.fields() == (0, n, n * 3, n * 7) for n, t in enumerate(mon.ticks))


# ----------------------------------------------------------------------------- random traffic
@cocotb.test()
async def test_random_traffic(dut):
    """2000 random frames with random gaps, checked against the reference model in frames.py."""
    # cocotb picks a new seed every run and prints it at the start; a failing run can be
    # repeated exactly with COCOTB_RANDOM_SEED=<that number>
    rng = random.Random(random.getrandbits(32))
    src, mon = await setup(dut, gap_prob=0.2, seed=rng.getrandbits(32))

    sent = []
    for _ in range(2000):
        frame, bad, label = random_frame(rng)            # label = kind of frame, only for messages
        sent.append((frame, bad, label))
        await src.send(frame, bad_fcs=bad, ifg=rng.choice([0, 0, 1, 3]))   # 0 idle cycles half the time
    await drain(dut, 20)

    # ask the reference model what should have happened for each frame n
    want_ticks, want_cancels = [], []
    for n, (frame, bad, _) in enumerate(sent):
        tick, cancel = model(frame, bad)
        if tick is not None:
            want_ticks.append((n, tick))
        if cancel:                                       # cancel = bad FCS AND a tick was sent
            want_cancels.append(n)
    got_ticks = [(t.frame, t.fields()) for t in mon.ticks]

    # pair by pair first, so a missing tick is reported at the frame where it went wrong;
    # the length check after catches extra ticks at the end that zip can't see
    for g, w in zip(got_ticks, want_ticks):
        assert g == w, (f"first mismatch: DUT gave {g}, model expects {w} "
                        f"(frame {w[0]} was '{sent[w[0]][2]}', bad_fcs={sent[w[0]][1]})")
    assert len(got_ticks) == len(want_ticks), \
        f"DUT gave {len(got_ticks)} ticks, model expects {len(want_ticks)}"
    assert mon.cancels == want_cancels, "cancels differ from the model"   # short list, plain == is enough
    assert all(t.after_last_byte == 1 for t in mon.ticks), "every tick must be 1 cycle after beat 6"

    kinds = {}                                           # count of each kind of frame, for the summary
    for _, _, label in sent:
        kinds[label] = kinds.get(label, 0) + 1
    dut._log.info("checked %d frames: %d ticks, %d cancels, mix %s",
                  len(sent), len(got_ticks), len(want_cancels), kinds)

