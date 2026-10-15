import bpy
from mathutils import Quaternion, Vector

# Plane Mode 摄像机初始旋转
_initial_camera_rotation = None

# Plane Mode 摄像机移动速度倍数
PLANE_MOVE_SPEED_MULTIPLIER: float = 5.0


def _move_forward(raw_distance: float) -> None:
    """沿相机自身局部 -Z 方向前进。

    只改 location，绝不写 matrix_world：
    matrix_world 是聚合值，写它会把 location / rotation / scale 一起重写，
    而 rotation 由姿态消息流专门负责，两者互相覆盖会让相机旋转来回跳。
    （相机朝向 = 局部 -Z；rotation_quaternion 是最新的旋转值。）
    """
    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Plane] No active camera")
        return

    distance = raw_distance * PLANE_MOVE_SPEED_MULTIPLIER

    camera.location = camera.location + (
        camera.rotation_quaternion @ Vector((0.0, 0.0, -distance))
    )


def handle_message(message):
    global _initial_camera_rotation

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Plane] Invalid data")
        return
    
    # 兼容单独发送的 forward 消息（现在前进量已合并进姿态消息里）
    if data.get("command") == "forward":
        _move_forward(float(data.get("distance", 0.1)))
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

    # 同一条消息里带来的前进量（plane_mode.gd 合并发送）。
    # 放在旋转之后：本帧的移动方向与刚设定的朝向一致。
    forward = float(data.get("forward", 0.0))

    if forward != 0.0:
        _move_forward(forward)