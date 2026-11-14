"""Read-only observations of the surveyed subway platforms in one client frame."""
from pathlib import Path
from collections import deque

import cv2
import numpy as np

from src.utils.common import load_yaml, nms


def thumbnail(gray):
    return cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)


class PlatformSurvey:
    def __init__(self, profile, ghosts, threshold):
        profile = Path(profile)
        self.data = load_yaml(str(profile))
        reference = cv2.imread(str(profile.parent / self.data["reference"]), cv2.IMREAD_GRAYSCALE)
        if reference is None:
            raise ValueError(f"Missing platform reference for {profile}")
        x, y, right, bottom = self.data["map_label"]
        self.map_label = reference[y:bottom, x:right].copy()
        small = thumbnail(reference)
        self.anchors = []
        for x, y, right, bottom in self.data["anchors"]:
            x, y = x // 2, y // 2
            self.anchors.append((x, y, small[y:bottom // 2, x:right // 2].copy()))
        extra = self.data.get("extra_landmarks")
        if extra:
            atlas = cv2.imread(str(profile.parent / extra["image"]), cv2.IMREAD_GRAYSCALE)
            height = extra["patch_height"]
            if atlas is None or atlas.shape[0] != len(extra["origins"]) * height:
                raise ValueError(f"Invalid landmark image for {profile}")
            for i, (x, y) in enumerate(extra["origins"]):
                self.anchors.append((x / 2, y / 2, atlas[i*height:(i+1)*height]))
        self.templates = []
        for template, mask in ghosts:
            # Use opaque interior pixels for the coarse search. Transparent
            # border colors depend on the scenery behind the sprite.
            gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            coarse = thumbnail(gray)
            interior = None
            if mask is not None:
                interior = np.uint8(thumbnail(mask) > 245) * 255
            self.templates.append((coarse, interior, template, mask))
        self.threshold = threshold
        self.last_offset = None

    def register(self, gray):
        x, y, right, bottom = self.data["map_label"]
        if gray.shape[1] != self.data["width"] or gray.shape[0] < bottom:
            return None
        # Rounding the game client to working width can shift HUD text by a
        # pixel even when every platform has exactly the expected translation.
        label = gray[max(0, y-3):bottom+3, max(0, x-3):right+3]
        if cv2.matchTemplate(label, self.map_label, cv2.TM_CCOEFF_NORMED).max() < 0.82:
            return None
        scene = thumbnail(gray[:self.data["playfield_bottom"]])
        # On consecutive frames the same landmarks are usually only a few
        # pixels away. Verify the translation against this frame before reuse.
        if self.last_offset is not None:
            local = []
            for x, y, patch in self.anchors:
                cx = round(x + self.last_offset[0] / 2)
                cy = round(y + self.last_offset[1] / 2)
                h, w = patch.shape
                left, top = max(0, cx-12), max(0, cy-12)
                if cx < 0 or cy < 0 or cx+w > scene.shape[1] or cy+h > scene.shape[0]:
                    continue
                roi = scene[top:cy+h+12, left:cx+w+12]
                _, score, _, (px, py) = cv2.minMaxLoc(cv2.matchTemplate(roi, patch, cv2.TM_CCOEFF_NORMED))
                if score >= 0.91:
                    local.append((2*(left+px-x), 2*(top+py-y)))
            if len(local) >= 3 and np.max(np.ptp(local, axis=0)) <= 4:
                self.last_offset = tuple(int(round(v)) for v in np.median(local, axis=0))
                return self.last_offset
        votes = []
        for x, y, patch in self.anchors:
            if patch.shape[0] > scene.shape[0]:
                continue
            result = cv2.matchTemplate(scene, patch, cv2.TM_CCOEFF_NORMED)
            _, score, _, (px, py) = cv2.minMaxLoc(result)
            if score >= 0.83:
                votes.append((2 * (px - x), 2 * (py - y)))
        # Repeated girders can match at the wrong x. Require independent
        # landmarks to agree, and reject ties between distinct translations.
        clusters = [[v for v in votes if max(abs(a-b) for a, b in zip(v, seed)) <= 4]
                    for seed in votes]
        if not clusters:
            return None
        best = max(clusters, key=len)
        if len(best) < 2 or any(len(c) == len(best) and not set(c).intersection(best)
                                for c in clusters):
            return None
        self.last_offset = tuple(int(round(v)) for v in np.median(best, axis=0))
        return self.last_offset

    def platform_at(self, point, offset, *, ghost=False):
        if offset is None or point is None:
            return None
        x, y = point[0] - offset[0], point[1] - offset[1]
        candidates = [name for name, (left, right, top) in self.data["platforms"].items()
                      if left <= x <= right and -8 <= top - y <= (34 if ghost else 12)]
        return candidates[0] if len(candidates) == 1 else None

    def feet(self, player):
        if player is None:
            return None
        dx, dy = self.data["feet_offset"]
        return player[0] + dx, player[1] + dy

    def minimap_feet(self, minimap, origin, offset, frame_shape):
        mapping = self.data.get("minimap_tracking")
        if minimap is None or not mapping or offset is None:
            return None
        h, w = minimap.shape[:2]
        if (*origin, w, h) != tuple(mapping["roi"]):
            return None
        yellow = cv2.inRange(cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV), (23,140,195), (35,255,255))
        _, _, stats, centers = cv2.connectedComponentsWithStats(yellow)
        dots = [center for (_, _, width, height, area), center in zip(stats[1:], centers[1:])
                if 3 <= width <= 6 and 3 <= height <= 6 and 7 <= area <= 25]
        if len(dots) != 1:
            return None
        x, y = (round(value*scale + base + shift) for value, scale, base, shift in
                zip(dots[0], mapping["scale"], mapping["origin"], offset))
        if 0 <= x < frame_shape[1] and 0 <= y < min(frame_shape[0], self.data["playfield_bottom"]):
            return x, y
        return None

    def monster_platform(self, monster, offset):
        x, y = monster["position"]
        h, w = monster["size"]
        return self.platform_at((x + w // 2, y + h), offset, ghost=True)

    def preview_route(self, floor, offset, ghosts):
        """Show the first candidate connector to a visible mob's platform."""
        if not floor:
            return None, None
        destinations = {self.monster_platform(m, offset) for m in ghosts}
        seen = {floor}
        queue = deque([(floor, None)])
        while queue:
            here, first = queue.popleft()
            if here in destinations:
                return here, first
            for edge in self.data["connections"]:
                if edge["from"] == here and edge["to"] not in seen:
                    seen.add(edge["to"])
                    queue.append((edge["to"], first or edge))
        return None, None

    def approach_direction(self, feet, offset, ghosts, center, reach_x, reach_y):
        step = self.approach_step(feet, offset, ghosts, center, reach_x, reach_y)
        if not step:
            return "none"
        return "right" if step > 0 else "left"

    def approach_step(self, feet, offset, ghosts, center, reach_x, reach_y):
        """Find the nearest firing position reachable by walking on this platform."""
        floor = self.platform_at(feet, offset)
        if floor is None:
            return None
        left, right, _ = self.data["platforms"][floor]
        # Keep the firing position inside the ledge even for another level.
        left += offset[0] + 24
        right += offset[0] - 24
        px, py = center
        steps = []
        for m in ghosts:
            x, y = m["position"]
            h, w = m["size"]
            mx, my = x + w // 2, y + h // 2
            if abs(my - py) > reach_y or abs(mx - px) <= reach_x:
                continue
            lo, hi = max(left, mx - reach_x), min(right, mx + reach_x)
            if lo <= hi:
                steps.append(min(max(px, lo), hi) - px)
        return min(steps, key=abs, default=None)

    def scan(self, frame, gray, bounds=None):
        """Find ghosts in the playfield, coarse grayscale then full-size masked BGR."""
        if frame.shape[1] != self.data["width"]:
            return []
        limit = min(frame.shape[0], self.data["playfield_bottom"])
        left0, top0, right, bottom = bounds or (0, 0, frame.shape[1], limit)
        left0, top0 = max(0, left0)//2*2, max(0, top0)//2*2
        frame = frame[top0:min(limit, bottom), left0:right]
        if min(frame.shape[:2]) < 2:
            return []
        coarse_frame = thumbnail(gray[top0:top0+frame.shape[0], left0:left0+frame.shape[1]])
        hits = []
        for coarse, interior, template, mask in self.templates:
            h, w = template.shape[:2]
            if h > frame.shape[0] or w > frame.shape[1]:
                continue
            result = cv2.matchTemplate(coarse_frame, coarse, cv2.TM_SQDIFF_NORMED, mask=interior)
            result[~np.isfinite(result)] = 1
            minima = (result <= 0.18) & (result == cv2.erode(result, np.ones((9, 9), np.uint8)))
            for cy, cx in zip(*np.nonzero(minima)):
                x, y = int(cx) * 2, int(cy) * 2
                left, top = max(0, x - 3), max(0, y - 3)
                roi = frame[top:y+h+3, left:x+w+3]
                if roi.shape[0] < h or roi.shape[1] < w:
                    continue
                fine = cv2.matchTemplate(roi, template, cv2.TM_SQDIFF_NORMED, mask=mask)
                score, _, (dx, dy), _ = cv2.minMaxLoc(fine)
                if not 0 <= score <= self.threshold:
                    continue
                x, y = left0 + left + dx, top0 + top + dy
                if any(x < r and x+w > l and y < b and y+h > t
                       for l, t, r, b in self.data["hud"]):
                    continue
                hits.append(dict(name="ghost", position=(x, y), size=(h, w), score=score))
        return nms(hits, iou_threshold=0.4)

    def draw(self, image, offset, feet, ghosts, in_range, status, show_route=False):
        platforms = self.data["platforms"]
        floor = self.platform_at(feet, offset)
        target, step = self.preview_route(floor, offset, ghosts) if show_route else (None, None)
        def label(text, xy, color):
            for ink, width in [((0, 0, 0), 4), (color, 2)]:
                cv2.putText(image, text, xy, cv2.FONT_HERSHEY_SIMPLEX, 0.65, ink, width, cv2.LINE_AA)

        if offset is not None:
            dx, dy = offset
            for x, y in self.data.get("portals", ()):
                if 0 <= x + dx < image.shape[1]:
                    cv2.drawMarker(image, (x+dx, y+dy), (40, 40, 255), cv2.MARKER_DIAMOND, 16, 2)
                    label("portal", (x+dx+10, y+dy-8), (40, 40, 255))
            for name, (left, right, y) in platforms.items():
                cv2.line(image, (left+dx, y+dy), (right+dx, y+dy), (230, 180, 0), 2)
                if right+dx > 0 and left+dx < image.shape[1]:
                    label(name, (max(5, left+dx), y+dy-5), (230, 180, 0))
            for edge in self.data["connections"]:
                x = edge["x"] + dx
                y = platforms[edge["from"]][2] + dy
                top = platforms[edge["to"]][2] + dy
                color = (0, 175, 255)
                cv2.arrowedLine(image, (x, y), (x, top), color, 3 if edge == step else 1, tipLength=0.10)
                label(edge["kind"] + ("" if edge["verified"] else "?"), (x+3, (y+top)//2), color)
        for ghost in ghosts:
            x, y = ghost["position"]
            h, w = ghost["size"]
            cv2.rectangle(image, (x, y), (x+w, y+h), (200, 255, 100), 2)
            label(self.monster_platform(ghost, offset) or "?", (x, y-4), (200, 255, 100))
        if feet:
            cv2.drawMarker(image, feet, (255, 0, 255), cv2.MARKER_CROSS, 18, 3)
        label(f"{status} | floor {floor or '?'} | visible {len(ghosts)} | in range {in_range}",
              (250, 25), (255, 255, 255))
        if target:
            route = f"via {step['kind']} at x={step['x'] + offset[0]}" if step else "same platform"
            label(f"Preview: {target} ({route})", (250, 52), (0, 175, 255))
