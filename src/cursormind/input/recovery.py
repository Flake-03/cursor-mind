"""One-time cleanup for masters left by the old MPX version. Not used at runtime."""
import fcntl
import hashlib
import os

from .x11 import X11


def recover_input() -> list[str]:
    token = hashlib.sha256(os.environ.get("DISPLAY", "").encode()).hexdigest()[:16]
    fd = os.open(f"/tmp/cursormind-{os.getuid()}-{token}.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Bản CursorMind cũ còn chạy; thoát tiến trình cũ trước khi dọn input.") from exc
        x = X11()
        try:
            devices = x.devices()
            core = next(d for d in devices if d.name == "Virtual core pointer" and d.use == 1)
            recovered = []
            for master in devices:
                if master.use == 1 and master.name.startswith("cursormind-"):
                    x.command("remove-master", master.id, "AttachToMaster", core.id, core.attachment)
                    if any(d.id in {master.id, master.attachment} for d in x.devices()):
                        raise RuntimeError(f"Chưa gỡ được master {master.name}.")
                    recovered.append(master.name)
            return recovered
        finally:
            x.close()
    finally:
        os.close(fd)
