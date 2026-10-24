#!/usr/bin/env python3
"""Reads/writes the E22-900T22U LoRa USB dongle's own configuration
registers (air data rate, UART baud rate, ...) over the same USB-serial
port the robot normally uses for transparent-mode traffic (link/
lora_server.py on Pi #1, lora_link.py on Pi #2).

2026-10-24 (Fabrice, explicit request): the link was found to be
saturated by the DGPS correction relay alone at the project's original
2.4 kbps air data rate (~300 B/s) -- not enough room left for ordinary
telemetry commands, let alone the automatic image snap fallback. This
tool exists to raise that air data rate (accepting shorter range in
exchange, by the module's own physical design -- a faster air rate needs
a better signal-to-noise ratio to decode, so it reaches less far for the
same transmit power) on BOTH dongles, since they must agree to keep
talking to each other at all.

IMPORTANT -- READ BEFORE RUNNING:

1. This dongle has NO GPIO M0/M1 pins exposed (unlike the bare E22-900T22S/D
   module) -- configuration mode is entered with its own PHYSICAL SIDE
   BUTTON: press and hold it for about 2 seconds until its LED goes to a
   STEADY/SOLID red (not blinking). Do this by hand, on the actual
   dongle, before running this script with --write. This script cannot
   do that step for you over the wire.
2. The protocol here (0xC0 write+save / 0xC1 read / the 9-byte register
   block) is well cross-validated across two independent open-source
   implementations written for this exact module family, but is NOT
   something Ebyte's own official manual could be retrieved to confirm
   word-for-word while writing this tool. Run with no flags (or just
   --port) first: that only READS and prints the current configuration,
   never writes anything -- do that on a bench, away from the robot,
   before trusting it in the field.
3. After a successful --write, press the dongle's button again (or
   unplug/replug it) to leave configuration mode and return to normal
   transparent-mode operation -- verify this actually happens (LED
   leaves the steady-red state) before considering the dongle done;
   this script can't confirm that part for you either.
4. Run this on EACH dongle in turn (Pi #1's and Pi #2's) with the SAME
   --air-rate and --uart-rate values -- a mismatch between the two ends
   means they can no longer understand each other at all.
5. Do this with the robot's own services stopped (run_robot.sh on Pi #1,
   the robot-webserver service on Pi #2) and LORA_ENABLED temporarily
   left as-is -- nothing here talks to those services, but having them
   also polling the same serial port while you're mid-reconfiguration
   just invites a collision on the wire.

After BOTH dongles are reconfigured and verified back in normal mode,
update each Pi's .env to match (see the printed summary at the end of
a successful run, and this project's README/.env.example):
  - LORA_BAUD_RATE=<your --uart-rate value>            (both Pi #1 and Pi #2)
  - LORA_AIR_RATE_BYTES_PER_S=<air rate in kbps * 1000 / 8>   (Pi #1 only --
    this is what link/server.py's LoraLinkBudget token bucket uses to
    model the real channel capacity; leaving it at the old 300 after
    raising the real air rate would keep the robot needlessly throttling
    itself below the new, faster link)
Then restart both services.

Usage examples:
  # Read-only, safe to run any time (module must already be in config
  # mode -- press the button first):
  python3 lora_configure.py --port /dev/ttyUSB0

  # Actually reconfigure to 9.6 kbps air rate / 19200 baud UART (the
  # 2026-10-24 target -- see this project's protocol docs page):
  python3 lora_configure.py --port /dev/ttyUSB0 --air-rate 9.6 --uart-rate 19200 --write
"""
import argparse
import sys
import time

# Confirmed register map for the SX1262/SX1268-based E22 family (NOT the
# older E32 family, which uses different chips and an incompatible
# layout) -- 9 bytes starting at address 0x00:
#   0x00 ADDH        0x01 ADDL        0x02 NETID
#   0x03 REG0 (SPED: UART baud [7:5] | parity [4:3] | air data rate [2:0])
#   0x04 REG1 (OPTION: TX power, RSSI-ambient enable, packet size, ...)
#   0x05 REG2 (CHAN: channel)
#   0x06 REG3 (TRANSMISSION_MODE: fixed/transparent, LBT, relay, ...)
#   0x07 CRYPT_H     0x08 CRYPT_L     (write-only; reads back as 0x00 if
#                                      no key is set -- matches this
#                                      project's current "no encryption
#                                      yet" state, see the protocol page)
CONFIG_START_ADDR = 0x00
CONFIG_LENGTH = 0x09

