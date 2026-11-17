"""Minimal robots.txt parser with wildcard (*, $) support, longest-match wins."""
from __future__ import annotations

import re


class Robots:
    def __init__(self, text: str | None = None, disallow_all: bool = False):
        self.disallow_all = disallow_all
        self.rules: list[tuple[bool, re.Pattern, int]] = []
        self.sitemaps: list[str] = []
        self.crawl_delay: float | None = None
        if text:
            self._parse(text)

    @staticmethod
    def _compile(path: str) -> re.Pattern:
        anchored = path.endswith("$")
        if anchored:
            path = path[:-1]
        rx = re.escape(path).replace(r"\*", ".*")
        return re.compile(rx + ("$" if anchored else ""))

    def _parse(self, text: str) -> None:
        groups: list[tuple[list[str], list[tuple[bool, str]], float | None]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        delay: float | None = None
        last_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "sitemap":
                self.sitemaps.append(val)
                continue
            if key == "user-agent":
                if not last_agent and agents:
                    groups.append((agents, rules, delay))
                    agents, rules, delay = [], [], None
                agents.append(val.lower())
                last_agent = True
                continue
            last_agent = False
            if key in ("allow", "disallow") and val:
                rules.append((key == "allow", val))
            elif key == "crawl-delay":
                try:
                    delay = float(val)
                except ValueError:
                    pass
        if agents:
            groups.append((agents, rules, delay))

        for g_agents, g_rules, g_delay in groups:
            if "*" in g_agents:
                self.rules += [(allow, self._compile(p), len(p)) for allow, p in g_rules]
                if g_delay is not None:
                    self.crawl_delay = g_delay

    def can_fetch(self, path_and_query: str) -> bool:
        if self.disallow_all:
            return False
        best: tuple[bool, int] | None = None
        for allow, rx, length in self.rules:
            if rx.match(path_and_query):
                if best is None or length > best[1] or (length == best[1] and allow):
                    best = (allow, length)
        return True if best is None else best[0]
