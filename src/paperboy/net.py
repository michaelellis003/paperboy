"""Shared HTTP client for all outbound API calls and downloads."""

import httpx

client = httpx.Client(
    timeout=30,
    follow_redirects=True,
    headers={"User-Agent": "paperboy/0.1"},
)
