"""
Camera Management System for PubCast AI

Professional camera source management supporting NDI, RTMP, SRT, virtual cameras,
and future integration with Rust rendering core for 3D voxel cameras.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class CameraTransport(str, Enum):
    """Delivery mechanism for a camera feed."""
    NDI = "ndi"
    RTMP = "rtmp"
    SRT = "srt"
    FILE = "file"
    VIRTUAL = "virtual"
    VOXEL_3D = "voxel_3d"  # For future Rust renderer integration


@dataclass
class CameraSource:
    """Describes a physical or virtual camera feed that can be recorded."""
    
    source_id: str
    name: str
    location: str
    description: str
    transport: CameraTransport
    endpoint: str
    audio_channels: int = 2
    video_enabled: bool = True
    audio_enabled: bool = True
    tags: List[str] = field(default_factory=list)
    is_private: bool = False
    
    # 3D positioning for voxel cameras (future Rust integration)
    position: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    fov: float = 60.0


@dataclass
class CameraStatus:
    """Represents the health of a camera feed."""
    
    source_id: str
    online: bool
    last_check: float = field(default_factory=time.time)
    latency_ms: Optional[float] = None
    frame_rate: Optional[float] = None
    notes: Optional[str] = None


class CameraManager:
    """
    Keeps track of all camera feeds and their latest health information.
    Designed for integration with Rust rendering core for high-performance 3D cameras.
    """

    def __init__(self) -> None:
        self._sources: Dict[str, CameraSource] = {}
        self._status: Dict[str, CameraStatus] = {}
        
        # Program/Preview switching (professional broadcast workflow)
        self._program_source: Optional[str] = None
        self._preview_source: Optional[str] = None

    # Registration and lookup
    def register(self, source: CameraSource) -> None:
        """Register a camera source."""
        self._sources[source.source_id] = source
        if source.source_id not in self._status:
            self._status[source.source_id] = CameraStatus(
                source_id=source.source_id,
                online=True,
                notes="Registered",
            )

    def remove(self, source_id: str) -> None:
        """Remove a camera source."""
        self._sources.pop(source_id, None)
        self._status.pop(source_id, None)

    def list_sources(self) -> List[CameraSource]:
        """Get all registered camera sources."""
        return list(self._sources.values())

    def get(self, source_id: str) -> Optional[CameraSource]:
        """Get specific camera source by ID."""
        return self._sources.get(source_id)

    # Program/Preview switching
    def set_program_source(self, source_id: str) -> bool:
        """Set the main program output source."""
        if source_id not in self._sources:
            return False
        self._program_source = source_id
        return True

    def set_preview_source(self, source_id: str) -> bool:
        """Set the preview monitor source."""
        if source_id not in self._sources:
            return False
        self._preview_source = source_id
        return True

    def get_program_source(self) -> Optional[CameraSource]:
        """Get current program source."""
        if self._program_source:
            return self._sources.get(self._program_source)
        return None

    def get_preview_source(self) -> Optional[CameraSource]:
        """Get current preview source."""  
        if self._preview_source:
            return self._sources.get(self._preview_source)
        return None

    # Status management
    def set_status(
        self,
        source_id: str,
        *,
        online: Optional[bool] = None,
        latency_ms: Optional[float] = None,
        frame_rate: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Update camera status metrics."""
        if source_id not in self._sources:
            raise KeyError(f"Unknown camera '{source_id}'")
            
        entry = self._status.setdefault(
            source_id,
            CameraStatus(source_id=source_id, online=True),
        )
        
        if online is not None:
            entry.online = online
        if latency_ms is not None:
            entry.latency_ms = latency_ms
        if frame_rate is not None:
            entry.frame_rate = frame_rate
        if notes is not None:
            entry.notes = notes
        entry.last_check = time.time()

    def status(self, source_id: str) -> Optional[CameraStatus]:
        """Get status for specific camera."""
        return self._status.get(source_id)

    def list_status(self) -> List[CameraStatus]:
        """Get status for all cameras."""
        return list(self._status.values())


# Factory function for default camera setup
def create_default_cameras() -> CameraManager:
    """Create camera manager with default virtual camera setup."""
    
    manager = CameraManager()
    
    # Virtual cameras for immediate use
    manager.register(
        CameraSource(
            source_id="wide_shot",
            name="Wide Shot",
            description="Wide angle view of the scene",
            location="virtual",
            transport=CameraTransport.VIRTUAL,
            endpoint="virtual://wide_shot",
            position=[0.0, 2.0, 5.0],
            rotation=[0.0, 0.0, 0.0],
            fov=75.0,
            tags=["virtual", "wide", "establishing"]
        )
    )
    
    manager.register(
        CameraSource(
            source_id="medium_shot", 
            name="Medium Shot",
            description="Medium framing for dialogue",
            location="virtual",
            transport=CameraTransport.VIRTUAL,
            endpoint="virtual://medium_shot",
            position=[2.0, 1.5, 3.0],
            rotation=[0.0, -15.0, 0.0],
            fov=50.0,
            tags=["virtual", "medium", "dialogue"]
        )
    )
    
    manager.register(
        CameraSource(
            source_id="close_up",
            name="Close Up",
            description="Close framing for emotion",
            location="virtual", 
            transport=CameraTransport.VIRTUAL,
            endpoint="virtual://close_up",
            position=[1.0, 1.0, 1.5],
            rotation=[0.0, 0.0, 0.0],
            fov=35.0,
            tags=["virtual", "close", "emotion"]
        )
    )
    
    manager.register(
        CameraSource(
            source_id="overhead",
            name="Overhead Shot", 
            description="Top-down view",
            location="virtual",
            transport=CameraTransport.VIRTUAL,
            endpoint="virtual://overhead",
            position=[0.0, 5.0, 0.0],
            rotation=[90.0, 0.0, 0.0],
            fov=90.0,
            tags=["virtual", "overhead", "establishing"]
        )
    )
    
    # Physical camera examples (NDI sources)
    manager.register(
        CameraSource(
            source_id="cam1_ndi",
            name="Camera 1 NDI",
            description="Physical NDI camera #1",
            location="studio",
            transport=CameraTransport.NDI,
            endpoint="ndi://camera-1",
            tags=["physical", "ndi", "studio"]
        )
    )
    
    manager.register(
        CameraSource(
            source_id="cam2_rtmp",
            name="Camera 2 RTMP", 
            description="RTMP stream camera #2",
            location="remote",
            transport=CameraTransport.RTMP,
            endpoint="rtmp://streaming-server/cam2",
            tags=["physical", "rtmp", "remote"]
        )
    )
    
    # Set default program/preview
    manager.set_program_source("wide_shot")
    manager.set_preview_source("medium_shot")
    
    return manager
