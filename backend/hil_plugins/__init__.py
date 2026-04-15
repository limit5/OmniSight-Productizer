"""C7 — HIL plugin family implementations (#216)."""

from backend.hil_plugins.camera import CameraHILPlugin
from backend.hil_plugins.audio import AudioHILPlugin
from backend.hil_plugins.display import DisplayHILPlugin

__all__ = ["CameraHILPlugin", "AudioHILPlugin", "DisplayHILPlugin"]
