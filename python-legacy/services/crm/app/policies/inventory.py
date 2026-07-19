"""Inventory policies for MSISDN and eSIM resources."""

from app.policies.base import PolicyViolation, policy

# v0.17 — terminal status set on port-out approve. Never re-issue.
_MSISDN_TERMINAL_STATES = {"ported_out"}


@policy("msisdn.reserve.status_must_be_available")
def check_msisdn_available(status: str, msisdn: str) -> None:
    if status != "available":
        raise PolicyViolation(
            rule="msisdn.reserve.status_must_be_available",
            message=f"MSISDN {msisdn} is not available (status={status})",
            context={"msisdn": msisdn, "status": status},
        )


@policy("msisdn.release.only_if_reserved_or_assigned")
def check_msisdn_releasable(status: str, msisdn: str) -> None:
    """Release sends an MSISDN back to ``available`` — only valid from
    ``reserved``/``assigned``. ``ported_out`` is terminal and must never
    flip back; doctrine v0.17+.
    """
    if status in _MSISDN_TERMINAL_STATES:
        raise PolicyViolation(
            rule="msisdn.release.terminal_status",
            message=(
                f"MSISDN {msisdn} is in terminal status '{status}' and "
                "cannot be released back to the pool"
            ),
            context={"msisdn": msisdn, "status": status},
        )
    if status not in ("reserved", "assigned"):
        raise PolicyViolation(
            rule="msisdn.release.only_if_reserved_or_assigned",
            message=f"MSISDN {msisdn} cannot be released (status={status})",
            context={"msisdn": msisdn, "status": status},
        )


@policy("msisdn.add_range.sane_prefix")
def check_sane_prefix(prefix: str, count: int) -> None:
    """Reject obvious garbage at the edge.

    Prefix must be 4–7 digits (Singapore-style 8-digit MSISDNs reserved
    bucket); count must be 1..10000 to keep a single bulk insert
    bounded. The numeric-prefix-plus-zero-padded suffix is what
    ``InventoryService.add_msisdn_range`` materializes.
    """
    if not prefix.isdigit():
        raise PolicyViolation(
            rule="msisdn.add_range.sane_prefix",
            message=f"Prefix '{prefix}' must be all digits",
            context={"prefix": prefix},
        )
    if not (4 <= len(prefix) <= 7):
        raise PolicyViolation(
            rule="msisdn.add_range.sane_prefix",
            message=f"Prefix '{prefix}' must be 4–7 digits long",
            context={"prefix": prefix, "length": len(prefix)},
        )
    if not (1 <= count <= 10000):
        raise PolicyViolation(
            rule="msisdn.add_range.sane_prefix",
            message=f"Count {count} must be in [1, 10000]",
            context={"count": count},
        )


@policy("esim.reserve.status_must_be_available")
def check_esim_available(status: str, iccid: str) -> None:
    if status != "available":
        raise PolicyViolation(
            rule="esim.reserve.status_must_be_available",
            message=f"eSIM {iccid} is not available (status={status})",
            context={"iccid": iccid, "status": status},
        )


@policy("esim.release.only_if_reserved_or_assigned")
def check_esim_releasable(status: str, iccid: str) -> None:
    if status not in ("reserved", "assigned"):
        raise PolicyViolation(
            rule="esim.release.only_if_reserved_or_assigned",
            message=f"eSIM {iccid} cannot be released (status={status})",
            context={"iccid": iccid, "status": status},
        )


@policy("esim.assign_msisdn.msisdn_must_be_reserved")
def check_msisdn_reserved_for_assign(msisdn_status: str, msisdn: str) -> None:
    if msisdn_status not in ("reserved", "assigned"):
        raise PolicyViolation(
            rule="esim.assign_msisdn.msisdn_must_be_reserved",
            message=f"MSISDN {msisdn} must be reserved or assigned before binding to eSIM",
            context={"msisdn": msisdn, "status": msisdn_status},
        )
