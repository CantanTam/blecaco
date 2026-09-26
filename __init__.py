import bpy
import os
import sys

_addon_dir = os.path.dirname(os.path.abspath(__file__))
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

bl_info = {
    "name": "Blecaco",
    "author": "Canta Tam",
    "version": (0, 1, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Blecaco",
    "description": "把 Blender 视口通过 WebSocket + JPEG 推到浏览器/Godot",
    "category": "3D View",
    "support": "COMMUNITY",
}

from .addon_property import (
    BLECACO_SceneProperties,
    register_properties,
    unregister_properties,
)
from .streamer import (
    register as streamer_register,
    unregister as streamer_unregister,
)
from .ui import (
    BLECACO_OT_start,
    BLECACO_OT_stop,
    BLECACO_PT_main_panel,
    unregister_ui,
)


_classes = (
    BLECACO_OT_start,
    BLECACO_OT_stop,
    BLECACO_PT_main_panel,
)


def register():
    register_properties()
    for cls in _classes:
        bpy.utils.register_class(cls)
    streamer_register()


def unregister():
    streamer_unregister()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    unregister_properties()
    unregister_ui()
