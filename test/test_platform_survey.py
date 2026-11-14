import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from src.engine.MonsterDetector import MonsterDetector
from src.engine.LadderNavigator import LadderNavigator
from src.utils.common import load_yaml, override_cfg


class PlatformSurveyTest(unittest.TestCase):
    def setUp(self):
        cfg = override_cfg(load_yaml("config/config_default.yaml"), load_yaml("config/config_custom.yaml"))
        cfg.update(single_platform=True, disable_viz=True)
        self.detector = MonsterDetector(cfg)
        self.assertTrue(self.detector.load_monster_images())
        reference = cv2.imread("docs/assets/ghost-platforms.jpg")
        # Restore the original client height for the center-biased title search.
        self.frame = cv2.copyMakeBorder(reference, 0, 70, 0, 0, cv2.BORDER_CONSTANT)
        self.gray = cv2.cvtColor(self.frame, cv2.COLOR_BGR2GRAY)
        self.survey = self.detector.survey

    def test_surveyed_frame_and_route_preview(self):
        offset = self.survey.register(self.gray)
        self.assertEqual(offset, (0, 0))
        ghosts = self.survey.scan(self.frame, self.gray)
        self.assertCountEqual([self.survey.monster_platform(m, offset) for m in ghosts],
                              ["middle", "upper_right", "ground", "ground"])
        target, edge = self.survey.preview_route("ledge", offset, ghosts)
        self.assertEqual((target, edge["kind"], edge["verified"]), ("ground", "drop", False))
        self.assertIsNone(self.survey.platform_at((1050, 210), offset))

    def test_pan_and_lost_registration(self):
        dx, dy = -182, 30
        pan = cv2.warpAffine(self.gray, np.float32([[1, 0, dx], [0, 1, dy]]), (1280, 720))
        pan[:155, :243] = self.gray[:155, :243]
        offset = self.survey.register(pan)
        self.assertEqual(offset, (dx, dy))
        self.assertEqual(self.survey.platform_at((430+dx, 380+dy), offset), "ledge")
        pan[:155, :243] = np.roll(self.gray[:155, :243], 1, axis=0)
        self.assertEqual(self.survey.register(pan), (dx, dy))
        pan[:155, :243] = 0
        self.assertIsNone(self.survey.register(pan))
        blank = np.zeros_like(self.gray)
        blank[:155, :243] = self.gray[:155, :243]
        self.assertIsNone(self.survey.register(blank))

    def test_far_east_game_frame_routes_toward_upper_ghosts(self):
        d = MonsterDetector(dict(self.detector.cfg, single_platform=False))
        self.assertTrue(d.load_monster_images())
        d._kb = Mock(input_status="Ready")
        d.get_img_frame = Mock(return_value=cv2.imread("docs/assets/ghost-platforms-east.jpg"))
        d.run_once()
        self.assertEqual(d.platform_offset, (-1859, -120))
        self.assertEqual(d.survey.platform_at(d.player_feet, d.platform_offset), "ground")
        self.assertEqual((d.ladder_nav.edge["x"], d.ladder_nav.target_floor), (2631, "upper_east"))
        self.assertEqual(d._kb.set_command.call_args.args, ("right none none",))
        with patch.object(d.survey, "register", return_value=None):
            d.run_once()
            self.assertIsNone(d.ladder_nav.edge)
        d.run_once()
        self.assertEqual(d.ladder_nav.edge["x"], 2631)

    def test_all_surveyed_platforms_have_connected_routes(self):
        profile = self.survey.data
        for edge in profile["connections"]:
            for endpoint in ("from", "to"):
                left, right, _ = profile["platforms"][edge[endpoint]]
                self.assertLessEqual(left, edge["x"], edge)
                self.assertLessEqual(edge["x"], right, edge)
        for origin, (left, right, y) in profile["platforms"].items():
            for target in profile["platforms"].keys() - {origin}:
                nav = LadderNavigator(profile)
                self.assertIsNotNone(nav.tick(origin, ((left+right)//2, y), {target}, 1),
                                     (origin, target))

    def test_off_center_medal_requires_shape_and_color(self):
        detector = self.detector
        medal = detector.img_title[:, :, :3].copy()
        # Blend against a lighter background to reproduce an edge candidate
        # between the ordinary 0.045 and candidate 0.055 score thresholds.
        medal = cv2.add(medal, np.full_like(medal, 28))
        mask = detector.title_mask > 0
        detector.img_frame = np.zeros_like(self.frame)
        x, y = 310, 456
        h, w = mask.shape
        crop = detector.img_frame[y:y+h, x:x+w]
        crop[mask] = medal[mask]
        for colored in (False, True, True):
            if colored:
                crop[mask] = medal[mask]
            else:
                crop[:] = cv2.cvtColor(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
            detector.frame_count += 1
            detector.img_frame_gray = cv2.cvtColor(detector.img_frame, cv2.COLOR_BGR2GRAY)
            detector._detect_player_by_title()
            self.assertEqual(detector._title_last_seen_frame == detector.frame_count, colored)
            if colored:
                self.assertEqual(detector.loc_title, (x, y))

    def test_single_platform_only_dispatches_for_assigned_floor(self):
        detector = self.detector
        detector.get_img_frame = Mock(return_value=self.frame)
        detector._kb = Mock()
        # Both ghosts fit within the 500x300 attack rectangle around (430,350).
        ledge = dict(position=(445, 320), size=(50, 40))
        upper = dict(position=(439, 168), size=(36, 40))
        lower = dict(position=(545, 375), size=(50, 40))
        found = [ledge, lower]
        with patch.object(self.survey, "scan", return_value=found):
            detector.run_once()
            self.assertEqual(detector.locked_platform, "ledge")
            self.assertEqual(detector.attack_targets, [ledge])
            self.assertEqual(detector._kb.set_command.call_args.args, ("face_right none attack",))
            found[:] = [lower, upper]
            detector.run_once()
            self.assertEqual(detector._kb.set_command.call_args.args, ("none none none",))
            # Walked or fell onto another platform that has a reachable ghost.
            def moved():
                detector.loc_player = (560, 405)
                detector._title_last_seen_frame = detector.frame_count
            with patch.object(detector, "_detect_player_by_title", side_effect=moved):
                detector.run_once()
                self.assertEqual(detector._kb.set_command.call_args.args, ("none none none",))
            found[:] = [ledge]
            with patch.object(detector, "_detect_player_by_title"):
                detector.run_once()
                self.assertEqual(detector.position_source, "minimap")
                self.assertEqual(detector._kb.set_command.call_args.args, ("face_right none attack",))
            with patch.object(detector, "_detect_player_by_title"), patch.object(detector, "_detect_minimap", return_value=None):
                detector.run_once()
                self.assertIsNone(detector.player_feet)
                self.assertEqual(detector._kb.set_command.call_args.args, ("none none none",))
            with patch.object(self.survey, "register", return_value=None):
                detector.run_once()
                self.assertEqual(detector._kb.set_command.call_args.args, ("none none none",))
            detector.run_once()
            self.assertEqual(detector._kb.set_command.call_args.args, ("face_right none attack",))

    def test_visible_chase_walk_then_attack_then_stop(self):
        cfg = self.detector.cfg.copy()
        cfg["single_platform"] = False
        detector = MonsterDetector(cfg)
        self.assertTrue(detector.load_monster_images())
        detector._kb = Mock()
        detector.get_img_frame = Mock(return_value=self.frame)
        foot_x = 450
        def on_ground():
            detector.loc_player = (foot_x, 523)
            detector._title_last_seen_frame = detector.frame_count
        with patch.object(detector, "_detect_player_by_title", side_effect=on_ground):
            # Real reference image has two ground ghosts beyond the old ROI.
            detector.run_once()
            self.assertEqual(len(detector.visible_ghosts), 4)
            self.assertEqual(detector._kb.set_command.call_args.args, ("right none none",))
            foot_x = 735
            detector.run_once()
            self.assertEqual(detector._kb.set_command.call_args.args, ("face_right none attack",))
            with patch.object(detector.survey, "scan", return_value=[]):
                detector.run_once()
                self.assertEqual(detector._kb.set_command.call_args.args, ("none none none",))

    def test_approach_only_when_a_firing_position_exists_on_current_platform(self):
        ghost = dict(position=(80, 475), size=(50, 40))
        approach = self.survey.approach_direction
        self.assertEqual(approach((800,553), (0,0), [ghost], (800,523), 250,150), "left")
        self.assertEqual(approach((430,380), (0,0), [ghost], (430,350), 250,150), "none")
        self.assertEqual(approach((800,553), None, [ghost], (800,523), 250,150), "none")

    def test_climb_from_ground_to_upper_platform_in_two_legs(self):
        nav = LadderNavigator(self.survey.data)
        goal = {"middle_east"}
        self.assertEqual(nav.tick("ground", (1480,553), goal, 0).move_x, "right")
        self.assertEqual(nav.edge["x"], 1537)
        self.assertEqual(nav.tick("ground", (1536,553), goal, 1).state, "CLIMB")
        self.assertEqual(nav.tick("ground", (1536,553), goal, 1.15).action, "grab_ladder")
        self.assertEqual(nav.tick(None, (1537,480), set(), 1.6).state, "CLIMB")
        nav.tick("lower_east", (1537,435), goal, 2)
        self.assertIsNone(nav.tick("lower_east", (1537,435), goal, 2.3))
        self.assertEqual(nav.tick("lower_east", (1537,435), goal, 2.5).move_x, "right")
        self.assertEqual(nav.edge["to"], "middle_east")

    def test_only_upper_ghosts_drive_real_hunt_entry_to_ladder(self):
        cfg = dict(self.detector.cfg, single_platform=False)
        detector = MonsterDetector(cfg)
        detector.load_monster_images()
        detector._kb = Mock(input_status="Ready")
        detector.get_img_frame = Mock(return_value=self.frame)
        x = 700
        def on_ground():
            detector.loc_player = (x, 523)
            detector._title_last_seen_frame = detector.frame_count
        ghost = dict(position=(750, 250), size=(50, 50))
        with patch.object(detector, "_detect_player_by_title", side_effect=on_ground), patch.object(
                detector.survey, "scan", return_value=[ghost]), patch(
                    "src.engine.MonsterDetector.time.monotonic", side_effect=[1, 1.2, 1.4, 1.6]):
            detector.run_once()
            self.assertEqual(detector.fsm.state, "WALK_TO_LADDER")
            x = detector.ladder_nav.edge["x"]
            detector.run_once()
            self.assertEqual(detector._kb.set_command.call_args.args, ("none none grab_ladder",))
            detector.cfg["bot"]["mode"] = "detect"
            detector.run_once()
            self.assertEqual(detector._kb.set_command.call_args.args, ("stop stop stop",))

    def test_failed_grab_releases_and_tries_another_route(self):
        nav = LadderNavigator(self.survey.data)
        nav.tick("ground", (1537,553), {"middle_east"}, 0)
        nav.tick("ground", (1537,553), {"middle_east"}, 0.2)
        self.assertEqual(nav.tick("ground", (1537,553), {"middle_east"}, 2).state, "LADDER_RETRY")
        self.assertEqual(nav.tick("ground", (1537,553), {"middle_east"}, 2.2).move_x, "right")
        self.assertEqual(nav.edge["x"], 2078)

    def test_ledge_to_upper_right_via_left_walkway_and_middle(self):
        d = MonsterDetector(dict(self.detector.cfg, single_platform=False))
        d.load_monster_images()
        d.get_img_frame = Mock(return_value=self.frame)
        d._kb = Mock(input_status="Ready")
        # The stored game frame has a bare ledge and targets to the right.
        d.run_once()
        self.assertEqual(d.ladder_nav.target_floor, "middle")
        self.assertEqual(d.ladder_nav.edge["to"], "upper_left")
        d.ladder_nav.reset()
        right = next(m for m in d.visible_ghosts if d.survey.monster_platform(m, (0, 0)) == "upper_right")
        x, y = 430, 380
        def locate():
            d.loc_player = (x, y - 30)
            d._title_last_seen_frame = d.frame_count
        with patch.object(d, "_detect_player_by_title", side_effect=locate), patch.object(
                d.survey, "scan", return_value=[right]), patch("src.engine.MonsterDetector.time.monotonic") as clock:
            clock.return_value = 1
            for kind, entrance, landing_y in [("climb", 430, 210), ("drop", 818, 322),
                                               ("climb", 1272, 210)]:
                d.run_once()
                self.assertEqual(d.ladder_nav.target_floor, "upper_right")
                self.assertEqual((d.ladder_nav.edge["kind"], d.ladder_nav.edge["x"]), (kind, entrance))
                x = entrance
                clock.return_value += 0.2
                d.run_once()
                self.assertEqual(d._kb.set_command.call_args.args,
                                 ("none none " + ("drop_through" if kind == "drop" else "grab_ladder"),))
                y = landing_y
                clock.return_value += 0.4
                d.run_once()
                clock.return_value += 0.7
                d.run_once()
                self.assertIsNone(d.ladder_nav.edge)
                if entrance != 1272:
                    self.assertEqual(d._kb.set_command.call_args.args, ("none none none",))
            self.assertEqual(d.fsm.state, "COMBAT_IN_PLACE")

    def test_clear_current_floor_before_climbing_and_defend_on_approach(self):
        d = MonsterDetector(dict(self.detector.cfg, single_platform=False))
        d.load_monster_images()
        d.get_img_frame = Mock(return_value=self.frame)
        d._kb = Mock(input_status="Ready")
        x = 912  # Still approaching the ladder at x=962.
        def locate():
            d.loc_player = (x, 523)
            d._title_last_seen_frame = d.frame_count
        found = [dict(position=(800, 385), size=(40, 40)), dict(position=(260, 510), size=(40, 40))]
        with patch.object(d, "_detect_player_by_title", side_effect=locate), patch.object(
                d.survey, "scan", return_value=found), patch(
                    "src.engine.MonsterDetector.time.monotonic", side_effect=[i / 5 for i in range(20)]):
            d.run_once()
            self.assertEqual(d._kb.set_command.call_args.args, ("left none none",))
            found.pop()
            d.run_once()
            self.assertEqual(d.fsm.state, "IDLE")
            d.run_once()
            self.assertEqual(d.fsm.state, "WALK_TO_LADDER")
            found.append(dict(position=(950, 510), size=(40, 40)))
            d.run_once()
            self.assertEqual(d.fsm.state, "COMBAT_IN_PLACE")
            self.assertIsNone(d.ladder_nav.edge)
            found.pop()
            d.run_once()
            self.assertEqual(d.fsm.state, "IDLE")
            x = 962
            d.run_once()
            self.assertEqual(d.fsm.state, "CLIMB")
            d.run_once()
            self.assertEqual(d.fsm.state, "CLIMB")
            found.append(dict(position=(950, 510), size=(40, 40)))
            d.run_once()
            self.assertEqual(d.fsm.state, "CLIMB")

    def test_continuous_rope_overshoots_ledge_without_penalizing_entrance(self):
        nav = LadderNavigator(self.survey.data)
        nav.tick("ground", (430,553), {"upper_left"}, 0)
        nav.tick("ground", (430,553), {"upper_left"}, 0.2)
        self.assertEqual(nav.edge["to"], "ledge")
        nav.tick("ledge", (430,380), {"upper_left"}, 0.8)
        nav.tick(None, (430,300), {"upper_left"}, 1.1)
        nav.tick("upper_left", (430,216), {"upper_left"}, 1.8)
        self.assertIsNone(nav.tick("upper_left", (430,215), {"upper_left"}, 2.1))
        self.assertFalse(nav.retry_after)

    def test_near_scan_returns_client_coords_and_expands_on_miss(self):
        detector = self.detector
        detector.get_img_frame = Mock(return_value=self.frame)
        feet_x = 450
        def on_ground():
            detector.loc_player = (feet_x, 523)
            detector._title_last_seen_frame = detector.frame_count
        with patch.object(detector, "_detect_player_by_title", side_effect=on_ground), patch.object(
                self.survey, "scan", wraps=self.survey.scan) as scan:
            detector.run_once()
            self.assertEqual((detector.scan_scope, scan.call_count), ("full", 2))
            full = detector.visible_ghosts
            feet_x = 735
            scan.reset_mock()
            detector.run_once()
            self.assertEqual((detector.scan_scope, scan.call_count), ("near", 1))
            self.assertEqual([m["position"] for m in detector.attack_targets],
                             [m["position"] for m in full if self.survey.monster_platform(m,(0,0)) == "ground"
                              and m["position"][0] < 970])

    def test_attack_nearby_unassigned_ghost_then_climb_to_upper_target(self):
        cfg = dict(self.detector.cfg, single_platform=False)
        d = MonsterDetector(cfg)
        d.load_monster_images()
        d._kb = Mock(input_status="Ready")
        d.get_img_frame = Mock(return_value=self.frame)
        def on_ground():
            d.loc_player = (700, 523)
            d._title_last_seen_frame = d.frame_count
        ghosts = [dict(position=(710,370),size=(50,40)), dict(position=(610,590),size=(50,40)),
                  dict(position=(800,625),size=(40,40))]
        with patch.object(d, "_detect_player_by_title", side_effect=on_ground), patch.object(
                d.survey, "scan", return_value=ghosts):
            d.run_once()
            self.assertTrue(d.attack_targets)
            self.assertEqual(d.fsm.state, "COMBAT_IN_PLACE")
            ghosts.pop(1)
            d.run_once()
            self.assertEqual(d.fsm.state, "IDLE")
            d.run_once()
            self.assertEqual(d.ladder_nav.target_floor, "lower")
            self.assertEqual(d.fsm.state, "WALK_TO_LADDER")

    def test_ladder_from_track_and_short_detection_gaps(self):
        nav = LadderNavigator(self.survey.data)
        self.assertEqual(nav.tick("track",(520,675),{"upper_left"},0).move_x,"right")
        self.assertEqual(nav.tick("track",(560,675),set(),0.3).move_x,"right")
        self.assertEqual(nav.tick("track",(620,675),{"upper_left"},0.45).state,"CLIMB")
        self.assertEqual(nav.tick("track",(620,675),{"upper_left"},0.6).state,"CLIMB")
        self.assertEqual(nav.tick(None,None,set(),0.8).action,"grab_ladder")
        self.assertEqual(nav.tick(None,None,set(),1.2).state, "CLIMB")
        self.assertIsNone(nav.tick(None,None,set(),1.5))
        self.assertEqual(nav.tick("track",(620,675),{"upper_left"},1.6).move_x, "right")
        self.assertEqual(nav.edge["x"], 1398)

    def test_minimap_position_requires_one_live_dot_and_correct_map(self):
        roi = np.zeros((74,230,3), dtype=np.uint8)
        for x in (66, 80):
            cv2.circle(roi, (x,46), 2, (30,255,255), -1)
            point = self.survey.minimap_feet(roi, (6,67), (-392,-120), self.frame.shape)
            if x == 66:
                self.assertEqual(point, (607,421))
            else:
                self.assertIsNone(point)
        self.assertIsNone(self.survey.minimap_feet(roi, (6,67), None, self.frame.shape))

    def test_drift_past_west_ladder_does_not_turn_back_immediately(self):
        nav = LadderNavigator(self.survey.data)
        self.assertEqual(nav.tick("ground",(961,553),{"lower"},0).state, "CLIMB")
        self.assertEqual(nav.tick("ground",(961,553),{"lower"},0.2).state, "CLIMB")
        self.assertEqual(nav.tick("ground",(1030,553),{"lower"},0.3).state, "CLIMB")
        self.assertEqual(nav.tick("ground",(1030,553),{"lower"},1).state, "LADDER_RETRY")
        self.assertIsNotNone(nav.tick("ground",(1030,553),{"lower"},2))
        self.assertNotEqual(nav.edge["x"], 962)

    def test_diagonal_grab_from_either_side_releases_at_center(self):
        for start, direction in ((386, "right"), (474, "left")):
            nav = LadderNavigator(self.survey.data)
            outside = start - 1 if direction == "right" else start + 1
            self.assertEqual(nav.tick("ground", (outside,553), {"upper_left"}, 0).state, "WALK_TO_LADDER")
            launch = nav.tick("ground", (start,553), {"upper_left"}, 0)
            self.assertEqual((launch.move_x, launch.action), (direction, "grab_ladder"))
            centered = nav.tick(None, (430,520), {"upper_left"}, .12)
            self.assertEqual((centered.move_x, centered.action), ("none", "grab_ladder"))
            drift = nav.tick(None, (375,510), {"upper_left"}, .3)
            self.assertEqual((drift.move_x, drift.action), ("none", "grab_ladder"))
            self.assertEqual(nav.tick(None, (375,510), {"upper_left"}, .51).state, "CLIMB")
            self.assertEqual(nav.tick(None, (375,510), {"upper_left"}, 1).state, "LADDER_RETRY")

    def test_airborne_offset_on_logged_962_ladder_does_not_cancel_up(self):
        nav = LadderNavigator(self.survey.data)
        nav.tick("ground", (1006,553), {"lower"}, 1)
        for x, now in [(923, 1.21), (960, 1.35), (924, 1.49), (962, 1.61)]:
            cmd = nav.tick(None, (x,501), {"lower"}, now)
            self.assertEqual((cmd.move_x, cmd.action), ("none", "grab_ladder"))
        nav.tick("lower", (962,435), {"lower"}, 2)
        self.assertIsNone(nav.tick("lower", (962,435), {"lower"}, 2.3))

    def test_resume_on_ladder_with_no_active_route_or_monsters(self):
        d = MonsterDetector(dict(self.detector.cfg, single_platform=False))
        d.load_monster_images()
        d._kb = Mock(input_status="Ready")
        d.get_img_frame = Mock(return_value=self.frame)
        y = 318
        def locate():
            d.loc_player = (634, y - 30)
            d._title_last_seen_frame = d.frame_count
        with patch.object(d, "_detect_player_by_title", side_effect=locate), patch.object(
                d.survey, "register", return_value=(-1710, 0)), patch.object(
                d.survey, "scan", return_value=[]), patch("src.engine.MonsterDetector.time.monotonic",
                                                         side_effect=[0, .16, .34, .50, .8, 1.4]):
            for expected in ["none none none", "none none none", "none up none", "none up none"]:
                d.run_once()
                self.assertEqual(d._kb.set_command.call_args.args, (expected,))
            y = 210
            d.run_once()
            d.run_once()
            self.assertIsNone(d.ladder_nav.edge)
            self.assertEqual(d._kb.set_command.call_args.args, ("none none none",))

    def test_walk_until_side_jump_range_and_limit_horizontal_hold(self):
        nav = LadderNavigator(self.survey.data)
        for x in (875,915,917):
            self.assertEqual(nav.tick("ground", (x,553), {"lower"}, 0).state, "WALK_TO_LADDER")
        launch = nav.tick("ground", (918,553), {"lower"}, .2)
        self.assertEqual((launch.move_x, launch.action), ("right", "grab_ladder"))
        self.assertEqual(nav.tick(None, (945,522), {"lower"}, .45).move_x, "none")

    def test_passing_ladder_in_air_does_not_resume(self):
        nav = LadderNavigator(self.survey.data)
        for i, y in enumerate((350, 333, 318, 305, 318)):
            self.assertEqual(nav.recover_hanging(None, (2344, y), i * .15).action, "none")
            self.assertFalse(nav.grabbing)
        self.assertIsNone(nav.recover_hanging(None, (2390, 318), .85))

    def test_frame_latency_advances_takeoff_only_when_approaching(self):
        for direction, sign in (("right", 1), ("left", -1)):
            nav = LadderNavigator(self.survey.data)
            def tick(distance, capture, now):
                return nav.tick("ground", (962-sign*distance, 553), {"lower"}, now, frame_time=capture)
            self.assertEqual(tick(72, 1, 1.10).move_x, direction)
            jump = tick(48, 1.10, 1.21)
            self.assertEqual((jump.move_x, jump.action), (direction, "grab_ladder"))
            self.assertEqual(tick(46, 1.12, 1.23).state, "CLIMB")

    def test_estimated_crossing_stops_until_post_brake_observation(self):
        nav = LadderNavigator(self.survey.data)
        def tick(x, capture, now):
            return nav.tick("ground", (x,553), {"lower"}, now, frame_time=capture)
        tick(917, 1, 1.1)
        self.assertEqual(tick(944, 1.1, 1.22).state, "WAIT_FOR_FOOTING")
        self.assertEqual(tick(952, 1.19, 1.24).state, "WAIT_FOR_FOOTING")
        corrected = tick(974, 1.28, 1.34)
        self.assertEqual((corrected.move_x, corrected.action), ("left", "grab_ladder"))

    def test_occluded_medal_uses_current_frame_and_rejects_ambiguity(self):
        d = self.detector
        medal = d.img_title[:, :, :3]
        h, w = medal.shape[:2]
        half = w//2
        blank = np.zeros((480,800,3), np.uint8)
        def locate(frame):
            d.frame_count += 1
            d.img_frame = frame
            d.img_frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            d._detect_player_by_title()
            return d._title_last_seen_frame == d.frame_count
        image = blank.copy()
        image[320:320+h, 400+half:400+w] = medal[:,half:]
        self.assertTrue(locate(image))
        self.assertEqual(d.loc_title,(400,320))
        self.assertFalse(locate(blank))
        image[280:280+h, 330+half:330+w] = medal[:,half:]
        self.assertFalse(locate(image))


if __name__ == "__main__":
    unittest.main()
