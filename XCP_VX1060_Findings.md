# XCP / VX1060 – Findings

**Goal:** 1 ms sampling via VX1060 (XCP on Ethernet, SWD). **Today:** only polling rate.

## Root cause
The A2L declares **no DAQ event channels** (`IF_DATA XCPplus`, lines 1095–1146):
- `DAQ DYNAMIC 0x0 0x0 0x0` → MAX_EVENT_CHANNEL = 0, no `/begin EVENT` blocks
- Only `OPTIONAL_CMD UPLOAD / SET_MTA`, no DAQ commands
→ INCA can only poll (SET_MTA/UPLOAD round trips, timed by the PC).

## Options
| Route | Needs | Result |
|---|---|---|
| VX timer events | Events configured in VX config + EVENT blocks in A2L | ~1 ms, not task-synchronous |
| VX1000 App Driver | Driver in ECU, event macro in 1 ms task | 1 ms, task-synchronous, consistent |

## Next steps
1. `python xcp_eth_probe.py info` → does the VX report event channels?
2. Put the printed EVENT blocks + DAQ `OPTIONAL_CMD`s into the A2L (or let the VX config tool export it).
3. `python xcp_eth_probe.py daq --event <n> --signal <addr>:4` → check interval ≈ 1.000 ms.
4. If there are no events: configure them in the VX config tool, or integrate the App Driver.

## Python script `xcp_eth_probe.py`
Standalone XCP-on-Ethernet client (standard library only, Python 3). It talks to the VX directly, without INCA or the A2L. Tested against a simulated slave only, not on the real VX yet.
Prerequisites: PC adapter in `100.100.10.x`, INCA disconnected (the VX may allow only one XCP connection).
Options: `--host` (default `100.100.10.2`), `--port` (default `5555`), `--udp` (default TCP). They go **before** the command: `python xcp_eth_probe.py --host 1.2.3.4 info`.

| Command | What it does |
|---|---|
| `info` | CONNECT, then GET_DAQ_PROCESSOR_INFO, GET_DAQ_RESOLUTION_INFO and GET_DAQ_EVENT_INFO for every event. Prints ready-to-paste A2L DAQ lines. |
| `daq --event N --signal ADDR:SIZE [--signal ...] [--duration s] [--prescaler p]` | Sets up 1 DAQ list (1 ODT) on event N, records for `duration` seconds, reports the sample interval from the VX timestamps. |
| `poll --signal ADDR:SIZE` | SHORT_UPLOAD in a loop (polling, like INCA today), for comparison. |

`ADDR` = `ECU_ADDRESS` of a MEASUREMENT (RAM address), `SIZE` in bytes (1/2/4).

**Reading the output**
| Output | Meaning → action |
|---|---|
| `Connected: ... (DAQ available)` | Resource bit DAQ is set → continue. `NOT available` → DAQ is off in the VX config. |
| Connection timeout / refused | Wrong IP or port, PC not in the same subnet, or INCA still connected. |
| `GET_DAQ_PROCESSOR_INFO failed: ... ERR_CMD_UNKNOWN` | VX offers no DAQ at all → polling only. Check the VX config / App Driver. |
| `MAX_EVENT_CHANNEL=0` or `No event channels reported` | DAQ exists, but no events → configure timer events in VX config, or App Driver. |
| `Event 0: 'xyz' cycle=1 x 1MS` | A 1 ms event exists. Its channel number (0) is the value for `--event` and for the A2L. |
| `Timestamp: 4 byte(s), unit 1US` | Timestamp format of the VX. Must match `TIMESTAMP_SUPPORTED` in the A2L. |
| A2L snippet (`MAX_DAQ`, `MAX_EVENT_CHANNEL`, `/begin EVENT`) | Replaces lines 1113–1116 of the A2L; the EVENT blocks go inside `/begin DAQ ... /end DAQ`. |
| `daq`: `mean 1.0xx ms, stdev small` | **Goal reached**: the VX samples at 1 ms → fix the A2L, then use INCA. |
| `daq`: `Received 0/1 sample(s)` | The event never fires (e.g. it needs the App Driver in the ECU). |
| `daq`: mean clearly > 1 ms, or large max | VX/SWD too slow for this signal set → fewer signals, faster SWD clock. |
| `daq`: `... (PC receive time, jittery)` | No timestamps from the VX; the interval was measured on the PC (less accurate). |
| `poll`: mean interval | Polling speed of the script only; not comparable to INCA's polling raster. |
| Other `ERR_*` names | Standard XCP error codes. The failing command is shown as hex (e.g. `0xE1` = WRITE_DAQ → bad address/size). |

