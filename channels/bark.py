"""Bark notification channel.

Keep Bark-specific endpoint parsing, payload fields and HTTP behavior here so
future Bark customization does not require touching the Codex event bridge.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
from typing import Any
from urllib import parse, request


DEFAULT_SERVER = "https://api.day.app"
DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_HISTORY_LINK_TEXT = "打开链接"
DEFAULT_DIRECT_DNS = ("223.5.5.5", "1.1.1.1", "8.8.8.8")
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
MACOS_IP_BOUND_IF = 25


class BarkTransportError(RuntimeError):
    """Raised when both the physical-interface and normal transports fail."""


def is_configured(config: dict[str, str]) -> bool:
    return bool(config.get("BARK_DEVICE_KEY") or config.get("BARK_URL"))


def endpoint(config: dict[str, str]) -> tuple[str, str]:
    """Resolve the Bark V2 push endpoint and device key."""
    server = config.get("BARK_SERVER", DEFAULT_SERVER).rstrip("/")
    device_key = config.get("BARK_DEVICE_KEY", "")
    shared_url = config.get("BARK_URL", "")
    if shared_url and not device_key:
        parts = parse.urlsplit(shared_url)
        if parts.scheme and parts.netloc:
            server = f"{parts.scheme}://{parts.netloc}"
            device_key = parts.path.strip("/").split("/")[0]
    if not device_key:
        raise ValueError("BARK_DEVICE_KEY is missing")
    return f"{server}/push", device_key


def build_payload(config: dict[str, str], title: str, body: str) -> dict[str, Any]:
    """Build the complete Bark payload; customize notification behavior here."""
    _, device_key = endpoint(config)
    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "device_key": device_key,
        "group": config.get("BARK_GROUP", "Codex"),
        "level": config.get("BARK_LEVEL", "active"),
    }
    optional_fields = {
        "icon": "BARK_ICON",
        "url": "BARK_OPEN_URL",
        "sound": "BARK_SOUND",
    }
    for payload_key, config_key in optional_fields.items():
        if config.get(config_key):
            payload[payload_key] = config[config_key]

    history_url = config.get("BARK_HISTORY_URL", "").strip()
    if history_url:
        link_text = config.get("BARK_HISTORY_LINK_TEXT", DEFAULT_HISTORY_LINK_TEXT).strip()
        link_text = link_text or DEFAULT_HISTORY_LINK_TEXT
        # Bark archives the Markdown source and renders this as a tappable link in
        # history. Keeping BARK_OPEN_URL empty prevents a notification tap from
        # opening the URL; Bark then follows its default route to message history.
        payload.pop("body", None)
        payload["markdown"] = f"{body}\n\n[{link_text}]({history_url})"
    return payload


def _is_enabled(value: str | None, default: bool = True) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _physical_interface(config: dict[str, str]) -> str:
    """Return the underlying macOS default interface, ignoring split TUN routes."""
    configured = config.get("BARK_DIRECT_INTERFACE", "").strip()
    if configured:
        return configured
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            ["/sbin/route", "-n", "get", "default"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        match = re.search(r"^\s*interface:\s*(\S+)\s*$", result.stdout, re.MULTILINE)
        if match and not match.group(1).startswith("utun"):
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    for candidate in ("en0", "en1", "en4", "en5", "en6"):
        try:
            socket.if_nametoindex(candidate)
            return candidate
        except OSError:
            continue
    return ""


def _bind_to_interface(sock: socket.socket, interface: str) -> None:
    if sys.platform != "darwin" or not interface:
        return
    index = socket.if_nametoindex(interface)
    sock.setsockopt(socket.IPPROTO_IP, MACOS_IP_BOUND_IF, struct.pack("I", index))


def _dns_question(host: str, query_id: int) -> bytes:
    labels = host.rstrip(".").split(".")
    name = b"".join(bytes([len(label.encode("idna"))]) + label.encode("idna") for label in labels) + b"\0"
    return struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + name + struct.pack("!HH", 1, 1)


def _skip_dns_name(data: bytes, offset: int) -> int:
    while offset < len(data):
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated DNS pointer")
            return offset + 2
        offset += 1 + length
    raise ValueError("truncated DNS name")


def _parse_dns_ipv4(data: bytes, query_id: int) -> list[str]:
    if len(data) < 12:
        raise ValueError("short DNS response")
    response_id, flags, questions, answers = struct.unpack("!HHHH", data[:8])
    if response_id != query_id or flags & 0x8000 == 0 or flags & 0x000F:
        raise ValueError("invalid DNS response")
    offset = 12
    for _ in range(questions):
        offset = _skip_dns_name(data, offset) + 4
    addresses: list[str] = []
    for _ in range(answers):
        offset = _skip_dns_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("truncated DNS answer")
        record_type, record_class, _, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        value = data[offset:offset + length]
        offset += length
        if record_type == 1 and record_class == 1 and length == 4:
            addresses.append(socket.inet_ntoa(value))
    return addresses


def _direct_dns_ipv4(host: str, interface: str, server: str, timeout: float) -> list[str]:
    query_id = random.SystemRandom().randrange(65536)
    packet = _dns_question(host, query_id)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        _bind_to_interface(sock, interface)
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)
        return _parse_dns_ipv4(data, query_id)
    finally:
        sock.close()


def _usable_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and address not in FAKE_IP_NETWORK


def _direct_ipv4_addresses(config: dict[str, str], host: str, interface: str, timeout: float) -> list[str]:
    addresses: list[str] = []
    configured_ips = config.get("BARK_DIRECT_IPS", "")
    addresses.extend(item.strip() for item in configured_ips.split(",") if item.strip())

    configured_dns = config.get("BARK_DIRECT_DNS", "")
    dns_servers = tuple(item.strip() for item in configured_dns.split(",") if item.strip()) or DEFAULT_DIRECT_DNS
    dns_timeout = max(0.5, min(2.0, timeout / max(2, len(dns_servers))))
    for server in dns_servers:
        try:
            addresses.extend(_direct_dns_ipv4(host, interface, server, dns_timeout))
            if any(_usable_ipv4(item) for item in addresses):
                break
        except (OSError, ValueError):
            continue

    try:
        addresses.extend(item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM))
    except OSError:
        pass
    return list(dict.fromkeys(item for item in addresses if _usable_ipv4(item)))


def _direct_post(
    config: dict[str, str],
    url: str,
    data: bytes,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    parts = parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("physical direct transport requires an HTTPS host")
    interface = _physical_interface(config)
    if sys.platform == "darwin" and not interface:
        raise OSError("physical network interface was not found")
    port = parts.port or 443
    addresses = _direct_ipv4_addresses(config, parts.hostname, interface, timeout)
    if not addresses:
        raise OSError(f"no usable direct address for {parts.hostname}")

    path = parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    request_headers = dict(headers)
    request_headers.update({
        "Host": parts.hostname if port == 443 else f"{parts.hostname}:{port}",
        "Content-Length": str(len(data)),
        "Connection": "close",
    })
    raw_headers = "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
    wire_request = f"POST {path} HTTP/1.1\r\n{raw_headers}\r\n".encode("latin1") + data

    errors: list[str] = []
    per_address_timeout = max(2.0, timeout / max(1, len(addresses)))
    for address in addresses:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tls_sock: ssl.SSLSocket | None = None
        try:
            sock.settimeout(per_address_timeout)
            _bind_to_interface(sock, interface)
            sock.connect((address, port))
            tls_sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
            tls_sock.sendall(wire_request)
            response = http.client.HTTPResponse(tls_sock)
            response.begin()
            return response.status, response.read()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            errors.append(f"{address}:{type(exc).__name__}")
        finally:
            if tls_sock is not None:
                tls_sock.close()
            else:
                sock.close()
    raise OSError("direct Bark connection failed: " + ", ".join(errors))


def _standard_post(url: str, data: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """Fallback without HTTP_PROXY/HTTPS_PROXY; system-level routing may still apply."""
    opener = request.build_opener(request.ProxyHandler({}))
    req = request.Request(url, data=data, headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as response:
        return response.status, response.read()


def send(
    config: dict[str, str],
    title: str,
    body: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    url, _ = endpoint(config)
    payload = build_payload(config, title, body)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "agent-task-notifier/1.0",
    }
    direct_error: Exception | None = None
    if _is_enabled(config.get("BARK_DIRECT"), default=True):
        try:
            status, raw_bytes = _direct_post(config, url, data, headers, timeout)
        except Exception as exc:
            direct_error = exc
        else:
            direct_error = None
    if direct_error is not None or not _is_enabled(config.get("BARK_DIRECT"), default=True):
        try:
            status, raw_bytes = _standard_post(url, data, headers, timeout)
        except Exception as fallback_error:
            if direct_error is not None:
                raise BarkTransportError(
                    f"direct={type(direct_error).__name__}; fallback={type(fallback_error).__name__}"
                ) from fallback_error
            raise

    raw = raw_bytes.decode("utf-8", errors="replace")
    if not 200 <= status < 300:
        raise RuntimeError(f"Bark HTTP {status}")
    if raw.strip():
        result = json.loads(raw)
        if result.get("code", 200) not in (200, "200"):
            raise RuntimeError("Bark rejected the message")
