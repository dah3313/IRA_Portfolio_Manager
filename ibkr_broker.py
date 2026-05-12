"""
IRAPM IBKR Broker — Phase 2a: connection lifecycle.

This module is the IBKR-specific implementation of the Broker protocol
(broker_protocol.py). It uses ib_async to talk to TWS or IB Gateway.

PHASE STATUS

  Phase 2a (this file, current state): Connection lifecycle only.
    Implemented: connect(), disconnect(), is_ready(),
      get_managed_account_id(), get_server_time().
    Stubs that raise NotImplementedError: all other Broker methods.
    The file is importable and the connection methods are exercised
    by unit tests. The full protocol surface is NOT yet usable.

  Phase 2b (next): State queries (get_positions, get_prices,
    get_account_summary, get_recent_activity).

  Phase 2c (final): Order placement (place_order with idempotent
    rediscovery and post-placement confirmation, get_order_status,
    cancel_order) plus ACH update.

LIBRARY

  ib_async 2.1.0 (pinned exactly in requirements.txt). Community fork
  of ib_insync, maintained by Matt Stancliff under the ib-api-reloaded
  GitHub org. See module-level docstring in broker_protocol.py for the
  rationale and library-abandonment mitigation strategy.

DEPLOYMENT REQUIREMENTS (encoded here for the runbook)

  TWS or IB Gateway must be running on the box and configured to
  accept API connections. Specifically:

    1. API enabled in TWS Configure → API → Settings.
    2. Port matches what IBKRBroker is constructed with:
         7497 = TWS paper trading
         7496 = TWS live trading
         4002 = IB Gateway paper trading
         4001 = IB Gateway live trading
       Use the gateway ports in production (it's lighter than TWS).
    3. "Download open orders on connection" MUST be checked. Without
       this, the idempotency design's open-order lookup returns
       incomplete results — silent failure mode. The runbook gates
       deployment on this setting.
    4. "Allow connections from localhost only" or Trusted IP
       127.0.0.1 added (we never connect from off-box).
    5. Memory Allocation increased to ≥ 4096 MB (per ib_async docs;
       prevents Gateway crashes on bulk data fetches).
    6. clientId assigned per box, fixed for the box's lifetime:
         Box-A: clientId=11
         Box-B: clientId=12
       Never use clientId=0 (special TWS "master client" mode that
       has different semantics for inbound order events; conflicts
       with our protocol's per-box-attribution design).

CONFIGURATION

  IBKRBroker takes all its configuration via constructor arguments.
  There are NO defaults: each deployment must explicitly pass
  host, port, client_id, and expected_account_id. This prevents the
  "default port silently routed to live" failure mode.

  Box-local config (NOT in the shared ruleset.yaml — these are
  per-box values) lives in /etc/irapm/box.yaml (per runbook §14.3
  TBD). The cycle launcher reads box.yaml, constructs an IBKRBroker,
  and passes it down.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from broker_protocol import (
    BrokerInconsistency,
    BrokerNotReady,
    BrokerRejection,
    BrokerUnreachable,
)
from broker_types import (
    AccountSummary,
    AchUpdateResult,
    ContractRef,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderStatusValue,
    OrderType,
    Position,
    Price,
    PriceStatus,
    RecentActivity,
    RecurringAchInfo,
    TimeInForce,
)
from clock import Clock, SystemClock


logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

BROKER_IMPL_TAG = "IBKRBroker"
"""Identifier stamped onto every ContractRef this broker mints. Used by
the protocol's cross-broker safety check."""

CONNECT_TIMEOUT_DEFAULT_SEC = 30.0
"""Default timeout for ib.connect(). ib_async raises asyncio.TimeoutError
if synchronization doesn't complete within this window. 30 sec is
generous for a healthy local TWS — typical handshake completes in
2-5 sec. The longer window is to tolerate occasional Gateway hiccups
without aborting the cycle."""

POST_PLACEMENT_CONFIRMATION_WINDOW_SEC = 15.0
"""Window within which place_order() must see the submitted order
in a recognized state (PendingSubmit, PreSubmitted, Submitted, Filled,
etc.) before raising BrokerInconsistency.

Fifteen seconds is generous for normal IBKR processing (typical
confirmation < 1 sec) and leaves headroom for occasional API-callback
lag during heavy-load periods or near IBKR's 23:45 ET daily reset.
The cost-benefit is asymmetric: a false-positive BrokerInconsistency
is the one halt category NOT eligible for 48h auto-resume (requires
operator review), so a slightly longer wait is cheap insurance.
A cycle stalling on it for the full 15 sec is still operator-visible
quickly (cycles run in the background, not interactively)."""

POST_PLACEMENT_ACTIVITY_LOOKBACK_HOURS = 48
"""How far back place_order() looks when searching for an existing
order with the same client_order_id (idempotent rediscovery). Matches
the operational_pause auto-resume window so that a cycle restarted
within 48h of its prior attempt's failure can find any orders that
attempt placed."""

# Acceptable TWS API ports — used for argument validation defense
# against transposed/typo'd port numbers (e.g., 7496 vs 7497).
KNOWN_TWS_PORTS = {7496, 7497, 4001, 4002}


# =============================================================================
# Decimal-conversion helper
# =============================================================================

