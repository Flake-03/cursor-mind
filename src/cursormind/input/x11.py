"""libX11/libXi bindings; the controller serializes every call."""
from __future__ import annotations

import ctypes as C
import ctypes.util
import os
import subprocess
import time
from dataclasses import dataclass

I, U, L, W, D, P = C.c_int, C.c_uint, C.c_ulong, C.c_ulong, C.c_double, C.c_void_p


class Device(C.Structure):
    _fields_ = [("deviceid", I), ("name", C.c_char_p), ("use", I),
                ("attachment", I), ("enabled", I), ("num_classes", I), ("classes", P)]


class Buttons(C.Structure):
    _fields_ = [("mask_len", I), ("mask", P)]


class Mods(C.Structure):
    _fields_ = [("base", I), ("latched", I), ("locked", I), ("effective", I)]


class XError(C.Structure):
    _fields_ = [("type", I), ("display", P), ("resourceid", W), ("serial", L),
                ("error_code", C.c_ubyte), ("request_code", C.c_ubyte),
                ("minor_code", C.c_ubyte)]


class MessageData(C.Union):
    _fields_ = [("b", C.c_char * 20), ("s", C.c_short * 10), ("l", C.c_long * 5)]


class ClientMessage(C.Structure):
    _fields_ = [("type", I), ("serial", L), ("send_event", I), ("display", P),
                ("window", W), ("message_type", W), ("format", I), ("data", MessageData)]


class Event(C.Union):
    _fields_ = [("client", ClientMessage), ("pad", C.c_long * 24)]


@dataclass(frozen=True)
class Input:
    id: int
    name: str
    use: int
    attachment: int


