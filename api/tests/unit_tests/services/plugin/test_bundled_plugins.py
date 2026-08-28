import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_load_bundled_plugins_rejects_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from services.plugin import bundled_plugins

    manifest = {
        "plugins": [
            {
                "plugin_id": "langgenius/example",
                "version": "1.0.0",
                "file": "../outside.difypkg",
                "sha256": "unused",
            }
        ]
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path.parent / "outside.difypkg").write_bytes(b"package")
    monkeypatch.setattr(bundled_plugins, "BUNDLED_PLUGIN_DIR", tmp_path)

    with pytest.raises(RuntimeError, match="Invalid bundled plugin path"):
        bundled_plugins.load_bundled_plugins()


def test_install_bundled_plugins_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from services.plugin import bundled_plugins

    package = b"offline-package"
    package_path = tmp_path / "example.difypkg"
    package_path.write_bytes(package)
    plugin = bundled_plugins.BundledPlugin(
        plugin_id="langgenius/example",
        version="1.0.0",
        path=package_path,
        sha256=hashlib.sha256(package).hexdigest(),
    )
    identifier = "langgenius/example:1.0.0@digest"
    manager = MagicMock()
    manager.list_plugins.return_value = [SimpleNamespace(plugin_unique_identifier=identifier)]
    monkeypatch.setattr(bundled_plugins, "load_bundled_plugins", MagicMock(return_value={plugin.plugin_id: plugin}))
    monkeypatch.setattr(bundled_plugins, "PluginInstaller", MagicMock(return_value=manager))
    monkeypatch.setattr(
        bundled_plugins.PluginService,
        "upload_pkg",
        MagicMock(
            return_value=SimpleNamespace(
                unique_identifier=identifier,
                manifest=SimpleNamespace(author="langgenius", name="example", version="1.0.0"),
            )
        ),
    )
    install = MagicMock()
    monkeypatch.setattr(bundled_plugins.PluginService, "install_from_local_pkg", install)

    response = bundled_plugins.install_bundled_plugins("tenant-1", [plugin.plugin_id])

    assert response is None
    install.assert_not_called()
