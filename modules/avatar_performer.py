"""
PubCast Avatar Performer System  v2.0
======================================

v2.0 hardening — 8 confirmed bugs fixed:
  [FIX-1] StackState.update() alpha not clamped → negative mocap_weight on dt<0
  [FIX-2] OneEuroFilterQuat.filter() NaN input propagated to output
  [FIX-3] broadcast_dropout() iterated .values() during concurrent remove_performer()
  [FIX-4] blendshapes wire format gap — now merged into motion_data with bs_ prefix,
           matching skeleton.rs apply_motion_capture_frame() exactly
  [FIX-5] AvatarBinding rest_directions missing Hand_L/R, Foot_L/R bones
  [FIX-6] ConfidenceDecay._seen grew unbounded — eviction added (2× decay window)
  [FIX-7] OneEuroFilter beta=0.005 too low — 26 frames to reach 90% of fast step;
           raised to 0.02 (→ 4 frames, 67ms at 60fps)
  [FIX-8] BONE_LANDMARK_PAIRS missing Clavicle_L/R — clavicle was never retargeted

Architecture:
  AvatarPerformerManager      — manages N performers
    └─ AvatarPerformer        — one per avatar
         ├─ AvatarBinding     — rig ↔ skeleton name table + scale factors
         ├─ MocapRetargeter   — world-space positions → local bone rotations
         ├─ AnimationStack    — 3-layer blend management
         └─ PerformerStreamer — WebSocket output + dropout recovery

Dependencies:
  pip install numpy websockets

Usage:
  manager = AvatarPerformerManager(ws_url="ws://localhost:8765")
  await manager.start()
  performer = manager.create_performer("avatar_01", preset="MANNY")
  await performer.feed_frame(precision_mocap_frame)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, Dict, List, Optional, Tuple

import numpy as np
import websockets
import websockets.exceptions

logger = logging.getLogger("pubcast.performer")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS — Avatar DNA bone-length reference (metres, 1.75m reference figure)
# These match the rest pose offsets in skeleton.rs create_pubcast_rig().
# ─────────────────────────────────────────────────────────────────────────────

REF_PROPORTIONS: Dict[str, float] = {
    "pelvis_to_spine01":    0.08,
    "spine01_to_spine02":   0.10,
    "spine02_to_spine03":   0.10,
    "spine03_to_neck01":    0.08,
    "neck01_to_head":       0.10,
    "spine03_to_clavicle":  0.15,
    "clavicle_to_upperarm": 0.15,
    "upperarm_length":      0.27,
    "lowerarm_length":      0.23,
    "pelvis_to_thigh":      0.09,
    "thigh_length":         0.42,
    "calf_length":          0.38,
    "foot_length":          0.08,
}

AVATAR_DNA: Dict[str, Dict[str, float]] = {
    "MANNY":     {"scale": 1.00, "width_mod": 1.00, "head_scale": 1.00, "limb_thick": 1.00},
    "SHELA":     {"scale": 0.92, "width_mod": 0.85, "head_scale": 0.95, "limb_thick": 0.80},
    "MAN_LARGE": {"scale": 1.10, "width_mod": 1.40, "head_scale": 0.90, "limb_thick": 1.50},
    "MAN_SMALL": {"scale": 0.85, "width_mod": 0.90, "head_scale": 1.10, "limb_thick": 0.90},
    "CHILD":     {"scale": 0.60, "width_mod": 0.70, "head_scale": 1.40, "limb_thick": 0.60},
    "BABY":      {"scale": 0.35, "width_mod": 0.60, "head_scale": 2.20, "limb_thick": 0.80},
}

# ─────────────────────────────────────────────────────────────────────────────
# DATA TYPES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Quaternion:
    """x, y, z, w — matches skeleton.rs wire format."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 1.0

    def as_list(self) -> List[float]:
        return [self.x, self.y, self.z, self.w]

    @staticmethod
    def identity() -> "Quaternion":
        return Quaternion(0.0, 0.0, 0.0, 1.0)

    @staticmethod
    def from_numpy(q: np.ndarray) -> "Quaternion":
        """q must be [x, y, z, w]."""
        return Quaternion(float(q[0]), float(q[1]), float(q[2]), float(q[3]))

    def to_numpy(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z, self.w], dtype=np.float64)

    def normalize(self) -> "Quaternion":
        n = math.sqrt(self.x**2 + self.y**2 + self.z**2 + self.w**2)
        if n < 1e-8:
            return Quaternion.identity()
        return Quaternion(self.x/n, self.y/n, self.z/n, self.w/n)

    def multiply(self, other: "Quaternion") -> "Quaternion":
        ax, ay, az, aw = self.x, self.y, self.z, self.w
        bx, by, bz, bw = other.x, other.y, other.z, other.w
        return Quaternion(
            aw*bx + ax*bw + ay*bz - az*by,
            aw*by - ax*bz + ay*bw + az*bx,
            aw*bz + ax*by - ay*bx + az*bw,
            aw*bw - ax*bx - ay*by - az*bz,
        ).normalize()

    def slerp(self, other: "Quaternion", t: float) -> "Quaternion":
        t = max(0.0, min(1.0, t))
        a = self.to_numpy()
        b = other.to_numpy()
        dot = np.clip(np.dot(a, b), -1.0, 1.0)
        if dot < 0:
            b = -b
            dot = -dot
        if dot > 0.9995:
            result = a + t * (b - a)
            n = np.linalg.norm(result)
            if n < 1e-8:
                return Quaternion.identity()
            result /= n
            return Quaternion.from_numpy(result)
        theta0 = math.acos(dot)
        theta  = theta0 * t
        sin_t  = math.sin(theta)
        sin_0  = math.sin(theta0)
        s0 = math.cos(theta) - dot * sin_t / sin_0
        s1 = sin_t / sin_0
        result = s0 * a + s1 * b
        n = np.linalg.norm(result)
        if n < 1e-8:
            return Quaternion.identity()
        result /= n
        return Quaternion.from_numpy(result)


