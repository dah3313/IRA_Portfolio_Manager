"""Phase 2c smoke tests for IBKRBroker order placement methods.

These tests use a more capable MockIB that simulates the lifecycle
of placeOrder + orderStatus callbacks. The actual ib_async behavior
must still be verified against paper trading before production trust.
"""

import sys
for m in list(sys.modules):
    if 'broker' in m or 'ibkr' in m or 'clock' in m:
        sys.modules.pop(m, None)

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Callable, Optional

from ibkr_broker import IBKRBroker, _to_decimal
from broker_protocol import (
    BrokerNotReady, BrokerInconsistency, BrokerRejection, BrokerUnreachable,
)
from broker_types import (
    PriceStatus, OrderStatusValue, ContractRef,
    OrderSide, OrderType, TimeInForce,
)
from clock import FrozenClock, AdvancingClock


# =============================================================================
# Mock ib_async objects (extended for Phase 2c)
# =============================================================================

@dataclass
class MockContract:
    symbol: str = ''
    secType: str = 'STK'
    exchange: str = 'SMART'
    currency: str = 'USD'
    conId: int = 0

class MockStock(MockContract):
    def __init__(self, symbol, exchange, currency):
        super().__init__(symbol=symbol, exchange=exchange, currency=currency)

@dataclass
class MockOrder:
    """Mimics ib_async.Order. Phase 2c sets orderRef, action, totalQuantity,
    lmtPrice, tif on this."""
    orderId: int = 0
    orderRef: str = ''
    action: str = ''
    totalQuantity: float = 0.0
    orderType: str = ''
    lmtPrice: float = 0.0
    tif: str = 'DAY'

class MockMarketOrder(MockOrder):
    def __init__(self, action: str, totalQuantity: float):
        super().__init__(action=action, totalQuantity=totalQuantity, orderType='MKT')

class MockLimitOrder(MockOrder):
    def __init__(self, action: str, totalQuantity: float, lmtPrice: float):
        super().__init__(action=action, totalQuantity=totalQuantity, lmtPrice=lmtPrice, orderType='LMT')

@dataclass
class MockOrderStatus:
    status: str = ''
    whyHeld: str = ''
    filled: float = 0.0
    remaining: float = 0.0
    avgFillPrice: float = 0.0

@dataclass
class MockExecution:
    execId: str
    shares: float
    price: float
    side: str = 'BOT'

@dataclass
class MockCommissionReport:
    commission: float = 0.0

@dataclass
class MockFill:
    time: datetime
    execution: MockExecution
    commissionReport: MockCommissionReport = field(default_factory=MockCommissionReport)

@dataclass
class MockTradeLogEntry:
    time: datetime
    status: str = ''

@dataclass
class MockTrade:
    contract: MockContract
    order: MockOrder
    orderStatus: MockOrderStatus
    fills: list = field(default_factory=list)
    log: list = field(default_factory=list)


class MockIB:
    """More capable mock for Phase 2c — simulates placeOrder lifecycle."""
    def __init__(self):
        self._positions = []
        self._account_values = []
        self._open_trades = []
        self._completed_trades = []
        self._fills = []
        self._tickers = {}
        self._trades = []  # cached trades for ib.trades()
        self._managed_accounts = ['U1234567']
        self._connected = True
        self._next_order_id = 1000
        # Test hook: function called after placeOrder to simulate
        # IBKR's response. Default: immediate Filled.
        self.place_order_simulator: Callable[[MockTrade], None] = self._default_simulator
        # Test hook: function called repeatedly during sleep() to
        # progress order state (e.g., Submitted → Filled over time).
        self.sleep_simulator: Optional[Callable[[float], None]] = None
        # Counter for sleep() calls (lets tests verify the wait loop ran)
        self.sleep_call_count = 0

    def isConnected(self): return self._connected
    def managedAccounts(self): return self._managed_accounts
    def positions(self): return self._positions
    def accountValues(self, account):
        return [v for v in self._account_values if v.account == account or v.account == '']
    def reqAllOpenOrders(self): return self._open_trades
    def reqCompletedOrders(self, apiOnly): return self._completed_trades
    def reqExecutions(self, execFilter=None): return self._fills
    def qualifyContracts(self, *contracts):
        for c in contracts:
            c.conId = abs(hash(c.symbol)) % 1000000 + 100000
        return list(contracts)
    def reqTickers(self, *contracts, regulatorySnapshot=False):
        return [self._tickers.get(c.symbol) for c in contracts]
    def trades(self): return self._trades
    def disconnect(self): self._connected = False

    def placeOrder(self, contract, order):
        """Mock placeOrder: assign orderId, construct Trade, run simulator."""
        order.orderId = self._next_order_id
        self._next_order_id += 1
        trade = MockTrade(
            contract=contract,
            order=order,
            orderStatus=MockOrderStatus(status=''),  # empty initially
            fills=[],
            log=[MockTradeLogEntry(time=datetime.now(timezone.utc))],
        )
        self._trades.append(trade)
        self._open_trades.append(trade)
        # Run the simulator to set initial state
        self.place_order_simulator(trade)
        return trade

    def sleep(self, secs):
        """Mock sleep: count the call, optionally progress state."""
        self.sleep_call_count += 1
        if self.sleep_simulator is not None:
            self.sleep_simulator(secs)

    def cancelOrder(self, order):
        """Mark the order as Cancelled in our trades."""
        for trade in self._trades:
            if trade.order is order:
                trade.orderStatus.status = 'Cancelled'
                return
        # Not found is silent here; broker.cancel_order's behavior is tested elsewhere

    @staticmethod
    def _default_simulator(trade: MockTrade) -> None:
        """Default: order gets Submitted immediately."""
        trade.orderStatus.status = 'Submitted'


