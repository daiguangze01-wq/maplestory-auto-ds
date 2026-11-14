# 客户端侧检测面：反作弊在玩家机器上能看到什么

> 仅基于公开资料讨论“能观察到什么、如何判断”，不含任何绕过或注入实现。标记：【确证】有官方文档 / 厂商页面 / 论文直接支持；【推断】由文档化行为推导；【传言】仅社区说法。

## 1. 输入注入检测：四条路径、三种可见性

| 路径 | 进入系统输入流 | 低层钩子 / 注入标志 | GetAsyncKeyState | Raw Input 设备归因 |
|---|---|---|---|---|
| SendInput | 是 | 可见，带 LLKHF_INJECTED / LLMHF_INJECTED | 可见 | 通常无（见下，未确证） |
| keybd_event | 是（旧接口，已被 SendInput 取代） | 可见，通常同样被标记 | 可见 | 无 |
| PostMessage | 否，只进目标线程消息队列 | 不可见 | 不可见 | 无 |
| SendMessage | 否，直接调用窗口过程 | 不可见 | 不可见 | 无 |

微软自己的 UI 自动化命令行文档把这组差异写得最直白：默认的 `post-message` 传输“无法触发通过 WH_KEYBOARD_LL 注册的全局热键”，用 GetAsyncKeyState 读键状态的应用“可能观察不到按住的修饰键”；而 `send-input`“产生完全真实的输入（修饰键对 GetAsyncKeyState 可见，并触发低层钩子）”（https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/ui-automation）。【确证】

低层钩子的判据是结构体标志位：KBDLLHOOKSTRUCT.flags 的 bit4 为 LLKHF_INJECTED（是否被注入）、bit1 为 LLKHF_LOWER_IL_INJECTED（是否来自更低完整性级别）；MSLLHOOKSTRUCT.flags 的 bit0 为 LLMHF_INJECTED（https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-kbdllhookstruct；https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-msllhookstruct）。【确证】低层钩子只能全局安装，但回调在安装线程执行、不向目标进程注入 DLL（https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexa），因此反作弊自装一个钩子成本很低；局限是它只看得见“被 OS 标记的注入”，看不见没走输入流的操作。【推断】

Raw Input 是用户态最直接的“输入→设备”归因通道：按 TLC（UsagePage 0x01 / UsageID 0x06 即键盘）注册后可拿设备句柄，能区分“同一类型的两只鼠标”，并处理 1000Hz 级高频事件（https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input）。【确证】但它能否看到 SendInput 事件，官方未承诺、社区实验结论不一，应按【未确证】处理。

GetAsyncKeyState 轮询是脚本常见形态：它读全局键状态，受 UIPI 与活动桌面限制，最低位“最近按下”标志不可靠（https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getasynckeystate）。【确证】它本身不产生输入事件，所以“谁在读键状态”在用户态没有直接痕迹，只能靠进程/模块与输入流不匹配来间接判断。【推断】UI Automation 的 InvokePattern/ValuePattern 是“驱动控件”而非模拟输入；微软的 UIA 命令行工具默认用 post-message 投递按键，同样过不了低层钩子，对 Raw Input 游戏基本无效。【确证】

## 2. 硬件宏与 HID 伪装

物理形态可分四类：原生 USB HID 单片机（Arduino、Pro Micro、Pico）；USB-串口桥（CH340/CP210x）加上位机——桥本身只提供串口、以串口或厂商自定义类枚举，不构成 HID，要冒充键鼠还需原生 USB MCU 或专用串口转 HID 器件；盒子类（KMBox/KMBox Net）经串口或网口收指令、再以 HID 键鼠身份上报；双机/采集卡把“看画面”和“发输入”整体移出游戏主机。

反作弊的枚举能力是文档化的：HidD_GetAttributes 可读 VID/PID/版本号（https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/hidsdi/nf-hidsdi-hidd_getattributes），再配合设备路径、HID Usage 与报告描述符解析，就能给每个输入源建档。【确证】

