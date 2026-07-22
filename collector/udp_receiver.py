"""
UDP telemetry receiver — counterpart to the PLC EquipmentModuleTelemetry FB.

The FB pushes one 622-byte datagram (typeEquipmentModuleTelemetry, S7
Serialize layout, big-endian) on every EM state/step change plus a 1 s
heartbeat snapshot.  This receiver parses each datagram and feeds the same
EMStateTracker callbacks the OPC UA subscription handler uses — trackers
dedupe unchanged values, so running both sources simultaneously is safe
(whichever arrives first wins; telemetry is scan-cycle fresh, so it
usually does).

Datagrams are mapped to an EM by (source IP, stationName, emLabel), where
source IP must match the host in the PLC's opc_endpoint.

Wire contract: see plc/TELEMETRY.md.  Bump _WIRE_VERSION together with the
UDT's version field.
"""
from __future__ import annotations

import datetime
import logging
import struct
from asyncio import DatagramProtocol
from urllib.parse import urlparse

from collector.state_tracker import EMStateTracker

log = logging.getLogger(__name__)

_WIRE_VERSION = 1
_PAYLOAD_LEN = 622

# statusBits
_BIT_AUTOMATIC   = 0x0001
_BIT_FAULT       = 0x0002
_BIT_RUNNING     = 0x0004
_BIT_PAUSED      = 0x0008
_BIT_STOPPED     = 0x0010
_BIT_UNKNOWN     = 0x0020
_BIT_STEP_FAULT  = 0x0040
_BIT_INTERLOCK_OK = 0x0080
_BIT_EXT_ALARM   = 0x0100

# Max plausible skew between PLC clock and collector clock before we fall
# back to receive time (PLC clocks drift / may never have been set).
_MAX_CLOCK_SKEW_S = 120

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _s7_string(buf: bytes, offset: int, max_len: int) -> str:
    """Parse an S7 String[max_len]: [max][len][chars...]."""
    cur = buf[offset + 1]
    if cur > max_len:
        cur = max_len
    return buf[offset + 2: offset + 2 + cur].decode("latin-1").strip()


def parse_payload(data: bytes) -> dict | None:
    """Parse one telemetry datagram; None if malformed/wrong version."""
    if len(data) < _PAYLOAD_LEN:
        return None
    if data[0] != _WIRE_VERSION:
        return None
    status_bits, seq = struct.unpack_from(">HI", data, 2)
    plc_time_ns, = struct.unpack_from(">Q", data, 8)
    active_seq, = struct.unpack_from(">h", data, 16)
    step_active_ms, = struct.unpack_from(">i", data, 18)
    return {
        "msg_type": data[1],
        "status_bits": status_bits,
        "seq": seq,
        "plc_time_ns": plc_time_ns,
        "active_seq": active_seq,
        "step_active_ms": step_active_ms,
        "station": _s7_string(data, 22, 32),
        "em_label": _s7_string(data, 56, 16),
        "step": _s7_string(data, 74, 60),
        "step_desc": _s7_string(data, 136, 200),
        "alarm_msg": _s7_string(data, 338, 200),
        "interlock_first_fail": _s7_string(data, 540, 80),
    }


def build_registry(clients) -> dict[tuple[str, str, str], EMStateTracker]:
    """(plc_ip, station_lower, em_label_lower) → tracker, from OpcClients."""
    registry: dict[tuple[str, str, str], EMStateTracker] = {}
    for client in clients:
        host = urlparse(client.endpoint).hostname or ""
        for tracker in client._trackers.values():
            key = (host, tracker.station.lower(), tracker.em_label.lower())
            registry[key] = tracker
    return registry


