#!/usr/bin/env python3
"""Codex event bridge for Agent Task Notifier's delivery channels.

No third-party Python packages are required. Secrets are loaded from notify.env
next to this file and are never written to the log.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable
from urllib import parse, request

from channels import bark


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "notify.env"
LOG_FILE = ROOT / "notifier.log"
STATE_DIR = ROOT / ".state" / "user-turns"
HTTP_TIMEOUT_SECONDS = 8
TITLE_BYTES = 360
ANSWER_BYTES = 3000
NEW_CONVERSATION = "新会话"
USER_TURN_MARKER_TTL_SECONDS = 2 * 24 * 60 * 60
USER_TURN_ALLOWLIST_ACTIVE_SECONDS = 14 * 24 * 60 * 60
TITLE_GENERATION_MARKERS = (
    "provide a short title for a task that will be created from that prompt",
    "the title you generate will be shown in the ui to represent the prompt",
)
ACTIVITY_SUMMARY_MARKERS = (
    "you write the one-line activity update displayed beneath an existing codex task title",
    "fill the structured summary field",
)
THREAD_DESCRIPTION_MARKERS = (
    "you are in a fork of an existing codex thread",
    "fill the structured description field",
    "only fill the description field",
)
TITLE_RECONSIDERATION_MARKERS = (
    "possible durable title checkpoint",
    "decide whether the thread's main durable purpose has changed",
    "set title and description to null",
)


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    # Process environment wins, which is useful for CI and one-off tests.
    for key, value in os.environ.items():
        if key.startswith(("FEISHU_", "BARK_", "WECOM_", "NTFY_", "WXPUSHER_", "IMESSAGE_", "NOTIFIER_")):
            values[key] = value
    return values


def log(message: str) -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            rotated = LOG_FILE.with_suffix(".log.1")
            if rotated.exists():
                rotated.unlink()
            LOG_FILE.replace(rotated)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def raw_text(value: Any) -> str:
    """Return event text without asking a model to summarize or rewrite it."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False).strip()


