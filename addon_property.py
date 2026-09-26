import bpy


class BLECACO_SceneProperties(bpy.types.PropertyGroup):
    port: bpy.props.IntProperty(name="端口", default=12121, min=1024, max=65535)
    fps: bpy.props.IntProperty(name="帧率", default=10, min=1, max=30)
    quality: bpy.props.IntProperty(name="JPEG 质量", default=70, min=10, max=100)


def register_properties():
    bpy.utils.register_class(BLECACO_SceneProperties)
    bpy.types.Scene.blecaco = bpy.props.PointerProperty(type=BLECACO_SceneProperties)


def unregister_properties():
    if hasattr(bpy.types.Scene, "blecaco"):
        del bpy.types.Scene.blecaco
    bpy.utils.unregister_class(BLECACO_SceneProperties)
