"""emtel — equipment-monitor telemetry client for Python-driven equipment.

Speaks the same UDP wire format as the PLC's EquipmentModuleTelemetry FB
(wire v4), so a Python script appears in the SCADA exactly like a PLC EM:
auto-registration, state timeline, step events, cycles, availability,
redundancy models. Nothing on the collector side knows the difference.

    from emtel import EquipmentModule

    em = EquipmentModule(line="MOD1", station="ST52000", em_label="main",
                         collector=("10.0.1.50", 15020))

    with em:                                    # heartbeat starts; EM = standby
        while True:
            with em.step("10", "Home axes"):
                home_axes()
            with em.step("20", "Load part"):
                with em.waiting_on("Upstream part present"):
                    conveyor.await_part()       # starved/blocked per UI config
                load()
            with em.step("30", "Weld"):
                weld()                          # an exception here -> step fault

Design rules:
  * Telemetry NEVER breaks production code. Every send is wrapped, the
    heartbeat runs on a daemon thread, and errors are counted (send_errors)
    rather than raised.
  * The collector marks an EM offline after 10 s of silence, so the
    heartbeat runs at 1 Hz — 10x margin.
  * Step names are the join key with the UI config (cycle start/complete,
    starved/blocked steps). Keep them stable, same discipline as the PLC.

Single file, standard library only — copy it next to your script.

Self-test (verifies a datagram reaches a collector):
    python emtel.py --selftest --line TEST --station ST99000 --host 10.0.1.50
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager

__all__ = ["EquipmentModule", "WIRE_VERSION"]

WIRE_VERSION = 4
PAYLOAD_LEN = 1142
DEFAULT_PORT = 15020

# The collector's offline sweeper fires at 10 s; stay well inside it.
HEARTBEAT_SEC = 1.0

_MSG_EVENT = 1
_MSG_HEARTBEAT = 2

# status bits — must match hub/internal/wire/wire.go
_AUTOMATIC = 0x0001
_FAULT = 0x0002
_RUNNING = 0x0004
_PAUSED = 0x0008
_STOPPED = 0x0010
_STEP_FAULT = 0x0040
_INTERLOCK_OK = 0x0080
_EXT_ALARM = 0x0100
_RESET = 0x0200

_MODE_BITS = {
    "idle": 0x0001, "step_mode": 0x0002, "mes_bypass": 0x0004,
    "dry_cycle": 0x0008, "end_of_cycle": 0x0010, "pause_at_home": 0x0020,
    "request_entry": 0x0040,
}

# S7 string capacities, in wire order
_W_STATION, _W_EM, _W_STEP, _W_DESC = 32, 16, 60, 200
_W_ALARM, _W_ILK, _W_COND, _W_WAIT, _W_LINE = 200, 160, 200, 200, 32


def _s7(text: str, maxlen: int) -> bytes:
    """Encode as an S7 STRING: [max][len][chars...], padded to max."""
    raw = (text or "").encode("latin-1", "replace")[:maxlen]
    return bytes((maxlen, len(raw))) + raw + bytes(maxlen - len(raw))


class EquipmentModule:
    """One equipment module reporting to the collector.

    A script that drives several independent modules can create several
    instances; each keeps its own identity, state, and heartbeat.
    """

    def __init__(self, line: str, station: str, em_label: str = "main",
                 collector=("127.0.0.1", DEFAULT_PORT), sequence: int = 1,
                 heartbeat: float = HEARTBEAT_SEC):
        if not line or not station:
            raise ValueError("line and station are required")
        self.line = line
        self.station = station
        self.em_label = em_label or "main"
        self.collector = (collector[0], int(collector[1]))
        self.heartbeat = float(heartbeat)

        self.sent = 0
        self.send_errors = 0
        self.last_error: str = ""

        self._lock = threading.RLock()
        self._sock = None
        self._thread = None
        self._stopping = threading.Event()
        self._seq = 0

        # wire state
        self._bits = _AUTOMATIC | _INTERLOCK_OK  # in auto, idle -> standby
        self._modes = 0
        self._active_sequence = int(sequence)
        self._step = ""
        self._step_desc = ""
        self._step_started = 0.0
        self._alarm = ""
        self._interlock_fails = ""
        self._fault_conds = ""
        self._waiting_on = ""

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> "EquipmentModule":
        """Open the socket and begin heartbeating. Idempotent."""
        with self._lock:
            if self._thread is not None:
                return self
            self._stopping.clear()
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            except OSError as e:  # pragma: no cover - platform dependent
                self._note_error(e)
                return self
            self._thread = threading.Thread(
                target=self._heartbeat_loop, name=f"emtel-{self.station}",
                daemon=True)
            self._thread.start()
        self._emit(_MSG_EVENT)
        return self

    def stop(self, final: str = "standby") -> None:
        """Stop heartbeating.

        final="standby" sends a last standby datagram so the EM reads idle
        (available, not producing) rather than snapping straight to a fault;
        it drifts to offline ~10 s later. final="" sends nothing.
        """
        with self._lock:
            if self._thread is None:
                return
            if final == "standby":
                self._bits = _AUTOMATIC | _INTERLOCK_OK
                self._step = self._step_desc = ""
                self._alarm = self._fault_conds = self._waiting_on = ""
        if final == "standby":
            self._emit(_MSG_EVENT)
        self._stopping.set()
        with self._lock:
            thread, self._thread = self._thread, None
        thread.join(timeout=2 * self.heartbeat)
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def __enter__(self) -> "EquipmentModule":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        # A crashed script is honestly a faulted machine: report it, then let
        # the EM go offline rather than pretending it parked cleanly.
        if exc_type is not None:
            self.fault(f"{exc_type.__name__}: {exc}", conditions="script aborted")
            self.stop(final="")
        else:
            self.stop()
        return False

    def online(self) -> "EquipmentModule":
        """Alias for use as `with em.online():` — reads better in scripts."""
        return self

    # ── steps ────────────────────────────────────────────────────────────

    @contextmanager
    def step(self, name: str, description: str = ""):
        """Run a block as one step: sets RUNNING, clears on exit.

        An exception inside the block raises a step fault carrying the
        exception text as the fault condition, then re-raises — your own
        error handling is unaffected.
        """
        self._enter_step(name, description)
        try:
            yield self
        except BaseException as e:
            self.fault(f"{name} failed", conditions=f"{type(e).__name__}: {e}")
            raise
        else:
            with self._lock:
                # leaving a step clears the waiting-on reason with it
                self._waiting_on = ""
            self._emit(_MSG_EVENT)

    def _enter_step(self, name: str, description: str) -> None:
        with self._lock:
            self._step = str(name)
            self._step_desc = description
            self._step_started = time.monotonic()
            self._waiting_on = ""
            # entering a step means the module is executing
            self._bits = (self._bits | _RUNNING | _AUTOMATIC | _INTERLOCK_OK)
            self._bits &= ~(_FAULT | _STEP_FAULT | _PAUSED)
            self._alarm = self._fault_conds = ""
        self._emit(_MSG_EVENT)

    @contextmanager
    def waiting_on(self, reason: str):
        """Mark the current step as waiting on an external condition.

        The hub classifies this as starved or blocked based on the step
        lists configured for this EM in the UI; an unconfigured step stays
        productive.
        """
        with self._lock:
            self._waiting_on = reason
        self._emit(_MSG_EVENT)
        try:
            yield self
        finally:
            with self._lock:
                self._waiting_on = ""
            self._emit(_MSG_EVENT)

    @contextmanager
    def sequence(self, index: int):
        """Report a different sequence index for the duration of the block."""
        with self._lock:
            previous, self._active_sequence = self._active_sequence, int(index)
        try:
            yield self
        finally:
            with self._lock:
                self._active_sequence = previous

    # ── explicit state control ───────────────────────────────────────────

    def standby(self) -> None:
        """In automatic, idle — available but not producing."""
        self._set(bits=_AUTOMATIC | _INTERLOCK_OK, step="", desc="",
                  alarm="", conds="", waiting="")

    def pause(self, reason: str = "Operator pause") -> None:
        self._set(bits=_AUTOMATIC | _PAUSED | _INTERLOCK_OK, alarm=reason)

    def manual(self) -> None:
        """Out of automatic — excluded from the availability denominator."""
        self._set(bits=_INTERLOCK_OK, step="", desc="", waiting="")

    def automatic(self) -> None:
        with self._lock:
            self._bits |= _AUTOMATIC | _INTERLOCK_OK
        self._emit(_MSG_EVENT)

    def fault(self, alarm: str, conditions: str = "", step_fault: bool = True) -> None:
        """Report the module as down. `conditions` carries the permissives
        or exception detail; the UI shows "alarm — conditions"."""
        bits = _AUTOMATIC | _FAULT | _INTERLOCK_OK
        if step_fault:
            bits |= _STEP_FAULT
        self._set(bits=bits, alarm=alarm, conds=conditions, waiting="")

    def clear_fault(self) -> None:
        self._set(bits=_AUTOMATIC | _INTERLOCK_OK, alarm="", conds="")

    def interlock_fail(self, conditions: str) -> None:
        """Drop interlock-OK — recorded as down with an interlock reason,
        whatever mode the equipment lands in."""
        self._set(bits=_AUTOMATIC, ilk=conditions)

    def interlock_clear(self) -> None:
        with self._lock:
            self._bits |= _INTERLOCK_OK
            self._interlock_fails = ""
        self._emit(_MSG_EVENT)

    def reset(self) -> None:
        """Pulse the reset bit (recorded as an operator event)."""
        with self._lock:
            self._bits |= _RESET
        self._emit(_MSG_EVENT)
        with self._lock:
            self._bits &= ~_RESET
        self._emit(_MSG_EVENT)

    def mode(self, **flags: bool) -> None:
        """Set mode flags, e.g. mode(dry_cycle=True, mes_bypass=False)."""
        with self._lock:
            for name, on in flags.items():
                mask = _MODE_BITS.get(name)
                if mask is None:
                    raise ValueError(
                        f"unknown mode {name!r}; known: {sorted(_MODE_BITS)}")
                self._modes = self._modes | mask if on else self._modes & ~mask
        self._emit(_MSG_EVENT)

    def _set(self, *, bits=None, step=None, desc=None, alarm=None,
             conds=None, ilk=None, waiting=None) -> None:
        with self._lock:
            if bits is not None:
                self._bits = bits
            if step is not None:
                self._step = step
            if desc is not None:
                self._step_desc = desc
            if alarm is not None:
                self._alarm = alarm
            if conds is not None:
                self._fault_conds = conds
            if ilk is not None:
                self._interlock_fails = ilk
            if waiting is not None:
                self._waiting_on = waiting
        self._emit(_MSG_EVENT)

    # ── wire ─────────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while not self._stopping.wait(self.heartbeat):
            self._emit(_MSG_HEARTBEAT)

    def _emit(self, msg_type: int) -> None:
        """Build and send one datagram. Never raises."""
        try:
            with self._lock:
                if self._sock is None:
                    return
                self._seq = (self._seq + 1) & 0xFFFFFFFF
                step_ms = 0
                if self._step and self._step_started:
                    step_ms = int((time.monotonic() - self._step_started) * 1000)
                packet = self._encode(msg_type, self._seq, step_ms)
                sock = self._sock
            sock.sendto(packet, self.collector)
            self.sent += 1
        except Exception as e:  # telemetry must never break the caller
            self._note_error(e)

    def _encode(self, msg_type: int, seq: int, step_ms: int) -> bytes:
        head = struct.pack(
            ">BBHHhIIQ", WIRE_VERSION, msg_type, self._bits, self._modes,
            self._active_sequence, seq, max(0, step_ms), time.time_ns())
        body = (
            _s7(self.station, _W_STATION)
            + _s7(self.em_label, _W_EM)
            + _s7(self._step, _W_STEP)
            + _s7(self._step_desc, _W_DESC)
            + _s7(self._alarm, _W_ALARM)
            + _s7(self._interlock_fails, _W_ILK)
            + _s7(self._fault_conds, _W_COND)
            + _s7(self._waiting_on, _W_WAIT)
            + _s7(self.line, _W_LINE)
        )
        packet = head + body
        assert len(packet) == PAYLOAD_LEN, f"payload {len(packet)} != {PAYLOAD_LEN}"
        return packet

    def _note_error(self, exc: Exception) -> None:
        self.send_errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<EquipmentModule {self.line}/{self.station}/{self.em_label} "
                f"sent={self.sent} errors={self.send_errors}>")


# ── self-test CLI ────────────────────────────────────────────────────────

def _selftest(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="emtel", description="send a short telemetry burst to a collector")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--host", default="127.0.0.1", help="collector host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--line", default="TEST")
    ap.add_argument("--station", default="ST99000")
    ap.add_argument("--em", default="main")
    ap.add_argument("--seconds", type=int, default=12)
    args = ap.parse_args(argv)

    em = EquipmentModule(line=args.line, station=args.station,
                         em_label=args.em, collector=(args.host, args.port))
    print(f"emtel v{WIRE_VERSION} -> {args.host}:{args.port} "
          f"as {args.line}/{args.station}/{args.em}")
    with em:
        deadline = time.time() + args.seconds
        n = 0
        while time.time() < deadline:
            n += 1
            with em.step("10", "Self-test step A"):
                time.sleep(1.5)
            with em.step("20", "Self-test step B"):
                with em.waiting_on("Self-test simulated wait"):
                    time.sleep(1.5)
            if n == 2:
                em.fault("Self-test fault", conditions="injected by --selftest")
                time.sleep(2)
                em.clear_fault()
    print(f"sent={em.sent} errors={em.send_errors} last_error={em.last_error or '-'}")
    if em.send_errors:
        print("FAILED — check host/port and firewall", file=sys.stderr)
        return 1
    print(f"OK — look for {args.line}/{args.station} in the SCADA "
          "(it registers as unconfirmed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
