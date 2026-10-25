"""
KeyBoardListener — global hotkey listener (F1~F12) for the autobot.

Adapted from MapleStoryAutoLevelUp (KenYu910645). Registers callable
handlers for function keys via register_func_key_handler('f1', fn).
"""
import threading
import time

import pygetwindow as gw
from pynput import keyboard

# Local import
from src.utils.logger import logger
from src.utils.common import is_mac

if is_mac():
    import Quartz


class KeyBoardListener():
    """
    KeyBoardListener
    """

    def __init__(self, cfg=None, is_autobot=True):
        self.cfg = cfg
        self.t_last_run = time.time()
        self.is_enable = True
        self.debounce_interval = 1  # second
        self.is_terminated = False
        self.fps = 0
        self.fps_limit = 30
        self.key_pressing = []  # keys currently pressed by user
        self.is_pressed_func_key = [False] * 12  # 'F1'...'F12'

        # Timer
        self.t_func_key = [0] * 12

        # Keys
        self.movement_keys = {
            keyboard.Key.up: "up",
            keyboard.Key.down: "down",
            keyboard.Key.left: "left",
            keyboard.Key.right: "right",
        }
        if cfg:
            special = {
                "space": keyboard.Key.space,
                "alt": keyboard.Key.alt,
                "ctrl": keyboard.Key.ctrl,
                "shift": keyboard.Key.shift,
                "enter": keyboard.Key.enter,
            }
            for semantic, configured in (
                ("jump", cfg.get("key", {}).get("jump", "space")),
                ("teleport", cfg.get("key", {}).get("teleport", "e")),
            ):
                name = str(configured).lower()
                pynput_key = special.get(name)
                if pynput_key is None and len(name) == 1:
                    pynput_key = keyboard.KeyCode.from_char(name)
                if pynput_key is not None:
                    self.movement_keys[pynput_key] = semantic
        self.func_keys = {
            getattr(keyboard.Key, f"f{i + 1}"): i for i in range(12)
        }

        self.func_key_handlers = {
            f"f{i}": self.do_nothing for i in range(1, 13)
        }

        # Start control thread
        if is_autobot:
            threading.Thread(target=self.run_for_autobot, daemon=True).start()
        else:
            self.window_title = cfg["game_window"]["title"]
            threading.Thread(target=self.run_for_route_recorder, daemon=True).start()

        listener = keyboard.Listener(on_press=self.on_press,
                                     on_release=self.on_release)
        listener.start()

    def do_nothing(self):
        pass

    def register_func_key_handler(self, key: str, handler: callable):
        """Register a handler for a function key like 'f1'...'f12'."""
        key = key.lower()
        if key in self.func_key_handlers:
            self.func_key_handlers[key] = handler
        else:
            logger.warning(f"[KeyBoardListener] '{key}' is not a supported function key.")

    def on_release(self, key):
        if key in self.movement_keys:
            try:
                self.key_pressing.remove(self.movement_keys[key])
            except ValueError:
                pass

    def on_press(self, key):
        try:
            if key in self.movement_keys:
                if self.movement_keys[key] not in self.key_pressing:
                    self.key_pressing.append(self.movement_keys[key])
            elif key in self.func_keys:
                idx = self.func_keys[key]  # 0-based index
                if not self.is_pressed_func_key[idx]:
                    self.is_pressed_func_key[idx] = True
                    self.t_func_key[idx] = time.time()
                    handler = self.func_key_handlers[f"f{idx + 1}"]
                    if handler:
                        try:
                            handler()
                        except Exception as e:
                            logger.error(f"[KeyBoardListener] Handler error: {e}")
        except Exception as e:
            logger.error(f"[KeyBoardListener] on_press error: {e}")

    def toggle_enable(self):
        self.is_enable = not self.is_enable
        logger.info(f"[KeyBoardListener] is_enable: {self.is_enable}")

    def disable(self):
        self.is_enable = False

    def enable(self):
        self.is_enable = True

    def stop(self):
        self.is_terminated = True

    def is_game_window_active(self):
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
            try:
                active_window = gw.getActiveWindow()
                if not active_window:
                    return False
                return self.window_title in active_window.title
            except Exception:
                return False

    def limit_fps(self):
        target_duration = 1.0 / self.fps_limit
        frame_duration = time.time() - self.t_last_run
        if frame_duration < target_duration:
            time.sleep(target_duration - frame_duration)
        self.fps = round(1.0 / max(time.time() - self.t_last_run, 0.001))
        self.t_last_run = time.time()

    def run_for_route_recorder(self):
        while not self.is_terminated:
            if not self.is_enable or not self.is_game_window_active():
                self.limit_fps()
                continue
            self.limit_fps()

    def run_for_autobot(self):
        while not self.is_terminated:
            self.limit_fps()
        logger.info("[KeyBoardListener] Terminated")
