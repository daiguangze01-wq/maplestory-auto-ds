"""Capture the game client in physical pixels and publish BGR detection frames."""
import ctypes
from ctypes.wintypes import RECT
import threading
import time

import cv2

from src.utils.common import get_game_window_title_by_token, load_image
from src.utils.logger import logger


class GameWindowCapturor:
    def __init__(self, cfg, test_image_path=""):
        self.frame = None
        self.frame_time = None
        self.hwnd = None
        self.window_title = ""
        self._control = None
        self._lock = threading.Lock()
        self._last_size = None
        settings = cfg["game_window"]
        if test_image_path:
            image = load_image(test_image_path)
            self.frame = image[settings.get("title_bar_height", 0):]
            return

        import win32gui
        from windows_capture import WindowsCapture

        token = settings["title"]
        self.window_title = get_game_window_title_by_token(token)
        self.hwnd = win32gui.FindWindow(None, self.window_title) if self.window_title else None
        if not self.hwnd:
            raise RuntimeError(f"No visible game window matching '{token}'")
        self._win = win32gui
        self._working_width = int(settings.get("working_width", 0))
        self._min_interval = 1.0 / max(1, cfg["system"]["fps_limit_window_capturor"])
        self._last_frame_time = 0.0
        capture = WindowsCapture(window_hwnd=self.hwnd, cursor_capture=False)
        capture.frame_handler = self._on_frame
        capture.closed_handler = self._clear_frame
        self._control = capture.start_free_threaded()
        logger.info(f"[Capture] Windows Graphics Capture: '{self.window_title}'")

    def _clear_frame(self):
        with self._lock:
            self.frame = None
            self.frame_time = None

    def _client_pixels(self, pixels):
        """Match this WGC surface to its physical DWM or window bounds before cropping."""
        win, hwnd = self._win, self.hwnd
        left, top = win.ClientToScreen(hwnd, (0, 0))
        _, _, width, height = win.GetClientRect(hwnd)
        frame_h, frame_w = pixels.shape[:2]
        if width <= 0 or height <= 0:
            return None
        if (width, height) == (frame_w, frame_h):
            return pixels
        rect = RECT()
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)  # DWMWA_EXTENDED_FRAME_BOUNDS
        )
        bounds = win.GetWindowRect(hwnd)
        if result == 0 and (rect.right - rect.left, rect.bottom - rect.top) == (frame_w, frame_h):
            bounds = (rect.left, rect.top, rect.right, rect.bottom)
        x, y = left - bounds[0], top - bounds[1]
        if ((bounds[2] - bounds[0], bounds[3] - bounds[1]) != (frame_w, frame_h)
                or x < 0 or y < 0 or x + width > frame_w or y + height > frame_h):
            return None  # Window resized between frame delivery and geometry lookup.
        return pixels[y:y + height, x:x + width]

    def _on_frame(self, frame, control):
        now = time.monotonic()
        if now - self._last_frame_time < self._min_interval:
            return
        self._last_frame_time = now
        user32 = ctypes.windll.user32
        # Coordinates must use physical pixels even if pyautogui set process DPI awareness.
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            if self._win.IsIconic(self.hwnd):
                self._clear_frame()
                return
            client = self._client_pixels(frame.frame_buffer)
            if client is None:
                self._clear_frame()
                return
            height, width = client.shape[:2]
            if self._working_width > 0 and width != self._working_width:
                target = (self._working_width, max(1, round(height * self._working_width / width)))
                interpolation = cv2.INTER_AREA if width > self._working_width else cv2.INTER_LINEAR
                client = cv2.resize(client, target, interpolation=interpolation)
            bgr = cv2.cvtColor(client, cv2.COLOR_BGRA2BGR)
            size = (width, height, bgr.shape[1], bgr.shape[0])
            if size != self._last_size:
                logger.info(f"[Capture] Client {width}x{height} -> detection {size[2]}x{size[3]}")
                self._last_size = size
            with self._lock:
                self.frame = bgr
                self.frame_time = now
        except self._win.error:
            self._clear_frame()
        finally:
            if previous:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))

    def get_frame(self):
        return self.get_timed_frame()[0]

    def get_timed_frame(self):
        if self.hwnd and (not self._win.IsWindow(self.hwnd) or self._win.IsIconic(self.hwnd)):
            self._clear_frame()
        with self._lock:
            return self.frame, self.frame_time

    def stop(self):
        if self._control is not None and not self._control.is_finished():
            self._control.stop()
        self._control = None
        self._clear_frame()
