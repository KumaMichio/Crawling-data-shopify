"""Polite async HTTP client: per-domain + global throttling, robots.txt, 429 backoff."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .robots import Robots

log = logging.getLogger(__name__)


class Interval:
    """Lets callers through no more often than once per `interval` seconds."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = time.monotonic() + self.interval


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("retry-after")
    if not val:
        return None
    try:
        return max(1.0, float(val))
    except ValueError:
        return None


class RateLimited(Exception):
    """A store kept answering 429 after all retries."""


class Fetcher:
    def __init__(self, s: Settings):
        self.s = s
        self.client = httpx.AsyncClient(
            timeout=s.request_timeout,
            follow_redirects=True,
            headers={"User-Agent": s.user_agent, "Accept-Language": "en-US,en;q=0.9"},
            # Shopify Markets geolocates the visitor (VN IP -> VND prices); pin the US market / USD.
            cookies={"localization": s.market_country, "cart_currency": s.market_currency},
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
        )
        self._domain: dict[str, Interval] = {}
        self._image: dict[str, Interval] = defaultdict(lambda: Interval(s.image_interval))
        self._global = Interval(s.global_interval)
        self._pause: dict[str, float] = {}
        self._global_pause = 0.0
        self._recent_429: dict[str, float] = {}
        self._robots: dict[str, Robots] = {}
        self._robots_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.stats: dict[str, int] = defaultdict(int)
        self.limited_hosts: set[str] = set()   # hosts still 429 after all retries
        self.fast_fail = False                  # tripped when many stores are rate-limited

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.aclose()

    # ---------- robots ----------
    async def robots(self, base: str) -> Robots:
        async with self._robots_locks[base]:
            if base not in self._robots:
                rob = Robots()
                r = await self.get(base + "/robots.txt", check_robots=False)
                if r is not None and r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
                    rob = Robots(r.text)
                elif r is not None and r.status_code in (401, 403):
                    rob = Robots(disallow_all=True)
                if rob.crawl_delay:
                    host = urlsplit(base).netloc.lower()
                    delay = min(rob.crawl_delay, self.s.max_crawl_delay)
                    self._domain_interval(host).interval = max(self.s.per_domain_interval, delay)
                self._robots[base] = rob
            return self._robots[base]

    async def allowed(self, url: str) -> bool:
        p = urlsplit(url)
        rob = await self.robots(f"{p.scheme}://{p.netloc}")
        return rob.can_fetch((p.path or "/") + (f"?{p.query}" if p.query else ""))

    # ---------- throttling ----------
    def _domain_interval(self, host: str) -> Interval:
        if host not in self._domain:
            self._domain[host] = Interval(self.s.per_domain_interval)
        return self._domain[host]

    async def _wait_turn(self, host: str, kind: str) -> None:
        while True:
            until = self._pause.get(host, 0.0)
            if kind == "page":
                until = max(until, self._global_pause)
            delay = until - time.monotonic()
            if delay <= 0:
                break
            await asyncio.sleep(delay)
        if kind == "image":
            await self._image[host].wait()
        elif kind == "api":
            await self._image[host].wait()  # third-party APIs: light per-host spacing
        else:
            await self._domain_interval(host).wait()
            await self._global.wait()

    def _register_429(self, host: str, wait: float) -> None:
        now = time.monotonic()
        self._pause[host] = max(self._pause.get(host, 0.0), now + wait)
        self._recent_429[host] = now
        recent = [h for h, t in self._recent_429.items() if now - t < 120]
        if len(recent) >= 3:
            pause = min(wait, 120.0)
            if self._global_pause < now + pause:
                log.warning("429s from %d stores recently - pausing all store requests %.0fs", len(recent), pause)
                self._global_pause = now + pause

    # ---------- main entry ----------
    async def get(self, url: str, *, kind: str = "page", check_robots: bool = True,
                  headers: dict | None = None) -> httpx.Response | None:
        """kind: 'page' (store site), 'api' (third-party widget API), 'image' (CDN)."""
        host = urlsplit(url).netloc.lower()
        if check_robots and kind == "page" and not await self.allowed(url):
            self.stats["robots_blocked"] += 1
            log.debug("robots.txt disallows %s", url)
            return None
        resp: httpx.Response | None = None
        retries = 0 if (kind == "page" and (self.fast_fail or host in self.limited_hosts)) else self.s.max_retries
        for attempt in range(retries + 1):
            await self._wait_turn(host, kind)
            try:
                resp = await self.client.get(url, headers=headers)
            except httpx.HTTPError as e:
                self.stats["network_errors"] += 1
                if attempt == retries:
                    log.debug("giving up on %s: %r", url, e)
                    return None
                await asyncio.sleep(2 ** attempt + random.random())
                continue
            self.stats["requests"] += 1
            code = resp.status_code
            if code in (429, 430):
                self.stats["429"] += 1
                if attempt == retries:
                    if kind == "page" and host not in self.limited_hosts:
                        self.limited_hosts.add(host)
                        if len(self.limited_hosts) >= self.s.rate_limit_trip_stores and not self.fast_fail:
                            self.fast_fail = True
                            log.warning("%d stores still rate-limited after retries - this IP looks blocked; "
                                        "remaining blocked stores will be skipped quickly", len(self.limited_hosts))
                    return resp
                wait = _retry_after(resp) or min(30.0 * 2 ** attempt, 300.0)
                self._register_429(host, wait)
                log.info("429 from %s, waiting %.0fs (attempt %d)", host, wait, attempt + 1)
                continue
            if code >= 500 and code not in (501, 505):
                await asyncio.sleep(5 * 2 ** attempt)
                continue
            return resp
        return resp