区分“真键鼠”与“伪装 HID”的信号，可信度差别很大：

- 【确证】厂商确实在做设备层识别：XIGNCODE3 官方功能列表明确写有“检测并阻断各类宏输入设备”（https://wellbia.com/?module=Html&action=SiteComp&sSubNo=1）。
- 【确证】描述符与 VID/PID 可伪造：KMBox 官方自述“支持自定义 USB 所有描述符”“回报率 100–8000Hz 可调”“支持监控物理键鼠、屏蔽物理键鼠”（https://www.kmbox.top/productIntro.html）。单看 VID/PID 只能筛掉偷懒的设备。
- 【推断】低成本 HID 的常见指纹：单一 TLC、无 LED 输出报告、字符串描述符缺失或与 VID 不符、报告率恒定、按键间隔方差极小、拓扑上是 Hub 下孤立的“键盘”。
- 【传言】“电气特性”（USB 帧/微帧时序抖动、信号完整性）：需主机控制器或内核 USB 栈级观测，公开资料没有产品化证据，目前更像研究方向。

所以硬件宏的对抗重点不在“像不像人”，而在设备归因是否自洽：一个从未被物理输入使用、却持续产生按键的 HID 端点本身就是异常。【推断】

## 3. 进程 / 内存 / 模块层

- 句柄与进程：枚举系统句柄表、找谁持有游戏进程的读写/注入权限句柄是标准做法（Sysinternals Handle 即公开实现，https://learn.microsoft.com/en-us/sysinternals/downloads/handle）；进程/线程/模块用 CreateToolhelp32Snapshot 或 EnumProcessModules 枚举（https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-createtoolhelp32snapshot）。【确证】
- 完整性与签名：模块列表与磁盘 PE 比对、签名/路径/时间戳校验。BattlEye 官方 FAQ（Ubisoft 转载）称其“扫描内存与磁盘文件”（https://www.ubisoft.com/en-gb/game/rainbow-six/siege/news-updates/2999gdyIXLbeiZP78R7545/battleye-faq）；GameGuard 官方称用 EXE/DLL 加密、CRC 与 MD5/Binary 模式库（https://nprotect.com/products/nprotect/gameguard）。【确证】
- 调试器：IsDebuggerPresent（底层即 PEB 的 BeingDebugged 标志）与 CheckRemoteDebuggerPresent（独立进程调试器）（https://learn.microsoft.com/en-us/windows/win32/api/debugapi/nf-debugapi-isdebuggerpresent）；更深一层是线程上下文里的调试寄存器（硬件断点）与时间差检测（GetThreadContext，https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getthreadcontext）。【确证】
- 注入与钩子：全局钩子（WH_KEYBOARD、WH_CBT 等）会把 DLL 注入同桌面进程（SetWindowsHookEx 文档），反作弊在自身模块列表里就能找到“不该出现的钩子 DLL”；WH_KEYBOARD_LL/WH_MOUSE_LL 在安装线程回调、不注入，只能靠行为发现。【确证 + 推断】
- 窗口与 overlay：EnumWindows（或 FindWindow）枚举顶层窗口，再用类名/标题匹配宏工具、调试器、overlay（https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumwindows）。【确证】反向的“反捕获”是 SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)，让窗口内容不被截取（https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity）；“检测别人在捕获我”则依赖窗口/模块/图形栈，公开细节很少。【推断】
- 虚拟机/沙箱：CPUID 的 hypervisor 位、时序、设备与驱动指纹是经典手法，学界视其为长期军备竞赛（BareCloud，https://www.usenix.org/conference/usenixsecurity14/technical-sessions/presentation/kirat）。【确证/推断】

## 4. 命名与事实