def make_broker_with_mock_ib():
    b = IBKRBroker(host='127.0.0.1', port=7497, client_id=11, expected_account_id='U1234567')
    mock = MockIB()
    b._ib = mock
    b._connected = True
    b._stock_class = MockStock
    # Inject the MarketOrder/LimitOrder classes
    b._order_class_factory = lambda order_type: \
        MockMarketOrder if order_type == OrderType.MKT else MockLimitOrder
    # Use a FrozenClock so confirmation window logic is deterministic
    b._clock = FrozenClock(datetime(2027, 8, 11, 10, 0, 0))
    return b, mock


now = datetime(2027, 8, 11, 10, 0, 0, tzinfo=timezone.utc)
naive_now = now.replace(tzinfo=None)


# =============================================================================
# T1: Happy-path MKT order
# =============================================================================
b, mock = make_broker_with_mock_ib()
contract = ContractRef(broker_impl='IBKRBroker', symbol='SGOV')

result = b.place_order(
    contract=contract,
    side=OrderSide.SELL,
    quantity=Decimal('143.250'),
    order_type=OrderType.MKT,
    client_order_id='cycle-abc-0-SGOV-SELL',
)
assert result.status == OrderStatusValue.WORKING, f"expected WORKING (Submitted), got {result.status}"
assert result.client_order_id == 'cycle-abc-0-SGOV-SELL'
assert not result.idempotent_rediscovery
# Verify the orderRef stamping
assert mock._trades[-1].order.orderRef == 'cycle-abc-0-SGOV-SELL'
# Verify Decimal→float preserved 143.250 → 143.25
assert mock._trades[-1].order.totalQuantity == 143.25
# Verify post-placement confirmation actually waited at least one sleep
assert mock.sleep_call_count >= 1
print('T1 happy-path MKT order ✓')


# =============================================================================
# T2: Happy-path LMT order
# =============================================================================
b, mock = make_broker_with_mock_ib()
result = b.place_order(
    contract=contract,
    side=OrderSide.BUY,
    quantity=Decimal('50'),
    order_type=OrderType.LMT,
    limit_price=Decimal('120.50'),
    client_order_id='cycle-abc-1-SGOV-BUY',
)
assert result.status == OrderStatusValue.WORKING
last_order = mock._trades[-1].order
assert isinstance(last_order, MockLimitOrder)
assert last_order.lmtPrice == 120.50
assert last_order.tif == 'DAY'
print('T2 happy-path LMT order ✓')


# =============================================================================
# T3: Idempotent rediscovery — order with same orderRef already exists
# =============================================================================
b, mock = make_broker_with_mock_ib()
# Pre-seed an open trade with our target orderRef
existing_trade = MockTrade(
    contract=MockContract(symbol='SGOV'),
    order=MockOrder(orderId=9001, orderRef='cycle-xyz-0-SGOV-SELL'),
    orderStatus=MockOrderStatus(status='Submitted'),
    fills=[],
    log=[MockTradeLogEntry(time=now - timedelta(minutes=10))],
)
mock._open_trades = [existing_trade]
mock._trades = [existing_trade]