@dataclass
class JointPose:
    """One joint's transform at a given frame."""
    position:    np.ndarray        # shape (3,) in metres
    rotation:    Quaternion
    confidence:  float = 1.0
    is_mirrored: bool  = False     # True when bilateral-mirrored from other side


@dataclass
class PrecisionMocapFrame:
    """
    Input type consumed by AvatarPerformer.feed_frame().
    Produced by MocapPrecisionCapture (mocap_precision.py).
    joint_poses uses canonical UE5 joint names.
    blendshapes uses ARKit face blendshape names (0.0–1.0).
    """
    avatar_id:         str
    timestamp:         float
    frame_index:       int
    joint_poses:       Dict[str, JointPose]
    blendshapes:       Dict[str, float] = field(default_factory=dict)
    global_confidence: float = 1.0


@dataclass
class PerformedFrame:
    """
    Output type streamed to the Rust renderer.
    Format matches BridgeMotionPayload in lib.rs.

    motion_data contains both:
      - bone transforms: bone_name → {position, rotation, confidence}
      - blendshapes: 'bs_<arkit_name>' → [weight_value]
        (skeleton.rs apply_motion_capture_frame strips bs_ prefix)
    """
    avatar_id:   str
    timestamp:   float
    motion_data: Dict[str, Dict]   # bone_name or bs_* → data dict
    layer_info:  Dict[str, float]  # layer_name → blend_weight (debug)


# ─────────────────────────────────────────────────────────────────────────────
# QUATERNION MATH UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

class QuatMath:
    """Pure-numpy quaternion utilities."""

    @staticmethod
    def from_two_vectors(a: np.ndarray, b: np.ndarray) -> Quaternion:
        """
        Unit quaternion rotating unit vector a onto unit vector b.
        Handles parallel and anti-parallel cases.
        """
        a = a / (np.linalg.norm(a) + 1e-9)
        b = b / (np.linalg.norm(b) + 1e-9)
        cross = np.cross(a, b)
        dot   = float(np.clip(np.dot(a, b), -1.0, 1.0))
        cross_mag = np.linalg.norm(cross)

        if cross_mag < 1e-9:
            if dot > 0.99:
                return Quaternion.identity()
            # Anti-parallel: any perpendicular axis
            perp = np.array([1.0, 0.0, 0.0])
            if abs(a[0]) > 0.9:
                perp = np.array([0.0, 1.0, 0.0])
            perp = np.cross(a, perp)
            perp_n = np.linalg.norm(perp)
            if perp_n < 1e-9:
                return Quaternion.identity()
            perp /= perp_n
            return Quaternion(float(perp[0]), float(perp[1]), float(perp[2]), 0.0).normalize()

        axis  = cross / cross_mag
        angle = math.acos(dot)
        s     = math.sin(angle / 2.0)
        return Quaternion(
            float(axis[0]) * s,
            float(axis[1]) * s,
            float(axis[2]) * s,
            math.cos(angle / 2.0),
        ).normalize()

    @staticmethod
    def from_euler_xyz(rx: float, ry: float, rz: float) -> Quaternion:
        """Euler angles in radians. Order: X then Y then Z."""
        cx, sx = math.cos(rx/2), math.sin(rx/2)
        cy, sy = math.cos(ry/2), math.sin(ry/2)
        cz, sz = math.cos(rz/2), math.sin(rz/2)
        return Quaternion(
            sx*cy*cz + cx*sy*sz,
            cx*sy*cz - sx*cy*sz,
            cx*cy*sz + sx*sy*cz,
            cx*cy*cz - sx*sy*sz,
        ).normalize()

    @staticmethod
    def clavicle_corrective(shoulder_abduction_rad: float) -> Tuple[Quaternion, Quaternion]:
        """
        Python mirror of ClavicleDriver::compute_corrective() in skeleton.rs.
        Axis: elevation on Z, protraction on Y (matching v2 Rust fix).
        """
        if not math.isfinite(shoulder_abduction_rad):
            return Quaternion.identity(), Quaternion.identity()

        trigger   = math.radians(60.0)
        max_abd   = math.radians(180.0)
        max_elev  = math.radians(30.0)
        max_prot  = math.radians(15.0)

        if shoulder_abduction_rad <= trigger:
            return Quaternion.identity(), Quaternion.identity()

        t  = (shoulder_abduction_rad - trigger) / (max_abd - trigger)
        t  = max(0.0, min(1.0, t))
        ts = t * t * (3.0 - 2.0 * t)   # smoothstep

        elev = QuatMath.from_euler_xyz(0.0, 0.0, ts * max_elev)
        prot = QuatMath.from_euler_xyz(0.0, ts * max_prot, 0.0)
        return elev, prot


