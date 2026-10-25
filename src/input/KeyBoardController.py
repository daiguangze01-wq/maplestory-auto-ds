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

# Library import
import pyautogui
from pynput import keyboard

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
    from src.utils.common import activate_game_window

if is_mac():
    import Quartz
else:
    import pygetwindow as gw

pyautogui.PAUSE = 0  # remove delay

# All simulated input shares one lock.  Health monitoring runs in a separate
# thread, so without this lock a potion press can interleave with an attack or
# movement transition and leave DirectInput in an inconsistent key state.
_INPUT_LOCK = threading.RLock()


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


def key_down(key):
    """Press key down (scan-code based for DirectInput games)."""
    if not key:
        return
    with _INPUT_LOCK:
        try:
            if HAS_PYDIRECTINPUT and not is_mac():
                k = _norm_key(key)
                if k in pydirectinput.KEYBOARD_MAPPING:
                    pydirectinput.keyDown(k)
                    return
            pyautogui.keyDown(key)
        except pyautogui.FailSafeException:
            logger.error("[KeyBoardController] FailSafe triggered: mouse in corner.")
        except Exception as e:
            logger.error(f"[KeyBoardController] key_down({key}) failed: {e}")


def key_up(key):
    """Release key (scan-code based for DirectInput games)."""
    if not key:
        return
    with _INPUT_LOCK:
        try:
            if HAS_PYDIRECTINPUT and not is_mac():
                k = _norm_key(key)
                if k in pydirectinput.KEYBOARD_MAPPING:
                    pydirectinput.keyUp(k)
                    return
            pyautogui.keyUp(key)
        except pyautogui.FailSafeException:
            logger.error("[KeyBoardController] FailSafe triggered: mouse in corner.")
        except Exception as e:
            logger.error(f"[KeyBoardController] key_up({key}) failed: {e}")


