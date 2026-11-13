# 人物状态机（CharacterFSM）

> 重构目标：把原本散落在 `MonsterDetector.run_once()` 里的决策逻辑收敛成一个纯逻辑状态机，
> 行为与原实现**逐帧等价**（有 golden 差分测试兜底）。

## 1. 为什么要有它

重构前，"角色该做什么"由三套隐式状态 + 一段 if/elif 决定：

| 隐式状态 | 原变量 | 位置 |
|---|---|---|
| 原地攻击 4 阶段 | `_in_place_state` / `_in_place_state_frame` / `_in_place_walk_dir` / `_in_place_backward_dir` | MonsterDetector |
| 搜索扫图 | `_hunt_no_monster_frames`、`_hunt_search_dir`、`_hunt_search_flip_frame`、`_hunt_search_sweeps`、`_hunt_search_pause_until`、`_hunt_search_anchor_x`、`_hunt_search_last_progress_x`、`_hunt_search_stall_frames`、`_hunt_search_last_reason` | MonsterDetector |
| 路线跟随 | `idx_routes`、`cmd_move_x/y/action`、`is_on_ladder` | MonsterDetector |

命令优先级完全靠 if 的顺序隐含表达（原地 > 追击 > 搜索 > 路线），
状态无法观测、无法单测，也没有地方挂全局覆盖（例如"测谎弹窗出现就停机"）。

## 2. 结构

```
                   ┌──────────────────────────────┐
  每帧事实          │            CharacterFSM       │        命令
  Perception ──────►  tick(p) → (state, Command)  ──────► KeyBoardController.set_command()
                   └──────────────────────────────┘
```

- `src/engine/CharacterFSM.py`：纯标准库实现（不 import cv2 / numpy / win32）
- `MonsterDetector` 只负责**提供事实**（怪物列表、朝向、小地图进度、路线命令…），不再决定动作
- 键盘线程读取完整的 `(move_x, move_y, action)` 命令，按上一动作和当前持键生成输入事件。

## 3. 状态与优先级

| 优先级 | 状态 | 进入条件 | 行为 |
|---|---|---|---|
| 1 | `COMBAT_IN_PLACE` | `mode=hunt` 且 `attack_in_place=true` | 4 阶段循环：attack(300) → pre_walk(8) → walk_away(4) → walk_back(4) |
| 2 | `COMBAT_PURSUE` | `mode=hunt` 且有怪 | 面向最近怪 + 攻击；怪 ≥ 3 用 AoE |
| 3 | `SEARCH` | `mode=hunt` 且无怪 | 左右扫图，定时/边界/卡住翻向，扫完暂停，随机跳 |
| 4 | 路线（基础层） | `enable_route` 且有路线图 | 颜色映射出的 `(x, y, action)`，被上面三层覆盖 |
| — | `DETECT` | `mode=detect` | 只输出路线命令（与原实现一致，通常为 none） |

> 注意：路线命令在 detect 模式下也会照发——这是重构前就有的行为，golden 测试锁定了它。
> 如果以后确定这是 bug，应当作为"行为变更"单独处理，并重新生成 golden。

## 4. 关键设计

- **frame_count 语义**：沿用探测器主循环的帧计数（15 Hz），FSM 内所有定时器都以"帧"为单位。
  `in_place_walk_interval: 300` = 20 秒，不是 300 毫秒。
- **随机数可注入**：`CharacterFSM(cfg, seed=...)`；种子取 `cfg["fsm"]["random_seed"]`（可缺省）。
  测试用固定种子复现随机跳。
- **状态可观测**：`fsm.state` / `fsm.state_frame` / `fsm.search_dir` 等属性会显示在监视窗口左下角。
- **不做 IO**：FSM 不写日志、不读文件、不碰键盘，方便离线跑上千帧的测试。

## 5. 如何加新状态

以"测谎弹窗 → 停机"为例：

1. 在 `Perception` 加字段：`alarm: bool = False`（探测器 CV 识别到弹窗时置位）
2. 在 `CharacterFSM.tick()` 最前面加最高优先级分支：

```python
if p.alarm:
    new_state = STATE_HALT           # 新增常量
    move_x = move_y = action = "none"
```

3. 在 `MonsterDetector.run_once()` 构建 Perception 时传入识别结果
4. 补单元测试：`alarm=True` 时命令必须是 `none none none`，且状态为 `HALT`
5. **这是行为变更**，golden 场景里 alarm 恒为 False，所以不会破坏现有 diff；新增的定向用例单独覆盖

## 6. 测试

```powershell
# 单元测试（14 个，含 golden 差分）
.\.venv\Scripts\python.exe test\test_character_fsm.py -v

# 集成测试（合成画面跑 run_once，不需要游戏窗口）
.\.venv\Scripts\python.exe test\test_fsm_integration.py -v

# 键盘执行测试（记录按下、松开的事件和顺序）
.\.venv\Scripts\python.exe test\test_key_state.py -v
```

- `test/fsm_scenarios.py`：13 个场景的**输入事实**生成器（不含决策逻辑）
- `test/fsm_golden.py`：重构前实现跑出的命令序列（gzip+base64，约 1.1KB）
- `test/test_character_fsm.py`：重放场景，逐帧比对命令；另有定向用例

### 修改行为时，golden 怎么处理

golden 是**重构前的冻结行为**。如果只是重构，不要动它；如果**故意**改了行为：

1. 从 git 历史取回旧实现：
   ```powershell
   git show <重构前commit>:src/engine/MonsterDetector.py > _legacy_monster_detector.py
   ```
2. 用旧实现的 `_get_in_place_command` / `_get_hunt_search_command` 和原决策块，
   在 `test/fsm_scenarios.py` 的场景上重跑一遍命令序列
3. 用新的命令序列替换 `test/fsm_golden.py` 里的 `GOLDEN_B64`

## 7. 持续移动与点按

键盘线程先松开新命令用不到的键，再按下新增的方向键。水平和垂直移动可以组合。例如，角色沿梯子向上时改变水平方向，向上键会一直保持按下。

| 原动作 | 新动作 | 发出的事件 |
|---|---|---|
| 静止 | 向左走 | 按下左键 |
| 向左走 | 继续向左走 | 无 |
| 向左走 | 向右走 | 松开左键，再按下右键 |
| 向右走 | 停止 | 松开右键 |
| 向右走 | 向左转身 | 松开右键，点按一次左键 |

转身、跳跃、传送在进入对应动作时点按一次，双跳和双传送各点按两次。持续攻击和群攻按各自冷却时间再次点按。拾取时暂时松开持有的水平方向键，结束后按最新命令恢复移动。

监视窗口的 `Keys` 行和 `kb.pressed_keys()` 显示本进程已发出且尚未松开的键。F1 暂停、失焦和退出都会释放这些键。恢复时重新应用当前命令。

## 8. 已知边界（下一步可做）

- 血蓝监控线程通过 `press_key_safe()` 点按药键，并通过 `is_need_force_heal` 请求优先回血。
- **没有 `HALT`**：测谎弹窗检测还没实现，这是当前最值得补的状态（见第 5 节）。
- **开环执行**：FSM 认为命令一定被执行，但失焦、注入失败、键位冲突都会让实际状态偏离；
  目前只有 `is_game_window_active()` 和 `_PRESSED_KEYS` 两个弱校验。
