# 前言
* 本项目参考了这个库[MapleStoryAutoLevelUp](https://github.com/KenYu910645/MapleStoryAutoLevelUp) 
* 只要是市面上的079版本的冒险岛都可以使用，测试过的版本有：wingms, mapleStoryGo。
* 还有，开挂是不对的，本人已经被封了3个号。

# 功能
 * [x] mini地图识别 (还有问题，需要调试)
 * [x] 角色识别 
 * [x] 怪物识别
 * [ ] 地图绘制 
 * [x] 自动打怪
 * [x] 自动吃药
 * [x] 测谎报警 (需要看看国服什么样子的策划界面)


# 0、如何启动（Windows）

> 注意：仓库原始 master 漏提交了 3 个模块（`src/engine/HealthMonitor.py`、
> `src/input/KeyBoardController.py`、`src/input/KeyBoardListener.py`），
> 本工作区已参考上游 [MapleStoryAutoLevelUp](https://github.com/KenYu910645/MapleStoryAutoLevelUp)
> 适配补齐，否则 `python src/main.py` 会直接 ImportError。

1. 先把游戏打开并进入地图（程序靠窗口标题抓画面）。
2. 确认游戏窗口标题，必要时改 `config/config_custom.yaml`（部分匹配即可），
   可以用 `python test/find-window.py` 扫描当前窗口标题：

```yaml
game_window:
  title: "冒险岛怀旧服"
```

3. 启动（游戏窗口要保持在前台，程序只在游戏窗口是前台时才发按键）：

```powershell
# 方式一：双击 run.bat，或命令行
run.bat

# 方式二：命令行直接跑
.\.venv\Scripts\python.exe .\src\main.py
```

可选参数：`--debug`（调试日志）、`--disable_viz`（关闭监视窗口）、
`--test_image <截图路径>`（离线跑一张图）、`--cfg custom`（自定义配置后缀）。

4. 第一次先只识别、不打怪：把 `config/config_custom.yaml` 里 `bot.mode` 改成 `detect`。
5. 监视窗口 `NewMaple` 快捷键：`F1` 开关键盘控制、`F2`/`F3` HP/MP 自动喝药、
   `F11` detect/hunt 切换、左键点击称号手动锁定角色、`S` 截图、`Q`/`Esc` 退出。

依赖已装在项目内的 `.venv`。如需重建（先确保系统有 Python 3.10+）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```


# 1、识别角色 + 怪物 + miniMap
## 1-1、演示
![识别怪物演示](demo_gif/recognize_user_map_monster.gif)
## 1-2、素材库找到怪物素材
* [素材下载地址](https://maplestory.wiki/GMS/83/mob/1110101)
* 把走动的素材下载下载，放到`monster/下载的怪物名称`文件夹下
* 大概长这样![下载的怪物素材demo](demo_gif/monster.png)
* 如何调整配置
  * 打开`config/config_data.yaml`，找到`mob`字段
  * 添加新的怪物配置，示例如下：只要改这三个字段就行
```yaml
monsters: ["black_axe_stump"]  # 指定怪物文件夹，数组可多个
diff_thres: 0.46               # 匹配阈值（越小越严格）
scales: [1]                    # 缩放比例，官方素材不匹配时改: [0.7, 0.85, 1.0, 1.15]            # 每N帧检测一次 (1=每帧, 3=流畅优先)
  ```

# 2、地图绘制
TODO

# 3、单独测试称号定位

如果你只想验证 `character/title.png` 能不能定位到角色，不想直接跑完整挂机流程，可以先跑：

```powershell
python .\test\test_title_locator.py
```

默认会读取：

- `config/config_default.yaml`
- `config/config_custom.yaml`
- `test/test_image/screenshot.png`

并输出：

- `score`
- 是否命中 `hit=True/False`
- `title_loc`
- `player_loc`

同时会生成调试图：

- `screenshot/title_locator_debug.png`

如果要换测试图：

```powershell
python .\test\test_title_locator.py --image test\test_image\screenshot.png
```

## 本工作区补丁说明

仓库原始 master 缺以下 3 个文件（作者漏提交），`python src/main.py` 会报
`ModuleNotFoundError`。本工作区已参考上游 MapleStoryAutoLevelUp 适配补齐：

- `src/input/KeyBoardController.py`（去掉 `buff_skill` 硬依赖，补 `toggle()` / `stop()`，
  按键强制要求游戏窗口在前台）
- `src/input/KeyBoardListener.py`（F1~F12 全局热键监听）
- `src/engine/HealthMonitor.py`（按本项目 config 的 `bar_*` / `*_rearm_percent` /
  `*_retry_timeout` 参数重写血条检测，支持 F2/F3 开关）

这三个文件适配自同项目的公开修复版
[Kisnnnnn/maplestory-auto-ds](https://github.com/Kisnnnnn/maplestory-auto-ds)（MIT），
如后续作者补了官方版本可直接替换。

## Star History

<a href="https://www.star-history.com/?repos=zhoufanglu%2Fmaplestory-auto-ds&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=zhoufanglu/maplestory-auto-ds&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=zhoufanglu/maplestory-auto-ds&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=zhoufanglu/maplestory-auto-ds&type=date&legend=top-left" />
 </picture>
</a>