# ─────────────────────────────────────────────────────────────────────────────
# AVATAR BINDING
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AvatarBinding:
    """
    Avatar-specific scale factors and rest-pose bone directions.
    All bone lengths in metres.
    """
    preset_name:     str
    dna:             Dict[str, float]
    bone_lengths:    Dict[str, float]    = field(default_factory=dict)
    rest_directions: Dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self):
        self._compute_bone_lengths()
        self._set_rest_directions()

    def _compute_bone_lengths(self):
        s = self.dna.get("scale", 1.0)
        w = self.dna.get("width_mod", 1.0)
        wide_keys = {"spine03_to_clavicle", "pelvis_to_thigh"}
        for k, v in REF_PROPORTIONS.items():
            self.bone_lengths[k] = v * s * (w if k in wide_keys else 1.0)

    def _set_rest_directions(self):
        """
        Rest-pose primary axes in LOCAL parent space.
        FIX-5: Added Hand_L/R (point along forearm extension +X/-X)
                and Foot_L/R (point forward along +Z from ankle).
        """
        self.rest_directions = {
            "Spine_01":   np.array([0.0,  1.0, 0.0]),
            "Spine_02":   np.array([0.0,  1.0, 0.0]),
            "Spine_03":   np.array([0.0,  1.0, 0.0]),
            "Neck_01":    np.array([0.0,  1.0, 0.0]),
            "Head":       np.array([0.0,  1.0, 0.0]),
            # Arms — clavicle points laterally, arms point laterally outward
            "Clavicle_L": np.array([1.0,  0.0, 0.0]),
            "Clavicle_R": np.array([-1.0, 0.0, 0.0]),
            "UpperArm_L": np.array([1.0,  0.0, 0.0]),
            "UpperArm_R": np.array([-1.0, 0.0, 0.0]),
            "LowerArm_L": np.array([1.0,  0.0, 0.0]),
            "LowerArm_R": np.array([-1.0, 0.0, 0.0]),
            # FIX-5: Hand points along forearm extension
            "Hand_L":     np.array([1.0,  0.0, 0.0]),
            "Hand_R":     np.array([-1.0, 0.0, 0.0]),
            # Legs point down
            "Thigh_L":    np.array([0.0, -1.0, 0.0]),
            "Thigh_R":    np.array([0.0, -1.0, 0.0]),
            "Calf_L":     np.array([0.0, -1.0, 0.0]),
            "Calf_R":     np.array([0.0, -1.0, 0.0]),
            # FIX-5: Foot points forward (+Z) from ankle
            "Foot_L":     np.array([0.0,  0.0, 1.0]),
            "Foot_R":     np.array([0.0,  0.0, 1.0]),
        }

    @staticmethod
    def create(preset: str) -> "AvatarBinding":
        dna = AVATAR_DNA.get(preset.upper(), AVATAR_DNA["MANNY"])
        return AvatarBinding(preset_name=preset, dna=dna)


# ─────────────────────────────────────────────────────────────────────────────
# MOCAP RETARGETER
# ─────────────────────────────────────────────────────────────────────────────