| 方案 | 客户端做什么 | 内核态 | 证据强度 |
|---|---|---|---|
| nProtect GameGuard（INCA） | 反 speedhack/宏/挂钩/调试；EXE/DLL 加密、CRC、客户端-服务端双重加密认证 | 官方页未写，社区长期称带驱动 | 功能【确证】/内核【未确证】（https://nprotect.com/products/nprotect/gameguard） |
| XIGNCODE3（Wellbia） | OS 层检测、宏输入设备检测、外部连接检测、模式与白名单、禁多开、资源防改 | 未公开 | 【确证】（https://wellbia.com/?module=Html&action=SiteComp&sSubNo=1） |
| BattlEye | 主动拦截 + 内存/磁盘扫描；驱动加载错误、测试签名模式报错 | 是，论文称 Windows 内核级 | 【确证】（https://www.ubisoft.com/en-gb/game/rainbow-six/siege/news-updates/2999gdyIXLbeiZP78R7545/battleye-faq；https://hal.science/hal-05482209） |
| EAC（Epic） | 官方称“分层”“预防优先”（https://www.easy.ac/services） | 官方未明说，社区普遍观察到驱动加载报错 | 【部分确证】 |
| Vanguard（Riot） | 内核驱动、开机启动、可阻止文件加载、默认启用接近 Secure Boot 的安全标准 | 是 | 【确证】（https://support.riotgames.com/en-us/riot/performance/what-is-vanguard） |
| BlackCipher / NGS（Nexon） | 公开技术资料极少 | 社区称内核 + 多进程 | 【未确证】 |

公开绕过案例确实存在：CheckMATE 2025 的 BattlEye 论文以动态 + 静态逆向拆解其架构并给出 PoC（https://hal.science/hal-05482209），说明“未文档化”不等于“不可逆”；本文不复述其方法。

## 5. 类别级做法与被打掉的位置

| 类别 | 客户端留下的痕迹 | 主要打掉机制 |
|---|---|---|
| 用户态模拟输入 | 注入标志、无设备归因 | 低层钩子标志 + Raw Input 设备比对 |
| 消息直写窗口 | 不进输入流，游戏不消费 | 改用 Raw Input；校验消息来源与焦点 |
| 轮询键状态 | 不产生输入事件 | 进程/模块扫描 + 时序相关性 + 服务端统计 |
| 硬件宏/盒子 | 真实 HID，仅设备指纹可疑 | 设备枚举/描述符/拓扑/报告率 + 行为统计 |
| 双机/采集卡 | 主机侧几乎无痕 | 只剩服务端行为分析 |
| 内存修改 / DLL 注入 | 句柄、异常模块、可执行内存 | 句柄扫描、模块完整性、内核回调 |
| 调试 / 逆向 | 调试器与断点痕迹 | 调试器检测、时间检测、反调试 |
| Overlay / 视觉辅助 | 顶层窗口、异常模块 | 窗口枚举、模块扫描、有限的捕获保护 |

## 6. 这一层对抗的胜负手

- 归因比形态更值钱。判断“输入像不像人”会被时序伪装骗过；判断“这条输入来自哪个设备、哪条栈”才是硬约束——硬件宏有效，正因为它把归因做成了真的。
- 内核态是双刃剑。观测点能下移到驱动/回调/USB 栈，但稳定性、隐私与兼容成本高，内核层同样是不对称攻防（BattlEye 被完整逆向即是例子）。
- 用户态检测是“成本税”。一行 LLKHF_INJECTED 很便宜，但可被内核级手段绕过，还会误伤屏幕阅读器、远程桌面与输入法；厂商因此普遍用组合信号 + 服务端兜底。
- 服务端是最后防线。反应时间分布、输入熵、命中率与路径统计，对双机、采集卡几乎是唯一手段，也是客户端误报的仲裁者。
- 一句话：客户端检测面擅长“提高成本、提供证据链”，不擅长“证明清白”；胜负手是设备归因、内核完整性与服务端统计三者的组合，而非某一个 API。
