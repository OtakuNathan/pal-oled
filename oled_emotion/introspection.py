from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from pal.behavior.decorators import affordance
from pal.behavior.contracts import AFFORDANCE_ACTIVATION_DELIBERATIVE, AFFORDANCE_VISIBILITY_RESIDENT
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.core.turn_events import TURN_END, TURN_START, TURN_TOOL_CALL_AFTER, TURN_TOOL_CALL_BEFORE, TurnEvent
from pal.execution.tool_facade import StrictToolModel, ToolGuidance
from pal.execution.tool_semantics import DIRECT_EXTERNAL_WRITE, INDIRECT_CONTROL
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext

VALID_EMOTIONS = [
    "happy", "sad", "angry", "crying", "curious",
    "shock", "sleepy", "thinking", "wink", "error",
    "standby", "working",
]


class OledEmotionInput(StrictToolModel):
    emotion: Literal[
        "happy",
        "sad",
        "angry",
        "crying",
        "curious",
        "shock",
        "sleepy",
        "thinking",
        "wink",
        "error",
        "standby",
        "working",
    ] = Field(description="Emotion animation to display.")


class OledEmotionOutput(StrictToolModel):
    emotion: str
    response: str


def _send_command(cmd: str, socket_path: Path, *, timeout: float = 3.0) -> str:
    if not socket_path.exists():
        return f"ERR: sidecar not running (socket not found at {socket_path})"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(socket_path))
        s.sendall(cmd.encode())
        resp = s.recv(1024).decode().strip()
        s.close()
        return resp
    except Exception as e:
        return f"ERR: {e}"


def _send_emotion(emotion: str, socket_path: Path) -> str:
    return _send_command(emotion, socket_path)


