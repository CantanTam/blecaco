import math
import time

import bpy
from mathutils import Quaternion, Vector

# Car Mode 摄像机初始旋转
_initial_camera_rotation = None

# ── 方向盘转向参数 ──
# 转向角 θ = 手机（已转换为相机坐标）的 X 轴与水平面的夹角
#
# 旋转轴：**相机自身 Y 轴**（相机的"上方向"）
#   → 转向跟随相机自身的姿态：以后加"地面追踪"（相机随地形倾斜）时，
#     转向轴也会跟着地形的法线走 ✓（绕世界 Z 会跟地形脱节 ✗）
#   （后乘 = 在相机自身坐标系里转）
STEER_IN_WORLD: bool = False
# 转向模式：
#   "rate"     = 角速度控制（打方向盘 → 持续旋转 → 可掉头）★推荐
#   "absolute" = 角度控制（方向盘角度 = 相机偏转角，转到底就停）
STEER_MODE: str = "rate"
# 满舵（= STEER_MAX_DEG）时的偏航角速度（度/秒）
#   60°/s → 打满舵约 3 秒转 180°（掉头）
#   90°/s → 2 秒掉头（更灵敏）
STEER_YAW_RATE_DEG_PER_SEC: float = 60.0
# 舵量力度曲线（>1 = 中心更细腻，转向更"稳"）
#   1.6 → 半舵(22.5°)只有 0.33 力度，偏钝
#   1.3 → 半舵 0.41，更接近直觉（推荐）
STEER_EXPO: float = 1.3
# 舵量平滑（秒）：抑制传感器抖动造成的转向毛刺
STEER_SMOOTH_TAU: float = 0.06
# 速度联动：车辆达到该速度（米/秒）时转向效率才满
STEER_FULL_SPEED: float = 8.0
# 速度联动开关
STEER_SPEED_LINK: bool = True
# 静止时的转向效率下限（0.0 = 停车完全不转向 ← 之前就是这个导致"打方向盘不动" ✗）
#   0.4 = 停车时也能以 40% 效率转向（推荐，方便取景/测试）
#   想完全模拟真车（停车不转）→ 设 0.0
STEER_MIN_SPEED_FACTOR: float = 0.4
# 相机自身 Y 轴（上方向）——配合 STEER_IN_WORLD = False
STEER_AXIS: tuple = (0.0, 1.0, 0.0)
STEER_SIGN: float = 1.0
STEER_GAIN: float = 1.0
STEER_MAX_DEG: float = 45.0
# 死区（度）：小于此角度完全不转向（抗手抖，防止画面缓慢自转）
STEER_DEADZONE_DEG: float = 3.0
# 迟滞系数：进入死区用 STEER_DEADZONE_DEG，退出用 DEADZONE × 此值
#   （3.0° 进入 / 2.4° 退出 → 边缘不会来回抖）
STEER_DEADZONE_EXIT_RATIO: float = 0.8
# True：只响应方向盘（不再跟随手机偏航/俯仰）→ 符合"简化转向"
STEER_IGNORE_PHONE_TILT: bool = True

# 当前转向角（弧度）
_steer_angle: float = 0.0

# 累计偏航角（弧度）：rate 模式下持续累加 → 支持掉头
_car_yaw: float = 0.0

# 平滑后的舵量（-1..1）
_steer_lock: float = 0.0

# 是否处于死区内（用于迟滞）
_steer_in_deadzone: bool = True
_last_message_time: float = 0.0

# 调试：每秒打印一次转向角
STEER_DEBUG: bool = False
_steer_debug_time: float = 0.0

