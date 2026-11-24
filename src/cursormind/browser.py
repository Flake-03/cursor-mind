"""Visible Chromium sessions with browser-local input and read-only DOM observation."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from contextlib import suppress
from pathlib import Path

from browser_use import BrowserProfile, BrowserSession
from browser_use.browser.session_manager import SessionManager
from browser_use.browser.watchdogs.dom_watchdog import DOMWatchdog
from browser_use.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
from cdp_use import CDPClient
from pydantic import PrivateAttr

from .config import Settings
from .coordinates import verify_visual_point
from .actions import InputAction, key_events
from .models import InputBlocked, InputGate
from .navigation import validate_start_url

log = logging.getLogger(__name__)

PAGE_STATE = """JSON.stringify({url:location.href, title:document.title,
    width:innerWidth,height:innerHeight,scrollX,scrollY,dpr:devicePixelRatio,
    visible:document.visibilityState,epoch:performance.timeOrigin,ready:document.readyState,
    humanInput:!!window.__cursormindInput?.human,\n    text:(document.body?.innerText||'').slice(0,16000)})"""

ELEMENT_STATE = """function() {
    if (!this.isConnected) throw Error('Element detached');
    const r=this.getBoundingClientRect();
    const x=Math.max(0,r.left)+(Math.min(innerWidth,r.right)-Math.max(0,r.left))/2;
    const y=Math.max(0,r.top)+(Math.min(innerHeight,r.bottom)-Math.max(0,r.top))/2;
    let hit=this.getRootNode().elementFromPoint(x,y);
    const direct=this===hit||this.contains(hit);
    while(hit?.shadowRoot?.elementFromPoint) {
        const child=hit.shadowRoot.elementFromPoint(x,y);
        if(!child||child===hit) break;
        hit=child;
    }
    let topHit=document.elementFromPoint(x,y),ancestor=this;
    while(ancestor&&ancestor!==topHit) ancestor=ancestor.parentElement||ancestor.getRootNode().host;
    return {x,y,width:r.width,height:r.height,
        inFrame:this.ownerDocument.defaultView!==this.ownerDocument.defaultView.top,
        visible:r.right>0&&r.bottom>0&&r.left<innerWidth&&r.top<innerHeight,
        hit:(direct||this===hit||this.contains(hit))&&(!!ancestor||this.contains(topHit)),
        focused:this.getRootNode().activeElement===this,
        editable:this.isContentEditable||this.tagName==='INPUT'||this.tagName==='TEXTAREA',
        value:this.isContentEditable?this.innerText:this.value,disabled:!!this.disabled};
}"""


class ObserverClient(CDPClient):
    """Explicit protocol allowlist; arbitrary model-written JavaScript is never exposed."""

    ALLOWED = {
        "Target.getTargets", "Target.getTargetInfo", "Target.setDiscoverTargets",
        "Target.createTarget", "Target.activateTarget", "Target.closeTarget",
        "Browser.getWindowForTarget", "Browser.setWindowBounds",
        "Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText",
        "Page.navigate", "Page.addScriptToEvaluateOnNewDocument",
        "Runtime.runIfWaitingForDebugger", "DOM.enable", "DOM.disable", "Browser.getVersion",
        "Target.attachToTarget", "Target.detachFromTarget", "Target.setAutoAttach",
        "Page.enable", "Page.getFrameTree", "Page.getLayoutMetrics", "Page.captureScreenshot",
        "Page.setLifecycleEventsEnabled", "Network.enable", "Network.disable",
        "DOM.getDocument", "DOM.describeNode", "DOM.resolveNode", "DOM.getBoxModel",
        "DOM.getContentQuads", "DOM.getFrameOwner", "DOM.getNodeForLocation",
        "DOMSnapshot.captureSnapshot", "Accessibility.getFullAXTree",
        "Runtime.enable", "Runtime.evaluate", "Runtime.callFunctionOn", "Runtime.getProperties",
        "Runtime.releaseObject", "Runtime.releaseObjectGroup",
    }

    async def send_raw(self, method, params=None, session_id=None):
        if method not in self.ALLOWED:
            raise RuntimeError(f"CursorMind không hỗ trợ CDP {method}.")
        # DOMWatchdog performs its own fixed serialization scripts. Forcing
        # throwOnSideEffect on every library call can break those scripts.
        async with asyncio.timeout(15):
            return await super().send_raw(method, params, session_id)


class ObserverSessions(SessionManager):
    async def _recover_agent_focus(self, crashed_target_id):
        # Recovery is a bookkeeping change, never Target.activateTarget/createTarget.
        self.browser_session.agent_focus_target_id = None


class ObserverBrowser(BrowserSession):
    _page_state: dict = PrivateAttr(default_factory=dict)
    _snapshot_target: str | None = PrivateAttr(default=None)

    async def start(self) -> None:
        if self._cdp_client_root is not None:
            return
        self._cdp_client_root = ObserverClient(self.cdp_url)
        await self._cdp_client_root.start()
        self.session_manager = ObserverSessions(self)
        await self.session_manager.start_monitoring()
        await self._cdp_client_root.send.Target.setAutoAttach(
            params={"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
        )
        # Attach only observers. No default input, popup acceptance, navigation or permissions.
        for cls, attribute in ((ScreenshotWatchdog, "_screenshot_watchdog"),
                               (DOMWatchdog, "_dom_watchdog")):
            cls.model_rebuild()
            watchdog = cls(event_bus=self.event_bus, browser_session=self)
            setattr(self, attribute, watchdog)
            watchdog.attach_to_session()
        self._watchdogs_attached = True
        await self.sync_active_tab()

    async def get_or_create_cdp_session(self, target_id=None, focus=True):
        target_id = target_id or self.agent_focus_target_id
        if not target_id or not self.session_manager:
            raise RuntimeError("Không có tab đang hiển thị; mở một tab rồi resume.")
        session = self.session_manager._get_session_for_target(target_id)
        if session is None:
            raise RuntimeError("Tab đang thay đổi; chờ rồi quan sát lại.")
        return session

    async def sync_active_tab(self) -> dict:
        targets = self.get_page_targets()
        preferred = self.agent_focus_target_id
        candidates = sorted(targets, key=lambda t: t.target_id != preferred)
        fallback = None
        for target in candidates:
            try:
                session = await self.get_or_create_cdp_session(target.target_id, focus=False)
                state = await self.state(session)
                if fallback is None:
                    fallback = (target.target_id, state)
                if state["visible"] == "visible":
                    self.agent_focus_target_id = target.target_id
                    return state
            except Exception:
                continue
        if fallback:
            self.agent_focus_target_id, state = fallback
            return state
        raise InputBlocked("Chưa đọc được tab browser; kiểm tra cửa sổ rồi tiếp tục.")

    async def state(self, session=None) -> dict:
        session = session or await self.get_or_create_cdp_session()
        result = await session.cdp_client.send.Runtime.evaluate(
            params={"expression": PAGE_STATE, "returnByValue": True},
            session_id=session.session_id,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("Không đọc được trạng thái trang hiện tại.")
        return json.loads(result["result"]["value"])

    async def get_browser_state_summary(self, **kwargs):
        self._page_state = await self.sync_active_tab()
        self._snapshot_target = self.agent_focus_target_id
        self._cached_browser_state_summary = None
        result = await super().get_browser_state_summary(**{**kwargs, "cached": False})
        self._cached_browser_state_summary = result
        return result

    async def assert_snapshot(self) -> dict:
        current = await self.sync_active_tab()
        if self.agent_focus_target_id != self._snapshot_target:
            raise RuntimeError("Tab đã đổi; cần quan sát lại trước khi thao tác.")
        for key in ("url", "epoch", "width", "height", "scrollX", "scrollY", "dpr"):
            if current.get(key) != self._page_state.get(key):
                raise RuntimeError(f"Trang đã đổi ({key}); bỏ thao tác theo ảnh cũ.")
        return current

    async def element(self, index: int):
        summary = self._cached_browser_state_summary
        node = summary.dom_state.selector_map.get(index) if summary else None
        if node is None:
            raise ValueError("Element index không còn hợp lệ; quan sát lại.")
        # Remote frames require a different coordinate space; never silently mis-click them.
        if node.target_id != self.agent_focus_target_id:
            raise ValueError("Phần tử trong iframe khác process; dùng ảnh và click_at thay cho index.")
        session = await self.get_or_create_cdp_session()
        resolved = await session.cdp_client.send.DOM.resolveNode(
            params={"backendNodeId": node.backend_node_id}, session_id=session.session_id,
        )
        object_id = resolved["object"]["objectId"]
        try:
            result = await session.cdp_client.send.Runtime.callFunctionOn(
                params={"objectId": object_id, "functionDeclaration": ELEMENT_STATE,
                        "returnByValue": True}, session_id=session.session_id,
            )
            if result.get("exceptionDetails"):
                raise RuntimeError("Không đo được phần tử; cần quan sát lại.")
            value = result["result"]["value"]
            if value.get("inFrame"):
                raise ValueError("Phần tử trong iframe; dùng screenshot và click_at.")
            return value
        finally:
            await session.cdp_client.send.Runtime.releaseObject(
                params={"objectId": object_id}, session_id=session.session_id,
            )

    async def screenshot(self) -> str:
        session = await self.get_or_create_cdp_session()
        result = await session.cdp_client.send.Page.captureScreenshot(
            params={"format": "png", "captureBeyondViewport": False},
            session_id=session.session_id,
        )
        return result["data"]

    async def on_NavigateToUrlEvent(self, event):
        raise RuntimeError("Dùng tool navigate để mở URL.")

    async def on_SwitchTabEvent(self, event):
        raise RuntimeError("Dùng tool switch_tab để chuyển tab.")

    async def on_CloseTabEvent(self, event):
        raise RuntimeError("Dùng tool close_tab để đóng tab.")

    async def on_TabCreatedEvent(self, event):
        pass

    async def on_TabClosedEvent(self, event):
        self._cached_browser_state_summary = None

    async def on_AgentFocusChangedEvent(self, event):
        self._cached_browser_state_summary = None

    async def remove_highlights(self):
        pass

    async def close_observer(self):
        await self.event_bus.stop(clear=True, timeout=2)
        if self.session_manager:
            await self.session_manager.clear()
        if self._cdp_client_root:
            await self._cdp_client_root.stop()
            self._cdp_client_root = None


# These listeners only request a cooperative pause. They never preventDefault,
# grab devices, intercept shortcuts, or suppress real input.
HUMAN_INPUT_SCRIPT = """(() => {
    if (window.__cursormindInput) return;
    const state = window.__cursormindInput = {human:false, busy:false, until:0};
    for (const kind of ['pointerdown','keydown','wheel','touchstart']) {
        addEventListener(kind, event => {
            if(event.isTrusted && !state.busy && performance.now() > state.until)
                state.human = true;
        }, {capture:true, passive:true});
    }
})()"""


class BrowserHost:
    def __init__(self, settings: Settings, gate: InputGate, profile: Path, slot: int):
        self.settings, self.gate = settings, gate
        self.profile, self.slot = profile, slot
        self.process: asyncio.subprocess.Process | None = None
        self.observer: ObserverBrowser | None = None
        self.downloads = profile.parent / "downloads"
        self._stderr = None
        self._input_lock = asyncio.Lock()
        self._installed: set[str] = set()
        self._pointer: dict[str, tuple[float, float]] = {}

    @property
    def ready(self) -> bool:
        return (self.process is not None and self.process.returncode is None
                and self.observer is not None)

    async def start(self, start_url: str = "") -> None:
        start_url = validate_start_url(start_url)
        if self.ready:
            return
        if self.process is not None:
            await self.close()
        self.profile.mkdir(parents=True, exist_ok=True)
        self.downloads.mkdir(parents=True, exist_ok=True)
        preferences = self.profile / "Default" / "Preferences"
        preferences.parent.mkdir(exist_ok=True)
        data = json.loads(preferences.read_text()) if preferences.exists() else {}
        data.setdefault("download", {}).update(
            default_directory=str(self.downloads), prompt_for_download=False,
        )
        preferences.write_text(json.dumps(data), encoding="utf-8")
        active_port = self.profile / "DevToolsActivePort"
        active_port.unlink(missing_ok=True)
        self._stderr = (self.profile.parent / "chromium.log").open("ab")
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.settings.chrome_path, f"--user-data-dir={self.profile}",
                "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                "--new-window", "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
                "--force-device-scale-factor=1", "--force-color-profile=srgb",
                f"--window-position={60 + self.slot * 48},{50 + self.slot * 40}",
                "--window-size=1100,800", start_url or self.settings.home_url,
                stdout=asyncio.subprocess.DEVNULL, stderr=self._stderr, start_new_session=True,
            )
            async with asyncio.timeout(30):
                lines = []
                while len(lines) < 2:
                    if self.process.returncode is not None:
                        raise RuntimeError(f"Chromium không mở được; xem {self.profile.parent / 'chromium.log'}")
                    if active_port.exists():
                        lines = active_port.read_text().splitlines()
                    if len(lines) < 2:
                        await asyncio.sleep(.08)
                self.observer = ObserverBrowser(
                    cdp_url=f"ws://127.0.0.1:{int(lines[0])}{lines[1]}",
                    browser_profile=BrowserProfile(
                        is_local=False, headless=False, keep_alive=True,
                        no_viewport=True, viewport=None,
                        highlight_elements=False, dom_highlight_elements=False,
                        enable_default_extensions=False, auto_download_pdfs=False,
                        cross_origin_iframes=False, demo_mode=False,
                    ),
                )
                # Target discovery/session attachment is asynchronous.
                while True:
                    try:
                        await self.observer.start()
                        await self.observer.sync_active_tab()
                        break
                    except (InputBlocked, RuntimeError):
                        await asyncio.sleep(.08)
        except BaseException:
            await self.close()
            raise

    async def _monitor(self, session):
        if session.session_id not in self._installed:
            await session.cdp_client.send_raw(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": HUMAN_INPUT_SCRIPT}, session_id=session.session_id,
            )
            self._installed.add(session.session_id)
        await self._internal(session, HUMAN_INPUT_SCRIPT)

    async def begin_task(self):
        # Called only for a new task or explicit resume, after the old runner stops.
        for target in self.observer.get_page_targets():
            session = await self.observer.get_or_create_cdp_session(target.target_id, focus=False)
            await self._monitor(session)
            await self._internal(session, "window.__cursormindInput.human=false")

    async def _internal(self, session, expression):
        result = await session.cdp_client.send_raw(
            "Runtime.evaluate", {"expression": expression, "throwOnSideEffect": False},
            session_id=session.session_id,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("Không cài được bộ nhường thao tác trên trang hiện tại.")
        return result

    async def check_user(self):
        state = await self.observer.sync_active_tab()
        session = await self.observer.get_or_create_cdp_session()
        await self._monitor(session)
        state = await self.observer.state(session)
        if state.get("humanInput"):
            raise InputBlocked("Bạn đang thao tác trên trang này. AI đã nhường; dùng /resume khi xong.")

    async def present(self):
        """Raise once per accepted task/resume, never for each mouse/key event."""
        target = self.observer.agent_focus_target_id
        root = self.observer._cdp_client_root
        result = await root.send_raw("Browser.getWindowForTarget", {"targetId": target})
        await root.send_raw("Browser.setWindowBounds",
                            {"windowId": result["windowId"], "bounds": {"windowState": "normal"}})
        await root.send_raw("Target.activateTarget", {"targetId": target})

    async def release_input(self):
        # Remove only our page overlay; desktop devices are never modified.
        if self.observer:
            with suppress(Exception):
                async with asyncio.timeout(2):
                    session = await self.observer.get_or_create_cdp_session()
                    await self._internal(session,
                        "window.__cursormindInput?.cursor?.remove()")

    async def point(self, x: float, y: float, state: dict, visual: bool = False):
        if not 0 <= x < state["width"] or not 0 <= y < state["height"]:
            raise ValueError("Điểm ngoài viewport; scroll rồi quan sát lại.")
        if visual:
            summary = self.observer._cached_browser_state_summary
            await asyncio.to_thread(
                verify_visual_point, summary.screenshot if summary else None,
                await self.observer.screenshot(), state["width"], x, y,
            )
        # CDP uses CSS viewport coordinates, independent of toolbar/desktop position.
        return (x, y), {k: state[k] for k in ("width", "height", "dpr")}

    async def input(self, revision: int, actions: list[InputAction], geometry=None):
        async with self._input_lock:
            self.gate.check(revision)
            if not self.ready:
                raise InputBlocked("Browser đã đóng; tiếp tục để mở lại.")
            state = await self.observer.sync_active_tab()
            target = self.observer.agent_focus_target_id
            self._pointer.setdefault(target, (state["width"] / 2, state["height"] / 2))
            session = await self.observer.get_or_create_cdp_session(target)
            await self._monitor(session)
            if state.get("humanInput"):
                raise InputBlocked("Bạn đang thao tác trên trang này. AI đã nhường; dùng /resume khi xong.")
            if geometry and geometry.get("target_id", target) != target:
                raise InputBlocked("Tab đã đổi trong lúc chuẩn bị thao tác; AI đã nhường cho bạn.")
            if geometry and any(state[k] != geometry[k] for k in geometry if k != "target_id"):
                raise ValueError("Viewport đã đổi; cần quan sát lại.")
            await self._internal(session, "window.__cursormindInput.busy=true")
            try:
                for index, action in enumerate(actions):
                    self.gate.check(revision)
                    if index:
                        current = await self.observer.state(session)
                        if current["epoch"] != state["epoch"] or current["url"] != state["url"]:
                            raise InputBlocked("Trang đã chuyển giữa các thao tác; cần quan sát lại.")
                    await self._perform(session, target, revision, action)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise InputBlocked(f"Đã dừng thao tác; kiểm tra trang trước khi tiếp tục: {exc}") from exc
            finally:
                with suppress(Exception):
                    async with asyncio.timeout(2):
                        await self._internal(session,
                            "window.__cursormindInput && (window.__cursormindInput.busy=false, "
                            "window.__cursormindInput.until=performance.now()+80)")

    async def _perform(self, session, target, revision, action):
        async def send(method, params):
            self.gate.check(revision)
            return await session.cdp_client.send_raw(method, params, session_id=session.session_id)

        async def mouse(kind, point, **extra):
            return await send("Input.dispatchMouseEvent",
                              {"type": kind, "x": point[0], "y": point[1], **extra})

        async def move(point, buttons=0):
            start = self._pointer.get(target, point)
            for step in range(1, 7):
                self.gate.check(revision)
                t = step / 6
                t = t * t * (3 - 2 * t)
                p = (start[0] + (point[0] - start[0]) * t,
                     start[1] + (point[1] - start[1]) * t)
                await mouse("mouseMoved", p, buttons=buttons)
                self._pointer[target] = p
                await self._internal(session, """(() => {
                    const state=window.__cursormindInput;
                    if(!state) return;
                    if(!state.cursor?.isConnected) {
                        const cursor=state.cursor=document.createElement('div');
                        cursor.setAttribute('aria-hidden','true');
                        cursor.title='CursorMind AI';
                        cursor.style.cssText='position:fixed;left:0;top:0;width:10px;height:10px;'
                            +'border:2px solid white;background:#8b5cf6;border-radius:50%;'
                            +'pointer-events:none;z-index:2147483647;box-shadow:0 1px 5px #3338;';
                        document.documentElement.appendChild(cursor);
                    }
                    const point=""" + json.dumps(list(p)) + """;
                    state.cursor.style.transform='translate3d('+point[0]+'px,'+point[1]+'px,0)';
                })()""")
                await asyncio.sleep(.008)
            self._pointer[target] = point

        if action.kind == "navigate":
            url = validate_start_url(action.url)
            if not url:
                raise ValueError("Thiếu URL đích.")
            if action.new_tab:
                result = await self.observer._cdp_client_root.send_raw(
                    "Target.createTarget", {"url": url, "background": True})
                await self._select_target(result["targetId"], revision)
            else:
                result = await send("Page.navigate", {"url": url})
                if result.get("errorText"):
                    raise RuntimeError(result["errorText"])
        elif action.kind == "switch_tab":
            await self._select_target(action.target, revision)
        elif action.kind == "close_tab":
            await self.observer._cdp_client_root.send_raw("Target.closeTarget", {"targetId": target})
            self.observer.agent_focus_target_id = None
        elif action.kind == "text":
            await send("Input.insertText", {"text": action.text})
        elif action.kind == "check_editable":
            result = await send("Runtime.evaluate", {
                "expression": """(() => {
                    let e=document.activeElement;
                    while(e?.shadowRoot?.activeElement) e=e.shadowRoot.activeElement;
                    return !!e && !e.disabled && !e.readOnly &&
                        (e.isContentEditable || e.tagName==='INPUT' || e.tagName==='TEXTAREA');
                })()""", "returnByValue": True,
            })
            if not result.get("result", {}).get("value"):
                raise ValueError("Ô nhập chưa nhận focus; chưa gửi text.")
        elif action.kind == "chord":
            events = key_events(action.keys)
            try:
                await send("Input.dispatchKeyEvent", events[0])
                self.gate.check(revision)
            finally:
                # Key release is cleanup, including after pause/cancellation.
                await session.cdp_client.send_raw("Input.dispatchKeyEvent", events[1],
                                                 session_id=session.session_id)
        elif action.kind in {"click", "move", "scroll"}:
            if action.point is None:
                raise ValueError("Thiếu tọa độ.")
            await move(action.point)
            if action.kind == "click":
                mask = {"left": 1, "right": 2, "middle": 4}[action.button]
                for count in range(1, action.clicks + 1):
                    try:
                        await mouse("mousePressed", action.point, button=action.button, buttons=mask, clickCount=count)
                    finally:
                        await session.cdp_client.send_raw("Input.dispatchMouseEvent",
                            {"type": "mouseReleased", "x": action.point[0], "y": action.point[1],
                             "button": action.button, "buttons": 0, "clickCount": count},
                            session_id=session.session_id)
            elif action.kind == "scroll":
                for _ in range(4):
                    await mouse("mouseWheel", action.point, deltaX=0, deltaY=-action.scroll * 25)
                    await asyncio.sleep(.012)
        elif action.kind == "drag":
            start = self._pointer.get(target)
            if start is None or action.point is None:
                raise ValueError("Cần điểm bắt đầu/kết thúc drag.")
            try:
                await mouse("mousePressed", start, button="left", buttons=1, clickCount=1)
                await move(action.point, buttons=1)
            finally:
                point = self._pointer.get(target, start)
                await session.cdp_client.send_raw("Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": point[0], "y": point[1],
                     "button": "left", "buttons": 0, "clickCount": 1}, session_id=session.session_id)
        else:
            raise ValueError(f"Action không hỗ trợ: {action.kind}")

    async def _select_target(self, target, revision):
        deadline = time.monotonic() + 5
        while True:
            self.gate.check(revision)
            if any(t.target_id == target for t in self.observer.get_page_targets()):
                try:
                    session = await self.observer.get_or_create_cdp_session(target, focus=False)
                    await self._monitor(session)
                    break
                except RuntimeError:
                    pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Chưa kết nối được tab mới.")
            await asyncio.sleep(.05)
        await self.observer._cdp_client_root.send_raw("Target.activateTarget", {"targetId": target})
        self.observer.agent_focus_target_id = target

    async def settle(self) -> dict:
        deadline = time.monotonic() + 5
        previous = None
        stable_since = time.monotonic()
        while True:
            try:
                state = await self.observer.sync_active_tab()
                session = await self.observer.get_or_create_cdp_session()
                if session.session_id not in self._installed:
                    await self._monitor(session)
                signature = (state["url"], state["epoch"], state["text"][:4000])
                if signature != previous:
                    previous, stable_since = signature, time.monotonic()
                elif state["ready"] != "loading" and time.monotonic() - stable_since >= .16:
                    return state
                if time.monotonic() >= deadline:
                    return state
            except (RuntimeError, InputBlocked):
                if time.monotonic() >= deadline:
                    raise
            await asyncio.sleep(.08)

    async def close(self) -> None:
        if self.observer:
            with suppress(Exception):
                await self.observer.close_observer()
            self.observer = None
        if self.process:
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
            self.process = None
        if self._stderr:
            self._stderr.close()
            self._stderr = None
        self._installed.clear()
        self._pointer.clear()