**DAQ commands to add to `PROTOCOL_LAYER`** (as `OPTIONAL_CMD ...`):
`SHORT_UPLOAD, GET_DAQ_PROCESSOR_INFO, GET_DAQ_RESOLUTION_INFO, GET_DAQ_EVENT_INFO, FREE_DAQ, ALLOC_DAQ, ALLOC_ODT, ALLOC_ODT_ENTRY, SET_DAQ_PTR, WRITE_DAQ, SET_DAQ_LIST_MODE, START_STOP_DAQ_LIST, START_STOP_SYNCH`

**EVENT block format:** `"name" "short" <channel> DAQ <max_daq_lists> <time_cycle> <time_unit> <priority>`. Time units: 3=1 µs, 6=1 ms, 7=10 ms.

## Glossary
- **DAQ:** the slave sends data by itself on an event. **Polling:** the tool requests each value.
- **Event channel:** a trigger (timer or ECU task) that a DAQ list is attached to.
- **OLDA:** VX1000 App Driver buffer; the ECU copies the signals into it on each event.
- **LMA / VMA:** load address (flash image) / runtime address (RAM).

## Side notes
- MEASUREMENT/CHARACTERISTIC do **not** need IF_DATA (only optional, to restrict events per signal).
- Both MEMORY_SEGMENTs use `SEGMENT 1` → indices should be unique (0, 1). Affects calibration only.
- Connection per A2L: TCP, `100.100.10.2:5555`.

## Finding IP / port
A2L → INCA hardware config (it already connects) → VX config tool (most reliable) → PC adapter in `100.100.10.x`, `ping` + `Test-NetConnection 100.100.10.2 -Port 5555` → Wireshark + VX power cycle. Close INCA before running the script.

## Other A2L checks
- `A2ML` block present (describes `XCPplus`), otherwise tools ignore IF_DATA.
- `MOD_PAR`: `EPK` + `ADDR_EPK` match the ECU build.
- `PAG 0x0` while 2 segments exist → `MAX_SEGMENTS` = 2 if page switching is wanted.

## Which address goes into the A2L
| Object | ECU_ADDRESS |
|---|---|
| MEASUREMENT (RAM variable) | Runtime address = **VMA** (RAM) |
| CHARACTERISTIC (calib) | Flash address = **LMA** (matches the hex file); `ADDRESS_MAPPING` redirects to the RAM copy online |
| NvM / EEPROM data | Address of the **RAM mirror**, not the NV storage |
| Core-local RAM (e.g. TCM) | Global alias address the debug bus can see |

Verify: compare a few ECU_ADDRESS values with the linker map file.
Current A2L: CHARACTERISTIC = LMA, MEASUREMENT = RAM → correct.

## Setup facts
- Cortex-M3: one global address map, debug can reach all RAM. Requirement: ~1 ms, **not** task-synchronous.
- VXconfig OLDA / STIM / resource management / status/config structs = App Driver features → not used without the driver.
- Timestamp type: **VX clock** (no ECU timer without the App Driver).
- Open question: does `info` show VX timer events? If not → ask Vector, or integrate the App Driver (init + one event call in the 1 ms task).

## App Driver integration (outline)
> File and macro names differ between driver versions. Follow the manual in the driver package.

1. **Get the package** from Vector (VX install / support), with a version that matches the VX firmware.
2. **Add to the build:** `VX1000.c`, `VX1000.h`, plus a config header (e.g. `VX1000_cfg.h`).
3. **Configure** (config header): target = Cortex-M, OLDA on, OLDA buffer size, number of events, STIM off, timestamp source (e.g. `DWT->CYCCNT` or a free-running timer).
4. **Linker:** put the driver struct and the OLDA buffer in a RAM section that is not cleared at startup, at the address VXconfig expects (start settings / OLDA addresses).
5. **Code:**
   ```c
   #include "VX1000.h"
   /* startup, after clocks + RAM init */
   VX1000_INIT();             /* or VX1000_INIT_ASYNC_START()/_END() */
   /* end of the 1 ms task, after the signals are updated */
   VX1000_EVENT(0);
   /* optional: VX1000_EVENT(1) in the 10 ms task */
   ```
6. **VXconfig:** memory handling = "use VX1000 App Driver settings", timestamp = ECU native / ECU sync, define events 0/1.
7. **A2L:** take the EVENT blocks from the VX export or from `xcp_eth_probe.py info`, then test with `daq`.

**Cost (estimate, check the map file):** code ~1–4 KB flash; RAM = driver struct (~100s of bytes) + OLDA buffer (configurable, ~1–8 KB typical). CPU: each event call copies the signals assigned to it → a few µs for tens of signals.
