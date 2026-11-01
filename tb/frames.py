"""Frame builders and the reference model ("golden model") for feed_handler.

Scapy builds real Ethernet/IPv4/UDP frames, so the headers, lengths and IP
checksum are exactly what a NIC would see on the wire.
"""
import ipaddress
import random
import struct

from scapy.layers.inet import IP, UDP, IPOption_NOP
from scapy.layers.l2 import Dot1Q, Ether

FEED_IP = "239.1.1.1"            # multicast group the exchange publishes on
FEED_PORT = 30001
FEED_MAC = "01:00:5e:01:01:01"   # IPv4 multicast MAC: 01:00:5e + low 23 bits of the IP
SRC_MAC = "02:00:00:00:00:01"
SRC_IP = "10.0.0.1"
MIN_FRAME = 60                   # Ethernet minimum frame, not counting the 4-byte FCS
MSG_LEN = 14                     # bytes in one quote message


def ip_to_int(ip: str) -> int:
    """'239.1.1.1' -> 0xEF010101, the 32-bit value the hardware input needs."""
    return int(ipaddress.IPv4Address(ip))


def quote(side="B", instrument=1, price=1_000_000, qty=10, msg_type="Q") -> bytes:
    """One 14-byte quote message, big-endian like most binary exchange feeds:

    offset 0  msg type    1 byte   'Q'
    offset 1  side        1 byte   'B' = bid, 'S' = ask
    offset 2  instrument  4 bytes
    offset 6  price       4 bytes  (integer ticks, e.g. 1/10000 of a unit)
    offset 10 qty         4 bytes
    """
    # struct.pack(">III", ...) = three numbers -> 12 bytes, big-endian (the opposite of unpack)
    return msg_type.encode() + side.encode() + struct.pack(">III", instrument, price, qty)


def build_frame(payload: bytes, dst_ip=FEED_IP, dport=FEED_PORT, vlan=None,
                ip_options=False, fragment=False, frag_offset=0, dont_fragment=False,
                proto=None, pad=True) -> bytes:
    """Ethernet + IPv4 + UDP frame (without FCS - the MAC strips and checks it).

    The keyword options make the frames the filter must reject.
    """
    eth = Ether(dst=FEED_MAC, src=SRC_MAC)
    if vlan is not None:
        eth = eth / Dot1Q(vlan=vlan)                 # adds 4 bytes: everything shifts
    ip = IP(src=SRC_IP, dst=dst_ip, ttl=8)
    if ip_options:
        ip.options = [IPOption_NOP()] * 4            # 24-byte IP header (IHL = 6)
    if dont_fragment:
        ip.flags = "DF"                              # common on real feeds, must be accepted
    if fragment:
        ip.flags = "MF"                              # "more fragments" set
    if frag_offset:
        ip.frag = frag_offset                        # in 8-byte units; last fragment has MF = 0
    if proto is not None:
        ip.proto = proto                             # e.g. 6 = TCP
    frame = bytes(eth / ip / UDP(sport=40000, dport=dport) / payload)   # "/" stacks the headers
    if pad and len(frame) < MIN_FRAME:
        frame += bytes(MIN_FRAME - len(frame))       # a NIC pads short frames with zeros
    return frame


def model(frame: bytes, bad_fcs: bool, cfg_ip=FEED_IP, cfg_port=FEED_PORT):
    """What should feed_handler output for this frame?

    The same rules as the RTL, written the simple way: whole frame at once,
    byte positions instead of beats and lanes.

    Returns (tick, cancel):
      tick   = (side, instrument, price, qty) if a tick should be sent, else None
               (side: 0 = bid, 1 = ask)
      cancel = True if that tick should later be cancelled (bad FCS)
    """
    # message not all there, or bad FCS already known on the message beat (frame ends on beat 6)
    if len(frame) < 56 or (bad_fcs and len(frame) == 56):
        return None, False
    # frame[n] = one byte as a number, frame[a:b] = bytes a to b-1
    ok = (frame[12:14] == b"\x08\x00"                      # EtherType = IPv4
          and frame[14] == 0x45                            # IPv4, 20-byte header
          and (frame[20] & 0x3F) == 0 and frame[21] == 0   # not a fragment: MF + offset 0, DF and reserved bit ignored
          and frame[23] == 17                              # protocol = UDP
          and frame[30:34] == ipaddress.IPv4Address(cfg_ip).packed   # .packed = the 4 IP bytes, big-endian
          and int.from_bytes(frame[36:38], "big") == cfg_port
          and int.from_bytes(frame[38:40], "big") >= 8 + MSG_LEN    # UDP length >= 22
          and frame[42] == ord("Q")                        # ord = character code, 'Q' = 0x51
          and frame[43] in (ord("B"), ord("S")))
    if not ok:
        return None, False
    side = 1 if frame[43] == ord("S") else 0
    instrument, price, qty = struct.unpack(">III", frame[44:56])    # 12 bytes -> three big-endian 32-bit numbers
    return (side, instrument, price, qty), bad_fcs                   # cancel only ever happens when a tick was sent


def random_frame(rng: random.Random):
    """A random frame from a realistic traffic mix. Returns (frame, bad_fcs, label)."""
    msg = quote(side=rng.choice("BS"), instrument=rng.randrange(1, 2**32),
                price=rng.randrange(2**32), qty=rng.randrange(2**32))
    extra = bytes(rng.randrange(256) for _ in range(rng.choice([0, 0, 0, 10, 200])))   # payload after the quote
    kind = rng.choices(
        ["feed", "wrong_port", "wrong_group", "vlan", "ip_options", "fragment",
         "tcp", "bad_msg_type", "bad_side", "short_udp"],
        weights=[50, 6, 6, 4, 4, 4, 4, 4, 4, 4])[0]      # about half are our feed
    if kind == "feed":
        frame = build_frame(msg + extra, dont_fragment=rng.random() < 0.5)
    elif kind == "wrong_port":
        frame = build_frame(msg, dport=FEED_PORT + rng.randrange(1, 100))
    elif kind == "wrong_group":
        frame = build_frame(msg, dst_ip=rng.choice(["239.1.1.2", "239.2.1.1", "224.0.0.1"]))
    elif kind == "vlan":
        frame = build_frame(msg, vlan=100)
    elif kind == "ip_options":
        frame = build_frame(msg, ip_options=True)
    elif kind == "fragment":
        if rng.random() < 0.5:
            frame = build_frame(msg + extra, fragment=True)                     # first fragment
        else:
            frame = build_frame(msg + extra, frag_offset=rng.randrange(1, 8192))  # later fragment
    elif kind == "tcp":
        frame = build_frame(msg, proto=6)
    elif kind == "bad_msg_type":
        frame = build_frame(quote(msg_type="X"))
    elif kind == "bad_side":
        frame = build_frame(quote(side="Z"))
    else:  # short_udp: only 10 payload bytes, so UDP length = 18 < 22 (padding fills the rest)
        frame = build_frame(msg[:10])
    bad_fcs = rng.random() < 0.1                         # 10% of frames are corrupt
    return frame, bad_fcs, kind