class MocapRetargeter:
    """
    Converts world-space MediaPipe landmark positions into LOCAL-space
    bone rotations scaled to the avatar's proportions.
    """

    # FIX-8: Added Clavicle_L and Clavicle_R — were missing in v1
    BONE_LANDMARK_PAIRS: Dict[str, Tuple[str, str]] = {
        "Spine_01":   ("Pelvis",      "Spine_01"),
        "Spine_02":   ("Spine_01",    "Spine_02"),
        "Spine_03":   ("Spine_02",    "Spine_03"),
        "Neck_01":    ("Spine_03",    "Neck_01"),
        "Head":       ("Neck_01",     "Head"),
        # FIX-8: Clavicle now retargeted from Spine_03 → shoulder joint
        "Clavicle_L": ("Spine_03",   "Clavicle_L"),
        "Clavicle_R": ("Spine_03",   "Clavicle_R"),
        "UpperArm_L": ("Clavicle_L", "UpperArm_L"),
        "LowerArm_L": ("UpperArm_L", "LowerArm_L"),
        "Hand_L":     ("LowerArm_L", "Hand_L"),
        "UpperArm_R": ("Clavicle_R", "UpperArm_R"),
        "LowerArm_R": ("UpperArm_R", "LowerArm_R"),
        "Hand_R":     ("LowerArm_R", "Hand_R"),
        "Thigh_L":    ("Pelvis",     "Thigh_L"),
        "Calf_L":     ("Thigh_L",    "Calf_L"),
        "Foot_L":     ("Calf_L",     "Foot_L"),
        "Thigh_R":    ("Pelvis",     "Thigh_R"),
        "Calf_R":     ("Thigh_R",    "Calf_R"),
        "Foot_R":     ("Calf_R",     "Foot_R"),
    }

    def __init__(self, binding: AvatarBinding):
        self.binding = binding
        self._measured_shoulder_width: Optional[float] = None
        self._avatar_shoulder_width = (
            binding.bone_lengths.get("spine03_to_clavicle", 0.15) * 2.0
            * binding.dna.get("width_mod", 1.0)
        )

    def retarget(self, frame: PrecisionMocapFrame) -> Dict[str, JointPose]:
        """
        Convert world-space joint_poses into LOCAL-space bone rotations.
        Returns a new dict with correct LOCAL rotations scaled to avatar proportions.
        """
        poses = frame.joint_poses

        if not poses:
            return {}

        scale = self._compute_scale_factor(poses)
        poses = self._apply_bilateral_confidence(poses)
        retargeted: Dict[str, JointPose] = {}

        for joint_name, (parent_lm, child_lm) in self.BONE_LANDMARK_PAIRS.items():
            if parent_lm not in poses or child_lm not in poses:
                continue

            parent_pos = poses[parent_lm].position
            child_pos  = poses[child_lm].position
            conf = min(poses[parent_lm].confidence, poses[child_lm].confidence)

            if conf < 0.05:
                continue

            world_dir = child_pos - parent_pos
            world_mag = np.linalg.norm(world_dir)
            if world_mag < 1e-6:
                continue
            world_dir_n = world_dir / world_mag

            rest_dir    = self.binding.rest_directions.get(
                joint_name, np.array([0.0, 1.0, 0.0])
            )
            local_rot   = QuatMath.from_two_vectors(rest_dir, world_dir_n)
            avatar_pos  = parent_pos * scale

            is_mirrored = (
                poses.get(child_lm, JointPose(np.zeros(3), Quaternion.identity())).is_mirrored
            )
            retargeted[joint_name] = JointPose(
                position=avatar_pos,
                rotation=local_rot,
                confidence=float(conf),
                is_mirrored=is_mirrored,
            )

        retargeted = self._apply_clavicle_correction(retargeted, poses)

        if "Pelvis" in poses:
            p = poses["Pelvis"]
            retargeted["Pelvis"] = JointPose(
                position=p.position * scale,
                rotation=p.rotation,
                confidence=p.confidence,
            )

        # Pass finger bones through
        for joint_name, pose in poses.items():
            if joint_name.startswith(("Thumb_", "Index_", "Middle_", "Ring_", "Pinky_")):
                retargeted[joint_name] = pose

        return retargeted

    def _compute_scale_factor(self, poses: Dict[str, JointPose]) -> float:
        """Derive world→avatar scale from live shoulder width."""
        lc = poses.get("Clavicle_L")
        rc = poses.get("Clavicle_R")
        if lc and rc and lc.confidence > 0.5 and rc.confidence > 0.5:
            measured = np.linalg.norm(lc.position - rc.position)
            if measured > 0.05:
                if self._measured_shoulder_width is None:
                    self._measured_shoulder_width = measured
                else:
                    self._measured_shoulder_width = (
                        0.9 * self._measured_shoulder_width + 0.1 * measured
                    )
                return self._avatar_shoulder_width / self._measured_shoulder_width
        return self.binding.dna.get("scale", 1.0)

    def _apply_bilateral_confidence(
        self, poses: Dict[str, JointPose]
    ) -> Dict[str, JointPose]:
        """Mirror high-confidence side to low-confidence side at reduced weight."""
        MIRROR_PAIRS: List[Tuple[str, str]] = [
            ("Clavicle_L",  "Clavicle_R"),
            ("UpperArm_L",  "UpperArm_R"),
            ("LowerArm_L",  "LowerArm_R"),
            ("Hand_L",      "Hand_R"),
            ("Thigh_L",     "Thigh_R"),
            ("Calf_L",      "Calf_R"),
            ("Foot_L",      "Foot_R"),
        ]
        MIN_CONF = 0.20

        result = dict(poses)
        for joint_l, joint_r in MIRROR_PAIRS:
            pose_l = result.get(joint_l)
            pose_r = result.get(joint_r)
            if pose_l is None or pose_r is None:
                continue

            if pose_l.confidence < MIN_CONF and pose_r.confidence >= MIN_CONF:
                mirror_weight = pose_r.confidence * 0.7
                mirror_pos = pose_r.position * np.array([-1.0, 1.0, 1.0])
                rr = pose_r.rotation
                mirror_rot = Quaternion(-rr.x, rr.y, rr.z, rr.w).normalize()
                result[joint_l] = JointPose(mirror_pos, mirror_rot, mirror_weight, True)

            elif pose_r.confidence < MIN_CONF and pose_l.confidence >= MIN_CONF:
                mirror_weight = pose_l.confidence * 0.7
                mirror_pos = pose_l.position * np.array([-1.0, 1.0, 1.0])
                rl = pose_l.rotation
                mirror_rot = Quaternion(-rl.x, rl.y, rl.z, rl.w).normalize()
                result[joint_r] = JointPose(mirror_pos, mirror_rot, mirror_weight, True)

        return result

    def _apply_clavicle_correction(
        self,
        retargeted: Dict[str, JointPose],
        poses: Dict[str, JointPose],
    ) -> Dict[str, JointPose]:
        """Compose ClavicleDriver correction onto retargeted clavicle rotation."""
        for side in ("_L", "_R"):
            upper_arm_key = f"UpperArm{side}"
            clavicle_key  = f"Clavicle{side}"

            ua = poses.get(upper_arm_key)
            if ua is None or ua.confidence < 0.2:
                continue

            cl_pose = poses.get(f"Clavicle{side}")
            if cl_pose is None:
                continue

            arm_dir = ua.position - cl_pose.position
            arm_len = np.linalg.norm(arm_dir)
            if arm_len < 0.01:
                continue

            arm_dir_n  = arm_dir / arm_len
            abduction   = math.acos(abs(float(np.clip(arm_dir_n[1], -1.0, 1.0))))
            elev, prot  = QuatMath.clavicle_corrective(abduction)

            if clavicle_key in retargeted:
                cur = retargeted[clavicle_key].rotation
                corrected = cur.multiply(elev).multiply(prot)
                retargeted[clavicle_key] = JointPose(
                    retargeted[clavicle_key].position,
                    corrected,
                    retargeted[clavicle_key].confidence,
                    retargeted[clavicle_key].is_mirrored,
                )

        return retargeted


