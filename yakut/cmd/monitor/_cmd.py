# Copyright (c) 2021 UAVCAN Consortium
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel@uavcan.org>

from __future__ import annotations
import asyncio
from typing import Optional, Dict, TypeVar, Generic, cast, Any
import click
import pyuavcan
from pyuavcan.transport import MessageDataSpecifier, ServiceDataSpecifier, Timestamp, AlienTransfer
import yakut
from ._model import N_NODES, N_SUBJECTS, N_SERVICES, Avatar, NodeState
from ._view import View
from ._iface import Iface
from ._ui import refresh_screen


@yakut.subcommand()
@yakut.pass_purser
@yakut.asynchronous
async def monitor(purser: yakut.Purser) -> None:
    """
    Display information about online nodes and network traffic in real time (like htop).
    The command may attempt to detect and report some basic network configuration/implementation issues.

    If the tool is launched with an anonymous node-ID, its functionality will be limited
    as it will be unable to send introspection requests to other nodes (like uavcan.node.GetInfo),
    but it will attempt to snoop on relevant data sent to other nodes and extract as much information as it can
    through passive monitoring.
    If there is another node like this one running on the network that is non-anonymous,
    it will act as a source of target illumination, allowing other monitors to benefit from its probing requests.

    The tool will set up low-level packet capture using the advanced network introspection API of PyUAVCAN.
    This may fail for some transports (UDP in particular) unless the user has the permissions required for packet
    capture and the system is configured appropriately (capabilities enabled, capture drivers installed, etc).
    Ethernet-based transports shall be connected to the SPAN port of the local switch (see port mirroring).
    Refer to the PyUAVCAN documentation to find out how to enable packet capture: https://pyuavcan.readthedocs.io.
    """
    import numpy as np
    from scipy.sparse import dok_matrix, spmatrix

    try:
        import uavcan.node
        import uavcan.node.port
    except ImportError as ex:
        from yakut.cmd.compile import make_usage_suggestion

        raise click.UsageError(make_usage_suggestion(ex.name))

    period = 2.0

    fir_window_duration = 10.0
    fir_samples = max(2, round(fir_window_duration / period))
    xfer_rates_filter: MovingAverage[spmatrix] = MovingAverage(fir_samples)
    byte_rates_filter: MovingAverage[spmatrix] = MovingAverage(fir_samples)

    total_transport_error_count = 0

    models: Dict[Optional[int], Avatar] = {}

    ts_last_trace: Optional[Timestamp] = None

    # We have to keep all counters in a sparse matrix for performance reasons,
    # otherwise we observe bad performance issues even with small networks.
    # The anonymous node stat is located at the max index.
    xfer_counts = dok_matrix((N_NODES + 1, N_SUBJECTS + N_SERVICES * 2), dtype=np.uint64)
    xfer_counts_prev = xfer_counts.copy()
    byte_counts = dok_matrix((N_NODES + 1, N_SUBJECTS + N_SERVICES * 2), dtype=np.uint64)
    byte_counts_prev = byte_counts.copy()

    def on_trace(ts: Timestamp, tr: AlienTransfer) -> None:
        nonlocal ts_last_trace
        ts_last_trace = ts
        # Create node instances lazily.
        node_id = tr.metadata.session_specifier.source_node_id
        if node_id not in models:
            _logger.info("New node %r", node_id)
            models[node_id] = Avatar(iface, node_id)
        # Update statistical counters.
        x = node_id
        if x is None:
            x = N_NODES
        y = linearize_data_specifier(tr.metadata.session_specifier.data_specifier)
        xfer_counts[x, y] += 1
        byte_counts[x, y] += sum(map(len, tr.fragmented_payload))

    def on_transport_error(tr: pyuavcan.transport.ErrorTrace) -> None:
        nonlocal total_transport_error_count
        total_transport_error_count += 1
        _ = tr

    with purser.get_node("monitor", allow_anonymous=True) as node:
        iface = Iface(node)
        iface.add_trace_handler(on_trace)
        iface.add_transport_error_handler(on_transport_error)

        # Special case: set up an entry for the local node manually because we won't be able to request own node.
        if node.id is not None:
            models[node.id] = Avatar(iface, node.id, info=node.info)

        _logger.debug("STARTING THE MAIN LOOP")
        view = View()
        next_redraw_at = node.loop.time()
        ts_prev = next_redraw_at
        while True:
            next_redraw_at += period
            await asyncio.sleep(next_redraw_at - node.loop.time())
            ts_started = node.loop.time()

            # If there were any network events since last update, use the latest time received from the network.
            # Only if there were no updates use the real time, which may happen IFF the local node is anonymous
            # and the network is completely silent (so not a typical scenario).
            # The point of this manipulation is to use the correct time when the tool has accumulated a long input
            # frame queue due to being unable to keep up with data processing in real time (hey, we're using Python).
            # In this case, using the real wall time would be harmful as it would lead the tool to believe that
            # the data is obsolete whereas it's the tool's view of time that is obsolete rather than the data itself.
            ts = float(ts_last_trace.monotonic) if ts_last_trace is not None else ts_started
            ts_last_trace = None
            dt_trace = ts - ts_prev
            ts_prev = ts

            # Update statistics
            xfer_deltas = xfer_counts - xfer_counts_prev
            xfer_rates_filter.update(xfer_deltas.astype(np.float64) / dt_trace)
            xfer_counts_prev = xfer_counts.copy()
            xfer_rates = xfer_rates_filter.compute()
            byte_rates_filter.update((byte_counts - byte_counts_prev).astype(np.float64) / dt_trace)
            byte_counts_prev = byte_counts.copy()
            byte_rates = byte_rates_filter.compute()
            elapsed_stats = node.loop.time() - ts_started

            # Recompute the new node state snapshots
            snapshot: Dict[Optional[int], NodeState] = {}
            for node_id, mo in sorted(models.items(), key=lambda x: x[0] if x[0] is not None else N_NODES):
                assert isinstance(mo, Avatar)
                snapshot[node_id] = mo.update(ts)

            # Render the model into a huge text buffer
            ts_render_started = node.loop.time()
            view.render(
                states=snapshot,
                xfer_deltas=xfer_deltas,
                xfer_rates=xfer_rates,
                byte_rates=byte_rates,
                total_transport_errors=total_transport_error_count,
                fir_window_duration=fir_window_duration,
            )
            elapsed_render = node.loop.time() - ts_render_started

            # Send the text buffer to the terminal (may block)
            await node.loop.run_in_executor(None, refresh_screen, view.flip_buffer())
            _logger.info(
                "Updated in %.3f (stats %.3f, render %.3f); dt %.3f; time lag %.3f",
                node.loop.time() - ts_started,
                elapsed_stats,
                elapsed_render,
                dt_trace,
                ts_started - ts,
            )