# Track that placeOrder was NOT called
place_calls_before = mock.sleep_call_count
result = b.place_order(
    contract=contract,
    side=OrderSide.SELL,
    quantity=Decimal('100'),
    order_type=OrderType.MKT,
    client_order_id='cycle-xyz-0-SGOV-SELL',
)
assert result.idempotent_rediscovery, "should have detected existing order"
assert result.client_order_id == 'cycle-xyz-0-SGOV-SELL'
assert result.status == OrderStatusValue.WORKING
# Verify NO new order was placed — original trades list unchanged
assert len(mock._open_trades) == 1
assert mock._open_trades[0] is existing_trade
print('T3 idempotent rediscovery ✓')


# =============================================================================
# T4: Idempotent rediscovery for completed (filled) order
# =============================================================================
b, mock = make_broker_with_mock_ib()
filled_trade = MockTrade(
    contract=MockContract(symbol='SGOV'),
    order=MockOrder(orderId=9002, orderRef='cycle-prev-0-SGOV-SELL'),
    orderStatus=MockOrderStatus(status='Filled'),
    fills=[MockFill(
        time=now - timedelta(hours=2),
        execution=MockExecution(execId='exec-1', shares=10, price=100.50),
        commissionReport=MockCommissionReport(commission=0),
    )],
    log=[MockTradeLogEntry(time=now - timedelta(hours=2, minutes=5))],
)
mock._completed_trades = [filled_trade]
result = b.place_order(
    contract=contract,
    side=OrderSide.SELL,
    quantity=Decimal('10'),
    order_type=OrderType.MKT,
    client_order_id='cycle-prev-0-SGOV-SELL',
)
assert result.idempotent_rediscovery
assert result.status == OrderStatusValue.FILLED
assert result.filled_quantity == Decimal('10')
print('T4 idempotent rediscovery for completed order ✓')


# =============================================================================
# T5: Post-placement BrokerRejection (order goes Inactive within window)
# =============================================================================
b, mock = make_broker_with_mock_ib()

# Simulator: place_order returns trade with empty status; sleep_simulator
# transitions it to 'Inactive' on first sleep.
def reject_on_sleep(secs):
    if mock._trades and not mock._trades[-1].orderStatus.whyHeld:
        mock._trades[-1].orderStatus.status = 'Inactive'
        mock._trades[-1].orderStatus.whyHeld = 'Insufficient buying power'
mock.place_order_simulator = lambda trade: None  # empty status initially
mock.sleep_simulator = reject_on_sleep

try:
    b.place_order(
        contract=contract,
        side=OrderSide.BUY,
        quantity=Decimal('1000000'),
        order_type=OrderType.MKT,
        client_order_id='cycle-reject-0-SGOV-BUY',
    )
    assert False, "should have raised BrokerRejection"
except BrokerRejection as e:
    assert 'Insufficient' in str(e)
    assert e.reason == 'Insufficient buying power'
print('T5 BrokerRejection on Inactive within window ✓')


# =============================================================================
# T6: Post-placement BrokerInconsistency (order never confirms)
# =============================================================================
b, mock = make_broker_with_mock_ib()
# Simulator: order stays with empty status forever
mock.place_order_simulator = lambda trade: None
mock.sleep_simulator = None  # no state progression

# Use an AdvancingClock so the confirmation window can actually expire
adv_clock = AdvancingClock(start=datetime(2027, 8, 11, 10, 0, 0))
b._clock = adv_clock

# Each sleep call should advance the clock by 100ms (matching the
# 0.1 seconds we pass to ib.sleep in production code)
def advance_on_sleep(secs):
    adv_clock.advance(seconds=int(secs) if secs >= 1 else 0,
                      minutes=0)
    # For fractional secs, use a custom advance
    if secs < 1:
        from datetime import timedelta as _td
        adv_clock._dt = adv_clock._dt + _td(seconds=secs)

mock.sleep_simulator = advance_on_sleep

try:
    b.place_order(
        contract=contract,
        side=OrderSide.SELL,
        quantity=Decimal('10'),
        order_type=OrderType.MKT,
        client_order_id='cycle-stuck-0-SGOV-SELL',
    )
    assert False, "should have raised BrokerInconsistency"
except BrokerInconsistency as e:
    assert 'did not appear' in str(e) or 'confirmation' in str(e).lower()
print('T6 BrokerInconsistency on confirmation timeout ✓')


# =============================================================================
# T7: Argument validation — negative quantity
# =============================================================================
b, mock = make_broker_with_mock_ib()
try:
    b.place_order(
        contract=contract, side=OrderSide.SELL, quantity=Decimal('-5'),
        order_type=OrderType.MKT, client_order_id='test',
    )
    assert False
