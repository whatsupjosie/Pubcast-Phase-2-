"""
MOTION CAPTURE INTEGRATION SYSTEM

The complete integration of the PubCast "Soft Triangle" avatar system
with the twin engine architecture. This system processes motion capture
data through sophisticated algorithms and streams it to the rendering
pipeline.

Components:
- Avatar System: PubCast avatars with swing-twist decomposition
- Motion Adapter: Real-time processing and filtering
- Tracking Engine: Advanced motion analysis and prediction
- Streaming Interface: Bridge integration for real-time data flow

The system maintains the "Soft Triangle" aesthetic philosophy while
providing broadcast-quality motion capture for podcasting applications.
"""

from __future__ import annotations

import asyncio
import logging
import time
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Set, Callable
from enum import Enum
import json
import threading
from pathlib import Path

logger = logging.getLogger("pubcast.motion")

class HandMode(Enum):
    STANDARD = "STANDARD"  # Thumb + Index + Mitt (3-Point)
    ADVANCED = "ADVANCED"  # Full 5-Finger

class AvatarPreset(Enum):
    MANNY = "MANNY"
    SHELA = "SHELA" 
    MAN_LARGE = "MAN_LARGE"
    MAN_SMALL = "MAN_SMALL"
    CHILD = "CHILD"
    BABY = "BABY"
    DOG = "DOG"

