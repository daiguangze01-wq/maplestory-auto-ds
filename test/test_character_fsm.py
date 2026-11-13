"""CharacterFSM 单元测试（标准库 unittest，不需要游戏窗口 / 不写日志）。

两个层次：
  1. golden 差分 —— 用**重构前**实现生成的命令序列逐帧比对，保证行为等价；
  2. 定向用例 —— 状态映射、原地循环节拍、AoE 阈值、路线优先级、RNG 可复现。

运行：
    .\\.venv\\Scripts\\python.exe test\\test_character_fsm.py -v
"""
import base64
import gzip
import json
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (BASE_DIR, TEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fsm_golden  # noqa: E402
import fsm_scenarios  # noqa: E402
from src.engine.CharacterFSM import (  # noqa: E402
    CharacterFSM,
    Perception,
    STATE_COMBAT_IN_PLACE,
    STATE_COMBAT_PURSUE,
    STATE_DETECT,
    STATE_SEARCH,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def load_golden() -> dict:
    raw = gzip.decompress(base64.b64decode(fsm_golden.GOLDEN_B64))
    return json.loads(raw.decode("utf-8"))


def _cfg(overrides: dict = None) -> dict:
    """base 配置（default + custom）+ 场景覆盖。"""
    return fsm_scenarios._deep_merge(fsm_scenarios.load_base_cfg(), overrides or {})


def _p(t: dict, cfg: dict, frame_count: int) -> Perception:
    return Perception(
        mode=t["m"],
        attack_style=cfg.get("bot", {}).get("attack", "directional"),
        attack_in_place=t["ip"],
        monster_count=t["n"],
        facing=t["f"],
        is_on_ladder=t["l"],
        frame_count=frame_count,
        route_active=t["ra"],
        route_cmd=tuple(t["rc"]),
        progress_x=t["px"],
        progress_src=t["ps"],
    )


# ---------------------------------------------------------------------------
# 1) golden 差分：与重构前实现行为等价
# ---------------------------------------------------------------------------

class GoldenDifferentialTest(unittest.TestCase):
    def test_matches_pre_refactor_behavior(self):
        scenarios = {s["name"]: s for s in fsm_scenarios.build_scenarios(_cfg())}
        golden = load_golden()
        golden_names = [g["name"] for g in golden["scenarios"]]

        self.assertEqual(
            set(scenarios), set(golden_names),
            "场景集与 golden 不一致；若是故意新增/删除场景，需要用旧实现重新生成 golden",
        )

        problems = []
        total_ticks = 0
        for g in golden["scenarios"]:
            scen = scenarios[g["name"]]
            ticks = scen["ticks"]
            if len(ticks) != len(g["cmds"]):
                problems.append(
                    f"{g['name']}: 场景 tick 数 {len(ticks)} != golden {len(g['cmds'])}"
                )
                continue
            fsm = CharacterFSM(scen["cfg"], seed=g["seed"])
            for i, (t, expected) in enumerate(zip(ticks, g["cmds"])):
                cmd = fsm.tick(_p(t, scen["cfg"], i))
                actual = [cmd.move_x, cmd.move_y, cmd.action]
                total_ticks += 1
                if actual != expected:
                    problems.append(
                        f"{g['name']} tick {i}: expected {expected} "
                        f"got {actual} (state={cmd.state})"
                    )
                    break

        if problems:
            self.fail("与重构前行为不一致:\n" + "\n".join(problems))
        self.assertGreater(total_ticks, 2000, "golden 覆盖的帧数似乎过少")


# ---------------------------------------------------------------------------
# 2) 定向用例
# ---------------------------------------------------------------------------

class StateMappingTest(unittest.TestCase):
    def test_detect_mode(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        cmd = fsm.tick(Perception(mode="detect", attack_in_place=True, frame_count=0))
        self.assertEqual(cmd.state, STATE_DETECT)
        self.assertEqual((cmd.move_x, cmd.move_y, cmd.action), ("none", "none", "none"))

    def test_hunt_states(self):
        cfg = _cfg()
        fsm = CharacterFSM(cfg, seed=0)
        self.assertEqual(
            fsm.tick(Perception(mode="hunt", attack_in_place=True, frame_count=0)).state,
            STATE_COMBAT_IN_PLACE,
        )
        fsm.reset()
        self.assertEqual(
            fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                monster_count=2, facing="left", frame_count=0)).state,
            STATE_COMBAT_PURSUE,
        )
        fsm.reset()
        self.assertEqual(
            fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                monster_count=0, frame_count=0)).state,
            STATE_SEARCH,
        )

    def test_state_frame_counts(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        for i in range(3):
            fsm.tick(Perception(mode="detect", frame_count=i))
        self.assertEqual(fsm.state_frame, 2)


class InPlaceCycleTest(unittest.TestCase):
    def test_cycle_period(self):
        """attack(30) → pre_walk(8) → walk_away(4) → walk_back(4)，周期 46 帧。

        注意 interval 有 max(30, ...) 兜底，所以压缩到 20 仍按 30 走。
        """
        cfg = _cfg({"directional_attack": {"in_place_walk_interval": 20}})
        fsm = CharacterFSM(cfg, seed=0)
        cmds = [
            fsm.tick(Perception(mode="hunt", attack_in_place=True,
                                monster_count=0, frame_count=i))
            for i in range(140)
        ]
        walk = [i for i, c in enumerate(cmds) if c.move_x in ("left", "right")]
        self.assertTrue(walk, "原地模式应出现周期性走位")
        self.assertEqual(walk[0], 37, "第一次走位应发生在 30(attack)+8(pre_walk)-1 帧之后")
        self.assertGreaterEqual(len(walk), 16, "140 帧内应至少走两个完整周期")
        self.assertEqual(walk[8] - walk[0], 46, "周期应为 46 帧")
        # 走位期间不攻击
        for i in (37, 38, 39, 40):
            self.assertEqual(cmds[i].action, "none")

    def test_attack_requires_monster(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        cmd = fsm.tick(Perception(mode="hunt", attack_in_place=True,
                                  monster_count=0, frame_count=0))
        self.assertEqual(cmd.action, "none", "原地模式没怪时不应空挥")


class AoeThresholdTest(unittest.TestCase):
    def test_threshold_is_three(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        cmd2 = fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                   monster_count=2, facing="left", frame_count=0))
        self.assertEqual((cmd2.move_x, cmd2.action), ("face_left", "attack"))
        cmd3 = fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                   monster_count=3, facing="right", frame_count=1))
        self.assertEqual((cmd3.move_x, cmd3.action), ("face_right", "aoe"))

    def test_aoe_skill_style_ignores_facing(self):
        """bot.attack = aoe_skill 时不转身（与原实现一致）。"""
        cfg = _cfg({"bot": {"attack": "aoe_skill"}})
        fsm = CharacterFSM(cfg, seed=0)
        cmd = fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                  attack_style="aoe_skill",
                                  monster_count=1, facing="left", frame_count=0))
        self.assertEqual((cmd.move_x, cmd.action), ("none", "attack"))


