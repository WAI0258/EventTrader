"""Project-owned optional capability discovery for event-trader."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from dataclasses import dataclass
from typing import Final


class CapabilityLookupError(LookupError):
    """Raised when a requested capability key is not part of the catalog."""


class CapabilityDiscoveryError(RuntimeError):
    """Raised when optional capability discovery returns malformed metadata."""


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Project-owned static metadata for an optional upstream capability."""

    key: str
    name: str
    module: str
    purpose: str


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """Project-owned availability state for an optional upstream capability."""

    key: str
    name: str
    module: str
    purpose: str
    available: bool


_CAPABILITY_CATALOG: Final[tuple[CapabilityDescriptor, ...]] = (
    CapabilityDescriptor(
        key="miroflow",
        name="MiroFlow",
        module="miroflow",
        purpose="Agent runtime and tooling",
    ),
    CapabilityDescriptor(
        key="mirothinker",
        name="MiroThinker",
        module="mirothinker",
        purpose="Analysis-heavy agent work",
    ),
)


def capability_catalog() -> tuple[CapabilityDescriptor, ...]:
    """Return the committed optional capability catalog."""
    return _CAPABILITY_CATALOG



def capability_descriptor(key: str) -> CapabilityDescriptor:
    """Return the descriptor for a known optional capability key."""
    for descriptor in _CAPABILITY_CATALOG:
        if descriptor.key == key:
            return descriptor

    raise CapabilityLookupError(f"unknown capability: {key}")



def discover_capability_status() -> tuple[CapabilityStatus, ...]:
    """Return availability metadata without importing optional packages eagerly."""
    return tuple(_discover_one(descriptor) for descriptor in _CAPABILITY_CATALOG)



def _discover_one(descriptor: CapabilityDescriptor) -> CapabilityStatus:
    spec = importlib.util.find_spec(descriptor.module)
    if spec is None:
        available = False
    elif isinstance(spec, importlib.machinery.ModuleSpec):
        available = True
    else:
        raise CapabilityDiscoveryError(
            f"Capability probe returned malformed metadata for {descriptor.key}: "
            f"{type(spec).__name__}"
        )

    return CapabilityStatus(
        key=descriptor.key,
        name=descriptor.name,
        module=descriptor.module,
        purpose=descriptor.purpose,
        available=available,
    )