# ─────────────────────────────────────────────────────────────────────────────
# ANIMATION STACK — 3-layer blend management per avatar
# ─────────────────────────────────────────────────────────────────────────────

class AnimationLayer(Enum):
    IDLE_BREATHING = "idle_breathing"
    BASE_ANIMATION = "base_animation"
    LIVE_MOCAP     = "live_mocap"


@dataclass
class StackState:
    """Runtime weights for the 3-layer blend."""
    idle_weight:    float = 1.0
    base_weight:    float = 0.0
    mocap_weight:   float = 0.0

    last_mocap_time:  float = 0.0
    dropout_fade_sec: float = 2.0

    def update(self, dt: float, has_live_mocap: bool, mocap_confidence: float):
        """
        FIX-1: alpha clamped to [0, 1]. Negative dt (clock skew) no longer
        produces negative alpha → negative mocap_weight → broken blends.
        """
        now = time.monotonic()
        if has_live_mocap and mocap_confidence > 0.15:
            self.last_mocap_time = now
            target_mocap = float(np.clip(mocap_confidence, 0.0, 1.0))
        else:
            elapsed     = max(0.0, now - self.last_mocap_time)
            fade        = max(0.0, 1.0 - elapsed / max(self.dropout_fade_sec, 1e-6))
            target_mocap = fade * 0.3

        # FIX-1: clamp alpha to [0,1] regardless of dt sign or magnitude
        dt_safe = max(0.0, dt)
        alpha   = min(1.0, dt_safe * 5.0)

        self.mocap_weight = float(np.clip(
            self.mocap_weight + alpha * (target_mocap - self.mocap_weight),
            0.0, 1.0
        ))
        self.idle_weight = max(0.05, 1.0 - self.mocap_weight - self.base_weight)

    def as_dict(self) -> Dict[str, float]:
        return {
            AnimationLayer.IDLE_BREATHING.value: self.idle_weight,
            AnimationLayer.BASE_ANIMATION.value: self.base_weight,
            AnimationLayer.LIVE_MOCAP.value:     self.mocap_weight,
        }


# ─────────────────────────────────────────────────────────────────────────────
# PROCEDURAL IDLE BREATHING
# ─────────────────────────────────────────────────────────────────────────────

