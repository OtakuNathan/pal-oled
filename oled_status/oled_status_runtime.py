from __future__ import annotations

from pathlib import Path

# NOTE: this module must NOT be called ``introspection``. Module names are cached
# process-wide by Python, and the oled_emotion plugin already ships a top-level
# module by that exact name -- a bare ``from introspection import ...`` here
# silently handed back the *oled_emotion* handle and failed with a manifest
# module_id mismatch. Same trap that bit st7789_face.
from status_provider import register_with_core


def build_plugin(*, plugin_dir: Path):
    class OledStatusCommunityBundle:
        plugin_id = "oled_status"
        version = "0.1.0"

        def start(self, scope):
            handle = register_with_core(scope.context, plugin_dir=plugin_dir)
            provider = handle.introspection_provider
            result = (
                provider.sidecar_manager.ensure_running()
                if provider.sidecar_manager
                else "ERR: no sidecar manager"
            )
            provider.mounted = result.startswith("already") or not result.startswith("ERR")
            provider.degraded = not provider.mounted
            if not provider.mounted:
                raise RuntimeError(result)
            provider.subscribe_turn_events()
            provider.push_status()
            scope.defer(provider.unsubscribe_turn_events)
            return handle

    return OledStatusCommunityBundle()
