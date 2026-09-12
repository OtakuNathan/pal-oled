"""Pal plugin: drive the OLED as a passive status board.

Pal the mind never touches this panel. This plugin -- a machine -- reads the same
runtime ports core uses to build /status and forwards a small structured
snapshot to the sidecar on turn start/end. The sidecar owns its own clock and
weather refresh. No tool call, no turn, and no token is spent on the board.

Why ports instead of a channel: a new channel endpoint would need a persisted
endpoint record, and nothing but the interactive setup wizard writes those
(``pal provider install`` only unpacks wheels). Hand-editing the runtime
database is off the table, so the plugin reads the ports directly instead --
structured data, no text parsing.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.core.turn_events import TURN_END, TURN_START, TurnEvent
from pal.execution.tool_facade import ToolGuidance
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext

ACTIVITY_WORKING = "WORKING"
ACTIVITY_IDLE = "IDLE"

# Nathan's timezone. The host runs Europe/London while his day runs on
# Asia/Shanghai -- which is also what every proactive schedule declares.
PAL_TIMEZONE = "Asia/Shanghai"


# --------------------------------------------------------------------------- #
# Status composition (ported from the runtime's own /status inputs)            #
# --------------------------------------------------------------------------- #


def _short_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _format_due(due: Any) -> str:
    text = str(due or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        try:
            moment = moment.astimezone(ZoneInfo(PAL_TIMEZONE))
        except Exception:
            moment = moment.astimezone()
    return moment.strftime("%H:%M")


def _next_due_label(manager: Any) -> str:
    """``HH:MM label`` for the soonest enabled proactive task, or empty."""
    registered = getattr(manager, "registered", None)
    schedule = getattr(manager, "schedule_engine", None)
    if not isinstance(registered, dict) or schedule is None:
        return ""
    candidates: list[tuple[str, str]] = []
    for proactive_id, definition in registered.items():
        if not bool(getattr(definition, "enabled", False)):
            continue
        try:
            due = schedule.next_due_at(str(proactive_id))
        except Exception:
            continue
        stamp = _format_due(due)
        if stamp:
            candidates.append((stamp, str(proactive_id)))
    if not candidates:
        return ""
    stamp, proactive_id = min(candidates)
    return f"{stamp} {proactive_id.split('-')[-1]}"[:16]


def compose_status(context: Any, *, activity: str) -> dict[str, str]:
    """Build the board's status fields from the runtime ports.

    Every lookup is optional and guarded: a broken port degrades one field
    instead of taking the panel down.
    """
    ports = getattr(context, "port_registry", None) or {}
    fields: dict[str, str] = {
        "activity": activity,
        "model": "",
        "think": "",
        "ctx": "",
        "cache": "",
        "requests": "",
        "next": "",
    }

    llm = ports.get("llm:llm")
    if llm is not None:
        endpoint_id = ""
        try:
            think_status = llm.thinking_status()
        except Exception:
            think_status = {}
        if isinstance(think_status, dict):
            endpoint_id = str(think_status.get("endpoint_id") or "")
            fields["model"] = str(think_status.get("model_id") or "")
            fields["think"] = str(think_status.get("current") or "")

        context_window = 0
        try:
            endpoint = llm.active_endpoint()
        except Exception:
            endpoint = None
        if endpoint is not None:
            context_window = max(0, int(getattr(endpoint, "context_window", 0) or 0))
            if not fields["model"]:
                fields["model"] = str(getattr(endpoint, "model_id", "") or "")
            if not endpoint_id:
                endpoint_id = str(getattr(endpoint, "endpoint_id", "") or "")

        try:
            usage = llm.usage_snapshot()
        except Exception:
            usage = {}
        if isinstance(usage, dict):
            rows = [
                row for row in (usage.get("by_endpoint") or []) if isinstance(row, dict)
            ]
            active_row = next(
                (row for row in rows if str(row.get("endpoint_id") or "") == endpoint_id),
                {},
            )
            latest_input = max(0, int(active_row.get("latest_input_tokens") or 0))
            latest_output = max(0, int(active_row.get("latest_output_tokens") or 0))
            reported = bool(active_row.get("latest_usage_reported"))
            responses = max(
                0, int(active_row.get("latest_provider_response_count") or 0)
            )
            if context_window > 0 and reported and responses == 1:
                used = latest_input + latest_output
                fields["ctx"] = (
                    f"{_short_tokens(used)}/{_short_tokens(context_window)} "
                    f"{used / context_window:.0%}"
                )
            elif context_window > 0:
                fields["ctx"] = f"-/{_short_tokens(context_window)}"
            ratio = float(
                usage.get("token_cache_ratio") or usage.get("cache_hit_rate") or 0.0
            )
            fields["cache"] = f"{ratio:.0%}"
            fields["requests"] = str(int(usage.get("successful_request_count") or 0))

    manager = ports.get("proactive:proactive_manager")
    if manager is not None:
        fields["next"] = _next_due_label(manager)

    return fields


# --------------------------------------------------------------------------- #
# Sidecar transport                                                            #
# --------------------------------------------------------------------------- #


def _send_payload(
    socket_path: Path, payload: dict[str, Any], *, timeout: float = 2.0
) -> str:
    if not socket_path.exists():
        return f"ERR: sidecar not running ({socket_path})"
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        conn.sendall(json.dumps(payload).encode())
        response = conn.recv(8192).decode("utf-8", "replace").strip()
        conn.close()
        return response
    except Exception as exc:  # surfaced verbatim to the caller
        return f"ERR: {exc}"


def _read_snapshot(socket_path: Path, *, timeout: float = 1.5) -> dict[str, Any]:
    raw = _send_payload(socket_path, {"kind": "snapshot"}, timeout=timeout)
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"raw": raw}
    return payload if isinstance(payload, dict) else {"raw": raw}


def _sidecar_is_alive(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False
    return _send_payload(socket_path, {"kind": "snapshot"}, timeout=1.0).startswith("{")


@dataclass
class OledStatusSidecarManager:
    plugin_dir: Path
    process: subprocess.Popen | None = None

    @property
    def sidecar_script(self) -> Path:
        return self.plugin_dir / "sidecar.py"

    @property
    def sidecar_dir(self) -> Path:
        return self.plugin_dir.parent.parent.parent / "data" / "oled_status"

    @property
    def sidecar_socket(self) -> Path:
        return self.sidecar_dir / "oled.sock"

    def ensure_running(self) -> str:
        if _sidecar_is_alive(self.sidecar_socket):
            return "already_running"
        self._stop_orphaned()
        if not self.sidecar_script.exists():
            return f"ERR: sidecar script not found at {self.sidecar_script}"
        self.sidecar_dir.mkdir(parents=True, exist_ok=True)
        stderr_log = self.sidecar_dir / "sidecar_stderr.log"
        self.process = subprocess.Popen(
            [
                "/usr/bin/python3",
                str(self.sidecar_script),
                "--sidecar-dir",
                str(self.sidecar_dir),
                "--timezone",
                PAL_TIMEZONE,
            ],
            stdout=subprocess.DEVNULL,
            stderr=open(stderr_log, "a"),
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _sidecar_is_alive(self.sidecar_socket):
                return "started"
            time.sleep(0.1)
        return "ERR: sidecar did not start within timeout"

    def stop_sync(self) -> None:
        if _sidecar_is_alive(self.sidecar_socket):
            _send_payload(self.sidecar_socket, {"kind": "quit"}, timeout=1.0)
        process = self.process
        if process is None:
            return
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        finally:
            self.process = None

    async def shutdown_async(self) -> None:
        await asyncio.to_thread(self.stop_sync)

    def _stop_orphaned(self) -> None:
        if self.sidecar_socket.exists() and not _sidecar_is_alive(self.sidecar_socket):
            try:
                self.sidecar_socket.unlink()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Turn bridge                                                                  #
# --------------------------------------------------------------------------- #


class OledTurnSubscriber:
    """Forwards a status snapshot whenever Pal's activity changes."""

    def __init__(self, socket_path: Path, context: Any) -> None:
        self._socket_path = socket_path
        self._context = context
        self.activity = ACTIVITY_IDLE
        self.last_response = ""
        self.last_push_at = 0.0

    def __call__(self, topic: str, event: TurnEvent) -> None:
        if topic == TURN_START:
            self.activity = ACTIVITY_WORKING
        elif topic == TURN_END:
            self.activity = ACTIVITY_IDLE
        else:
            return
        self.push()

    def push(self) -> str:
        fields = compose_status(self._context, activity=self.activity)
        response = _send_payload(
            self._socket_path, {"kind": "status", "status": fields}
        )
        self.last_response = response
        self.last_push_at = time.time()
        return response


