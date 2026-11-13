"""
FSM 行为基准的场景生成器。

golden（test/data/fsm_golden.json）由**重构前**的 MonsterDetector（git HEAD）
在本文件的场景上跑出来；重构后用 CharacterFSM 重放同一批场景，逐帧比对命令。

注意：本文件生成的是"输入事实"（Perception），不包含任何决策逻辑。
如果修改了场景参数，必须用旧实现重新生成 golden，否则基准失效。
"""
import copy
import os

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_base_cfg() -> dict:
    """与 main.py 一致：config_default + config_custom。"""
    with open(os.path.join(BASE_DIR, "config", "config_default.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    custom = os.path.join(BASE_DIR, "config", "config_custom.yaml")
    if os.path.exists(custom):
        with open(custom, encoding="utf-8") as f:
            _deep_merge(cfg, yaml.safe_load(f))
    return cfg


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _monsters(count: int, facing: str, px: int = 400) -> list:
    """按期望朝向摆怪，供旧实现 _get_attack_facing() 复现出同一个 facing。"""
    if count <= 0:
        return []
    dx = -120 if facing == "left" else (120 if facing == "right" else 0)
    return [
        {"name": f"m{i}", "position": (px + dx - 20, 300), "size": (40, 40), "score": 0.9}
        for i in range(count)
    ]


def _tick(mode="hunt", n=0, facing="none", ladder=False, in_place=False,
          route_active=False, route_cmd=("none", "none", "none"),
          px=None, ps="none") -> dict:
    if n <= 0:
        facing = "none"
    return {
        "m": mode, "n": n, "f": facing, "l": ladder, "ip": in_place,
        "ra": route_active, "rc": tuple(route_cmd),
        "px": px, "ps": ps,
    }


def _scenario(name, cfg, seed, ticks) -> dict:
    return {"name": name, "cfg": cfg, "seed": seed, "ticks": ticks}


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def build_scenarios(base_cfg: dict) -> list:
    scen = []

    # 1) detect 模式：不应产生任何命令
    scen.append(_scenario(
        "detect_mode", {},
        seed=1,
        ticks=[_tick(mode="detect") for _ in range(30)],
    ))

    # 2) 原地攻击（真实配置）：300 帧内不切换子状态，覆盖 attack 分支
    ticks = []
    for i in range(120):
        ticks.append(_tick(
            in_place=True, n=i % 6, facing=("none", "left", "right")[i % 3],
            ladder=(i % 11 == 0),
        ))
    scen.append(_scenario("in_place_realtime", {}, seed=2, ticks=ticks))

    # 3) 原地攻击完整循环（压缩 interval 到 20 帧）：
    #    attack(20) -> pre_walk(8) -> walk_away(4) -> walk_back(4)
    ticks = [_tick(in_place=True, n=2, facing="left") for _ in range(200)]
    scen.append(_scenario(
        "in_place_cycle_compressed",
        {"directional_attack": {"in_place_walk_interval": 20}},
        seed=3, ticks=ticks,
    ))

    # 4) 追击模式：怪物数量覆盖 aoe 阈值 3
    ticks = []
    for i in range(80):
        ticks.append(_tick(
            n=i % 7, facing=("none", "left", "right")[i % 3],
            ladder=(i % 9 == 0),
        ))
    scen.append(_scenario("pursue_mixed", {}, seed=4, ticks=ticks))

    # 5) 搜索（真实配置）：200 帧内不会翻向，只覆盖初始化
    ticks = [_tick(n=0, px=100 + 0.05 * i, ps="minimap") for i in range(200)]
    scen.append(_scenario("search_realtime", {}, seed=5, ticks=ticks))

    # 6) 定时翻向（flip 压缩到 120）
    ticks = [_tick(n=0, px=100 + 0.05 * i, ps="minimap") for i in range(400)]
    scen.append(_scenario(
        "search_timer_flip",
        {"directional_attack": {"search_flip_frames": 120}},
        seed=6, ticks=ticks,
    ))

    # 7) 边界翻向：位移持续增大，超过 search_max_offset_px
    ticks = [_tick(n=0, px=100 + 3 * i, ps="minimap") for i in range(400)]
    scen.append(_scenario(
        "search_bound_flip",
        {"directional_attack": {"search_max_offset_px": 200, "search_flip_frames": 100000}},
        seed=7, ticks=ticks,
    ))

    # 8) 卡住翻向：位置不动，stall 阈值 8
    ticks = [_tick(n=0, px=100.0, ps="minimap") for _ in range(200)]
    scen.append(_scenario(
        "search_stall",
        {"directional_attack": {
            "search_stall_frames": 8, "search_stall_min_delta_px": 1.0,
            "search_max_offset_px": 0, "search_flip_frames": 100000}},
        seed=8, ticks=ticks,
    ))

    # 9) 扫完暂停：max_sweeps=2 后暂停 12 帧
    ticks = [_tick(n=0, px=100.0, ps="minimap") for _ in range(300)]
    scen.append(_scenario(
        "search_pause",
        {"directional_attack": {
            "search_max_sweeps": 2, "search_pause_frames": 12,
            "search_flip_frames": 120, "search_max_offset_px": 0}},
        seed=9, ticks=ticks,
    ))

    # 10) 随机跳：覆盖 RNG 消耗顺序
    ticks = [_tick(n=0, px=100.0, ps="minimap") for _ in range(400)]
    scen.append(_scenario(
        "search_random_jump",
        {"directional_attack": {
            "search_flip_frames": 100000, "search_max_offset_px": 0,
            "search_random_jump_chance": 0.03, "search_random_jump_cd": 45}},
        seed=10, ticks=ticks,
    ))

    # 11) 路线优先级混合：在 hunt/原地/追击/搜索/detect 之间切换
    route_cmds = [
        ("none", "none", "none"),
        ("left", "none", "none"),
        ("right", "none", "none"),
        ("none", "up", "none"),
        ("none", "down", "none"),
        ("left", "none", "jump"),
        ("right", "none", "teleport"),
        ("none", "none", "goal"),
        ("face_left", "none", "none"),
    ]
    ticks = []
    for i in range(300):
        mode = ("hunt", "hunt", "detect")[i % 3]
        ticks.append(_tick(
            mode=mode, in_place=(i % 7 == 0), n=i % 4,
            facing=("none", "left", "right")[i % 3],
            ladder=(i % 13 == 0), route_active=True,
            route_cmd=route_cmds[i % len(route_cmds)],
            px=200 + 2 * i, ps="global",
        ))
    scen.append(_scenario(
        "route_mixed", {"bot": {"enable_route": True}}, seed=11, ticks=ticks,
    ))

    # 12) 爬梯 + 路线 + 追击：面向为 none 时回落到路线横移
    ticks = []
    for i in range(100):
        ticks.append(_tick(
            n=2 if i % 2 == 0 else 0,
            facing=("none", "left", "right")[i % 3],
            ladder=True, route_active=True,
            route_cmd=("left", "none", "none") if i % 2 == 0 else ("right", "none", "none"),
            px=500.0, ps="global",
        ))
    scen.append(_scenario(
        "ladder_route", {"bot": {"enable_route": True}}, seed=12, ticks=ticks,
    ))

    # 13) 路线开启但进度取不到（ps=none）
    ticks = [_tick(n=0, route_active=True, route_cmd=("left", "none", "none"))
             for _ in range(60)]
    scen.append(_scenario(
        "route_no_progress", {"bot": {"enable_route": True}}, seed=13, ticks=ticks,
    ))

    # 挂上场景级 cfg（深拷贝 base + override）
    out = []
    for s in scen:
        cfg = _deep_merge(copy.deepcopy(base_cfg), copy.deepcopy(s["cfg"]))
        out.append({"name": s["name"], "cfg": cfg, "seed": s["seed"], "ticks": s["ticks"]})
    return out
