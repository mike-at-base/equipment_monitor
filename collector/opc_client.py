"""
OPC UA client for one PLC.  Uses asyncua subscriptions (push-based, not polling).
Publish interval = 100 ms; sampling interval = 100 ms per item.

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


class _SubHandler:
    """Duck-typed asyncua subscription handler (asyncua 1.1.x removed SubHandler base)."""

    def __init__(self, node_map: dict[int, tuple[EMStateTracker, str, int | None]]) -> None:
        # node_map: node_id_int → (tracker, tag_role, seq_index_or_None)
        # tag_role: 'step' | 'step_desc' | 'faulted' | 'ext_msg'  — seq_idx = N (sequence index)
        #           'active_seq' | 'automatic' | 'em_fault' | 'running'  — seq_idx = None
        self._map = node_map

    def datachange_notification(self, node, val, data) -> None:
        try:
            nid = node.nodeid.Identifier
            entry = self._map.get(nid)
            if entry is None:
                return
            tracker, role, seq_idx = entry
            ts = data.monitored_item.Value.SourceTimestamp or datetime.datetime.now(datetime.timezone.utc)

            if role == "step":
                tracker.on_step_change(seq_idx, str(val) if val else "", ts)
            elif role == "step_desc":
                tracker.on_step_desc_change(seq_idx, str(val).strip() if val else None, ts)
            elif role == "faulted":
                tracker.on_fault_change(seq_idx, bool(val), ts)
            elif role == "ext_msg":
                tracker._ext_msg[seq_idx] = str(val).strip() if val else None
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


class OpcClient:
    """Manages one asyncua connection + subscriptions for a single PLC."""

    def __init__(self, plc_name: str, endpoint: str,
                 trackers: dict[int, EMStateTracker]) -> None:
        self.plc_name = plc_name
        self.endpoint = endpoint
        # em_id → tracker
        self._trackers = trackers
        self._running = True

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
        client.session_timeout = SESSION_TIMEOUT_MS  # match S7-1500 grant
        client.timeout         = REQUEST_TIMEOUT_S   # per-request timeout
        async with client:
            node_map: dict[int, tuple[EMStateTracker, str, int | None]] = {}
            nodes: list[Any] = []

            for em_id, tracker in self._trackers.items():
                em_nodes = await self._resolve_em_nodes(
                    client, tracker, node_map
                )
                nodes.extend(em_nodes)

            if not nodes:
                log.warning("[%s] No nodes resolved — check em_db_path in config.yaml",
                            self.plc_name)
                return

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
            server_state = client.get_node("i=2259")
            while self._running:
                await asyncio.sleep(10)
                await server_state.read_value()   # raises on disconnect → reconnect
                await self._heartbeat(connected=True, node_count=len(nodes))

    async def _resolve_em_nodes(
        self,
        client: Client,
        tracker: EMStateTracker,
        node_map: dict,
    ) -> list:
        """Browse the PLC namespace to resolve NodeIds for one EM."""
        nodes = []
        ns = 3  # Siemens S7 OPC UA namespace index

        def make_node(path: str):
            return client.get_node(ua.NodeId(path, ns))

        raw_path = tracker.em_db_path if hasattr(tracker, "em_db_path") else ""
        db_path  = siemens_path(raw_path)

        async def try_node(path: str, role: str, seq_idx: int | None) -> bool:
            try:
                node = make_node(path)
                await node.read_data_value()  # verify it exists
                nid = node.nodeid.Identifier
                node_map[nid] = (tracker, role, seq_idx)
                nodes.append(node)
                return True
            except Exception:
                log.debug("Node not found: %s", path)
                return False

        base = db_path

        # EM-level tags
        await try_node(f'{base}.status.mode.automatic',  'automatic',  None)
        await try_node(f'{base}.status.alarm.fault',     'em_fault',   None)
        await try_node(f'{base}.status.running',         'running',    None)
        await try_node(f'{base}.status.activeSequence',  'active_seq', None)

        # Subscribe to stepControl[N-1] for every sequence N on this EM.
        #
        # The S7-1500 PLC uses 1-indexed sequence numbers (activeSequence = 1, 2, 3…)
        # but the stepControl ARRAY is 0-indexed in OPC UA.  Sequence N writes its
        # step data to stepControl[N-1].  seq_idx (1-based, from config) is kept as
        # the logical identifier passed through node_map so the handler and
        # is_active guard stay consistent with activeSequence values.
        for seq_idx in tracker.seq_indices:
            sc = f'{base}.stepControl[{seq_idx - 1}]'
            await try_node(f'{sc}.step',                'step',      seq_idx)
            await try_node(f'{sc}.description',         'step_desc', seq_idx)
            await try_node(f'{sc}.faulted',             'faulted',   seq_idx)
            await try_node(f'{sc}.externalFaultMessage','ext_msg',   seq_idx)

        return nodes

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
