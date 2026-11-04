from pathlib import Path
from types import SimpleNamespace
import importlib

import pytest
from pal.plugins.contracts import PluginBuildContext


def test_status_projects_device_errors_without_changing_display_fields(monkeypatch):
    import json
    from pal.shared import IntrospectionCall
    plugin_dir = Path(__file__).parents[1] / 'oled_status'
    monkeypatch.syspath_prepend(str(plugin_dir))
    module = importlib.import_module('status_provider')
    report = 'Traceback (most recent call last):\n  File "private.py", line 7\nOSError: display disconnected'
    provider = module.OledStatusProvider(sidecar_manager=SimpleNamespace(sidecar_socket=Path('/unused')))
    monkeypatch.setattr(module, '_send_payload', lambda *a, **k: json.dumps({
        'last_error': report, 'screen': {'error': 'visible user text'}}))
    result = provider.status(IntrospectionCall(name='status'))
    assert 'OSError: display disconnected' in result.llm_text
    assert 'Traceback' not in result.llm_text
    assert 'visible user text' in result.llm_text
    assert result.structured['last_error'] == report
    monkeypatch.setattr(module, '_send_payload', lambda *a, **k: report)
    result = provider.status(IntrospectionCall(name='status'))
    assert 'OSError: display disconnected' in result.llm_text
    assert 'Traceback' not in result.llm_text


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
