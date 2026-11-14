"""集成测试：合成画面跑通 MonsterDetector.run_once() → CharacterFSM → set_command 链路。

不依赖游戏窗口，也不写日志（注入空 logger，避免测试在只读/受限环境里失败）。
运行：
    .\\.venv\\Scripts\\python.exe test\\test_fsm_integration.py -v
"""
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (BASE_DIR, TEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _install_null_logger():
    """MonsterDetector 会 import logger 并在 import 时创建日志文件。"""
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

from src.utils.common import load_yaml, override_cfg  # noqa: E402
from src.engine.MonsterDetector import MonsterDetector  # noqa: E402


class _KBStub:
    def __init__(self):
        self.commands = []
        self.is_enable = True
        self.input_status = "Ready"

    def set_command(self, cmd: str):
        self.commands.append(cmd)

    def pressed_keys(self):
        return []


def _load_cfg() -> dict:
    cfg = load_yaml("config/config_default.yaml")
    override_cfg(cfg, load_yaml("config/config_custom.yaml"))
    cfg["bot"]["chase_visible"] = False  # Exercise the original stationary mode.
    return cfg


class FsmIntegrationTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(0)
        # 模拟一张游戏画面：随机纹理 + 底部 UI 条
        img = rng.integers(0, 255, (600, 800, 3), dtype=np.uint8)
        img[610:, :, :] = 40
        self.tmp = os.path.join(tempfile.gettempdir(), "fsm_integration_frame.png")
        cv2.imwrite(self.tmp, img)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_run_once_drives_fsm(self):
        cfg = _load_cfg()
        detector = MonsterDetector(cfg, test_image=self.tmp)
        self.assertTrue(detector.load_monster_images(), "monster 素材加载失败")

        kb = _KBStub()
        detector._kb = kb
        detector.health_monitor = None
        detector.is_show_debug = False

        self.assertIsNotNone(detector.fsm, "MonsterDetector 必须持有 CharacterFSM")

        n_frames = 30
        for _ in range(n_frames):
            self.assertEqual(detector.run_once(), 0)

        self.assertEqual(len(kb.commands), n_frames, "每帧都应交出一条命令")
        for cmd in kb.commands:
            parts = cmd.split()
            self.assertEqual(len(parts), 3, f"命令格式应为 3 段: {cmd!r}")
        self.assertIn(
            detector.fsm.state,
            ("DETECT", "COMBAT_IN_PLACE", "COMBAT_PURSUE", "SEARCH"),
        )
        self.assertEqual(detector.fsm.state_frame, n_frames - 1)

    def test_overlay_renders_fsm_and_key_line(self):
        """覆盖层会画 FSM 状态行和按键状态行；有 cv2 绘制异常会在这里暴露。"""
        cfg = _load_cfg()
        detector = MonsterDetector(cfg, test_image=self.tmp)
        self.assertTrue(detector.load_monster_images())

        detector._kb = _KBStub()
        detector.health_monitor = None
        detector.is_show_debug = True
        for _ in range(3):
            self.assertEqual(detector.run_once(), 0)

        self.assertIsNotNone(detector.img_frame_debug)
        detector._update_debug_overlay()  # 不抛异常即通过

    def test_attack_stops_when_last_target_leaves_skill_rectangle(self):
        cfg = _load_cfg()
        cfg["title"]["enable"] = False
        detector = MonsterDetector(cfg, test_image=self.tmp)
        detector.is_show_debug = False
        detector._kb = _KBStub()
        # Synthetic player at (400, 300). Include the boundary, exclude the search margin.
        inside = {"name": "ghost", "position": (640, 440), "size": (20, 20)}
        outside_x = dict(inside, position=(641, 440))
        outside_y = dict(inside, position=(640, 441))
        upper_edge = dict(inside, position=(390, 140))
        above_edge = dict(inside, position=(390, 139))
        detector.get_monsters_in_range = Mock(side_effect=[
            [inside, outside_x, outside_y], [outside_x, outside_y], [], [upper_edge], [above_edge],
        ])
        for _ in range(5):
            self.assertEqual(detector.run_once(), 0)
        self.assertEqual(detector._kb.commands, [
            "face_right none attack", "none none none", "none none none",
            "none none attack", "none none none",
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