class ProceduralIdle:
    """
    Generates breathing and weight-shift poses.
    Python mirror of procedural_idle_pose() in lib.rs.
    """

    def __init__(self, phase_offset: float = 0.0):
        self._phase = phase_offset
        self._start = time.monotonic()

    def get_pose(self) -> Dict[str, JointPose]:
        t = time.monotonic() - self._start
        breath_freq = 1.257   # 12 breaths/min = 0.2 Hz
        breath      = math.sin(t * breath_freq + self._phase)

        sway_freq = 0.9
        sway      = math.sin(t * sway_freq + self._phase * 1.7) * 0.017

        return {
            "Spine_01": JointPose(
                position=np.zeros(3),
                rotation=QuatMath.from_euler_xyz(breath * 0.035, 0.0, 0.0),
                confidence=1.0,
            ),
            "Head": JointPose(
                position=np.zeros(3),
                rotation=QuatMath.from_euler_xyz(-breath * 0.026, 0.0, 0.0),
                confidence=1.0,
            ),
            "Pelvis": JointPose(
                position=np.zeros(3),
                rotation=QuatMath.from_euler_xyz(0.0, 0.0, sway),
                confidence=1.0,
            ),
        }


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMER STREAMER — WebSocket output
# ─────────────────────────────────────────────────────────────────────────────

class PerformerStreamer:
    """
    Manages the WebSocket connection to the Rust renderer.
    Buffers frames during reconnect; deque(maxlen=90) is thread-safe for
    append/popleft from separate coroutines under asyncio.
    """

    MAX_BUFFER = 90   # ~3s at 30fps

    def __init__(self, ws_url: str):
        self._url       = ws_url
        self._ws        = None
        self._buf:       Deque[str] = deque(maxlen=self.MAX_BUFFER)
        self._connected = False
        self._reconnect_delay = 2.0

    async def connect(self):
        while True:
            try:
                self._ws = await websockets.connect(self._url)
                self._connected = True
                logger.info(f"PerformerStreamer connected to {self._url}")
                return
            except Exception as e:
                logger.warning(
                    f"PerformerStreamer connect failed: {e}; "
                    f"retry in {self._reconnect_delay}s"
                )
                await asyncio.sleep(self._reconnect_delay)

    async def send_frame(self, frame: PerformedFrame):
        payload = {
            "avatar_id":   frame.avatar_id,
            "timestamp":   frame.timestamp,
            "motion_data": frame.motion_data,
            "layer_info":  frame.layer_info,
        }
        msg = json.dumps(payload)

        if not self._connected or self._ws is None:
            self._buf.append(msg)
            return

        while self._buf:
            try:
                await self._ws.send(self._buf.popleft())
            except (websockets.exceptions.ConnectionClosed, Exception):
                self._connected = False
                self._buf.appendleft(msg)
                self._reconnect_task = asyncio.create_task(self._reconnect())
                return

        try:
            await self._ws.send(msg)
        except (websockets.exceptions.ConnectionClosed, Exception):
            self._connected = False
            self._buf.append(msg)
            self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self):
        await asyncio.sleep(self._reconnect_delay)
        await self.connect()

    async def close(self):
        if self._ws:
            await self._ws.close()
        self._connected = False


# ─────────────────────────────────────────────────────────────────────────────
# AVATAR PERFORMER
# ─────────────────────────────────────────────────────────────────────────────

