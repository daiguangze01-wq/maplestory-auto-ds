"""
CharacterFSM — 人物状态机（纯逻辑）

把原本散落在 ``MonsterDetector.run_once()`` 里的决策逻辑收敛成单一状态机：

  - 命令优先级：路线 < 搜索 < 追击 < 原地攻击（与原实现一致）
  - 原地攻击 4 阶段循环：attack → pre_walk → walk_away → walk_back
  - 无怪搜索状态：定时 / 边界 / 卡住翻向、扫完暂停、随机跳
  - 检测模式的空命令

设计约束（有意为之）：
  - 不 import cv2 / numpy / win32，只依赖标准库和 cfg dict → 可离线单测
  - 不碰键盘，只产出 ``Command(move_x, move_y, action)`` 三元组
  - 所有定时器沿用探测器的 ``frame_count``（15 Hz 语义），不自己数秒
  - 随机数用独立 ``random.Random`` 实例，可注入种子 → 测试可复现

行为等价性由 ``test/test_character_fsm.py`` 用重构前生成的 golden 逐帧校验。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Tuple

# 状态名（用于日志 / 覆盖层显示）
STATE_DETECT = "DETECT"
STATE_COMBAT_IN_PLACE = "COMBAT_IN_PLACE"
STATE_COMBAT_PURSUE = "COMBAT_PURSUE"
STATE_SEARCH = "SEARCH"
STATE_IDLE = "IDLE"

_ROUTE_NONE = ("none", "none", "none")


@dataclass(frozen=True)
class Perception:
    """每帧从探测器传进来的事实，不含任何决策。"""

    mode: str = "detect"                  # detect / hunt
    attack_style: str = "directional"     # directional / aoe_skill
    attack_in_place: bool = False
    monster_count: int = 0
    facing: str = "none"                  # 最近怪物相对玩家的方向：left / right / none
    is_on_ladder: bool = False
    frame_count: int = 0                  # 探测器主循环帧计数（15 Hz）
    route_active: bool = False            # enable_route 且当前有路线图
    route_cmd: Tuple[str, str, str] = _ROUTE_NONE
    progress_x: Optional[float] = None    # 稳定的水平位移基准（小地图/全局坐标）
    progress_src: str = "none"            # global / minimap / none
    single_platform: bool = False         # Assigned platform, stationary team hunting


@dataclass(frozen=True)
class Command:
    """下发给 KeyBoardController 的命令。"""

    move_x: str = "none"
    move_y: str = "none"
    action: str = "none"
    state: str = STATE_IDLE


class CharacterFSM:
    """把 Perception 映射成 Command 的状态机。"""

    def __init__(self, cfg: dict, seed: Optional[int] = None):
        self.cfg = cfg
        self._rng = random.Random(seed)
        self.state = STATE_IDLE
        self.state_frame = 0
        self._reset_internal_state()

    # ------------------------------------------------------------------
    # 状态复位
    # ------------------------------------------------------------------

    def _reset_internal_state(self):
        # 原地攻击子状态机（防攻击锁定的周期性走位）
        self._in_place_state = "attack"       # attack | pre_walk | walk_away | walk_back
        self._in_place_state_frame = 0
        self._in_place_walk_dir = "left"      # 没有朝向信息时的兜底方向
        self._in_place_backward_dir = "left"  # 后退方向（朝向的反方向）

        # 无怪搜索状态
        self._hunt_no_monster_frames = 0
        self._hunt_search_dir = "left"
        self._hunt_search_flip_frame = 0
        self._hunt_search_sweeps = 0
        self._hunt_search_pause_until = 0
        self._hunt_search_anchor_x = None
        self._hunt_search_last_progress_x = None
        self._hunt_search_stall_frames = 0
        self._hunt_search_last_reason = "idle"
        self._hunt_next_jump_frame = 0

    def reset(self):
        """复位所有子状态（切换角色 / 重新开始挂机时调用）。"""
        self._reset_internal_state()
        self.state = STATE_IDLE
        self.state_frame = 0

    # ------------------------------------------------------------------
    # 只读调试信息（覆盖层 / 日志用）
    # ------------------------------------------------------------------

    @property
    def no_monster_frames(self) -> int:
        return self._hunt_no_monster_frames

    @property
    def search_dir(self) -> str:
        return self._hunt_search_dir

    @property
    def search_sweeps(self) -> int:
        return self._hunt_search_sweeps

    @property
    def search_stall_frames(self) -> int:
        return self._hunt_search_stall_frames

    @property
    def search_last_reason(self) -> str:
        return self._hunt_search_last_reason

    @property
    def in_place_phase(self) -> str:
        return self._in_place_state

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def tick(self, p: Perception) -> Command:
        """推进一帧，返回本帧命令。"""
        move_x, move_y, action = "none", "none", "none"
        route_move_x, route_move_y = "none", "none"

        # ① 路线命令作为基础层（detect 模式下也会照发，与原实现一致）
        if p.route_active:
            route_move_x, route_move_y, route_action = p.route_cmd
            move_x, move_y = route_move_x, route_move_y
            action = route_action

        # ② 按优先级覆盖
        if p.single_platform:
            # Turning to attack is allowed. Suppress periodic walks, route
            # commands and searching so a ledge near its edge stays occupied.
            active = p.mode == "hunt" and p.monster_count > 0
            move_x = (f"face_{p.facing}" if active and p.facing in ("left", "right")
                      and p.attack_style == "directional" else "none")
            move_y, action = "none", "none"
            if active:
                threshold = self.cfg.get("directional_attack", {}).get("aoe_monster_count", 3)
                action = "aoe" if p.monster_count >= threshold else "attack"
            new_state = STATE_COMBAT_IN_PLACE if active else STATE_IDLE
        elif p.mode == "hunt" and p.attack_in_place:
            self._reset_hunt_search_state()
            move_x, move_y, action = self._in_place_command(p)
            new_state = STATE_COMBAT_IN_PLACE
        elif p.mode == "hunt" and p.monster_count > 0:
            self._reset_hunt_search_state()
            aoe_threshold = int(
                self.cfg.get("directional_attack", {}).get("aoe_monster_count", 3)
            )
            action = "aoe" if p.monster_count >= aoe_threshold else "attack"
            if p.attack_style == "directional":
                if p.facing in ("left", "right"):
                    move_x = f"face_{p.facing}"
                    if not p.is_on_ladder:
                        move_y = "none"
                elif route_move_x in ("left", "right") and p.is_on_ladder:
                    move_x = route_move_x
            new_state = STATE_COMBAT_PURSUE
        elif p.mode == "hunt":
            self._hunt_no_monster_frames += 1
            if p.attack_style == "directional":
                search_x, search_y, search_action = self._search_command(p)
                if (search_x != "none" or search_y != "none"
                        or search_action != "none"):
                    move_x, move_y, action = search_x, search_y, search_action
            new_state = STATE_SEARCH
        else:
            new_state = STATE_DETECT

        if (p.monster_count <= 0 or p.mode != "hunt") and action in ("attack", "aoe"):
            action = "none"
        self._advance_state(new_state)
        return Command(move_x=move_x, move_y=move_y, action=action, state=self.state)

    def _advance_state(self, new_state: str):
        if new_state == self.state:
            self.state_frame += 1
        else:
            self.state = new_state
            self.state_frame = 0

    # ------------------------------------------------------------------
    # 原地攻击子状态机
    # ------------------------------------------------------------------

    def _in_place_command(self, p: Perception) -> Tuple[str, str, str]:
        """原地攻击 + 周期性小走位（防止长时间站桩导致攻击失效）。

        状态机：
          attack --(interval)--> pre_walk --(stop_frames)--> walk_away
          --(walk_frames)--> walk_back --(walk_frames)--> attack
        """
        cfg = self.cfg.get("directional_attack", {})
        interval = max(30, int(cfg.get("in_place_walk_interval", 300)))
        walk_frames = max(1, int(cfg.get("in_place_walk_frames", 20)))
        stop_frames = max(1, int(cfg.get("in_place_stop_attack_frames", 8)))

        if self._in_place_state == "attack":
            self._in_place_state_frame += 1
            if self._in_place_state_frame >= interval:
                if p.facing in ("left", "right"):
                    self._in_place_backward_dir = "right" if p.facing == "left" else "left"
                else:
                    self._in_place_backward_dir = (
                        "right" if self._in_place_walk_dir == "left" else "left"
                    )
                self._in_place_walk_dir = self._in_place_backward_dir
                self._in_place_state = "pre_walk"
                self._in_place_state_frame = 0
        elif self._in_place_state == "pre_walk":
            self._in_place_state_frame += 1
            if self._in_place_state_frame >= stop_frames:
                self._in_place_state = "walk_away"
                self._in_place_state_frame = 0
        elif self._in_place_state == "walk_away":
            self._in_place_state_frame += 1
            if self._in_place_state_frame >= walk_frames:
                self._in_place_state = "walk_back"
                self._in_place_state_frame = 0
        elif self._in_place_state == "walk_back":
            self._in_place_state_frame += 1
            if self._in_place_state_frame >= walk_frames:
                self._in_place_state = "attack"
                self._in_place_state_frame = 0

        if self._in_place_state == "walk_away":
            return self._in_place_backward_dir, "none", "none"
        if self._in_place_state == "walk_back":
            forward_dir = "right" if self._in_place_backward_dir == "left" else "left"
            return forward_dir, "none", "none"

        # attack / pre_walk：站住；只有 attack 阶段才输出攻击
        if self._in_place_state == "attack":
            if p.monster_count > 0:
                aoe_threshold = int(cfg.get("aoe_monster_count", 3))
                action = "aoe" if p.monster_count >= aoe_threshold else "attack"
                if p.facing in ("left", "right"):
                    return f"face_{p.facing}", "none", action
                return "none", "none", action
            return "none", "none", "none"
        return "none", "none", "none"

    # ------------------------------------------------------------------
    # 搜索子状态机
    # ------------------------------------------------------------------

    def _reset_hunt_search_state(self):
        """找到目标 / 进入原地模式时清零搜索状态。"""
        self._hunt_no_monster_frames = 0
        self._hunt_search_flip_frame = 0
        self._hunt_search_sweeps = 0
        self._hunt_search_pause_until = 0
        self._hunt_search_anchor_x = None
        self._hunt_search_last_progress_x = None
        self._hunt_search_stall_frames = 0
        self._hunt_search_last_reason = "target"

    def _search_command(self, p: Perception) -> Tuple[str, str, str]:
        """左右扫图找怪；返回 (move_x, move_y, action)。"""
        cfg = self.cfg.get("directional_attack", {})
        if not bool(cfg.get("search_enabled", True)):
            return "none", "none", "none"

        # 爬梯交给路线逻辑，不做横向搜索
        if p.is_on_ladder:
            return "none", "none", "none"

        trigger_frames = max(1, int(cfg.get("search_trigger_frames", 4)))
        if self._hunt_no_monster_frames < trigger_frames:
            return "none", "none", "none"

        if p.frame_count < self._hunt_search_pause_until:
            return "none", "none", "none"

        flip_frames = max(4, int(cfg.get("search_flip_frames", 18)))
        progress_x, progress_src = p.progress_x, p.progress_src
        if progress_x is not None and self._hunt_search_anchor_x is None:
            self._hunt_search_anchor_x = progress_x
        if progress_x is not None and self._hunt_search_last_progress_x is None:
            self._hunt_search_last_progress_x = progress_x

        flip_reason = None

        # 卡住检测
        stall_frames_limit = int(cfg.get("search_stall_frames", 8))
        if stall_frames_limit > 0:
            stall_min_delta_px = float(cfg.get("search_stall_min_delta_px", 1.0))
            if progress_x is not None and self._hunt_search_last_progress_x is not None:
                progress_delta = abs(progress_x - self._hunt_search_last_progress_x)
                if progress_delta <= stall_min_delta_px:
                    self._hunt_search_stall_frames += 1
                else:
                    self._hunt_search_stall_frames = 0
                self._hunt_search_last_progress_x = progress_x
                if (progress_src in ("global", "minimap")
                        and self._hunt_search_stall_frames >= stall_frames_limit):
                    flip_reason = "stall"

        # 边界检测
        max_offset_px = float(cfg.get("search_max_offset_px", 0))
        if (flip_reason is None
                and max_offset_px > 0
                and progress_src in ("global", "minimap")
                and progress_x is not None
                and self._hunt_search_anchor_x is not None):
            offset_x = progress_x - self._hunt_search_anchor_x
            if self._hunt_search_dir == "left" and offset_x <= -max_offset_px:
                flip_reason = "bound"
            elif self._hunt_search_dir == "right" and offset_x >= max_offset_px:
                flip_reason = "bound"

        # 定时翻向
        if self._hunt_search_flip_frame <= 0:
            self._hunt_search_flip_frame = p.frame_count + flip_frames
        elif flip_reason is None and p.frame_count >= self._hunt_search_flip_frame:
            flip_reason = "timer"

        if flip_reason is not None:
            self._hunt_search_dir = "right" if self._hunt_search_dir == "left" else "left"
            self._hunt_search_flip_frame = p.frame_count + flip_frames
            self._hunt_search_sweeps += 1
            self._hunt_search_stall_frames = 0
            self._hunt_search_last_reason = flip_reason
            if progress_x is not None:
                self._hunt_search_anchor_x = progress_x
                self._hunt_search_last_progress_x = progress_x

        # 扫完一轮 → 暂停
        max_sweeps = int(cfg.get("search_max_sweeps", 0))
        if max_sweeps > 0 and self._hunt_search_sweeps >= max_sweeps:
            pause_frames = max(0, int(cfg.get("search_pause_frames", 12)))
            self._hunt_search_pause_until = p.frame_count + pause_frames
            self._hunt_search_sweeps = 0
            self._hunt_search_last_reason = "pause"
            return "none", "none", "none"

        action = "none"

        # 搜索时随机跳（唯一的随机行为）
        jump_chance = float(cfg.get("search_random_jump_chance", 0.0))
        if jump_chance > 0:
            jump_cd = max(1, int(cfg.get("search_random_jump_cd", 30)))
            if p.frame_count >= self._hunt_next_jump_frame:
                if self._rng.random() < jump_chance:
                    action = "jump"
                    self._hunt_next_jump_frame = p.frame_count + jump_cd

        return self._hunt_search_dir, "none", action
