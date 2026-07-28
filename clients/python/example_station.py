"""Worked example: instrumenting a Python-driven station with emtel.

A vision-inspect-and-sort cell that loops forever. Run it against a dev
collector and watch it appear in the SCADA:

    python example_station.py --host 127.0.0.1 --line PYDEMO

The point of the example is the SHAPE, not the fake work: one `with
em.step(...)` per real phase of the cycle, `waiting_on` around blocking
waits for other equipment, and ordinary exceptions becoming faults.

To make the SCADA compute cycle time and flow losses for this EM, open its
Config tab and set (step names must match the ones below):
    cycle start step     10
    cycle complete step  50
    starved steps        20      (waiting for an incoming part)
    blocked steps        50      (waiting for the outfeed to clear)
"""

from __future__ import annotations

import argparse
import random
import time

from emtel import EquipmentModule


# ── stand-ins for the real equipment calls ───────────────────────────────

def home_axes() -> None:
    time.sleep(0.6)


def await_infeed_part() -> None:
    """Blocks until upstream delivers — this is starved time."""
    time.sleep(random.uniform(0.5, 3.0))


def clamp_part() -> None:
    time.sleep(0.4)


def run_vision() -> bool:
    time.sleep(random.uniform(0.8, 1.4))
    if random.random() < 0.08:
        raise RuntimeError("camera returned no match within 1500 ms")
    return random.random() > 0.15  # pass/fail grade


def sort_part(passed: bool) -> None:
    time.sleep(0.5)


def await_outfeed_clear() -> None:
    """Blocks until downstream takes the part — this is blocked time."""
    time.sleep(random.uniform(0.2, 2.5))


# ── the instrumented cycle ───────────────────────────────────────────────

def run_cycle(em: EquipmentModule) -> None:
    with em.step("10", "Home axes"):
        home_axes()

    with em.step("20", "Load part"):
        with em.waiting_on("Infeed conveyor part present"):
            await_infeed_part()
        clamp_part()

    with em.step("30", "Vision inspect"):
        passed = run_vision()          # a raise here becomes a step fault

    with em.step("40", "Sort"):
        sort_part(passed)

    with em.step("50", "Release part"):
        with em.waiting_on("Outfeed conveyor clear"):
            await_outfeed_clear()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15020)
    ap.add_argument("--line", default="PYDEMO")
    ap.add_argument("--station", default="ST52000")
    ap.add_argument("--em", default="main")
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    em = EquipmentModule(line=args.line, station=args.station,
                         em_label=args.em, collector=(args.host, args.port))

    done = 0
    with em:
        while args.cycles == 0 or done < args.cycles:
            try:
                run_cycle(em)
                done += 1
            except Exception as e:
                # em.step already reported the fault with the exception text;
                # here we do the recovery the operator would otherwise do.
                print(f"cycle failed: {e} — recovering")
                time.sleep(2.0)
                em.clear_fault()
    print(f"completed {done} cycles; sent={em.sent} errors={em.send_errors}")


if __name__ == "__main__":
    main()
