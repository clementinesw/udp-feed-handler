"""AXI4-Stream driver and an output monitor for the feed_handler testbench."""
import random
from dataclasses import dataclass

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge


class AxisSource:
    """Drives frames into the DUT's AXI4-Stream input, 8 bytes per beat, like a 10G MAC."""

    def __init__(self, dut, gap_prob=0.0, seed=None):
        self.dut = dut
        self.gap_prob = gap_prob          # chance of an idle cycle before each beat
        self.rng = random.Random(seed)    # own random generator, fixed seed = same gaps every run

    def idle(self):
        """No beat on the bus: everything 0 (a real MAC might leave old values, which is why the design checks tvalid)."""
        d = self.dut
        d.s_axis_tvalid.value = 0
        d.s_axis_tlast.value = 0
        d.s_axis_tuser.value = 0
        d.s_axis_tkeep.value = 0
        d.s_axis_tdata.value = 0

    async def send(self, frame: bytes, bad_fcs=False, ifg=0):
        """Send one frame. bad_fcs: MAC flags it corrupt on the last beat. ifg: idle cycles after."""
        d = self.dut
        # cut the frame into 8-byte chunks, one per beat (bytes 0-7, 8-15, ...); the last can be shorter
        beats = [frame[i:i + 8] for i in range(0, len(frame), 8)]
        for i, chunk in enumerate(beats):
            # coin flip before each beat: random() is a new number between 0 and 1 every time,
            # so with gap_prob = 0.5 about half the flips give an idle cycle (can be several in a row)
            while self.gap_prob and self.rng.random() < self.gap_prob:
                self.idle()                                   # tvalid low for a cycle
                await RisingEdge(d.clk)
            last = i == len(beats) - 1
            # Byte 0 of the chunk goes in lane 0 = tdata[7:0], so pack little-endian.
            # ljust pads a short last chunk with zeros up to 8 bytes (full chunks are unchanged).
            d.s_axis_tdata.value = int.from_bytes(chunk.ljust(8, b"\x00"), "little")
            d.s_axis_tkeep.value = (1 << len(chunk)) - 1      # e.g. 4 bytes -> 0b00001111
            d.s_axis_tvalid.value = 1
            d.s_axis_tlast.value = int(last)
            d.s_axis_tuser.value = int(last and bad_fcs)   # bad FCS is only ever flagged on the last beat
            await RisingEdge(d.clk)                        # the design takes this beat on this edge
        self.idle()
        for _ in range(ifg):
            await RisingEdge(d.clk)


@dataclass
class Tick:
    frame: int            # index of the frame (0 = first frame sent)
    side: int
    instrument: int
    price: int
    qty: int
    latency: int          # cycles from the first beat of the frame to tick_valid
    after_last_byte: int  # cycles from the beat holding the last message byte to tick_valid

    def fields(self):
        return (self.side, self.instrument, self.price, self.qty)


class Monitor:
    """Samples inputs and outputs once per cycle, after the clock edge has settled.

    Because it sees the input beat and the outputs at the same moment, it can tie
    every tick or cancel to the frame that caused it and measure latency in cycles.
    """

    def __init__(self, dut):
        self.dut = dut
        self.ticks = []       # list of Tick
        self.cancels = []     # frame indices that got a tick_cancel

    def start(self):
        cocotb.start_soon(self._run())

    async def _run(self):
        d = self.dut
        cycle = 0
        frame = 0             # index of the frame whose beats are arriving
        beat = 0              # beat index within that frame
        first_beat_cycle = {}
        prev = None           # (frame, cycle) of the input beat seen in the previous cycle
        # first_beat_cycle: frame number -> cycle its beat 0 arrived (a dictionary)

        while True:
            await ReadOnly()          # sample at the end of the current time step (values settled)
            cycle += 1

            # Outputs are registered, so they belong to the beat of the previous cycle.
            # (outputs are checked before prev is updated with this cycle's beat)
            if d.tick_valid.value == 1:
                assert prev is not None, "tick_valid high but there was no input beat last cycle"
                f, c = prev
                self.ticks.append(Tick(
                    frame=f, side=int(d.tick_side.value),
                    instrument=int(d.tick_instrument.value),
                    price=int(d.tick_price.value), qty=int(d.tick_qty.value),
                    latency=cycle - first_beat_cycle[f], after_last_byte=cycle - c))   # cycles count gaps too
            if d.tick_cancel.value == 1:
                assert prev is not None, "tick_cancel high but there was no input beat last cycle"
                self.cancels.append(prev[0])

            if d.s_axis_tvalid.value == 1:
                if beat == 0:
                    first_beat_cycle[frame] = cycle
                prev = (frame, cycle)
                if d.s_axis_tlast.value == 1:
                    frame += 1
                    beat = 0
                else:
                    beat += 1
            else:
                prev = None

            await RisingEdge(d.clk)
