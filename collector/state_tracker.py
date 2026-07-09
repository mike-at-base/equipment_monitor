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
# If description flips very close to a step edge, it's often already the
# ARRIVING step's description. Keep the prior description for the DEPARTING
# step event in that case.
_DESC_EDGE_GUARD_MS = 250
_INTERLOCK_REASON_MAX_AGE_S = 30


class EMStateTracker:
    def __init__(self, em_id: int, station: str, em_label: str,
                 seq_indices: list[int],
                 seq_is_production: dict[int, bool] | None = None,
                 seq_blocked_steps: dict[int, set[str]] | None = None,
                 seq_starved_steps: dict[int, set[str]] | None = None) -> None:
        self.em_id      = em_id
        self.station    = station
        self.em_label   = em_label
        self.seq_indices = seq_indices
        self._seq_is_production = seq_is_production or {}
        self._seq_blocked_steps = seq_blocked_steps or {}
        self._seq_starved_steps = seq_starved_steps or {}

        # Active sequence — informational only; step events are tagged by
        # which stepControl[N] fired, not by reading activeSequence.
        self._active_seq: int | None = None

        # Step state — one entry per sequence index
        self._step:       dict[int, str | None]               = {i: None  for i in seq_indices}
        self._step_desc:  dict[int, str | None]               = {i: None  for i in seq_indices}
        self._step_start: dict[int, datetime.datetime | None] = {i: None  for i in seq_indices}
        self._step_desc_ts:      dict[int, datetime.datetime | None] = {i: None for i in seq_indices}
        self._step_desc_prev:    dict[int, str | None] = {i: None for i in seq_indices}
        self._step_desc_prev_ts: dict[int, datetime.datetime | None] = {i: None for i in seq_indices}

        # Fault state — one entry per sequence index
        # Initialise to None so the first fault notification after collector
        # startup is always processed (important for restart reconciliation).
        self._faulted:         dict[int, bool | None]             = {i: None  for i in seq_indices}
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
        self._paused:    bool | None = False
        self._stopped:   bool | None = False
        self._unknown_status: bool | None = False
        # Last timestamp written to em_availability_raw for this EM.
        # Some PLC notifications share identical source timestamps; ensure
        # strict monotonic writes so "latest row" queries are deterministic.
        self._last_avail_ts: datetime.datetime | None = None
        self._runtime_state: str | None = None

        # ── Down event — root-cause tracking (sticky) ─────────────────────────
        self._down_start_ts:    datetime.datetime | None = None
        self._down_reason_type: str | None               = None
        self._down_reason_desc: str | None               = None
        self._down_seq_idx:     int | None               = None
        self._down_step:        str | None               = None
        self._down_fault_msg:   str | None               = None

        # Most recent parsed interlock health snapshot (from OPC interlock
        # struct datachange subscription). Used to distinguish true interlock
        # stops from operator-initiated HMI stops.
        self._interlock_reason: str | None = None
        self._interlock_has_fail: bool = False
        self._interlock_ts: datetime.datetime | None = None

        # Flow-loss substate while still available (blocked/starved).
        self._flow_start_ts: datetime.datetime | None = None
        self._flow_kind: str | None = None
        self._flow_reason_desc: str | None = None
        self._flow_seq_idx: int | None = None
        self._flow_step_name: str | None = None

        # Optional callback fired the moment a down event opens.  OpcClient
        # sets this to schedule an async on-demand read of the relevant PLC
        # struct (activeStepBranch / interlock) so the placeholder reason can
        # be enriched with live condition descriptions.
        #
        # Signature:
        # callable(em_id: int, reason_type: str, seq_idx: int | None,
        #          start_ts: datetime.datetime | None)
        self.on_down_event_opened: Callable[
            [int, str, int | None, datetime.datetime | None], None
        ] | None = None
        self.on_flow_event_opened: Callable[
            [int, str, int | None, datetime.datetime | None], None
        ] | None = None

    @staticmethod
    def _normalize_step_desc(step_name: str | None, desc: str | None) -> str | None:
        """
        PLC bug workaround: STEP_STOP can retain the previous step description.
        Force description to NULL whenever the step is STEP_STOP.
        """
        if step_name == "STEP_STOP":
            return None
        return desc

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

        should_enrich = False
        try:
            conn = get_pool().getconn()
            try:
                existing = q.get_open_down_event(conn, self.em_id)
                if existing:
                    self._down_start_ts = existing["start_ts"]
                    self._down_reason_type = existing["reason_type"]
                    self._down_reason_desc = existing["reason_desc"]
                    self._down_seq_idx = existing["seq_index"]
                    self._down_step = existing["step_name"]
                    self._down_fault_msg = existing["fault_msg"]
                    # Collector restart can adopt an open placeholder event
                    # from DB; re-run enrichment to replace placeholder text.
                    should_enrich = self._down_reason_type in ("interlock", "step_fault")
                else:
                    self._down_start_ts    = ts
                    self._down_reason_type = reason_type
                    self._down_reason_desc = reason_desc
                    self._down_seq_idx     = seq_idx
                    self._down_step        = step_name
                    self._down_fault_msg   = fault_msg

                    log.debug("[%s/%s] down event OPEN  type=%s desc=%s",
                              self.station, self.em_label, reason_type, reason_desc)
                    q.open_down_event(
                        conn, self.em_id, ts,
                        reason_type, reason_desc,
                        seq_idx, step_name, fault_msg,
                    )
                    conn.commit()
                    should_enrich = True
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("open_down_event failed em=%d", self.em_id)

        # Notify listener so OpcClient can enrich reason via async PLC read
        if should_enrich and self.on_down_event_opened is not None:
            try:
                enrich_type = self._down_reason_type or reason_type
                enrich_seq = self._down_seq_idx if self._down_seq_idx is not None else seq_idx
                self.on_down_event_opened(
                    self.em_id, enrich_type, enrich_seq, self._down_start_ts,
                )
            except Exception:
                log.exception("on_down_event_opened callback failed em=%d",
                              self.em_id)

    def update_down_event_reason(self, reason_desc: str,
                                  reason_type: str | None = None,
                                  start_ts_override: datetime.datetime | None = None) -> None:
        """
        Replace ``reason_desc`` (and optionally ``reason_type``) on the
        currently-open down event.  Called by OpcClient after an async PLC
        struct read completes.  No-op if the event has already been closed.

        ``reason_type`` is used to demote 'interlock' → 'manual' when the
        on-demand interlock read shows no failing conditions.
        """
        if start_ts_override is not None:
            start_ts = start_ts_override
        elif self._down_start_ts is None:
            # Async enrichment can complete after a short down event already
            # closed. Recover the most recent row as an update target.
            try:
                conn = get_pool().getconn()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT start_ts
                        FROM em_down_event
                        WHERE em_id = %s
                        ORDER BY start_ts DESC
                        LIMIT 1
                        """,
                        (self.em_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return
                    start_ts = row[0]
                finally:
                    get_pool().putconn(conn)
            except Exception:
                log.exception("resolve_down_event_for_reason_update failed em=%d", self.em_id)
                return
        else:
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
        start_ts = self._down_start_ts

        # On restart, memory may not know about an already-open DB row yet.
        if start_ts is None:
            try:
                conn = get_pool().getconn()
                try:
                    existing = q.get_open_down_event(conn, self.em_id)
                    if existing is None:
                        return
                    start_ts = existing["start_ts"]
                    self._down_reason_type = existing["reason_type"]
                    self._down_reason_desc = existing["reason_desc"]
                    self._down_seq_idx = existing["seq_index"]
                    self._down_step = existing["step_name"]
                    self._down_fault_msg = existing["fault_msg"]
                finally:
                    get_pool().putconn(conn)
            except Exception:
                log.exception("get_open_down_event failed em=%d", self.em_id)
                return

        log.debug("[%s/%s] down event CLOSE type=%s desc=%s dur=%.0fs",
                  self.station, self.em_label,
                  self._down_reason_type, self._down_reason_desc,
                  (ts - start_ts).total_seconds())

        # Ensure placeholder text does not persist in history if async
        # enrichment could not resolve before close.
        if self._down_reason_desc == "Manual / interlock (reading conditions...)":
            if self._em_fault:
                self._down_reason_desc = "EM fault active (interlock details unavailable)"
                self._down_reason_type = "interlock"
            else:
                self._down_reason_desc = "Manual mode"
                self._down_reason_type = "manual"
            try:
                conn = get_pool().getconn()
                try:
                    q.update_down_event_reason(
                        conn, self.em_id, start_ts,
                        self._down_reason_desc,
                        self._down_reason_type,
                    )
                    conn.commit()
                finally:
                    get_pool().putconn(conn)
            except Exception:
                log.exception("finalize_down_event_reason failed em=%d", self.em_id)

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

        active_is_prod = (
            self._active_seq is not None
            and self._seq_is_production.get(self._active_seq, False)
        )

        interlock_reason = self._recent_interlock_reason(ts)

        if self._em_fault:
            state = "unscheduled_down"
        elif self._automatic and self._active_seq is not None and not active_is_prod:
            # Sequence is active but not marked production (e.g. Home).
            # Treat as unplanned downtime per availability policy.
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
            # If interlock conditions are currently failing, attribute stop
            # to interlock directly. If interlocks are healthy, assume manual
            # operator stop from HMI.
            if interlock_reason:
                self._try_open_down_event(
                    ts,
                    reason_type="interlock",
                    reason_desc=interlock_reason,
                )
            else:
                self._try_open_down_event(
                    ts,
                    reason_type="manual",
                    reason_desc="Manual mode",
                )

        elif state == "unscheduled_down":
            # EM-level fault rose without an accompanying step-fault notification.
            # Open with a placeholder; if a step-fault edge follows, the sticky
            # guard prevents overwrite, but on_fault_change will still update
            # the seq_idx context.  Treat this as interlock-class so the
            # interlock struct read fires.
            if self._automatic and self._active_seq is not None and not active_is_prod:
                self._try_open_down_event(
                    ts,
                    reason_type="interlock",
                    reason_desc=(
                        f"Non-production sequence active ({self._active_seq})"
                    ),
                    seq_idx=self._active_seq,
                    step_name=self._step.get(self._active_seq),
                )
            elif interlock_reason:
                self._try_open_down_event(
                    ts,
                    reason_type="interlock",
                    reason_desc=interlock_reason,
                )
            else:
                self._try_open_down_event(
                    ts,
                    reason_type="interlock",
                    reason_desc="EM fault (reading conditions...)",
                )

    def _flow_kind_for_step(self, seq_idx: int | None, step_name: str | None) -> str | None:
        if seq_idx is None or not step_name:
            return None
        step = str(step_name).strip()
        if not step:
            return None
        blocked = self._seq_blocked_steps.get(seq_idx, set())
        starved = self._seq_starved_steps.get(seq_idx, set())
        if step in blocked:
            return "blocked"
        if step in starved:
            return "starved"
        return None

    def _try_open_flow_event(
        self, ts: datetime.datetime, kind: str,
        seq_idx: int, step_name: str,
    ) -> None:
        if self._flow_start_ts is not None:
            return
        reason_desc = f"{kind.title()} (reading permissives...)"
        should_enrich = False
        try:
            conn = get_pool().getconn()
            try:
                existing = q.get_open_flow_event(conn, self.em_id)
                if existing:
                    self._flow_start_ts = existing["start_ts"]
                    self._flow_kind = existing["kind"]
                    self._flow_reason_desc = existing["reason_desc"]
                    self._flow_seq_idx = existing["seq_index"]
                    self._flow_step_name = existing["step_name"]
                    should_enrich = bool(
                        self._flow_reason_desc
                        and "reading permissives" in self._flow_reason_desc.lower()
                    )
                else:
                    self._flow_start_ts = ts
                    self._flow_kind = kind
                    self._flow_reason_desc = reason_desc
                    self._flow_seq_idx = seq_idx
                    self._flow_step_name = step_name
                    q.open_flow_event(
                        conn, self.em_id, ts, kind, reason_desc, seq_idx, step_name,
                    )
                    conn.commit()
                    should_enrich = True
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("open_flow_event failed em=%d", self.em_id)
            return
        if should_enrich and self.on_flow_event_opened is not None:
            try:
                self.on_flow_event_opened(
                    self.em_id,
                    self._flow_kind or kind,
                    self._flow_seq_idx if self._flow_seq_idx is not None else seq_idx,
                    self._flow_start_ts,
                )
            except Exception:
                log.exception("on_flow_event_opened callback failed em=%d", self.em_id)

    def update_flow_event_reason(
        self, reason_desc: str,
        start_ts_override: datetime.datetime | None = None,
    ) -> None:
        start_ts = start_ts_override or self._flow_start_ts
        if start_ts is None:
            return
        self._flow_reason_desc = reason_desc
        try:
            conn = get_pool().getconn()
            try:
                q.update_flow_event_reason(conn, self.em_id, start_ts, reason_desc)
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("update_flow_event_reason failed em=%d", self.em_id)

    def _try_close_flow_event(self, ts: datetime.datetime) -> None:
        start_ts = self._flow_start_ts
        if start_ts is None:
            try:
                conn = get_pool().getconn()
                try:
                    existing = q.get_open_flow_event(conn, self.em_id)
                    if existing:
                        start_ts = existing["start_ts"]
                    else:
                        return
                finally:
                    get_pool().putconn(conn)
            except Exception:
                return
        self._flow_start_ts = None
        self._flow_kind = None
        self._flow_reason_desc = None
        self._flow_seq_idx = None
        self._flow_step_name = None
        try:
            conn = get_pool().getconn()
            try:
                q.close_flow_event(conn, self.em_id, start_ts, ts)
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("close_flow_event failed em=%d", self.em_id)

    def _check_flow_event(self, ts: datetime.datetime) -> None:
        if self._automatic is None or self._em_fault is None:
            return
        active_seq = self._active_seq
        active_step = self._step.get(active_seq) if active_seq is not None else None
        active_is_prod = (
            active_seq is not None
            and self._seq_is_production.get(active_seq, False)
        )
        # Blocked/starved are production, available substates.
        if (not self._automatic) or self._em_fault or (not active_is_prod):
            self._try_close_flow_event(ts)
            return
        kind = self._flow_kind_for_step(active_seq, active_step)
        if kind is None:
            self._try_close_flow_event(ts)
            return
        if self._flow_start_ts is None:
            self._try_open_flow_event(ts, kind, int(active_seq), str(active_step))
            return
        if kind != self._flow_kind:
            self._try_close_flow_event(ts)
            self._try_open_flow_event(ts, kind, int(active_seq), str(active_step))

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
        self._emit_availability_raw(ts)
        self._check_down_event(ts)
        self._check_flow_event(ts)

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

        departing_desc = self._normalize_step_desc(
            prev_step, self._step_desc.get(seq_idx),
        )
        desc_ts = self._step_desc_ts.get(seq_idx)
        prev_desc = self._step_desc_prev.get(seq_idx)
        if (
            prev_step not in (None, "STEP_STOP")
            and prev_desc is not None
            and desc_ts is not None
            and (0 <= (ts - desc_ts).total_seconds() * 1000 <= _DESC_EDGE_GUARD_MS)
        ):
            # Description changed right at the edge; treat that update as
            # belonging to the ARRIVING step and keep previous description for
            # the DEPARTING step event row.
            departing_desc = self._normalize_step_desc(prev_step, prev_desc)

        try:
            conn = get_pool().getconn()
            try:
                # Write step_event for the DEPARTING step
                if prev_step is not None:
                    q.insert_step_event(
                        conn, self.em_id, seq_idx,
                        ts=ts,
                        step_name=prev_step,
                        step_desc=departing_desc,
                        duration_ms=duration_ms,
                        was_faulted=bool(self._faulted.get(seq_idx, False)),
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
                        self._normalize_step_desc(
                            step_name, self._step_desc.get(seq_idx),
                        ),
                        ts,
                    )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("on_step_change failed em=%d seq=%d",
                          self.em_id, seq_idx)

        if step_name == "STEP_STOP":
            self._step_desc[seq_idx] = None
        self._step[seq_idx]       = step_name
        self._step_start[seq_idx] = ts

        if seq_idx == self._active_seq:
            self._check_flow_event(ts)

    def on_step_desc_change(self, seq_idx: int, desc: str | None,
                            ts: datetime.datetime) -> None:
        """Update step description and patch em_current_step if active sequence."""
        step = self._step.get(seq_idx)
        new_desc = self._normalize_step_desc(step, desc)
        if new_desc == self._step_desc.get(seq_idx):
            return
        self._step_desc_prev[seq_idx] = self._step_desc.get(seq_idx)
        self._step_desc_prev_ts[seq_idx] = self._step_desc_ts.get(seq_idx)
        self._step_desc[seq_idx] = new_desc
        self._step_desc_ts[seq_idx] = ts
        if not step:
            return
        is_active = (self._active_seq is None or self._active_seq == seq_idx)
        if not is_active:
            return
        try:
            conn = get_pool().getconn()
            try:
                q.upsert_current_step(
                    conn, self.em_id, seq_idx, step,
                    new_desc, ts,
                )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("upsert_current_step (desc) failed em=%d seq=%d",
                          self.em_id, seq_idx)

    def on_fault_change(self, seq_idx: int, faulted: bool,
                        ts: datetime.datetime) -> None:
        if self._faulted.get(seq_idx) is not None and faulted == self._faulted.get(seq_idx):
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
            # If the EM-level fault edge opened a generic interlock/manual
            # event first, promote it to step_fault now that we know the
            # sequence context.
            if self._down_start_ts is not None and self._down_reason_type in ("interlock", "manual"):
                self._down_reason_type = "step_fault"
                self._down_reason_desc = placeholder
                self._down_seq_idx = seq_idx
                self._down_step = step
                self._down_fault_msg = ext_msg
                try:
                    conn = get_pool().getconn()
                    try:
                        q.update_down_event_reason(
                            conn, self.em_id, self._down_start_ts,
                            placeholder, reason_type="step_fault",
                        )
                        q.update_down_event_context(
                            conn, self.em_id, self._down_start_ts,
                            seq_idx, step, ext_msg,
                        )
                        conn.commit()
                    finally:
                        get_pool().putconn(conn)
                except Exception:
                    log.exception("promote_down_event_to_step_fault failed em=%d seq=%d",
                                  self.em_id, seq_idx)
                # Trigger step-fault reason enrichment immediately.  The down
                # event may have originally opened as interlock/manual before
                # the sequence fault edge arrived.
                if self.on_down_event_opened is not None:
                    try:
                        self.on_down_event_opened(
                            self.em_id, "step_fault", seq_idx, self._down_start_ts,
                        )
                    except Exception:
                        log.exception(
                            "on_down_event_opened (promote step_fault) failed em=%d seq=%d",
                            self.em_id, seq_idx,
                        )

        try:
            conn = get_pool().getconn()
            try:
                if faulted:
                    # Rising edge — if restart happened mid-fault and a row is
                    # already open, adopt it instead of inserting a duplicate.
                    existing = q.get_open_fault(conn, self.em_id, seq_idx)
                    if existing is not None:
                        self._fault_id[seq_idx] = existing["id"]
                        self._fault_start[seq_idx] = existing["fault_start"]
                        self._fault_step[seq_idx] = existing.get("step_name")
                        self._fault_step_desc[seq_idx] = existing.get("step_desc")
                    else:
                        self._fault_start[seq_idx]     = ts
                        self._fault_step[seq_idx]      = self._step.get(seq_idx)
                        self._fault_step_desc[seq_idx] = self._step_desc.get(seq_idx)
                        fault_id = q.insert_fault_start(
                            conn, self.em_id, seq_idx,
                            fault_start=ts,
                            step_name=self._fault_step[seq_idx],
                        step_desc=self._normalize_step_desc(
                            self._fault_step[seq_idx],
                            self._fault_step_desc[seq_idx],
                        ),
                            ext_msg=self._ext_msg.get(seq_idx),
                        )
                        self._fault_id[seq_idx] = fault_id
                else:
                    # Falling edge — close tracked fault, or any open DB row
                    # left over from before a collector restart.
                    fid = self._fault_id.get(seq_idx)
                    if fid is None:
                        existing = q.get_open_fault(conn, self.em_id, seq_idx)
                        if existing is not None:
                            fid = existing["id"]
                            self._fault_start[seq_idx] = existing["fault_start"]
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
        if self._last_avail_ts is not None and ts <= self._last_avail_ts:
            ts = self._last_avail_ts + datetime.timedelta(microseconds=1)
        self._last_avail_ts = ts
        try:
            conn = get_pool().getconn()
            try:
                q.insert_availability_raw(
                    conn, self.em_id, ts,
                    self._automatic, self._em_fault, self._running,
                    self._active_seq,
                    (
                        None if self._active_seq is None
                        else self._seq_is_production.get(self._active_seq, False)
                    ),
                )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("insert_availability_raw failed em=%d", self.em_id)

    def _derive_runtime_state(self) -> str | None:
        if self._automatic is None or self._em_fault is None or self._running is None:
            return None
        paused = bool(self._paused) if self._paused is not None else False
        stopped = bool(self._stopped) if self._stopped is not None else False
        unknown_status = bool(self._unknown_status) if self._unknown_status is not None else False
        if self._em_fault:
            return "faulted"
        if unknown_status:
            return "unknown"
        if stopped:
            return "stopped"
        if paused:
            return "paused"
        if self._running:
            return "running"
        # No explicit runtime status is asserted by the EM status bits.
        # Keep this separate from "manual stop" attribution logic so we can
        # observe and audit unclassified state windows.
        return "unknown"

    def _emit_runtime_transition(self, ts: datetime.datetime) -> None:
        state = self._derive_runtime_state()
        if state is None:
            return
        if state == self._runtime_state:
            return
        from_state = self._runtime_state
        self._runtime_state = state

        seq = self._active_seq
        step_name = self._step.get(seq) if seq is not None else None
        step_desc = self._step_desc.get(seq) if seq is not None else None
        try:
            conn = get_pool().getconn()
            try:
                q.insert_runtime_transition(
                    conn, self.em_id, ts,
                    from_state, state,
                    self._automatic, self._running,
                    self._paused, self._stopped,
                    self._unknown_status,
                    self._em_fault,
                    seq,
                    (
                        None if seq is None
                        else self._seq_is_production.get(seq, False)
                    ),
                    step_name, step_desc,
                )
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.exception("insert_runtime_transition failed em=%d", self.em_id)
        if state == "unknown":
            log.warning(
                "runtime_state unknown em=%d auto=%s run=%s paused=%s stopped=%s unknown=%s fault=%s active_seq=%s",
                self.em_id, self._automatic, self._running, self._paused,
                self._stopped, self._unknown_status, self._em_fault, self._active_seq,
            )

    def on_automatic_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._automatic:
            return
        self._automatic = val
        self._emit_availability_raw(ts)
        self._emit_runtime_transition(ts)
        self._check_down_event(ts)
        self._check_flow_event(ts)

    def on_em_fault_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._em_fault:
            return
        self._em_fault = val
        self._emit_availability_raw(ts)
        self._emit_runtime_transition(ts)
        self._check_down_event(ts)
        self._check_flow_event(ts)

    def on_running_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._running:
            return
        self._running = val
        self._emit_availability_raw(ts)
        self._emit_runtime_transition(ts)
        self._check_down_event(ts)
        self._check_flow_event(ts)

    def on_paused_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._paused:
            return
        self._paused = val
        self._emit_runtime_transition(ts)
        self._check_down_event(ts)
        self._check_flow_event(ts)

    def on_stopped_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._stopped:
            return
        self._stopped = val
        self._emit_runtime_transition(ts)
        self._check_down_event(ts)
        self._check_flow_event(ts)

    def on_unknown_status_change(self, val: bool, ts: datetime.datetime) -> None:
        if val == self._unknown_status:
            return
        self._unknown_status = val
        self._emit_runtime_transition(ts)
        self._check_down_event(ts)
        self._check_flow_event(ts)

    def on_interlock_snapshot(self, reason: str | None, ts: datetime.datetime) -> None:
        """Track latest interlock condition health from live PLC struct data."""
        self._interlock_reason = reason
        self._interlock_has_fail = bool(reason)
        self._interlock_ts = ts

    def _recent_interlock_reason(self, ts: datetime.datetime) -> str | None:
        if not self._interlock_has_fail or not self._interlock_reason:
            return None
        if self._interlock_ts is None:
            return self._interlock_reason
        age = (ts - self._interlock_ts).total_seconds()
        if age < 0:
            return self._interlock_reason
        if age <= _INTERLOCK_REASON_MAX_AGE_S:
            return self._interlock_reason
        return None
