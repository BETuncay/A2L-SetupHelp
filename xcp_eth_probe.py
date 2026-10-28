#!/usr/bin/env python3
"""Minimal XCP-on-Ethernet probe for the VX1060. Standard library only.

Subcommands:
  info  - connect, read DAQ capabilities and all event channels, print matching A2L blocks
  daq   - measure signals on one event channel and report the real sample interval
  poll  - read one signal with SHORT_UPLOAD in a loop (what INCA does today), for comparison

Examples:
  python xcp_eth_probe.py --host 100.100.10.2 --port 5555 info
  python xcp_eth_probe.py daq --event 0 --signal 0x20001234:4 --duration 5
  python xcp_eth_probe.py poll --signal 0x20001234:4 --duration 5
"""
import argparse
import socket
import statistics
import struct
import time

ERRORS = {
    0x00: "ERR_CMD_SYNCH", 0x10: "ERR_CMD_BUSY", 0x11: "ERR_DAQ_ACTIVE", 0x12: "ERR_PGM_ACTIVE",
    0x20: "ERR_CMD_UNKNOWN", 0x21: "ERR_CMD_SYNTAX", 0x22: "ERR_OUT_OF_RANGE",
    0x23: "ERR_WRITE_PROTECTED", 0x24: "ERR_ACCESS_DENIED", 0x25: "ERR_ACCESS_LOCKED",
    0x26: "ERR_PAGE_NOT_VALID", 0x27: "ERR_MODE_NOT_VALID", 0x28: "ERR_SEGMENT_NOT_VALID",
    0x29: "ERR_SEQUENCE", 0x2A: "ERR_DAQ_CONFIG", 0x30: "ERR_MEMORY_OVERFLOW",
    0x31: "ERR_GENERIC", 0x32: "ERR_VERIFY", 0x33: "ERR_RESOURCE_TEMPORARY_NOT_ACCESSIBLE",
}
TIME_UNITS = ["1NS", "10NS", "100NS", "1US", "10US", "100US", "1MS", "10MS", "100MS", "1S"]
UNIT_SECONDS = [10.0 ** e for e in range(-9, 1)]
ID_FIELD_LEN = {0: 1, 1: 2, 2: 3, 3: 4}  # DAQ_KEY_BYTE bits 6..7 -> DTO header length
TS_SIZE = {0: 0, 1: 1, 2: 2, 4: 4}       # TIMESTAMP_MODE bits 0..2 -> timestamp bytes


class XcpError(Exception):
    pass


