"""Outbound network policy — SSRF protection for the HTTP tool.

Default policy: only public, single-label hostnames resolved over DNS are
allowed; every resolved address must be global-unicast. Cloud metadata IPs,
loopback, RFC1918, link-local (including 169.254.169.254), reserved, and
multicast ranges are blocked. Redirects are never followed automatically.
The policy is explicit deployment configuration, never trusted from workflow
input alone.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..errors import FlowcraftError, FailureClass


@dataclass
class NetworkPolicy:
    allow_private: bool = False          # explicit opt-in for self-hosted LAN use
    allow_localhost: bool = False        # explicit opt-in (e.g. local LLM servers)
    allowed_hosts: list[str] = field(default_factory=list)  # if non-empty: allowlist
    denied_hosts: list[str] = field(default_factory=list)
    allowed_schemes: tuple[str, ...] = ("https", "http")

    def check(self, url: str) -> None:
        """Raise FlowcraftError(CONFIG) if the URL is not permitted."""
        parsed = urlparse(url)
        if parsed.scheme not in self.allowed_schemes:
            raise _blocked(f"scheme {parsed.scheme!r} is not allowed", url)
        host = parsed.hostname
        if not host:
            raise _blocked("URL has no hostname", url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        deny = _match_host(host, self.denied_hosts)
        if deny:
            raise _blocked(f"host matches denied pattern {deny!r}", url)

        if self.allowed_hosts:
            if not _match_host(host, self.allowed_hosts):
                raise _blocked("host is not in the configured allowlist", url)

        # Resolve and classify *every* address (defeats multi-A-record bypass).
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise _blocked(f"DNS resolution failed for {host!r}: {e}", url)
        addrs = {i[4][0] for i in infos}
        if not addrs:
            raise _blocked(f"no addresses resolved for {host!r}", url)
        for a in addrs:
            self._check_ip(a, host)

    def _check_ip(self, raw: str, host: str) -> None:
        ip = ipaddress.ip_address(raw)
        if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise _blocked(f"{host} resolves to forbidden address class ({ip})", raw)
        if ip.is_loopback:
            if not self.allow_localhost:
                raise _blocked(
                    f"{host} resolves to loopback {ip}; set allow_localhost to permit", raw
                )
            return
        if ip.is_link_local:
            # Covers 169.254.0.0/16 incl. cloud metadata endpoints.
            raise _blocked(
                f"{host} resolves to link-local {ip} (cloud metadata range is blocked)", raw
            )
        if (ip.is_private or getattr(ip, "is_site_local", False)) and not self.allow_private:
            raise _blocked(
                f"{host} resolves to private address {ip}; set allow_private=true in "
                "the deployment network policy to permit this deliberately",
                raw,
            )


def _match_host(host: str, patterns: list[str]) -> str | None:
    host = host.lower().rstrip(".")
    for p in patterns:
        pl = p.lower().lstrip(".")
        if pl.startswith("*"):
            if host.endswith(pl[1:]) or host == pl[1:].lstrip("."):
                return p
        elif host == pl:
            return p
    return None


def _blocked(reason: str, ctx: str) -> FlowcraftError:
    return FlowcraftError(
        f"request blocked by network policy: {reason}",
        code="ssrf_blocked",
        failure_class=FailureClass.CONFIG,
        details={"context": ctx},
    )