@dataclass
class BoneTransform:
    """Transform data for a single bone"""
    position: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])  # Quaternion
    scale: List[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    
@dataclass 
class MotionFrame:
    """Complete motion capture frame for an avatar"""
    avatar_id: str
    timestamp: float
    bones: Dict[str, BoneTransform] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

class TrackingMath:
    """
    Core mathematical utilities for motion processing.
    Implements the swing-twist decomposition and other advanced algorithms.
    """
    
    @staticmethod
    def quaternion_multiply(q1: List[float], q2: List[float]) -> List[float]:
        """Multiply two quaternions"""
        x1, y1, z1, w1 = q1
        x2, y2, z2, w2 = q2
        return [
            w1*x2 + x1*w2 + y1*z2 - z1*y2,
            w1*y2 - x1*z2 + y1*w2 + z1*x2,
            w1*z2 + x1*y2 - y1*x2 + z1*w2,
            w1*w2 - x1*x2 - y1*y2 - z1*z2
        ]
    
    @staticmethod
    def quaternion_conjugate(q: List[float]) -> List[float]:
        """Get quaternion conjugate"""
        return [-q[0], -q[1], -q[2], q[3]]
    
    @staticmethod
    def quaternion_normalize(q: List[float]) -> List[float]:
        """Normalize a quaternion"""
        magnitude = sum(x*x for x in q) ** 0.5
        if magnitude < 1e-6:
            return [0.0, 0.0, 0.0, 1.0]
        return [x / magnitude for x in q]
    
    @staticmethod
    def decompose_swing_twist(quat: List[float], direction_axis: List[float] = None) -> tuple[List[float], List[float]]:
        """
        Decompose rotation into swing (direction) and twist (axial rotation).
        This is the core algorithm that makes the "Soft Triangle" aesthetic work.
        
        Args:
            quat: (x, y, z, w) quaternion
            direction_axis: The bone's primary axis (usually [1, 0, 0])
            
        Returns:
            (swing_quaternion, twist_quaternion)
        """
        if direction_axis is None:
            direction_axis = [1.0, 0.0, 0.0]
        
        qx, qy, qz, qw = quat
        dx, dy, dz = direction_axis
        
        # Project the rotation onto the twist axis
        projection = qx * dx + qy * dy + qz * dz
        
        # Calculate twist quaternion (rotation around the bone axis)
        twist_q = [
            projection * dx,
            projection * dy,
            projection * dz,
            qw
        ]
        
        # Normalize twist quaternion
        twist_q = TrackingMath.quaternion_normalize(twist_q)
        
        # Calculate swing quaternion (swing = total * inverse(twist))
        twist_conjugate = TrackingMath.quaternion_conjugate(twist_q)
        swing_q = TrackingMath.quaternion_multiply(quat, twist_conjugate)
        swing_q = TrackingMath.quaternion_normalize(swing_q)
        
        return swing_q, twist_q
    
    @staticmethod 
    def euler_to_quaternion(euler_degrees: List[float]) -> List[float]:
        """Convert Euler angles (degrees) to quaternion"""
        import math
        
        # Convert to radians
        x, y, z = [math.radians(angle) for angle in euler_degrees]
        
        # Calculate quaternion
        cy = math.cos(z * 0.5)
        sy = math.sin(z * 0.5)
        cp = math.cos(y * 0.5)
        sp = math.sin(y * 0.5)
        cr = math.cos(x * 0.5)
        sr = math.sin(x * 0.5)

        return [
            sr * cp * cy - cr * sp * sy,  # x
            cr * sp * cy + sr * cp * sy,  # y  
            cr * cp * sy - sr * sp * cy,  # z
            cr * cp * cy + sr * sp * sy   # w
        ]

class BoneNode:
    """
    A single bone in the PubCast avatar skeleton.
    Supports the "Soft Triangle" visual system and advanced tracking.
    """
    
    def __init__(self, name: str, parent: Optional['BoneNode'] = None, shape_type: str = "NONE"):
        self.name = name
        self.parent = parent
        self.children: List['BoneNode'] = []
        self.shape_type = shape_type  # 'TRIANGLE_PRISM', 'SPHERE_BUFFER', etc.
        
        # Transform data
        self.local_rotation = [0.0, 0.0, 0.0, 1.0]  # Quaternion
        self.local_position = [0.0, 0.0, 0.0]
        self.rest_rotation = [0.0, 0.0, 0.0, 1.0]    # Default pose
        
        # Tracking state
        self.is_tracking_enabled = True
        self.confidence = 1.0  # Motion tracking confidence (0-1)
        self.velocity = [0.0, 0.0, 0.0]  # For prediction
        
        # Visual properties
        self.visual_mesh = None
        self.visible = True
        
        # Parent-child relationship
        if parent:
            parent.children.append(self)
    
    def set_rotation(self, quat: List[float]) -> None:
        """Set bone rotation (only if tracking enabled)"""
        if self.is_tracking_enabled:
            self.local_rotation = TrackingMath.quaternion_normalize(quat)
    
    def get_current_rotation(self) -> List[float]:
        """Get current rotation (tracking or rest pose)"""
        if self.is_tracking_enabled:
            return self.local_rotation
        else:
            return self.rest_rotation
    
    def set_casual_default(self, euler_tuple: List[float]) -> None:
        """Set the casual/rest pose"""
        self.rest_rotation = TrackingMath.euler_to_quaternion(euler_tuple)

class PubCastRig:
    """
    The complete PubCast avatar skeleton system.
    Implements the "Soft Triangle" hierarchy with 89-bone UE5 compatibility.
    """
    
    def __init__(self, preset_name: str = "MANNY"):
        self.bones: Dict[str, BoneNode] = {}
        self.preset_name = preset_name
        self.dna = self._get_avatar_dna(preset_name)
        
        # Build the skeleton
        self._build_hierarchy()
        self._build_extras()
        self._apply_casual_defaults()
        self._assign_visual_meshes()
        
        logger.info(f"ðŸŽ¯ PubCast Rig built: {preset_name} ({len(self.bones)} bones)")
    
    def _get_avatar_dna(self, preset: str) -> Dict[str, float]:
        """Get avatar DNA/proportions for different presets"""
        dna_presets = {
            "MANNY": {"scale": 1.0, "width_mod": 1.0, "head_scale": 1.0, "limb_thick": 1.0},
            "SHELA": {"scale": 0.92, "width_mod": 0.85, "head_scale": 0.95, "limb_thick": 0.8},
            "MAN_LARGE": {"scale": 1.1, "width_mod": 1.4, "head_scale": 0.9, "limb_thick": 1.5},
            "MAN_SMALL": {"scale": 0.85, "width_mod": 0.9, "head_scale": 1.1, "limb_thick": 0.9},
            "CHILD": {"scale": 0.6, "width_mod": 0.7, "head_scale": 1.4, "limb_thick": 0.6},
            "BABY": {"scale": 0.35, "width_mod": 0.6, "head_scale": 2.2, "limb_thick": 0.8},
            "DOG": {"scale": 0.8, "width_mod": 0.8, "head_scale": 1.0, "spine_orient": "HORIZONTAL"}
        }
        return dna_presets.get(preset, dna_presets["MANNY"])
    
    def _build_hierarchy(self) -> None:
        """Build the core 89-bone skeleton hierarchy"""
        # Root and core
        root = self._add_bone("Root", None, "NONE")
        pelvis = self._add_bone("Pelvis", root, "TRIANGLE_WEDGE")
        
        # Spine stack
        spine_01 = self._add_bone("Spine_01", pelvis, "TRIANGLE_PRISM")
        spine_02 = self._add_bone("Spine_02", spine_01, "TRIANGLE_PRISM") 
        spine_03 = self._add_bone("Spine_03", spine_02, "TRIANGLE_PRISM")
        neck = self._add_bone("Neck_01", spine_03, "POLE")
        head = self._add_bone("Head", neck, "COWL_FEATURELESS")
        
        # Arms (with swing-twist separation)
        for side in ["_L", "_R"]:
            clavicle = self._add_bone(f"Clavicle{side}", spine_03, "FLOATING_PAD")
            
            # Upper arm: Hinge (for direction) + Twist (for rotation)
            upper_arm = self._add_bone(f"UpperArm{side}", clavicle, "SPHERE_BUFFER")
            upper_twist = self._add_bone(f"UpperArm_Twist{side}", upper_arm, "TRIANGLE_PRISM")
            
            # Lower arm: Similar structure
            lower_arm = self._add_bone(f"LowerArm{side}", upper_arm, "SPHERE_BUFFER") 
            lower_twist = self._add_bone(f"LowerArm_Twist{side}", lower_arm, "TRIANGLE_PRISM")
            
            # Hand
            self._build_hand(lower_arm, side)
            
            # Legs
            thigh = self._add_bone(f"Thigh{side}", pelvis, "SPHERE_BUFFER")
            thigh_twist = self._add_bone(f"Thigh_Twist{side}", thigh, "TRIANGLE_PRISM")
            calf = self._add_bone(f"Calf{side}", thigh, "TRIANGLE_PRISM")
            
            # Foot
            self._build_foot(calf, side)
    
    def _build_hand(self, arm_end: BoneNode, side: str) -> None:
        """Build hand with Standard mode (Thumb + Index + Blade)"""
        # High-precision digits
        self._add_bone(f"Thumb_01{side}", arm_end, "TRIANGLE_PRISM") 
        self._add_bone(f"Index_01{side}", arm_end, "STYLUS_TAPER")
        
        # The blade (fused middle/ring/pinky for podcast gesturing)
        self._add_bone(f"Blade_Array{side}", arm_end, "BLADE_WING")
        
        # Hidden bones for advanced mode (when needed)
        self._add_bone(f"Middle_01{side}", arm_end, "NONE")
        self._add_bone(f"Ring_01{side}", arm_end, "NONE") 
        self._add_bone(f"Pinky_01{side}", arm_end, "NONE")
    
    def _build_foot(self, leg_end: BoneNode, side: str) -> None:
        """Build foot with spring blade design"""
        foot = self._add_bone(f"Foot{side}", leg_end, "TRIANGLE_HEAVY")
        ball = self._add_bone(f"Ball{side}", foot, "SPHERE_BUFFER")
        toe = self._add_bone(f"Toe{side}", ball, "BLADE_TOE")
    
    def _build_extras(self) -> None:
        """Build additional bones (tail, jaw, etc.)"""
        # Tail for non-human avatars
        tail_parent = self.bones["Pelvis"]
        for i in range(1, 6):
            tail_parent = self._add_bone(f"Tail_0{i}", tail_parent, "CONE_TAPER")
        
        # Jaw for speech animation
        self._add_bone("Jaw", self.bones["Head"], "U_SHAPE_PRISM")
        
        # Attachment points
        self._add_bone("Socket_Hand_R", self.bones.get("LowerArm_R"), "NONE")
        self._add_bone("Socket_Hip_L", self.bones["Pelvis"], "NONE")
    
    def _add_bone(self, name: str, parent: Optional[BoneNode], shape: str) -> BoneNode:
        """Add a bone to the skeleton"""
        node = BoneNode(name, parent, shape)
        self.bones[name] = node
        return node
    
    def _apply_casual_defaults(self) -> None:
        """Apply casual pose to avoid T-pose look"""
        casual_pose = {
            # Arms down by sides, slight internal roll
            "UpperArm_L": [0, 0, -80],
            "UpperArm_R": [0, 0, 80],
            # Slight elbow bend
            "LowerArm_L": [0, 15, 0],
            "LowerArm_R": [0, -15, 0],
            # Natural stance width
            "Thigh_L": [0, 0, 5],
            "Thigh_R": [0, 0, -5],
            # Natural spine curve
            "Spine_01": [0, 5, 0],
            "Neck_01": [0, -5, 0]
        }
        
        for bone_name, rotation_euler in casual_pose.items():
            if bone_name in self.bones:
                self.bones[bone_name].set_casual_default(rotation_euler)
    
    def _assign_visual_meshes(self) -> None:
        """Assign visual mesh files to bones"""
        mesh_assignments = {
            "Head": "GEO_Head_Cowl_V1.obj",
            "Spine_03": "GEO_Torso_Upper.obj", 
            "Pelvis": "GEO_Torso_Lower.obj",
            
            # Arms (assign to twist bones for proper rotation)
            "UpperArm_Twist_L": "GEO_Limb_Triangle.obj",
            "LowerArm_Twist_L": "GEO_Forearm_Triangle.obj",
            "UpperArm_L": "GEO_Joint_Sphere_Buffer.obj",
            
            "UpperArm_Twist_R": "GEO_Limb_Triangle.obj", 
            "LowerArm_Twist_R": "GEO_Forearm_Triangle.obj",
            "UpperArm_R": "GEO_Joint_Sphere_Buffer.obj",
            
            # Legs
            "Thigh_Twist_L": "GEO_Limb_Triangle.obj",
            "Calf_L": "GEO_Limb_Triangle.obj",
            "Thigh_L": "GEO_Joint_Sphere_Buffer.obj",
            
            "Thigh_Twist_R": "GEO_Limb_Triangle.obj",
            "Calf_R": "GEO_Limb_Triangle.obj", 
            "Thigh_R": "GEO_Joint_Sphere_Buffer.obj",
            
            # Hands
            "Thumb_01_L": "GEO_Triangle_Prism.obj",
            "Index_01_L": "GEO_Stylus_Taper.obj",
            "Blade_Array_L": "GEO_Blade_Wing.obj",
            
            "Thumb_01_R": "GEO_Triangle_Prism.obj",
            "Index_01_R": "GEO_Stylus_Taper.obj", 
            "Blade_Array_R": "GEO_Blade_Wing.obj",
            
            # Feet
            "Foot_L": "GEO_Foot_Wedge.obj",
            "Ball_L": "GEO_Joint_Sphere_Buffer.obj",
            "Toe_L": "GEO_Blade_Toe.obj",
            
            "Foot_R": "GEO_Foot_Wedge.obj",
            "Ball_R": "GEO_Joint_Sphere_Buffer.obj",
            "Toe_R": "GEO_Blade_Toe.obj"
        }
        
        for bone_name, mesh_file in mesh_assignments.items():
            if bone_name in self.bones:
                self.bones[bone_name].visual_mesh = mesh_file

class GripSolver:
    """
    The smart grip system for podcast-optimized hand control.
    Ensures natural hand poses and proper object interaction.
    """
    
    def __init__(self, mode: HandMode = HandMode.STANDARD):
        self.mode = mode
    
    def update_grip(
        self, 
        rig: PubCastRig, 
        raw_hand_data: Dict[str, List[float]], 
        side: str,
        is_grasping_object: bool = False
    ) -> None:
        """Process hand motion data for natural gesturing"""
        
        # Direct control for thumb and index (high precision)
        if f"Thumb_01{side}" in rig.bones:
            rig.bones[f"Thumb_01{side}"].set_rotation(raw_hand_data.get("Thumb", [0,0,0,1]))
        if f"Index_01{side}" in rig.bones:
            rig.bones[f"Index_01{side}"].set_rotation(raw_hand_data.get("Index", [0,0,0,1]))
        
        # Smart blade processing
        if f"Blade_Array{side}" in rig.bones:
            blade_rotation = self._calculate_blade_rotation(
                raw_hand_data, is_grasping_object
            )
            rig.bones[f"Blade_Array{side}"].set_rotation(blade_rotation)
    
    def _calculate_blade_rotation(
        self, 
        raw_data: Dict[str, List[float]], 
        is_grasping: bool
    ) -> List[float]:
        """Calculate optimal blade rotation from middle/ring/pinky data"""
        
        middle_rot = raw_data.get("Middle", [0,0,0,1])
        ring_rot = raw_data.get("Ring", [0,0,0,1]) 
        pinky_rot = raw_data.get("Pinky", [0,0,0,1])
        
        if is_grasping:
            # Grip mode: Use maximum curl for secure grip
            return self._max_curl_rotation([middle_rot, ring_rot, pinky_rot])
        else:
            # Gesture mode: Average for natural movement
            return self._average_rotations([middle_rot, ring_rot, pinky_rot])
    
    def _max_curl_rotation(self, rotations: List[List[float]]) -> List[float]:
        """Find rotation with maximum curl for gripping"""
        # Simplified: return first rotation (would implement proper curl analysis)
        return rotations[0] if rotations else [0,0,0,1]
    
    def _average_rotations(self, rotations: List[List[float]]) -> List[float]:
        """Average multiple quaternion rotations"""
        if not rotations:
            return [0,0,0,1]
        
        # Simplified averaging (proper quaternion SLERP would be better)
        avg_x = sum(q[0] for q in rotations) / len(rotations)
        avg_y = sum(q[1] for q in rotations) / len(rotations)
        avg_z = sum(q[2] for q in rotations) / len(rotations)
        avg_w = sum(q[3] for q in rotations) / len(rotations)
        
        return TrackingMath.quaternion_normalize([avg_x, avg_y, avg_z, avg_w])

class TrackingEngine:
    """
    Advanced motion tracking and processing engine.
    Handles swing-twist decomposition, prediction, and filtering.
    """
    
    def __init__(self, rig: PubCastRig):
        self.rig = rig
        self.grip_solver = GripSolver()
        
        # Performance tracking
        self._frame_count = 0
        self._last_frame_time = 0.0
        self._processing_times: List[float] = []
    
    def solve_limb_tracking(self, bone_name: str, raw_rotation_quat: List[float]) -> None:
        """
        Process limb rotation using swing-twist decomposition.
        This is the core of the "Soft Triangle" motion system.
        """
        try:
            # Decompose rotation into swing (direction) and twist (axial)
            swing, twist = TrackingMath.decompose_swing_twist(raw_rotation_quat)
            
            # Apply to appropriate bones based on type
            if any(limb in bone_name for limb in ["UpperArm", "Thigh"]):
                # The hinge bone gets swing (pointing direction)
                if bone_name in self.rig.bones:
                    self.rig.bones[bone_name].set_rotation(swing)
                
                # The twist bone gets axial rotation
                twist_name = bone_name.replace("UpperArm", "UpperArm_Twist").replace("Thigh", "Thigh_Twist")
                if twist_name in self.rig.bones:
                    self.rig.bones[twist_name].set_rotation(twist)
            
            elif any(limb in bone_name for limb in ["LowerArm", "Calf"]):
                # Similar processing for lower limbs
                if bone_name in self.rig.bones:
                    self.rig.bones[bone_name].set_rotation(swing)
                
                twist_name = bone_name.replace("LowerArm", "LowerArm_Twist")
                if twist_name in self.rig.bones:
                    self.rig.bones[twist_name].set_rotation(twist)
            
            else:
                # Standard bones get full rotation
                if bone_name in self.rig.bones:
                    self.rig.bones[bone_name].set_rotation(raw_rotation_quat)
                    
        except Exception as e:
            logger.error(f"ðŸŽ¯ Limb tracking failed for {bone_name}: {e}")
    
    def process_hand_data(self, raw_hand_data: Dict[str, List[float]], side: str) -> None:
        """Process hand motion data through grip solver"""
        try:
            self.grip_solver.update_grip(self.rig, raw_hand_data, side)
        except Exception as e:
            logger.error(f"ðŸŽ¯ Hand processing failed for {side}: {e}")

class PubCastAvatar:
    """
    Complete PubCast avatar with motion processing.
    The final integrated system that combines skeleton, tracking, and streaming.
    """
    
    def __init__(
        self, 
        avatar_id: str,
        preset: str = "MANNY",
        hand_mode: HandMode = HandMode.STANDARD
    ):
        self.avatar_id = avatar_id
        self.preset = preset
        self.hand_mode = hand_mode
        
        # Core systems
        self.rig = PubCastRig(preset)
        self.tracking_engine = TrackingEngine(self.rig)
        
        # Performance state
        self._active = True
        self._last_update = 0.0
        self._frame_count = 0
        
        logger.info(f"ðŸŽ­ PubCast Avatar '{avatar_id}' created ({preset})")
    
    def update_frame(self, raw_mocap_frame: Dict[str, List[float]]) -> None:
        """
        Process a complete motion capture frame.
        This is called at high frequency (60-120 FPS) for real-time motion.
        """
        start_time = time.time()
        
        try:
            # Process limb rotations (excluding hands)
            for bone_name, rotation in raw_mocap_frame.items():
                if not any(finger in bone_name for finger in ["Thumb", "Index", "Middle", "Ring", "Pinky"]):
                    self.tracking_engine.solve_limb_tracking(bone_name, rotation)
            
            # Process hands separately
            left_hand_data = self._extract_hand_data(raw_mocap_frame, "_L")
            self.tracking_engine.process_hand_data(left_hand_data, "_L")
            
            right_hand_data = self._extract_hand_data(raw_mocap_frame, "_R")
            self.tracking_engine.process_hand_data(right_hand_data, "_R")
            
            # Update frame statistics
            self._frame_count += 1
            self._last_update = time.time()
            
        except Exception as e:
            logger.error(f"ðŸŽ­ Frame update failed for {self.avatar_id}: {e}")
    
    def _extract_hand_data(self, frame: Dict[str, List[float]], side: str) -> Dict[str, List[float]]:
        """Extract hand-specific data from motion frame"""
        return {
            "Thumb": frame.get(f"Thumb_01{side}", [0,0,0,1]),
            "Index": frame.get(f"Index_01{side}", [0,0,0,1]),
            "Middle": frame.get(f"Middle_01{side}", [0,0,0,1]),
            "Ring": frame.get(f"Ring_01{side}", [0,0,0,1]),
            "Pinky": frame.get(f"Pinky_01{side}", [0,0,0,1])
        }
    
    def get_visual_bone_data(self) -> Dict[str, Dict[str, Any]]:
        """
        Get current bone data for rendering.
        Returns only bones that have visual meshes attached.
        """
        visual_data = {}
        
        for bone_name, bone in self.rig.bones.items():
            if bone.visual_mesh and bone.visible:
                visual_data[bone_name] = {
                    "position": bone.local_position,
                    "rotation": bone.get_current_rotation(),
                    "mesh": bone.visual_mesh,
                    "shape_type": bone.shape_type,
                    "confidence": bone.confidence
                }
        
        return visual_data
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get avatar performance statistics"""
        current_time = time.time()
        uptime = current_time - (self._last_update - (self._frame_count / max(1, self._frame_count)) * 60)
        
        return {
            "avatar_id": self.avatar_id,
            "preset": self.preset,
            "active": self._active,
            "frame_count": self._frame_count,
            "last_update": self._last_update,
            "uptime_seconds": uptime,
            "bone_count": len(self.rig.bones),
            "visual_bone_count": len([b for b in self.rig.bones.values() if b.visual_mesh])
        }

class MotionAdapter:
    """
    Static motion processing adapter.
    Converts raw motion data into clean visual data for the renderer.
    """
    
    @staticmethod
    def process_frame(avatar_instance: PubCastAvatar, raw_frame: Dict[str, List[float]]) -> Dict[str, Dict[str, Any]]:
        """
        Master processing function.
        Takes raw sensor data and returns clean visual data for rendering.
        """
        try:
            # Update avatar internal state
            avatar_instance.update_frame(raw_frame)
            
            # Extract visual bone data
            visual_data = avatar_instance.get_visual_bone_data()
            
            return visual_data
            
        except Exception as e:
            logger.error(f"ðŸŽ¯ Motion adapter processing failed: {e}")
            return {}

class MotionSystem:
    """
    Complete motion capture system manager.
    Coordinates multiple avatars and streaming to the rendering pipeline.
    """
    
    def __init__(self, bridge: Optional[Any] = None):
        self.bridge = bridge
        
        # Avatar management
        self.avatars: Dict[str, PubCastAvatar] = {}
        self.motion_adapter = MotionAdapter()
        
        # Streaming state
        self._streaming = False
        self._stream_task: Optional[asyncio.Task] = None
        
        # Performance tracking
        self._total_frames_processed = 0
        self._start_time = time.time()
        
        logger.info("ðŸŽ¯ Motion System initialized")
    
    def set_bridge(self, bridge: Any) -> None:
        """Update the bridge connection"""
        self.bridge = bridge
        logger.info("ðŸŽ¯ Motion System: Bridge connection updated")
    
    def create_avatar(
        self, 
        avatar_id: str, 
        preset: str = "MANNY",
        hand_mode: HandMode = HandMode.STANDARD
    ) -> PubCastAvatar:
        """Create and register a new avatar"""
        
        if avatar_id in self.avatars:
            logger.warning(f"ðŸŽ¯ Avatar {avatar_id} already exists, replacing")
        
        avatar = PubCastAvatar(avatar_id, preset, hand_mode)
        self.avatars[avatar_id] = avatar
        
        logger.info(f"ðŸŽ­ Avatar created: {avatar_id} ({preset})")
        return avatar
    
    def remove_avatar(self, avatar_id: str) -> bool:
        """Remove an avatar from the system"""
        if avatar_id in self.avatars:
            del self.avatars[avatar_id]
            logger.info(f"ðŸŽ­ Avatar removed: {avatar_id}")
            return True
        return False
    
    async def start_streaming(self) -> None:
        """Start streaming motion data to the rendering pipeline"""
        if self._streaming:
            logger.warning("ðŸŽ¯ Motion streaming already active")
            return
        
        self._streaming = True
        self._stream_task = asyncio.create_task(self._streaming_loop())
        
        logger.info("ðŸŽ¯ Motion streaming started")
    
    async def stop_streaming(self) -> None:
        """Stop motion data streaming"""
        self._streaming = False
        
        if self._stream_task:
            self._stream_task.cancel()
            self._stream_task = None
        
        logger.info("ðŸŽ¯ Motion streaming stopped")
    
    async def _streaming_loop(self) -> None:
        """Main streaming loop - sends motion data to bridge"""
        target_fps = 60  # Target streaming rate
        frame_interval = 1.0 / target_fps
        
        while self._streaming:
            try:
                start_time = time.time()
                
                # Process all active avatars
                for avatar_id, avatar in self.avatars.items():
                    if avatar._active:
                        # Get current visual bone data
                        visual_data = avatar.get_visual_bone_data()
                        
                        # Send to bridge if available
                        if self.bridge and visual_data:
                            self.bridge.send_motion_data(avatar_id, visual_data)
                
                self._total_frames_processed += 1
                
                # Maintain target framerate
                elapsed = time.time() - start_time
                sleep_time = max(0, frame_interval - elapsed)
                await asyncio.sleep(sleep_time)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ðŸŽ¯ Streaming loop error: {e}")
                await asyncio.sleep(0.1)  # Brief pause on error
    
    def process_motion_frame(self, avatar_id: str, raw_frame: Dict[str, List[float]]) -> bool:
        """Process a motion capture frame for a specific avatar"""
        if avatar_id not in self.avatars:
            logger.warning(f"ðŸŽ¯ Avatar {avatar_id} not found for motion processing")
            return False
        
        try:
            # Process through motion adapter
            visual_data = self.motion_adapter.process_frame(self.avatars[avatar_id], raw_frame)
            
            # Send to bridge immediately (real-time)
            if self.bridge and visual_data:
                self.bridge.send_motion_data(avatar_id, visual_data)
            
            return True
            
        except Exception as e:
            logger.error(f"ðŸŽ¯ Motion frame processing failed for {avatar_id}: {e}")
            return False
    
    def get_system_stats(self) -> Dict[str, Any]:
        """Get motion system statistics"""
        uptime = time.time() - self._start_time
        
        return {
            "avatars": len(self.avatars),
            "streaming": self._streaming,
            "total_frames_processed": self._total_frames_processed,
            "uptime_seconds": uptime,
            "average_fps": self._total_frames_processed / uptime if uptime > 0 else 0,
            "avatar_stats": {
                avatar_id: avatar.get_performance_stats() 
                for avatar_id, avatar in self.avatars.items()
            }
        }
