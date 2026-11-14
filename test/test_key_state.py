"""键盘按下状态追踪的单元测试（打桩，不真的发按键、不写日志）。

运行：
    .\\.venv\\Scripts\\python.exe test\\test_key_state.py -v
"""
import os
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BASE_DIR, os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _install_null_logger():
    if "src.utils.logger" in sys.modules:
        return
    mod = types.ModuleType("src.utils.logger")

    class _NullLogger:
        def __getattr__(self, name):
            return lambda *a, **k: None

    mod.logger = _NullLogger()
    mod.MSLogger = object
    sys.modules["src.utils.logger"] = mod


_install_null_logger()

from src.input import KeyBoardController as kbc  # noqa: E402
from src.input import KeyBoardListener as kbl  # noqa: E402


class KeyStateTrackerTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        for stub in (
            patch.object(kbc, "HAS_PYDIRECTINPUT", False),
            patch.object(kbc.pyautogui, "keyDown", side_effect=lambda k: self.events.append(("down", k))),
            patch.object(kbc.pyautogui, "keyUp", side_effect=lambda k: self.events.append(("up", k))),
            patch.object(kbc.time, "sleep"),
        ):
            stub.start()
            self.addCleanup(stub.stop)
        kbc._PRESSED_KEYS.clear()
        self.addCleanup(kbc._PRESSED_KEYS.clear)

        with patch.object(kbc.threading.Thread, "start"):
            self.kb = kbc.KeyBoardController({
                "game_window": {"title": "test"},
                "key": {"directional_attack": "x", "aoe_skill": "a", "jump": "alt",
                        "teleport": "e", "add_hp": "end", "add_mp": "home"},
                "bot": {"attack": "directional"},
            })
        self.kb.is_game_window_active = lambda: True

    def tick(self, command):
        self.kb.set_command(command)
        self.kb._tick()

    def test_tracks_down_and_up(self):
        self.assertEqual(kbc.get_pressed_keys(), [])
        kbc.key_down("left")
        kbc.key_down("up")
        self.assertEqual(kbc.get_pressed_keys(), ["left", "up"])
        kbc.key_up("left")
        self.assertEqual(kbc.get_pressed_keys(), ["up"])

    def test_repeated_key_down_keeps_single_entry(self):
        kbc.key_down("left")
        kbc.key_down("left")
        self.assertEqual(kbc.get_pressed_keys(), ["left"])
        self.assertEqual(self.events, [("down", "left")])

    def test_press_key_is_transient(self):
        kbc.press_key("alt", duration=0)
        self.assertEqual(kbc.get_pressed_keys(), [], "press_key 结束后不应残留按下状态")
        self.assertEqual(self.events, [("down", "alt"), ("up", "alt")])

    def test_key_up_unknown_key_is_noop(self):
        kbc.key_up("numpad5")
        self.assertEqual(kbc.get_pressed_keys(), [])
        self.assertEqual(self.events, [])

    def test_release_all_clears_tracked_transients(self):
        kbc.key_down("left")
        kbc.key_down("alt")          # 模拟持续按住中的瞬时键（异常情况）
        self.events.clear()
        self.kb.release_all_key()
        self.kb.release_all_key()

        self.assertEqual(kbc.get_pressed_keys(), [])
        self.assertCountEqual(self.events, [("up", "left"), ("up", "alt")])

    def test_idle_walk_reverse_stop(self):
        for command in ("none none none", "left none none", "left none none",
                        "right none none", "stop stop stop", "stop stop stop"):
            self.tick(command)
        self.assertEqual(self.events, [
            ("down", "left"), ("up", "left"), ("down", "right"), ("up", "right"),
        ])

    def test_attack_only_held_when_allowed_and_focused(self):
        self.kb.game_has_higher_integrity = True
        self.tick("face_right none attack")
        self.assertIn("privileges", self.kb.input_status)
        self.assertEqual(self.events, [])
        self.assertEqual(self.kb.pressed_keys(), [])
        self.kb.game_has_higher_integrity = False
        self.kb.is_game_window_active = lambda: False
        self.kb._tick()
        self.assertEqual(self.kb.input_status, "Waiting for game focus")
        self.assertEqual(self.events, [])
        self.kb.is_game_window_active = lambda: True
        self.kb._tick()
        self.assertEqual(self.kb.pressed_keys(), ["x"])
        self.kb.is_game_window_active = lambda: False
        self.kb._tick()
        self.assertEqual(self.events[-2:], [("down", "x"), ("up", "x")])

    def test_turn_and_tap_while_climbing(self):
        self.tick("right up none")
        self.events.clear()
        for _ in range(3):
            self.tick("face_left up jump")
        self.assertEqual(self.events, [
            ("up", "right"), ("down", "left"), ("up", "left"),
            ("down", "alt"), ("up", "alt"),
        ])
        self.assertEqual(self.kb.pressed_keys(), ["up"])
        self.events.clear()
        self.tick("none down none")
        self.tick("face_left down jump")
        self.assertEqual(self.events, [
            ("up", "up"), ("down", "down"), ("down", "left"), ("up", "left"),
            ("down", "alt"), ("up", "alt"),
        ])

    def test_ladder_grab_taps_once_then_holds_up_until_exit(self):
        self.kb.cfg["key"]["jump"] = "c"
        self.tick("right none none")
        for _ in range(3):
            self.tick("none none grab_ladder")
        with patch.object(kbc.time, "monotonic", return_value=self.kb._climb_until):
            self.tick("none none attack")
        self.assertEqual(self.events, [("down", "right"), ("up", "right"), ("down", "up"),
                                      ("down", "c"), ("up", "c"), ("up", "up"), ("down", "x")])

    def test_side_grab_overlaps_direction_jump_and_up_then_releases_direction(self):
        self.kb.cfg["key"]["jump"] = "c"
        with patch.object(kbc.time, "monotonic", side_effect=[1, 1.007, 1.08, 1.25]):
            self.tick("left none grab_ladder")
            self.tick("left none grab_ladder")
        self.tick("stop stop stop")
        self.assertEqual(self.events, [("down", "left"), ("down", "up"), ("down", "c"),
                                       ("up", "c"), ("up", "left"), ("up", "up")])

    def test_attack_and_other_commands_cannot_interrupt_first_second(self):
        self.kb.cfg["key"]["jump"] = "c"
        with patch.object(kbc.time, "monotonic", return_value=1), patch.object(
                kbc.time, "sleep", side_effect=lambda _: self.kb.set_command("none none attack")):
            self.tick("right none grab_ladder")
        with patch.object(kbc.time, "monotonic", return_value=1.999):
            for command in ("left none none", "none none attack", "none none drop_through", "none none none"):
                self.tick(command)
                self.assertEqual(self.kb.pressed_keys(), ["up"])
            self.assertFalse(self.kb.press_key_safe("z"))
        with patch.object(kbc.time, "monotonic", return_value=2):
            self.tick("left none none")
        self.assertEqual(self.kb.pressed_keys(), ["left"])
        self.assertEqual(self.events.count(("down", "c")), 1)

    def test_ladder_holds_up_before_jump(self):
        self.kb.cfg["key"]["jump"] = "c"
        holds = []
        with patch.object(kbc.time, "sleep", side_effect=lambda duration: holds.append(
                (duration, self.kb.pressed_keys()))):
            self.tick("none none grab_ladder")
            self.tick("none none grab_ladder")
        self.assertEqual(holds, [(0.007, ["up"]), (0.03, ["c", "up"])])
        self.assertEqual(self.kb.pressed_keys(), ["up"])

    def test_focus_loss_overrides_active_ladder_protection(self):
        self.tick("none none grab_ladder")
        self.assertEqual(self.kb.pressed_keys(), ["up"])
        self.kb.is_game_window_active = lambda: False
        self.tick("none none grab_ladder")
        self.assertEqual(self.kb.pressed_keys(), [])
        self.kb.is_game_window_active = lambda: True
        self.tick("left none none")
        self.assertEqual(self.kb.pressed_keys(), ["left"])

    def test_focus_lost_during_ladder_lead_releases_up_without_jumping(self):
        self.kb.cfg["key"]["jump"] = "c"
        with patch.object(kbc.time, "sleep", side_effect=lambda _: setattr(
                self.kb, "is_game_window_active", lambda: False)):
            self.tick("none none grab_ladder")
        self.assertEqual(self.events, [("down", "up"), ("up", "up")])

    def test_drop_releases_walk_and_taps_jump_once_with_down_held(self):
        self.kb.cfg["key"]["jump"] = "c"
        self.tick("right none none")
        for _ in range(3):
            self.tick("none none drop_through")
        self.tick("left none none")
        self.assertEqual(self.events, [("down", "right"), ("up", "right"), ("down", "down"),
                                       ("down", "c"), ("up", "c"), ("up", "down"), ("down", "left")])

    def test_double_actions_fire_once_on_entry(self):
        for action, key in (("double_jump", "alt"), ("double_teleport", "e")):
            with self.subTest(action=action):
                self.events.clear()
                self.tick(f"none none {action}")
                self.tick(f"none none {action}")
                self.assertEqual(self.events, [("down", key), ("up", key)] * 2)

    def test_switch_attack_keys_while_walking(self):
        for action in ("attack", "attack", "aoe", "aoe", "none"):
            self.tick(f"left none {action}")
        self.assertEqual(self.events, [("down", "left"), ("down", "x"), ("up", "x"),
                                       ("down", "a"), ("up", "a")])

    def test_shared_attack_key_is_held_until_exit(self):
        self.kb.attack_key = self.kb.aoe_key = "s"
        for action in ("attack", "aoe", "attack", "none", "none", "aoe", "jump"):
            self.tick(f"none none {action}")
        self.assertEqual(self.events, [("down", "s"), ("up", "s"), ("down", "s"),
                                       ("up", "s"), ("down", "alt"), ("up", "alt")])

    def test_jump_attack_taps_in_order_and_shares_cooldown(self):
        self.kb.jump_attack = True
        self.kb.cfg["key"]["jump"] = "c"
        self.kb.attack_key = self.kb.aoe_key = "s"
        with patch.object(kbc.time, "monotonic", side_effect=[1, 1.1, 1.7]):
            self.tick("none none attack")
            self.tick("none none aoe")
            self.tick("none none aoe")
            self.tick("none none none")
        self.assertEqual(self.events, [("down", "c"), ("up", "c"), ("down", "s"), ("up", "s")] * 2)
        self.assertEqual(self.kb.pressed_keys(), [])

    def test_jump_attack_stops_if_target_lost_during_jump(self):
        self.kb.jump_attack = True
        self.kb.cfg["key"]["jump"] = "c"
        with patch.object(kbc.time, "sleep", side_effect=lambda _: self.kb.set_command("none none none")):
            self.tick("none none attack")
        self.assertEqual(self.events, [("down", "c"), ("up", "c")])

    def test_attack_restarts_after_turn_but_not_for_same_facing_or_deadzone(self):
        self.kb.attack_key = self.kb.aoe_key = "s"
        for cmd in ("face_right none attack", "none none attack", "face_right none aoe",
                    "face_left none aoe", "face_left none attack", "none none none"):
            self.tick(cmd)
        self.assertEqual(self.events, [
            ("down", "right"), ("up", "right"), ("down", "s"),
            ("up", "s"), ("down", "left"), ("up", "left"), ("down", "s"), ("up", "s"),
        ])

    def test_focus_pause_resume_and_shutdown(self):
        self.tick("right none none")
        self.kb.is_game_window_active = lambda: False
        self.kb._tick()
        self.tick("left none none")
        self.kb.is_game_window_active = lambda: True
        self.kb._tick()
        self.kb.disable()
        self.kb._tick()
        self.kb.enable()
        self.kb._tick()
        self.kb.stop()
        self.kb._tick()
        self.assertEqual(self.events, [
            ("down", "right"), ("up", "right"), ("down", "left"), ("up", "left"),
            ("down", "left"), ("up", "left"),
        ])

    def test_failed_release_is_retried_before_reversal(self):
        self.tick("left none none")
        with patch.object(kbc.pyautogui, "keyUp", side_effect=OSError):
            self.tick("right none attack")
        self.assertEqual(self.kb.pressed_keys(), ["left"])
        self.tick("right none attack")
        self.assertEqual(self.events, [
            ("down", "left"), ("up", "left"), ("down", "right"),
            ("down", "x"),
        ])

    def test_directinput_failure_does_not_commit_transition(self):
        backend = Mock(KEYBOARD_MAPPING={"left": 1})
        backend.keyDown.side_effect = [False, True]
        backend.keyUp.side_effect = [False, True]
        with patch.multiple(kbc, HAS_PYDIRECTINPUT=True, pydirectinput=backend,
                            is_mac=lambda: False, create=True):
            self.tick("left none none")
            self.assertEqual(self.kb.pressed_keys(), [])
            self.tick("left none none")
            self.assertEqual(self.kb.pressed_keys(), ["left"])
            self.tick("stop stop stop")
            self.assertEqual(self.kb.pressed_keys(), ["left"])
            self.tick("stop stop stop")
            self.assertEqual(self.kb.pressed_keys(), [])
        self.assertEqual(backend.method_calls, [
            call.keyDown("left"), call.keyDown("left"), call.keyUp("left"), call.keyUp("left"),
        ])

    def test_health_tap_with_shared_movement_binding_restores_hold(self):
        self.tick("left none none")
        self.assertTrue(self.kb.press_key_safe("left", action_name="HP potion"))
        self.tick("left none none")
        self.assertEqual(self.events, [
            ("down", "left"), ("up", "left"), ("down", "left"),
            ("up", "left"), ("down", "left"),
        ])

    def test_pickup_releases_and_resumes_current_direction(self):
        self.tick("left up none")
        self.kb.pickup_key = "z"
        self.kb.pickup_taps = 2
        self.kb.pickup_interval = 60
        self.kb.t_last_pickup = 0
        self.events.clear()
        self.kb._tick()
        self.tick("right up none")
        self.assertEqual(self.events, [
            ("up", "left"), ("down", "z"), ("up", "z"),
            ("down", "z"), ("up", "z"), ("down", "right"),
        ])


class HotkeyTest(unittest.TestCase):
    def test_release_rearms_only_that_function_key(self):
        with patch.object(kbl.keyboard, "Listener") as hook, patch.object(kbl.threading.Thread, "start"):
            listener = kbl.KeyBoardListener()
        toggle, switch = Mock(), Mock()
        listener.register_func_key_handler("f1", toggle)
        listener.register_func_key_handler("f11", switch)
        f1, f11 = kbl.keyboard.Key.f1, kbl.keyboard.Key.f11
        listener.on_press(f1)
        listener.on_press(f11)
        listener.on_release(f11)
        listener.on_press(f11)
        listener.on_press(f1)  # OS repeat while held
        toggle.assert_called_once()
        self.assertEqual(switch.call_count, 2)
        listener.on_release(f1)
        listener.on_press(f1)
        self.assertEqual(toggle.call_count, 2)
        listener.stop()
        hook.return_value.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
