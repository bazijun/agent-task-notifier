#!/usr/bin/env python3
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock

import setup


class SetupTests(unittest.TestCase):
    def target(self, root: Path, label: str = "codex-test") -> setup.CodexTarget:
        return setup.CodexTarget(root, label)

    def test_parse_bark_full_url(self) -> None:
        server, key = setup.parse_bark_credential("https://api.day.app/device-key/Test message")
        self.assertEqual(server, "https://api.day.app")
        self.assertEqual(key, "device-key")

    def test_android_wizard_defaults_to_ntfy(self) -> None:
        values: dict[str, str] = {}

        def answer(prompt: str, default: str = "") -> str:
            if prompt.startswith("接收设备"):
                return "android"
            if prompt.startswith("选择通道"):
                self.assertEqual(default, "ntfy")
            return default

        with (
            mock.patch.object(setup, "ask", side_effect=answer),
            mock.patch.object(setup, "ask_secret", side_effect=["random-private-topic", ""]),
            redirect_stdout(io.StringIO()),
        ):
            setup.configure_channels(values)
        self.assertEqual(values["NTFY_TOPIC"], "random-private-topic")
        self.assertNotIn("BARK_DEVICE_KEY", values)

    def test_ios_wizard_prompts_to_install_bark(self) -> None:
        values: dict[str, str] = {}
        output = io.StringIO()

        def answer(prompt: str, default: str = "") -> str:
            if prompt.startswith("选择通道"):
                self.assertEqual(default, "bark")
            return default

        with (
            mock.patch.object(setup, "ask", side_effect=answer),
            mock.patch.object(setup, "ask_secret", return_value="test-key"),
            redirect_stdout(output),
        ):
            setup.configure_channels(values)
        self.assertEqual(values["BARK_DEVICE_KEY"], "test-key")
        self.assertIn("先在 iPhone/iPad 安装 Bark App", output.getvalue())

    def test_parse_instance_supports_explicit_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = setup.parse_instance(f"my-codex={temp_dir}")
        self.assertEqual(target.label, "my-codex")

    def test_cockpit_label_uses_instance_id_prefix(self) -> None:
        self.assertEqual(setup.label_for_home(Path("/tmp/abcdef1234567890")), "codex-abcdef")

    def test_write_env_preserves_bark_history_link_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "notify.env"
            values = {
                "BARK_DIRECT": "1",
                "BARK_OPEN_URL": "",
                "BARK_HISTORY_URL": "myremote://",
                "BARK_HISTORY_LINK_TEXT": "打开远程工具",
            }
            with mock.patch.object(setup, "ENV_FILE", env_file):
                setup.write_env(values)
            content = env_file.read_text(encoding="utf-8")
            self.assertIn("BARK_DIRECT=1\n", content)
            self.assertIn("BARK_OPEN_URL=\n", content)
            self.assertIn("BARK_HISTORY_URL=myremote://\n", content)
            self.assertIn("BARK_HISTORY_LINK_TEXT=打开远程工具\n", content)

    def test_install_preserves_non_sky_notify_per_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            config.write_text('model = "test"\nnotify = ["old-program", "turn-ended"]\n', encoding="utf-8")
            setup.install_notify(self.target(root, "codex-extra"))
            command = tomllib.loads(config.read_text(encoding="utf-8"))["notify"]
            self.assertIn("--instance-label", command)
            self.assertEqual(command[command.index("--instance-label") + 1], "codex-extra")
            self.assertEqual(command[command.index("--codex-home") + 1], str(root))
            forwarded = json.loads(command[command.index("--forward-command-json") + 1])
            self.assertEqual(forwarded, ["old-program", "turn-ended"])
            self.assertTrue(list(root.glob("config.toml.before-task-notifier-*")))

    def test_install_keeps_cockpit_sky_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sky = str(root / "computer-use" / "SkyComputerUseClient")
            config = root / "config.toml"
            config.write_text(f"notify = {json.dumps([sky, 'turn-ended'])}\n", encoding="utf-8")
            setup.install_notify(self.target(root, "codex-one"))
            command = tomllib.loads(config.read_text(encoding="utf-8"))["notify"]
            self.assertEqual(command[:2], [sky, "turn-ended"])
            previous = json.loads(command[command.index("--previous-notify") + 1])
            self.assertTrue(setup.is_bridge_command(previous))
            self.assertEqual(previous[previous.index("--instance-label") + 1], "codex-one")
            self.assertEqual(previous[previous.index("--codex-home") + 1], str(root))

    def test_reinstall_is_idempotent_and_updates_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            config.write_text('notify = ["SkyComputerUseClient", "turn-ended"]\n', encoding="utf-8")
            setup.install_notify(self.target(root, "old-label"))
            setup.install_notify(self.target(root, "new-label"))
            command = tomllib.loads(config.read_text(encoding="utf-8"))["notify"]
            previous = json.loads(command[command.index("--previous-notify") + 1])
            self.assertEqual(previous[previous.index("--instance-label") + 1], "new-label")
            self.assertNotIn("--forward-command-json", previous)

    def test_permission_hook_merges_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            hooks_file = root / "hooks.json"
            hooks_file.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": []}]}}), encoding="utf-8")
            target = self.target(root, "codex-two")
            setup.install_permission_hook(target)
            setup.install_permission_hook(target)
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertIn("SessionStart", data["hooks"])
            groups = data["hooks"]["PermissionRequest"]
            self.assertEqual(len(groups), 1)
            command = groups[0]["hooks"][0]["command"]
            self.assertIn("--instance-label codex-two", command)
            self.assertIn(f"--codex-home {root}", command)

    def test_user_prompt_hook_merges_without_replacing_existing_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            hooks_file = root / "hooks.json"
            hooks_file.write_text(json.dumps({
                "hooks": {
                    "UserPromptSubmit": [{
                        "hooks": [{"type": "command", "command": "node existing-hook.mjs"}],
                    }],
                },
            }), encoding="utf-8")
            target = self.target(root, "codex-two")
            setup.install_user_prompt_hook(target)
            setup.install_user_prompt_hook(target)
            groups = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
            commands = [handler["command"] for group in groups for handler in group["hooks"]]
            self.assertIn("node existing-hook.mjs", commands)
            bridge_commands = [command for command in commands if "--mark-user-turn" in command]
            self.assertEqual(len(bridge_commands), 1)
            self.assertIn("--instance-label codex-two", bridge_commands[0])
            self.assertIn(f"--codex-home {root}", bridge_commands[0])

    def test_install_base_handles_multiple_targets_and_drops_legacy_forward(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "one"
            second = root / "two"
            first.mkdir()
            second.mkdir()
            (first / "config.toml").write_text("", encoding="utf-8")
            (second / "config.toml").write_text("", encoding="utf-8")
            env_file = root / "notify.env"
            values = {"BARK_DEVICE_KEY": "secret", "NOTIFIER_FORWARD_COMMAND_JSON": "[]"}
            targets = [self.target(first, "one"), self.target(second, "two")]
            with mock.patch.object(setup, "ENV_FILE", env_file):
                setup.install_base(values, targets)
            self.assertTrue((first / "hooks.json").exists())
            self.assertTrue((second / "hooks.json").exists())
            first_hooks = json.loads((first / "hooks.json").read_text(encoding="utf-8"))["hooks"]
            self.assertIn("PermissionRequest", first_hooks)
            self.assertIn("UserPromptSubmit", first_hooks)
            self.assertNotIn("NOTIFIER_FORWARD_COMMAND_JSON", env_file.read_text(encoding="utf-8"))

    def test_no_permission_hook_still_installs_user_turn_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config.toml").write_text("", encoding="utf-8")
            env_file = root / "notify.env"
            with mock.patch.object(setup, "ENV_FILE", env_file):
                setup.install_base({}, [self.target(root)], include_hook=False)
            hooks = json.loads((root / "hooks.json").read_text(encoding="utf-8"))["hooks"]
            self.assertIn("UserPromptSubmit", hooks)
            self.assertNotIn("PermissionRequest", hooks)


if __name__ == "__main__":
    unittest.main()