def _sidecar_is_alive(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False
    resp = _send_command("status", socket_path, timeout=1.0)
    return resp.startswith("{")


@dataclass
class OledSidecarManager:
    plugin_dir: Path
    process: subprocess.Popen | None = None

    @property
    def sidecar_script(self) -> Path:
        return self.plugin_dir / "sidecar.py"

    @property
    def sidecar_dir(self) -> Path:
        return self.plugin_dir.parent.parent.parent / "data" / "oled_sidecar"

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
            ["/usr/bin/python3", str(self.sidecar_script),
             "--plugin-dir", str(self.plugin_dir),
             "--sidecar-dir", str(self.sidecar_dir)],
            stdout=subprocess.DEVNULL,
            stderr=open(stderr_log, "a"),
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _sidecar_is_alive(self.sidecar_socket):
                return "started"
            time.sleep(0.1)
        return "ERR: sidecar did not start within timeout"

    def stop_sync(self) -> None:
        if _sidecar_is_alive(self.sidecar_socket):
            _send_command("quit", self.sidecar_socket, timeout=1.0)
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


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="community:oled_emotion",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="community:oled_emotion",
    target_kind="module",
)
@dataclass
class OledEmotionIntrospectionProvider:
    module_id: str = "oled_emotion"
    mounted: bool = True
    degraded: bool = False
    subscriber: OledTurnStateSubscriber | None = None
    sidecar_manager: OledSidecarManager | None = None
    turn_event_bus: Any | None = None
    turn_events_subscribed: bool = False

    def _subscribe_turn_events(self) -> None:
        if self.turn_event_bus is None or self.subscriber is None:
            return
        self.turn_event_bus.subscribe(TURN_START, self.subscriber)
        self.turn_event_bus.subscribe(TURN_TOOL_CALL_BEFORE, self.subscriber)
        self.turn_event_bus.subscribe(TURN_TOOL_CALL_AFTER, self.subscriber)
        self.turn_event_bus.subscribe(TURN_END, self.subscriber)
        self.turn_events_subscribed = True

    def _unsubscribe_turn_events(self) -> None:
        if self.turn_event_bus is None or self.subscriber is None:
            self.turn_events_subscribed = False
            return
        self.turn_event_bus.unsubscribe(TURN_START, self.subscriber)
        self.turn_event_bus.unsubscribe(TURN_TOOL_CALL_BEFORE, self.subscriber)
        self.turn_event_bus.unsubscribe(TURN_TOOL_CALL_AFTER, self.subscriber)
        self.turn_event_bus.unsubscribe(TURN_END, self.subscriber)
        self.turn_events_subscribed = False

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="show_emotion",
        aliases=("show_oled_emotion",),
        InputModel=OledEmotionInput,
        OutputModel=OledEmotionOutput,
        guidance=ToolGuidance(
            purpose="Display one OLED emotion animation through the device sidecar.",
            use_when="an expressive reaction or meaningful working-state change naturally fits the conversation",
            do_not_use_when="the animation would be repetitive or would be presented as proof of internal emotion or runtime state",
            failure_next_steps="inspect OLED status before retrying and do not claim the animation was displayed without success",
        ),
        execution=DIRECT_EXTERNAL_WRITE,
        examples=({"emotion": "happy"},),
        metadata={"omit_family_in_canonical": True},
    )
    def show_emotion(self, call: IntrospectionCall) -> IntrospectionResult:
        emotion = str(call.args.get("emotion") or "").strip().lower()
        if emotion not in VALID_EMOTIONS:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text=f"invalid emotion: {emotion}",
                llm_text=f"Invalid emotion '{emotion}'. Valid: {VALID_EMOTIONS}",
            )
        socket_path = self.sidecar_manager.sidecar_socket if self.sidecar_manager else None
        if socket_path is None:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="no sidecar manager",
                llm_text="OLED sidecar not configured.",
            )
        result = _send_emotion(emotion, socket_path)
        if result.startswith("OK"):
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text=f"emotion displayed: {emotion}",
                structured={"emotion": emotion, "response": result},
                llm_text=f"Displayed '{emotion}' on OLED.",
            )
        else:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text=f"failed to display emotion: {result}",
                llm_text=f"Failed to display '{emotion}' on OLED: {result}",
            )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="status",
        aliases=("oled_emotion_status",),
        guidance=ToolGuidance(
            purpose="Inspect the OLED emotion sidecar's current status.",
            use_when="live OLED sidecar health or state is needed",
            do_not_use_when="the request is to attach, detach, or display an animation",
            failure_next_steps="inspect OLED module configuration if the sidecar socket is unavailable",
        ),
    )
    def status(self, call: IntrospectionCall) -> IntrospectionResult:
        socket_path = self.sidecar_manager.sidecar_socket if self.sidecar_manager else None
        if socket_path is None:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="no sidecar manager",
                llm_text="OLED sidecar not configured.",
            )
        resp = _send_command("status", socket_path)
        try:
            payload = json.loads(resp)
        except Exception:
            payload = {"raw": resp}
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="oled sidecar status",
            structured=payload,
            llm_text=render_titled_structured_for_llm("OLED sidecar status", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        aliases=("oled_emotion_show",),
        guidance=ToolGuidance(
            purpose="Show installed OLED emotion module configuration and capabilities.",
            use_when="module paths, supported emotions, or configured sidecar details are needed",
            do_not_use_when="only current sidecar health is needed; use oled_emotion_status",
            failure_next_steps="inspect the installed plugin files and sidecar configuration",
        ),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = {
            "module_id": "oled_emotion",
            "plugin_dir": str(self.sidecar_manager.plugin_dir) if self.sidecar_manager else None,
            "sidecar_socket": str(self.sidecar_manager.sidecar_socket) if self.sidecar_manager else None,
            "valid_emotions": VALID_EMOTIONS,
            "sidecar_running": _sidecar_is_alive(self.sidecar_manager.sidecar_socket) if self.sidecar_manager else False,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="oled emotion module info",
            structured=payload,
            llm_text=render_titled_structured_for_llm("OLED emotion module", payload),
        )


class OledTurnStateSubscriber:
    state: str = "standby"

    def __init__(self, socket_path: Path):
        self._socket_path = socket_path

    def __call__(self, topic: str, event: TurnEvent) -> None:
        if topic == TURN_START:
            _send_emotion("thinking", self._socket_path)
            self.state = "thinking"
        elif topic == TURN_TOOL_CALL_BEFORE:
            if self.state != "working":
                _send_emotion("working", self._socket_path)
            self.state = "working"
        elif topic == TURN_TOOL_CALL_AFTER:
            _send_emotion("thinking", self._socket_path)
            self.state = "thinking"
        elif topic == TURN_END:
            if event.get("status") == "failed":
                _send_emotion("error", self._socket_path)
            _send_emotion("standby", self._socket_path)
            self.state = "standby"


def register_with_core(context: MainContext, *, plugin_dir: Path) -> ModuleHandle:
    sidecar_manager = OledSidecarManager(plugin_dir=plugin_dir)
    socket_path = sidecar_manager.sidecar_socket
    subscriber = OledTurnStateSubscriber(socket_path=socket_path)
    provider = OledEmotionIntrospectionProvider(
        subscriber=subscriber,
        sidecar_manager=sidecar_manager,
        turn_event_bus=context.turn_event_bus,
    )

    handle = ModuleHandle(
        module_id="oled_emotion",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        shutdown_sync=provider.sidecar_manager.stop_sync if provider.sidecar_manager else None,
        shutdown_async=provider.sidecar_manager.shutdown_async if provider.sidecar_manager else None,
    )
    context.register_module(handle)
    return handle
