"""
KeyBoardController
Simulate user keyboard input to control character in the game.

Adapted from MapleStoryAutoLevelUp (KenYu910645) for maplestory-auto-ds:
- Removed buff_skill dependency (config may not define it)
- Config keys use safe .get() defaults
- Added toggle() alias and stop() for main.py compatibility
"""
import threading
import time
from contextlib import closing

# Library import
import pyautogui

try:
    import pydirectinput
    pydirectinput.PAUSE = 0  # remove delay
    HAS_PYDIRECTINPUT = True
except ImportError:
    HAS_PYDIRECTINPUT = False

# Local import
from src.utils.logger import logger
from src.utils.common import is_mac, is_windows
if is_windows():
    import win32api
    import win32con
    import win32process
    import win32security
    from src.utils.common import activate_game_window

if is_mac():
    import Quartz

pyautogui.PAUSE = 0  # remove delay

# All simulated input shares one lock.  Health monitoring runs in a separate
# thread, so without this lock a potion press can interleave with an attack or
# movement transition and leave DirectInput in an inconsistent key state.
_INPUT_LOCK = threading.RLock()
_PRESSED_KEYS = set()


def _integrity_level(process):
    with closing(win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)) as token:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
        return sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)


def _game_has_higher_integrity(hwnd):
    if not hwnd or not is_windows():
        return False
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        with closing(win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)) as game:
            return _integrity_level(game) > _integrity_level(win32api.GetCurrentProcess())
    except win32api.error as exc:
        logger.warning(f"[Input] Could not compare Windows process integrity: {exc}")
        return False


def _norm_key(key):
    """Normalize key name for pydirectinput (scan-code based)."""
    k = str(key).lower().strip()
    alias = {
        "control": "ctrl",
        "pgup": "pageup",
        "pgdn": "pagedown",
        "pgdown": "pagedown",
        "escape": "escape",
    }
    return alias.get(k, k)


def get_pressed_keys():
    """Snapshot for the display thread, including any tap currently in flight."""
    return sorted(_PRESSED_KEYS.copy())


def _set_key_state(key, down):
    """Send one transition and remember it only after the backend succeeds."""
    if not key:
        return False
    key = _norm_key(key)
    with _INPUT_LOCK:
        if (key in _PRESSED_KEYS) == down:
            return True
        try:
            if HAS_PYDIRECTINPUT and not is_mac() and key in pydirectinput.KEYBOARD_MAPPING:
                send = pydirectinput.keyDown if down else pydirectinput.keyUp
                if not send(key):
                    return False
            else:
                if not pyautogui.isValidKey(key):
                    return False
                send = pyautogui.keyDown if down else pyautogui.keyUp
                send(key)
        except Exception as e:
            logger.error(f"[KeyBoardController] Key transition failed ({key}, down={down}): {e}")
            return False
        if down:
            _PRESSED_KEYS.add(key)
        else:
            _PRESSED_KEYS.remove(key)
        return True


def key_down(key):
    return _set_key_state(key, True)


def key_up(key):
    return _set_key_state(key, False)


