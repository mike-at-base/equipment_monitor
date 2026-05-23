"""
Per-EM state machine.  Receives value-change notifications from OpcClient,
detects edges, and writes events to the database.

Step / fault state is tracked per sequence index because each stepControl[N]
slot is independent — the PLC writes to stepControl[N] when sequence N is
active, and other slots may change independently (e.g. a fault in seq 1 while
seq 2 is running).

EM-level availability signals (automatic / em_fault / running) are scalars
because they apply to the whole EM regardless of which sequence is active.
"""
from __future__ import annotations

import datetime
import logging

import db.queries as q
from db.connection import get_pool

log = logging.getLogger(__name__)

INITIAL_STEP = "SEQUENCE_INITIAL_STEP"


class EMStateTracker:
    def __init__(self, em_id: int, station: str, em_label: str,
                 seq_indices: list[int]) -> None:
        self.em_id      = em_id
        self.station    = station
        self.em_label   = em_label
        self.seq_indices = seq_indices

        # Active sequence — informational only; step events are tagged by
        # which stepControl[N] fired, not by reading activeSequence.
        self._active_seq: int | None = None

        # Step state — one entry per sequence index
        self._step:       dict[int, str | None]               = {i: None  for i in seq_indices}
        self._step_desc:  dict[int, str | None]               = {i: None  for i in seq_indices}
        self._step_start: dict[int, datetime.datetime | None] = {i: None  for i in seq_indices}

        # Fault state — one entry per sequence index
        self._faulted:         dict[int, bool]                    = {i: False for i in seq_indices}
        self._fault_id:        dict[int, int | None]              = {i: None  for i in seq_indices}
        self._fault_start:     dict[int, datetime.datetime | None] = {i: None for i in seq_indices}
        self._fault_step:      dict[int, str | None]              = {i: None  for i in seq_indices}
        self._fault_step_desc: dict[int, str | None]              = {i: None  for i in seq_indices}
        self._ext_msg:         dict[int, str | None]              = {i: None  for i in seq_indices}

        # EM-level raw signals — written on every change to em_availability_raw.
        # Initialised to None so the first OPC notification always triggers a
        # write regardless of whether the PLC value happens to be False.
        self._automatic: bool | None = None
        self._em_fault:  bool | None = None
        self._running:   bool | None = None

    # ── Startup correction ───────────────────────────────────────────────────

    def flush_current_step(self, ts: datetime.datetime) -> None:
        """
        Called ~2 s after subscription startup once the initial burst has
        settled.  The burst fires all subscribed nodes simultaneously; the
        last notification written wins, which may be an idle sequence's
        STEP_STOP even though a different sequence is actively running.

        Priority:
          1. Known active sequence (non-zero) and its current step.
          2. First sequence whose step is not STEP_STOP (heuristic when
             activeSequence = 0 / not yet reported by PLC).
        """
        seq  = self._active_seq          # None or 0 when PLC not in a seq
        step = self._step.get(seq) if seq else None
        desc = self._step_desc.get(seq) if seq else None

        if not step:
            # Fallback: find any sequence that isn't parked at STEP_STOP
            for i in self.seq_indices:
                s = self._step.get(i)
                if s and s != "STEP_STOP":
                    seq, step, desc = i, s, self._step_desc.get(i)
                    break

        if not step:
            return

        log.debug("[%s/%s] flush_current_step → seq=%s step=%s",
                  self.station, self.em_label, seq, step)
        try:
            conn = get_pool().getconn()
            try:
                q.upsert_current_step(conn, self.em_id, seq, step, desc, ts)
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("flush_current_step failed em=%d", self.em_id)

    # ── Tag callbacks (called from OpcClient subscription handler) ──────────

    def on_active_seq_change(self, val: int | None,
                             ts: datetime.datetime) -> None:
        """Track which sequence is active and refresh em_current_step to match."""
        if val == self._active_seq:
            return
        self._active_seq = val
        log.debug("[%s/%s] activeSequence -> %s", self.station, self.em_label, val)

        # Immediately update em_current_step with the new active sequence's
        # current step so the dashboard switches instantly on sequence change.
        if val is None:
            return
        step = self._step.get(val)
        if not step:
            return
        try:
            conn = get_pool().getconn()
            try:
                q.upsert_current_step(
                    conn, self.em_id, val, step,
                    self._step_desc.get(val), ts,
                )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("upsert_current_step (seq change) failed em=%d", self.em_id)

    def on_step_change(self, seq_idx: int, step_name: str,
                       ts: datetime.datetime) -> None:
        step_name = step_name.strip() if step_name else ""

        if step_name == self._step.get(seq_idx):
            return

        prev_step  = self._step.get(seq_idx)
        prev_start = self._step_start.get(seq_idx)

        duration_ms: int | None = None
        if prev_start is not None and prev_step is not None:
            duration_ms = int((ts - prev_start).total_seconds() * 1000)

        try:
            conn = get_pool().getconn()
            try:
                # Write step_event for the DEPARTING step
                if prev_step is not None:
                    q.insert_step_event(
                        conn, self.em_id, seq_idx,
                        ts=ts,
                        step_name=prev_step,
                        step_desc=self._step_desc.get(seq_idx),
                        duration_ms=duration_ms,
                        was_faulted=self._faulted.get(seq_idx, False),
                    )
                # Track the ARRIVING step for the live status dashboard,
                # but only when this sequence is the active one.  All
                # stepControl[N] slots fire on subscription startup; without
                # this guard the last notification wins regardless of which
                # sequence is actually running.
                # When _active_seq is None (not yet received) we allow any
                # write; on_active_seq_change will correct it once known.
                # 0 means "no sequence active" on S7-1500 — treat same as None
                is_active = (not self._active_seq or
                             self._active_seq == seq_idx)
                if step_name and is_active:
                    q.upsert_current_step(
                        conn, self.em_id, seq_idx, step_name,
                        self._step_desc.get(seq_idx), ts,
                    )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("on_step_change failed em=%d seq=%d",
                          self.em_id, seq_idx)

        self._step[seq_idx]       = step_name
        self._step_start[seq_idx] = ts

    def on_step_desc_change(self, seq_idx: int, desc: str | None,
                            ts: datetime.datetime) -> None:
        """Update step description and patch em_current_step if active sequence."""
        self._step_desc[seq_idx] = desc
        step = self._step.get(seq_idx)
        if not step:
            return
        is_active = (self._active_seq is None or self._active_seq == seq_idx)
        if not is_active:
            return
        try:
            conn = get_pool().getconn()
            try:
                q.upsert_current_step(conn, self.em_id, seq_idx, step, desc, ts)
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("upsert_current_step (desc) failed em=%d seq=%d",
                          self.em_id, seq_idx)

    def on_fault_change(self, seq_idx: int, faulted: bool,
                        ts: datetime.datetime) -> None:
        if faulted == self._faulted.get(seq_idx, False):
            return
        self._faulted[seq_idx] = faulted

        try:
            conn = get_pool().getconn()
            try:
                if faulted:
                    # Rising edge — open fault record
                    self._fault_start[seq_idx]     = ts
                    self._fault_step[seq_idx]      = self._step.get(seq_idx)
                    self._fault_step_desc[seq_idx] = self._step_desc.get(seq_idx)
                    fault_id = q.insert_fault_start(
                        conn, self.em_id, seq_idx,
                        fault_start=ts,
                        step_name=self._fault_step[seq_idx],
                        step_desc=self._fault_step_desc[seq_idx],
                        ext_msg=self._ext_msg.get(seq_idx),
                    )
                    self._fault_id[seq_idx] = fault_id
                else:
                    # Falling edge — close fault record
                    fid = self._fault_id.get(seq_idx)
                    if fid is not None:
                        f_start = self._fault_start.get(seq_idx) or ts
                        dur_ms  = int((ts - f_start).total_seconds() * 1000)
                        q.close_fault(conn, fid, fault_end=ts, duration_ms=dur_ms)
                        self._fault_id[seq_idx] = None
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("fault_change failed em=%d seq=%d", self.em_id, seq_idx)

    def _emit_availability_raw(self, ts: datetime.datetime) -> None:
        """Write a snapshot of the three raw signals to em_availability_raw.
        Skipped until all three values have been received at least once."""
        if self._automatic is None or self._em_fault is None or self._running is None:
            return
        try:
            conn = get_pool().getconn()
            try:
                q.insert_availability_raw(
                    conn, self.em_id, ts,
                    self._automatic, self._em_fault, self._running,
                )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("insert_availability_raw failed em=%d", self.em_id)

    def on_automatic_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._automatic:
            return
        self._automatic = val
        self._emit_availability_raw(ts)

    def on_em_fault_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._em_fault:
            return
        self._em_fault = val
        self._emit_availability_raw(ts)

    def on_running_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._running:
            return
        self._running = val
        self._emit_availability_raw(ts)