CMD_READ = 0xC1
CMD_WRITE_SAVE = 0xC0

# REG0 bits 7-5: UART baud rate (Pi <-> dongle, NOT the over-the-air rate).
UART_BAUD_TO_BITS = {
    1200: 0b000,
    2400: 0b001,
    4800: 0b010,
    9600: 0b011,  # module's own factory default
    19200: 0b100,
    38400: 0b101,
    57600: 0b110,
    115200: 0b111,
}
BITS_TO_UART_BAUD = {v: k for k, v in UART_BAUD_TO_BITS.items()}

# REG0 bits 2-0: air data rate (the real over-the-air throughput this
# whole exercise is about) -- 2.4 kbps is this project's original
# default (~300 bytes/s); 9.6 kbps (~1200 bytes/s) is the 2026-10-24
# target after the link was found saturated by DGPS corrections alone.
AIR_RATE_TO_BITS = {
    0.3: 0b000,
    1.2: 0b001,
    2.4: 0b010,  # module's own factory default
    4.8: 0b011,
    9.6: 0b100,
    19.2: 0b101,
    38.4: 0b110,
    62.5: 0b111,
}
BITS_TO_AIR_RATE = {v: k for k, v in AIR_RATE_TO_BITS.items()}

PARITY_BITS_NAME = {0b00: "8N1", 0b01: "8O1", 0b10: "8E1", 0b11: "8N1"}


def decode_reg0(reg0: int) -> dict:
    return {
        "uart_baud_bits": (reg0 >> 5) & 0b111,
        "parity_bits": (reg0 >> 3) & 0b11,
        "air_rate_bits": reg0 & 0b111,
    }


def describe_config(payload: bytes) -> str:
    if len(payload) != CONFIG_LENGTH:
        return f"(unexpected length: {len(payload)} bytes, expected {CONFIG_LENGTH})"
    addh, addl, netid, reg0, reg1, reg2, reg3, crypt_h, crypt_l = payload
    d = decode_reg0(reg0)
    uart_baud = BITS_TO_UART_BAUD.get(d["uart_baud_bits"], f"unknown ({d['uart_baud_bits']:03b})")
    air_rate = BITS_TO_AIR_RATE.get(d["air_rate_bits"], f"unknown ({d['air_rate_bits']:03b})")
    parity = PARITY_BITS_NAME.get(d["parity_bits"], f"unknown ({d['parity_bits']:02b})")
    lines = [
        f"  ADDH/ADDL     : 0x{addh:02X}{addl:02X}",
        f"  NETID         : 0x{netid:02X}",
        f"  REG0 (0x{reg0:02X})     : UART baud={uart_baud}  parity={parity}  air_rate={air_rate} kbps",
        f"  REG1 (0x{reg1:02X})     : TX power / packet size / option bits (left untouched by this tool)",
        f"  REG2 (0x{reg2:02X})     : channel (left untouched by this tool)",
        f"  REG3 (0x{reg3:02X})     : transmission mode bits (left untouched by this tool)",
        f"  CRYPT_H/L     : 0x{crypt_h:02X}{crypt_l:02X}"
        + ("  (no key set -- matches this project's current no-encryption state)" if crypt_h == 0 and crypt_l == 0
           else "  (non-zero -- a key IS set; this tool echoes it back unchanged, never touches it)"),
    ]
    return "\n".join(lines)


def read_config(ser) -> bytes:
    ser.reset_input_buffer()
    ser.write(bytes([CMD_READ, CONFIG_START_ADDR, CONFIG_LENGTH]))
    ser.flush()
    reply = ser.read(3 + CONFIG_LENGTH)
    if len(reply) != 3 + CONFIG_LENGTH:
        raise RuntimeError(
            f"Got {len(reply)} bytes back, expected {3 + CONFIG_LENGTH}. "
            "Is the module actually in configuration mode (steady red LED, "
            "button held ~2s)? Is --port/--baud correct?"
        )
    header, addr, length = reply[0], reply[1], reply[2]
    if header not in (CMD_READ, CMD_WRITE_SAVE) or addr != CONFIG_START_ADDR or length != CONFIG_LENGTH:
        raise RuntimeError(f"Unexpected reply header: {reply[:3].hex()} -- not a valid config read response")
    return reply[3:]


