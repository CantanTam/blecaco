import bpy
from mathutils import Quaternion


# Plane Mode 摄像机初始旋转
_initial_camera_rotation = None


def handle_message(message):
    global _initial_camera_rotation

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Plane] Invalid data")
        return

    # 1. 初始化：记录摄像机初始旋转
    if data.get("command") == "initialize":
        camera = bpy.context.scene.camera

        if camera is None:
            print("[Blecaco][Plane] No active camera")
            return

        camera.rotation_mode = 'QUATERNION'

        _initial_camera_rotation = (
            camera.rotation_quaternion.copy()
        )

        print(
            "[Blecaco][Plane] Initial camera rotation captured"
        )
        return

    # 2. 接收手机四元数
    try:
        x = float(data["x"])
        y = float(data["y"])
        z = float(data["z"])
        w = float(data["w"])

    except (KeyError, TypeError, ValueError):
        print("[Blecaco][Plane] Invalid quaternion data")
        return

    # 防止零四元数
    if (w * w + x * x + y * y + z * z) < 1e-16:
        print("[Blecaco][Plane] Invalid zero quaternion")
        return

    rotation = Quaternion((w, x, y, z))
    rotation.normalize()

    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Plane] No active camera")
        return

    camera.rotation_mode = 'QUATERNION'

    # 兼容尚未收到初始化指令的情况
    if _initial_camera_rotation is None:
        _initial_camera_rotation = (
            camera.rotation_quaternion.copy()
        )

    # 保留现有 Free Mode 验证过的旋转公式
    camera.rotation_quaternion = (
        _initial_camera_rotation @ rotation
    )