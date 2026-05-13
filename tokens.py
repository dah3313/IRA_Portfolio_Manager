"""
tokens.py — Hardware token detection (§10).

PURPOSE:
    Enumerate USB tokens by type, returning counts of inserted Phase 3
    tokens and STOP INCOME tokens. The daily-token cycle uses this to
    update the per-box token observation file (§10.3); the weekly cycle
    uses the combined observation to drive the Phase machine (§4) and
    the Income state machine (§6.2).

DESIGN:
    - `TokenDetector` is a Protocol. Production deployments inject
      `LinuxUSBTokenDetector` (uses pyudev on Ubuntu 24.04). Simulation
      and tests inject `MockTokenDetector` which returns pre-set counts.
    - Detection identifies tokens by type+presence per §10.1 — vendor
      ID, product ID, capacity, filesystem label, or similar properties
      detectable without inspecting serial numbers.
    - The detector returns a TokenObservation with a status enum so
      "could not enumerate" is distinguishable from "zero tokens
      inserted" (§10.4 UNAVAILABLE state).

PRODUCTION: TokenDetector implementations are responsible for the
actual USB enumeration logic. The TokenConfig provides the matching
criteria (USB VID:PID pairs or label patterns) the operator records
when provisioning the physical tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Protocol, runtime_checkable


# --- Token type catalog ----------------------------------------------------

class TokenType(str, Enum):
    """The two token types per §10.2."""
    PHASE3 = "phase3"
    STOPINCOME = "stopincome"


class ObservationStatus(str, Enum):
    """Outcome of a daily detection cycle."""
    OK = "ok"
    """USB enumeration succeeded; counts are reliable."""

    UNAVAILABLE = "unavailable"
    """Could not enumerate (USB subsystem failure, permission denied,
    pyudev error). The previous values must hold (§10.4) — the calling
    cycle does not overwrite known-good counts with UNAVAILABLE."""


# --- Configuration ---------------------------------------------------------

@dataclass(frozen=True)
class TokenMatchRule:
    """One rule for identifying tokens of a given type.

    Production implementations check the USB device against these
    criteria; any non-empty field is required to match.

    `vendor_id` / `product_id` are hex strings (e.g., '0951', '1666').
    `vendor_name` and `product_name` are case-insensitive substring matches.
    `label` is the filesystem label (case-insensitive exact match).
    `capacity_bytes_min` and `capacity_bytes_max` bound the storage size.

    The MockTokenDetector ignores the rule contents and just returns
    pre-set counts.
    """
    token_type: TokenType
    vendor_id: str | None = None
    product_id: str | None = None
    vendor_name: str | None = None
    product_name: str | None = None
    label: str | None = None
    capacity_bytes_min: int | None = None
    capacity_bytes_max: int | None = None


@dataclass(frozen=True)
class TokenConfig:
    """Box-local token configuration. Each box has its own; the rules
    may differ across boxes if the operator uses different physical
    token brands on each.

    `phase3_rules` and `stopincome_rules` are evaluated independently;
    a single physical device matches at most one rule (the first rule
    whose criteria all pass).
    """
    phase3_rules: tuple[TokenMatchRule, ...]
    stopincome_rules: tuple[TokenMatchRule, ...]


# --- Observation shape -----------------------------------------------------

@dataclass(frozen=True)
class TokenObservation:
    """One detection result per §10.3 / §6.7 token observation file.

    `phase3_count`: number of Phase 3 tokens currently inserted in this
                    box (expected: 2 during Phases 1/2; 0 during Phase 3
                    activation/post-latch).
    `stopincome_count`: number of STOP INCOME tokens currently inserted
                        in this box (expected: 0 normally; 1 when paused).
    `status`: OK on successful enumeration, UNAVAILABLE on failure.
    """
    box_id: str
    timestamp: datetime
    phase3_count: int
    stopincome_count: int
    status: ObservationStatus
    error: str | None = None  # populated only when status == UNAVAILABLE


# --- Detector protocol -----------------------------------------------------

@runtime_checkable
class TokenDetector(Protocol):
    """Interface every token-detection backend satisfies."""

    def detect(self, box_id: str, now: datetime) -> TokenObservation:
        """Enumerate USB devices and return counts.

        Implementations must not raise on transient enumeration failures;
        return TokenObservation with status=UNAVAILABLE and an error
        string instead. This keeps the daily-token cycle from aborting
        on a USB hiccup (§10.4 design).
        """
        ...


# --- Mock detector (sim/dev) -----------------------------------------------

class MockTokenDetector:
    """Returns pre-configured counts. Used by IPMS and unit tests.

    The test can set the next observation via `set_counts(phase3, stop)`
    or arm an UNAVAILABLE response via `arm_unavailable(error_msg)`.
    """

    def __init__(self,
                 phase3_count: int = 2,
                 stopincome_count: int = 0) -> None:
        self._phase3 = phase3_count
        self._stopincome = stopincome_count
        self._armed_unavailable: str | None = None

    # Test/sim controls
    def set_counts(self, *, phase3: int, stopincome: int) -> None:
        self._phase3 = phase3
        self._stopincome = stopincome
        self._armed_unavailable = None

    def arm_unavailable(self, error_msg: str = "mock unavailable") -> None:
        self._armed_unavailable = error_msg

    # Protocol method
    def detect(self, box_id: str, now: datetime) -> TokenObservation:
        if self._armed_unavailable is not None:
            err = self._armed_unavailable
            self._armed_unavailable = None  # one-shot
            return TokenObservation(
                box_id=box_id,
                timestamp=now,
                phase3_count=0,
                stopincome_count=0,
                status=ObservationStatus.UNAVAILABLE,
                error=err,
            )
        return TokenObservation(
            box_id=box_id,
            timestamp=now,
            phase3_count=self._phase3,
            stopincome_count=self._stopincome,
            status=ObservationStatus.OK,
        )


# --- Linux USB detector (production, Ubuntu 24.04) -------------------------

class LinuxUSBTokenDetector:
    """Production detector for Ubuntu 24.04 using pyudev.

    DEPLOYMENT NOTE:
        pyudev must be installed (`pip install pyudev`). The system user
        running IRAPM needs read access to /dev/disk/by-id/ and the
        udev sysfs entries — typically achieved by adding the user to
        the `plugdev` group. The operator records device match criteria
        for their specific token brands in `box.yaml` (§15.8); those
        criteria are loaded into TokenConfig at startup.

    The implementation deliberately does no caching: each call to
    `detect()` re-enumerates from sysfs, so a token unplugged between
    calls produces an updated count immediately.
    """

    def __init__(self, config: TokenConfig) -> None:
        self._config = config
        self._pyudev = None  # lazy-loaded to avoid import-time failure on dev boxes

    def _import_pyudev(self):
        """Import pyudev on first use. Raises ImportError with a
        deployment-actionable message if pyudev is not installed."""
        if self._pyudev is not None:
            return self._pyudev
        try:
            import pyudev  # type: ignore
        except ImportError as e:
            raise ImportError(
                "pyudev is not installed. Run `pip install pyudev` on the "
                "deployment box. See §15.9 of IRAPM_SPECIFICATION.md."
            ) from e
        self._pyudev = pyudev
        return pyudev

    def _device_matches_rule(self, device, rule: TokenMatchRule) -> bool:
        """Apply a TokenMatchRule against one pyudev device. Returns
        True iff every non-None field in the rule matches the device's
        properties."""
        # USB IDs are attributes on the parent USB device; the
        # block-device child carries them via the inherited attrs.
        get = device.get
        vid = get("ID_VENDOR_ID") or ""
        pid = get("ID_MODEL_ID") or ""
        vendor_name = get("ID_VENDOR") or ""
        product_name = get("ID_MODEL") or ""
        label = get("ID_FS_LABEL") or ""

        if rule.vendor_id and rule.vendor_id.lower() != vid.lower():
            return False
        if rule.product_id and rule.product_id.lower() != pid.lower():
            return False
        if rule.vendor_name and rule.vendor_name.lower() not in vendor_name.lower():
            return False
        if rule.product_name and rule.product_name.lower() not in product_name.lower():
            return False
        if rule.label and rule.label.lower() != label.lower():
            return False
        if rule.capacity_bytes_min is not None or rule.capacity_bytes_max is not None:
            try:
                # pyudev: device.attributes['size'] gives 512-byte sectors
                size_sectors = int(device.attributes.asstring("size").strip())
                size_bytes = size_sectors * 512
            except (KeyError, ValueError, AttributeError):
                return False
            if rule.capacity_bytes_min is not None and size_bytes < rule.capacity_bytes_min:
                return False
            if rule.capacity_bytes_max is not None and size_bytes > rule.capacity_bytes_max:
                return False
        return True

    def _classify(self, device) -> TokenType | None:
        """Return which token type a device matches (or None)."""
        for rule in self._config.phase3_rules:
            if self._device_matches_rule(device, rule):
                return TokenType.PHASE3
        for rule in self._config.stopincome_rules:
            if self._device_matches_rule(device, rule):
                return TokenType.STOPINCOME
        return None

    def detect(self, box_id: str, now: datetime) -> TokenObservation:
        try:
            pyudev = self._import_pyudev()
            context = pyudev.Context()
            # Iterate block partitions (the filesystem label lives at
            # the partition level for FAT/exFAT-formatted tokens).
            # USB sticks present as 'block' subsystem devices with
            # DEVTYPE in {'disk', 'partition'}.
            phase3 = 0
            stopincome = 0
            seen_token_ids: set[str] = set()  # dedupe disk+partition double-count
            for device in context.list_devices(subsystem="block"):
                if device.get("ID_BUS") != "usb":
                    continue
                # Unique identity for dedup: USB VID:PID:serial preferred,
                # fallback to devpath.
                key = (
                    device.get("ID_USB_SERIAL")
                    or device.get("ID_SERIAL")
                    or device.device_path
                )
                if key in seen_token_ids:
                    continue
                kind = self._classify(device)
                if kind is None:
                    continue
                seen_token_ids.add(key)
                if kind == TokenType.PHASE3:
                    phase3 += 1
                else:
                    stopincome += 1

            return TokenObservation(
                box_id=box_id,
                timestamp=now,
                phase3_count=phase3,
                stopincome_count=stopincome,
                status=ObservationStatus.OK,
            )
        except Exception as e:  # noqa: BLE001 — any failure is UNAVAILABLE
            return TokenObservation(
                box_id=box_id,
                timestamp=now,
                phase3_count=0,
                stopincome_count=0,
                status=ObservationStatus.UNAVAILABLE,
                error=f"{type(e).__name__}: {e}",
            )


# --- AND-semantics across boxes --------------------------------------------

@dataclass(frozen=True)
class CombinedTokenState:
    """The system-level token state derived from per-box observations.

    Per §10 / §6.2:
      - Phase 3 trigger fires when ALL Phase 3 tokens are removed on
        BOTH boxes (phase3_count == 0 on both).
      - STOP INCOME pause fires when ≥ stopincome_token_count_required
        STOP INCOME tokens are inserted on BOTH boxes.
      - On any UNAVAILABLE observation or mismatch between boxes, the
        previous state holds (§6.2, §10.5).
    """
    phase3_all_removed: bool
    """True iff both boxes report phase3_count == 0."""

    stopincome_active_on_both: bool
    """True iff both boxes report stopincome_count >= configured threshold."""

    any_unavailable: bool
    """True iff either observation has status=UNAVAILABLE."""

    counts_match: bool
    """True iff the two boxes' counts agree on both token types."""