def recover_mouse():
    """Move mouse to a safe position to avoid accidental clicks."""
    try:
        screen_w, screen_h = pyautogui.size()
        pyautogui.moveTo(screen_w // 2, 20)
    except Exception:
        pass


def press_key(key, duration=0.05):
    """Tap a key, restoring a movement hold if the bindings overlap."""
    if not key:
        return False
    with _INPUT_LOCK:
        was_held = _norm_key(key) in _PRESSED_KEYS
        if was_held and not key_up(key):
            return False
        if not key_down(key):
            return False
        try:
            time.sleep(duration)
        finally:
            success = key_up(key)
            if was_held and success:
                success = key_down(key)
        return success


class KeyBoardController():
    """
    KeyBoardController — runs a daemon thread that continuously applies
    the current movement/action command to the game window.
    """

    def __init__(self, cfg, window_hwnd=None):
        self.cfg = cfg
        self._command = ("none", "none", "none")
        self._last_face = None
        self._last_action = None
        self.jump_attack = cfg["bot"].get("jump_attack", False)
        self.jump_attack_interval = float(cfg["bot"].get("jump_attack_interval", 0.65))
        self._next_jump_attack = 0.0
        self.window_title = cfg["game_window"]["title"]
        self.window_hwnd = window_hwnd
        self.game_has_higher_integrity = _game_has_higher_integrity(window_hwnd)
        self.input_status = "Waiting"
        if self.game_has_higher_integrity:
            logger.warning("[Input] Game has higher Windows integrity. 请用普通权限启动游戏，"
                           "或右键 run.bat 选择“以管理员身份运行”。")
        self.fps = 0  # Frame per seconds

        # Timers
        self.t_last_run = time.time()
        self.t_last_skill = 0.0  # Last attack/skill time

        # Flags
        self.is_enable = True
        self.is_need_force_heal = False
        self.is_terminated = False

        # Parameters (config keys may be absent in this project's config)
        system_cfg = cfg.get("system", {})
        self.fps_limit = system_cfg.get("fps_limit_keyboard_controller", 30)
        self.auto_focus = bool(system_cfg.get("auto_focus", False))
        self.focus_retry_seconds = max(
            1.0, float(system_cfg.get("focus_retry_seconds", 3.0))
        )

        # Buff skills are optional in this project
        self.buff_keys = cfg.get("buff_skill", {}).get("keys", [])
        self.buff_cooldowns = cfg.get("buff_skill", {}).get("cooldown", [])
        self.buff_action_cooldown = cfg.get("buff_skill", {}).get("action_cooldown", 1.0)
        self.t_last_buff_cast = [0] * len(self.buff_keys)

        # Set up attack keys
        self.attack_key = ""
        self.aoe_key = cfg["key"].get("aoe_skill", "")
        if cfg["bot"]["attack"] == "aoe_skill":
            self.attack_key = cfg["key"]["aoe_skill"]
        elif cfg["bot"]["attack"] == "directional":
            self.attack_key = cfg["key"]["directional_attack"]
        else:
            raise ValueError(f"Unexpected attack type: {cfg['bot']['attack']}")

        # Periodic loot pickup
        self.pickup_key = cfg["key"].get("pickup", "")
        self.pickup_interval = cfg.get("bot", {}).get("pickup_interval", 0)
        self.pickup_taps = max(1, cfg.get("bot", {}).get("pickup_taps", 3))
        self.t_last_pickup = time.time()

        # Start keyboard control thread
        threading.Thread(target=self.run, daemon=True).start()

        logger.info("[KeyBoardController] Init done")

    def toggle_enable(self):
        """Toggle keyboard control on/off."""
        with _INPUT_LOCK:
            if self.is_enable:
                self.disable()
            else:
                self.enable()
            logger.info(f"KB toggled, is_enable: {self.is_enable}")

    def toggle(self):
        """Alias of toggle_enable (used by main.py F1 handler)."""
        self.toggle_enable()

    def disable(self):
        """Disable keyboard controller."""
        with _INPUT_LOCK:
            self.is_enable = False
            self.release_all_key()

    def enable(self):
        """Enable keyboard controller."""
        with _INPUT_LOCK:
            self.is_enable = True

    def stop(self):
        """Stop the controller thread and release all keys."""
        with _INPUT_LOCK:
            self.is_terminated = True
            self.release_all_key()

    def set_command(self, new_command):
        """
        Set keyboard command. Format: "<left_right> <up_down> <action>"
        e.g. "left none jump", "stop stop stop", "none none attack"
        """
        move_x, move_y, action = new_command.split()
        self._command = (move_x, move_y, action)

    def is_game_window_active(self):
        """Check if the game window is currently the foreground window."""
        if is_mac():
            active_window = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID
            )
            for window in active_window:
                window_name = window.get(Quartz.kCGWindowName, '')
                if window_name and self.window_title in window_name:
                    return True
            return False
        else:
            # Use win32gui directly — pygetwindow sometimes caches stale
            # data and reports the wrong foreground window title.
            try:
                import win32gui
                hwnd = win32gui.GetForegroundWindow()
                if not hwnd:
                    return False
                if self.window_hwnd is not None:
                    return hwnd == self.window_hwnd
                title = win32gui.GetWindowText(hwnd)
                return self.window_title in title
            except Exception:
                return False

    def release_all_key(self):
        """Leave the current action and release exactly the keys we own."""
        with _INPUT_LOCK:
            for key in get_pressed_keys():
                key_up(key)
            self._last_face = None
            self._last_action = None

    pressed_keys = staticmethod(get_pressed_keys)

    def press_key_safe(self, key, duration=0.05, action_name="key"):
        """Press only while this controller and the game window are active.

        HealthMonitor uses this method instead of the module-level press_key so
        potion and return-home actions can never leak into another application.
        """
        with _INPUT_LOCK:
            if self.is_terminated or not self.is_enable or self.game_has_higher_integrity:
                return False
            if not self.is_game_window_active():
                logger.warning(
                    f"[KeyBoardController] Skipped {action_name}: focus changed"
                )
                return False
            return press_key(key, duration)

    def limit_fps(self):
        """Cap loop rate to fps_limit."""
        target_duration = 1.0 / self.fps_limit
        frame_duration = time.time() - self.t_last_run
        if frame_duration < target_duration:
            time.sleep(target_duration - frame_duration)
        self.fps = round(1.0 / max(time.time() - self.t_last_run, 0.001))
        self.t_last_run = time.time()

    def _apply_holds(self, move_x, move_y, action):
        """Release obsolete keys, establish movement/facing, then hold the attack key."""
        held = []
        face = None
        if move_x in ("left", "right"):
            held.append(move_x)
        elif move_x in ("face_left", "face_right"):
            face = move_x[len("face_"):]
        elif move_x not in ("none", "stop"):
            logger.error(f"[KeyBoardController] Unsupported horizontal command: {move_x}")
        if move_y in ("up", "down"):
            held.append(move_y)
        elif move_y not in ("none", "stop"):
            logger.error(f"[KeyBoardController] Unsupported vertical command: {move_y}")

        attack = {"attack": self.attack_key, "aoe": self.aoe_key}.get(action)
        desired = set(held)
        if attack and not self.jump_attack:
            desired.add(_norm_key(attack))
            if face and face != self._last_face:
                # Turning can interrupt the skill. Finish the turn before
                # starting a new attack hold even when both skills share S.
                desired.discard(_norm_key(attack))
        for key in sorted(_PRESSED_KEYS - desired):
            if not key_up(key):
                return False
        for key in held:
            if not key_down(key):
                return False
        if face:
            if face != self._last_face and not press_key(face, duration=0.03):
                return False
            self._last_face = face
        elif not attack or move_x in ("left", "right"):
            self._last_face = None
        if attack is not None and not self.jump_attack:
            if not self.is_game_window_active() or not key_down(attack):
                self.input_status = "Attack key not held"
                return False
            self.t_last_skill = time.time()
        return True

    def _apply_action(self, action):
        if self.jump_attack and action in ("attack", "aoe"):
            now = time.monotonic()
            if now >= self._next_jump_attack:
                self._next_jump_attack = now + self.jump_attack_interval
                if self.press_key_safe(self.cfg["key"]["jump"], 0.08, "jump attack"):
                    # The detector can withdraw the attack while C is down.
                    current = self._command[2]
                    if current in ("attack", "aoe"):
                        key = self.attack_key if current == "attack" else self.aoe_key
                        if self.press_key_safe(key, 0.06, "airborne attack"):
                            self.t_last_skill = time.time()
            self._last_action = action
            return
        if action != self._last_action:
            keys = self.cfg["key"]
            if action in ("double_jump", "double_teleport"):
                key = keys[action[len("double_"):]]
                duration = 0.06 if action == "double_jump" else 0.05
                if not press_key(key, duration):
                    return
                time.sleep(0.05)
                if not self.press_key_safe(key, duration, action):
                    return
            elif action in ("jump", "teleport", "add_hp", "add_mp"):
                duration = 0.08 if action == "jump" else 0.05
                if not press_key(keys[action], duration):
                    return
            elif action not in ("goal", "none", "stop", "attack", "aoe"):
                logger.error(f"[KeyBoardController] Unsupported action command: {action}")
        self._last_action = action

    def _pickup(self):
        """Pause horizontal movement for pickup. The next tick restores current intent."""
        with _INPUT_LOCK:
            for key in _PRESSED_KEYS.difference({"up", "down"}):
                if not key_up(key):
                    return
        for _ in range(self.pickup_taps):
            if not self.press_key_safe(self.pickup_key, duration=0.04, action_name="pickup"):
                return
            time.sleep(0.09)
        logger.info(f"[Pickup] Tapped pickup key '{self.pickup_key}' x{self.pickup_taps}")
        self.t_last_pickup = self.t_last_skill = time.time()

    def _tick(self):
        """Apply the latest complete command while input is enabled and focused."""
        with _INPUT_LOCK:
            if self.is_terminated or not self.is_enable:
                self.input_status = "Paused"
                self.release_all_key()
                return False
            if self.game_has_higher_integrity:
                self.input_status = "Game has higher Windows privileges"
                self.release_all_key()
                return False
            if not self.is_game_window_active():
                self.input_status = "Waiting for game focus"
                self.release_all_key()
                return False
            self.input_status = "Ready"

            for i, buff_key in enumerate(self.buff_keys):
                cooldown = self.buff_cooldowns[i] if i < len(self.buff_cooldowns) else 60
                now = time.time()
                if (now - self.t_last_buff_cast[i] >= cooldown
                        and now - self.t_last_skill > self.buff_action_cooldown):
                    if self.press_key_safe(buff_key, action_name="buff"):
                        logger.info(f"[Buff] Press buff skill key: '{buff_key}'")
                        self.t_last_buff_cast[i] = self.t_last_skill = time.time()
                    break

            pickup_due = (self.pickup_key and self.pickup_interval > 0
                          and time.time() - self.t_last_pickup >= self.pickup_interval)
            if not pickup_due:
                move_x, move_y, action = self._command
                if self.is_need_force_heal:
                    action = "add_hp"
                    self._last_action = None
                if action == "grab_ladder":
                    if self._last_action != action:
                        if not self._apply_holds("none", "none", "none"):
                            return False
                        if not self.press_key_safe(self.cfg["key"]["jump"], 0.08, "ladder jump"):
                            return False
                        self._last_action = action
                    if self._command[2] == action and self.is_game_window_active():
                        return self._apply_holds("none", "up", "none")
                    return False
                if self._apply_holds(move_x, move_y, action):
                    self._apply_action(action)
                elif self.input_status == "Ready":
                    self.input_status = "Key transition failed"
                return True

        self._pickup()
        return True

    def run(self):
        """Main control loop (daemon thread)."""
        t_last_inactive_warn = 0.0
        t_last_activate_attempt = 0.0
        try:
            while not self.is_terminated:
                active = self._tick()
                if not active and self.input_status == "Waiting for game focus":
                    # Auto-pull the game window back to the foreground instead of
                    # silently dropping all keys. Throttled to avoid spamming.
                    now = time.time()
                    if self.auto_focus and is_windows() and self.window_title and \
                            now - t_last_activate_attempt > self.focus_retry_seconds:
                        activate_game_window(self.window_title)
                        t_last_activate_attempt = now
                    if now - t_last_inactive_warn > 5.0:
                        focus_msg = (
                            "Trying to re-activate the game window..."
                            if self.auto_focus else
                            "Automation is paused until the game regains focus."
                        )
                        logger.warning(
                            "[KeyBoardController] Game window not focused "
                            f"(is_enable={self.is_enable}) — keys are NOT being sent. "
                            f"{focus_msg}"
                        )
                        t_last_inactive_warn = now
                self.limit_fps()
        finally:
            self.release_all_key()
            logger.info("[KeyBoardController] terminated")