class XcpEth:
    def __init__(self, host, port, udp=False, timeout=2.0):
        self.udp = udp
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))
        self.ctr = 0
        self.rx = []            # UDP: one datagram may carry several XCP packets
        self.e = "<"            # slave byte order, updated on CONNECT
        self.max_cto = 8
        self.max_dto = 8
        self.dto_handler = None

    def close(self):
        self.sock.close()

    def pack(self, fmt, *values):
        return struct.pack(self.e + fmt, *values)

    # --- transport: XCP on Ethernet header is LEN(2) CTR(2), then the XCP packet ---
    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed by slave")
            buf += chunk
        return buf

    def recv_packet(self):
        if self.udp:
            while not self.rx:
                data = self.sock.recv(65535)
                while len(data) >= 4:
                    length, _ = struct.unpack_from("<HH", data)
                    self.rx.append(data[4:4 + length])
                    data = data[4 + length:]
            return self.rx.pop(0)
        length, _ = struct.unpack("<HH", self._recv_exact(4))
        return self._recv_exact(length)

    def cmd(self, payload):
        self.sock.send(struct.pack("<HH", len(payload), self.ctr) + payload)
        self.ctr = (self.ctr + 1) & 0xFFFF
        while True:
            p = self.recv_packet()
            if p[0] == 0xFF:
                return p
            if p[0] == 0xFE:
                code = p[1] if len(p) > 1 else -1
                raise XcpError(f"command 0x{payload[0]:02X} -> {ERRORS.get(code, hex(code))}")
            if p[0] >= 0xFC:    # EV / SERV packets
                continue
            if self.dto_handler:
                self.dto_handler(p)

    # --- standard commands ---
    def connect(self):
        r = self.cmd(b"\xFF\x00")
        self.e = ">" if r[2] & 0x01 else "<"
        self.max_cto = r[3]
        (self.max_dto,) = struct.unpack_from(self.e + "H", r, 4)
        return dict(resource=r[1], comm_mode=r[2], max_cto=self.max_cto, max_dto=self.max_dto,
                    protocol=r[6] if len(r) > 6 else None, transport=r[7] if len(r) > 7 else None)

    def disconnect(self):
        self.cmd(b"\xFE")

    def upload(self, n):
        out = b""
        while n > 0:
            k = min(n, self.max_cto - 1)
            out += self.cmd(bytes([0xF5, k]))[1:1 + k]
            n -= k
        return out

    def short_upload(self, addr, size, ext=0):
        return self.cmd(bytes([0xF4, size, 0, ext]) + self.pack("I", addr))[1:1 + size]

    # --- DAQ commands ---
    def daq_processor_info(self):
        r = self.cmd(b"\xDA")
        props, max_daq, max_ev, min_daq, key = struct.unpack_from(self.e + "BHHBB", r, 1)
        return dict(props=props, max_daq=max_daq, max_event=max_ev, min_daq=min_daq, key=key)

    def daq_resolution_info(self):
        r = self.cmd(b"\xD9")
        gran, max_entry, _, _, ts_mode, ts_ticks = struct.unpack_from(self.e + "BBBBBH", r, 1)
        return dict(granularity=gran, max_odt_entry=max_entry, ts_mode=ts_mode, ts_ticks=ts_ticks)

    def daq_event_info(self, event):
        r = self.cmd(b"\xD7\x00" + self.pack("H", event))
        props, max_list, name_len, cycle, unit, prio = struct.unpack_from("BBBBBB", r, 1)
        # GET_DAQ_EVENT_INFO leaves the MTA pointing at the event name
        name = self.upload(name_len).decode("ascii", "replace") if name_len else f"Event_{event}"
        return dict(channel=event, props=props, max_daq_list=max_list, name=name,
                    cycle=cycle, unit=unit, priority=prio)

    def setup_daq(self, event, signals, timestamp=True, prescaler=1):
        self.cmd(b"\xD6")                                              # FREE_DAQ
        self.cmd(b"\xD5\x00" + self.pack("H", 1))                      # ALLOC_DAQ: 1 list
        self.cmd(b"\xD4\x00" + self.pack("HB", 0, 1))                  # ALLOC_ODT: 1 ODT
        self.cmd(b"\xD3\x00" + self.pack("HBB", 0, 0, len(signals)))   # ALLOC_ODT_ENTRY
        self.cmd(b"\xE2\x00" + self.pack("HBB", 0, 0, 0))              # SET_DAQ_PTR
        for addr, size in signals:
            self.cmd(bytes([0xE1, 0xFF, size, 0]) + self.pack("I", addr))  # WRITE_DAQ
        mode = 0x10 if timestamp else 0x00
        self.cmd(bytes([0xE0, mode]) + self.pack("HHBB", 0, event, prescaler, 0))  # SET_DAQ_LIST_MODE
        self.cmd(b"\xDE\x02" + self.pack("H", 0))                      # START_STOP_DAQ_LIST: select
        self.cmd(b"\xDD\x01")                                          # START_STOP_SYNCH: start selected

    def stop_daq(self):
        self.cmd(b"\xDD\x00")                                          # START_STOP_SYNCH: stop all


def event_type(props):
    daq, stim = props & 0x04, props & 0x08
    return "DAQ_STIM" if daq and stim else "STIM" if stim else "DAQ"


def unit_name(unit):
    return TIME_UNITS[unit] if unit < len(TIME_UNITS) else f"?{unit}"


def parse_signal(text):
    addr, _, size = text.partition(":")
    return int(addr, 0), int(size or "4", 0)


def cmd_info(x, args):
    try:
        pi = x.daq_processor_info()
    except XcpError as err:
        print(f"GET_DAQ_PROCESSOR_INFO failed: {err}")
        print("-> The slave does not offer DAQ in its current configuration (polling only).")
        return
    p = pi["props"]
    print(f"\nDAQ processor: {'DYNAMIC' if p & 0x01 else 'STATIC'}, "
          f"prescaler={'yes' if p & 0x02 else 'no'}, timestamps={'yes' if p & 0x10 else 'no'}, "
          f"MAX_DAQ={pi['max_daq']}, MAX_EVENT_CHANNEL={pi['max_event']}, MIN_DAQ={pi['min_daq']}, "
          f"ID field type={(pi['key'] >> 6) & 3}")
    try:
        ri = x.daq_resolution_info()
        ts = ri["ts_mode"]
        print(f"Timestamp: {TS_SIZE.get(ts & 7, '?')} byte(s), unit {unit_name(ts >> 4)}, "
              f"ticks {ri['ts_ticks']}, fixed={'yes' if ts & 0x08 else 'no'}")
    except XcpError as err:
        print(f"GET_DAQ_RESOLUTION_INFO failed: {err}")

    events = []
    for ch in range(pi["max_event"]):
        try:
            ev = x.daq_event_info(ch)
        except XcpError as err:
            print(f"GET_DAQ_EVENT_INFO({ch}) failed: {err}")
            continue
        events.append(ev)
        print(f"  Event {ch}: '{ev['name']}' cycle={ev['cycle']} x {unit_name(ev['unit'])} "
              f"type={event_type(ev['props'])} max_daq_list={ev['max_daq_list']} prio={ev['priority']}")
    if not events:
        print("-> No event channels reported. Configure timer/polling events in the VX config "
              "or integrate the VX1000 App Driver.")
        return

    print("\n/* ---- A2L snippet for IF_DATA XCPplus / DAQ ---- */")
    print(f"DYNAMIC\n0x{pi['max_daq']:X}  /* MAX_DAQ */\n0x{len(events):X}  /* MAX_EVENT_CHANNEL */"
          f"\n0x{pi['min_daq']:X}  /* MIN_DAQ */")
    for ev in events:
        short = ev["name"].replace(" ", "_")[:8]
        print(f'/begin EVENT\n  "{ev["name"]}" "{short}" 0x{ev["channel"]:X}\n'
              f'  {event_type(ev["props"])} 0x{ev["max_daq_list"]:X} 0x{ev["cycle"]:X} '
              f'0x{ev["unit"]:X} 0x{ev["priority"]:X}\n/end EVENT')


