from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import signal
import sys

from .config import Settings

HELP = """Lệnh local (không cần model hiểu giọng nói):
  /create "Nghiên cứu"              Tạo agent, chưa giao việc
  /task <agent> <mục tiêu>           Giao task mới
  /task <agent> <URL> [mục tiêu]     Mở URL ngay khi bắt đầu task
  /update <agent> <mục tiêu đầy đủ>  Thay yêu cầu, giữ tiến độ
  /agents                           Liệt kê agent và task
  /status <agent>                    Xem tiến độ và kết quả
  /pause [agent]                     Pause một agent hoặc tất cả
  /resume <agent|all>                Quan sát lại rồi tiếp tục
  /cancel <agent>                    Hủy task, giữ browser
  /close <agent>                     Đóng browser, giữ profile
  /rename <agent> <tên mới>          Đổi tên, giữ profile
  /mute                             Bật/tắt microphone
  /help                             Xem các lệnh
  /exit                             Dừng và dọn tài nguyên
Tên chứa khoảng trắng đặt trong dấu nháy. Có thể dùng agent_id thay tên.
Bạn cũng có thể nói hoặc nhập yêu cầu bằng tiếng Việt bình thường.
"""


async def read_line() -> str:
    loop = asyncio.get_running_loop()
    line = loop.create_future()
    print("\nBạn> ", end="", flush=True)

    def readable():
        if not line.done():
            line.set_result(sys.stdin.readline())

    loop.add_reader(sys.stdin.fileno(), readable)
    try:
        value = await line
        return value.strip() if value else "/exit"
    finally:
        loop.remove_reader(sys.stdin.fileno())


async def run(settings: Settings, text_only: bool):
    # Configure browser-use before its first import; only Gemini receives task context.
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
    os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"
    from .live import LiveAssistant
    from .manager import AgentManager

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    manager = AgentManager(settings)
    runtime_log = logging.getLogger("cursormind")
    file_handler = logging.FileHandler(settings.data_dir / "runtime.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    runtime_log.addHandler(file_handler)
    audio = None
    assistant = None
    background = []
    controls: set[asyncio.Task] = set()
    try:
        if not text_only:
            from .audio import AudioIO
            audio = AudioIO()
        assistant = LiveAssistant(settings, manager, audio)
        background = [asyncio.create_task(assistant.run(), name="gemini-live")]

        async def events():
            previous = {}
            while True:
                event = await manager.events.get()
                task = event.get("task")
                if not task:
                    continue
                key = (task["task_id"], task["revision"], task["status"], task["progress"], event["browser_ready"])
                if previous.get(event["agent_id"]) == key:
                    continue
                previous[event["agent_id"]] = key
                print(f"\n[{event['name']}] {task['status']}: "
                      + (task["result"] or task["error"] or task["progress"]), flush=True)
                if task["status"] in {"completed", "failed", "waiting_for_user"}:
                    await assistant.report(event)

        background.append(asyncio.create_task(events(), name="agent-events"))
        loop = asyncio.get_running_loop()

        def emergency_pause():
            # Block input synchronously before scheduling any cleanup.
            for agent in manager.agents.values():
                agent.gate.block("Emergency pause.")
            operation = asyncio.create_task(manager.pause_all("Emergency pause từ SIGUSR1."))
            controls.add(operation)
            operation.add_done_callback(controls.discard)

        loop.add_signal_handler(signal.SIGUSR1, emergency_pause)
        application = asyncio.current_task()
        for stop_signal in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(stop_signal, application.cancel)
        print(f"CursorMind | PID {os.getpid()} | {'text' if text_only else 'voice + text'}")
        print("Gõ /help để xem lệnh; /pause dừng toàn bộ input. Ctrl+C để thoát.")
        while True:
            line = await read_line()
            if not line:
                continue
            if line == "/exit":
                break
            try:
                if line.startswith("/"):
                    result = await command(line, assistant, audio)
                    if result is not None:
                        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                else:
                    if audio:
                        audio.clear_output()
                    await assistant.send_text(line)
            except Exception as exc:
                print(f"Lỗi: {exc}", file=sys.stderr, flush=True)
    finally:
        for stop_signal in (signal.SIGUSR1, signal.SIGTERM, signal.SIGHUP):
            asyncio.get_running_loop().remove_signal_handler(stop_signal)
        try:
            try:
                await manager.pause_all("CursorMind đang thoát.")
                if assistant:
                    await assistant.close()
            finally:
                for job in background:
                    job.cancel()
                await asyncio.gather(*background, *controls, return_exceptions=True)
                await manager.close()
        finally:
            if audio:
                audio.close()
            runtime_log.removeHandler(file_handler)
            file_handler.close()


async def command(line, assistant, audio):
    parts = shlex.split(line)
    name, values = parts[0], parts[1:]
    manager = assistant.manager
    if name == "/help":
        print(HELP)
    elif name == "/agents":
        return {"paused_all": manager.hold, "agents": manager.list_agents()}
    elif name == "/pause" and not values:
        return await manager.pause_all()
    elif name == "/resume" and values == ["all"]:
        return await manager.resume_all()
    elif name == "/mute":
        return {"muted": audio.toggle_mute()} if audio else {"note": "Đang ở chế độ --text."}
    elif name == "/create" and values:
        return await manager.create(" ".join(values))
    elif name in {"/task", "/update", "/rename"} and len(values) >= 2:
        if name == "/task":
            start_url = values[1] if values[1].startswith(("https://", "http://")) else ""
            return await manager.assign(values[0], " ".join(values[1:]), start_url=start_url)
        handler = {"/task": manager.assign, "/update": manager.update, "/rename": manager.rename}[name]
        return await handler(values[0], " ".join(values[1:]))
    elif name == "/status" and len(values) == 1:
        return manager.resolve(values[0]).summary()
    elif name in {"/pause", "/resume", "/cancel", "/close"} and len(values) == 1:
        handler = {"/pause": manager.pause, "/resume": manager.resume,
                   "/cancel": manager.cancel, "/close": manager.close_agent}[name]
        return await handler(values[0])
    else:
        raise ValueError("Cú pháp chưa đúng; xem /help.")


def main():
    parser = argparse.ArgumentParser(description="CursorMind: voice-directed browser agents")
    parser.add_argument("--text", action="store_true", help="Dùng text và transcript, không mở microphone/loa")
    parser.add_argument("--debug", action="store_true", help="Hiện log kỹ thuật browser")
    parser.add_argument(
        "--recover-input", action="store_true",
        help="Gỡ seat CursorMind còn sót và trả thiết bị về master thật; không cần API key/uinput",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    for name in ("websockets", "httpx", "httpcore", "google", "cdp_use"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        if args.recover_input:
            from .input.recovery import recover_input

            print(f"Đã gỡ {len(recover_input())} seat CursorMind cũ còn sót.")
            return
        asyncio.run(run(Settings.load(), args.text))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nCursorMind đã dừng.")
    except Exception as exc:
        parser.exit(1, f"Không chạy được CursorMind: {exc}\n")