def write_config(ser, payload: bytes) -> bytes:
    assert len(payload) == CONFIG_LENGTH
    ser.reset_input_buffer()
    ser.write(bytes([CMD_WRITE_SAVE, CONFIG_START_ADDR, CONFIG_LENGTH]) + payload)
    ser.flush()
    time.sleep(0.2)  # the module needs a short moment to commit to EEPROM before it replies
    reply = ser.read(3 + CONFIG_LENGTH)
    if len(reply) != 3 + CONFIG_LENGTH:
        raise RuntimeError(
            f"Write sent, but got {len(reply)} bytes back instead of {3 + CONFIG_LENGTH} -- "
            "the module may not have accepted it. Re-read (run with no --write) to check "
            "the actual current state before trying again."
        )
    return reply[3:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True, help="Serial device for the dongle, e.g. /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=9600,
                         help="Baud rate to TALK to the module while it's already in configuration mode "
                              "(this project's current default is 9600 -- not the --uart-rate you're setting "
                              "below, which only takes effect for its NEXT boot into normal/transparent mode)")
    parser.add_argument("--air-rate", type=float, choices=sorted(AIR_RATE_TO_BITS), default=None,
                         help="New air data rate in kbps (the real over-the-air throughput). Omit to leave unchanged.")
    parser.add_argument("--uart-rate", type=int, choices=sorted(UART_BAUD_TO_BITS), default=None,
                         help="New UART baud rate between the Pi and the dongle (separate from --air-rate -- "
                              "see the project's protocol docs page). Omit to leave unchanged.")
    parser.add_argument("--write", action="store_true",
                         help="Actually write the change (default: read-only, just prints the current config)")
    args = parser.parse_args()

    import serial  # deferred so --help works even without pyserial installed

    print(f"Opening {args.port} @ {args.baud} baud (must already be in configuration mode -- steady red LED)...")
    with serial.Serial(args.port, args.baud, timeout=2.0) as ser:
        try:
            current = read_config(ser)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        print("\nCurrent configuration:")
        print(describe_config(current))

        if args.air_rate is None and args.uart_rate is None:
            print("\nNo --air-rate/--uart-rate given -- read-only run, nothing changed.")
            return 0

        reg0 = current[3]
        d = decode_reg0(reg0)
        new_uart_bits = UART_BAUD_TO_BITS[args.uart_rate] if args.uart_rate is not None else d["uart_baud_bits"]
        new_air_bits = AIR_RATE_TO_BITS[args.air_rate] if args.air_rate is not None else d["air_rate_bits"]
        new_reg0 = (new_uart_bits << 5) | (d["parity_bits"] << 3) | new_air_bits
        new_payload = bytearray(current)
        new_payload[3] = new_reg0

        print(f"\nPlanned change: REG0 0x{reg0:02X} -> 0x{new_reg0:02X}")
        print("New configuration would be:")
        print(describe_config(bytes(new_payload)))

        if not args.write:
            print("\n(Dry run -- pass --write to actually apply this. Nothing changed.)")
            return 0

        confirm = input("\nType YES to write this to the module's EEPROM now: ").strip()
        if confirm != "YES":
            print("Aborted -- nothing written.")
            return 1

        try:
            after = write_config(ser, bytes(new_payload))
        except RuntimeError as exc:
            print(f"ERROR during write: {exc}", file=sys.stderr)
            return 1

        if after != bytes(new_payload):
            print("\nWARNING: readback after write does not match what was sent -- "
                  "re-run with no --write to check the module's actual current state "
                  "before trusting it.", file=sys.stderr)
            print("Readback:")
            print(describe_config(after))
            return 1

        print("\nWrite confirmed by readback. Configuration is now:")
        print(describe_config(after))
        print(
            "\nNext steps:\n"
            "  1. Press the dongle's button again (or unplug/replug it) to leave\n"
            "     configuration mode -- confirm the LED stops being steady red.\n"
            "  2. Repeat this whole procedure on the OTHER Pi's dongle with the SAME\n"
            "     --air-rate/--uart-rate values.\n"
            "  3. Update .env on each Pi: LORA_BAUD_RATE=<your --uart-rate>\n"
            "     (Pi #1 only, additionally) LORA_AIR_RATE_BYTES_PER_S="
            f"{int((args.air_rate or BITS_TO_AIR_RATE[d['air_rate_bits']]) * 1000 / 8)}\n"
            "  4. Restart both services and verify telemetry/DGPS/snap all still work."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
