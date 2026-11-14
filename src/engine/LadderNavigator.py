"""Select upward ladder routes and check progress in map coordinates."""
from heapq import heappop, heappush
from itertools import count

from src.engine.CharacterFSM import Command


class LadderNavigator:
    def __init__(self, profile):
        self.platforms = profile["platforms"]
        self.ladders = [edge for edge in profile["connections"] if edge["kind"] == "climb"]
        self.retry_after = {}
        self.reset()

    def reset(self):
        self.edge = None
        self.target_floor = None
        self.grabbing = False
        self.best_distance = float("inf")
        self.landing_since = None

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
            for edge in self.ladders:
                if edge["from"] != here or self.retry_after.get((here, edge["x"]), 0) > now:
                    continue
                height = self.platforms[here][2] - self.platforms[edge["to"]][2]
                heappush(queue, (cost + abs(pos-edge["x"]) + height, next(order),
                                 edge["to"], edge["x"], first or edge))

    def tick(self, floor, position, destinations, now):
        if position is None:
            self.reset()
            return None
        x, y = position
        # Reconsider the destination while still walking. Once airborne,
        # finish this ladder before accepting combat or another route.
        if not self.grabbing and (floor in destinations or self.target_floor not in destinations):
            self.reset()
        if not self.edge and floor and floor not in destinations:
            self._start_route(floor, x, destinations, now)
        if not self.edge:
            return None
        edge = self.edge
        dx = edge["x"] - x
        top = self.platforms[edge["to"]][2]
        if self.grabbing:
            if floor == edge["to"] and abs(top-y) <= 5 and abs(dx) <= 24:
                if self.landing_since is None:
                    self.landing_since = now
                elif now - self.landing_since >= 0.25:
                    self.reset()
                    return None
            else:
                self.landing_since = None
            distance = abs(y-top)
            command = Command("none", "none", "grab_ladder", "CLIMB")
        elif floor != edge["from"]:
            self.reset()
            return None
        elif abs(dx) <= 12:
            self.grabbing = True
            self.best_distance = float("inf")
            self.last_progress = now
            distance = abs(y-top)
            command = Command("none", "none", "grab_ladder", "CLIMB")
        else:
            distance = abs(dx)
            command = Command("right" if dx > 0 else "left", "none", "none", "WALK_TO_LADDER")
        if distance < self.best_distance - 3:
            self.best_distance, self.last_progress = distance, now
        if now - self.last_progress >= 1.6:
            self.retry_after[(edge["from"], edge["x"])] = now + 6
            self.reset()
            return Command(state="LADDER_RETRY")
        return command
