"""
Health Monitor — takes the bottom UI strip of the game window and calculates
the player's HP/MP (and optional EXP) bar percentages. Drinks potions when
the percentage drops below the configured threshold.

Adapted for maplestory-auto-ds:
- Bar detection parameters come from config: bar_count, bar_white_thresh,
  bar_min_aspect, bar_max_aspect, bar_min_area, bar_max_area,
  bar_min_width/max_width, bar_min_height/max_height
- Supports enable_hp / enable_mp toggles (F2/F3 hotkeys) via toggle_hp/toggle_mp
- Supports hp_rearm_percent / mp_rearm_percent (hysteresis) and
  hp_retry_timeout / mp_retry_timeout (repeat-dose while still low)
"""
import threading
import time

import cv2
import numpy as np

# Local Import
from src.utils.logger import logger
from src.utils.common import get_bar_percent


class HealthMonitor:
    """
    Independent health monitoring thread that can heal while other
    actions are running.
    """

    def __init__(self, cfg, kb_controller):
        self.cfg = cfg
        self.kb = kb_controller
        self.is_terminated = False
        self.enabled = True
        self.thread = None

        hm_cfg = cfg.get("health_monitor", {})

        # HP / MP enable switches (toggled by F2 / F3)
        self.enable_hp = hm_cfg.get("enable_hp", True)
        self.enable_mp = hm_cfg.get("enable_mp", True)

        # Health monitoring state
        self.hp_percent = 100.0
        self.mp_percent = 100.0
        self.exp_percent = 100.0

        # Hysteresis: once triggered, stay in "healing" state until the
        # percentage recovers above rearm value
        self.hp_healing = False
        self.mp_healing = False

        # Require repeated, similar readings before acting. A single bad color
        # match must never be able to trigger a potion key.
        self.confirm_frames = max(1, int(hm_cfg.get("confirm_frames", 2)))
        self.confirm_max_delta = max(
            0.0, float(hm_cfg.get("confirm_max_delta", 8.0))
        )
        self._pending_values = {
            "HP": {"value": None, "count": 0},
            "MP": {"value": None, "count": 0},
            "EXP": {"value": None, "count": 0},
        }

        # Timers
        self.t_last_heal = 0.0
        self.t_last_mp = 0.0
        self.t_last_hp_reduce = 0.0
        self.t_last_run = 0.0
        self.t_hp_watch_dog = time.time()

        # Frame data (updated by main thread)
        self.img_frame = None
        self.frame_lock = threading.Lock()

        # FPS settings
        self.fps_limit = hm_cfg.get("fps_limit", 20)
        self.fps = 0

        # Bar detection parameters
        self.bar_count = hm_cfg.get("bar_count", 3)
        self.bar_white_thresh = hm_cfg.get("bar_white_thresh", 240)
        self.bar_min_aspect = hm_cfg.get("bar_min_aspect", 2.5)
        self.bar_max_aspect = hm_cfg.get("bar_max_aspect", 12.0)
        self.bar_min_area = hm_cfg.get("bar_min_area", 800)
        self.bar_max_area = hm_cfg.get("bar_max_area", 8000)
        self.bar_min_width = hm_cfg.get("bar_min_width", 0)
        self.bar_max_width = hm_cfg.get("bar_max_width", 10 ** 9)
        self.bar_min_height = hm_cfg.get("bar_min_height", 0)
        self.bar_max_height = hm_cfg.get("bar_max_height", 10 ** 9)

        # Debug information: bar locs and sizes [(x, y, w, h), ...]
        self.loc_size_bars = []

        logger.info("[Health Monitor] Init done")

    def start(self):
        """Start health monitoring thread."""
        if not self.is_terminated:
            self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.thread.start()
            logger.info("[Health Monitor] Started")

    def stop(self):
        """Stop health monitoring thread."""
        self.is_terminated = True
        if self.thread:
            self.thread.join()
            logger.info("[Health Monitor] Terminated")

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def toggle_hp(self):
        """Toggle HP auto-potion (F2)."""
        self.enable_hp = not self.enable_hp
        logger.info(f"[Health Monitor] enable_hp: {self.enable_hp}")

    def toggle_mp(self):
        """Toggle MP auto-potion (F3)."""
        self.enable_mp = not self.enable_mp
        logger.info(f"[Health Monitor] enable_mp: {self.enable_mp}")

    def update_frame(self, img_frame):
        """Update frame data (bottom UI strip) from main thread."""
        with self.frame_lock:
            self.img_frame = img_frame

    def get_hp_mp_exp_percent(self):
        """
        Extract HP / MP / EXP percentages from the bottom UI strip.

        Detects white bar borders via thresholding + contours, filters by
        aspect ratio / area / size, sorts left-to-right, then measures the
        fill ratio of each bar.

        Returns:
            (hp, mp, exp) floats in [0, 100], or None for undetected bars.
        """
        bar_mode = self.cfg.get("health_monitor", {}).get("bar_mode", "white")
        if bar_mode == "color":
            return self._get_hp_mp_exp_percent_color()

        with self.frame_lock:
            if self.img_frame is None:
                return None, None, None
            img_frame = self.img_frame.copy()

        gray = cv2.cvtColor(img_frame, cv2.COLOR_BGR2GRAY)
        white_mask = cv2.inRange(gray, self.bar_white_thresh, 255)

        contours, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)

        bars = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if h <= 0:
                continue
            aspect = w / h
            area = w * h
            if not (self.bar_min_aspect <= aspect <= self.bar_max_aspect):
                continue
            if not (self.bar_min_area <= area <= self.bar_max_area):
                continue
            if not (self.bar_min_width <= w <= self.bar_max_width):
                continue
            if not (self.bar_min_height <= h <= self.bar_max_height):
                continue
            bars.append((x, y, w, h))

        # Sort left-to-right
        bars = sorted(bars, key=lambda b: b[0])

        expected = self.bar_count
        if len(bars) < expected:
            if len(bars) != expected:
                return None, None, None

        self.loc_size_bars = bars[:3]

        percents = [get_bar_percent(img_frame[y:y + h, x:x + w])
                    for x, y, w, h in self.loc_size_bars]

        if expected >= 3:
            hp, mp, exp = percents[0], percents[1], percents[2]
        else:
            hp, mp, exp = percents[0], percents[1], None
        return hp, mp, exp

    def _confirm_value(self, name, value):
        """Return a reading only after N consecutive similar observations."""
        state = self._pending_values[name]
        if value is None or not np.isfinite(value):
            state["value"] = None
            state["count"] = 0
            return None

        value = float(np.clip(value, 0.0, 100.0))
        previous = state["value"]
        if previous is None or abs(value - previous) > self.confirm_max_delta:
            state["value"] = value
            state["count"] = 1
        else:
            state["value"] = (previous + value) / 2.0
            state["count"] += 1

        if state["count"] < self.confirm_frames:
            return None
        return state["value"]

    def _get_hp_mp_exp_percent_color(self):
        """
        Color-based bar detection for private servers (e.g. v079) whose HP/MP/EXP
        bars are solid color fills (red/blue/green/yellow) without white borders.
        """
        with self.frame_lock:
            if self.img_frame is None:
                return None, None, None
            img_frame = self.img_frame.copy()

        H, W = img_frame.shape[:2]
        hm_cfg = self.cfg.get("health_monitor", {})

        # Preferred mode: calibrated rectangles around the full inner tracks.
        # Unlike contour bounding boxes, these rectangles keep the denominator
        # constant when a bar becomes shorter.
        fixed_regions = hm_cfg.get("fixed_color_bar_regions", {})
        if fixed_regions:
            results = {}
            bars = []
            color_ranges = hm_cfg.get("color_ranges", {})
            for name in ("HP", "MP", "EXP"):
                region = fixed_regions.get(name)
                color_range = color_ranges.get(name)
                if not region or not color_range or len(color_range) != 2:
                    results[name] = None
                    continue
                x0 = int(W * float(region.get("x_start", 0.0)))
                x1 = int(W * float(region.get("x_end", 1.0)))
                y0 = int(H * float(region.get("y_start", 0.0)))
                y1 = int(H * float(region.get("y_end", 1.0)))
                x0, y0 = max(0, x0), max(0, y0)
                x1, y1 = min(W, x1), min(H, y1)
                if x1 <= x0 or y1 <= y0:
                    results[name] = None
                    continue
                roi = img_frame[y0:y1, x0:x1]
                lower = np.array(color_range[0], dtype=np.uint8)
                upper = np.array(color_range[1], dtype=np.uint8)
                ratio = self._measure_fixed_color_bar(
                    roi, lower, upper,
                    min_col_coverage=float(
                        hm_cfg.get("fixed_min_col_coverage", 0.12)
                    ),
                    min_density=float(hm_cfg.get("fixed_min_density", 0.08)),
                )
                results[name] = self._confirm_value(name, ratio)
                bars.append((x0, y0, x1 - x0, y1 - y0))
            self.loc_size_bars = bars
            return results.get("HP"), results.get("MP"), results.get("EXP")

        sb_cfg = hm_cfg.get("state_bar", {})
        x0 = int(W * sb_cfg.get("x_start", 0.12))
        x1 = int(W * sb_cfg.get("x_end", 0.88))
        y0 = int(H * sb_cfg.get("y_start", 0.88))
        y1 = int(H * sb_cfg.get("y_end", 1.0))
        roi = img_frame[y0:y1, x0:x1]
        if roi.size == 0:
            return None, None, None

        color_ranges = hm_cfg.get("color_ranges", {
            "HP": [[0, 0, 120], [100, 50, 255]],
            "MP": [[150, 80, 0], [230, 130, 40]],
            "EXP": [[0, 140, 60], [80, 230, 255]],
        })

        results = {}
        bars = []

        # 用子区域把 HP/MP/EXP 分开，避免颜色掩码串到相邻条或背景
        roi_h, roi_w = roi.shape[:2]
        bar_regions = hm_cfg.get("color_bar_regions", {
            "HP": {"x_start": 0.0, "x_end": 0.6, "y_start": 0.0, "y_end": 0.6},
            "MP": {"x_start": 0.4, "x_end": 1.0, "y_start": 0.0, "y_end": 0.6},
            "EXP": {"x_start": 0.0, "x_end": 1.0, "y_start": 0.5, "y_end": 1.0},
        })
        morph_kernel = tuple(hm_cfg.get("color_morph_kernel", [21, 7]))
        min_w = hm_cfg.get("color_min_width", 80)
        min_aspect = hm_cfg.get("color_min_aspect", 2.0)

        for name in ("HP", "MP", "EXP"):
            rng = color_ranges.get(name)
            if not rng or len(rng) != 2:
                results[name] = None
                continue
            lower = np.array(rng[0], dtype=np.uint8)
            upper = np.array(rng[1], dtype=np.uint8)

            reg = bar_regions.get(name, {})
            rx0 = int(reg.get("x_start", 0.0) * roi_w)
            rx1 = int(reg.get("x_end", 1.0) * roi_w)
            ry0 = int(reg.get("y_start", 0.0) * roi_h)
            ry1 = int(reg.get("y_end", 1.0) * roi_h)
            sub_roi = roi[ry0:ry1, rx0:rx1]
            if sub_roi.size == 0:
                results[name] = None
                continue

            ratio, rect = self._detect_color_bar(
                sub_roi, lower, upper,
                min_w=min_w, min_aspect=min_aspect,
                morph_kernel=morph_kernel
            )
            # 子区域失败时回退到全 ROI
            if ratio is None:
                ratio, rect = self._detect_color_bar(
                    roi, lower, upper,
                    min_w=min_w, min_aspect=min_aspect,
                    morph_kernel=morph_kernel
                )
                if rect is not None:
                    rx, ry, rw, rh = rect
                    bars.append((x0 + rx, y0 + ry, rw, rh))
            else:
                if rect is not None:
                    rx, ry, rw, rh = rect
                    bars.append((x0 + rx0 + rx, y0 + ry0 + ry, rw, rh))
            results[name] = ratio

        self.loc_size_bars = bars
        return (
            self._confirm_value("HP", results.get("HP")),
            self._confirm_value("MP", results.get("MP")),
            self._confirm_value("EXP", results.get("EXP")),
        )

    @staticmethod
    def _measure_fixed_color_bar(
        roi, lower, upper, min_col_coverage=0.12, min_density=0.08
    ):
        """Measure fill against a fixed full-track rectangle.

        The rightmost confidently colored column represents the end of the
        left-to-right fill. Density validation rejects unrelated isolated
        pixels while allowing gradients and segmented bar artwork.
        """
        if roi is None or roi.size == 0:
            return None
        mask = cv2.inRange(roi, lower, upper)
        height, width = mask.shape[:2]
        if height <= 0 or width <= 0:
            return None

        kernel_width = max(1, min(7, width // 20))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 1))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        col_coverage = np.mean(mask > 0, axis=0)
        filled = np.where(col_coverage >= min_col_coverage)[0]
        if filled.size == 0:
            return None

        fill_end = int(filled[-1]) + 1
        if fill_end <= 0:
            return None
        density = float(np.mean(mask[:, :fill_end] > 0))
        if density < min_density:
            return None
        return fill_end / width * 100.0

    @staticmethod
    def _detect_color_bar_v2(roi, lower, upper, bar_name="HP"):
        """
        Detect color bar fill ratio.

        Strategy: find the bar FRAME (white border at the bar's y position),
        then measure the fill ratio WITHIN the frame. This handles sparse fills
        (e.g. HP at 35% has gaps) better than finding a solid fill contour.

        For HP/MP: y~75-85 in ROI (state bar 88%-100% of frame)
        For EXP:   y~110-125 in ROI
        """
        # Step 1: find the bar y band by looking for white/light border pixels
        # The bar has a white/light border at the top and bottom
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
        H, W = gray.shape[:2]

        # Find white border rows: rows with many bright pixels
        bright_per_row = np.sum(gray > 200, axis=1)
        # Find rows with significant bright content (likely bar borders)
        border_rows = np.where(bright_per_row > W * 0.3)[0]

        if bar_name == "EXP":
            # EXP bar is in the lower portion of the ROI
            target_band = (110, min(130, H))
        else:
            # HP/MP bars are in the upper portion
            target_band = (70, 95)

        # Step 2: get color mask
        mask = cv2.inRange(roi, lower, upper)

        # Step 3: find bar frame using white border
        # Scan the target band for the bar's x range
        # The bar frame is bordered by white pixels at top and bottom
        band = gray[target_band[0]:target_band[1], :]
        if band.size == 0:
            return None, None

        # Find x positions where the bar exists (has white border above/below)
        # The bar border is typically 1-2 px thick
        # Use the top border (1-2 rows above the bar fill)
        border_y = max(0, target_band[0] - 2)
        border_band = gray[border_y:target_band[0], :] if border_y < target_band[0] else gray[target_band[0]:target_band[0]+2, :]

        # Project white pixels onto x-axis in the border band
        border_white = border_band > 200
        border_x_proj = np.sum(border_white, axis=0)

        # Find contiguous x segments with significant border
        in_bar = border_x_proj > 0
        # Find segments
        segments = []
        start = None
        for x in range(W):
            if in_bar[x] and start is None:
                start = x
            elif not in_bar[x] and start is not None:
                if x - start >= 50:  # minimum bar width
                    segments.append((start, x))
                start = None
        if start is not None and W - start >= 50:
            segments.append((start, W))

        if not segments:
            # Fallback: find the widest mask contour
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
            closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
            contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            best = None
            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                if w < 30 or h <= 0 or w/h < 3.0:
                    continue
                if best is None or w > best[2]:
                    best = (x, y, w, h)
            if best:
                x, y, w, h = best
                patch = mask[y:y+h, x:x+w]
                filled_cols = np.count_nonzero(patch, axis=0) > 0
                ratio = np.sum(filled_cols) / w * 100
                return ratio, best
            return None, None

        # Step 4: for each bar segment, measure fill ratio
        # We need to identify which segment is HP and which is MP
        # HP is on the LEFT, MP is on the RIGHT (for HP/MP bars)
        # EXP is a single wide segment
        results = []
        for seg_x0, seg_x1 in segments:
            seg_w = seg_x1 - seg_x0
            # Measure fill within this segment at the bar y band
            fill_band = mask[target_band[0]:target_band[1], seg_x0:seg_x1]
            if fill_band.size == 0:
                continue
            # Column fill: a column is "filled" if at least half the band has fill color
            col_fill = np.mean(fill_band > 0, axis=0) > 0.3
            fill_ratio = np.sum(col_fill) / seg_w * 100
            results.append((seg_x0, seg_x1, fill_ratio))

        if bar_name == "EXP":
            # EXP: use the widest segment
            best = max(results, key=lambda r: r[1] - r[0])
            x0, x1, ratio = best
            return ratio, (x0, target_band[0], x1 - x0, target_band[1] - target_band[0])
        else:
            # HP: leftmost segment, MP: rightmost segment
            if bar_name == "HP" and results:
                x0, x1, ratio = results[0]  # leftmost
                return ratio, (x0, target_band[0], x1 - x0, target_band[1] - target_band[0])
            elif bar_name == "MP" and results:
                x0, x1, ratio = results[-1]  # rightmost
                return ratio, (x0, target_band[0], x1 - x0, target_band[1] - target_band[0])

        return None, None

    @staticmethod
    def _detect_color_bar(roi, lower, upper, min_w, min_aspect, morph_kernel=(7, 3)):
        """Detect the widest horizontal color bar in ROI and return fill %."""
        mask = cv2.inRange(roi, lower, upper)
        # 闭运算：把水平填充条内部断点连起来，但核不宜过大（避免把相邻条粘连）
        kx, ky = morph_kernel
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(1, int(kx)), max(1, int(ky))))
        closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        best = None
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w < min_w or h <= 0:
                continue
            if w / h < min_aspect:
                continue
            if best is None or w > best[2]:
                best = (x, y, w, h)
        if best is None:
            return None, None
        x, y, w, h = best
        patch = mask[y:y + h, x:x + w]
        filled_cols = np.count_nonzero(patch, axis=0) > 0
        fill_width = int(np.sum(filled_cols))
        ratio = (fill_width / w) * 100.0 if w > 0 else 0.0
        return ratio, (x, y, w, h)

    def _monitor_loop(self):
        """Main monitoring loop (daemon thread)."""
        hm_cfg = self.cfg.get("health_monitor", {})

        while not self.is_terminated:
            try:
                if not self.enabled:
                    self.limit_fps()
                    continue

                t_cur = time.time()

                hp_percent, mp_percent, exp_percent = self.get_hp_mp_exp_percent()
                if hp_percent is not None:
                    if self.hp_percent > hp_percent:
                        self.t_last_hp_reduce = t_cur
                    self.hp_percent = hp_percent
                if mp_percent is not None:
                    self.mp_percent = mp_percent
                if exp_percent is not None:
                    self.exp_percent = exp_percent

                hp_thres = hm_cfg.get("add_hp_percent", 50)
                mp_thres = hm_cfg.get("add_mp_percent", 10)
                hp_rearm = hm_cfg.get("hp_rearm_percent", hp_thres + 5)
                mp_rearm = hm_cfg.get("mp_rearm_percent", mp_thres + 5)
                hp_retry = hm_cfg.get("hp_retry_timeout", 2.0)
                mp_retry = hm_cfg.get("mp_retry_timeout", 1.5)
                hp_cd = hm_cfg.get("add_hp_cooldown", 0.5)
                mp_cd = hm_cfg.get("add_mp_cooldown", 0.5)
                watchdog_timeout = hm_cfg.get("return_home_watch_dog_timeout", 3)

                # ---------------- HP ----------------
                if self.enable_hp:
                    if self.hp_healing:
                        # In healing state: keep dosing until rearm value
                        if self.hp_percent >= hp_rearm:
                            self.hp_healing = False
                        elif t_cur - self.t_last_heal > hp_retry:
                            self._heal()
                            self.t_last_heal = t_cur
                    else:
                        if self.hp_percent <= hp_thres and t_cur - self.t_last_heal > hp_cd:
                            sent = self._heal()
                            self.t_last_heal = t_cur
                            if sent:
                                self.hp_healing = True
                                logger.info(f"[Health Monitor] Auto heal triggered, "
                                            f"HP: {self.hp_percent:.1f}%")

                    # Optional: force heal via KeyBoardController
                    if hm_cfg.get("force_heal", False):
                        if self.hp_percent < hp_thres:
                            self.kb.is_need_force_heal = True
                        else:
                            self.kb.is_need_force_heal = False

                    # Optional: return home when potions exhausted
                    if hm_cfg.get("return_home_if_no_potion", False):
                        if self.hp_percent >= hp_thres:
                            self.t_hp_watch_dog = t_cur
                        elif t_cur - self.t_hp_watch_dog > watchdog_timeout:
                            logger.warning(f"[Health Monitor] HP({self.hp_percent:.1f}%) < "
                                           f"{hp_thres:.1f}% for "
                                           f"{round(t_cur - self.t_hp_watch_dog, 2)}s. "
                                           f"Return home.")
                            if self.kb.press_key_safe(
                                    self.cfg["key"]["return_home"],
                                    action_name="return home"):
                                self.is_terminated = True
                                self.kb.is_terminated = True

                # ---------------- MP ----------------
                if self.enable_mp:
                    if self.mp_healing:
                        if self.mp_percent >= mp_rearm:
                            self.mp_healing = False
                        elif t_cur - self.t_last_mp > mp_retry:
                            self._add_mp()
                            self.t_last_mp = t_cur
                    else:
                        if self.mp_percent <= mp_thres and t_cur - self.t_last_mp > mp_cd:
                            sent = self._add_mp()
                            self.t_last_mp = t_cur
                            if sent:
                                self.mp_healing = True
                                logger.info(f"[Health Monitor] Auto MP triggered, "
                                            f"MP: {self.mp_percent:.1f}%")

                self.limit_fps()

            except Exception as e:
                logger.error(f"[Health Monitor] {e}")
                self.limit_fps()

    def _heal(self):
        """Execute heal action."""
        try:
            return self.kb.press_key_safe(
                self.cfg["key"]["add_hp"], 0.05, action_name="HP potion"
            )
        except Exception as e:
            logger.error(f"[Health Monitor] Heal action failed: {e}")
            return False

    def _add_mp(self):
        """Execute MP recovery action."""
        try:
            return self.kb.press_key_safe(
                self.cfg["key"]["add_mp"], 0.05, action_name="MP potion"
            )
        except Exception as e:
            logger.error(f"[Health Monitor] MP action failed: {e}")
            return False

    def limit_fps(self):
        """Cap loop rate to fps_limit."""
        target_duration = 1.0 / self.fps_limit
        frame_duration = time.time() - self.t_last_run
        if frame_duration < target_duration:
            time.sleep(target_duration - frame_duration)
        self.fps = round(1.0 / max(time.time() - self.t_last_run, 0.001))
        self.t_last_run = time.time()