except ValueError as e:
    assert 'positive' in str(e)
print('T7a argument validation: negative qty ✓')

# Missing limit price for LMT
try:
    b.place_order(
        contract=contract, side=OrderSide.SELL, quantity=Decimal('5'),
        order_type=OrderType.LMT, client_order_id='test',
    )
    assert False
except ValueError as e:
    assert 'limit_price' in str(e).lower()
print('T7b argument validation: missing LMT price ✓')

# Limit price on MKT order
try:
    b.place_order(
        contract=contract, side=OrderSide.SELL, quantity=Decimal('5'),
        order_type=OrderType.MKT, limit_price=Decimal('100'),
        client_order_id='test',
    )
    assert False
except ValueError as e:
    assert 'limit_price' in str(e).lower() and 'None' in str(e)
print('T7c argument validation: limit_price on MKT ✓')


# =============================================================================
# T8: Cross-broker ContractRef rejection
# =============================================================================
b, mock = make_broker_with_mock_ib()
wrong_contract = ContractRef(broker_impl='SyntheticBroker', symbol='SGOV')
try:
    b.place_order(
        contract=wrong_contract, side=OrderSide.SELL, quantity=Decimal('10'),
        order_type=OrderType.MKT, client_order_id='test',
    )
    assert False
except BrokerInconsistency as e:
    assert 'SyntheticBroker' in str(e) and 'IBKRBroker' in str(e)
print('T8 cross-broker ContractRef rejection ✓')


# =============================================================================
# T9: get_order_status — found in cached trades
# =============================================================================
b, mock = make_broker_with_mock_ib()
seeded = MockTrade(
    contract=MockContract(symbol='SGOV'),
    order=MockOrder(orderId=5001, orderRef='cycle-x-0-SGOV-SELL'),
    orderStatus=MockOrderStatus(
        status='Submitted', filled=0.0, remaining=10.0, avgFillPrice=0.0,
    ),
    log=[MockTradeLogEntry(time=now - timedelta(minutes=5))],
)
mock._trades = [seeded]
status = b.get_order_status('cycle-x-0-SGOV-SELL')
assert status.status == OrderStatusValue.WORKING
assert status.broker_order_id == '5001'
assert status.filled_quantity == Decimal('0')
assert status.remaining_quantity == Decimal('10')
print('T9 get_order_status from cached trades ✓')


# =============================================================================
# T10: get_order_status — UNKNOWN when not found
# =============================================================================
b, mock = make_broker_with_mock_ib()
status = b.get_order_status('does-not-exist')
assert status.status == OrderStatusValue.UNKNOWN
print('T10 get_order_status UNKNOWN for missing ✓')


# =============================================================================
# T11: get_order_status — fallback to recent activity
# =============================================================================
b, mock = make_broker_with_mock_ib()
# Not in cached trades, but in completed_trades (which feeds recent_activity)
older_trade = MockTrade(
    contract=MockContract(symbol='SGOV'),
    order=MockOrder(orderId=6001, orderRef='cycle-old-0-SGOV-SELL'),
    orderStatus=MockOrderStatus(status='Filled'),
    fills=[MockFill(
        time=now - timedelta(hours=2),
        execution=MockExecution(execId='ex1', shares=10, price=100),
        commissionReport=MockCommissionReport(commission=0),
    )],
    log=[MockTradeLogEntry(time=now - timedelta(hours=2, minutes=5))],
)
mock._completed_trades = [older_trade]
status = b.get_order_status('cycle-old-0-SGOV-SELL')
assert status.status == OrderStatusValue.FILLED
assert status.filled_quantity == Decimal('10')
print('T11 get_order_status falls back to recent activity ✓')


# =============================================================================
# T12: cancel_order — happy path
# =============================================================================
b, mock = make_broker_with_mock_ib()
working = MockTrade(
    contract=MockContract(symbol='SGOV'),
    order=MockOrder(orderId=7001, orderRef='cycle-cancel-0-SGOV-SELL'),
    orderStatus=MockOrderStatus(status='Submitted'),
)
mock._trades = [working]
b.cancel_order('cycle-cancel-0-SGOV-SELL')
assert working.orderStatus.status == 'Cancelled'
print('T12 cancel_order happy path ✓')