print(
    "[Blecaco][Car] 方向盘模式已加载：旋转轴 =",
    STEER_AXIS,
    "（(0,0,1)=局部Z / (0,1,0)=局部Y）",
)

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
    global _initial_camera_rotation, _steer_angle
    global _car_yaw, _last_message_time, _steer_lock
    global _steer_in_deadzone

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
        _car_yaw = 0.0
        _steer_lock = 0.0
        _steer_in_deadzone = True
        _last_message_time = time.monotonic()

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

    # ── 方向盘：由"相机 X 轴与水平面的夹角"决定转向角 ──
    # 手机四元数已在 getrotation.gd 里转换成相机坐标
    # （X = -phone.y / Y = phone.x / Z = phone.z），所以这里直接乘就能得到相机姿态。
    phone_frame = _initial_camera_rotation @ rotation

    x_axis = phone_frame @ Vector((1.0, 0.0, 0.0))
    angle_deg = math.degrees(
        math.asin(max(-1.0, min(1.0, x_axis.z)))
    )

    # 死区（带迟滞）：进入用 STEER_DEADZONE_DEG，退出用 × RATIO
    deadzone_limit = (
        STEER_DEADZONE_DEG
        if _steer_in_deadzone
        else STEER_DEADZONE_DEG * STEER_DEADZONE_EXIT_RATIO
    )

    if abs(angle_deg) <= deadzone_limit:
        angle_deg = 0.0
        _steer_in_deadzone = True
    else:
        _steer_in_deadzone = False

    angle_deg = max(-STEER_MAX_DEG, min(STEER_MAX_DEG, angle_deg))
    angle_deg *= STEER_GAIN

    _steer_angle = math.radians(angle_deg)

    steer_axis = Vector(STEER_AXIS).normalized()
    steer = Quaternion(
        steer_axis,
        STEER_SIGN * _steer_angle,
    )

    # 前进量（转向的速度联动要用）
    forward = float(data.get("forward", 0.0))

    # 方向盘灵敏度倍率（手机端三档：0.6 / 1.0 / 1.6）
    try:
        steer_scale = float(data.get("steer_scale", 1.0))
    except (TypeError, ValueError):
        steer_scale = 1.0

    steer_scale = max(0.0, min(5.0, steer_scale))

    if STEER_IGNORE_PHONE_TILT:
        base = _initial_camera_rotation
    else:
        # 旧行为：完整跟随手机姿态
        base = phone_frame

    now = time.monotonic()
    dt = now - _last_message_time
    _last_message_time = now
    dt = min(max(dt, 0.0), 0.1)     # 防止卡顿造成大跳

    if STEER_MODE == "rate":
        # ── 角速度模式：方向盘角度 → 偏航角速度，持续累加 → 可掉头 ──
        # ① 归一化舵量（-1 左满舵 ~ +1 右满舵）
        lock_raw = _steer_angle / math.radians(STEER_MAX_DEG)
        lock_raw = max(-1.0, min(1.0, lock_raw))

        # ② 力度曲线（保留符号）
        lock_target = math.copysign(
            abs(lock_raw) ** STEER_EXPO,
            lock_raw,
        ) if lock_raw != 0.0 else 0.0

        # ③ 平滑（抑制抖动）：与帧率无关的指数逼近
        k = 1.0 - math.exp(-dt / max(STEER_SMOOTH_TAU, 1e-4))

        if abs(lock_target - _steer_lock) < 1e-5:
            _steer_lock = lock_target
        else:
            _steer_lock += (lock_target - _steer_lock) * k

        # ④ 速度联动：车速越快转向越有效；停车时不转向
        if STEER_SPEED_LINK and dt > 1e-4:
            speed_mps = abs(forward) / dt * CAR_MOVE_SPEED_MULTIPLIER
            speed_factor = max(
                STEER_MIN_SPEED_FACTOR,
                min(1.0, speed_mps / max(STEER_FULL_SPEED, 1e-3)),
            )
        else:
            speed_factor = 1.0

        _car_yaw += (
            math.radians(STEER_YAW_RATE_DEG_PER_SEC)
            * _steer_lock
            * speed_factor
            * steer_scale
            * dt
        )

        # 用累计角构造转向
        steer = Quaternion(
            steer_axis,
            STEER_SIGN * _car_yaw,
        )
    else:
        # ── 角度模式：方向盘角度 = 相机偏转角（转到底就停）──
        pass

    if STEER_DEBUG:
        import time as _time
        global _steer_debug_time

        now = _time.monotonic()

        if now - _steer_debug_time >= 1.0:
            _steer_debug_time = now
            print(
                "[Blecaco][Car] 转向角 = %.1f°  轴 = %s"
                % (
                    math.degrees(_steer_angle),
                    tuple(round(v, 2) for v in steer_axis),
                )
            )

    if STEER_IN_WORLD:
        # 前乘 = 在世界坐标系里绕世界轴转
        target = steer @ base
    else:
        # 后乘 = 绕相机自身 Y 轴（上方向）转 → 跟随相机姿态 ✓
        target = base @ steer

    camera.rotation_quaternion = target

    # 处理同一条姿态消息中的前进量
    if forward != 0.0:
        _move_forward(forward)