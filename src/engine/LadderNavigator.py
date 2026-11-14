"""Route to higher platforms through mapped ladder and drop connections."""
from heapq import heappop, heappush
from itertools import count

from src.engine.CharacterFSM import Command

CLIMB_PROTECTION_SECONDS = 1.2


class LadderNavigator:
    def __init__(self, profile):
        self.platforms = profile["platforms"]
        self.connections = [edge for edge in profile["connections"] if edge["kind"] in ("climb", "drop")]
        self.retry_after = {}
        self.reset()

    def reset(self):
        self.edge = None
        self.target_floor = None
        self.grabbing = False
        self.grab_started = None
        self.resumed = False
        self.hanging_sample = None
        self.grab_direction = "none"
        self.drift_since = None
        self.approach_sample = None
        self.braked_at = None
        self.best_distance = float("inf")
        self.landing_since = None
        self.landing_floor = None
        self.landing_y = None
        self.last_target_seen = None
        self.last_position_seen = None

    def avoid_current(self, now):
        if self.edge:
            self.retry_after[(self.edge["from"], self.edge["x"])] = now + 30
        self.reset()

    def missing_position(self, now):
        self.approach_sample = None
        self.hanging_sample = None
        self.grab_direction = "none"
        # Brief title occlusion may interrupt the feet measurement mid-climb.
        if self.edge and now - self.last_position_seen > 0.45 and not self.climb_protected(now):
            self.avoid_current(now)
        if self.edge:
            return self._traversal_command() if self.grabbing else Command(state="WAIT_FOR_FOOTING")
        return None

    def climb_protected(self, now):
        return (self.grabbing and self.edge["kind"] == "climb"
                and now < self.grab_started + CLIMB_PROTECTION_SECONDS)

    def _traversal_command(self):
        if self.edge["kind"] == "drop":
            return Command(action="drop_through", state="DROP_THROUGH")
        if self.resumed:
            return Command(move_y="up", state="CLIMB")
        return Command(move_x=self.grab_direction, action="grab_ladder", state="CLIMB")

    def recover_hanging(self, floor, position, now):
        if floor is not None or self.grabbing:
            self.hanging_sample = None
            return None
        x, y = position
        candidates = [edge for edge in self.connections if edge["kind"] == "climb"
                      and abs(x - edge["x"]) <= 8
                      and self.platforms[edge["to"]][2] + 8 < y < self.platforms[edge["from"]][2] - 12
                      and self.retry_after.get((edge["from"], edge["x"]), 0) <= now]
        if len(candidates) != 1:
            self.hanging_sample = None
            return None
        edge = candidates[0]
        sample = self.hanging_sample
        if (sample is None or sample[0] != edge or abs(x-sample[1]) > 3 or abs(y-sample[2]) > 4
                or now - self.last_position_seen > 0.45):
            self.hanging_sample = (edge, x, y, now)
        self.last_position_seen = now
        if now - self.hanging_sample[3] < 0.3:
            return Command(state="WAIT_FOR_FOOTING")
        self.reset()
        self.edge, self.target_floor = edge, edge["to"]
        self.grabbing = self.resumed = True
        self.grab_started = now
        self.last_progress = self.last_position_seen = self.last_target_seen = now
        self.best_distance = y - self.platforms[edge["to"]][2]
        return self._traversal_command()

    def _start_route(self, floor, x, destinations, now):
        # Different entrances to the same platform have different walking costs.
        order = count()
        queue = [(0, next(order), floor, x, None)]
        visited = set()
        while queue:
            cost, _, here, pos, first = heappop(queue)
            if (here, pos) in visited:
                continue
            visited.add((here, pos))
            if here in destinations and first:
                self.edge, self.target_floor = first, here
                self.last_progress = now
                return
            for edge in self.connections:
                if edge["from"] != here or self.retry_after.get((here, edge["x"]), 0) > now:
                    continue
                if edge["to"] == floor:
                    continue
                height = abs(self.platforms[here][2] - self.platforms[edge["to"]][2])
                heappush(queue, (cost + abs(pos-edge["x"]) + height, next(order),
                                 edge["to"], edge["x"], first or edge))

    def tick(self, floor, position, destinations, now, *, coarse_position=False, frame_time=None):
        if position is None:
            return self.missing_position(now)
        x, y = position
        # Reconsider the destination while still walking. Once airborne,
        # finish this ladder before accepting combat or another route.
        if self.target_floor in destinations:
            self.last_target_seen = now
        if (self.edge and not self.grabbing and self.target_floor not in destinations
                and now - self.last_target_seen > 1.0):
            self.reset()
        if not self.edge and floor and floor not in destinations:
            self._start_route(floor, x, destinations, now)
            if self.edge:
                self.last_target_seen = now
        if not self.edge:
            return None
        self.last_position_seen = now
        edge = self.edge
        dx = edge["x"] - x
        top = self.platforms[edge["to"]][2]
        dropping = edge["kind"] == "drop"
        # The minimap dot is quantized to about 16 map pixels per pixel.
        grab_radius = 8 if coarse_position else 4
        launch_dx = dx
        if not dropping and not self.grabbing and floor == edge["from"] and frame_time is not None:
            # A decision may arrive a camera frame plus a keyboard tick after the observation.
            previous = self.approach_sample
            self.approach_sample = (frame_time, x, coarse_position)
            if (now - frame_time > 0.28 or (self.braked_at is not None and frame_time <= self.braked_at)):
                self.approach_sample = None
                return Command(state="WAIT_FOR_FOOTING")
            self.braked_at = None
            if previous and previous[2] == coarse_position:
                dt = frame_time - previous[0]
                if 0.035 <= dt <= 0.35:
                    speed = (x - previous[1]) / dt
                    crossed = (edge["x"] - previous[1]) * dx < 0
                    if speed * dx > 0 and abs(speed) <= 500:
                        lead = min(32, abs(speed) * (max(0, now - frame_time) + 1/30))
                        launch_dx = dx - (lead if dx > 0 else -lead)
                    if crossed or launch_dx * dx < 0:
                        # We may already have crossed. Release movement and remeasure.
                        self.braked_at = now
                        self.approach_sample = None
                        return Command(state="WAIT_FOR_FOOTING")
        else:
            self.approach_sample = None
        if self.grabbing:
            if (self.grab_direction == "right" and dx <= grab_radius
                    or self.grab_direction == "left" and dx >= -grab_radius
                    or self.grab_direction != "none" and now - self.grab_started >= 0.20):
                self.grab_direction = "none"
            # An early launch can start up to 32 pixels outside the usual entry zone.
            if abs(dx) > 36 and (not dropping or floor == edge["from"]) and not (
                    self.grab_direction != "none" and abs(dx) <= self.launch_distance + 3):
                # A single airborne reading just beyond the rails can precede
                # the grab. Stop steering but keep Up through the next readings.
                self.grab_direction = "none"
                if floor is not None or abs(dx) > 60 or (
                        self.drift_since is not None and now - self.drift_since >= 0.20):
                    if self.climb_protected(now):
                        return self._traversal_command()
                    self.avoid_current(now)
                    return Command(state="LADDER_RETRY")
                if self.drift_since is None:
                    self.drift_since = now
            else:
                self.drift_since = None
            # A continuous rope can carry us past an intermediate platform.
            # Require stable feet so crossing a platform in flight isn't a landing.
            reached = floor == edge["to"] if dropping else floor and self.platforms[floor][2] <= top
            if reached and abs(dx) <= 24:
                if floor != self.landing_floor or abs(y - self.landing_y) > 5:
                    self.landing_floor = floor
                    self.landing_y = y
                    self.landing_since = now
                elif now - self.landing_since >= 0.25 and not self.climb_protected(now):
                    self.reset()
                    return None
            else:
                self.landing_floor = None
            distance = top - y if dropping else y - top
            command = self._traversal_command()
        elif floor is None:
            if now - self.last_progress > 1.6:
                self.avoid_current(now)
            return Command(state="WAIT_FOR_FOOTING")
        elif floor != edge["from"]:
            self.reset()
            return None
        elif abs(launch_dx) <= (12 if dropping else 44):
            if not dropping and abs(launch_dx) > grab_radius:
                self.grab_direction = "right" if dx > 0 else "left"
            self.launch_distance = abs(dx)
            self.grab_started = now
            self.grabbing = True
            self.best_distance = float("inf")
            self.last_progress = now
            distance = abs(y-top)
            command = self._traversal_command()
        else:
            distance = abs(dx)
            state = "WALK_TO_DROP" if dropping else "WALK_TO_LADDER"
            direction = "right" if dx > 0 else "left"
            command = Command(direction, "none", "none", state)
        if distance < self.best_distance - 3:
            self.best_distance, self.last_progress = distance, now
        if now - self.last_progress >= 1.6:
            self.avoid_current(now)
            return Command(state="LADDER_RETRY")
        return command