def linearize_data_specifier(ds: pyuavcan.transport.DataSpecifier) -> int:
    if isinstance(ds, MessageDataSpecifier):
        return int(ds.subject_id)
    if isinstance(ds, ServiceDataSpecifier):
        if ds.role == ServiceDataSpecifier.Role.REQUEST:
            return int(ds.service_id + N_SUBJECTS)
        if ds.role == ServiceDataSpecifier.Role.RESPONSE:
            return int(ds.service_id + N_SUBJECTS + N_SERVICES)
    assert False


_T = TypeVar("_T")


class MovingAverage(Generic[_T]):
    """
    Beware that this implementation trades off numerical stability for speed.
    This is adequate for a visualization tool but may be unsuitable in most other applications.
    The reason it is unstable is that instead of recomputing the sum at every iteration
    it keeps a running sum whose delta is updated at every update.
    The delta update may cause accumulation of numerical errors over time.
    """

    def __init__(self, depth: int) -> None:
        from collections import deque

        self._depth = int(depth)
        self._samples: deque[_T] = deque(maxlen=depth)
        self._sum: Any = None

    def update(self, x: _T) -> None:
        if x is None:
            raise ValueError(repr(x))
        if self._sum is not None:
            if len(self._samples) >= self._depth:
                self._sum -= self._samples.popleft()
            self._sum += x
        else:
            from copy import copy

            self._sum = copy(x)
        self._samples.append(x)

    def compute(self) -> _T:
        return cast(_T, self._sum * (1.0 / len(self._samples)))


_logger = yakut.get_logger(__name__)
