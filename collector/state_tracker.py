"""
Per-EM state machine.  Receives value-change notifications from OpcClient,
detects edges, and writes events to the database.

Step / fault state is tracked per sequence index because each stepControl[N]
slot is independent — the PLC writes to stepControl[N] when sequence N is
active, and other slots may change independently (e.g. a fault in seq 1 while
seq 2 is running).

EM-level availability signals (automatic / em_fault / running) are scalars
because they apply to the whole EM regardless of which sequence is active.

Down event / fault reason tracking
───────────────────────────────────
When the machine becomes unavailable, one ``em_down_event`` row is opened
with a placeholder reason.  The OpcClient then performs an asynchronous
on-demand read of the relevant PLC struct (activeStepBranch for step faults,
interlock for EM-level faults) to enrich the reason with descriptions of
the conditions that were actually blocking.

This design keeps the tracker PLC-library-agnostic: no constants for branch
count or condition count are required — whatever the struct read returns is
what we use.  Resilient to library changes.

Sticky root cause: once a down event opens, secondary events (door opens
after a fault → manual mode; piled-on interlocks) do NOT replace the
original reason.  The row is closed only when the machine returns to
productive or standby.
"""
from __future__ import annotations

import datetime
import logging
from typing import Callable

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

        # ── Down event — root-cause tracking (sticky) ─────────────────────────
        self._down_start_ts:    datetime.datetime | None = None
        self._down_reason_type: str | None               = None
        self._down_reason_desc: str | None               = None
        self._down_seq_idx:     int | None               = None
        self._down_step:        str | None               = None
        self._down_fault_msg:   str | None               = None

        # Optional callback fired the moment a down event opens.  OpcClient
        # sets this to schedule an async on-demand read of the relevant PLC
        # struct (activeStepBranch / interlock) so the placeholder reason can
        # be enriched with live condition descriptions.
        #
        # Signature: callable(em_id: int, reason_type: str, seq_idx: int | None)
        self.on_down_event_opened: Callable[[int, str, int | None], None] | None = None

    # ── Down-event helpers ────────────────────────────────────────────────────

    def _try_open_down_event(
        self, ts: datetime.datetime,
        reason_type: str, reason_desc: str | None,
        seq_idx: int | None = None,
        step_name: str | None = None,
        fault_msg: str | None = None,
    ) -> None:
        """
        Open a new down event — silently no-ops if one is already open.
        This is the sticky-root-cause guarantee: the first event wins.

        On open, fires ``on_down_event_opened`` so the OpcClient can perform
        an async PLC read to enrich the placeholder reason.
        """
        if self._down_start_ts is not None:
            return

        self._down_start_ts    = ts
        self._down_reason_type = reason_type
        self._down_reason_desc = reason_desc
        self._down_seq_idx     = seq_idx
        self._down_step        = step_name
        self._down_fault_msg   = fault_msg

        log.debug("[%s/%s] down event OPEN  type=%s desc=%s",
                  self.station, self.em_label, reason_type, reason_desc)
        try:
            conn = get_pool().getconn()
            try:
                q.open_down_event(
                    conn, self.em_id, ts,
                    reason_type, reason_desc,
                    seq_idx, step_name, fault_msg,
                )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("open_down_event failed em=%d", self.em_id)

        # Notify listener so OpcClient can enrich reason via async PLC read
        if self.on_down_event_opened is not None:
            try:
                self.on_down_event_opened(self.em_id, reason_type, seq_idx)
            except Exception:
                log.exception("on_down_event_opened callback failed em=%d",
                              self.em_id)

    def update_down_event_reason(self, reason_desc: str,
                                  reason_type: str | None = None) -> None:
        """
        Replace ``reason_desc`` (and optionally ``reason_type``) on the
        currently-open down event.  Called by OpcClient after an async PLC
        struct read completes.  No-op if the event has already been closed.

        ``reason_type`` is used to demote 'interlock' → 'manual' when the
        on-demand interlock read shows no failing conditions.
        """
        if self._down_start_ts is None:
            return
        start_ts = self._down_start_ts
        self._down_reason_desc = reason_desc
        if reason_type is not None:
            self._down_reason_type = reason_type
        log.debug("[%s/%s] down event UPDATE type=%s desc=%s",
                  self.station, self.em_label,
                  self._down_reason_type, reason_desc)
        try:
            conn = get_pool().getconn()
            try:
                q.update_down_event_reason(
                    conn, self.em_id, start_ts, reason_desc, reason_type,
                )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("update_down_event_reason failed em=%d", self.em_id)

    def _try_close_down_event(self, ts: datetime.datetime) -> None:
        """Close the current down event (no-op if none is open)."""
        if self._down_start_ts is None:
            return

        start_ts = self._down_start_ts
        log.debug("[%s/%s] down event CLOSE type=%s desc=%s dur=%.0fs",
                  self.station, self.em_label,
                  self._down_reason_type, self._down_reason_desc,
                  (ts - start_ts).total_seconds())

        # Clear in-memory state before the DB write so that any exception
        # in the write doesn't leave the tracker stuck.
        self._down_start_ts    = None
        self._down_reason_type = None
        self._down_reason_desc = None
        self._down_seq_idx     = None
        self._down_step        = None
        self._down_fault_msg   = None

        try:
            conn = get_pool().getconn()
            try:
                q.close_down_event(conn, self.em_id, start_ts, ts)
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("close_down_event failed em=%d", self.em_id)

    def _check_down_event(self, ts: datetime.datetime) -> None:
        """
        Called after any EM-level signal changes (automatic / em_fault / running).
        Opens or closes the current down event based on the derived SEMI E10 state.

        Step-fault down events are opened earlier (in on_fault_change) so the
        reason carries seq_idx + step name from the start.  This method
        handles:
          • Recovery: productive or standby  → close any open event
          • Manual mode                      → open with placeholder
          • EM-level fault with no step fault → open with placeholder

        In every open case the OpcClient async enricher will replace the
        placeholder text once the PLC struct read completes.
        """
        if self._automatic is None or self._em_fault is None or self._running is None:
            return

        if self._em_fault:
            state = "unscheduled_down"
        elif self._automatic and self._running:
            state = "productive"
        elif self._automatic and not self._running:
            state = "standby"
        else:
            state = "manual"

        if state in ("productive", "standby"):
            self._try_close_down_event(ts)

        elif state == "manual":
            # Could be operator-initiated stop OR an interlock blocking
            # auto-mode entry.  Open with interlock reason_type so the
            # async read of the interlock struct runs; if no conditions
            # are failing, the enricher rewrites the reason to "Manual mode".
            self._try_open_down_event(
                ts,
                reason_type="interlock",
                reason_desc="Manual / interlock (reading conditions...)",
            )

        elif state == "unscheduled_down":
            # EM-level fault rose without an accompanying step-fault notification.
            # Open with a placeholder; if a step-fault edge follows, the sticky
            # guard prevents overwrite, but on_fault_change will still update
            # the seq_idx context.  Treat this as interlock-class so the
            # interlock struct read fires.
            self._try_open_down_event(
                ts,
                reason_type="interlock",
                reason_desc="EM fault (reading conditions...)",
            )

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

        if faulted:
            # Rising edge — open down event with placeholder reason.  The
            # OpcClient enricher will replace this with descriptions of the
            # failed permissive conditions once the activeStepBranch read
            # completes.  _try_open_down_event no-ops if one is already
            # open (sticky root cause).
            step    = self._step.get(seq_idx)
            ext_msg = self._ext_msg.get(seq_idx)
            placeholder = ext_msg or f"Step {step or '?'} faulted"
            self._try_open_down_event(
                ts,
                reason_type="step_fault",
                reason_desc=placeholder,
                seq_idx=seq_idx,
                step_name=step,
                fault_msg=ext_msg,
            )

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
        self._check_down_event(ts)

    def on_em_fault_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._em_fault:
            return
        self._em_fault = val
        self._emit_availability_raw(ts)
        self._check_down_event(ts)

    def on_running_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._running:
            return
        self._running = val
        self._emit_availability_raw(ts)
        self._check_down_event(ts)
