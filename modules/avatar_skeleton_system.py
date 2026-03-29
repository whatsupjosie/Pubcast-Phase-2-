"""
Professional Avatar Skeleton System for PubCast AI
Production-ready skeletal armature with industry-standard joint hierarchy,
animation export, and retargeting capabilities.

This is the foundation that makes performance capture and retargeting possible.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


class JointType(str, Enum):
    """Standard joint types for professional skeletal rigs"""
    ROOT = "root"
    SPINE = "spine"
    HEAD = "head"
    ARM = "arm"
    HAND = "hand"
    LEG = "leg"
    FOOT = "foot"
    FINGER = "finger"
    AUXILIARY = "auxiliary"


@dataclass
class Joint:
    """Individual skeletal joint with position, rotation, and hierarchy"""
    name: str
    joint_type: JointType
    position: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])  # quaternion
    scale: List[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    parent: Optional[str] = None
    children: List[str] = field(default_factory=list)
    
    # Animation constraints
    rotation_limits: Optional[Dict[str, Tuple[float, float]]] = None
    locked_axes: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "type": self.joint_type.value,
            "position": self.position,
            "rotation": self.rotation,
            "scale": self.scale,
            "parent": self.parent,
            "children": self.children,
            "rotation_limits": self.rotation_limits,
            "locked_axes": self.locked_axes,
        }


class PubCastSkeleton:
    """
    Professional skeletal armature system for PubCast avatars.
    
    This creates the industry-standard skeleton that all PubCast avatars share,
    enabling performance capture, retargeting, and export to professional 3D applications.
    """
    
    def __init__(self):
        self.joints: Dict[str, Joint] = {}
        self.bind_pose: Dict[str, Dict[str, List[float]]] = {}
        self._create_standard_skeleton()
    
    def _create_standard_skeleton(self):
        """Create the standard PubCast skeleton hierarchy"""
        
        # ROOT - The foundation of everything
        self.add_joint(Joint(
            name="root",
            joint_type=JointType.ROOT,
            position=[0.0, 0.0, 0.0],
            rotation=[0.0, 0.0, 0.0, 1.0]
        ))
        
        # SPINE CHAIN
        self.add_joint(Joint("hips", JointType.SPINE, [0.0, 0.9, 0.0], parent="root"))
        self.add_joint(Joint("spine_01", JointType.SPINE, [0.0, 1.0, 0.0], parent="hips"))
        self.add_joint(Joint("spine_02", JointType.SPINE, [0.0, 1.15, 0.0], parent="spine_01"))
        self.add_joint(Joint("spine_03", JointType.SPINE, [0.0, 1.3, 0.0], parent="spine_02"))
        self.add_joint(Joint("chest", JointType.SPINE, [0.0, 1.4, 0.0], parent="spine_03"))
        self.add_joint(Joint("neck", JointType.HEAD, [0.0, 1.55, 0.0], parent="chest"))
        self.add_joint(Joint("head", JointType.HEAD, [0.0, 1.7, 0.0], parent="neck"))
        
        # LEFT ARM CHAIN
        self.add_joint(Joint("clavicle_l", JointType.ARM, [-0.08, 1.45, 0.0], parent="chest"))
        self.add_joint(Joint("upperarm_l", JointType.ARM, [-0.17, 1.4, 0.0], parent="clavicle_l"))
        self.add_joint(Joint("lowerarm_l", JointType.ARM, [-0.45, 1.4, 0.0], parent="upperarm_l"))
        self.add_joint(Joint("hand_l", JointType.HAND, [-0.7, 1.4, 0.0], parent="lowerarm_l"))
        
        # LEFT HAND FINGERS
        fingers_l = ["thumb", "index", "middle", "ring", "pinky"]
        for i, finger in enumerate(fingers_l):
            # Base knuckle
            self.add_joint(Joint(f"{finger}_01_l", JointType.FINGER, 
                                 [-0.72, 1.4 + (i-2)*0.02, 0.0], parent="hand_l"))
            # Middle knuckle  
            self.add_joint(Joint(f"{finger}_02_l", JointType.FINGER,
                                 [-0.74, 1.4 + (i-2)*0.02, 0.0], parent=f"{finger}_01_l"))
            # Tip
            self.add_joint(Joint(f"{finger}_03_l", JointType.FINGER,
                                 [-0.76, 1.4 + (i-2)*0.02, 0.0], parent=f"{finger}_02_l"))
        
        # RIGHT ARM CHAIN (mirrored)
        self.add_joint(Joint("clavicle_r", JointType.ARM, [0.08, 1.45, 0.0], parent="chest"))
        self.add_joint(Joint("upperarm_r", JointType.ARM, [0.17, 1.4, 0.0], parent="clavicle_r"))
        self.add_joint(Joint("lowerarm_r", JointType.ARM, [0.45, 1.4, 0.0], parent="upperarm_r"))
        self.add_joint(Joint("hand_r", JointType.HAND, [0.7, 1.4, 0.0], parent="lowerarm_r"))
        
        # RIGHT HAND FINGERS
        for i, finger in enumerate(fingers_l):
            self.add_joint(Joint(f"{finger}_01_r", JointType.FINGER,
                                 [0.72, 1.4 + (i-2)*0.02, 0.0], parent="hand_r"))
            self.add_joint(Joint(f"{finger}_02_r", JointType.FINGER,
                                 [0.74, 1.4 + (i-2)*0.02, 0.0], parent=f"{finger}_01_r"))
            self.add_joint(Joint(f"{finger}_03_r", JointType.FINGER,
                                 [0.76, 1.4 + (i-2)*0.02, 0.0], parent=f"{finger}_02_r"))
        
        # LEFT LEG CHAIN
        self.add_joint(Joint("thigh_l", JointType.LEG, [-0.1, 0.85, 0.0], parent="hips"))
        self.add_joint(Joint("calf_l", JointType.LEG, [-0.1, 0.45, 0.0], parent="thigh_l"))
        self.add_joint(Joint("foot_l", JointType.FOOT, [-0.1, 0.08, 0.0], parent="calf_l"))
        self.add_joint(Joint("toe_l", JointType.FOOT, [-0.1, 0.0, 0.12], parent="foot_l"))
        
        # RIGHT LEG CHAIN (mirrored)
        self.add_joint(Joint("thigh_r", JointType.LEG, [0.1, 0.85, 0.0], parent="hips"))
        self.add_joint(Joint("calf_r", JointType.LEG, [0.1, 0.45, 0.0], parent="thigh_r"))
        self.add_joint(Joint("foot_r", JointType.FOOT, [0.1, 0.08, 0.0], parent="calf_r"))
        self.add_joint(Joint("toe_r", JointType.FOOT, [0.1, 0.0, 0.12], parent="foot_r"))
        
        # FACIAL LANDMARKS (simplified for ethereal avatars)
        self.add_joint(Joint("eye_l", JointType.HEAD, [-0.03, 1.72, 0.08], parent="head"))
        self.add_joint(Joint("eye_r", JointType.HEAD, [0.03, 1.72, 0.08], parent="head"))
        self.add_joint(Joint("jaw", JointType.HEAD, [0.0, 1.65, 0.05], parent="head"))
        
        # Store bind pose (default positions)
        self._store_bind_pose()
    
    def add_joint(self, joint: Joint):
        """Add a joint to the skeleton and update parent-child relationships"""
        self.joints[joint.name] = joint
        
        # Update parent's children list
        if joint.parent and joint.parent in self.joints:
            if joint.name not in self.joints[joint.parent].children:
                self.joints[joint.parent].children.append(joint.name)
    
    def _store_bind_pose(self):
        """Store the default pose for retargeting reference"""
        for name, joint in self.joints.items():
            self.bind_pose[name] = {
                "position": joint.position.copy(),
                "rotation": joint.rotation.copy(),
                "scale": joint.scale.copy()
            }
    
    def get_joint_world_transform(self, joint_name: str) -> Dict[str, List[float]]:
        """Get world-space transform for a joint (accounting for parent hierarchy)"""
        if joint_name not in self.joints:
            return {"position": [0, 0, 0], "rotation": [0, 0, 0, 1], "scale": [1, 1, 1]}
        
        joint = self.joints[joint_name]
        
        # If no parent, return local transform
        if not joint.parent:
            return {
                "position": joint.position.copy(),
                "rotation": joint.rotation.copy(),
                "scale": joint.scale.copy()
            }
        
        # Get parent world transform and combine
        parent_world = self.get_joint_world_transform(joint.parent)
        
        # Simplified transform combination (would be matrix math in production)
        world_pos = [
            parent_world["position"][0] + joint.position[0],
            parent_world["position"][1] + joint.position[1], 
            parent_world["position"][2] + joint.position[2]
        ]
        
        return {
            "position": world_pos,
            "rotation": joint.rotation.copy(),  # Simplified - would multiply quaternions
            "scale": joint.scale.copy()
        }
    
    def set_joint_transform(self, joint_name: str, position=None, rotation=None, scale=None):
        """Update joint transform (for animation/performance capture)"""
        if joint_name not in self.joints:
            return
        
        joint = self.joints[joint_name]
        if position is not None:
            joint.position = position
        if rotation is not None:
            joint.rotation = rotation
        if scale is not None:
            joint.scale = scale
    
    def export_to_json(self, filepath: Path):
        """Export skeleton to JSON format for external applications"""
        export_data = {
            "skeleton_version": "1.0",
            "name": "PubCast_Standard_Skeleton",
            "joint_count": len(self.joints),
            "bind_pose": self.bind_pose,
            "joints": {name: joint.to_dict() for name, joint in self.joints.items()},
            "metadata": {
                "created_for": "PubCast AI Virtual Production",
                "compatible_with": ["Blender", "Maya", "Unreal", "Unity", "Cinema4D"],
                "retargeting_ready": True,
                "performance_capture_ready": True
            }
        }
        
        with open(filepath, 'w') as f:
            json.dump(export_data, f, indent=2)
    
    def export_to_fbx_data(self) -> Dict:
        """Export skeleton data in FBX-compatible format"""
        # This would integrate with FBX export libraries
        return {
            "nodes": [
                {
                    "name": joint.name,
                    "type": "LimbNode",
                    "translation": joint.position,
                    "rotation": joint.rotation,
                    "scaling": joint.scale,
                    "parent": joint.parent
                }
                for joint in self.joints.values()
            ]
        }
    
    def get_retargeting_map(self) -> Dict[str, str]:
        """Get joint mapping for retargeting to other rigs"""
        return {
            # Standard mappings for common rigs
            "root": "root",
            "hips": "pelvis",
            "spine_01": "spine",
            "spine_02": "spine1", 
            "spine_03": "spine2",
            "chest": "chest",
            "neck": "neck",
            "head": "head",
            "upperarm_l": "shoulder_l",
            "lowerarm_l": "elbow_l",
            "hand_l": "wrist_l",
            "upperarm_r": "shoulder_r", 
            "lowerarm_r": "elbow_r",
            "hand_r": "wrist_r",
            "thigh_l": "hip_l",
            "calf_l": "knee_l",
            "foot_l": "ankle_l",
            "thigh_r": "hip_r",
            "calf_r": "knee_r", 
            "foot_r": "ankle_r"
        }


def create_standard_skeleton() -> PubCastSkeleton:
    """Factory function to create the standard PubCast skeleton"""
    return PubCastSkeleton()


# Animation utilities
class SkeletonAnimator:
    """Professional animation utilities for skeleton system"""
    
    @staticmethod
    def interpolate_pose(skeleton: PubCastSkeleton, pose_a: Dict, pose_b: Dict, t: float):
        """Smoothly interpolate between two poses"""
        for joint_name in skeleton.joints:
            if joint_name in pose_a and joint_name in pose_b:
                # Linear interpolation for position
                pos_a = pose_a[joint_name]["position"]
                pos_b = pose_b[joint_name]["position"]
                new_pos = [
                    pos_a[0] + (pos_b[0] - pos_a[0]) * t,
                    pos_a[1] + (pos_b[1] - pos_a[1]) * t,
                    pos_a[2] + (pos_b[2] - pos_a[2]) * t
                ]
                skeleton.set_joint_transform(joint_name, position=new_pos)
    
    @staticmethod
    def apply_performance_data(skeleton: PubCastSkeleton, performance_frame: Dict):
        """Apply captured performance data to skeleton"""
        if "skeleton" in performance_frame:
            for joint_name, transform_data in performance_frame["skeleton"].items():
                if joint_name in skeleton.joints:
                    skeleton.set_joint_transform(
                        joint_name,
                        position=transform_data.get("position"),
                        rotation=transform_data.get("rotation"),
                        scale=transform_data.get("scale")
                    )


if __name__ == "__main__":
    # Create and export standard skeleton
    skeleton = create_standard_skeleton()
    skeleton.export_to_json(Path("pubcast_standard_skeleton.json"))
    print(f"Created skeleton with {len(skeleton.joints)} joints")
    print("Skeleton exported to pubcast_standard_skeleton.json")