class RoutePrecedenceTest(unittest.TestCase):
    def test_route_command_in_detect_mode(self):
        """detect 模式下路线命令照发（与原实现一致）。"""
        fsm = CharacterFSM(_cfg(), seed=0)
        cmd = fsm.tick(Perception(mode="detect", route_active=True,
                                  route_cmd=("left", "none", "jump"), frame_count=0))
        self.assertEqual((cmd.move_x, cmd.move_y, cmd.action), ("left", "none", "jump"))

    def test_combat_overrides_route(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        cmd = fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                  monster_count=1, facing="left",
                                  route_active=True,
                                  route_cmd=("right", "none", "none"),
                                  frame_count=0))
        self.assertEqual(cmd.move_x, "face_left")

    def test_ladder_falls_back_to_route(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        cmd = fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                  monster_count=1, facing="none",
                                  is_on_ladder=True, route_active=True,
                                  route_cmd=("right", "none", "none"),
                                  frame_count=0))
        self.assertEqual(cmd.move_x, "right")


class SearchTest(unittest.TestCase):
    def test_search_waits_trigger_frames(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        cmd = fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                  monster_count=0, frame_count=0))
        self.assertEqual(cmd.move_x, "none", "触发帧数未到前不应移动")

    def test_search_moves_after_trigger(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        fsm.reset()
        cmd = None
        for i in range(10):
            cmd = fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                      monster_count=0, frame_count=i))
        self.assertIn(cmd.move_x, ("left", "right"))

    def test_random_jump_reproducible(self):
        cfg = _cfg({"directional_attack": {
            "search_random_jump_chance": 0.5,
            "search_random_jump_cd": 5,
            "search_flip_frames": 100000,
            "search_max_offset_px": 0,
        }})

        def run(seed):
            fsm = CharacterFSM(cfg, seed=seed)
            return [
                fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                    monster_count=0, frame_count=i)).action
                for i in range(200)
            ]

        same_a, same_b = run(3), run(3)
        other = run(4)
        self.assertEqual(same_a, same_b, "同一 seed 必须可复现")
        self.assertIn("jump", same_a)
        self.assertNotEqual(same_a, other, "不同 seed 应产生不同随机序列")

    def test_reset_clears_search_state(self):
        fsm = CharacterFSM(_cfg(), seed=0)
        for i in range(20):
            fsm.tick(Perception(mode="hunt", attack_in_place=False,
                                monster_count=0, frame_count=i))
        self.assertGreater(fsm.no_monster_frames, 0)
        fsm.reset()
        self.assertEqual(fsm.no_monster_frames, 0)
        self.assertEqual(fsm.state, "IDLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
