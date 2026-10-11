import bpy
from mathutils import Quaternion


def handle_message(message):
    # handler.py 传入的是完整的 JSON 消息
    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Free] Invalid quaternion data")
        return

    # 读取 Godot 发送的四元数分量
    try:
        x = float(data["x"])
        y = float(data["y"])
        z = float(data["z"])
        w = float(data["w"])
    except (KeyError, TypeError, ValueError):
        print("[Blecaco][Free] Missing or invalid quaternion values")
        return

    # Blender Quaternion 的构造顺序是 W、X、Y、Z

    # 避免无效的零四元数
    if (w * w + x * x + y * y + z * z) < 1e-16:
        print("[Blecaco][Free] Invalid zero quaternion")
        return

    rotation = Quaternion((w, x, y, z))
    rotation.normalize()

    # 获取当前场景的活动摄像机
    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Free] No active camera in current scene")
        return

    # 将活动摄像机切换为四元数旋转模式
    camera.rotation_mode = 'QUATERNION'

    # 直接应用 Godot 发送的转换后四元数
    camera.rotation_quaternion = rotation

    print(
        "[Blecaco][Free] Camera rotation updated: "
        f"X={rotation.x:.4f}, "
        f"Y={rotation.y:.4f}, "
        f"Z={rotation.z:.4f}, "
        f"W={rotation.w:.4f}"
    )