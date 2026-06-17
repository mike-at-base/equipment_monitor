"""
OPC UA client for one PLC.  Uses asyncua subscriptions (push-based, not polling).

Subscription strategy — subscribe to TRIGGERS, read STRUCTS on demand
─────────────────────────────────────────────────────────────────────
The collector subscribes only to the small set of nodes that drive state
changes (~4 EM-level + 4 per sequence ≈ 16 nodes per EM).  When a fault rises
or the EM goes manual, the down event opens with a placeholder reason and an
async on-demand read of the relevant PLC struct fills in the actual condition
descriptions:

  • Step fault rising edge → read ``stepControl[N-1].activeStepBranch``
    (whole array, one round-trip).  asyncua deserialises the struct and we
    walk every enabled branch's ``permissive.condition[]`` to find the ones
    with ``ok = False`` — those descriptions are the root cause.

  • EM goes manual / EM-level fault → read ``interlock`` (whole struct, one
    round-trip).  Same idea, applied to EM-level interlock conditions.

This adapts automatically to whatever ``SEQUENCE_MAX_STEP_BRANCHES`` /
permissive array size the PLC library exposes — no hard-coded constants to
keep in sync, no thousands of "probe" subscriptions during startup.

Note on data types
──────────────────
``client.load_data_type_definitions()`` fetches the PLC's custom UDTs and
generates Python classes so struct reads deserialise into objects with the
right attributes.  Without it, ``read_value()`` on a struct node returns raw
ExtensionObject bytes that we can't parse.  If type loading fails the
collector still works for state tracking — only reason enrichment is lost,
and placeholders remain.

⚠  S7-1500 MinimumSamplingInterval defaults to 1000 ms in TIA Portal.
   Ask the PLC engineer to lower it to 10–100 ms to capture fast steps.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

from asyncua import Client, ua

import db.queries as q
from db.connection import get_pool
from collector.state_tracker import EMStateTracker

log = logging.getLogger(__name__)

PUBLISH_INTERVAL_MS = 500   # S7-1500 revises 100 ms up to 500 ms anyway
RECONNECT_DELAY_S   = 10

# S7-1500 grants a 30 s session timeout regardless of what is requested.
# Setting this explicitly prevents asyncua calibrating its renewal timer
# against the (ignored) requested value of 3600 s.
SESSION_TIMEOUT_MS  = 30_000

# Per-request timeout.  Default asyncua value (4 s) is too tight when the
# PLC is busy; 15 s gives the renewal handshake room to complete.
REQUEST_TIMEOUT_S   = 15
OPC_CLIENT_NAME     = "equipment-monitor-app"
OPC_APPLICATION_URI = "urn:equipment-monitor:app"

# Max number of failed-condition descriptions to concatenate into one reason
# string.  Anything past this gets truncated with "(+N more)".
_MAX_REASON_CONDITIONS = 5


def siemens_path(em_db_path: str) -> str:
    """
    Siemens S7-1500 OPC UA requires the DB name in double quotes:
        'ST10000_Station_DB.mainEquipmentModule'
        → '"ST10000_Station_DB".mainEquipmentModule'
    If the path is already quoted, return it unchanged.
    """
    if em_db_path.startswith('"'):
        return em_db_path
    dot = em_db_path.find('.')
    if dot == -1:
        return f'"{em_db_path}"'
    return f'"{em_db_path[:dot]}"{em_db_path[dot:]}'


# ── Struct parsing helpers ───────────────────────────────────────────────────
#
# asyncua's UDT loader generates Python classes whose attribute names usually
# match the PLC field names exactly.  Casing can differ across UDT generators
# (some uppercase the first letter), so _safe_attr tries a few variants to
# stay tolerant.


def _safe_attr(obj: Any, *names: str) -> Any:
    """Return first matching value from attrs or dict keys."""
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
            # Be tolerant to key-case differences from UDT decoding.
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() == n.lower():
                    return v
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


def _as_bool(value: Any, default: bool = True) -> bool:
    """
    Parse PLC/UADT values to bool safely.
    Avoid bool("False") == True pitfalls for string payloads.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("false", "0", "no", "off"):
            return False
        if v in ("true", "1", "yes", "on"):
            return True
        return default
    return bool(value)


