from __future__ import annotations

import asyncio
import fcntl
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import uuid4

from .browser import BrowserHost
from .config import Settings
from .models import TERMINAL, InputGate, Task, TaskStatus
from .navigation import resolve_start_url
from .worker import BrowserWorker

log = logging.getLogger(__name__)


@dataclass
class ManagedAgent:
    id: str
    name: str
    host: BrowserHost
    gate: InputGate
    task: Task | None = None
    runner: asyncio.Task | None = None
    journal: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False

    def summary(self) -> dict:
        return {"agent_id": self.id, "name": self.name,
                "browser_ready": self.host.ready,
                "task": self.task.summary() if self.task else None}


class AgentManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.agents: dict[str, ManagedAgent] = {}
        self.events: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._lock = asyncio.Lock()
        self.connected = False
        self.hold = False
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.catalog_path = settings.data_dir / "agents.json"
        self._instance_lock = (settings.data_dir / ".instance.lock").open("a")
        try:
            fcntl.flock(self._instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.catalog = json.loads(self.catalog_path.read_text()) if self.catalog_path.exists() else {}
        except BlockingIOError as exc:
            self._instance_lock.close()
            raise RuntimeError("CursorMind khác đang dùng DATA_DIR này; thoát phiên cũ trước.") from exc
        except BaseException:
            self._instance_lock.close()
            raise

    def _save_catalog(self):
        temporary = self.catalog_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.catalog_path)

    def _notify(self, agent: ManagedAgent):
        event = agent.summary()
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(event)

    def resolve(self, reference: str) -> ManagedAgent:
        matches = [a for a in self.agents.values()
                   if not a.closed and (reference == a.id or reference.casefold() == a.name.casefold())]
        if len(matches) != 1:
            raise ValueError("Không xác định được agent. Dùng list_agents để lấy tên hoặc ID.")
        return matches[0]

    async def create(self, name: str) -> dict:
        name = name.strip()
        if not name or len(name) > 60:
            raise ValueError("Tên agent phải có 1–60 ký tự.")
        async with self._lock:
            if any(a.name.casefold() == name.casefold() for a in self.agents.values()):
                raise ValueError("Agent đã tồn tại; giao việc bằng ID/tên đang có.")
            if len(self.agents) >= self.settings.max_agents:
                raise ValueError(f"Tối đa {self.settings.max_agents} agent đang mở.")
            agent_id = next((key for key, value in self.catalog.items()
                             if value.casefold() == name.casefold()), uuid4().hex[:10])
            gate = InputGate()
            used = {a.host.slot for a in self.agents.values()}
            slot = next(n for n in range(self.settings.max_agents) if n not in used)
            host = BrowserHost(self.settings, gate, self.settings.data_dir / agent_id / "profile", slot)
            agent = ManagedAgent(agent_id, name, host, gate)
            self.agents[agent_id] = agent
            self.catalog[agent_id] = name
            self._save_catalog()
            self._notify(agent)
            return {**agent.summary(), "note": "Agent đã tạo; cửa sổ Chromium mở khi nhận task."}

    def list_agents(self) -> list[dict]:
        return [a.summary() for a in self.agents.values() if not a.closed]

    async def _stop_runner(self, agent: ManagedAgent):
        if agent.runner is not None:
            if not agent.runner.done():
                agent.runner.cancel()
            with suppress(asyncio.CancelledError):
                try:
                    await agent.runner
                except Exception:
                    log.exception("Worker %s gặp lỗi khi dọn tài nguyên", agent.id)
            agent.runner = None

    def _launch(self, agent: ManagedAgent):
        if agent.closed:
            raise ValueError("Agent đã đóng; hãy tạo lại để tiếp tục.")
        if not self.connected or self.hold:
            agent.task.status = TaskStatus.PAUSED
            agent.task.progress = "Đang pause toàn hệ thống. Dùng resume_all khi kết nối sẵn sàng."
            self._notify(agent)
            return
        agent.task.status = TaskStatus.RUNNING
        agent.gate.open(agent.task.revision)
        worker = BrowserWorker(
            self.settings, agent.host, agent.gate, agent.task, agent.journal,
            lambda: self._notify(agent),
            self.settings.data_dir / agent.id / "tasks" / agent.task.id,
        )
        agent.runner = asyncio.create_task(worker.run(), name=f"agent-{agent.id}")
        self._notify(agent)

    async def assign(self, reference: str, goal: str, start_url: str = "") -> dict:
        if not goal.strip() or len(goal) > 12000:
            raise ValueError("Yêu cầu phải có 1–12000 ký tự.")
        start_url = resolve_start_url(goal, start_url)
        agent = self.resolve(reference)
        agent.gate.block("Đang chuyển sang task mới.")
        async with agent.lock:
            if agent.task and agent.task.status not in TERMINAL:
                agent.task.status = TaskStatus.CANCELLED
                agent.task.revision += 1
            await self._stop_runner(agent)
            agent.task = Task(goal=goal.strip(), start_url=start_url)
            agent.journal = []
            self._launch(agent)
            return agent.summary()

    async def update(self, reference: str, goal: str) -> dict:
        if not goal.strip() or len(goal) > 12000:
            raise ValueError("Yêu cầu phải có 1–12000 ký tự.")
        agent = self.resolve(reference)
        agent.gate.block("Đang cập nhật yêu cầu.")
        async with agent.lock:
            if not agent.task or agent.task.status in TERMINAL:
                raise ValueError("Không có task đang làm để cập nhật; hãy giao task mới.")
            agent.task.revision += 1
            agent.task.status = TaskStatus.PAUSED
            await self._stop_runner(agent)
            agent.task.goal = goal.strip()
            # Inspect current progress after a goal change rather than opening
            # the old goal's startup URL again.
            agent.task.start_url = ""
            self._launch(agent)
            return agent.summary()

    async def pause(self, reference: str, reason="Đã tạm dừng theo yêu cầu.") -> dict:
        agent = self.resolve(reference)
        agent.gate.block(reason)
        async with agent.lock:
            if agent.task and agent.task.status not in TERMINAL:
                agent.task.status = TaskStatus.PAUSED
                agent.task.progress = reason
                agent.task.revision += 1
            await self._stop_runner(agent)
            self._notify(agent)
            return agent.summary()

    async def resume(self, reference: str) -> dict:
        agent = self.resolve(reference)
        async with agent.lock:
            if not agent.task or agent.task.status not in {TaskStatus.PAUSED, TaskStatus.WAITING}:
                raise ValueError("Agent không có task đang pause/chờ người dùng.")
            await self._stop_runner(agent)
            agent.task.revision += 1
            self._launch(agent)
            return agent.summary()

    async def cancel(self, reference: str) -> dict:
        agent = self.resolve(reference)
        agent.gate.block("Đã hủy task.")
        async with agent.lock:
            if agent.task and agent.task.status not in TERMINAL:
                agent.task.status = TaskStatus.CANCELLED
                agent.task.revision += 1
            await self._stop_runner(agent)
            self._notify(agent)
            return agent.summary()

    async def pause_all(self, reason="Đã dừng toàn bộ input.") -> dict:
        self.hold = True
        agents = [a for a in self.agents.values() if not a.closed]
        for agent in agents:
            agent.gate.block(reason)
        await asyncio.gather(*(self.pause(a.id, reason) for a in agents), return_exceptions=True)
        return {"paused": True, "agents": self.list_agents()}

    async def resume_all(self) -> dict:
        if not self.connected:
            raise ValueError("Gemini Live chưa kết nối lại; input vẫn đang bị chặn.")
        self.hold = False
        for agent in list(self.agents.values()):
            if not agent.closed and agent.task and agent.task.status in {TaskStatus.PAUSED, TaskStatus.WAITING}:
                await self.resume(agent.id)
        return {"paused": False, "agents": self.list_agents()}

    async def rename(self, reference: str, name: str) -> dict:
        agent = self.resolve(reference)
        name = name.strip()
        if not name or len(name) > 60:
            raise ValueError("Tên agent phải có 1–60 ký tự.")
        async with self._lock:
            if any(k != agent.id and v.casefold() == name.casefold() for k, v in self.catalog.items()):
                raise ValueError("Tên này đã được dùng cho một profile khác.")
            agent.name = name
            self.catalog[agent.id] = name
            self._save_catalog()
            self._notify(agent)
        return agent.summary()

    async def close_agent(self, reference: str) -> dict:
        agent = self.resolve(reference)
        agent.gate.block("Đang đóng agent.")
        async with agent.lock:
            agent.closed = True
            if agent.task and agent.task.status not in TERMINAL:
                agent.task.status = TaskStatus.CANCELLED
                agent.task.revision += 1
            await self._stop_runner(agent)
            try:
                await agent.host.close()
            finally:
                self.agents.pop(agent.id, None)
        return {"closed": agent.id, "name": agent.name, "profile_preserved": True}

    async def close(self):
        try:
            await self.pause_all("CursorMind đang thoát.")
            for agent in list(self.agents.values()):
                try:
                    await self.close_agent(agent.id)
                except Exception:
                    log.exception("Không dọn được agent %s", agent.id)
        finally:
            self._instance_lock.close()