def clip_utf8(value: Any, limit: int) -> str:
    """Byte-limit raw event text so the APNs payload remains below its limit."""
    text = raw_text(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "…"
    clipped = encoded[: limit - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore").rstrip()
    return clipped + suffix


def humanize_text(value: Any, limit: int) -> str:
    """Turn common Codex Markdown/JSON output into a readable push preview."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    text = value.strip()

    # Codex sometimes returns a small JSON result; show its values, not syntax.
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                preferred = [parsed.get(key) for key in ("summary", "answer", "message", "result") if parsed.get(key)]
                if not preferred:
                    preferred = [parsed.get(key) for key in ("title", "description") if parsed.get(key)]
                if preferred:
                    text = " — ".join(str(item) for item in preferred)
        except json.JSONDecodeError:
            pass

    # Keep link labels while removing local paths/URLs and Markdown decoration.
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = text.replace("```", "").replace("`", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def latest_user_message(event: dict[str, Any]) -> str:
    """Extract the latest raw user prompt, removing only desktop wrapper metadata."""
    messages = event.get("input-messages") or event.get("input_messages") or []
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        candidate: Any = item
        if isinstance(item, dict):
            candidate = item.get("text") or item.get("content") or item.get("message") or ""
            if isinstance(candidate, list):
                candidate = " ".join(
                    str(part.get("text", "")) if isinstance(part, dict) else str(part)
                    for part in candidate
                )
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        # Desktop payloads may wrap the real prompt with file/environment metadata.
        if "## My request:" in candidate:
            candidate = candidate.rsplit("## My request:", 1)[-1]
        candidate = re.sub(r"<environment_context>.*?</environment_context>", "", candidate, flags=re.DOTALL)
        candidate = re.sub(r"<image\b.*?</image>", "", candidate, flags=re.DOTALL)
        candidate = candidate.strip()
        if candidate:
            return candidate
    return ""


def internal_metadata_generation_kind(event: dict[str, Any]) -> str:
    """Identify hidden desktop turns that only maintain task-list metadata."""
    event_type = str(event.get("type") or event.get("hook_event_name") or "")
    if event_type != "agent-turn-complete":
        return ""

    messages = event.get("input-messages") or event.get("input_messages") or []
    prompt = raw_text(messages).lower()
    answer = raw_text(event.get("last-assistant-message"))
    try:
        parsed = json.loads(answer)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""

    keys = set(parsed)
    if (
        all(marker in prompt for marker in TITLE_GENERATION_MARKERS)
        and (
            (
                keys <= {"title", "description"}
                and isinstance(parsed.get("title"), str)
                and isinstance(parsed.get("description"), str)
            )
            or (keys == {"suggestions"} and isinstance(parsed.get("suggestions"), list))
        )
    ):
        return "title-generation"
    if (
        all(marker in prompt for marker in ACTIVITY_SUMMARY_MARKERS)
        and (
            "only fill the summary field" in prompt
            or "only fill the summary and compactsummary fields" in prompt
        )
        and keys <= {"summary", "compactSummary"}
        and isinstance(parsed.get("summary"), str)
    ):
        return "activity-summary"
    if (
        all(marker in prompt for marker in THREAD_DESCRIPTION_MARKERS)
        and keys <= {"description"}
        and isinstance(parsed.get("description"), str)
    ):
        return "thread-description"
    if (
        all(marker in prompt for marker in TITLE_RECONSIDERATION_MARKERS)
        and keys <= {"title", "description"}
    ):
        return "title-reconsideration"
    return ""


def is_internal_title_generation(event: dict[str, Any]) -> bool:
    """Backward-compatible helper retained for existing callers and tests."""
    return internal_metadata_generation_kind(event) == "title-generation"


def is_subagent_event(event: dict[str, Any]) -> bool:
    return bool(event.get("agent_id") or event.get("agentId") or event.get("agent-id"))


def _state_scope(codex_home: str | Path | None) -> Path:
    raw_home = str(codex_home or Path.home() / ".codex")
    scope = hashlib.sha256(raw_home.encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / scope


def _turn_marker(scope: Path, session_id: str, turn_id: str) -> Path:
    identity = f"{session_id}\0{turn_id}".encode("utf-8")
    return scope / f"{hashlib.sha256(identity).hexdigest()}.turn"


def _atomic_stamp(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(value, encoding="utf-8")
    temp.replace(path)


def mark_user_turn(event: dict[str, Any], codex_home: str | Path | None = None) -> bool:
    """Record root UserPromptSubmit provenance without retaining prompt content."""
    event_type = str(event.get("hook_event_name") or event.get("hookEventName") or "")
    if event_type != "UserPromptSubmit":
        return False
    if is_subagent_event(event):
        log("event=UserPromptSubmit result=ignored-subagent")
        return False
    session_id = raw_text(
        event.get("session_id") or event.get("session-id") or event.get("thread_id") or event.get("thread-id")
    )
    turn_id = raw_text(event.get("turn_id") or event.get("turn-id"))
    if not session_id or not turn_id:
        return False

    scope = _state_scope(codex_home)
    now = time.time()
    _atomic_stamp(_turn_marker(scope, session_id, turn_id), str(now))
    _atomic_stamp(scope / "active", str(now))
    for marker in scope.glob("*.turn"):
        try:
            if now - marker.stat().st_mtime > USER_TURN_MARKER_TTL_SECONDS:
                marker.unlink()
        except OSError:
            continue
    log("event=UserPromptSubmit result=marked-user-turn")
    return True


def consume_user_turn_marker(
    event: dict[str, Any],
    codex_home: str | Path | None = None,
) -> tuple[bool | None, str]:
    """Return true/false for an active allowlist, or None before its hook is active."""
    scope = _state_scope(codex_home)
    active = scope / "active"
    try:
        if time.time() - active.stat().st_mtime > USER_TURN_ALLOWLIST_ACTIVE_SECONDS:
            return None, "allowlist-inactive"
    except OSError:
        return None, "allowlist-inactive"

    session_id = raw_text(event.get("thread-id") or event.get("thread_id") or event.get("session_id"))
    turn_id = raw_text(event.get("turn-id") or event.get("turn_id"))
    if not session_id or not turn_id:
        return False, "missing-turn-identity"
    marker = _turn_marker(scope, session_id, turn_id)
    try:
        marker.unlink()
    except FileNotFoundError:
        return False, "unmarked-turn"
    except OSError:
        return False, "marker-read-failed"
    return True, "marked-user-turn"


def project_name(event: dict[str, Any], codex_home: str | Path | None = None) -> str:
    """Resolve the Codex sidebar project containing this event's cwd."""
    cwd = raw_text(event.get("cwd"))
    if not cwd:
        return NEW_CONVERSATION

    home = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    try:
        state = json.loads((home / ".codex-global-state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return NEW_CONVERSATION

    projects = state.get("local-projects") if isinstance(state, dict) else None
    if not isinstance(projects, dict):
        return NEW_CONVERSATION

    cwd_path = Path(cwd).expanduser().resolve(strict=False)
    matches: list[tuple[int, str]] = []
    for project in projects.values():
        if not isinstance(project, dict):
            continue
        name = project.get("name")
        roots = project.get("rootPaths")
        if not isinstance(name, str) or not name.strip() or not isinstance(roots, list):
            continue
        for root in roots:
            if not isinstance(root, str) or not root.strip():
                continue
            root_path = Path(root).expanduser().resolve(strict=False)
            if cwd_path == root_path or root_path in cwd_path.parents:
                matches.append((len(root_path.parts), name.strip()))

    return max(matches, default=(0, NEW_CONVERSATION))[1]


def format_event(
    event: dict[str, Any],
    instance_label: str = "",
    codex_home: str | Path | None = None,
) -> tuple[str, str, str]:
    event_type = str(event.get("type") or event.get("hook_event_name") or "unknown")
    project = project_name(event, codex_home)
    now = dt.datetime.now().astimezone().strftime("%m-%d %H:%M")

    if event_type == "agent-turn-complete":
        question = latest_user_message(event)
        title = clip_utf8(question or "Codex 已回复", TITLE_BYTES)
        answer = clip_utf8(event.get("last-assistant-message") or "本轮任务已经结束。", ANSWER_BYTES)
        metadata = [f"项目：{project}"]
        if instance_label:
            metadata.append(f"实例：{instance_label}")
        return title, f"{answer}\n\n" + "\n".join(metadata), event_type
    elif event_type == "PermissionRequest":
        title = "⏸️ Codex 等待你授权"
        tool_name = event.get("tool_name") or "操作"
        tool_input = event.get("tool_input") or {}
        description = tool_input.get("description") if isinstance(tool_input, dict) else None
        detail = description or f"Codex 准备执行 {tool_name}，需要你回到任务中确认。"
    elif event_type == "Stop":
        title = "✅ Codex 已停止执行"
        detail = event.get("last_assistant_message") or "本轮任务已经停止。"
    elif event_type == "notifier-test":
        title = "🧪 Codex 通知测试成功"
        detail = event.get("last-assistant-message") or "看到这条消息，说明远程通知通道已经接通。"
    else:
        title = f"Codex 状态：{event_type}"
        detail = event.get("last-assistant-message") or event.get("last_assistant_message") or "状态已更新。"

    lines = [humanize_text(detail, 1100), f"项目：{project}"]
    if instance_label:
        lines.append(f"实例：{instance_label}")
    lines.append(f"时间：{now}")
    return title, "\n".join(lines), event_type


def http_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    all_headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "agent-task-notifier/1.0"}
    if headers:
        all_headers.update(headers)
    req = request.Request(url, data=data, headers=all_headers, method="POST")
    with request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8", errors="replace")
        if not 200 <= response.status < 300:
            raise RuntimeError(f"HTTP {response.status}")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw[:200]}


def retry(channel: str, operation: Callable[[], None]) -> str:
    started = time.monotonic()
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            operation()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log(f"channel={channel} result=ok duration_ms={elapsed_ms}")
            return f"{channel}: ok"
        except Exception as exc:  # Channel errors must never break Codex itself.
            last_error = exc
            if attempt == 0:
                time.sleep(0.8)
    safe_error = type(last_error).__name__ if last_error else "unknown"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log(f"channel={channel} result=failed error={safe_error} duration_ms={elapsed_ms}")
    return f"{channel}: failed ({safe_error})"


def send_feishu(config: dict[str, str], title: str, body: str) -> None:
    webhook = config["FEISHU_WEBHOOK_URL"]
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": f"{title}\n\n{body}"}}
    secret = config.get("FEISHU_SECRET", "")
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
        signature = base64.b64encode(hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()).decode("ascii")
        payload.update({"timestamp": timestamp, "sign": signature})
    response = http_json(webhook, payload)
    code = response.get("code", response.get("StatusCode", 0))
    if code not in (0, "0", None):
        raise RuntimeError("Feishu rejected the message")


def send_wecom(config: dict[str, str], title: str, body: str) -> None:
    payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n\n{body}"}}
    response = http_json(config["WECOM_WEBHOOK_URL"], payload)
    if response.get("errcode", 0) != 0:
        raise RuntimeError("WeCom rejected the message")


def send_ntfy(config: dict[str, str], title: str, body: str) -> None:
    server = config.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    topic = config["NTFY_TOPIC"].strip("/")
    url = f"{server}/{parse.quote(topic, safe='')}"
    headers = {
        "Title": title,
        "Priority": config.get("NTFY_PRIORITY", "default"),
        "Tags": "white_check_mark,robot_face",
        "User-Agent": "agent-task-notifier/1.0",
    }
    token = config.get("NTFY_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"HTTP {response.status}")


def send_wxpusher(config: dict[str, str], title: str, body: str) -> None:
    payload = {
        "appToken": config["WXPUSHER_APP_TOKEN"],
        "content": f"## {title}\n\n{body}",
        "summary": title[:100],
        "contentType": 3,
        "uids": [uid.strip() for uid in config["WXPUSHER_UIDS"].split(",") if uid.strip()],
    }
    response = http_json("https://wxpusher.zjiecode.com/api/send/message", payload)
    if response.get("code") not in (1000, "1000"):
        raise RuntimeError("WxPusher rejected the message")


def send_imessage(config: dict[str, str], title: str, body: str) -> None:
    target = config["IMESSAGE_TARGET"]
    script = """
on run argv
  set targetAddress to item 1 of argv
  set messageText to item 2 of argv
  tell application "Messages"
    set targetService to first service whose service type = iMessage
    set targetBuddy to buddy targetAddress of targetService
    send messageText to targetBuddy
  end tell
end run
"""
    subprocess.run(
        ["/usr/bin/osascript", "-e", script, target, f"{title}\n\n{body}"],
        check=True,
        timeout=HTTP_TIMEOUT_SECONDS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def configured_channels(config: dict[str, str]) -> list[tuple[str, Callable[[], None]]]:
    title, body = config.pop("__title"), config.pop("__body")
    channels: list[tuple[str, Callable[[], None]]] = []
    if config.get("FEISHU_WEBHOOK_URL"):
        channels.append(("feishu", lambda: send_feishu(config, title, body)))
    if bark.is_configured(config):
        channels.append(("bark", lambda: bark.send(config, title, body)))
    if config.get("WECOM_WEBHOOK_URL"):
        channels.append(("wecom", lambda: send_wecom(config, title, body)))
    if config.get("NTFY_TOPIC"):
        channels.append(("ntfy", lambda: send_ntfy(config, title, body)))
    if config.get("WXPUSHER_APP_TOKEN") and config.get("WXPUSHER_UIDS"):
        channels.append(("wxpusher", lambda: send_wxpusher(config, title, body)))
    if config.get("IMESSAGE_TARGET"):
        channels.append(("imessage", lambda: send_imessage(config, title, body)))
    return channels


def forward_existing_notify(encoded: str, raw_event: str) -> str | None:
    if not encoded:
        return None
    try:
        command = json.loads(encoded)
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("not a string array")
        subprocess.run(command + [raw_event], timeout=5, check=False)
        log("channel=existing-notify result=forwarded")
        return "existing-notify: forwarded"
    except Exception as exc:
        log(f"channel=existing-notify result=failed error={type(exc).__name__}")
        return f"existing-notify: failed ({type(exc).__name__})"


def read_event(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.test:
        event = {
            "type": "agent-turn-complete",
            "cwd": str(Path.cwd()),
            "thread-id": "test-notification",
            "input-messages": ["测试 Codex 任务通知"],
            "last-assistant-message": "这是一条测试通知。正文末尾会显示项目名称和实例标签。",
        }
        return event, json.dumps(event, ensure_ascii=False)
    if args.hook:
        raw = sys.stdin.read()
        return json.loads(raw), raw
    raw = args.payload or (args.extra[-1] if args.extra else "")
    if not raw:
        raise ValueError("missing Codex notification JSON payload")
    return json.loads(raw), raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge Codex events to phone/IM notifications")
    parser.add_argument("--test", action="store_true", help="send a test notification")
    parser.add_argument("--hook", action="store_true", help="read a Codex hook event from stdin")
    parser.add_argument("--mark-user-turn", action="store_true", help="record a UserPromptSubmit turn from stdin")
    parser.add_argument("--codex-notify", action="store_true", help="accept Codex notify JSON as the last argument")
    parser.add_argument("--instance-label", default="", help="label shown in the notification body")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", ""), help="Codex home used to resolve sidebar projects")
    parser.add_argument("--forward-command-json", default="", help="preserved previous notify command")
    parser.add_argument("payload", nargs="?", help="notification JSON")
    parser.add_argument("extra", nargs="*")
    args = parser.parse_args()

    try:
        if args.mark_user_turn:
            raw_event = sys.stdin.read()
            mark_user_turn(json.loads(raw_event), args.codex_home)
            return 0
        event, raw_event = read_event(args)
        event_type = str(event.get("type") or event.get("hook_event_name") or "unknown")
        if event_type == "agent-turn-complete" and not args.test:
            if is_subagent_event(event):
                log("event=agent-turn-complete result=ignored-subagent")
                return 0
            metadata_kind = internal_metadata_generation_kind(event)
            if metadata_kind:
                log(f"event=agent-turn-complete result=ignored-internal-{metadata_kind}")
                return 0
            user_turn, reason = consume_user_turn_marker(event, args.codex_home)
            if user_turn is False:
                log(f"event=agent-turn-complete result=ignored-{reason}")
                return 0
        title, body, event_type = format_event(event, args.instance_label, args.codex_home)
        config = load_env()
        log(f"event={event_type} received")
        channel_config = dict(config)
        channel_config["__title"] = title
        channel_config["__body"] = body
        channels = configured_channels(channel_config)
        results: list[str] = []
        forward_command = args.forward_command_json or config.get("NOTIFIER_FORWARD_COMMAND_JSON", "")
        has_forward = not args.hook and bool(forward_command)
        if channels or has_forward:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(7, len(channels) + int(has_forward))) as pool:
                futures = [pool.submit(retry, name, operation) for name, operation in channels]
                forward_future = pool.submit(forward_existing_notify, forward_command, raw_event) if has_forward else None
                results.extend(future.result() for future in futures)
                if forward_future:
                    forward_result = forward_future.result()
                    if forward_result:
                        results.append(forward_result)
        else:
            log(f"event={event_type} result=no-channels-configured")
            results.append("no channels configured")
        if args.test:
            print("\n".join(results))
            if any("failed" in result or result == "no channels configured" for result in results):
                return 1
        return 0
    except Exception as exc:
        log(f"bridge result=failed error={type(exc).__name__}")
        if args.test:
            print(f"bridge: failed ({type(exc).__name__}); see notifier.log", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
