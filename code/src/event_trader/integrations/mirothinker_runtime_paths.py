"""Shared vendor-runtime path resolution for MiroThinker integrations."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any, cast


class MiroThinkerRuntimePathError(RuntimeError):
    """Raised when the vendored MiroThinker runtime path set is malformed."""


def prepare_mirothinker_runtime(vendor_root: Path) -> None:
    append_mirothinker_import_roots(vendor_root)
    _patch_windows_stdio_permission_fallback()


def append_mirothinker_import_roots(vendor_root: Path) -> None:
    for path in reversed(mirothinker_import_roots(vendor_root)):
        import_root = str(path)
        if import_root not in sys.path:
            sys.path.insert(0, import_root)


def build_mirothinker_child_pythonpath(
    *,
    vendor_root: Path,
    project_src: Path,
) -> str:
    pythonpath_entries = [str(project_src.resolve(strict=False))]
    pythonpath_entries.extend(
        str(path) for path in mirothinker_import_roots(vendor_root)
    )
    existing = os.environ.get("PYTHONPATH")
    if existing:
        pythonpath_entries.append(existing)
    return os.pathsep.join(_dedupe_preserving_order(pythonpath_entries))


def mirothinker_import_roots(vendor_root: Path) -> tuple[Path, ...]:
    app_root = (vendor_root / "apps" / "miroflow-agent").resolve(strict=False)
    deps_root = (app_root / ".deps").resolve(strict=False)
    tools_root = (
        vendor_root / "libs" / "miroflow-tools" / "src"
    ).resolve(strict=False)
    if not app_root.exists():
        raise MiroThinkerRuntimePathError(
            f"Missing vendored MiroThinker runtime path: {app_root}"
        )
    if not tools_root.exists():
        raise MiroThinkerRuntimePathError(
            f"Missing vendored MiroThinker runtime path: {tools_root}"
        )

    roots: list[Path] = [app_root]
    if deps_root.exists():
        roots.append(deps_root)
        _extend_pywin32_roots(roots, deps_root)

    for env_name in (".mt-venv", ".venv"):
        site_packages = _site_packages_root(app_root / env_name)
        if site_packages is None:
            continue
        roots.append(site_packages)
        _extend_pywin32_roots(roots, site_packages)

    roots.append(tools_root)
    return tuple(_dedupe_preserving_order(roots))


def _site_packages_root(env_root: Path) -> Path | None:
    site_packages = env_root / "Lib" / "site-packages"
    if site_packages.exists():
        return site_packages.resolve(strict=False)
    return None


def _extend_pywin32_roots(roots: list[Path], base_root: Path) -> None:
    for relative in ("win32", "win32/lib", "pywin32_system32"):
        candidate = (base_root / relative).resolve(strict=False)
        if candidate.exists():
            roots.append(candidate)


def _patch_windows_stdio_permission_fallback() -> None:
    if sys.platform != "win32":
        return

    stdio_module = cast(Any, importlib.import_module("mcp.client.stdio"))
    win_utils_module = cast(Any, importlib.import_module("mcp.os.win32.utilities"))
    current = getattr(stdio_module, "create_windows_process", None)
    if getattr(current, "_event_trader_patched", False):
        return

    original = win_utils_module.create_windows_process

    async def patched_create_windows_process(
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        errlog=sys.stderr,
        cwd: Path | str | None = None,
    ):
        try:
            return await original(command, args, env, errlog, cwd)
        except PermissionError as exc:
            if getattr(exc, "winerror", None) != 5:
                raise
            return await win_utils_module._create_windows_fallback_process(
                command,
                args,
                env,
                errlog,
                cwd,
            )

    patched_create_windows_process._event_trader_patched = True  # type: ignore[attr-defined]
    win_utils_module.create_windows_process = patched_create_windows_process
    stdio_module.create_windows_process = patched_create_windows_process


def _dedupe_preserving_order[T](values: list[T]) -> list[T]:
    seen: set[T] = set()
    ordered: list[T] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


__all__ = [
    "MiroThinkerRuntimePathError",
    "append_mirothinker_import_roots",
    "build_mirothinker_child_pythonpath",
    "mirothinker_import_roots",
    "prepare_mirothinker_runtime",
]
