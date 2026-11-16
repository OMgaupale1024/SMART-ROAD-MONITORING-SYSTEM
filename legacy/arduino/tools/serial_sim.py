"""Dev/test tool — write the RoadSense protocol to a serial port (no Arduino needed).

This is NOT part of the app. It exists so you can exercise the *real* serial read path
(SerialWorker + pyserial) without hardware, by pairing two virtual COM ports:

    Windows: install com0com, which creates a linked pair e.g. COM20 <-> COM21
    Linux/macOS: socat -d -d pty,raw,echo=0 pty,raw,echo=0

Then run this against one end and connect RoadSense Desktop to the other:

    python tools/serial_sim.py COM20            # writer
    # in the app, Connect to COM21 at 115200

The app's built-in Developer Simulation Mode does the same thing without a serial port;
use this only when you specifically want to test serial I/O.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time

import serial


def run(port: str, baud: int, rate_hz: float) -> None:
    ser = serial.Serial(port, baud)
    period = 1.0 / rate_hz
    rng = random.Random()
    prev_ay = 0
    counter = 0
    ser.write(b"HELLO,ROADSENSE,1\n")
    print(f"Writing RoadSense protocol to {port} at {baud} baud, {rate_hz:g} Hz. Ctrl+C to stop.")
    try:
        while True:
            counter += 1
            t_ms = int(time.monotonic() * 1000)
            ay = int(1500 * math.sin(counter / 8.0) + rng.gauss(0, 250))
            shock = abs(ay - prev_ay)
            prev_ay = ay
            roll = rng.random()
            status = "NORMAL"
            if roll > 0.985:
                status, shock = "POTHOLE", rng.randint(15000, 22000)
            elif roll > 0.94:
                status, shock = "SPEED_BREAKER", rng.randint(8000, 14000)
            distance = "NA" if rng.random() > 0.9 else round(rng.uniform(5, 120), 1)
            ser.write(f"T,{t_ms},{ay},{shock},{distance},{status}\n".encode())
            if status != "NORMAL":
                ser.write(f"E,{t_ms},{ay},{shock},{distance},{status}\n".encode())
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        ser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Feed the RoadSense serial protocol to a COM port.")
    parser.add_argument("port", help="serial port to write to, e.g. COM20 or /dev/pts/3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--rate", type=float, default=10.0, help="messages per second")
    args = parser.parse_args()
    try:
        run(args.port, args.baud, args.rate)
    except serial.SerialException as exc:
        print(f"Could not open {args.port}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
