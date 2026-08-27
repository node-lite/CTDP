from __future__ import annotations

import time
from pathlib import Path

import requests

from .util import atomic_write


class HttpClient:
    def __init__(self, timeout: int = 60, retries: int = 4):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "nodelite-deps/0.1"

    def get_bytes(self, url: str, *, optional: bool = False) -> bytes | None:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, timeout=self.timeout)
                if optional and response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.content
            except requests.RequestException as error:
                last_error = error
                if attempt + 1 < self.retries:
                    time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"failed to download {url}: {last_error}")

    def get_text(self, url: str, *, optional: bool = False) -> str | None:
        value = self.get_bytes(url, optional=optional)
        return None if value is None else value.decode("utf-8")

    def download(self, url: str, destination: Path, *, optional: bool = False) -> bool:
        value = self.get_bytes(url, optional=optional)
        if value is None:
            return False
        atomic_write(destination, value)
        return True
