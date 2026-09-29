#!/usr/bin/env python3
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest import mock

import notify_bridge as bridge
from channels import bark


class NotifyBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_home.cleanup)
        self.codex_home = Path(self.temp_home.name)
        self.state_patch = mock.patch.object(bridge, "STATE_DIR", self.codex_home / ".state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        (self.codex_home / ".codex-global-state.json").write_text(json.dumps({
            "local-projects": {
                "one": {"name": "my-project", "rootPaths": ["/tmp/my-project"]},
                "two": {"name": "test", "rootPaths": ["/tmp/test"]},
            },
        }), encoding="utf-8")

    def test_load_env_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "notify.env"
            path.write_text("# comment\nFEISHU_WEBHOOK_URL=https://example.test/hook\nEMPTY=\n", encoding="utf-8")
            values = bridge.load_env(path)
            self.assertEqual(values["FEISHU_WEBHOOK_URL"], "https://example.test/hook")

    def test_test_notification_fails_without_exposing_channel_error(self) -> None:
        output = io.StringIO()
        with (
            mock.patch("sys.argv", ["notify_bridge.py", "--test"]),
            mock.patch.object(bridge, "load_env", return_value={"BARK_DEVICE_KEY": "test-key"}),
            mock.patch.object(bridge.bark, "send", side_effect=RuntimeError("private-url-or-key")),
            mock.patch.object(bridge.time, "sleep"),
            mock.patch.object(bridge, "log"),
            redirect_stdout(output),
        ):
            result = bridge.main()
        self.assertEqual(result, 1)
        self.assertIn("bark: failed (RuntimeError)", output.getvalue())
        self.assertNotIn("private-url-or-key", output.getvalue())

    def test_complete_event_format(self) -> None:
        title, body, event_type = bridge.format_event({
            "type": "agent-turn-complete",
            "cwd": "/tmp/my-project",
            "input-messages": ["Please run all tests"],
            "last-assistant-message": "[Report](/tmp/report.md): **All tests passed**",
            "thread-id": "thread-1234567890",
        }, codex_home=self.codex_home)
        self.assertEqual(title, "Please run all tests")
        self.assertEqual(body, "[Report](/tmp/report.md): **All tests passed**\n\n项目：my-project")
        self.assertEqual(event_type, "agent-turn-complete")

    def test_json_answer_is_kept_raw(self) -> None:
        title, body, _ = bridge.format_event({
            "type": "agent-turn-complete",
            "cwd": "/tmp/test",
            "input-messages": ["测试连接"],
            "last-assistant-message": '{"summary":"连接正常，测试通过"}',
        }, codex_home=self.codex_home)
        self.assertEqual(title, "测试连接")
        self.assertEqual(body, '{"summary":"连接正常，测试通过"}\n\n项目：test')

    def test_wrapped_prompt_uses_my_request(self) -> None:
        title, body, _ = bridge.format_event({
            "type": "agent-turn-complete",
            "cwd": "/tmp/test",
            "input-messages": ["<environment_context>noise</environment_context>\n## My request:\n帮我修复登录问题"],
            "last-assistant-message": "已经修复。",
        }, codex_home=self.codex_home)
        self.assertEqual(title, "帮我修复登录问题")
        self.assertEqual(body, "已经修复。\n\n项目：test")

    def test_missing_project_uses_free_chat(self) -> None:
        title, body, _ = bridge.format_event({
            "type": "agent-turn-complete",
            "input-messages": ["你好"],
            "last-assistant-message": "你好，有什么可以帮你？",
        }, codex_home=self.codex_home)
        self.assertEqual(title, "你好")
        self.assertTrue(body.endswith("项目：新会话"))

    def test_unregistered_cwd_uses_new_conversation(self) -> None:
        _, body, _ = bridge.format_event({
            "type": "agent-turn-complete",
            "cwd": "/tmp/generated-from-first-message",
            "input-messages": ["你好"],
            "last-assistant-message": "你好",
        }, codex_home=self.codex_home)
        self.assertTrue(body.endswith("项目：新会话"))

    def test_project_uses_sidebar_name_and_matches_child_path(self) -> None:
        event = {"cwd": "/tmp/my-project/packages/app"}
        self.assertEqual(bridge.project_name(event, self.codex_home), "my-project")

    def test_internal_title_generation_is_ignored(self) -> None:
        event = {
            "type": "agent-turn-complete",
            "input-messages": [
                "You are a helpful assistant. You will be presented with a user prompt, and your job is to provide a short title for a task that will be created from that prompt. "
                "The title you generate will be shown in the UI to represent the prompt."
            ],
            "last-assistant-message": '{"description":"Run a test task","title":"Run test"}',
        }
        self.assertTrue(bridge.is_internal_title_generation(event))

    def test_title_suggestions_and_subagent_do_not_consume_user_turn(self) -> None:
        prompt = (
            "You are a helpful assistant. You will be presented with a user prompt, and your job is to "
            "provide a short title for a task that will be created from that prompt. "
            "The title you generate will be shown in the UI to represent the prompt."
        )
        user_turn = {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "turn-1",
            "input-messages": ["帮我修复通知"],
            "last-assistant-message": "修复完成。",
        }
        title_suggestions = {
            **user_turn,
            "input-messages": [prompt],
            "last-assistant-message": '{"suggestions":[]}',
        }
        subagent = {**user_turn, "agent-id": "worker-1"}
        self.assertEqual(bridge.internal_metadata_generation_kind(title_suggestions), "title-generation")
        with (
            mock.patch.object(bridge, "log"),
            mock.patch("sys.argv", ["notify_bridge.py", "--codex-home", str(self.codex_home)]),
            mock.patch.object(bridge, "read_event", side_effect=[
                (title_suggestions, "{}"), (subagent, "{}"), (user_turn, "{}"),
            ]),
            mock.patch.object(bridge, "load_env", return_value={"BARK_DEVICE_KEY": "test-key"}),
            mock.patch.object(bridge.bark, "send") as send,
        ):
            self.assertTrue(bridge.mark_user_turn({
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-1",
                "turn_id": "turn-1",
            }, self.codex_home))
            self.assertEqual(bridge.main(), 0)
            self.assertEqual(bridge.main(), 0)
            send.assert_not_called()
            self.assertEqual(bridge.main(), 0)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[1], "帮我修复通知")

    def test_quoted_title_prompt_with_normal_answer_is_not_ignored(self) -> None:
        event = {
            "type": "agent-turn-complete",
            "input-messages": [
                "Why did I receive: provide a short title for a task that will be created from that prompt; "
                "the title you generate will be shown in the UI to represent the prompt"
            ],
            "last-assistant-message": "这是内部标题生成任务。",
        }
        self.assertFalse(bridge.is_internal_title_generation(event))

    def test_internal_activity_summary_is_ignored(self) -> None:
        event = {
            "type": "agent-turn-complete",
            "input-messages": [
                "You write the one-line activity update displayed beneath an existing Codex task title.\n"
                "Fill the structured summary field with one plain-text sentence of at most 280 characters.\n"
                "Do not answer the request or perform additional work; only fill the summary field."
            ],
            "last-assistant-message": '{"summary":"已完成真实任务"}',
        }
        self.assertEqual(bridge.internal_metadata_generation_kind(event), "activity-summary")

    def test_internal_activity_summary_with_compact_summary_is_ignored(self) -> None:
        event = {
            "type": "agent-turn-complete",
            "input-messages": [
                "You write the one-line activity update displayed beneath an existing Codex task title.\n"
                "Fill the structured summary field with one plain-text sentence.\n"
                "Do not answer the request or perform additional work; only fill the summary and compactSummary fields."
            ],
            "last-assistant-message": '{"summary":"完成","compactSummary":"完成"}',
        }
        self.assertEqual(bridge.internal_metadata_generation_kind(event), "activity-summary")

    def test_activity_prompt_quoted_by_user_is_not_ignored(self) -> None:
        event = {
            "type": "agent-turn-complete",
            "input-messages": [
                "为什么我收到 You write the one-line activity update displayed beneath an existing Codex task title，"
                "以及 fill the structured summary field？"
            ],
            "last-assistant-message": "这是桌面端内部活动摘要。",
        }
        self.assertEqual(bridge.internal_metadata_generation_kind(event), "")

    def test_user_prompt_marker_allows_exactly_one_matching_completion(self) -> None:
        hook_event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "thread-1",
            "turn_id": "turn-1",
            "prompt": "帮我修复问题",
        }
        notify_event = {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "turn-1",
        }
        self.assertTrue(bridge.mark_user_turn(hook_event, self.codex_home))
        self.assertEqual(
            bridge.consume_user_turn_marker(notify_event, self.codex_home),
            (True, "marked-user-turn"),
        )
        self.assertEqual(
            bridge.consume_user_turn_marker(notify_event, self.codex_home),
            (False, "unmarked-turn"),
        )

    def test_allowlist_is_inactive_before_first_user_prompt_hook(self) -> None:
        decision = bridge.consume_user_turn_marker({
            "thread-id": "thread-1",
            "turn-id": "turn-1",
        }, self.codex_home)
        self.assertEqual(decision, (None, "allowlist-inactive"))

    def test_subagent_prompt_does_not_activate_user_allowlist(self) -> None:
        marked = bridge.mark_user_turn({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "thread-1",
            "turn_id": "turn-1",
            "agent_id": "subagent-1",
            "prompt": "internal work",
        }, self.codex_home)
        self.assertFalse(marked)
        self.assertEqual(
            bridge.consume_user_turn_marker({"thread-id": "thread-1", "turn-id": "turn-1"}, self.codex_home),
            (None, "allowlist-inactive"),
        )

    def test_instance_label_is_appended(self) -> None:
        _, body, _ = bridge.format_event({
            "type": "agent-turn-complete",
            "cwd": "/tmp/test",
            "input-messages": ["测试"],
            "last-assistant-message": "完成",
        }, "codex-one", self.codex_home)
        self.assertTrue(body.endswith("项目：test\n实例：codex-one"))

    def test_permission_event_format(self) -> None:
        title, body, _ = bridge.format_event({
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"description": "允许部署到服务器吗？"},
        })
        self.assertIn("等待你授权", title)
        self.assertIn("允许部署", body)

    def test_configured_channels(self) -> None:
        config = {
            "__title": "T",
            "__body": "B",
            "FEISHU_WEBHOOK_URL": "https://example.test/feishu",
            "NTFY_TOPIC": "random-topic",
        }
        names = [name for name, _ in bridge.configured_channels(config)]
        self.assertEqual(names, ["feishu", "ntfy"])

    def test_forward_command_requires_string_array(self) -> None:
        result = bridge.forward_existing_notify(json.dumps({"bad": True}), "{}")
        self.assertIn("failed", result or "")

    def test_bark_payload_customization(self) -> None:
        config = {
            "BARK_SERVER": "https://api.day.app",
            "BARK_DEVICE_KEY": "test-key",
            "BARK_GROUP": "Codex Tasks",
            "BARK_ICON": "https://example.test/codex.png",
            "BARK_OPEN_URL": "myremote://",
            "BARK_SOUND": "minuet",
        }
        payload = bark.build_payload(config, "Codex 已回复", "测试内容")
        self.assertEqual(payload["device_key"], "test-key")
        self.assertEqual(payload["group"], "Codex Tasks")
        self.assertEqual(payload["icon"], "https://example.test/codex.png")
        self.assertEqual(payload["url"], "myremote://")
        self.assertEqual(payload["sound"], "minuet")

    def test_bark_history_link_does_not_set_notification_open_url(self) -> None:
        config = {
            "BARK_DEVICE_KEY": "test-key",
            "BARK_HISTORY_URL": "myremote://",
        }
        payload = bark.build_payload(config, "Codex 已回复", "测试内容")
        self.assertNotIn("url", payload)
        self.assertNotIn("body", payload)
        self.assertEqual(payload["markdown"], "测试内容\n\n[打开链接](myremote://)")

    def test_bark_full_url_compatibility(self) -> None:
        url, key = bark.endpoint({"BARK_URL": "https://push.example.test/device-key/test"})
        self.assertEqual(url, "https://push.example.test/push")
        self.assertEqual(key, "device-key")

    def test_bark_direct_dns_parser_returns_real_ipv4(self) -> None:
        query_id = 1234
        question = bark._dns_question("api.day.app", query_id)[12:]
        answer = (
            b"\xc0\x0c"
            + struct.pack("!HHIH", 1, 1, 60, 4)
            + socket.inet_aton("43.155.109.24")
        )
        response = struct.pack("!HHHHHH", query_id, 0x8180, 1, 1, 0, 0) + question + answer
        self.assertEqual(bark._parse_dns_ipv4(response, query_id), ["43.155.109.24"])

    def test_bark_direct_addresses_drop_fake_ip(self) -> None:
        config = {
            "BARK_DIRECT_IPS": "198.18.0.57,43.155.109.24",
            "BARK_DIRECT_DNS": "223.5.5.5",
        }
        with (
            mock.patch.object(bark, "_direct_dns_ipv4", return_value=[]),
            mock.patch.object(bark.socket, "getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.58", 0)),
            ]),
        ):
            addresses = bark._direct_ipv4_addresses(config, "api.day.app", "en0", 8)
        self.assertEqual(addresses, ["43.155.109.24"])

    def test_bark_send_prefers_physical_direct_transport(self) -> None:
        config = {"BARK_DEVICE_KEY": "test-key"}
        with (
            mock.patch.object(bark, "_direct_post", return_value=(200, b'{"code":200}')) as direct,
            mock.patch.object(bark, "_standard_post") as standard,
        ):
            bark.send(config, "title", "body")
        direct.assert_called_once()
        standard.assert_not_called()

    def test_bark_send_falls_back_without_environment_proxy(self) -> None:
        config = {"BARK_DEVICE_KEY": "test-key"}
        with (
            mock.patch.object(bark, "_direct_post", side_effect=OSError("direct failed")),
            mock.patch.object(bark, "_standard_post", return_value=(200, b'{"code":200}')) as standard,
        ):
            bark.send(config, "title", "body")
        standard.assert_called_once()


if __name__ == "__main__":
    unittest.main()