# --------------------------------------------------------------------------- #
# Plugin provider                                                              #
# --------------------------------------------------------------------------- #


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="community:oled_status",
    target_kind="module",
)
@dataclass
class OledStatusProvider:
    module_id: str = "oled_status"
    mounted: bool = True
    degraded: bool = False
    subscriber: OledTurnSubscriber | None = None
    sidecar_manager: OledStatusSidecarManager | None = None
    turn_event_bus: Any | None = None
    main_context: Any | None = None
    turn_events_subscribed: bool = False

    def subscribe_turn_events(self) -> None:
        if self.turn_event_bus is None or self.subscriber is None:
            return
        self.turn_event_bus.subscribe(TURN_START, self.subscriber)
        self.turn_event_bus.subscribe(TURN_END, self.subscriber)
        self.turn_events_subscribed = True

    def unsubscribe_turn_events(self) -> None:
        if self.turn_event_bus is None or self.subscriber is None:
            self.turn_events_subscribed = False
            return
        self.turn_event_bus.unsubscribe(TURN_START, self.subscriber)
        self.turn_event_bus.unsubscribe(TURN_END, self.subscriber)
        self.turn_events_subscribed = False

    def push_status(self) -> str:
        if self.subscriber is None:
            return "ERR: no subscriber"
        return self.subscriber.push()

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="status",
        aliases=("oled_status_status",),
        guidance=ToolGuidance(
            purpose="Inspect the OLED status board sidecar's health and current fields.",
            use_when="live OLED board health, current screen, or displayed fields are needed",
            do_not_use_when="the request is to change what the board shows",
            failure_next_steps="check the sidecar stderr log under the runtime data dir",
        ),
    )
    def status(self, call: IntrospectionCall) -> IntrospectionResult:
        socket_path = self.sidecar_manager.sidecar_socket if self.sidecar_manager else None
        if socket_path is None:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="no sidecar manager",
                llm_text="OLED status sidecar not configured.",
            )
        payload = _read_snapshot(socket_path)
        payload["sidecar_running"] = _sidecar_is_alive(socket_path)
        payload["activity"] = self.subscriber.activity if self.subscriber else ""
        payload["turn_events_subscribed"] = self.turn_events_subscribed
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="oled status board",
            structured=payload,
            llm_text=render_titled_structured_for_llm("OLED status board", payload),
        )


def register_with_core(context: MainContext, *, plugin_dir: Path) -> ModuleHandle:
    sidecar_manager = OledStatusSidecarManager(plugin_dir=plugin_dir)
    subscriber = OledTurnSubscriber(sidecar_manager.sidecar_socket, context)
    provider = OledStatusProvider(
        subscriber=subscriber,
        sidecar_manager=sidecar_manager,
        turn_event_bus=context.turn_event_bus,
        main_context=context,
    )
    handle = ModuleHandle(
        module_id="oled_status",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        shutdown_sync=sidecar_manager.stop_sync,
        shutdown_async=sidecar_manager.shutdown_async,
    )
    context.register_module(handle)
    return handle