def combine_observations(
    box_a: TokenObservation,
    box_b: TokenObservation,
    *,
    stopincome_token_count_required: int,
) -> CombinedTokenState:
    """Apply AND semantics across two box observations per §10."""
    any_unavail = (box_a.status != ObservationStatus.OK
                   or box_b.status != ObservationStatus.OK)
    counts_match = (box_a.phase3_count == box_b.phase3_count
                    and box_a.stopincome_count == box_b.stopincome_count)
    phase3_all_removed = (
        not any_unavail
        and box_a.phase3_count == 0
        and box_b.phase3_count == 0
    )
    stopincome_active = (
        not any_unavail
        and box_a.stopincome_count >= stopincome_token_count_required
        and box_b.stopincome_count >= stopincome_token_count_required
    )
    return CombinedTokenState(
        phase3_all_removed=phase3_all_removed,
        stopincome_active_on_both=stopincome_active,
        any_unavailable=any_unavail,
        counts_match=counts_match,
    )


__all__ = [
    "TokenType",
    "ObservationStatus",
    "TokenMatchRule",
    "TokenConfig",
    "TokenObservation",
    "TokenDetector",
    "MockTokenDetector",
    "LinuxUSBTokenDetector",
    "CombinedTokenState",
    "combine_observations",
]