class X11:
    def __init__(self):
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            raise RuntimeError("Requires a native Xorg session, not Xwayland")
        self.x = C.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self.xi = C.CDLL(ctypes.util.find_library("Xi") or "libXi.so.6")
        self._bind(self.x, "XInitThreads", I, [])()
        self._bind(self.x, "XOpenDisplay", P, [C.c_char_p])
        self._bind(self.x, "XCloseDisplay", I, [P])
        self._bind(self.x, "XDefaultRootWindow", W, [P])
        self._bind(self.x, "XSync", I, [P, I])
        self._bind(self.x, "XFree", I, [P])
        self._bind(self.x, "XQueryTree", I, [P, W, C.POINTER(W), C.POINTER(W), C.POINTER(C.POINTER(W)), C.POINTER(U)])
        self._bind(self.x, "XGetGeometry", I, [P, W, C.POINTER(W), C.POINTER(I), C.POINTER(I), C.POINTER(U), C.POINTER(U), C.POINTER(U), C.POINTER(U)])
        self._bind(self.x, "XTranslateCoordinates", I, [P, W, W, I, I, C.POINTER(I), C.POINTER(I), C.POINTER(W)])
        self._bind(self.x, "XInternAtom", W, [P, C.c_char_p, I])
        self._bind(self.x, "XSendEvent", I, [P, W, I, C.c_long, C.POINTER(Event)])
        self._bind(self.x, "XGetWindowProperty", I, [P, W, W, C.c_long, C.c_long, I, W, C.POINTER(W), C.POINTER(I), C.POINTER(L), C.POINTER(L), C.POINTER(P)])
        self._bind(self.xi, "XIQueryVersion", I, [P, C.POINTER(I), C.POINTER(I)])
        self._bind(self.xi, "XIQueryDevice", C.POINTER(Device), [P, I, C.POINTER(I)])
        self._bind(self.xi, "XIFreeDeviceInfo", None, [C.POINTER(Device)])
        self._bind(self.xi, "XISetFocus", I, [P, I, W, L])
        self._bind(self.xi, "XIGetFocus", I, [P, I, C.POINTER(W)])
        self._bind(self.xi, "XISetClientPointer", I, [P, W, I])
        self._bind(self.xi, "XIGetClientPointer", I, [P, W, C.POINTER(I)])
        self._bind(self.xi, "XIQueryPointer", I, [P, I, W, C.POINTER(W), C.POINTER(W), C.POINTER(D), C.POINTER(D), C.POINTER(D), C.POINTER(D), C.POINTER(Buttons), C.POINTER(Mods), C.POINTER(Mods)])
        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("Cannot open DISPLAY; run as desktop user with matching XAUTHORITY")
        self.errors = []
        self._handler_type = C.CFUNCTYPE(I, P, C.POINTER(XError))
        self._handler = self._handler_type(self._on_error)
        self._set_handler = self._bind(self.x, "XSetErrorHandler", P, [P])
        self._previous_handler = self._set_handler(C.cast(self._handler, P))
        self.root = self.x.XDefaultRootWindow(self.display)
        major, minor = I(2), I(0)
        try:
            if self.xi.XIQueryVersion(self.display, C.byref(major), C.byref(minor)) != 0:
                raise RuntimeError("XInput2 >= 2.0 is required")
            self.sync()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _bind(lib, name, result, args):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
        return fn

    def _on_error(self, display, event):
        e = event.contents
        self.errors.append((e.error_code, e.request_code, e.minor_code, e.resourceid))
        return 0

    def sync(self):
        self.x.XSync(self.display, 0)
        if self.errors:
            errors, self.errors = self.errors, []
            raise RuntimeError(f"X11 rejected request(s): {errors}")

    def close(self):
        if self.display:
            self.x.XCloseDisplay(self.display)
            self.display = None
            self._set_handler(self._previous_handler)

    def command(self, *args):
        return subprocess.run(["xinput", *map(str, args)], check=True,
                              capture_output=True, text=True, timeout=3).stdout

    def devices(self):
        count = I()
        ptr = self.xi.XIQueryDevice(self.display, 0, C.byref(count))
        try:
            self.sync()
            return [Input(d.deviceid, d.name.decode(), d.use, d.attachment)
                    for d in ptr[:count.value]]
        finally:
            if ptr:
                self.xi.XIFreeDeviceInfo(ptr)

    def wait_device(self, name, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = [d for d in self.devices() if d.name == name]
            if len(found) == 1:
                return found[0]
            time.sleep(.04)
        raise RuntimeError(f"Xorg did not expose unique device {name!r}")

    def focus(self, keyboard):
        value = W()
        self.xi.XIGetFocus(self.display, keyboard, C.byref(value))
        self.sync()
        return value.value

    def set_focus(self, keyboard, window):
        self.xi.XISetFocus(self.display, keyboard, window, 0)
        self.sync()

    def set_client_pointer(self, window: int, pointer: int) -> None:
        self.xi.XISetClientPointer(self.display, window, pointer)
        self.sync()

    def client_pointer(self, window: int) -> int:
        pointer = I()
        found = self.xi.XIGetClientPointer(self.display, window, C.byref(pointer))
        self.sync()
        return pointer.value if found else 0

    def activate_window(self, window: int, current: int) -> None:
        """Ask the WM to restore the user's pre-launch window (EWMH)."""
        event = Event()
        event.client.type = 33  # ClientMessage
        event.client.send_event = 1
        event.client.display = self.display
        event.client.window = window
        event.client.message_type = self.x.XInternAtom(self.display, b"_NET_ACTIVE_WINDOW", 0)
        event.client.format = 32
        event.client.data.l[0] = 2  # Window-management request
        event.client.data.l[1] = 0  # CurrentTime
        event.client.data.l[2] = current
        if not self.x.XSendEvent(self.display, self.root, 0, (1 << 19) | (1 << 20), C.byref(event)):
            raise RuntimeError("Window manager không nhận yêu cầu khôi phục cửa sổ.")
        self.sync()

    def pointer(self, device):
        root, child, rx, ry, wx, wy = W(), W(), D(), D(), D(), D()
        buttons, mods, group = Buttons(), Mods(), Mods()
        ok = self.xi.XIQueryPointer(self.display, device, self.root, C.byref(root), C.byref(child),
                                   C.byref(rx), C.byref(ry), C.byref(wx), C.byref(wy),
                                   C.byref(buttons), C.byref(mods), C.byref(group))
        if buttons.mask:
            self.x.XFree(buttons.mask)
        self.sync()
        if not ok:
            raise RuntimeError("Pointer is on another X screen")
        return rx.value, ry.value, child.value

    def ancestors(self, window):
        result = []
        while window and window != self.root:
            if window in result or len(result) > 64:
                raise RuntimeError("Invalid window tree")
            result.append(window)
            root, parent, count, children = W(), W(), U(), C.POINTER(W)()
            self.x.XQueryTree(self.display, window, C.byref(root), C.byref(parent), C.byref(children), C.byref(count))
            if children:
                self.x.XFree(children)
            self.sync()
            window = parent.value
        return result

    def prop(self, window, name):
        atom = self.x.XInternAtom(self.display, name.encode(), 1)
        if not atom:
            return None
        actual, fmt, count, remaining, data = W(), I(), L(), L(), P()
        self.x.XGetWindowProperty(self.display, window, atom, 0, 16384, 0, 0,
                                 C.byref(actual), C.byref(fmt), C.byref(count), C.byref(remaining), C.byref(data))
        try:
            self.sync()
            if fmt.value == 32:
                return list(C.cast(data, C.POINTER(L))[:count.value])
            if fmt.value == 8:
                return C.string_at(data, count.value).decode("utf-8", "replace")
            return None
        finally:
            if data:
                self.x.XFree(data)

    def windows(self):
        result = []
        for window in self.prop(self.root, "_NET_CLIENT_LIST") or []:
            try:
                result.append({"id": window, "title": self.prop(window, "_NET_WM_NAME") or self.prop(window, "WM_NAME"),
                               "pid": self.pid(window), "geometry": self.geometry(window)})
            except RuntimeError:
                continue  # Window closed during enumeration.
        return result

    def pid(self, window):
        values = self.prop(window, "_NET_WM_PID")
        return values[0] if values else None

    def client_window(self, window):
        if window in (0, 1):  # None / PointerRoot are focus constants, not client XIDs.
            return None
        clients = set(self.prop(self.root, "_NET_CLIENT_LIST") or [])
        return next((w for w in self.ancestors(window) if w in clients), None)

    def geometry(self, window):
        root, x, y, width, height, border, depth = W(), I(), I(), U(), U(), U(), U()
        self.x.XGetGeometry(self.display, window, C.byref(root), C.byref(x), C.byref(y),
                            C.byref(width), C.byref(height), C.byref(border), C.byref(depth))
        child = W()
        self.x.XTranslateCoordinates(self.display, window, self.root, 0, 0, C.byref(x), C.byref(y), C.byref(child))
        self.sync()
        return {"x": x.value, "y": y.value, "width": width.value, "height": height.value}
