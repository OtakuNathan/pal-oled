from pathlib import Path
from types import SimpleNamespace
import importlib

import pytest
from pal.plugins.contracts import PluginBuildContext


def test_oled_factory_accepts_the_host_build_context_without_starting_hardware(monkeypatch, tmp_path):
    plugin_dir = Path(__file__).parents[1] / "oled_status"
    monkeypatch.syspath_prepend(str(plugin_dir))
    module = importlib.import_module("oled_status_runtime")
    bundle = module.build_plugin(PluginBuildContext(runtime_root=tmp_path, plugin_dir=plugin_dir))
    assert bundle.plugin_id == "oled_status"
    with pytest.raises(ValueError, match="plugin_dir"):
        module.build_plugin(PluginBuildContext(runtime_root=tmp_path))
    status = importlib.import_module("status_provider").compose_status(SimpleNamespace(), activity="idle")
    assert status["activity"] == "idle"
    assert all(isinstance(value, str) for value in status.values())