class TelemetryReceiver(DatagramProtocol):
    def __init__(self, registry: dict[tuple[str, str, str], EMStateTracker]) -> None:
        self._registry = registry
        self._last_seq: dict[tuple[str, str, str], int] = {}
        self._unknown_logged: set[tuple[str, str, str]] = set()

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            payload = parse_payload(data)
            if payload is None:
                log.debug("telemetry: malformed datagram from %s (%d bytes)",
                          addr[0], len(data))
                return
            key = (addr[0], payload["station"].lower(), payload["em_label"].lower())
            tracker = self._registry.get(key)
            if tracker is None:
                if key not in self._unknown_logged:
                    self._unknown_logged.add(key)
                    log.warning("telemetry: no EM configured for %s %s/%s",
                                addr[0], payload["station"], payload["em_label"])
                return
            self._check_seq(key, payload["seq"])
            self._dispatch(tracker, payload)
        except Exception:
            log.exception("telemetry: datagram handling failed from %s", addr)

    def _check_seq(self, key, seq: int) -> None:
        last = self._last_seq.get(key)
        if last is not None:
            if seq == last:
                return  # duplicate delivery — trackers dedupe anyway
            gap = seq - last - 1
            if 0 < gap < 1000:
                log.warning("telemetry: %s/%s missed %d datagram(s) "
                            "(heartbeat will self-heal)", key[1], key[2], gap)
            elif gap < 0 and seq > 10:
                log.warning("telemetry: %s/%s sequence went backwards "
                            "(%d -> %d)", key[1], key[2], last, seq)
            # seq <= 10 after a big drop = PLC restart; silent
        self._last_seq[key] = seq

    @staticmethod
    def _timestamp(plc_time_ns: int) -> datetime.datetime:
        now = datetime.datetime.now(datetime.timezone.utc)
        if plc_time_ns <= 0:
            return now
        ts = _EPOCH + datetime.timedelta(microseconds=plc_time_ns / 1000.0)
        if abs((ts - now).total_seconds()) > _MAX_CLOCK_SKEW_S:
            return now  # PLC clock not trustworthy
        return ts

    def _dispatch(self, tracker: EMStateTracker, p: dict) -> None:
        ts = self._timestamp(p["plc_time_ns"])
        bits = p["status_bits"]

        # The PLC never clears status.alarm.message after a fault recovers,
        # so only trust it while an alarm bit is actually asserted.
        alarm_active = bool(bits & (_BIT_FAULT | _BIT_STEP_FAULT | _BIT_EXT_ALARM))
        alarm_msg = (p["alarm_msg"] or None) if alarm_active else None

        # Context first (reason sources), so any down event opened by the
        # state/fault handlers below sees fresh values from THIS datagram.
        tracker.on_alarm_msg_change(alarm_msg, ts)
        tracker.on_interlock_snapshot(
            None if (bits & _BIT_INTERLOCK_OK) else
            (p["interlock_first_fail"] or "Interlock not OK"), ts,
        )

        active_seq = p["active_seq"] if p["active_seq"] > 0 else None
        tracker.on_active_seq_change(active_seq, ts)

        if active_seq is not None and active_seq in tracker.seq_indices:
            # The datagram's alarm message is scan-consistent with the fault
            # bit — feed it through the ext-msg path so step-fault events and
            # down-event reasons carry the PLC's own fault text immediately.
            tracker._ext_msg[active_seq] = alarm_msg
            tracker._ext_msg_active[active_seq] = alarm_active
            tracker.on_step_desc_change(active_seq, p["step_desc"] or None, ts)
            tracker.on_step_change(active_seq, p["step"], ts)
            tracker.on_fault_change(active_seq, bool(bits & _BIT_STEP_FAULT), ts)

        # All six EM-level signals applied atomically — per-signal callbacks
        # here would emit transient half-applied runtime states.
        tracker.on_status_snapshot(
            automatic=bool(bits & _BIT_AUTOMATIC),
            em_fault=bool(bits & _BIT_FAULT),
            running=bool(bits & _BIT_RUNNING),
            paused=bool(bits & _BIT_PAUSED),
            stopped=bool(bits & _BIT_STOPPED),
            unknown_status=bool(bits & _BIT_UNKNOWN),
            ts=ts,
        )
