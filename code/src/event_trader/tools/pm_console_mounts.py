"""Persistent, atomically reloadable PM Console mount catalog."""

from __future__ import annotations

import hashlib
import threading
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar


class PMConsoleMountCatalogError(ValueError):
    """Raised when the operator mount catalog cannot be accepted."""


@dataclass(frozen=True, slots=True)
class PMConsoleMountSnapshot:
    """One immutable view of the currently effective PM Console mounts."""

    generation: int
    bindings: Mapping[str, Any]
    reload_error: str | None = None


_T = TypeVar("_T")
BindingBuilder = Callable[[Path], _T]


class PMConsoleMountRegistry:
    """Load a config-only catalog and publish complete binding snapshots atomically.

    The catalog is intentionally not watched by a background thread.  A request
    calls :meth:`snapshot`, which fingerprints and reloads the file synchronously
    when it changed.  A malformed edit therefore cannot partially replace the
    last-known-good snapshot.
    """

    def __init__(
        self,
        catalog_path: str | Path,
        *,
        build_binding: BindingBuilder[_T],
    ) -> None:
        self.catalog_path = Path(catalog_path).expanduser().resolve(strict=False)
        self._build_binding: BindingBuilder[Any] = build_binding
        self._lock = threading.RLock()
        self._fingerprint: tuple[str, str] | None = None
        self._snapshot: PMConsoleMountSnapshot = PMConsoleMountSnapshot(
            generation=0,
            bindings=MappingProxyType({}),
            reload_error=None,
        )

    def snapshot(self) -> PMConsoleMountSnapshot:
        """Return one request-consistent snapshot, reloading if the catalog changed."""

        with self._lock:
            fingerprint, read_error = _fingerprint_catalog(self.catalog_path)
            if fingerprint == self._fingerprint:
                return self._snapshot
            self._fingerprint = fingerprint
            try:
                bindings: Mapping[str, Any] = self._load_bindings()
            except PMConsoleMountCatalogError as exc:
                self._snapshot = PMConsoleMountSnapshot(
                    generation=self._snapshot.generation,
                    bindings=self._snapshot.bindings,
                    reload_error=str(exc),
                )
            except Exception as exc:  # defensive boundary around config/build code
                self._snapshot = PMConsoleMountSnapshot(
                    generation=self._snapshot.generation,
                    bindings=self._snapshot.bindings,
                    reload_error=f"could not reload PM Console mount catalog: {exc}",
                )
            else:
                self._snapshot = PMConsoleMountSnapshot(
                    generation=self._snapshot.generation + 1,
                    bindings=MappingProxyType(dict(bindings)),
                    reload_error=None,
                )
            if read_error is not None and self._snapshot.reload_error is None:
                self._snapshot = PMConsoleMountSnapshot(
                    generation=self._snapshot.generation,
                    bindings=self._snapshot.bindings,
                    reload_error=read_error,
                )
            return self._snapshot

    def _load_bindings(self) -> Mapping[str, Any]:
        if not self.catalog_path.exists():
            raise PMConsoleMountCatalogError(
                f"PM Console mount catalog does not exist: {self.catalog_path}"
            )
        try:
            payload = tomllib.loads(self.catalog_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PMConsoleMountCatalogError(
                f"could not read PM Console mount catalog {self.catalog_path}: {exc}"
            ) from exc
        except tomllib.TOMLDecodeError as exc:
            raise PMConsoleMountCatalogError(
                f"PM Console mount catalog contains invalid TOML: {exc}"
            ) from exc
        if set(payload) != {"version", "mount"}:
            raise PMConsoleMountCatalogError(
                "PM Console mount catalog may contain only version and mount fields."
            )
        if isinstance(payload.get("version"), bool) or payload.get("version") != 1:
            raise PMConsoleMountCatalogError("PM Console mount catalog version must be 1.")
        raw_mounts = payload.get("mount")
        if not isinstance(raw_mounts, list) or not raw_mounts:
            raise PMConsoleMountCatalogError(
                "PM Console mount catalog must contain at least one [[mount]] entry."
            )

        bindings: dict[str, Any] = {}
        config_paths: set[Path] = set()
        for index, raw_mount in enumerate(raw_mounts, start=1):
            if not isinstance(raw_mount, dict) or set(raw_mount) != {"config"}:
                raise PMConsoleMountCatalogError(
                    f"mount #{index} must contain only the config field."
                )
            raw_config = raw_mount.get("config")
            if not isinstance(raw_config, str) or not raw_config.strip():
                raise PMConsoleMountCatalogError(f"mount #{index} config must be a path.")
            config_path = Path(raw_config).expanduser()
            if not config_path.is_absolute():
                config_path = self.catalog_path.parent / config_path
            config_path = config_path.resolve(strict=False)
            if config_path in config_paths:
                raise PMConsoleMountCatalogError(
                    f"duplicate mount config path: {config_path}"
                )
            config_paths.add(config_path)
            binding = self._build_binding(config_path)
            target_key = getattr(binding, "target_key", None)
            layout = getattr(binding, "layout", None)
            workspace_root = getattr(layout, "root", None)
            if not isinstance(target_key, str) or not target_key:
                raise PMConsoleMountCatalogError(
                    f"mount #{index} config did not resolve a target key: {config_path}"
                )
            if not isinstance(workspace_root, Path):
                raise PMConsoleMountCatalogError(
                    f"mount #{index} config did not resolve a workspace root: {config_path}"
                )
            if target_key in bindings:
                raise PMConsoleMountCatalogError(f"duplicate target key: {target_key}")
            for existing_target, existing_binding in bindings.items():
                existing_layout = getattr(existing_binding, "layout", None)
                existing_root = getattr(existing_layout, "root", None)
                if existing_root == workspace_root:
                    raise PMConsoleMountCatalogError(
                        "duplicate workspace root: "
                        f"{existing_target} and {target_key} both use {workspace_root}"
                    )
            market_data_root = getattr(binding, "market_data_root", None)
            if not isinstance(market_data_root, Path):
                raise PMConsoleMountCatalogError(
                    f"mount {target_key!r} did not resolve a shared market-data root."
                )
            for existing_target, existing_binding in bindings.items():
                existing_root = getattr(existing_binding, "market_data_root", None)
                if existing_root != market_data_root:
                    raise PMConsoleMountCatalogError(
                        "all PM Console mounts must use the same shared market-data root: "
                        f"{existing_target}={existing_root}, {target_key}={market_data_root}"
                    )
            bindings[target_key] = binding
        return bindings


def _fingerprint_catalog(path: Path) -> tuple[tuple[str, str] | None, str | None]:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return ("missing", str(path)), None
    except OSError as exc:
        return ("error", str(exc)), f"could not read PM Console mount catalog {path}: {exc}"
    return ("sha256", hashlib.sha256(content).hexdigest()), None


__all__ = [
    "PMConsoleMountCatalogError",
    "PMConsoleMountRegistry",
    "PMConsoleMountSnapshot",
]
