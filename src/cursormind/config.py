from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from .navigation import validate_start_url


@dataclass(frozen=True)
class Settings:
    api_key: str
    live_model: str
    browser_model: str
    chrome_path: str
    data_dir: Path
    max_agents: int
    max_steps: int
    task_timeout: int
    home_url: str = "about:blank"

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key:
            raise ValueError("Điền GEMINI_API_KEY trong .env trước khi chạy.")
        chrome = os.getenv("CHROME_PATH", "").strip() or next(
            (path for name in ("google-chrome", "chromium", "chromium-browser")
             if (path := shutil.which(name))), ""
        )
        if not chrome or not Path(chrome).is_file():
            raise ValueError("Không tìm thấy Chromium/Chrome. Đặt CHROME_PATH trong .env.")
        values = {name: int(os.getenv(name, str(default))) for name, default in (
            ("MAX_AGENTS", 2), ("MAX_STEPS", 60), ("TASK_TIMEOUT", 600)
        )}
        if any(value < 1 for value in values.values()):
            raise ValueError("MAX_AGENTS, MAX_STEPS và TASK_TIMEOUT phải lớn hơn 0.")
        home_url = os.getenv("HOME_URL", "about:blank").strip() or "about:blank"
        if home_url != "about:blank":
            home_url = validate_start_url(home_url)
        return cls(
            api_key=key,
            live_model=os.getenv("LIVE_MODEL", "gemini-3.8-live"),
            browser_model=os.getenv("BROWSER_MODEL", "gemini-3.8-flash"),
            chrome_path=chrome,
            data_dir=Path(os.getenv("DATA_DIR", ".data")).expanduser().resolve(),
            max_agents=values["MAX_AGENTS"],
            max_steps=values["MAX_STEPS"],
            task_timeout=values["TASK_TIMEOUT"],
            home_url=home_url,
        )