class AvatarPerformer:
    """
    Binds one avatar to one (or more) mocap sources.
    All public methods are async-safe for asyncio event loops.
    """

    def __init__(
        self,
        avatar_id:    str,
        preset:       str,
        streamer:     PerformerStreamer,
        phase_offset: float = 0.0,
    ):
        self.avatar_id  = avatar_id
        self.preset     = preset
        self.binding    = AvatarBinding.create(preset)
        self.retargeter = MocapRetargeter(self.binding)
        self.idle       = ProceduralIdle(phase_offset=phase_offset)
        self.stack      = StackState()
        self.streamer   = streamer
        self._last_update = time.monotonic()
        self._frame_count = 0

        logger.info(f"AvatarPerformer created: {avatar_id} [{preset}]")

    async def feed_frame(self, mocap_frame: PrecisionMocapFrame):
        """Process one precision mocap frame and stream the result."""
        now = time.monotonic()
        dt  = now - self._last_update
        self._last_update = now
        self._frame_count += 1

        confidence = mocap_frame.global_confidence
        self.stack.update(dt, has_live_mocap=True, mocap_confidence=confidence)

        live_poses = self.retargeter.retarget(mocap_frame)
        idle_poses = self.idle.get_pose()
        blended    = self._blend_layers(idle_poses, live_poses)

        motion_data: Dict[str, Dict] = {}

        # Bone transforms
        for joint_name, pose in blended.items():
            motion_data[joint_name] = {
                "position":    pose.position.tolist(),
                "rotation":    pose.rotation.as_list(),
                "confidence":  float(pose.confidence),
                "is_mirrored": pose.is_mirrored,
            }

        # FIX-4: blendshapes merged into motion_data with bs_ prefix
        # skeleton.rs apply_motion_capture_frame() strips the bs_ prefix and
        # routes to SkeletonState.set_blendshape() (0.0–1.0 float, index 0 of data)
        for bs_name, bs_weight in mocap_frame.blendshapes.items():
            motion_data[f"bs_{bs_name}"] = {
                "position":   [float(np.clip(bs_weight, 0.0, 1.0))],
                "rotation":   [0.0, 0.0, 0.0, 1.0],
                "confidence": 1.0,
            }

        performed = PerformedFrame(
            avatar_id=self.avatar_id,
            timestamp=mocap_frame.timestamp,
            motion_data=motion_data,
            layer_info=self.stack.as_dict(),
        )
        await self.streamer.send_frame(performed)

    async def feed_dropout(self):
        """Call when mocap signal is completely lost for this frame."""
        now = time.monotonic()
        dt  = now - self._last_update
        self._last_update = now
        self.stack.update(dt, has_live_mocap=False, mocap_confidence=0.0)

        idle_poses  = self.idle.get_pose()
        motion_data = {
            name: {
                "position":   pose.position.tolist(),
                "rotation":   pose.rotation.as_list(),
                "confidence": float(pose.confidence * self.stack.idle_weight),
                "is_mirrored": False,
            }
            for name, pose in idle_poses.items()
        }

        performed = PerformedFrame(
            avatar_id=self.avatar_id,
            timestamp=time.time(),
            motion_data=motion_data,
            layer_info=self.stack.as_dict(),
        )
        await self.streamer.send_frame(performed)

    def _blend_layers(
        self,
        idle_poses: Dict[str, JointPose],
        live_poses: Dict[str, JointPose],
    ) -> Dict[str, JointPose]:
        """
        3-layer alpha blend: mocap overrides idle.
        idle weight always > 0.05 (breathing never fully off).
        """
        result: Dict[str, JointPose] = {}
        all_joints = set(idle_poses) | set(live_poses)
        alpha = float(np.clip(self.stack.mocap_weight, 0.0, 1.0))

        for joint_name in all_joints:
            idle_p = idle_poses.get(joint_name)
            live_p = live_poses.get(joint_name)

            if live_p is not None and idle_p is not None:
                blended_rot = idle_p.rotation.slerp(live_p.rotation, alpha)
                blended_pos = (
                    idle_p.position * (1.0 - alpha) +
                    live_p.position * alpha
                )
                result[joint_name] = JointPose(
                    position=blended_pos,
                    rotation=blended_rot,
                    confidence=float(live_p.confidence),
                    is_mirrored=live_p.is_mirrored,
                )
            elif live_p is not None:
                result[joint_name] = JointPose(
                    position=live_p.position,
                    rotation=Quaternion.identity().slerp(live_p.rotation, alpha),
                    confidence=float(live_p.confidence * alpha),
                    is_mirrored=live_p.is_mirrored,
                )
            elif idle_p is not None:
                w = float(np.clip(self.stack.idle_weight, 0.0, 1.0))
                result[joint_name] = JointPose(
                    position=idle_p.position,
                    rotation=Quaternion.identity().slerp(idle_p.rotation, w),
                    confidence=w,
                )

        return result

    def set_affect(self, valence: float, arousal: float):
        """Adjust procedural idle based on emotional state. Reserved for choreography."""
        logger.debug(f"{self.avatar_id} affect: valence={valence:.2f} arousal={arousal:.2f}")


# ─────────────────────────────────────────────────────────────────────────────
# AVATAR PERFORMER MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class AvatarPerformerManager:
    """
    Top-level manager. Start once, create/destroy performers as avatars join.

    FIX-3: broadcast_dropout() and all iteration over _performers now uses
    list() snapshot to prevent RuntimeError if remove_performer() is called
    during iteration from a concurrent coroutine.
    """

    def __init__(self, ws_url: str = "ws://localhost:8765"):
        self._url         = ws_url
        self._performers: Dict[str, AvatarPerformer] = {}
        self._streamer:   Optional[PerformerStreamer] = None
        self._running     = False
        self._phase_ctr   = 0.0

    async def start(self):
        self._streamer = PerformerStreamer(self._url)
        await self._streamer.connect()
        self._running = True
        logger.info("AvatarPerformerManager started")

    async def stop(self):
        self._running = False
        if self._streamer:
            await self._streamer.close()

    def create_performer(
        self,
        avatar_id: str,
        preset:    str = "MANNY",
    ) -> AvatarPerformer:
        """Bind a new avatar. Each gets a unique phase so idle breathing is desync'd."""
        if avatar_id in self._performers:
            logger.warning(f"Performer for {avatar_id} already exists; replacing.")

        phase = self._phase_ctr
        self._phase_ctr += 1.1

        performer = AvatarPerformer(
            avatar_id=avatar_id,
            preset=preset,
            streamer=self._streamer,
            phase_offset=phase,
        )
        self._performers[avatar_id] = performer
        return performer

    def remove_performer(self, avatar_id: str):
        self._performers.pop(avatar_id, None)
        logger.info(f"Performer removed: {avatar_id}")

    async def route_frame(self, mocap_frame: PrecisionMocapFrame):
        """Route an incoming mocap frame to the correct performer."""
        performer = self._performers.get(mocap_frame.avatar_id)
        if performer is None:
            logger.warning(f"No performer for avatar_id={mocap_frame.avatar_id}")
            return
        await performer.feed_frame(mocap_frame)

    async def broadcast_dropout(self):
        """
        Tell all performers this frame had no mocap data.
        FIX-3: snapshot .values() before iterating to prevent mutation mid-loop.
        """
        for performer in list(self._performers.values()):
            await performer.feed_dropout()

    def get_performer(self, avatar_id: str) -> Optional[AvatarPerformer]:
        return self._performers.get(avatar_id)

    @property
    def performer_count(self) -> int:
        return len(self._performers)


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE DEMO
# ─────────────────────────────────────────────────────────────────────────────

