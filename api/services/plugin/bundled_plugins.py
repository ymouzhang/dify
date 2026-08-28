"""Install pinned, self-contained plugin packages shipped in the API image."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from core.plugin.entities.plugin_daemon import PluginInstallTaskStartResponse, PluginInstallTaskStatus
from core.plugin.impl.plugin import PluginInstaller
from core.plugin.plugin_service import PluginService

logger = logging.getLogger(__name__)

BUNDLED_PLUGIN_DIR = Path(os.getenv("BUNDLED_PLUGIN_DIR", "/app/api/bundled_plugins/packages"))


@dataclass(frozen=True)
class BundledPlugin:
    plugin_id: str
    version: str
    path: Path
    sha256: str


def load_bundled_plugins() -> dict[str, BundledPlugin]:
    root = BUNDLED_PLUGIN_DIR.resolve()
    payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    plugins: dict[str, BundledPlugin] = {}
    for raw in payload.get("plugins", []):
        plugin_id = str(raw["plugin_id"])
        package_path = (root / str(raw["file"])).resolve()
        if not package_path.is_relative_to(root) or not package_path.is_file():
            raise RuntimeError(f"Invalid bundled plugin path: {plugin_id}")
        if plugin_id in plugins:
            raise RuntimeError(f"Duplicate bundled plugin id: {plugin_id}")
        plugins[plugin_id] = BundledPlugin(
            plugin_id=plugin_id,
            version=str(raw["version"]),
            path=package_path,
            sha256=str(raw["sha256"]),
        )
    return plugins


def _read_verified_package(plugin: BundledPlugin) -> bytes:
    package = plugin.path.read_bytes()
    digest = hashlib.sha256(package).hexdigest()
    if digest != plugin.sha256:
        raise RuntimeError(
            f"Bundled plugin checksum mismatch for {plugin.plugin_id}: expected {plugin.sha256}, got {digest}"
        )
    return package


def install_bundled_plugins(tenant_id: str, requested_plugin_ids: list[str]) -> PluginInstallTaskStartResponse | None:
    bundled = load_bundled_plugins()
    selected = [bundled[plugin_id] for plugin_id in requested_plugin_ids if plugin_id in bundled]
    if not selected:
        return None

    installed_identifiers = {plugin.plugin_unique_identifier for plugin in PluginInstaller().list_plugins(tenant_id)}
    identifiers_to_install: list[str] = []
    for plugin in selected:
        response = PluginService.upload_pkg(tenant_id, _read_verified_package(plugin))
        actual_plugin_id = f"{response.manifest.author}/{response.manifest.name}"
        if actual_plugin_id != plugin.plugin_id or response.manifest.version != plugin.version:
            raise RuntimeError(
                f"Bundled plugin manifest mismatch: expected {plugin.plugin_id}:{plugin.version}, "
                f"got {actual_plugin_id}:{response.manifest.version}"
            )
        if response.unique_identifier not in installed_identifiers:
            identifiers_to_install.append(response.unique_identifier)

    if not identifiers_to_install:
        return None
    logger.info("Installing bundled plugins for tenant %s: %s", tenant_id, identifiers_to_install)
    return PluginService.install_from_local_pkg(tenant_id, identifiers_to_install)


def wait_for_plugin_install(
    tenant_id: str,
    response: PluginInstallTaskStartResponse | None,
    timeout_seconds: int | None = None,
) -> None:
    if response is None or response.all_installed:
        return
    timeout = timeout_seconds or int(os.getenv("BUNDLED_PLUGIN_INSTALL_TIMEOUT", "600"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = PluginService.fetch_install_task(tenant_id, response.task_id)
        if task.status == PluginInstallTaskStatus.Success:
            return
        if task.status == PluginInstallTaskStatus.Failed:
            failures = [
                f"{plugin.plugin_id}: {plugin.message}"
                for plugin in task.plugins
                if plugin.status == PluginInstallTaskStatus.Failed
            ]
            raise RuntimeError("Bundled plugin installation failed: " + "; ".join(failures))
        time.sleep(2)
    raise TimeoutError(f"Bundled plugin installation timed out after {timeout}s for tenant {tenant_id}")
