"""Shared HTTP helper: retry on throttling and transient server errors."""

from __future__ import annotations

import time

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        # Response bodies from both APIs carry no secrets; keep them short in logs.
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 5,
    sleep=None,
    **kwargs,
) -> httpx.Response:
    sleep = sleep or time.sleep
    for attempt in range(retries + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt == retries:
                raise
            sleep(min(2**attempt, 30))
            continue
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            retry_after = resp.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else min(2**attempt, 30)
            sleep(delay)
            continue
        if resp.status_code >= 400:
            raise ApiError(method, url, resp.status_code, resp.text)
        return resp
    raise AssertionError("unreachable")
