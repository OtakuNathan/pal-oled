from __future__ import annotations

from pathlib import Path

from introspection import register_with_core


def build_plugin(*, plugin_dir: Path):
    class OledEmotionCommunityBundle:
        plugin_id = "oled_emotion"
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
            provider._subscribe_turn_events()
            scope.defer(provider._unsubscribe_turn_events)
            return handle

    return OledEmotionCommunityBundle()