def _parse_conditions(source: Any) -> list[dict]:
    """
    Extract condition rows from PLC UDT objects.
    Accepts permissive structs directly and also larger wrappers (e.g. interlock
    structs) that nest condition arrays under permissive/branch fields.
    """
    if source is None:
        return []

    def _parse_condition_array(cond_arr: Any) -> list[dict]:
        if not isinstance(cond_arr, (list, tuple)):
            return []
        out: list[dict] = []
        for c in cond_arr:
            if c is None:
                continue
            ok = _safe_attr(c, 'ok', 'Ok', 'OK')
            desc = _safe_attr(c, 'description', 'Description')
            out.append({
                'ok': _as_bool(ok, default=True),
                'description': (str(desc).strip() if desc else ''),
            })
        return out

    visited: set[int] = set()

    def _walk(obj: Any, depth: int = 0) -> list[dict]:
        if obj is None or depth > 4:
            return []
        oid = id(obj)
        if oid in visited:
            return []
        visited.add(oid)

        if isinstance(obj, (list, tuple)):
            out: list[dict] = []
            for item in obj:
                out.extend(_walk(item, depth + 1))
            return out

        direct = _safe_attr(
            obj, 'condition', 'Condition', 'conditions', 'Conditions',
        )
        parsed = _parse_condition_array(direct)
        if parsed:
            return parsed

        out: list[dict] = []
        for child_name in ('permissive', 'Permissive', 'interlock', 'Interlock',
                           'branch', 'Branch', 'branches', 'Branches',
                           'activeStepBranch', 'ActiveStepBranch'):
            child = _safe_attr(obj, child_name)
            if child is not None:
                out.extend(_walk(child, depth + 1))

        # Generic dict fallback: recurse through nested values so that
        # unfamiliar wrapper field names still get scanned.
        if isinstance(obj, dict):
            for v in obj.values():
                out.extend(_walk(v, depth + 1))
        return out

    return _walk(source)


def _parse_step_branches(branches_value: Any) -> list[dict]:
    """
    Walk an ``activeStepBranch`` array (list of ``typeStepBranch``) and return
    ``[{'enabled': bool, 'conditions': [...]}]``.
    """
    if branches_value is None or not isinstance(branches_value, (list, tuple)):
        return []
    out: list[dict] = []
    for b in branches_value:
        if b is None:
            continue
        enabled = _safe_attr(b, 'enabled', 'Enabled')
        permissive = _safe_attr(b, 'permissive', 'Permissive')
        out.append({
            'enabled': bool(enabled) if enabled is not None else True,
            'conditions': _parse_conditions(permissive),
        })
    return out


def _dedupe_in_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for s in items:
        if s and s not in seen:
            seen.add(s)
            result.append(s)
    return result


def _join_reason(descs: list[str], prefix: str | None = None) -> str | None:
    """Concatenate failed-condition descriptions into one reason string."""
    descs = _dedupe_in_order(descs)
    if not descs:
        return None
    reason = "; ".join(descs[:_MAX_REASON_CONDITIONS])
    if len(descs) > _MAX_REASON_CONDITIONS:
        reason += f" (+{len(descs) - _MAX_REASON_CONDITIONS} more)"
    if prefix:
        reason = f"{prefix} — {reason}"
    return reason


def _build_step_fault_reason(branches: list[dict],
                              ext_msg: str | None = None) -> str | None:
    """
    Collect failed-condition descriptions across every enabled branch.
    Returns None if no failed conditions are visible (caller keeps placeholder).
    """
    descs: list[str] = []
    for b in branches:
        if not b.get('enabled', True):
            continue
        for c in b.get('conditions', []):
            if not c['ok'] and c['description']:
                descs.append(c['description'])
    return _join_reason(descs, prefix=ext_msg)


def _build_interlock_reason(conditions: list[dict]) -> str | None:
    descs = [c['description'] for c in conditions
             if not c['ok'] and c['description']]
    return _join_reason(descs)


# ── Subscription handler ─────────────────────────────────────────────────────