def recover_mouse():
    """Move mouse to a safe position to avoid accidental clicks."""
    try:
        screen_w, screen_h = pyautogui.size()
        pyautogui.moveTo(screen_w // 2, 20)
    except Exception:
        pass


def press_key(key, duration=0.05):
    """Press a key for a short duration via the active key backend."""
    if not key:
        return
    with _INPUT_LOCK:
        key_down(key)
        time.sleep(duration)
        key_up(key)


class KeyBoardController():
    """
    KeyBoardController — runs a daemon thread that continuously applies
    the current movement/action command to the game window.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.cmd_action = "none"
        self.cmd_up_down = "none"
        self.cmd_left_right = "none"
        self.cmd_up_down_last = ""
        self.cmd_left_right_last = ""
        self.cmd_action_last = ""
        self.window_title = cfg["game_window"]["title"]
        self.fps = 0  # Frame per seconds

        # Timers
        self.t_last_run = time.time()
        self.t_last_skill = 0.0  # Last attack/skill time
        self.t_last_attack = 0.0
        self.t_last_aoe = 0.0

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
        self.attack_cooldown = max(
            0.0, float(cfg.get("directional_attack", {}).get("cooldown", 0.9))
        )
        self.aoe_cooldown = max(
            0.0, float(cfg.get("aoe_skill", {}).get("cooldown", 1.0))
        )

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
        self.is_enable = not self.is_enable
        logger.info(f"KB toggled, is_enable: {self.is_enable}")
        # Make sure all keys are released
        self.release_all_key()

    def toggle(self):
        """Alias of toggle_enable (used by main.py F1 handler)."""
        self.toggle_enable()

    def disable(self):
        """Disable keyboard controller."""
        self.is_enable = False
        self.release_all_key()

    def enable(self):
        """Enable keyboard controller."""
        self.is_enable = True

    def stop(self):
        """Stop the controller thread and release all keys."""
        self.is_terminated = True
        self.release_all_key()

    def set_command(self, new_command):
        """
        Set keyboard command. Format: "<left_right> <up_down> <action>"
        e.g. "left none jump", "stop stop stop", "none none attack"
        """
        self.cmd_left_right, self.cmd_up_down, self.cmd_action = new_command.split()

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
                title = win32gui.GetWindowText(hwnd)
                return self.window_title in title
            except Exception:
                return False

    def release_all_key(self):
        """Release all movement keys and the attack key."""
        with _INPUT_LOCK:
            key_up("left")
            key_up("right")
            key_up("up")
            key_up("down")
            key_up(self.attack_key)

    def press_key_safe(self, key, duration=0.05, action_name="key"):
        """Press only while this controller and the game window are active.

        HealthMonitor uses this method instead of the module-level press_key so
        potion and return-home actions can never leak into another application.
        """
        if self.is_terminated or not self.is_enable:
            logger.debug(f"[KeyBoardController] Skipped {action_name}: controller disabled")
            return False
        if not self.is_game_window_active():
            logger.warning(
                f"[KeyBoardController] Skipped {action_name}: game window not focused"
            )
            return False
        with _INPUT_LOCK:
            # Re-check after waiting for another input sequence to finish.
            if not self.is_game_window_active():
                logger.warning(
                    f"[KeyBoardController] Skipped {action_name}: focus changed"
                )
                return False
            press_key(key, duration)
        return True

    def limit_fps(self):
        """Cap loop rate to fps_limit."""
        target_duration = 1.0 / self.fps_limit
        frame_duration = time.time() - self.t_last_run
        if frame_duration < target_duration:
            time.sleep(target_duration - frame_duration)
        self.fps = round(1.0 / max(time.time() - self.t_last_run, 0.001))
        self.t_last_run = time.time()

    def run(self):
        """Main control loop (daemon thread)."""
        t_last_inactive_warn = 0.0
        t_last_activate_attempt = 0.0
        was_window_active = False
        while not self.is_terminated:
            if not self.is_enable:
                self.limit_fps()
                continue

            window_active = self.is_game_window_active()
            if not window_active:
                if was_window_active:
                    self.release_all_key()
                    self.cmd_left_right_last = ""
                    self.cmd_up_down_last = ""
                    self.cmd_action_last = ""
                    logger.warning(
                        "[KeyBoardController] Game focus lost; all held keys released."
                    )
                was_window_active = False
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
                continue
            was_window_active = True

            # Optional buff skills
            for i, buff_skill_key in enumerate(self.buff_keys):
                cooldown = self.buff_cooldowns[i] if i < len(self.buff_cooldowns) else 60
                if time.time() - self.t_last_buff_cast[i] >= cooldown and \
                        time.time() - self.t_last_skill > self.buff_action_cooldown:
                    press_key(buff_skill_key)
                    logger.info(f"[Buff] Press buff skill key: '{buff_skill_key}'")
                    self.t_last_buff_cast[i] = time.time()
                    self.t_last_skill = time.time()
                    break

            # Force heal
            if self.is_need_force_heal:
                self.cmd_action = "add_hp"

            # ---------------- Periodic loot pickup ----------------
            if self.pickup_key and self.pickup_interval > 0 and \
                    time.time() - self.t_last_pickup >= self.pickup_interval:
                # Stop attacking briefly, then tap the pickup key a few times
                key_up(self.attack_key)
                key_up("left")
                key_up("right")
                for _ in range(self.pickup_taps):
                    press_key(self.pickup_key, duration=0.04)
                    time.sleep(0.09)
                logger.info(f"[Pickup] Tapped pickup key '{self.pickup_key}' x{self.pickup_taps}")
                self.t_last_pickup = time.time()
                self.t_last_skill = time.time()
                self.cmd_action = "none"

            # ---------------- Left-Right ----------------
            if self.cmd_left_right == "left":
                key_up("right")
                key_down("left")
            elif self.cmd_left_right == "right":
                key_up("left")
                key_down("right")
            elif self.cmd_left_right in ("face_left", "face_right"):
                # Turn to face direction without walking: tap the arrow key once.
                face_key = "left" if self.cmd_left_right == "face_left" else "right"
                opposite_key = "right" if face_key == "left" else "left"
                key_up(opposite_key)
                if self.cmd_left_right_last != self.cmd_left_right:
                    press_key(face_key, duration=0.03)
            elif self.cmd_left_right == "stop":
                key_up("left")
                key_up("right")
            elif self.cmd_left_right == "none":
                if self.cmd_left_right_last not in ("none",):
                    key_up("left")
                    key_up("right")
            else:
                logger.error("[KeyBoardController] Unsupported left-right command: "
                             f"{self.cmd_left_right}")
            self.cmd_left_right_last = self.cmd_left_right

            # ---------------- Up-Down ----------------
            if self.cmd_up_down == "up":
                key_up("down")
                key_down("up")
            elif self.cmd_up_down == "down":
                key_up("up")
                key_down("down")
            elif self.cmd_up_down == "stop":
                key_up("up")
                key_up("down")
            elif self.cmd_up_down == "none":
                if self.cmd_up_down_last != "none":
                    key_up("up")
                    key_up("down")
            else:
                logger.error("[KeyBoardController] Unsupported up-down command: "
                             f"{self.cmd_up_down}")
            self.cmd_up_down_last = self.cmd_up_down

            # ---------------- Action ----------------
            if self.cmd_action == "jump":
                if self.cmd_action_last != "jump":
                    logger.info(f"[Jump] Pressing jump key "
                                f"'{self.cfg['key'].get('jump', '?')}'")
                press_key(self.cfg["key"]["jump"], duration=0.08)
            elif self.cmd_action == "double_jump":
                press_key(self.cfg["key"]["jump"], duration=0.06)
                time.sleep(0.05)
                press_key(self.cfg["key"]["jump"], duration=0.06)
            elif self.cmd_action == "teleport":
                press_key(self.cfg["key"]["teleport"])
            elif self.cmd_action == "double_teleport":
                press_key(self.cfg["key"]["teleport"])
                time.sleep(0.05)
                press_key(self.cfg["key"]["teleport"])
            elif self.cmd_action == "attack":
                now = time.time()
                if now - self.t_last_attack >= self.attack_cooldown:
                    press_key(self.attack_key)
                    self.t_last_attack = now
                    self.t_last_skill = now
            elif self.cmd_action == "aoe":
                now = time.time()
                if self.aoe_key and now - self.t_last_aoe >= self.aoe_cooldown:
                    press_key(self.aoe_key)
                    self.t_last_aoe = now
                    self.t_last_skill = now
            elif self.cmd_action == "add_hp":
                press_key(self.cfg["key"]["add_hp"])
                self.cmd_action = "none"
            elif self.cmd_action == "add_mp":
                press_key(self.cfg["key"]["add_mp"])
                self.cmd_action = "none"
            elif self.cmd_action in ("goal", "none", "stop"):
                pass
            else:
                logger.error("[KeyBoardController] Unsupported action command: "
                             f"{self.cmd_action}")
            self.cmd_action_last = self.cmd_action

            self.limit_fps()

        self.release_all_key()
        logger.info("[KeyBoardController] terminated")
