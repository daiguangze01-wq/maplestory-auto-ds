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
        self.assertEqual(nav.edge["x"], 1534)
        self.assertEqual(nav.tick("ground", (1533,553), goal, 1).action, "grab_ladder")
        self.assertEqual(nav.tick(None, (1534,480), set(), 1.6).state, "CLIMB")
        nav.tick("lower_east", (1534,435), goal, 2)
        self.assertIsNone(nav.tick("lower_east", (1534,435), goal, 2.3))
        self.assertEqual(nav.tick("lower_east", (1534,435), goal, 2.5).move_x, "right")
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
                detector.survey, "scan", return_value=[ghost]):
            detector.run_once()
            self.assertEqual(detector.fsm.state, "WALK_TO_LADDER")
            x = detector.ladder_nav.edge["x"]
            detector.run_once()
            self.assertEqual(detector._kb.set_command.call_args.args, ("none none grab_ladder",))
            detector.cfg["bot"]["mode"] = "detect"
            detector.run_once()
            self.assertEqual(detector._kb.set_command.call_args.args, ("none none none",))

    def test_failed_grab_releases_and_tries_another_route(self):
        nav = LadderNavigator(self.survey.data)
        nav.tick("ground", (1534,553), {"middle_east"}, 0)
        self.assertEqual(nav.tick("ground", (1534,553), {"middle_east"}, 2).state, "LADDER_RETRY")
        self.assertEqual(nav.tick("ground", (1534,553), {"middle_east"}, 2.2).move_x, "right")
        self.assertEqual(nav.edge["x"], 2078)


if __name__ == "__main__":
    unittest.main()
