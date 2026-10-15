import bpy
from mathutils import Quaternion, Vector

# Car Mode 摄像机初始旋转
_initial_camera_rotation = None

# Car Mode 摄像机移动速度倍数
CAR_MOVE_SPEED_MULTIPLIER: float = 5.0


def _move_forward(raw_distance: float) -> None:
    """沿相机自身局部 -Z 方向前进。

    只修改 location，不写 matrix_world，
    避免位置与旋转数据互相覆盖。
    """
    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Car] No active camera")
        return

    distance = raw_distance * CAR_MOVE_SPEED_MULTIPLIER

    camera.location = camera.location + (
        camera.rotation_quaternion @ Vector((0.0, 0.0, -distance))
    )


def handle_message(message):
    global _initial_camera_rotation

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Car] Invalid data")
        return

    # 兼容单独发送的 forward 消息
    if data.get("command") == "forward":
        _move_forward(float(data.get("distance", 0.1)))
        return

    # 初始化：记录摄像机初始旋转
    if data.get("command") == "initialize":
        camera = bpy.context.scene.camera

        if camera is None:
            print("[Blecaco][Car] No active camera")
            return

        camera.rotation_mode = 'QUATERNION'
        _initial_camera_rotation = camera.rotation_quaternion.copy()

        print("[Blecaco][Car] Initial camera rotation captured")
        return

    # 接收手机四元数
    try:
        x = float(data["x"])
        y = float(data["y"])
        z = float(data["z"])
        w = float(data["w"])

    except (KeyError, TypeError, ValueError):
        print("[Blecaco][Car] Invalid quaternion data")
        return

    # 防止零四元数
    if (w * w + x * x + y * y + z * z) < 1e-16:
        print("[Blecaco][Car] Invalid zero quaternion")
        return

    rotation = Quaternion((w, x, y, z))
    rotation.normalize()

    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Car] No active camera")
        return

    camera.rotation_mode = 'QUATERNION'

    # 兼容尚未收到初始化指令的情况
    if _initial_camera_rotation is None:
        _initial_camera_rotation = camera.rotation_quaternion.copy()

    # 应用手机姿态
    camera.rotation_quaternion = (
        _initial_camera_rotation @ rotation
    )

    # 处理同一条姿态消息中的前进量
    forward = float(data.get("forward", 0.0))

    if forward != 0.0:
        _move_forward(forward)