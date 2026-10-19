import math
import time

import bpy
from mathutils import Quaternion, Vector

# 横滚限制（度）：手机横滚太大时限制相机滚转，避免画面上下颠倒
#   75° = 接近"侧飞"的角度；想更自由就调到 89，想更稳就 45
PLANE_MAX_ROLL_DEG: float = 75.0

# 是否启用横滚限制。
# ⚠️ 暂时关闭：用户要求 plane 的旋转**用回 free_mode 的原始逻辑**
#    （camera.rotation_quaternion = _initial_camera_rotation @ rotation，不做限制）
#    需要限制时改 True 即可恢复。
PLANE_CLAMP_ROLL: bool = False

# Plane Mode 摄像机初始旋转
_initial_camera_rotation = None

# 上次消息时间（用于计算 dt）
_last_message_time: float = 0.0
# 最近一次消息间隔（_move_forward 也要用）
_last_dt: float = 0.0
# 平滑后的移动方向（飞行轨迹防抖）
_smoothed_direction: Vector = Vector((0.0, 0.0, 0.0))

# 理想位置（累加原始步进）；相机实际位置向它平滑逼近 → 轨迹平滑
_target_location = None

#: LinkButton 切到 unlink 时保存的相机旋转（relink 时当新基准）
_saved_camera_rotation = None

# Plane Mode 摄像机移动速度倍数
PLANE_MOVE_SPEED_MULTIPLIER: float = 5.0

# ── 飞行轨迹防抖 ──
# ⚠️ 只平滑"位移方向"，**不平滑、也不限制相机旋转**（相机可以自由转）
# 相机自由转、飞机沿平滑后的方向飞 → 飞行曲线稳、画面也跟手
PLANE_PATH_SMOOTH_TAU: float = 0.12     # 越大轨迹越直；0 = 关闭


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

    # 用平滑后的方向 → 飞行轨迹更稳
    if _smoothed_direction.length > 1e-6:
        forward = _smoothed_direction
    else:
        forward = camera.rotation_quaternion @ Vector((0.0, 0.0, -1.0))

    global _target_location

    if _target_location is None:
        _target_location = camera.location.copy()

    _target_location = _target_location + (forward * distance)

    # ── 轨迹防抖：相机位置平滑逼近理想位置（路径更顺滑）──
    dt = _last_dt

    if PLANE_PATH_SMOOTH_TAU > 0.0 and dt > 1e-5:
        k = 1.0 - math.exp(-dt / PLANE_PATH_SMOOTH_TAU)
        camera.location = camera.location.lerp(_target_location, k)
    else:
        camera.location = _target_location.copy()


def handle_message(message):
    global _initial_camera_rotation, _saved_camera_rotation
    global _last_message_time, _smoothed_direction, _last_dt
    global PLANE_PATH_SMOOTH_TAU

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Plane] Invalid data")
        return
    
    # 兼容单独发送的 forward 消息（现在前进量已合并进姿态消息里）
    if data.get("command") == "forward":
        _move_forward(float(data.get("distance", 0.1)))
        return

    # 0.1 LinkButton 切到 unlink：记下相机当前旋转
    if data.get("command") == "unlink":
        camera = bpy.context.scene.camera

        if camera is None:
            print("[Blecaco][Plane] No active camera")
            return

        camera.rotation_mode = 'QUATERNION'
        _saved_camera_rotation = camera.rotation_quaternion.copy()

        print(
            "[Blecaco][Plane] Unlink: saved camera rotation = "
            f"X={_saved_camera_rotation.x:.4f}, "
            f"Y={_saved_camera_rotation.y:.4f}, "
            f"Z={_saved_camera_rotation.z:.4f}, "
            f"W={_saved_camera_rotation.w:.4f}"
        )
        return

    # 0.2 LinkButton 切回 link：用保存的相机旋转当新的初始基准
    if data.get("command") == "relink":
        camera = bpy.context.scene.camera

        if camera is None:
            print("[Blecaco][Plane] No active camera")
            return

        camera.rotation_mode = 'QUATERNION'

        if _saved_camera_rotation is None:
            _initial_camera_rotation = camera.rotation_quaternion.copy()
            print(
                "[Blecaco][Plane] Relink: no saved rotation, "
                "using current camera rotation as baseline"
            )
            return

        # ⚠️ 不要覆盖相机当前旋转 ✗
        #    用户明确不需要"旋转位置自动校正"：
        #    预览页里拖动时间轴/播放会把相机移到别处，若这里硬拉回
        #    unlink 那一刻的姿态，就会看到"自动校正初始化"的跳变 ✗
        #    只把**当前**姿态作为手机新基准（手机端 relink 时会重新校准 ✓）
        _initial_camera_rotation = camera.rotation_quaternion.copy()

        print(
            "[Blecaco][Plane] Relink: camera rotation restored to saved baseline"
        )
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

        _last_message_time = time.monotonic()
        _smoothed_direction = Vector((0.0, 0.0, 0.0))

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
    target = _initial_camera_rotation @ rotation

    # 横滚限制（默认关闭 → 完全等同 free_mode 的旋转逻辑）
    if PLANE_CLAMP_ROLL:
        target = _clamp_roll(target, PLANE_MAX_ROLL_DEG)

    # 相机旋转：完全自由（不平滑、不限位）
    camera.rotation_quaternion = target

    # ── 飞行轨迹防抖：只平滑"位移方向"，不影响相机旋转 ──
    now = time.monotonic()
    dt = now - _last_message_time
    _last_message_time = now
    dt = min(max(dt, 0.0), 0.1)
    _last_dt = dt

    direction = target @ Vector((0.0, 0.0, -1.0))

    if PLANE_PATH_SMOOTH_TAU > 0.0 and dt > 1e-5:
        k = 1.0 - math.exp(-dt / PLANE_PATH_SMOOTH_TAU)

        if _smoothed_direction.length < 1e-6:
            _smoothed_direction = direction.copy()
        else:
            blended = _smoothed_direction + (direction - _smoothed_direction) * k

            if blended.length > 1e-6:
                _smoothed_direction = blended.normalized()
    else:
        _smoothed_direction = direction.copy()

    # 手机传来的轨迹防抖时间常数（StableButton 四档）
    stable_tau = data.get("stable_tau")

    if isinstance(stable_tau, (int, float)) and stable_tau >= 0.0:
        PLANE_PATH_SMOOTH_TAU = float(min(stable_tau, 2.0))

    # 同一条消息里带来的前进量（plane_mode.gd 合并发送）。
    # 放在旋转之后：本帧的移动方向与刚设定的朝向一致。
    forward = float(data.get("forward", 0.0))

    if forward != 0.0:
        _move_forward(forward)


## 把相机姿态的横滚限制在 ±max_deg 以内（视线方向保持不变）
def _clamp_roll(target: Quaternion, max_deg: float) -> Quaternion:
    forward = target @ Vector((0.0, 0.0, -1.0))

    # 接近垂直时不处理（to_track_quat 会退化）
    if abs(forward.z) >= 0.999:
        return target

    # 无滚转的参考姿态
    level = forward.to_track_quat('-Z', 'Y')

    # 从 level 到 target 的旋转 = 横滚量
    difference = level.rotation_difference(target)

    axis = difference.axis.copy()
    angle = difference.angle

    # 让轴与视线同向，角度才有稳定符号
    if axis.dot(forward) < 0.0:
        axis = -axis
        angle = -angle

    limit = math.radians(max_deg)

    if abs(angle) <= limit:
        return target

    clamped = max(-limit, min(limit, angle))

    return Quaternion(forward, clamped) @ level