class _SubHandler:
    """
    Duck-typed asyncua subscription handler (asyncua 1.1.x removed SubHandler
    base).  Routes value-change notifications to the right EMStateTracker
    callback.

    Roles in node_map:
      • 'step' | 'step_desc' | 'faulted' | 'ext_msg'         payload = seq_idx
      • 'active_seq' | 'automatic' | 'em_fault' | 'running'  payload = None

    Condition state is read on demand by OpcClient when down events open, so
    condition tags are deliberately NOT subscribed here.
    """

    def __init__(self, node_map: dict[int, tuple]) -> None:
        self._map = node_map

    def datachange_notification(self, node, val, data) -> None:
        try:
            nid = node.nodeid.Identifier
            entry = self._map.get(nid)
            if entry is None:
                return
            tracker, role, payload = entry
            ts = data.monitored_item.Value.SourceTimestamp or datetime.datetime.now(datetime.timezone.utc)

            if role == "step":
                tracker.on_step_change(payload, str(val) if val else "", ts)
            elif role == "step_desc":
                tracker.on_step_desc_change(payload, str(val).strip() if val else None, ts)
            elif role == "faulted":
                tracker.on_fault_change(payload, bool(val), ts)
            elif role == "ext_msg":
                tracker._ext_msg[payload] = str(val).strip() if val else None
            elif role == "active_seq":
                # S7-1500 returns 0 when no sequence is running; treat as None
                seq_val = int(val) if val else None
                tracker.on_active_seq_change(seq_val, ts)
            elif role == "automatic":
                tracker.on_automatic_change(bool(val), ts)
            elif role == "em_fault":
                tracker.on_em_fault_change(bool(val), ts)
            elif role == "running":
                tracker.on_running_change(bool(val), ts)
        except Exception:
            log.exception("datachange_notification error")


# ── OPC client ───────────────────────────────────────────────────────────────