def _to_decimal(value) -> Decimal:
    """Convert any numeric-like value to Decimal safely.

    ib_async returns numeric fields as a mix of float, Decimal, and
    occasionally str. Direct float→Decimal conversion produces ugly
    representation artifacts (Decimal(0.1) → Decimal('0.10000...555')),
    so we always go through str() for floats.

    Raises ValueError if the value cannot be parsed as a Decimal.
    Returns Decimal('0') for None (callers that need to distinguish
    None from 0 should check before calling).
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Float artifacts defense: go through str().
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    # Fallback: try str() conversion. Rare types like numpy scalars
    # would land here.
    try:
        return Decimal(str(value))
    except Exception as e:
        raise ValueError(
            f"Cannot convert {value!r} (type {type(value).__name__}) "
            f"to Decimal: {e}"
        )


# =============================================================================
# IBKRBroker
# =============================================================================

class IBKRBroker:
    """IBKR-backed Broker implementation.

    Phase 2a status: connection lifecycle implemented; state queries
    and order placement raise NotImplementedError pending Phase 2b/2c.

    Conforms structurally to broker_protocol.Broker.

    USAGE (once all phases complete)

        broker = IBKRBroker(
            host='127.0.0.1',
            port=4001,                  # IB Gateway live
            client_id=11,               # box-A
            expected_account_id='U1234567',
            clock=SystemClock(),
        )
        with broker_session(broker) as b:
            positions = b.get_positions()
            # ... cycle logic ...

    CONSTRUCTION PHILOSOPHY

    All configuration is explicit in the constructor — no defaults for
    host/port/client_id/expected_account_id. The runbook gates
    deployment on the cycle launcher correctly reading box.yaml and
    passing the right values. A silent default of port=7497 or
    client_id=1 would be a serious safety regression.
    """

    # -------------------------------------------------------------------------
    # Construction
    # -------------------------------------------------------------------------

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        expected_account_id: str,
        clock: Optional[Clock] = None,
    ) -> None:
        """Construct an IBKRBroker. Does NOT connect.

        Args:
          host: TWS/Gateway hostname. Almost always '127.0.0.1' — we
            connect to a local TWS/Gateway, never to a remote one.
          port: TWS/Gateway API port. Validated against KNOWN_TWS_PORTS;
            unknown ports raise ValueError at construction time
            (typo defense — easy to confuse 7497/7496/4001/4002).
          client_id: Persistent client ID for this box. Box-local
            config supplies this; 11 for box-A, 12 for box-B. Must
            NOT be 0 (TWS master-client mode has different semantics).
          expected_account_id: IBKR account number this broker MUST
            be connected to. connect() verifies the actual connected
            account matches; mismatch raises BrokerInconsistency.
            Defense against accidental wrong-account connection.
          clock: Time source. Defaults to SystemClock() since IBKRBroker
            is the production path; tests can inject FrozenClock if
            they want timestamp determinism (but generally tests
            should target SyntheticBroker, not IBKRBroker).
        """
        # ---- Argument validation ----
        if not host:
            raise ValueError("host is required (typically '127.0.0.1')")

        if port not in KNOWN_TWS_PORTS:
            raise ValueError(
                f"port {port} is not a known TWS/Gateway port. Valid: "
                f"7497 (TWS paper), 7496 (TWS live), 4002 (Gateway paper), "
                f"4001 (Gateway live). If you intended a non-standard port, "
                f"update KNOWN_TWS_PORTS in ibkr_broker.py."
            )

        if client_id == 0:
            raise ValueError(
                "client_id=0 is reserved for TWS master-client mode and "
                "has different semantics that conflict with IRAPM's "
                "per-box-attribution design. Use a non-zero client_id "
                "(typically 11 for box-A, 12 for box-B)."
            )
        if client_id < 0:
            raise ValueError(f"client_id must be positive; got {client_id}")

        if not expected_account_id:
            raise ValueError(
                "expected_account_id is required. Set this to your IBKR "
                "account number (e.g., 'U1234567'). connect() verifies "
                "the actual connected account matches this value; "
                "mismatch refuses to proceed."
            )

        # ---- Store config ----
        self._host = host
        self._port = port
        self._client_id = client_id
        self._expected_account_id = expected_account_id
        self._clock = clock or SystemClock()

        # ---- ib_async client (constructed lazily on first connect) ----
        # We defer ib_async import to first connect() call so that the
        # rest of the codebase can import ibkr_broker.py without
        # ib_async installed (e.g., in environments that only run
        # simulator tests with SyntheticBroker).
        self._ib = None  # type: ignore[var-annotated]

        # ---- Connection state ----
        self._connected = False

        # ---- Contract cache: symbol → ContractRef ----
        # Populated lazily as positions are fetched or orders are placed.
        # Survives across reconnects since the underlying IBKR contracts
        # (conId, exchange, currency) don't change for ETF symbols.
        self._contract_cache: dict[str, ContractRef] = {}

        logger.info(
            "IBKRBroker constructed: host=%s port=%d client_id=%d "
            "expected_account=%s",
            host, port, client_id, expected_account_id,
        )

    # -------------------------------------------------------------------------
    # Connection lifecycle (Phase 2a — IMPLEMENTED)
    # -------------------------------------------------------------------------

    def connect(self, *, timeout_sec: float = CONNECT_TIMEOUT_DEFAULT_SEC) -> None:
        """Establish a fresh connection to TWS/Gateway and verify it.

        Steps:
          1. Lazily import ib_async (deferred to avoid hard dependency).
          2. Construct (or reuse) the IB() client object.
          3. Call ib.connect() with our configured host/port/client_id.
             Per ib_async docs, this blocks until "fully synchronized
             and ready to serve requests" — meaning open orders and
             account snapshots have been downloaded.
          4. Verify the connected account matches expected_account_id.
             Mismatch is BrokerInconsistency and we disconnect.
          5. Set self._connected = True. Subsequent state-query methods
             will work.

        IDEMPOTENCY & SELF-HEALING (Change 4):
          If our internal flag says we're connected, cross-check with
          ib_async.isConnected() before treating the call as a no-op.
          When the library reports the underlying connection has been
          lost (TWS restart, network blip, killed process between
          cycles), we flip our flag back to False and proceed with a
          real reconnect. This aligns connect() with is_ready()'s view
          of reality and supports the fail-safe / self-resolving policy:
          an operator (or the cycle launcher) calling connect() on a
          stale-flag broker gets an actual fresh connection rather than
          a silent no-op that leaves them in the broken state.

        Translation of ib_async exceptions:
          asyncio.TimeoutError                → BrokerUnreachable
          ConnectionRefusedError               → BrokerUnreachable
          OSError (broader network failures)   → BrokerUnreachable
          (account-mismatch detected internally) → BrokerInconsistency

        Args:
          timeout_sec: Max time to wait for ib.connect() to complete.
            Default 30 sec; the design's per-cycle lifecycle calls
            this once per cycle, so 30 sec is well within cycle
            timing budgets.

        Raises:
          BrokerUnreachable: TCP/timeout/auth failure within timeout.
          BrokerInconsistency: connected to wrong account; library
            returned data we don't trust.
        """
        if self._connected:
            # Cross-check the library's view before declaring no-op.
            # If ib_async disagrees, the underlying socket has dropped
            # since the last successful connect() and our internal
            # flag is stale — fall through to a real reconnect.
            library_says_connected = False
            if self._ib is not None:
                try:
                    library_says_connected = bool(self._ib.isConnected())
                except Exception as e:
                    # If isConnected() itself raised, treat as not
                    # connected. Don't propagate — we're about to
                    # reconnect anyway.
                    logger.warning(
                        "connect(): isConnected() raised during stale-flag "
                        "cross-check; treating as not-connected and "
                        "reconnecting: %s", e,
                    )
            if library_says_connected:
                logger.debug("connect() called when already connected; no-op.")
                return
            # Stale flag detected. Reset and proceed to a real reconnect.
            logger.info(
                "connect(): internal flag was True but ib_async reports "
                "not connected (TWS restart or connection drop suspected). "
                "Resetting state and reconnecting."
            )
            self._connected = False

        # ---- Lazy import of ib_async ----
        try:
            from ib_async import IB
        except ImportError as e:
            raise BrokerUnreachable(
                f"ib_async library is not installed: {e}. Install with "
                f"`pip install ib_async==2.1.0` (the version pinned for "
                f"IRAPM). Without ib_async, IBKRBroker cannot function; "
                f"SyntheticBroker is available for non-production use."
            ) from e

        # Construct the IB client on first connect; reuse across reconnects
        # within this IBKRBroker instance's lifetime.
        if self._ib is None:
            self._ib = IB()

        logger.info(
            "Connecting to TWS/Gateway: host=%s port=%d client_id=%d "
            "timeout=%.1fs",
            self._host, self._port, self._client_id, timeout_sec,
        )

        # ---- Call ib.connect(); translate exceptions ----
        try:
            self._ib.connect(
                host=self._host,
                port=self._port,
                clientId=self._client_id,
                timeout=timeout_sec,
                # readonly=False: we will place orders. readonly=True
                # would be safer for a diagnostic tool; not for the
                # cycle launcher.
                readonly=False,
                # raiseSyncErrors=True: per ib_async docs, this causes
                # initial sync errors to raise ConnectionError rather
                # than just being logged. We WANT loud failures.
                raiseSyncErrors=True,
            )
        except asyncio.TimeoutError as e:
            raise BrokerUnreachable(
                f"ib.connect() timed out after {timeout_sec}s. "
                f"Verify TWS/Gateway is running and listening on port "
                f"{self._port}, and that API connections are enabled."
            ) from e
        except ConnectionRefusedError as e:
            raise BrokerUnreachable(
                f"Connection refused by TWS/Gateway at "
                f"{self._host}:{self._port}. Is TWS/Gateway running?"
            ) from e
        except ConnectionError as e:
            # ib_async raises ConnectionError on initial-sync failures
            # when raiseSyncErrors=True. The message has the diagnostic
            # info; preserve it.
            raise BrokerUnreachable(
                f"TWS/Gateway connection or initial-sync error: {e}"
            ) from e
        except OSError as e:
            raise BrokerUnreachable(
                f"OS-level network error during connect to "
                f"{self._host}:{self._port}: {e}"
            ) from e

        # ---- Verify account ----
        # ib_async populates managedAccounts() during the sync that
        # connect() blocks on; the list should be non-empty post-connect.
        try:
            accounts = self._ib.managedAccounts()
        except Exception as e:
            self._safe_disconnect_after_failure()
            raise BrokerInconsistency(
                f"managedAccounts() failed after connect(): {e}. "
                f"ib_async should have populated this during initial sync."
            ) from e

        if not accounts:
            self._safe_disconnect_after_failure()
            raise BrokerInconsistency(
                "managedAccounts() returned empty list after connect(). "
                "Initial-sync handshake appears incomplete; refusing to "
                "proceed."
            )

        if len(accounts) > 1:
            # IRAPM is designed for a single managed account. Multiple
            # managed accounts would suggest a Family Office or Friends
            # & Family advisor setup; the cycle logic assumes one
            # account and a multi-account environment is silently
            # ambiguous. Refuse.
            self._safe_disconnect_after_failure()
            raise BrokerInconsistency(
                f"Multiple managed accounts returned: {accounts!r}. "
                f"IRAPM requires a single-account setup. If you are an "
                f"advisor or have linked accounts, configure TWS to "
                f"present only the IRA account to this API client."
            )

        actual_account = accounts[0]
        if actual_account != self._expected_account_id:
            self._safe_disconnect_after_failure()
            raise BrokerInconsistency(
                f"Connected to account {actual_account!r} but configuration "
                f"expected {self._expected_account_id!r}. Refusing to "
                f"proceed — this defends against accidental wrong-account "
                f"connection (e.g., paper-port pointed at live TWS, or "
                f"a TWS instance logged into a different IBKR user)."
            )

        # ---- All checks passed ----
        self._connected = True
        logger.info(
            "Connected to account %s on client_id=%d.",
            actual_account, self._client_id,
        )

    def disconnect(self) -> None:
        """Close the connection cleanly. Idempotent; never raises.

        Per the protocol contract, this is called in finally blocks and
        MUST NOT raise. We catch every exception ib_async could plausibly
        raise (including ones it shouldn't) and log them.
        """
        if not self._connected or self._ib is None:
            logger.debug("disconnect() called when not connected; no-op.")
            return

        try:
            self._ib.disconnect()
            logger.info("Disconnected from TWS/Gateway.")
        except Exception as e:
            # Log loudly but DO NOT raise — disconnect-in-finally must
            # not mask the original exception that triggered the
            # finally block.
            logger.error(
                "disconnect() raised an exception (suppressed): %s", e,
                exc_info=True,
            )
        finally:
            self._connected = False

    def _safe_disconnect_after_failure(self) -> None:
        """Internal helper: disconnect after a post-connect check fails.

        Used when connect() succeeded at the TCP/sync level but a
        subsequent validation (e.g., account mismatch) failed. We
        want to close the connection so the next attempt is clean,
        but we don't want disconnect errors to mask the original
        validation failure that's about to be raised.
        """
        if self._ib is None:
            return
        try:
            self._ib.disconnect()
        except Exception as e:
            logger.warning(
                "_safe_disconnect_after_failure: disconnect raised "
                "(ignored): %s", e,
            )
        self._connected = False

    def is_ready(self) -> bool:
        """True if the broker is connected and fully synchronized.

        Cross-checks against ib_async's own isConnected() to catch the
        case where ib_async noticed a connection loss but we haven't
        observed it yet (e.g., TWS process killed between cycles).
        """
        if not self._connected:
            return False
        if self._ib is None:
            return False
        try:
            return bool(self._ib.isConnected())
        except Exception as e:
            logger.warning(
                "is_ready: ib.isConnected() raised (treating as not "
                "ready): %s", e,
            )
            return False

    # -------------------------------------------------------------------------
    # Misc (Phase 2a — IMPLEMENTED)
    # -------------------------------------------------------------------------

    def get_managed_account_id(self) -> str:
        """Return the IBKR account number we're connected to."""
        self._require_connected()
        assert self._ib is not None  # narrowing for type checker
        try:
            accounts = self._ib.managedAccounts()
        except Exception as e:
            raise BrokerInconsistency(
                f"managedAccounts() raised: {e}"
            ) from e
        if not accounts:
            raise BrokerInconsistency(
                "managedAccounts() returned empty list while connected."
            )
        return accounts[0]

    def get_server_time(self) -> datetime:
        """Return TWS server time (UTC).

        UNCERTAINTY FLAG: ib_async's IB.reqCurrentTime() returns a
        datetime; verify timezone handling against paper trading.
        Some IBKR API versions returned this as naive UTC; others as
        epoch seconds. The implementation below assumes datetime
        return; will be tightened in Phase 2b verification.
        """
        self._require_connected()
        assert self._ib is not None
        try:
            current = self._ib.reqCurrentTime()
        except Exception as e:
            raise BrokerInconsistency(
                f"reqCurrentTime() raised: {e}"
            ) from e
        # If it's a datetime, return it (assuming UTC); if not, this
        # will surface as a clear error in paper-trading verification.
        if isinstance(current, datetime):
            return current
        raise BrokerInconsistency(
            f"reqCurrentTime() returned {type(current).__name__} "
            f"(expected datetime). Verify ib_async version compatibility."
        )

    # -------------------------------------------------------------------------
    # Internal helper: clock seam translated to UTC tz-aware
    # -------------------------------------------------------------------------

    def _clock_now_utc(self) -> datetime:
        """Return current clock time as tz-aware UTC.

        The Clock seam exposes now() (naive local) and now_et()
        (tz-aware ET). IRAPM types use tz-aware UTC; we go via now_et()
        which is always tz-aware, then convert.
        """
        from datetime import timezone as _tz
        et = self._clock.now_et()
        return et.astimezone(_tz.utc)

    # -------------------------------------------------------------------------
    # Internal helper: precondition check
    # -------------------------------------------------------------------------

    def _require_connected(self) -> None:
        """Raise BrokerNotReady if not currently connected.

        Used as the first line of every Broker method (other than
        connect/disconnect/is_ready) to provide a uniform precondition
        failure.
        """
        if not self._connected or self._ib is None:
            raise BrokerNotReady(
                "IBKRBroker method called when not connected. Call "
                "connect() first, or use the broker_session() context "
                "manager."
            )
        # Belt-and-suspenders: also check ib_async's own view, since
        # the connection could have dropped between cycles.
        try:
            ib_says_connected = bool(self._ib.isConnected())
        except Exception:
            # If isConnected() itself raised, treat as not-ready.
            ib_says_connected = False
        if not ib_says_connected:
            # Our internal flag says connected but ib_async disagrees —
            # the underlying connection has been lost. Update our
            # state and raise.
            self._connected = False
            raise BrokerNotReady(
                "Connection to TWS/Gateway has been lost since the "
                "last connect(). The next cycle attempt will retry."
            )

    # =========================================================================
    # PHASE 2b — state queries (IMPLEMENTED)
    # =========================================================================
    #
    # ib_async keeps positions, account values, open orders, and executions
    # automatically synchronized with TWS/IBG via the asyncio event loop —
    # after connect() returns, ib.positions() / ib.accountValues() /
    # ib.openTrades() return the most recent snapshot without a fresh
    # round-trip. We use these "current state" accessors where possible
    # for speed.
    #
    # For get_recent_activity (split-brain defense), we deliberately use
    # the explicit req* methods (reqAllOpenOrders, reqCompletedOrders,
    # reqExecutions) to FORCE a fresh fetch — we don't trust the cached
    # event stream for this critical safety check, because:
    #   - The cached open-orders stream only includes orders from THIS
    #     client by default. reqAllOpenOrders includes all clients,
    #     which is what we need to detect peer-box and external activity.
    #   - The cached event stream may not have caught up if the cycle
    #     starts immediately after connect; the req* methods block until
    #     the fresh snapshot arrives.
    # =========================================================================

    def get_positions(self) -> list[Position]:
        """Return current positions in the managed account.

        Source: ib.positions() — cached, kept in sync by ib_async.
        We filter to positions where account == self._expected_account_id
        (defensive: if multiple accounts somehow appeared, we only
        return ours). We also filter out zero-quantity positions per
        the protocol contract.

        Side effect: populates self._contract_cache with the underlying
        ib_async Contract object for each symbol, so subsequent
        place_order() calls can reuse the resolved contract without
        re-fetching.
        """
        self._require_connected()
        assert self._ib is not None

        try:
            raw_positions = self._ib.positions()
        except Exception as e:
            raise BrokerInconsistency(
                f"ib.positions() raised: {e}"
            ) from e

        result: list[Position] = []
        for rp in raw_positions:
            # ib_async Position dataclass: account, contract, position, avgCost
            if rp.account != self._expected_account_id:
                # Defensive: shouldn't happen given connect() account
                # validation, but filter just in case.
                logger.warning(
                    "get_positions: skipping position for non-managed "
                    "account %r (expected %r)",
                    rp.account, self._expected_account_id,
                )
                continue

            # ib_async returns quantity as float (or Decimal in newer
            # versions); convert via str() to avoid float artifacts.
            quantity = _to_decimal(rp.position)
            if quantity == 0:
                # Zero-position rows happen when a position is fully
                # closed mid-session; ib_async may keep them in the
                # cache. Filter per protocol contract.
                continue

            symbol = rp.contract.symbol
            contract_ref = self._cache_contract(symbol, rp.contract)

            avg_cost = _to_decimal(rp.avgCost)

            # Market value: we don't have a fresh price here without
            # a separate reqMktData call. For now, populate with
            # (quantity × avg_cost) as a stale-but-non-zero placeholder.
            # The decision layer should call get_prices() for fresh
            # market values; market_value here is reported only for
            # operator-facing summary purposes.
            #
            # UNCERTAINTY FLAG: ib_async's PortfolioItem has a
            # marketValue field, but Position does not. We use
            # ib.portfolio() to get marketValue if it's important.
            # For now, avg_cost * quantity is the conservative choice.
            market_value = quantity * avg_cost

            result.append(Position(
                contract=contract_ref,
                symbol=symbol,
                quantity=quantity,
                market_value=market_value,
                avg_cost=avg_cost,
            ))

        return result

    def get_prices(self, symbols: list[str]) -> dict[str, Price]:
        """Return current prices for the named symbols via snapshot
        market data requests.

        Uses ib.reqTickers() with snapshot=True semantics — blocks until
        all tickers are populated or until ib_async's internal timeout
        (typically ~11 sec). Returns a Price with status=OK for each
        symbol that produced a usable last/midpoint, or
        status=UNAVAILABLE for symbols where no price could be obtained
        (market closed, no subscription, contract not qualified, etc.).

        Contracts are taken from self._contract_cache, populated by
        get_positions() or by explicit qualifyContracts() if no prior
        position exists. For symbols not in the cache, we attempt to
        qualify them as US Stock ETFs (SMART exchange, USD currency) —
        this works for all IRAPM ETF universe symbols. Mutual fund
        support is not in scope for v2 (IRAPM_v2 is ETF-only per
        the asset universe).

        UNCERTAINTY FLAG: ib.reqTickers() snapshot semantics and
        timeout behavior need paper-trading verification. In
        particular, whether a symbol with no live tick data returns a
        Ticker with all-NaN fields (which we'd treat as UNAVAILABLE)
        or raises an exception, varies by IBKR data subscription
        configuration.
        """
        self._require_connected()
        assert self._ib is not None

        if not symbols:
            return {}

        # Build contract list, qualifying as-needed for symbols we
        # haven't seen before.
        contracts = []
        symbol_order: list[str] = []
        unresolved: list[str] = []
        for symbol in symbols:
            cached = self._contract_cache.get(symbol)
            if cached is not None and "ib_contract" in cached.payload:
                # Reuse the underlying ib_async Contract.
                contracts.append(cached.payload["ib_contract"])
                symbol_order.append(symbol)
            else:
                unresolved.append(symbol)

        # Qualify unknown symbols as US ETFs (Stock, SMART, USD).
        # This is correct for the IRAPM ETF universe; if IRAPM ever
        # adds non-US instruments, this needs revisiting.
        if unresolved:
            # Resolve the Stock class. Tests can inject a mock by
            # setting self._stock_class before connect().
            stock_class = getattr(self, "_stock_class", None)
            if stock_class is None:
                try:
                    from ib_async import Stock  # local import (lazy)
                except ImportError as e:
                    raise BrokerUnreachable(
                        f"ib_async Stock import failed: {e}"
                    ) from e
                stock_class = Stock
            new_contracts = [stock_class(s, "SMART", "USD") for s in unresolved]
            try:
                qualified = self._ib.qualifyContracts(*new_contracts)
            except Exception as e:
                raise BrokerInconsistency(
                    f"qualifyContracts() raised for symbols {unresolved}: {e}"
                ) from e
            for sym, qc in zip(unresolved, qualified):
                # qualifyContracts may return Contract objects with
                # conId populated; if not, the symbol is unresolvable.
                if not getattr(qc, "conId", 0):
                    logger.warning(
                        "get_prices: symbol %r could not be qualified "
                        "(no conId); will report UNAVAILABLE", sym,
                    )
                    continue
                self._cache_contract(sym, qc)
                contracts.append(qc)
                symbol_order.append(sym)

        # Fetch tickers in snapshot mode.
        result: dict[str, Price] = {}
        if contracts:
            try:
                tickers = self._ib.reqTickers(*contracts, regulatorySnapshot=False)
            except Exception as e:
                raise BrokerInconsistency(
                    f"reqTickers() raised: {e}"
                ) from e

            now = self._clock_now_utc()
            for symbol, ticker in zip(symbol_order, tickers):
                price_value = self._extract_price_from_ticker(ticker)
                if price_value is None:
                    result[symbol] = Price(
                        symbol=symbol,
                        status=PriceStatus.UNAVAILABLE,
                        price=None,
                        as_of=None,
                    )
                else:
                    result[symbol] = Price(
                        symbol=symbol,
                        status=PriceStatus.OK,
                        price=price_value,
                        as_of=now,
                    )

        # For any input symbol we couldn't resolve at all, report UNAVAILABLE.
        for symbol in symbols:
            if symbol not in result:
                result[symbol] = Price(
                    symbol=symbol,
                    status=PriceStatus.UNAVAILABLE,
                    price=None,
                    as_of=None,
                )

        return result

    def get_account_summary(self) -> AccountSummary:
        """Return cash, settled cash, unsettled cash, and buying power.

        Source: ib.accountValues() — cached, kept in sync. Each value
        has tag, value, currency, account, modelCode. We filter to
        our account and USD currency, and pick the four tags we need.

        TAG MAPPING (IBKR-side → IRAPM-side):
          'TotalCashValue'  → total_cash_value
          'SettledCash'     → settled_cash
          'BuyingPower'     → buying_power
          (unsettled_cash is computed: total_cash_value - settled_cash)

        UNCERTAINTY FLAG: IBKR has many cash-related tags
        ('AvailableFunds', 'NetLiquidationByCurrency', 'TotalCashBalance',
        etc.) and the exact semantics of each vary slightly by account
        type. The four picked here are the conservative choice for an
        IRA cash account but need paper-trading verification to confirm
        they sum/relate as expected.
        """
        self._require_connected()
        assert self._ib is not None

        try:
            values = self._ib.accountValues(self._expected_account_id)
        except Exception as e:
            raise BrokerInconsistency(
                f"accountValues() raised: {e}"
            ) from e

        # Index by (tag, currency)
        by_tag: dict[tuple[str, str], str] = {}
        for v in values:
            # v has tag, value, currency, account, modelCode
            if getattr(v, "modelCode", "") != "":
                # Skip model-portfolio rows; we only want the real
                # account.
                continue
            by_tag[(v.tag, v.currency)] = v.value

        def _get(tag: str, default: str = "0") -> Decimal:
            # Prefer USD; fall back to BASE (some accounts report BASE only).
            for currency in ("USD", "BASE"):
                key = (tag, currency)
                if key in by_tag:
                    raw = by_tag[key]
                    try:
                        return _to_decimal(raw)
                    except Exception as e:
                        raise BrokerInconsistency(
                            f"accountValue tag {tag!r} ({currency}) returned "
                            f"non-numeric value {raw!r}: {e}"
                        ) from e
            return _to_decimal(default)

        total_cash = _get("TotalCashValue")
        settled = _get("SettledCash", default=str(total_cash))
        buying_power = _get("BuyingPower", default=str(settled))
        unsettled = total_cash - settled

        return AccountSummary(
            account_id=self._expected_account_id,
            total_cash_value=total_cash,
            settled_cash=settled,
            unsettled_cash=unsettled,
            buying_power=buying_power,
        )

    def get_recent_activity(self, *, since: datetime) -> RecentActivity:
        """Return open orders + recently-completed orders + recent fills.

        This is the split-brain defense linchpin (master/slave design
        defense layer 3). We FORCE a fresh fetch via the req* methods
        rather than reading the cached state, because:

          1. reqAllOpenOrders includes orders from ALL clients on the
             account — peer-box orders, manual TWS orders, our own
             orders. The cached ib.openTrades() only includes this
             client's orders.
          2. reqCompletedOrders pulls TWS's recent-completions log,
             which includes everything that completed today and
             possibly yesterday.
          3. reqExecutions returns fills from the broker's recent
             history (filtered by time via ExecutionFilter).

        We aggregate, deduplicate by client_order_id (orderRef), and
        return in the protocol's RecentActivity shape.

        UNCERTAINTY FLAG: several behaviors here need paper-trading
        verification:
          - The exact retention window of reqCompletedOrders (TWS
            docs say "today and previous day" but real behavior may
            vary).
          - Whether reqExecutions with no filter returns all executions
            or is rate-limited.
          - PendingSubmit and PendingCancel orders' visibility in
            reqAllOpenOrders.
        """
        self._require_connected()
        assert self._ib is not None
        now = self._clock_now_utc()

        # Fetch the three lists. Each can raise; translate to typed
        # exceptions.
        try:
            open_trades = self._ib.reqAllOpenOrders()
        except Exception as e:
            raise BrokerInconsistency(
                f"reqAllOpenOrders() raised: {e}"
            ) from e

        try:
            # apiOnly=False so we also see manually-placed TWS orders
            # — needed for the external-activity detection path.
            completed_trades = self._ib.reqCompletedOrders(apiOnly=False)
        except Exception as e:
            raise BrokerInconsistency(
                f"reqCompletedOrders() raised: {e}"
            ) from e

        try:
            fills = self._ib.reqExecutions()
        except Exception as e:
            raise BrokerInconsistency(
                f"reqExecutions() raised: {e}"
            ) from e

        # Convert to protocol types and filter by `since` where appropriate.
        open_results: list[OrderResult] = [
            self._trade_to_order_result(t) for t in open_trades
        ]

        recent_completed: list[OrderResult] = []
        for t in completed_trades:
            # Determine submission time from the trade log if available,
            # else use the orderStatus or fall back to "now" (which
            # would always be within the window).
            submitted_at = self._trade_submitted_at(t, fallback=now)
            if submitted_at >= since:
                recent_completed.append(self._trade_to_order_result(t))

        recent_fills: list[Fill] = []
        for f in fills:
            fill_time = self._fill_time(f, fallback=now)
            if fill_time >= since:
                recent_fills.append(self._ib_fill_to_protocol_fill(f, fill_time))

        # Sort per protocol contract.
        open_results.sort(key=lambda o: o.submitted_at)
        recent_completed.sort(key=lambda o: o.submitted_at)
        recent_fills.sort(key=lambda fil: fil.fill_time)

        return RecentActivity(
            as_of=now,
            open_orders=open_results,
            recently_completed_orders=recent_completed,
            recent_fills=recent_fills,
        )

    # -------------------------------------------------------------------------
    # Internal helpers for Phase 2b
    # -------------------------------------------------------------------------

    def _cache_contract(self, symbol: str, ib_contract) -> ContractRef:
        """Store the ib_async Contract in our cache and return the
        opaque ContractRef the action layer sees.

        The payload carries the underlying ib_async Contract so that
        Phase 2c's place_order() can reuse it without re-resolving.
        """
        # Build a stable ContractRef. Use conId, exchange, currency in
        # the payload — these uniquely identify the IBKR instrument.
        payload = {
            "conId": getattr(ib_contract, "conId", 0),
            "exchange": getattr(ib_contract, "exchange", ""),
            "currency": getattr(ib_contract, "currency", ""),
            "secType": getattr(ib_contract, "secType", ""),
            # Reference to the actual Contract object (for placeOrder
            # later). NOT serialized through the protocol — the action
            # layer never reads payload. Lives only as long as this
            # IBKRBroker instance.
            "ib_contract": ib_contract,
        }
        ref = ContractRef(
            broker_impl=BROKER_IMPL_TAG,
            symbol=symbol,
            payload=payload,
        )
        self._contract_cache[symbol] = ref
        return ref

    def _extract_price_from_ticker(self, ticker) -> Optional[Decimal]:
        """Extract a usable price from an ib_async Ticker.

        Priority order:
          1. `last` (most recent trade)
          2. midpoint of bid/ask if both are present and positive
          3. `close` (yesterday's close, fallback for closed-market)
          4. None (UNAVAILABLE)

        ib_async represents missing values as NaN (float) or None.
        We treat NaN as missing.
        """
        import math

        def _valid(v) -> bool:
            if v is None:
                return False
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return False
            if math.isnan(fv) or fv <= 0:
                return False
            return True

        last = getattr(ticker, "last", None)
        if _valid(last):
            return _to_decimal(last)

        bid = getattr(ticker, "bid", None)
        ask = getattr(ticker, "ask", None)
        if _valid(bid) and _valid(ask):
            # Midpoint
            return (_to_decimal(bid) + _to_decimal(ask)) / Decimal("2")

        close = getattr(ticker, "close", None)
        if _valid(close):
            return _to_decimal(close)

        return None

    def _trade_to_order_result(self, trade) -> OrderResult:
        """Convert an ib_async Trade to our protocol OrderResult.

        ib_async Trade has: contract, order, orderStatus, fills (list).
        We extract the orderRef (our client_order_id), map status,
        convert fills.
        """
        order = trade.order
        os = trade.orderStatus
        # client_order_id: stored at IBKR as orderRef. For orders not
        # placed by IRAPM (external manual orders), orderRef will be
        # empty; we report empty string and let the action layer's
        # external-activity logic decide what to do.
        client_order_id = getattr(order, "orderRef", "") or ""
        broker_order_id = str(getattr(order, "orderId", 0))

        # Map status
        status = self._map_ib_status(getattr(os, "status", ""))
        rejection_reason: Optional[str] = None
        if status == OrderStatusValue.REJECTED:
            # ib_async sometimes carries reason in orderStatus.whyHeld
            # or in the trade.log. Use whyHeld as the primary source.
            rejection_reason = getattr(os, "whyHeld", "") or None

        # submitted_at: ib_async Trade.log is a list of TradeLogEntry
        # with .time. Use the first entry's time as submission time.
        submitted_at = self._trade_submitted_at(trade, fallback=self._clock_now_utc())

        # Convert fills
        protocol_fills = [
            self._ib_fill_to_protocol_fill(f, self._fill_time(f, fallback=submitted_at))
            for f in trade.fills
        ]

        # Compute aggregates from fills (safer than trusting orderStatus
        # numeric fields, which can be stale)
        filled_qty = sum((f.quantity for f in protocol_fills), Decimal("0"))
        if protocol_fills:
            total_value = sum(
                (f.quantity * f.price for f in protocol_fills),
                Decimal("0"),
            )
            avg_price = total_value / filled_qty if filled_qty > 0 else None
        else:
            avg_price = None

        return OrderResult(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            status=status,
            submitted_at=submitted_at,
            fills=protocol_fills,
            filled_quantity=filled_qty,
            average_fill_price=avg_price,
            rejection_reason=rejection_reason,
            idempotent_rediscovery=False,
        )

    def _ib_fill_to_protocol_fill(self, ib_fill, fill_time: datetime) -> Fill:
        """Convert an ib_async Fill (which wraps Execution + CommissionReport)
        to our protocol Fill."""
        execution = ib_fill.execution
        cr = getattr(ib_fill, "commissionReport", None)
        commission = Decimal("0")
        if cr is not None:
            commission = _to_decimal(getattr(cr, "commission", 0))

        return Fill(
            fill_id=str(execution.execId),
            fill_time=fill_time,
            quantity=_to_decimal(execution.shares),
            price=_to_decimal(execution.price),
            commission=commission,
        )

    def _trade_submitted_at(self, trade, *, fallback: datetime) -> datetime:
        """Extract the earliest log timestamp from a Trade as submission
        time. Returns the fallback if log is empty or times are missing.
        """
        log_entries = getattr(trade, "log", []) or []
        for entry in log_entries:
            t = getattr(entry, "time", None)
            if isinstance(t, datetime):
                # ib_async log times are UTC tz-aware per docs.
                if t.tzinfo is None:
                    from datetime import timezone as _tz
                    t = t.replace(tzinfo=_tz.utc)
                return t
        return fallback

    def _fill_time(self, ib_fill, *, fallback: datetime) -> datetime:
        """Extract fill time from an ib_async Fill, with fallback."""
        # ib_async Fill has .time (datetime, UTC) directly.
        t = getattr(ib_fill, "time", None)
        if isinstance(t, datetime):
            if t.tzinfo is None:
                from datetime import timezone as _tz
                t = t.replace(tzinfo=_tz.utc)
            return t
        return fallback

    def _map_ib_status(self, ib_status: str) -> OrderStatusValue:
        """Map ib_async/TWS order status string to OrderStatusValue.

        TWS states: PendingSubmit, PendingCancel, PreSubmitted,
        Submitted, ApiPending, ApiCancelled, Cancelled, Filled, Inactive.
        """
        mapping = {
            "PendingSubmit": OrderStatusValue.PENDING,
            "PreSubmitted": OrderStatusValue.PENDING,
            "ApiPending": OrderStatusValue.PENDING,
            "Submitted": OrderStatusValue.WORKING,
            "PendingCancel": OrderStatusValue.WORKING,  # still alive
            "Filled": OrderStatusValue.FILLED,
            "Cancelled": OrderStatusValue.CANCELLED,
            "ApiCancelled": OrderStatusValue.CANCELLED,
            "Inactive": OrderStatusValue.REJECTED,
            # See OrderStatusValue docstring for the Inactive
            # conservative-rejection design choice and its uncertainty.
        }
        return mapping.get(ib_status, OrderStatusValue.UNKNOWN)


    # =========================================================================
    # PHASE 2c — order placement (IMPLEMENTED)
    # =========================================================================
    #
    # This is the most safety-critical part of the broker layer. The
    # design is structured around six failure modes that idempotency
    # must survive (F1-F6 from the design discussion):
    #
    #   F1. Cycle crash after placeOrder returned but before state file
    #       write. The order is at IBKR; our state forgot. Restart calls
    #       place_order again with the same client_order_id.
    #   F2. Cycle crash mid-placeOrder (network blip). Unclear whether
    #       IBKR accepted the order. Restart re-attempts.
    #   F3. Slave promotion after master executed a withdrawal. Slave
    #       sees broker-side orders the dead master placed.
    #   F4. Cycle restart while a prior order is still PendingSubmit.
    #       Need to find it in open orders, not just completions.
    #   F5. Operator manually places an order via TWS portal that
    #       overlaps with what we want to place.
    #   F6. ib-async-level retry semantics on transient errors.
    #
    # Defense pattern (the same for all 6):
    #   1. Query IBKR for any order with orderRef == client_order_id
    #      across (a) open orders, (b) completed orders within 48h,
    #      (c) recent fills within 48h.
    #   2. If found: return that order's state with
    #      idempotent_rediscovery=True. No new submission.
    #   3. If not found: submit, then confirm the order appears in
    #      open-orders within POST_PLACEMENT_CONFIRMATION_WINDOW_SEC.
    #      Failure to confirm raises BrokerInconsistency.
    # =========================================================================

    def place_order(
        self,
        *,
        contract: ContractRef,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType,
        limit_price: Optional[Decimal] = None,
        client_order_id: str,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> OrderResult:
        """Submit an order to IBKR, or return an existing order with
        the same client_order_id.

        See broker_protocol.Broker.place_order docstring for the full
        contract. Key points for this implementation:

          - The IBKR-side identifier for our client_order_id is
            `Order.orderRef`. We set this on the Order object before
            placing; it round-trips through TWS and appears in
            reqAllOpenOrders / reqCompletedOrders / reqExecutions
            responses.

          - Pre-place lookup: we call get_recent_activity() ourselves
            (a method on this same class), search for matching
            orderRef, and return an idempotent rediscovery if found.
            The 48h lookback in get_recent_activity matches the
            operational_pause auto-resume window.

          - Post-place confirmation: we wait up to
            POST_PLACEMENT_CONFIRMATION_WINDOW_SEC for the order to
            appear in IBKR's open-orders table (or transition to
            a terminal state). Failure raises BrokerInconsistency.

          - During the confirmation window we also catch rejection:
            if TWS sets orderStatus to Inactive within the window,
            raise BrokerRejection with whyHeld as the reason.
        """
        self._require_connected()
        assert self._ib is not None

        # ---- Protocol-level argument validation ----
        if quantity <= 0:
            raise ValueError(f"quantity must be positive; got {quantity}")
        if order_type == OrderType.LMT and limit_price is None:
            raise ValueError("limit_price is required for LMT orders")
        if order_type == OrderType.MKT and limit_price is not None:
            raise ValueError("limit_price must be None for MKT orders")
        if contract.broker_impl != BROKER_IMPL_TAG:
            raise BrokerInconsistency(
                f"ContractRef was minted by {contract.broker_impl!r} but "
                f"this broker is {BROKER_IMPL_TAG!r}. Cross-broker contract "
                f"refs are not permitted."
            )

        # ---- Pre-place idempotent rediscovery ----
        # Search for any order with matching orderRef within the
        # 48h lookback window. If found, return its current state
        # without submitting a new order.
        from datetime import timedelta as _td
        now = self._clock_now_utc()
        since = now - _td(hours=POST_PLACEMENT_ACTIVITY_LOOKBACK_HOURS)
        try:
            recent = self.get_recent_activity(since=since)
        except Exception as e:
            # If we can't query, we can't safely place either — bail.
            raise BrokerInconsistency(
                f"Pre-place activity query failed; refusing to place "
                f"order to avoid duplicate-execution risk: {e}"
            ) from e

        existing = self._find_order_by_ref(recent, client_order_id)
        if existing is not None:
            logger.info(
                "place_order: idempotent rediscovery of %s "
                "(status=%s, filled=%s/%s)",
                client_order_id, existing.status,
                existing.filled_quantity, quantity,
            )
            return existing.model_copy(update={"idempotent_rediscovery": True})

        # ---- Construct the ib_async Order ----
        # The Stock contract was either cached in get_positions() / a
        # prior get_prices() call, or we need to qualify it now.
        ib_contract = self._resolve_ib_contract(contract)

        ib_order = self._build_ib_order(
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )

        # ---- Submit ----
        logger.info(
            "place_order: submitting %s %s %s of %s order_type=%s "
            "limit=%s client_order_id=%s",
            side.value, quantity, "shares", contract.symbol,
            order_type.value, limit_price, client_order_id,
        )
        try:
            trade = self._ib.placeOrder(ib_contract, ib_order)
        except Exception as e:
            # Translate to BrokerInconsistency — placeOrder shouldn't
            # raise in normal operation; if it does, we don't know
            # whether the order reached IBKR or not. The next cycle
            # will pick up the orderRef in its pre-place lookup if
            # the order did land.
            raise BrokerInconsistency(
                f"ib.placeOrder() raised unexpectedly. Order may or may "
                f"not have reached IBKR. Next cycle's pre-place lookup "
                f"will detect it if it did: {e}"
            ) from e

        # ---- Post-placement confirmation ----
        # Wait up to POST_PLACEMENT_CONFIRMATION_WINDOW_SEC for the
        # order to appear in a recognized state.
        confirmation_deadline = now + _td(seconds=POST_PLACEMENT_CONFIRMATION_WINDOW_SEC)
        rejected_reason: Optional[str] = None
        confirmed = False

        while self._clock_now_utc() < confirmation_deadline:
            # Let the asyncio loop spin so callbacks deliver.
            try:
                self._ib.sleep(0.1)
            except Exception as e:
                # sleep should not raise; if it does, treat the
                # post-place check as failed.
                logger.warning("ib.sleep raised during confirmation: %s", e)
                break

            status = self._map_ib_status(getattr(trade.orderStatus, "status", ""))

            # Terminal rejection within the window: raise BrokerRejection.
            if status == OrderStatusValue.REJECTED:
                rejected_reason = getattr(trade.orderStatus, "whyHeld", "") \
                                  or "Order moved to Inactive with no whyHeld"
                break

            # Any non-UNKNOWN status means TWS has acknowledged the
            # order (PendingSubmit, PreSubmitted, Submitted, Filled, etc.)
            if status != OrderStatusValue.UNKNOWN:
                confirmed = True
                break

        if rejected_reason is not None:
            logger.error(
                "place_order: REJECTED by broker — client_order_id=%s "
                "reason=%s", client_order_id, rejected_reason,
            )
            raise BrokerRejection(
                f"Order {client_order_id} rejected: {rejected_reason}",
                reason=rejected_reason,
            )

        if not confirmed:
            # The order did not appear in any recognized state within
            # the confirmation window. We don't know what happened to
            # it. Per protocol contract, this is BrokerInconsistency.
            logger.error(
                "place_order: post-placement confirmation timeout for "
                "client_order_id=%s after %.1fs",
                client_order_id, POST_PLACEMENT_CONFIRMATION_WINDOW_SEC,
            )
            raise BrokerInconsistency(
                f"Order {client_order_id} was submitted but did not appear "
                f"in IBKR's open-orders table within "
                f"{POST_PLACEMENT_CONFIRMATION_WINDOW_SEC} seconds. "
                f"The order's true state is unknown. Next cycle's pre-place "
                f"lookup may detect it; operator should also check TWS."
            )

        # ---- Build the OrderResult from the Trade ----
        result = self._trade_to_order_result(trade)
        logger.info(
            "place_order: confirmed at broker — client_order_id=%s "
            "broker_order_id=%s status=%s",
            result.client_order_id, result.broker_order_id, result.status,
        )
        return result

    def get_order_status(self, client_order_id: str) -> OrderStatus:
        """Return the current status of a previously-placed order.

        Looks up the client_order_id in ib_async's cached trades (which
        ib_async keeps in sync via the orderStatusEvent stream). For
        orders placed by THIS client this works; for orders placed by
        a peer box or by a prior cycle attempt that this client never
        saw, we fall back to querying via get_recent_activity().
        """
        self._require_connected()
        assert self._ib is not None

        # First try the cached trades — fast path for orders this
        # client placed during the current connection.
        try:
            trades = self._ib.trades()
        except Exception as e:
            raise BrokerInconsistency(f"ib.trades() raised: {e}") from e

        for trade in trades:
            if getattr(trade.order, "orderRef", "") == client_order_id:
                return self._trade_to_order_status(trade)

        # Fall back to a fresh query via get_recent_activity. This
        # covers orders placed in prior cycles or by the peer box.
        from datetime import timedelta as _td
        since = self._clock_now_utc() - _td(hours=POST_PLACEMENT_ACTIVITY_LOOKBACK_HOURS)
        recent = self.get_recent_activity(since=since)

        for order_result in recent.open_orders + recent.recently_completed_orders:
            if order_result.client_order_id == client_order_id:
                return OrderStatus(
                    client_order_id=order_result.client_order_id,
                    broker_order_id=order_result.broker_order_id,
                    status=order_result.status,
                    filled_quantity=order_result.filled_quantity,
                    remaining_quantity=Decimal("0"),  # not knowable here
                    average_fill_price=order_result.average_fill_price,
                    rejection_reason=order_result.rejection_reason,
                )

        # Not found anywhere — per protocol, return UNKNOWN.
        return OrderStatus(
            client_order_id=client_order_id,
            broker_order_id=None,
            status=OrderStatusValue.UNKNOWN,
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("0"),
        )

    def cancel_order(self, client_order_id: str) -> None:
        """Cancel an open order. Idempotent."""
        self._require_connected()
        assert self._ib is not None

        # Find the order in cached trades.
        try:
            trades = self._ib.trades()
        except Exception as e:
            raise BrokerInconsistency(f"ib.trades() raised: {e}") from e

        for trade in trades:
            if getattr(trade.order, "orderRef", "") == client_order_id:
                status = self._map_ib_status(
                    getattr(trade.orderStatus, "status", "")
                )
                if status in (
                    OrderStatusValue.FILLED,
                    OrderStatusValue.CANCELLED,
                    OrderStatusValue.REJECTED,
                ):
                    # Already terminal; no-op per protocol.
                    return
                try:
                    self._ib.cancelOrder(trade.order)
                except Exception as e:
                    raise BrokerRejection(
                        f"cancelOrder() refused: {e}",
                        reason=str(e),
                    ) from e
                return

        # Order not found in this client's trades. Per protocol, this
        # is a legitimate state (the order may have completed, been
        # cancelled, or never existed under our client). No-op.
        logger.info(
            "cancel_order: no matching order in this client's trades "
            "for client_order_id=%s (idempotent no-op)",
            client_order_id,
        )

    def get_recurring_ach(self) -> RecurringAchInfo:
        """Return the broker-side state of the recurring monthly ACH.

        UNCERTAINTY FLAG: ib_async does not currently expose a clean
        API for reading recurring ACH state. The IBKR portal manages
        the recurring transfer; the TWS API surface for this is
        limited and historically has required custom requests not
        wrapped by ib_async.

        The conservative implementation here returns
        is_configured=False until a working API path is verified
        against paper trading. The action layer (Phase 3) should
        treat is_configured=False as "operator must configure the
        recurring ACH via the broker portal" and emit a Warning
        rather than attempting any ACH update.
        """
        self._require_connected()
        logger.warning(
            "get_recurring_ach: returning is_configured=False — ib_async "
            "does not expose recurring ACH state via a verified API path. "
            "Operator must configure recurring ACH via IBKR portal. See "
            "the UNCERTAINTY FLAG in the implementation."
        )
        return RecurringAchInfo(
            is_configured=False,
            amount_dollars=None,
            destination_reference=None,
            next_scheduled_date=None,
        )

    def update_recurring_ach(
        self, new_amount_dollars: Decimal,
    ) -> AchUpdateResult:
        """Change the recurring monthly ACH amount.

        UNCERTAINTY FLAG: see get_recurring_ach docstring. The current
        IBKR API surface for ACH updates is limited and not well-
        exposed via ib_async. The conservative implementation here
        returns success=False with a rejection_reason that the
        operator must perform the update manually via the IBKR portal.

        The action layer (Phase 3) treats success=False as a Warning,
        does NOT pause the cycle (since the recurring transfer
        continues at the prior amount), and emits an operator alert
        with the new amount the system would have set. The operator
        then performs the update via the portal at their convenience.

        This deliberately-conservative behavior protects against
        accidentally botching the recurring transfer via an unverified
        API path. Once paper-trading verification establishes the
        correct API surface, this implementation will be replaced.
        """
        self._require_connected()
        logger.warning(
            "update_recurring_ach: returning success=False — ib_async "
            "does not expose a verified ACH update API path. Operator "
            "must perform the update via IBKR portal. Requested amount: "
            "$%s.", new_amount_dollars,
        )
        return AchUpdateResult(
            success=False,
            requested_amount_dollars=new_amount_dollars,
            confirmed_amount_dollars=None,
            rejection_reason=(
                "ib_async does not expose a verified API path for "
                "updating the recurring ACH amount. IRAPM is configured "
                "to surface this as a Warning rather than risk an "
                "incorrect update via an unverified API. Operator must "
                "update the recurring transfer to $"
                f"{new_amount_dollars} via the IBKR portal manually."
            ),
        )

    # -------------------------------------------------------------------------
    # Internal helpers for Phase 2c
    # -------------------------------------------------------------------------

    def _find_order_by_ref(
        self,
        recent: RecentActivity,
        client_order_id: str,
    ) -> Optional[OrderResult]:
        """Search RecentActivity for an order with matching orderRef.

        Checks open orders first (most likely to be there for an
        in-flight cycle), then recently-completed (handles the case
        where a previous attempt's order has since filled or been
        cancelled).
        """
        for order in recent.open_orders:
            if order.client_order_id == client_order_id:
                return order
        for order in recent.recently_completed_orders:
            if order.client_order_id == client_order_id:
                return order
        return None

    def _resolve_ib_contract(self, contract: ContractRef):
        """Get the underlying ib_async Contract from a ContractRef.

        Uses the contract cache populated by get_positions() and
        get_prices(). If the contract isn't cached (e.g., a symbol
        we've never traded), qualifies it as a US ETF on the fly.
        """
        cached = self._contract_cache.get(contract.symbol)
        if cached is not None and "ib_contract" in cached.payload:
            return cached.payload["ib_contract"]

        # Not cached. Qualify it as a US ETF.
        assert self._ib is not None
        stock_class = getattr(self, "_stock_class", None)
        if stock_class is None:
            try:
                from ib_async import Stock
            except ImportError as e:
                raise BrokerUnreachable(
                    f"ib_async Stock import failed: {e}"
                ) from e
            stock_class = Stock

        ib_contract = stock_class(contract.symbol, "SMART", "USD")
        try:
            qualified = self._ib.qualifyContracts(ib_contract)
        except Exception as e:
            raise BrokerInconsistency(
                f"qualifyContracts() failed for {contract.symbol}: {e}"
            ) from e
        if not qualified or not getattr(qualified[0], "conId", 0):
            raise BrokerInconsistency(
                f"Could not qualify contract for {contract.symbol!r} "
                f"(no conId returned). Verify the symbol is a valid "
                f"US-listed ETF with active trading."
            )
        self._cache_contract(contract.symbol, qualified[0])
        return qualified[0]

    def _build_ib_order(
        self,
        *,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType,
        limit_price: Optional[Decimal],
        time_in_force: TimeInForce,
        client_order_id: str,
    ):
        """Construct an ib_async Order with our client_order_id stamped
        as the orderRef.

        Uses MarketOrder/LimitOrder convenience classes from ib_async
        rather than raw Order — these set the right orderType field
        for us.
        """
        order_class = getattr(self, "_order_class_factory", None)
        if order_class is None:
            try:
                from ib_async import MarketOrder, LimitOrder
            except ImportError as e:
                raise BrokerUnreachable(
                    f"ib_async order classes import failed: {e}"
                ) from e
            order_class = {
                OrderType.MKT: MarketOrder,
                OrderType.LMT: LimitOrder,
            }
        elif callable(order_class):
            # Test injection point: a factory taking (order_type) and
            # returning a class.
            order_class = {
                OrderType.MKT: order_class(OrderType.MKT),
                OrderType.LMT: order_class(OrderType.LMT),
            }

        action = "BUY" if side == OrderSide.BUY else "SELL"
        # Decimal → float for ib_async's totalQuantity field. Going
        # via str() to preserve representation (avoids float-artifact
        # issues for typical IRAPM quantities like 143.250).
        qty_float = float(str(quantity))

        if order_type == OrderType.MKT:
            order = order_class[OrderType.MKT](action, qty_float)
        else:
            # LMT
            assert limit_price is not None
            lmt_float = float(str(limit_price))
            order = order_class[OrderType.LMT](action, qty_float, lmt_float)

        # Stamp the client_order_id as orderRef. This is the key field
        # for idempotent rediscovery.
        order.orderRef = client_order_id

        # TimeInForce
        order.tif = "DAY" if time_in_force == TimeInForce.DAY else "GTC"

        return order

    def _trade_to_order_status(self, trade) -> OrderStatus:
        """Convert an ib_async Trade to our protocol OrderStatus.

        Distinct from _trade_to_order_result (which produces a fuller
        OrderResult); this is the lighter-weight status snapshot.
        """
        os = trade.orderStatus
        status = self._map_ib_status(getattr(os, "status", ""))
        filled = _to_decimal(getattr(os, "filled", 0))
        remaining = _to_decimal(getattr(os, "remaining", 0))
        avg_price_raw = getattr(os, "avgFillPrice", 0)
        avg_price = _to_decimal(avg_price_raw) if avg_price_raw else None

        rejection_reason: Optional[str] = None
        if status == OrderStatusValue.REJECTED:
            rejection_reason = getattr(os, "whyHeld", "") or None

        return OrderStatus(
            client_order_id=getattr(trade.order, "orderRef", "") or "",
            broker_order_id=str(getattr(trade.order, "orderId", 0)),
            status=status,
            filled_quantity=filled,
            remaining_quantity=remaining,
            average_fill_price=avg_price,
            rejection_reason=rejection_reason,
        )


__all__ = [
    "IBKRBroker",
    "BROKER_IMPL_TAG",
    "KNOWN_TWS_PORTS",
    "CONNECT_TIMEOUT_DEFAULT_SEC",
]
