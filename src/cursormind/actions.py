"""Browser-local actions. No operating-system input devices are used."""
from dataclasses import dataclass


@dataclass(frozen=True)
class InputAction:
    kind: str
    point: tuple[float, float] | None = None
    keys: tuple[str, ...] = ()
    text: str = ""
    button: str = "left"
    clicks: int = 1
    scroll: int = 0
    url: str = ""
    target: str = ""
    new_tab: bool = False


def key_events(names: tuple[str, ...]) -> list[dict]:
    aliases = {"CTRL": "CONTROL", "ESC": "ESCAPE", "RETURN": "ENTER",
               "LEFT": "ARROWLEFT", "RIGHT": "ARROWRIGHT", "UP": "ARROWUP", "DOWN": "ARROWDOWN"}
    modifiers = {"ALT": 1, "CONTROL": 2, "META": 4, "SHIFT": 8}
    special = {"ENTER": ("Enter", "Enter", 13), "TAB": ("Tab", "Tab", 9),
               "ESCAPE": ("Escape", "Escape", 27), "BACKSPACE": ("Backspace", "Backspace", 8),
               "DELETE": ("Delete", "Delete", 46), "SPACE": (" ", "Space", 32),
               "ARROWLEFT": ("ArrowLeft", "ArrowLeft", 37), "ARROWUP": ("ArrowUp", "ArrowUp", 38),
               "ARROWRIGHT": ("ArrowRight", "ArrowRight", 39), "ARROWDOWN": ("ArrowDown", "ArrowDown", 40),
               "HOME": ("Home", "Home", 36), "END": ("End", "End", 35),
               "PAGEUP": ("PageUp", "PageUp", 33), "PAGEDOWN": ("PageDown", "PageDown", 34)}
    normalized = [aliases.get(n.upper(), n.upper()) for n in names]
    mask = sum(modifiers[n] for n in set(normalized) if n in modifiers)
    main = [n for n in normalized if n not in modifiers]
    if len(main) != 1 or len(normalized) > 5:
        raise ValueError("Dùng một phím chính cùng các modifier CTRL/SHIFT/ALT.")
    name = main[0]
    if name in special:
        key, code, vk = special[name]
    elif len(name) == 1 and name.isascii() and name.isalnum():
        key = name if mask & 8 else name.lower()
        code = ("Key" if name.isalpha() else "Digit") + name
        vk = ord(name)
    else:
        raise ValueError(f"Phím chưa hỗ trợ: {name}")
    if (name in {"L", "T", "W"} and mask & 2) or mask & 4:
        raise ValueError("Dùng navigate/switch_tab/close_tab; không dùng phím điều khiển cửa sổ.")
    down = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk,
            "modifiers": mask}
    if name == "ENTER":
        down["text"] = "\r"
    elif mask == 0 and len(key) == 1:
        down["text"] = key
    return [down, {k: v for k, v in {**down, "type": "keyUp"}.items() if k != "text"}]
