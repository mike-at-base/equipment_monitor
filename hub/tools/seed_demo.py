"""Seed a demo line with history, for exercising dashboards and charts.

Writes straight to the database rather than driving the UDP collector: live
senders only ever produce data at the right-hand edge of the window, and the
point of this is to have a week of it. That means the tracker is bypassed, so
this script is responsible for keeping the tables consistent with each other
(a down state_interval needs a matching down_episode, a cycle needs the step
events it is made of).

    python seed_demo.py                 # create DEMO1 with 7 days of history
    python seed_demo.py --days 3
    python seed_demo.py --delete        # remove the line and everything under it

Restart emhub afterwards: it snapshots the equipment hierarchy at startup.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import random

import psycopg2

# same default as emhub (cmd/emhub/main.go); override with --dsn
DSN = "postgres://monitor:monitor@localhost:5432/emhub"

# Two lines, because comparing the same machine across lines is one of the
# main things the comparison widgets are for. DEMO2 reuses DEMO1's station
# and module names on purpose, so a like-for-like comparison is possible.
LINES = ["DEMO1", "DEMO2"]

# ── the fleet ─────────────────────────────────────────────────────────────
# Shaped like a real cell: a main module that calls robots, robots that call
# magazines. Twenty EMs over five stations, so line/station/EM scopes and the
# multi-EM comparisons all have something to chew on.
STATIONS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("ST10000", "Infeed", [
        ("main", "Infeed control"),
        ("conv01", "Infeed conveyor"),
        ("dest01", "Destacker"),
    ]),
    ("ST20000", "Press", [
        ("main", "Press control"),
        ("press01", "Press 1"),
        ("press02", "Press 2"),
    ]),
    ("ST34000", "Cell", [
        ("main", "Cell control"),
        ("rb01", "Robot 1"),
        ("rb02", "Robot 2"),
        ("mag01", "Magazine 1"),
        ("mag02", "Magazine 2"),
        ("mag03", "Magazine 3"),
        ("mag04", "Magazine 4"),
    ]),
    ("ST40000", "Vision", [
        ("main", "Vision control"),
        ("cam01", "Camera 1"),
        ("cam02", "Camera 2"),
    ]),
    ("ST50000", "Outfeed", [
        ("main", "Outfeed control"),
        ("pack01", "Packer 1"),
        ("pack02", "Packer 2"),
        ("pal01", "Palletiser"),
    ]),
]

# the second line is a subset — same names, different behaviour
STATIONS2: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("ST20000", "Press", [
        ("main", "Press control"),
        ("press01", "Press 1"),
        ("press02", "Press 2"),
    ]),
    ("ST34000", "Cell", [
        ("main", "Cell control"),
        ("rb01", "Robot 1"),
        ("mag01", "Magazine 1"),
        ("mag02", "Magazine 2"),
    ]),
]

LAYOUT = {"DEMO1": STATIONS, "DEMO2": STATIONS2}

# Two shifts Mon–Sat with a lunch gap, as minutes from local midnight. The
# gap matters: it is what makes production-aware availability differ from
# wall-clock availability.
SHIFTS = [(360, 840), (870, 1320)]  # 06:00–14:00, 14:30–22:00
SHIFT_DOWS = [1, 2, 3, 4, 5, 6]

STEPS = [
    ("10", "Wait for part", 1.5, 0.6),
    ("20", "Clamp and locate", 4.0, 0.8),
    ("30", "Process", 40.0, 6.0),
    ("40", "Quality check", 6.0, 1.5),
    ("45", "Purge nozzle", 5.0, 1.0),  # tagged NVA on the EMs that have it
    ("50", "Release part", 3.0, 1.0),
]

FAULTS = [
    ("alarm", "AL-104 gripper did not confirm closed"),
    ("alarm", "AL-221 servo following error"),
    ("alarm", "AL-317 air pressure below minimum"),
    ("interlock", "Light curtain 2 broken"),
    ("interlock", "Guard door 4 open"),
    ("fault", "Vacuum not achieved within timeout"),
]
STARVED = [
    "upstream ST10000 has no part ready",
    "magazine empty, awaiting refill",
    "operator load request not acknowledged",
]
BLOCKED = [
    "downstream ST50000 outfeed full",
    "palletiser awaiting pallet change",
    "conveyor stopped, accumulation full",
]


class Profile:
    """How one EM behaves. The spread across the fleet is the point — a
    dashboard where every EM looks the same teaches nothing."""

    def __init__(self, rng: random.Random, label: str, station: str):
        self.rng = rng
        # controllers are fast and boring; the equipment they call is not
        ctrl = label == "main"
        self.base = 12.0 if ctrl else rng.uniform(45, 130)
        self.jitter = 0.06 if ctrl else rng.uniform(0.08, 0.30)
        # a second, slower mode: a real machine that sometimes retries
        self.bimodal = (not ctrl) and rng.random() < 0.35
        self.slow_mult = rng.uniform(1.6, 2.4)
        self.slow_share = rng.uniform(0.05, 0.18)
        # slow creep over the week, so the drift chart has something to find
        self.drift = rng.uniform(-0.04, 0.16)
        self.p_fault = 0.0 if ctrl else rng.uniform(0.001, 0.010)
        # every fleet has one or two problem machines; without them the
        # availability comparison is a flat wall of 98% and shows nothing
        self.bad_actor = (not ctrl) and rng.random() < 0.15
        if self.bad_actor:
            self.p_fault *= 4
        self.repair_mu = 1.6 if self.bad_actor else 1.0
        self.p_starve = rng.uniform(0.01, 0.14)
        self.p_block = rng.uniform(0.01, 0.12)
        self.has_nva = (not ctrl) and rng.random() < 0.4
        self.station = station

    def cycle_seconds(self, progress: float) -> float:
        v = self.base * (1 + self.drift * progress)
        if self.bimodal and self.rng.random() < self.slow_share:
            v *= self.slow_mult
        return max(1.0, self.rng.gauss(v, v * self.jitter))


def local_tz() -> dt.tzinfo:
    tz = dt.datetime.now().astimezone().tzinfo
    assert tz is not None
    return tz


def shift_windows(days: int, tz: dt.tzinfo) -> list[tuple[dt.datetime, dt.datetime]]:
    """Shift windows over the last `days` days, ending now."""
    now = dt.datetime.now(tz)
    out = []
    for d in range(days, -1, -1):
        day = (now - dt.timedelta(days=d)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        # weekday() is Mon=0; SHIFT_DOWS is Sun=0 to match schedule_shift
        dow = (day.weekday() + 1) % 7
        if dow not in SHIFT_DOWS:
            continue
        for start_min, end_min in SHIFTS:
            s = day + dt.timedelta(minutes=start_min)
            e = day + dt.timedelta(minutes=end_min)
            if s >= now:
                continue
            out.append((s, min(e, now)))
    return out


def upsert_hierarchy(cur, line: str) -> dict[tuple[str, str, str], int]:
    cur.execute(
        "INSERT INTO line (name, display_name) VALUES (%s,%s) "
        "ON CONFLICT (name) DO UPDATE SET display_name=EXCLUDED.display_name "
        "RETURNING id", (line, f"Demo line {line[-1]}"))
    line_id = cur.fetchone()[0]

    cur.execute("DELETE FROM schedule_shift WHERE line_id=%s", (line_id,))
    for dow in SHIFT_DOWS:
        for s, e in SHIFTS:
            cur.execute(
                "INSERT INTO schedule_shift (line_id,dow,start_min,end_min) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (line_id, dow, s, e))

    ids: dict[tuple[str, str, str], int] = {}
    for station, st_display, ems in LAYOUT[line]:
        cur.execute(
            "INSERT INTO station (line_id, name, display_name) VALUES (%s,%s,%s) "
            "ON CONFLICT (line_id, name) DO UPDATE SET display_name=EXCLUDED.display_name "
            "RETURNING id", (line_id, station, st_display))
        station_id = cur.fetchone()[0]
        for label, display in ems:
            cur.execute(
                "INSERT INTO em (station_id, em_label, display_name, confirmed, wire_version) "
                "VALUES (%s,%s,%s,TRUE,5) "
                "ON CONFLICT (station_id, em_label) DO UPDATE SET "
                "  display_name=EXCLUDED.display_name, confirmed=TRUE "
                "RETURNING id", (station_id, label, display))
            ids[(line, station, label)] = cur.fetchone()[0]
    return ids


def write_sequence(cur, em_id: int, has_nva: bool) -> None:
    cur.execute(
        "INSERT INTO sequence (em_id, seq_index, name, is_production, "
        "  cycle_start_step, cycle_complete_step, starved_steps, blocked_steps, nva_steps) "
        "VALUES (%s,1,'main',TRUE,'10','50','10','50',%s) "
        "ON CONFLICT (em_id, seq_index) DO UPDATE SET "
        "  is_production=TRUE, cycle_start_step='10', cycle_complete_step='50', "
        "  starved_steps='10', blocked_steps='50', nva_steps=EXCLUDED.nva_steps",
        (em_id, "45" if has_nva else ""))


def generate(em_id: int, prof: Profile, windows, buffers) -> None:
    """Walk each shift emitting contiguous state intervals, and the cycles and
    step events that go with the productive ones."""
    rng = prof.rng
    states, steps, cycles, episodes, ops = buffers
    steps_used = [s for s in STEPS if s[0] != "45" or prof.has_nva]
    total_span = (windows[-1][1] - windows[0][0]).total_seconds() if windows else 1

    for w_start, w_end in windows:
        t = w_start
        while t < w_end:
            progress = (t - windows[0][0]).total_seconds() / total_span
            roll = rng.random()

            if roll < prof.p_fault:
                # a fault: down interval, matching episode, and an ack part way
                mins = rng.lognormvariate(prof.repair_mu, 0.8)
                end = min(t + dt.timedelta(minutes=mins), w_end)
                rtype, reason = rng.choice(FAULTS)
                ack = t + (end - t) * rng.uniform(0.15, 0.6)
                states.append((t, em_id, end, "down", rtype, reason, 1, "30", ack))
                episodes.append((t, em_id, end, rtype, reason, 1, "30", ack,
                                 rng.randint(0, 2),
                                 int((end - t).total_seconds() * 1000)))
                ops.append((ack, em_id, "ack"))
                t = end
                continue

            if roll < prof.p_fault + prof.p_starve:
                end = min(t + dt.timedelta(seconds=rng.uniform(20, 240)), w_end)
                states.append((t, em_id, end, "starved", "flow",
                               rng.choice(STARVED), 1, "10", None))
                t = end
                continue

            if roll < prof.p_fault + prof.p_starve + prof.p_block:
                end = min(t + dt.timedelta(seconds=rng.uniform(20, 200)), w_end)
                states.append((t, em_id, end, "blocked", "flow",
                               rng.choice(BLOCKED), 1, "50", None))
                t = end
                continue

            # a normal cycle: split the duration across the steps in proportion
            # to their nominal weights, so step spread and cycle time agree
            secs = prof.cycle_seconds(progress)
            end = t + dt.timedelta(seconds=secs)
            if end > w_end:
                break
            weights = [rng.gauss(base, sd) for _, _, base, sd in steps_used]
            weights = [max(0.2, x) for x in weights]
            scale = secs / sum(weights)

            st = t
            nva_ms = 0
            for (name, desc, _, _), wgt in zip(steps_used, weights):
                dur = wgt * scale
                s_end = st + dt.timedelta(seconds=dur)
                steps.append((st, em_id, s_end, 1, name, desc,
                              int(dur * 1000), False, ""))
                if name == "45":
                    nva_ms = int(dur * 1000)
                st = s_end

            # the whole cycle is productive except any purge step, which is
            # running-but-not-adding-value
            if nva_ms:
                nva_start = end - dt.timedelta(milliseconds=nva_ms)
                states.append((t, em_id, nva_start, "productive", "", "", 1, "30", None))
                states.append((nva_start, em_id, end, "nva", "", "Purge nozzle",
                               1, "45", None))
            else:
                states.append((t, em_id, end, "productive", "", "", 1, "30", None))

            work_ms = int(secs * 1000 * 0.82)
            # a few cycles are cut short by an intervention rather than
            # completing normally
            interrupted = rng.random() < 0.004
            cycles.append((t, em_id, end, 1,
                           t + dt.timedelta(milliseconds=work_ms), work_ms,
                           int(secs * 1000) - work_ms, int(secs * 1000), interrupted))
            t = end

        # keep the timeline contiguous between shifts so gaps read as "off",
        # not as a hole in the data
        if t < w_end:
            states.append((t, em_id, w_end, "standby", "", "", 1, "", None))


def copy_in(cur, table: str, cols: str, rows: list[tuple]) -> None:
    if not rows:
        return
    buf = io.StringIO()
    for r in rows:
        buf.write("\t".join(
            r"\N" if v is None else
            ("t" if v is True else "f" if v is False else
             (v.isoformat() if isinstance(v, dt.datetime) else str(v)))
            for v in r) + "\n")
    buf.seek(0)
    cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN", buf)


def delete_all(cur) -> None:
    cur.execute("SELECT id, name FROM line WHERE name = ANY(%s)", (LINES,))
    rows = cur.fetchall()
    if not rows:
        print("nothing to delete")
        return
    for line_id, name in rows:
        cur.execute("""
            SELECT e.id FROM em e
            JOIN station s ON s.id = e.station_id
            WHERE s.line_id = %s""", (line_id,))
        ids = [r[0] for r in cur.fetchall()]
        for table in ("state_interval", "step_event", "cycle", "mode_interval",
                      "down_episode", "operator_event"):
            cur.execute(f"DELETE FROM {table} WHERE em_id = ANY(%s)", (ids,))
        # line cascades to station -> em -> sequence
        cur.execute("DELETE FROM line WHERE id=%s", (line_id,))
        print(f"deleted {name}: {len(ids)} EMs and their history")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dsn", default=DSN)
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    conn = psycopg2.connect(a.dsn)
    conn.autocommit = False
    cur = conn.cursor()

    if a.delete:
        delete_all(cur)
        conn.commit()
        return 0

    tz = local_tz()
    windows = shift_windows(a.days, tz)
    if not windows:
        print("no shift windows in that range")
        return 1
    print(f"{len(windows)} shift windows, "
          f"{windows[0][0]:%Y-%m-%d %H:%M} .. {windows[-1][1]:%Y-%m-%d %H:%M}")

    delete_all(cur)  # idempotent: re-seeding replaces, never doubles up
    ids: dict[tuple[str, str, str], int] = {}
    for line in LINES:
        got = upsert_hierarchy(cur, line)
        print(f"{len(got)} EMs on {line}")
        ids.update(got)

    states: list[tuple] = []
    steps: list[tuple] = []
    cycles: list[tuple] = []
    episodes: list[tuple] = []
    ops: list[tuple] = []

    for i, ((line, station, label), em_id) in enumerate(sorted(ids.items())):
        rng = random.Random(a.seed * 1000 + i)
        prof = Profile(rng, label, station)
        write_sequence(cur, em_id, prof.has_nva)
        generate(em_id, prof, windows, (states, steps, cycles, episodes, ops))
        print(f"  {line}/{station}/{label:8s} base={prof.base:6.1f}s "
              f"{'bimodal ' if prof.bimodal else '        '}"
              f"{'BAD ' if prof.bad_actor else '    '}"
              f"drift={prof.drift:+.0%} fault={prof.p_fault:.1%}")

    print(f"writing {len(cycles)} cycles, {len(steps)} step events, "
          f"{len(states)} state intervals, {len(episodes)} down episodes")
    copy_in(cur, "state_interval",
            "start_ts,em_id,end_ts,state,reason_type,reason,seq_index,step_name,ack_ts", states)
    copy_in(cur, "step_event",
            "start_ts,em_id,end_ts,seq_index,step_name,step_desc,duration_ms,was_faulted,branch_taken", steps)
    copy_in(cur, "cycle",
            "start_ts,em_id,end_ts,seq_index,work_end_ts,work_ms,exchange_ms,total_ms,interrupted", cycles)
    copy_in(cur, "down_episode",
            "start_ts,em_id,end_ts,reason_type,reason,seq_index,step_name,ack_ts,retries,down_ms", episodes)
    copy_in(cur, "operator_event", "ts,em_id,event", ops)
    conn.commit()
    print("done — restart emhub so it picks up the new equipment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