# =============================================================================
# T13: cancel_order — already-filled is no-op
# =============================================================================
b, mock = make_broker_with_mock_ib()
filled = MockTrade(
    contract=MockContract(symbol='SGOV'),
    order=MockOrder(orderId=7002, orderRef='cycle-fill-0-SGOV-SELL'),
    orderStatus=MockOrderStatus(status='Filled'),
)
mock._trades = [filled]
b.cancel_order('cycle-fill-0-SGOV-SELL')  # should be no-op (no exception)
assert filled.orderStatus.status == 'Filled'  # unchanged
print('T13 cancel_order on filled is no-op ✓')


# =============================================================================
# T14: cancel_order — non-existent is no-op
# =============================================================================
b, mock = make_broker_with_mock_ib()
b.cancel_order('does-not-exist')  # should be silent no-op
print('T14 cancel_order on non-existent is no-op ✓')


# =============================================================================
# T15: get_recurring_ach returns is_configured=False (uncertainty flag)
# =============================================================================
b, mock = make_broker_with_mock_ib()
ach = b.get_recurring_ach()
assert not ach.is_configured
assert ach.amount_dollars is None
print('T15 get_recurring_ach uncertainty path: is_configured=False ✓')


# =============================================================================
# T16: update_recurring_ach returns success=False with informative reason
# =============================================================================
b, mock = make_broker_with_mock_ib()
result = b.update_recurring_ach(Decimal('3500.00'))
assert not result.success
assert result.requested_amount_dollars == Decimal('3500.00')
assert result.confirmed_amount_dollars is None
assert 'portal' in (result.rejection_reason or '').lower()
print('T16 update_recurring_ach uncertainty path: success=False with portal-instruction ✓')


# =============================================================================
# T17: Decimal precision preserved through float conversion
# =============================================================================
# Critical: 143.250 (typical fractional share qty) must round-trip exactly
b, mock = make_broker_with_mock_ib()
b.place_order(
    contract=contract,
    side=OrderSide.SELL,
    quantity=Decimal('143.250'),
    order_type=OrderType.MKT,
    client_order_id='cycle-prec-0-SGOV-SELL',
)
order_qty = mock._trades[-1].order.totalQuantity
assert order_qty == 143.25, f"precision loss: {order_qty} != 143.25"

# LMT price precision
b2, mock2 = make_broker_with_mock_ib()
b2.place_order(
    contract=contract,
    side=OrderSide.BUY,
    quantity=Decimal('10'),
    order_type=OrderType.LMT,
    limit_price=Decimal('120.05'),
    client_order_id='cycle-prec-1-SGOV-BUY',
)
order_lmt = mock2._trades[-1].order.lmtPrice
assert order_lmt == 120.05, f"precision loss: {order_lmt} != 120.05"
print('T17 Decimal→float precision preserved for IRAPM quantities ✓')


# =============================================================================
# T18: client_order_id stamping in orderRef
# =============================================================================
b, mock = make_broker_with_mock_ib()
b.place_order(
    contract=contract,
    side=OrderSide.SELL,
    quantity=Decimal('10'),
    order_type=OrderType.MKT,
    client_order_id='cycle-550e8400-e29b-41d4-a716-446655440000-0-SGOV-SELL',
)
assert mock._trades[-1].order.orderRef == \
    'cycle-550e8400-e29b-41d4-a716-446655440000-0-SGOV-SELL'
print('T18 client_order_id stamped as orderRef ✓')


# =============================================================================
# T19: Pre-place query failure → BrokerInconsistency
# =============================================================================
b, mock = make_broker_with_mock_ib()
# Make reqAllOpenOrders raise
def boom(*args, **kwargs):
    raise RuntimeError("simulated TWS query failure")
mock.reqAllOpenOrders = boom
try:
    b.place_order(
        contract=contract,
        side=OrderSide.SELL,
        quantity=Decimal('10'),
        order_type=OrderType.MKT,
        client_order_id='cycle-broken-query-0-SGOV-SELL',
    )
    assert False
except BrokerInconsistency as e:
    assert 'activity query' in str(e).lower() or 'reqAllOpenOrders' in str(e)
print('T19 pre-place query failure → BrokerInconsistency (no duplicate risk) ✓')


# =============================================================================
# T20: TIF GTC routes through correctly
# =============================================================================
b, mock = make_broker_with_mock_ib()
b.place_order(
    contract=contract,
    side=OrderSide.SELL,
    quantity=Decimal('10'),
    order_type=OrderType.MKT,
    client_order_id='cycle-gtc-0-SGOV-SELL',
    time_in_force=TimeInForce.GTC,
)
assert mock._trades[-1].order.tif == 'GTC'
print('T20 TimeInForce.GTC routes through ✓')


print()
print('All 20 Phase 2c tests passed.')
