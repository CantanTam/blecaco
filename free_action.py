import bpy
from mathutils import Quaternion


# 进入 Free Mode 时记录的摄像机初始旋转
_initial_camera_rotation = None


def handle_message(message):
    global _initial_camera_rotation

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Free] Invalid quaternion data")
        return

    # 收到初始化指令：记录当前活动摄像机的旋转
    if data.get("command") == "initialize":
        camera = bpy.context.scene.camera

        if camera is None:
            print("[Blecaco][Free] No active camera")
            return

        camera.rotation_mode = 'QUATERNION'
        _initial_camera_rotation = camera.rotation_quaternion.copy()

        print("[Blecaco][Free] Initial camera rotation captured")
        return

    # 读取 Godot 发送的相对旋转四元数
    try:
        x = float(data["x"])
        y = float(data["y"])
        z = float(data["z"])
        w = float(data["w"])
    except (KeyError, TypeError, ValueError):
        print("[Blecaco][Free] Missing or invalid quaternion values")
        return

    # 避免无效的零四元数
    if (w * w + x * x + y * y + z * z) < 1e-16:
        print("[Blecaco][Free] Invalid zero quaternion")
        return

    rotation = Quaternion((w, x, y, z))
    rotation.normalize()

    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Free] No active camera in current scene")
        return

    camera.rotation_mode = 'QUATERNION'

    # 兼容尚未收到初始化指令的情况
    if _initial_camera_rotation is None:
        _initial_camera_rotation = camera.rotation_quaternion.copy()

    # 在摄像机初始姿态的基础上叠加手机相对旋转
    camera.rotation_quaternion = (
        _initial_camera_rotation @ rotation
    )

    print(
        "[Blecaco][Free] Camera rotation updated: "
        f"X={camera.rotation_quaternion.x:.4f}, "
        f"Y={camera.rotation_quaternion.y:.4f}, "
        f"Z={camera.rotation_quaternion.z:.4f}, "
        f"W={camera.rotation_quaternion.w:.4f}"
    )