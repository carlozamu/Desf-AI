from __future__ import annotations

from datetime import datetime


def log(tag: str, message: str) -> None:
    print(f"[{tag}] {datetime.now().isoformat(timespec='seconds')}: {message}")