def cmd_daq(x, args):
    signals = [parse_signal(s) for s in args.signal]
    pi = x.daq_processor_info()
    ri = x.daq_resolution_info()
    id_len = ID_FIELD_LEN[(pi["key"] >> 6) & 3]
    ts_size = TS_SIZE.get(ri["ts_mode"] & 7, 0) if (pi["props"] & 0x10) else 0
    tick_s = ri["ts_ticks"] * UNIT_SECONDS[min(ri["ts_mode"] >> 4, 9)]
    payload = sum(size for _, size in signals)
    if id_len + ts_size + payload > x.max_dto:
        raise SystemExit(f"Signals ({payload} bytes) do not fit into one DTO (MAX_DTO={x.max_dto}).")

    samples = []    # (slave timestamp or None, host time, raw data)

    def on_dto(p):
        data = p[id_len:]
        ts = None
        if ts_size:
            ts = int.from_bytes(data[:ts_size], "little" if x.e == "<" else "big")
            data = data[ts_size:]
        samples.append((ts, time.perf_counter(), data))

    x.dto_handler = on_dto
    x.setup_daq(args.event, signals, timestamp=bool(ts_size), prescaler=args.prescaler)
    print(f"DAQ running on event {args.event} for {args.duration} s ...")
    end = time.perf_counter() + args.duration
    try:
        while time.perf_counter() < end:
            try:
                p = x.recv_packet()
            except socket.timeout:
                continue
            if p[0] < 0xFC:
                on_dto(p)
    finally:
        x.stop_daq()
        x.dto_handler = None

    if len(samples) < 2:
        print(f"Received {len(samples)} sample(s). The event is probably never triggered.")
        return
    if ts_size:
        wrap = 1 << (8 * ts_size)
        dts = [((b[0] - a[0]) % wrap) * tick_s for a, b in zip(samples, samples[1:])]
        source = "slave timestamps"
    else:
        dts = [b[1] - a[1] for a, b in zip(samples, samples[1:])]
        source = "PC receive time (jittery)"
    ms = [d * 1000 for d in dts]
    print(f"{len(samples)} samples, interval from {source}: mean {statistics.mean(ms):.3f} ms, "
          f"min {min(ms):.3f} ms, max {max(ms):.3f} ms, stdev {statistics.pstdev(ms):.3f} ms")
    for ts, _, data in samples[:5]:
        print(f"  ts={ts} data={data[:payload].hex()}")


def cmd_poll(x, args):
    addr, size = parse_signal(args.signal[0])
    times = []
    end = time.perf_counter() + args.duration
    while time.perf_counter() < end:
        x.short_upload(addr, size)
        times.append(time.perf_counter())
    ms = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    print(f"{len(times)} SHORT_UPLOADs, mean interval {statistics.mean(ms):.3f} ms, "
          f"max {max(ms):.3f} ms")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="100.100.10.2")
    ap.add_argument("--port", type=lambda s: int(s, 0), default=5555)
    ap.add_argument("--udp", action="store_true", help="use XCP on UDP instead of TCP")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("info")
    for name in ("daq", "poll"):
        sp = sub.add_parser(name)
        sp.add_argument("--signal", action="append", required=True,
                        help="ADDRESS[:SIZE], e.g. 0x20001234:4 (repeat for several signals)")
        sp.add_argument("--duration", type=float, default=5.0)
        if name == "daq":
            sp.add_argument("--event", type=lambda s: int(s, 0), required=True)
            sp.add_argument("--prescaler", type=int, default=1)
    args = ap.parse_args()

    x = XcpEth(args.host, args.port, udp=args.udp)
    try:
        c = x.connect()
        print(f"Connected: MAX_CTO={c['max_cto']} MAX_DTO={c['max_dto']} "
              f"byte order={'Motorola' if x.e == '>' else 'Intel'} resources=0x{c['resource']:02X} "
              f"(DAQ {'available' if c['resource'] & 0x04 else 'NOT available'})")
        {"info": cmd_info, "daq": cmd_daq, "poll": cmd_poll}[args.command](x, args)
    finally:
        try:
            x.disconnect()
        except Exception:
            pass
        x.close()


if __name__ == "__main__":
    main()