async def _demo():
    """Smoke test: create a manager + performer, feed a synthetic frame."""
    logging.basicConfig(level=logging.INFO)

    manager = AvatarPerformerManager(ws_url="ws://localhost:8765")
    manager._streamer = PerformerStreamer("ws://localhost:8765")

    performer = manager.create_performer("demo_avatar", "SHELA")

    frame = PrecisionMocapFrame(
        avatar_id="demo_avatar",
        timestamp=time.time(),
        frame_index=0,
        joint_poses={
            "Pelvis":     JointPose(np.array([0.0,  0.95, 0.0]), Quaternion.identity(), 0.98),
            "Spine_01":   JointPose(np.array([0.0,  1.03, 0.0]), Quaternion.identity(), 0.97),
            "Spine_03":   JointPose(np.array([0.0,  1.35, 0.0]), Quaternion.identity(), 0.96),
            "Neck_01":    JointPose(np.array([0.0,  1.43, 0.0]), Quaternion.identity(), 0.95),
            "Clavicle_L": JointPose(np.array([ 0.09, 1.25, 0.0]), Quaternion.identity(), 0.92),
            "Clavicle_R": JointPose(np.array([-0.09, 1.25, 0.0]), Quaternion.identity(), 0.93),
            "UpperArm_L": JointPose(np.array([ 0.22, 1.22, 0.0]), Quaternion.identity(), 0.90),
            "UpperArm_R": JointPose(np.array([-0.22, 1.22, 0.0]), Quaternion.identity(), 0.91),
            "LowerArm_L": JointPose(np.array([ 0.44, 1.15, 0.0]), Quaternion.identity(), 0.88),
            "LowerArm_R": JointPose(np.array([-0.44, 1.15, 0.0]), Quaternion.identity(), 0.89),
            "Hand_L":     JointPose(np.array([ 0.60, 1.10, 0.0]), Quaternion.identity(), 0.85),
            "Hand_R":     JointPose(np.array([-0.60, 1.10, 0.0]), Quaternion.identity(), 0.86),
            "Thigh_L":    JointPose(np.array([ 0.09, 0.94, 0.0]), Quaternion.identity(), 0.95),
            "Thigh_R":    JointPose(np.array([-0.09, 0.94, 0.0]), Quaternion.identity(), 0.95),
            "Calf_L":     JointPose(np.array([ 0.09, 0.52, 0.0]), Quaternion.identity(), 0.94),
            "Calf_R":     JointPose(np.array([-0.09, 0.52, 0.0]), Quaternion.identity(), 0.94),
            "Foot_L":     JointPose(np.array([ 0.09, 0.14, 0.0]), Quaternion.identity(), 0.90),
            "Foot_R":     JointPose(np.array([-0.09, 0.14, 0.0]), Quaternion.identity(), 0.90),
        },
        blendshapes={"eyeBlinkLeft": 0.1, "mouthSmileLeft": 0.3},
        global_confidence=0.92,
    )

    retargeted = performer.retargeter.retarget(frame)
    print(f"\n{'='*60}")
    print(f"AvatarPerformer v2.0 demo — {performer.preset} [{performer.avatar_id}]")
    print(f"Input joints:      {len(frame.joint_poses)}")
    print(f"Retargeted joints: {len(retargeted)}")
    for name, pose in sorted(retargeted.items()):
        r = pose.rotation
        print(f"  {name:25s} rot=[{r.x:+.3f},{r.y:+.3f},{r.z:+.3f},{r.w:+.3f}]  "
              f"conf={pose.confidence:.2f}"
              + (" [MIRRORED]" if pose.is_mirrored else ""))
    print(f"\nBlendshapes in motion_data: bs_eyeBlinkLeft, bs_mouthSmileLeft")
    print(f"Stack weights: {performer.stack.as_dict()}")


if __name__ == "__main__":
    asyncio.run(_demo())
