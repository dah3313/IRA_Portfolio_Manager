"""
constants.py — IRA PM domain constants the simulator needs at formatter time.

Public exports:
    PER_POSITION_RESIDUAL_USD — cascade halt floor per position
    CASH_BUFFER_PHASE_1_USD — Phase 1 cash buffer target
    CASH_BUFFER_PHASE_2_USD — Phase 2 cash buffer target
    SGOV_BUFFER_TARGET_USD — SGOV buffer dollar target
    SGOV_BUFFER_TOLERANCE_USD — refill-due deficit tolerance

See also: IPMS_SPECIFICATION.md §4.3 (cascade-depth tracking),
    IBKR_PM_CB_Final_Calibration.md operator clarification
    §3.4.10.1 (residuals are dollar-only)


WHY THIS FILE EXISTS

The IPMS does not own these values; the IRA PM does. Production code
will read them from the IRA PM's ruleset.yaml at runtime. But the
simulator's output formatters need to know them at formatter time
to compute derived columns:

    - cascade_log.md per-tick rows need `distance_to_residual` for
      every position, which is `market_value - PER_POSITION_RESIDUAL_USD`.
    - cascade_log.md needs `at_residual` flags, which fire when
      `market_value <= PER_POSITION_RESIDUAL_USD`.
    - balances_monthly.md sgov_buffer column makes more sense when
      operators can compare it to the canonical SGOV target.

These constants mirror the IPMV1 ruleset values as of 1.6.1 and the
operator-confirmed design intent from the audit (per_position_residual_usd
is canonical; bucket-level percentages were never intended).

When the IRA PM lands, this file's role shifts: instead of carrying
hardcoded constants, it imports them from the IRA PM's ruleset
loader. The interface (the constant names) stays stable so formatter
code is unchanged. Until then, hardcoded values are the simulator's
only source of truth for these.

DRIFT RISK

If the IRA PM's eventual ruleset uses different values for these
constants (e.g., the operator decides to raise the residual to
$2,000), this file MUST be updated to match or formatters will
mislabel `at_residual` rows. A regression test in checkpoint 6 will
verify these values match the canonical IRA PM ruleset once it
exists.
"""

from __future__ import annotations

from decimal import Decimal


# ============================================================================
# CASCADE RESIDUAL
# ============================================================================
# Per-position dollar floor. Withdrawals halt for a position once its
# market_value drops to this level; the cascade walks to the next tier.
# Operator-confirmed (audit §3.4.10.1) as the canonical and original
# design — bucket-level percentages were never intended.
# ============================================================================

PER_POSITION_RESIDUAL_USD: Decimal = Decimal("1500.00")


# ============================================================================
# CASH BUFFER
# ============================================================================
# Phase-aware cash buffer. Phase 1 holds $4k to allow IBKR auto-ACH
# to fire one more time even if the manager process dies. Phase 2
# holds only commission cushion since there are no withdrawals.
# ============================================================================

CASH_BUFFER_PHASE_1_USD: Decimal = Decimal("4000.00")
CASH_BUFFER_PHASE_2_USD: Decimal = Decimal("1000.00")


# ============================================================================
# SGOV BUFFER
# ============================================================================
# Dollar target for the withdrawal buffer. Sized at 24 months of the
# initial $3,000/month withdrawal target. NOT scaled with subsequent
# CPI raises — the operator can re-tune via parameter override if
# they want a larger buffer at higher target rates.
#
# Tolerance defines the "buffer below target" threshold for refill
# dispatch. A buffer at $71,950 against target $72,000 with $250
# tolerance is NOT considered refill-due; the deficit must exceed
# the tolerance for refill to fire.
# ============================================================================

SGOV_BUFFER_TARGET_USD: Decimal = Decimal("72000.00")
SGOV_BUFFER_TOLERANCE_USD: Decimal = Decimal("250.00")
