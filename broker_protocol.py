"""
IRAPM Broker Layer — Protocol definition and typed exceptions.

This module defines the `Broker` Protocol: the contract the action layer
talks to and the contract that concrete broker implementations
(IBKRBroker, SyntheticBroker) must satisfy. Use of PEP 544 Protocol
means implementations need not subclass anything — they just need to
provide the named methods with compatible signatures.

The data types crossing this boundary are in broker_types.py. Do not
add IBKR-specific or ib-async-specific types to method signatures here;
the whole point of the protocol is to keep those concepts on one side
of the boundary.

USAGE PATTERN

  Every cycle interacts with the broker via this sequence:

      with broker_session(broker) as ready_broker:
          positions    = ready_broker.get_positions()
          summary      = ready_broker.get_account_summary()
          recent       = ready_broker.get_recent_activity(since=...)

          # ... cycle logic uses positions, summary, recent ...

          for plan_entry in plan.order_entries:
              result = ready_broker.place_order(
                  contract=plan_entry.contract,
                  side=plan_entry.side,
                  quantity=plan_entry.quantity,
                  order_type=plan_entry.order_type,
                  limit_price=plan_entry.limit_price,
                  client_order_id=plan_entry.client_order_id,
              )
              # ... record result, check status ...

  The `broker_session` context manager (see below) handles connect /
  ensure_ready / disconnect-in-finally lifecycle. Cycle code should
  never call connect()/disconnect() directly — go through the context
  manager.

PROTOCOL CONFORMANCE

  Concrete implementations must satisfy every method below. The
  protocol uses `@runtime_checkable` so `isinstance(impl, Broker)`
  checks the method set at runtime (one-time startup verification;
  not used in hot paths).

  Sim-vs-real testing (Phase 1) exercises this contract via a shared
  test suite — the same tests run against IBKRBroker (paper trading)
  and SyntheticBroker, and any divergence is a contract violation in
  one of the implementations.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Iterator, Optional, Protocol, runtime_checkable

from broker_types import (
    AccountSummary,
    AchUpdateResult,
    ContractRef,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Price,
    RecentActivity,
    RecurringAchInfo,
    TimeInForce,
)


# =============================================================================
# Typed exceptions
# =============================================================================
#
# A small, fixed set of exception classes the action layer can pattern-match
# on. Concrete broker implementations translate library-specific exceptions
# (ib_async errors, network errors, etc.) into these. Raw library exceptions
# must never escape the broker module.
#
# The action layer's response to each (per IRAPM spec §11 and D-SPEC-8):
#
#   BrokerNotReady       → cycle aborts at step 1 (input refresh); §11.2.1
#                          broker-connectivity-loss; Critical (self-healed
#                          weird) — no operational_pause (transient by
#                          nature). Next cycle retries.
#
#   BrokerUnreachable    → same as BrokerNotReady (subtype). Distinguished
#                          for log/alert clarity: "could not reach broker"
#                          vs "broker responded but not ready yet".
#
#   BrokerRejection      → broker accepted the connection and processed the
#                          request, but rejected the operation (order
#                          rejected, ACH update refused, etc.). §11.2.2
#                          handles this — Critical (self-healed weird),
#                          operational_pause with pause_reason='order_rejection';
#                          48h auto-resume.
#
#   BrokerInconsistency  → broker returned data that violates an invariant
#                          we depend on. The action layer's response depends
#                          on the sub-case (§11.2.14):
#                            - Account-ID mismatch at connect, or malformed
#                              Trade data → Critical (hard broke):
#                              operational_pause with pause_reason=
#                              'broker_inconsistency'; no auto-resume.
#                            - Post-placement confirmation timeout, or
#                              pre-place query failure → Critical (self-
#                              healed weird): alert only, NO operational_pause.
#                              The next cycle's fresh broker query resolves
#                              the ambiguity via the established idempotency
#                              mechanism.
#                          The broker module raises the same exception in
#                          all cases; the action layer's exception handler
#                          inspects the BrokerInconsistency's cause field
#                          (or equivalent metadata) to choose the response.
# =============================================================================


class BrokerError(Exception):
    """Base for all broker-layer exceptions. The action layer should NOT
    catch this directly; catch the specific subclasses below so the
    response can be tailored per category."""


class BrokerNotReady(BrokerError):
    """Broker connection is not in a usable state.

    Raised when:
      - A method is called before connect()
      - connect() timed out before snapshots arrived
      - The connection has been administratively closed but the broker
        hasn't been re-connected yet
    """


class BrokerUnreachable(BrokerNotReady):
    """Broker is unreachable at the network/transport level.

    Subtype of BrokerNotReady — caller may treat identically; the
    distinction exists for alert/log clarity. Raised when:
      - TCP connection to TWS/Gateway refused or timed out
      - DNS resolution failure for the broker host
      - Authentication failed at the API layer
    """


class BrokerRejection(BrokerError):
    """Broker received the request and explicitly refused it.

    Distinct from BrokerNotReady: the broker is healthy and responding,
    but it declined to do what we asked. Includes a human-readable
    `reason` attribute carrying the broker's rejection message.
    """

    def __init__(self, message: str, reason: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason = reason


class BrokerInconsistency(BrokerError):
    """Broker returned data that violates an invariant we depend on.

    Examples that raise this:
      - Connected account ID doesn't match the configured expected
        account ID at startup (hard-broke sub-case)
      - A returned Trade carries malformed numeric values, an
        impossible execId collision, or wrong client_id attribution
        (hard-broke sub-case)
      - place_order() succeeded but the order doesn't appear in the
        open-orders table within 15 seconds — the post-placement
        confirmation check (self-healed-weird sub-case)
      - get_recent_activity() raised, leaving the idempotency lookup
        blind; the cycle refused to place (self-healed-weird sub-case)

    The action layer's response depends on the sub-case per §11.2.14
    and D-SPEC-8: hard-broke sub-cases set operational_pause with
    pause_reason='broker_inconsistency' and no auto-resume;
    self-healed-weird sub-cases alert only with no pause, since the
    next cycle's fresh broker query resolves the ambiguity via the
    established idempotency mechanism. The exception itself does not
    encode the distinction — the action layer's handler inspects the
    BrokerInconsistency's cause / context to classify.
    """


# =============================================================================
# Broker Protocol
# =============================================================================

@runtime_checkable
class Broker(Protocol):
    """The interface the action layer (and decision layer, for state
    queries) talks to. Concrete implementations: IBKRBroker (production
    against IBKR TWS/Gateway via ib-async) and SyntheticBroker (in
    IPMS, for backtests and tests).

    Every method below is synchronous from the caller's view, returns
    plain data types from broker_types.py, and raises only the four
    typed exceptions above. No callbacks, no event handlers, no
    async/await visible to the caller.

    METHOD GROUPS (referenced in IRAPM spec §9.5 — TBD update):
      Group 1: connection lifecycle (3 methods)
      Group 2: state queries (4 methods)
      Group 3: order placement (3 methods)
      Group 4: ACH (2 methods)
      Group 5: miscellaneous (2 methods)

    Total surface: 14 methods. (Slightly over the "13 methods" target
    in earlier design notes — added `get_recurring_ach()` for read-side
    symmetry with update_recurring_ach.)
    """

    # -------------------------------------------------------------------------
    # Group 1: connection lifecycle
    # -------------------------------------------------------------------------

    def connect(self, *, timeout_sec: float = 30.0) -> None:
        """Establish a fresh connection and wait for full readiness.

        After this returns successfully:
          - TCP connection established
          - API handshake complete
          - Initial position snapshot received and cached
          - Initial open-orders snapshot received and cached
          - Initial account-summary snapshot received and cached

        Concretely: all the data needed to answer get_positions(),
        get_account_summary(), and get_recent_activity() for the
        *initial* call is available. Subsequent calls during this
        connection may re-query the broker for fresher data.

        Per the per-cycle connection lifecycle, this is called at
        cycle start. Idempotent and self-healing: calling connect()
        when already connected is a no-op IF the underlying transport
        is still alive. If the implementation detects the transport
        has been lost since the last successful connect (e.g., TWS
        restart between cycles), it performs a real reconnect rather
        than returning early. This aligns connect()'s view of
        readiness with is_ready()'s and supports a fail-safe operating
        posture: operators (and the cycle launcher) calling connect()
        on a stale broker get an actual fresh connection.

        Raises:
          BrokerUnreachable: TCP/DNS/auth failure within timeout_sec.
          BrokerNotReady: handshake completed but snapshots didn't
            arrive within timeout_sec.
          BrokerInconsistency: connected to broker but the account ID
            doesn't match configured expectations.
        """
        ...

    def disconnect(self) -> None:
        """Close the broker connection cleanly.

        Idempotent: safe to call when not connected (no-op). Designed
        to be called in a finally-block — never raises.

        Implementations should NOT attempt to cancel orders or do
        cleanup work here; the connection close is purely a
        network-level operation. Order state at the broker is
        unaffected.
        """
        ...

    def is_ready(self) -> bool:
        """True if the broker is fully ready to answer queries.

        For operator diagnostics, NOT for use as a precondition in
        cycle code. Cycle code should call connect() and rely on its
        success/failure; calling is_ready() in a hot path is a
        time-of-check-to-time-of-use bug waiting to happen.
        """
        ...

    # -------------------------------------------------------------------------
    # Group 2: state queries
    # -------------------------------------------------------------------------

    def get_positions(self) -> list[Position]:
        """Return current positions in the managed account.

        Excludes zero-quantity positions. Includes all symbols the
        broker reports, including any not in IRAPM's allowlist —
        those become I3 invariant test failures at the decision-layer
        boundary (the broker is just reporting truth).

        Raises:
          BrokerNotReady: connect() not yet called or connection lost.
        """
        ...

    def get_prices(self, symbols: list[str]) -> dict[str, Price]:
        """Return current prices for the named symbols.

        For each symbol, returns a Price with status=OK and a valid
        `price` and `as_of`, or status=UNAVAILABLE if no price could
        be obtained (market closed for new symbols, no feed
        subscription, etc.).

        Implementations should request snapshot-mode quotes (not
        streaming) — IRAPM is batch and doesn't need real-time updates.

        Raises:
          BrokerNotReady: connect() not yet called or connection lost.
        """
        ...

    def get_account_summary(self) -> AccountSummary:
        """Return cash, settled cash, unsettled cash, and buying power.

        See AccountSummary docstring for field semantics.

        Raises:
          BrokerNotReady: connect() not yet called or connection lost.
          BrokerInconsistency: connected account_id doesn't match
            configured expectation (caught at the implementation layer
            and translated; could also be raised by connect() depending
            on when the check fires).
        """
        ...

    def get_recent_activity(self, *, since: datetime) -> RecentActivity:
        """Return open orders, recently-completed orders, and recent
        fills in a single atomic snapshot.

        This is the split-brain defense queryable (master/slave design
        defense layer 3). Called by the action layer before placing
        any orders to check for conflicting activity (whether from a
        previous cycle attempt, the peer box, or external operator
        activity via the broker portal).

        The `since` parameter bounds the recently-completed-orders
        and recent-fills lists. Open orders are returned regardless of
        age — an order placed yesterday that's still open today
        appears in open_orders.

        UNCERTAINTY FLAG: the IBKRBroker implementation needs paper-
        trading verification of:
          - reqAllOpenOrders behavior across PendingSubmit/PendingCancel
            edge states
          - reqCompletedOrders retention window (TWS docs suggest
            "today and previous day" but real behavior may vary)
          - Whether reqExecutions filters by submission time or fill
            time (we want fill time)

        Raises:
          BrokerNotReady: connect() not yet called or connection lost.
        """
        ...

    # -------------------------------------------------------------------------
    # Group 3: order placement
    # -------------------------------------------------------------------------

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
        """Submit an order to the broker, or return an existing order
        with the same client_order_id.

        IDEMPOTENCY CONTRACT
          Before submitting, the implementation queries the broker for
          any order (open, completed within 48h, or with fills within
          48h) whose broker-side order reference matches
          `client_order_id`. If found, returns OrderResult representing
          the existing order with `idempotent_rediscovery=True`. The
          caller can detect this and decide whether to wait for the
          rediscovered order to complete or treat it as already-handled.

          The caller is responsible for generating `client_order_id`
          deterministically from cycle state (see CycleAttempt in
          cycle_attempt.py for the canonical format). Two place_order
          calls with the same client_order_id are guaranteed to return
          consistent results: either both succeed with the same broker
          order, or both raise the same BrokerRejection.

        POST-PLACEMENT CONFIRMATION
          After submitting (when no existing order was found), the
          implementation waits up to 15 seconds for the order to appear
          in the broker's open-orders table. If the order does not
          appear within that window, raises BrokerInconsistency —
          this catches the rare TWS failure mode where placeOrder
          returns success but the order doesn't reach the exchange.

        SIDE CONVENTIONS
          - quantity is always positive. side determines BUY vs SELL.
          - limit_price required if order_type == LMT, must be None
            otherwise. ValueError raised at the protocol layer for
            mismatches.

        Args:
          contract: Opaque ContractRef from a prior get_positions() or
            equivalent. Must have been minted by THIS broker
            implementation; passing a ref minted elsewhere raises
            BrokerInconsistency.
          side: BUY or SELL.
          quantity: Positive Decimal. Fractional share quantities are
            permitted (IBKR supports fractional ETF shares).
          order_type: MKT or LMT.
          limit_price: Required iff order_type == LMT.
          client_order_id: Caller-supplied deterministic identifier.
            Must be unique across the account's history. Format
            convention: 'cycle-{cycle_uuid}-{plan_entry_index}-
            {symbol}-{side}'. Stored at the broker as orderRef
            (IBKR) or equivalent.
          time_in_force: DAY (default) or GTC. IRAPM cycle logic only
            generates DAY orders.

        Returns:
          OrderResult with status in {PENDING, WORKING, FILLED,
          PARTIAL, CANCELLED, REJECTED}. For idempotent rediscoveries,
          status reflects the order's current state (which may have
          progressed since the original submission).

        Raises:
          ValueError: order_type/limit_price mismatch, negative
            quantity, etc. — protocol-level validation, not a broker
            issue.
          BrokerNotReady: not connected.
          BrokerRejection: broker explicitly rejected the order.
            OrderResult.status would be REJECTED but we raise here so
            the action layer can branch cleanly; the rejection_reason
            is in the exception's `.reason`.
          BrokerInconsistency: order submitted but didn't appear in
            open orders within 15 sec; or other internal-consistency
            violation.
        """
        ...

    def get_order_status(self, client_order_id: str) -> OrderStatus:
        """Return the current status of a previously-placed order.

        Lightweight compared to extracting the order from
        get_recent_activity() — useful for polling during a cycle.

        Args:
          client_order_id: The deterministic identifier supplied to
            place_order().

        Returns:
          OrderStatus with the current state. If no order with this
          client_order_id exists at the broker, returns OrderStatus
          with status=OrderStatusValue.UNKNOWN (NOT an exception —
          the absence of an order is a legitimate query outcome).

        Raises:
          BrokerNotReady: not connected.
        """
        ...

    def cancel_order(self, client_order_id: str) -> None:
        """Cancel an open order.

        Idempotent: cancelling an already-cancelled or filled order
        is a no-op (no exception). If the order doesn't exist at all,
        also a no-op (matches the get_order_status() UNKNOWN
        semantics — absence is a legitimate state).

        Raises:
          BrokerNotReady: not connected.
          BrokerRejection: broker explicitly refused the cancel
            (rare; usually means the order is already in a terminal
            state but the broker reports it inconsistently).
        """
        ...

    # -------------------------------------------------------------------------
    # Group 4: ACH (recurring monthly transfer)
    # -------------------------------------------------------------------------

    def get_recurring_ach(self) -> RecurringAchInfo:
        """Return the broker-side state of the recurring monthly ACH.

        See RecurringAchInfo docstring for field semantics. Used to
        verify that the broker's current recurring amount matches what
        IRAPM's schedule_state expects; mismatches trigger §8.2.7's
        ACH update flow.

        Raises:
          BrokerNotReady: not connected.
        """
        ...

    def update_recurring_ach(self, new_amount_dollars: Decimal) -> AchUpdateResult:
        """Change the recurring monthly ACH amount.

        Per §7.9, the action layer generates ACHScheduleUpdate plan
        entries when Income state transitions or when the schedule's
        annual review changes the monthly withdrawal amount; those
        entries call this method.

        UNCERTAINTY FLAG: see AchUpdateResult docstring for the
        verification work needed before production trust. Until
        verified, the implementation conservatively treats any
        non-success response as failure and returns
        AchUpdateResult(success=False) rather than raising
        BrokerRejection — the action layer escalates via the §8.2.7
        Warning path (system continues at the prior amount).

        Raises:
          BrokerNotReady: not connected.
        """
        ...

    # -------------------------------------------------------------------------
    # Group 5: miscellaneous
    # -------------------------------------------------------------------------

    def get_managed_account_id(self) -> str:
        """Return the broker's identifier for the managed account.

        Used at startup to verify the connection landed on the right
        account. Compared against a configured expected account ID
        (per box, in box-local config — same place as the assigned
        clientId).

        Raises:
          BrokerNotReady: not connected.
        """
        ...

    def get_server_time(self) -> datetime:
        """Return the broker server's current time (UTC).

        INFORMATIONAL ONLY. Used by runbook diagnostics to verify
        clock alignment between IRAPM's box and the broker. Cycle
        decisions use the captured cycle clock (cycle_attempt.py),
        not this value.

        Raises:
          BrokerNotReady: not connected.
        """
        ...

    def resolve_symbol(self, symbol: str) -> ContractRef:
        """Return a ContractRef for `symbol`, suitable for place_order().

        Used by the action layer for first-time BUY orders of symbols
        not currently held in the account (the only documented case is
        the Phase 1→Phase 2 transition's first GBIL purchase — §7.2.1
        — since GBIL doesn't exist in Phase 1). For symbols already
        held, the action layer prefers the ContractRef returned by
        get_positions() (which carries broker-implementation-specific
        payload) and only falls back to resolve_symbol() for the new-
        symbol case.

        Implementations:
          - SyntheticBroker: mints a ContractRef with its BROKER_IMPL_TAG
            and the symbol; no broker round-trip required.
          - IBKRBroker: queries TWS via reqContractDetails to resolve
            the symbol to a conId / exchange / currency triple, populates
            ContractRef.payload accordingly, and caches the result for
            the connection's lifetime. May raise BrokerInconsistency if
            the symbol is ambiguous (multiple matching contracts) or
            BrokerRejection if the broker reports the symbol as unknown.

        Raises:
          BrokerNotReady: not connected.
          BrokerInconsistency: symbol cannot be uniquely resolved (e.g.,
            multiple matching contracts on different exchanges and the
            implementation has no tie-breaking rule).
          BrokerRejection: broker reports symbol unknown.
        """
        ...


# =============================================================================
# Context manager for the per-cycle connection lifecycle
# =============================================================================

@contextmanager
def broker_session(broker: Broker, *, timeout_sec: float = 30.0) -> Iterator[Broker]:
    """Context manager that runs the per-cycle connect/disconnect dance.

    Use:

        with broker_session(broker) as b:
            positions = b.get_positions()
            # ... cycle logic ...

    Connect happens on entry; disconnect happens on exit (including
    exception paths). The yielded object IS the same broker — the
    context manager just bookends the lifecycle.

    Exceptions during connect() propagate to the caller (cycle aborts
    at step 1). Exceptions during cycle work propagate after
    disconnect (cycle aborts at action layer). disconnect() itself
    never raises.

    Why a context manager and not raw connect/disconnect calls in
    cycle code: disconnect-in-finally is mandatory for the per-cycle
    lifecycle, and the context manager enforces it. Cycle code that
    forgets disconnect leaks a TWS connection slot until the cycle
    process exits.
    """
    broker.connect(timeout_sec=timeout_sec)
    try:
        yield broker
    finally:
        broker.disconnect()


__all__ = [
    # Protocol
    "Broker",
    # Context manager
    "broker_session",
    # Exceptions
    "BrokerError",
    "BrokerNotReady",
    "BrokerUnreachable",
    "BrokerRejection",
    "BrokerInconsistency",
]