class OpcClient:
    """Manages one asyncua connection + subscriptions for a single PLC."""

    def __init__(self, plc_name: str, endpoint: str,
                 trackers: dict[int, EMStateTracker]) -> None:
        self.plc_name = plc_name
        self.endpoint = endpoint
        # em_id → tracker
        self._trackers = trackers
        self._running = True
        # Loading Siemens UDT definitions is expensive and appears to retain
        # memory across reconnects in asyncua internals. Keep it one-time per
        # OpcClient instance instead of on every reconnect attempt.
        self._types_loaded = False

        # Struct-node refs cached for on-demand reads (populated in
        # _resolve_em_nodes).  Shape, keyed by em_id:
        #   {'interlock':     <Node>,
        #    'step_branches': {seq_idx: <Node>, ...}}
        self._struct_nodes: dict[int, dict] = {}

    async def run(self) -> None:
        while self._running:
            try:
                await self._connect_and_subscribe()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("[%s] Connection error — retrying in %ds",
                              self.plc_name, RECONNECT_DELAY_S)
                await self._heartbeat(connected=False)
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def stop(self) -> None:
        self._running = False

    # ── Internal ──────────────────────────────────────────────────────────

    async def _connect_and_subscribe(self) -> None:
        log.info("[%s] Connecting to %s", self.plc_name, self.endpoint)
        client = Client(url=self.endpoint)
        # OPC UA application/session identity shown on the PLC server.
        client.name            = OPC_CLIENT_NAME
        client.description     = OPC_CLIENT_NAME
        client.application_uri = OPC_APPLICATION_URI
        client.session_timeout = SESSION_TIMEOUT_MS  # match S7-1500 grant
        client.timeout         = REQUEST_TIMEOUT_S   # per-request timeout
        async with client:
            # Load the PLC's custom UDT definitions so subsequent struct reads
            # (activeStepBranch, interlock) deserialise into objects with the
            # right attributes.  Without this, struct reads return raw bytes
            # and reason enrichment cannot work.  Failure is non-fatal —
            # state tracking still functions, only the reason text stays at
            # its placeholder.
            if not self._types_loaded:
                try:
                    await client.load_data_type_definitions()
                    self._types_loaded = True
                    log.info("[%s] Loaded custom data type definitions",
                             self.plc_name)
                except Exception:
                    log.warning("[%s] load_data_type_definitions failed — fault "
                                "reason enrichment will be limited",
                                self.plc_name, exc_info=True)

            self._struct_nodes.clear()
            node_map: dict[int, tuple[EMStateTracker, str, Any]] = {}
            nodes: list[Any] = []

            # avail_nodes: em_id → (tracker, auto_node, fault_node, running_node)
            # kept for periodic re-reads in the keep-alive loop below
            avail_nodes: dict[int, tuple] = {}

            # Wire the down-event-opened callback so reason enrichment runs
            # automatically whenever a tracker opens an event.
            for tracker in self._trackers.values():
                tracker.on_down_event_opened = self._schedule_reason_enrichment

            for em_id, tracker in self._trackers.items():
                em_nodes = await self._resolve_em_nodes(
                    client, tracker, node_map, avail_nodes
                )
                nodes.extend(em_nodes)

            if not nodes:
                log.warning("[%s] No nodes resolved — check em_db_path in config.yaml",
                            self.plc_name)
                # Trigger outer reconnect handling (heartbeat false + backoff)
                # instead of spinning a tight no-node loop.
                raise RuntimeError("no nodes resolved")

            handler = _SubHandler(node_map)
            sub = await client.create_subscription(PUBLISH_INTERVAL_MS, handler)
            await sub.subscribe_data_change(
                nodes,
                queuesize=10,
                # Request 100 ms sampling; server will honour its MinimumSamplingInterval
                attr=ua.AttributeIds.Value,
            )

            log.info("[%s] Subscribed to %d nodes", self.plc_name, len(nodes))
            await self._heartbeat(connected=True, node_count=len(nodes))

            # Allow the initial subscription burst to settle, then correct
            # em_current_step for each EM.  The burst fires all subscribed
            # nodes; notification order is server-determined, so the last
            # stepControl[N] to arrive may not be from the active sequence.
            # flush_current_step() re-writes the right step after the dust
            # settles, using _active_seq (if known) or the first non-STOP seq.
            await asyncio.sleep(2.0)
            flush_ts = datetime.datetime.now(datetime.timezone.utc)
            for tracker in self._trackers.values():
                tracker.flush_current_step(flush_ts)

            # Keep connection alive.  Read ServerState (i=2259) every 10 s as
            # an explicit keep-alive — belt-and-suspenders alongside the
            # subscription's publish mechanism.  Session timeout is 30 s so
            # this keeps well within the window.
            #
            # Every 30 s also re-read the three availability signals directly
            # to catch notifications that the S7-1500 drops when a signal
            # bounces faster than its MinimumSamplingInterval (typically 1 s).
            server_state = client.get_node("i=2259")
            tick = 0
            while self._running:
                await asyncio.sleep(10)
                await server_state.read_value()   # raises on disconnect → reconnect
                await self._heartbeat(connected=True, node_count=len(nodes))
                tick += 1
                if tick % 3 == 0:               # every 30 s
                    await self._resync_availability(avail_nodes)

    async def _resync_availability(
        self,
        avail_nodes: dict[int, tuple],
    ) -> None:
        """
        Re-read automatic / fault / running directly from the PLC and push
        any changed values through the tracker callbacks.  Runs every 30 s to
        recover from S7-1500 dropped notifications (signal bounces within one
        MinimumSamplingInterval are delivered inconsistently).
        """
        ts = datetime.datetime.now(datetime.timezone.utc)
        for em_id, (tracker, auto_node, fault_node, run_node) in avail_nodes.items():
            try:
                auto    = bool(await auto_node.read_value())
                fault   = bool(await fault_node.read_value())
                running = bool(await run_node.read_value())
                tracker.on_automatic_change(auto,    ts)
                tracker.on_em_fault_change(fault,    ts)
                tracker.on_running_change(running,   ts)
            except Exception:
                log.debug("resync_availability failed em=%d", em_id)

    async def _resolve_em_nodes(
        self,
        client: Client,
        tracker: EMStateTracker,
        node_map: dict,
        avail_nodes: dict,
    ) -> list:
        """
        Resolve subscription NodeIds for one EM AND cache the struct-node refs
        we'll read on demand when down events open.

        Subscriptions (~16 per EM) cover only signals that drive state changes:
          • EM-level: automatic, em_fault, running, activeSequence
          • Per-sequence: step, description, faulted, externalFaultMessage

        Struct refs (no subscription, read on demand):
          • interlock                          — for EM-level / manual events
          • stepControl[N-1].activeStepBranch  — for step-fault events
        """
        nodes = []
        ns = 3  # Siemens S7 OPC UA namespace index

        def make_node(path: str):
            return client.get_node(ua.NodeId(path, ns))

        raw_path = tracker.em_db_path if hasattr(tracker, "em_db_path") else ""
        db_path  = siemens_path(raw_path)

        async def try_node(path: str, role: str, payload: Any) -> bool:
            try:
                node = make_node(path)
                await node.read_data_value()  # verify it exists
                nid = node.nodeid.Identifier
                node_map[nid] = (tracker, role, payload)
                nodes.append(node)
                return True
            except Exception:
                log.debug("Node not found: %s", path)
                return False

        base = db_path

        # ── EM-level subscriptions (4 trigger nodes) ──────────────────────────
        await try_node(f'{base}.status.mode.automatic',  'automatic',  None)
        await try_node(f'{base}.status.alarm.fault',     'em_fault',   None)
        await try_node(f'{base}.status.running',         'running',    None)
        await try_node(f'{base}.status.activeSequence',  'active_seq', None)

        # Save node refs for periodic re-reads (avail signal bounce recovery)
        avail_nodes[tracker.em_id] = (
            tracker,
            make_node(f'{base}.status.mode.automatic'),
            make_node(f'{base}.status.alarm.fault'),
            make_node(f'{base}.status.running'),
        )

        # ── Per-sequence step subscriptions (4 trigger nodes per sequence) ────
        #
        # The S7-1500 PLC uses 1-indexed sequence numbers (activeSequence = 1, 2, 3…)
        # but the stepControl ARRAY is 0-indexed in OPC UA.  Sequence N writes its
        # step data to stepControl[N-1].  seq_idx (1-based, from config) is kept
        # as the logical identifier passed through node_map so the handler and
        # is_active guard stay consistent with activeSequence values.
        for seq_idx in tracker.seq_indices:
            sc = f'{base}.stepControl[{seq_idx - 1}]'
            await try_node(f'{sc}.step',                'step',      seq_idx)
            await try_node(f'{sc}.description',         'step_desc', seq_idx)
            await try_node(f'{sc}.faulted',             'faulted',   seq_idx)
            await try_node(f'{sc}.externalFaultMessage','ext_msg',   seq_idx)

        # ── Struct refs for on-demand reads (no subscription) ────────────────
        # Reading the whole struct in one round-trip when a down event opens
        # adapts automatically to whatever branch / condition count the PLC
        # library exposes.  No constants to keep in sync.
        self._struct_nodes[tracker.em_id] = {
            'interlock':     make_node(f'{base}.interlock'),
            'step_branches': {
                seq_idx: make_node(
                    f'{base}.stepControl[{seq_idx - 1}].activeStepBranch'
                )
                for seq_idx in tracker.seq_indices
            },
        }

        return nodes

    # ── On-demand reason enrichment ──────────────────────────────────────────

    def _schedule_reason_enrichment(
        self, em_id: int, reason_type: str, seq_idx: int | None,
    ) -> None:
        """
        Called by EMStateTracker when a down event opens (sync, from the
        subscription handler running inside the asyncua event loop).
        Schedules an async PLC struct read so the placeholder reason text
        gets replaced with descriptions of the conditions actually failing.
        """
        tracker = self._trackers.get(em_id)
        if tracker is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("no running event loop — skipping reason enrichment "
                      "em=%d type=%s", em_id, reason_type)
            return

        if reason_type == "step_fault" and seq_idx is not None:
            loop.create_task(self._enrich_step_fault_reason(tracker, seq_idx))
        elif reason_type == "interlock":
            loop.create_task(self._enrich_interlock_reason(tracker))
        # 'manual' / unknown — no enrichment

    async def _enrich_step_fault_reason(
        self, tracker: EMStateTracker, seq_idx: int,
    ) -> None:
        """
        Read ``stepControl[N-1].activeStepBranch`` (one struct, one round-trip)
        and update the open down event's reason with the failed-condition
        descriptions.  No-op if the event has already closed.
        """
        struct = self._struct_nodes.get(tracker.em_id)
        if not struct:
            return
        branch_node = struct.get('step_branches', {}).get(seq_idx)
        if branch_node is None:
            return

        try:
            raw = await branch_node.read_value()
        except Exception:
            log.debug("read activeStepBranch failed em=%d seq=%d",
                      tracker.em_id, seq_idx, exc_info=True)
            return

        branches = _parse_step_branches(raw)
        reason = _build_step_fault_reason(
            branches,
            ext_msg=tracker._ext_msg.get(seq_idx),
        )
        if reason:
            tracker.update_down_event_reason(reason)

    async def _enrich_interlock_reason(self, tracker: EMStateTracker) -> None:
        """
        Read ``interlock`` (one struct, one round-trip) and update the open
        down event's reason.  If no interlock condition is failing, this was
        an operator-initiated manual stop — rewrite reason to "Manual mode"
        and demote reason_type accordingly.
        """
        struct = self._struct_nodes.get(tracker.em_id)
        if not struct:
            return
        interlock_node = struct.get('interlock')
        if interlock_node is None:
            return

        try:
            raw = await interlock_node.read_value()
        except Exception:
            log.debug("read interlock failed em=%d", tracker.em_id, exc_info=True)
            return

        # If a sequence fault already claimed root-cause ownership, do not let
        # the interlock enricher overwrite it.
        if tracker._down_reason_type == "step_fault":
            return

        conditions = _parse_conditions(raw)
        reason = _build_interlock_reason(conditions)
        if reason:
            tracker.update_down_event_reason(reason)
        else:
            # Only demote to manual if there is no active EM fault.
            # If fault is still active but interlock detail parsing yields
            # nothing, keep it as an interlock-class fault reason.
            if tracker._em_fault:
                tracker.update_down_event_reason(
                    "EM fault active (interlock details unavailable)",
                    reason_type="interlock",
                )
            else:
                tracker.update_down_event_reason("Manual mode", reason_type="manual")

    async def _heartbeat(self, connected: bool, node_count: int = 0) -> None:
        try:
            conn = get_pool().getconn()
            try:
                q.update_heartbeat(conn, self.plc_name, connected, node_count)
                conn.commit()
            finally:
                get_pool().putconn(conn)
        except Exception:
            log.debug("Heartbeat write failed")


def build_clients_from_config(config: dict) -> list[OpcClient]:
    """
    Build one OpcClient per enabled PLC, with all enabled EMs attached.
    Syncs config tables to DB as a side effect.
    """
    clients = []
    for plc_cfg in config.get("plcs", []):
        if not plc_cfg.get("enabled", True):
            continue

        plc_name = plc_cfg["name"]
        endpoint  = plc_cfg["opc_endpoint"]
        plc_id    = q.upsert_plc(plc_name, endpoint, enabled=True)

        trackers: dict[int, EMStateTracker] = {}

        for em_cfg in plc_cfg.get("equipment_modules", []):
            enabled = em_cfg.get("enabled", True)
            em_id = q.upsert_em(
                plc_id,
                station=em_cfg["station"],
                display_name=em_cfg["display_name"],
                em_db_path=em_cfg["em_db_path"],
                em_label=em_cfg["em_label"],
                enabled=enabled,
            )
            for seq in em_cfg.get("sequences", []):
                q.upsert_sequence(
                    em_id,
                    seq_index=seq["index"],
                    seq_name=seq["name"],
                    is_production=seq.get("is_production", False),
                    cycle_start_step=seq.get(
                        "cycle_start_step", "SEQUENCE_INITIAL_STEP"
                    ),
                )

            if not enabled:
                continue

            seq_indices = [s["index"] for s in em_cfg.get("sequences", [])]
            tracker = EMStateTracker(
                em_id=em_id,
                station=em_cfg["station"],
                em_label=em_cfg["em_label"],
                seq_indices=seq_indices,
            )
            # Attach db_path so opc_client can build node paths
            tracker.em_db_path = em_cfg["em_db_path"]
            trackers[em_id] = tracker

        clients.append(OpcClient(plc_name, endpoint, trackers))

    return clients
