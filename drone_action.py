"""Drone Mode：接收手机双摇杆的归一化向量，驱动活动摄像机。

手机端（drone_mode.gd）发送：
    {"type":"action","mode":"drone","data":{"command":"initialize"}}
    {"type":"action","mode":"drone","data":{
        "command":"stick",
        "lx":-1..1, "ly":-1..1,      # 左摇杆：平移
        "rx":-1..1, "ry":-1..1,      # 右摇杆：转向
        "dt":0.0333                  # 本条消息代表的时间步长（秒）
    }}

坐标约定（Godot 屏幕坐标 → 本模块）：
    +x 向右、+y 向下
    所以 上推 ly=-1、下推 ly=+1、右推 rx=+1、左推 rx=-1

手感调参都在下面的常量里（改这里不用动手机端）：
    - 死区、指数曲线
    - 平移速度、偏航/俯仰角速度
"""

import math
import time

import bpy
from mathutils import Matrix, Quaternion, Vector

# ── 摇杆响应曲线 ──
# 死区：小于该值的输入视为 0（避免手指微抖导致漂移）
STICK_DEADZONE: float = 0.08
# 指数：>1 时中心附近更细腻、边缘更灵敏（1.0 = 线性）
STICK_EXPO: float = 1.6

# ── 摇杆驱动摄像机本体的参数（不再使用空物体）──
# 左杆左右：绕**世界 Z 轴**水平转向（度/秒）
RIG_YAW_RATE: float = 90.0
# 左杆上下：沿世界 Z 轴升降（米/秒）
RIG_LIFT_SPEED: float = 2.0
# 右杆左右：沿自身局部 X 轴移动（米/秒）
RIG_STRAFE_SPEED: float = 2.0
# 右杆上下：沿自身局部 Y 轴移动（米/秒）
RIG_LONG_SPEED: float = 2.0

# 右杆"上推"是否走 +Y（False 则上推走 -Y）
RIGHT_STICK_UP_POSITIVE_Y: bool = True

# ── 速度档位（由手机端按摇杆距离分档，随颜色变化）──
# 档位切换时的加速度 / 减速度（倍率/秒）：避免速度突变
SPEED_TIER_ACCEL: float = 3.0
SPEED_TIER_DECEL: float = 4.0
# 档位是否同时缩放"偏航角速度"。
# 按用户要求：档位只影响**移动速度**，不影响绕局部 Z 的旋转 → False
SCALE_YAW_WITH_TIER: bool = False

# ── 双指倾斜（TiltArea）：绕活动摄像机自身局部 X 轴旋转 ──
TILT_RATE: float = 90.0          # 度/秒（满速）
# 倾斜极限：视线不能越过"正上/正下"（|forward.z| 不能到 1，否则姿态退化）
# 俯仰上限（度）：禁止视线完全垂直（89° → |forward.z| ≈ 0.99985）
TILT_MAX_ELEVATION_DEG: float = 89.0
TILT_FORWARD_Z_LIMIT: float = math.sin(math.radians(TILT_MAX_ELEVATION_DEG))
# 上滑是抬头(True) 还是低头(False) —— 若手感反了改这里
TILT_UP_IS_PITCH_UP: bool = True

# "打平/朝下" 两个一次性动作的**平滑过渡**（指数缓出）
TILT_SNAP_TAU: float = 0.20          # 时间常数（秒），越小越快（0.10→0.20 = 慢一倍）
TILT_SNAP_INTERVAL: float = 0.016    # 过渡定时器间隔（≈60Hz）
TILT_SNAP_EPS_DEG: float = 0.2       # 剩余角度小于它就直接收尾

# 单条消息允许的最大时间步长（防止卡顿后瞬移）
MAX_DELTA: float = 0.1

# 调试：每秒打印一次"最近收到什么 / 物体变成什么"（定位问题后改成 False）
DEBUG_STATE: bool = True


def _shape(value) -> float:
    """把摇杆原始值 (-1..1) 变成手感曲线后的值：死区 → 归一化 → 指数。"""
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.0

    raw = max(-1.0, min(1.0, raw))

    if abs(raw) <= STICK_DEADZONE:
        return 0.0

    sign = -1.0 if raw < 0.0 else 1.0
    scaled = (abs(raw) - STICK_DEADZONE) / (1.0 - STICK_DEADZONE)

    return sign * (scaled ** STICK_EXPO)


# 无人机支架空物体的名字


def _get_camera():
    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Drone] 当前场景没有活动摄像机")
        return None

    camera.rotation_mode = 'QUATERNION'

    return camera


def handle_message(message):
    """handler.py 传进来的是**整条消息** {type, mode, data}，这里取出内层 data。"""
    if not isinstance(message, dict):
        print("[Blecaco][Drone] 消息不是字典，已忽略")
        return

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Drone] data 不是字典，已忽略")
        return

    command = data.get("command")

    if command is None:
        print("[Blecaco][Drone] 缺少 command 字段，已忽略：", data)
        return

    # 进入 Drone 控制：现在直接操作摄像机本体，不再创建空物体
    if command == "enable":
        camera = _get_camera()

        if camera is not None:
            camera.rotation_mode = 'QUATERNION'

        print("[Blecaco][Drone] 收到 enable（直接控制摄像机，无空物体）")
        return

    # 退出 Drone 控制：没有空物体需要清理
    if command == "disable":
        print("[Blecaco][Drone] 收到 disable（无空物体需要清理）")
        return

    # 进入 Drone Mode 时的初始化：只确保四元数模式
    if command == "initialize":
        camera = _get_camera()

        if camera is not None:
            print(
                "[Blecaco][Drone] 已初始化：摄像机=%s（四元数模式）"
                % camera.name
            )

        return

    # 双指倾斜 / 归水平 / 朝下 三个旋转命令
    if command in ("tilt", "tilt_level", "tilt_down"):
        _handle_tilt(command, data)
        return

    if command != "stick":
        return

    global _speed_scale_l, _speed_scale_r

    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Drone] 没有活动摄像机")
        return

    camera.rotation_mode = 'QUATERNION'

    dt = _clamp_dt(data)

    if dt <= 0.0:
        return

    _debug_accumulate(data, dt)

    lx = _shape(data.get("lx", 0.0))
    ly = _shape(data.get("ly", 0.0))
    rx = _shape(data.get("rx", 0.0))
    ry = _shape(data.get("ry", 0.0))

    # 档位倍率 → 平滑加减速逼近
    scale_l = _ramp_speed_scale(
        _speed_scale_l,
        _speed_target(data.get("speed_l", 1.0)),
        dt,
    )
    scale_r = _ramp_speed_scale(
        _speed_scale_r,
        _speed_target(data.get("speed_r", 1.0)),
        dt,
    )

    _speed_scale_l = scale_l
    _speed_scale_r = scale_r

    # 方向单位化：档位决定"总速度"，方向决定各轴分量的比例
    left_dir = Vector((lx, ly))
    left_len = left_dir.length
    right_dir = Vector((rx, ry))
    right_len = right_dir.length

    # ① 左杆左右 → 绕**世界 Z 轴**水平转向（左推 = 左转）
    try:
        yaw_sensitivity = float(data.get("yaw_scale", 1.0))
    except (TypeError, ValueError):
        yaw_sensitivity = 1.0

    yaw_sensitivity = max(0.0, min(5.0, yaw_sensitivity))

    if lx != 0.0:
        yaw_scale = scale_l if SCALE_YAW_WITH_TIER else 1.0
        yaw = RIG_YAW_RATE * dt * (-lx) * yaw_scale * yaw_sensitivity
        camera.rotation_quaternion = (
            Quaternion((0.0, 0.0, 1.0), math.radians(yaw))
            @ camera.rotation_quaternion
        )

    # ② 左杆上下 → 沿世界 Z 轴升降
    lift_dir = (-ly / left_len) if left_len > 1e-5 else 0.0
    lift = RIG_LIFT_SPEED * dt * lift_dir * scale_l

    # ③ 右杆左右 → 沿 X`（摄像机 X 轴在水平面的投影）
    # ④ 右杆上下 → 沿 Y`（摄像机 -Z 轴在水平面的投影）
    strafe_dir = (rx / right_len) if right_len > 1e-5 else 0.0
    long_dir = (-ry / right_len) if right_len > 1e-5 else 0.0

    if not RIGHT_STICK_UP_POSITIVE_Y:
        long_dir = -long_dir

    strafe = RIG_STRAFE_SPEED * dt * strafe_dir * scale_r
    longitudinal = RIG_LONG_SPEED * dt * long_dir * scale_r

    # 计算水平投影轴
    quat = camera.rotation_quaternion
    camera_x = quat @ Vector((1.0, 0.0, 0.0))
    camera_forward = quat @ Vector((0.0, 0.0, -1.0))

    x_axis_flat = Vector((camera_x.x, camera_x.y, 0.0))
    y_axis_flat = Vector((camera_forward.x, camera_forward.y, 0.0))

    # 视线接近垂直时的兜底（正常有 ±89° 限制，几乎不会触发）
    if x_axis_flat.length < 1e-5:
        x_axis_flat = Vector((1.0, 0.0, 0.0))
    else:
        x_axis_flat.normalize()

    if y_axis_flat.length < 1e-5:
        y_axis_flat = Vector((0.0, 1.0, 0.0))
    else:
        y_axis_flat.normalize()

    step_world = (
        x_axis_flat * strafe
        + y_axis_flat * longitudinal
        + Vector((0.0, 0.0, lift))
    )

    if step_world.length > 0.0:
        camera.location = camera.location + step_world


# ============================================================
# 辅助函数
# ============================================================

_rig_warned: bool = False

# 当前速度倍率（左右各一个，平滑逼近目标档位）
_speed_scale_l: float = 1.0
_speed_scale_r: float = 1.0

# ── 调试统计（每秒打印一次）──
_dbg_time: float = 0.0
_dbg_count: int = 0
_dbg_sum_lx: float = 0.0
_dbg_sum_ly: float = 0.0
_dbg_sum_rx: float = 0.0
_dbg_sum_ry: float = 0.0
_dbg_tilt_count: int = 0
_dbg_tilt_sum: float = 0.0
_dbg_level_count: int = 0
_dbg_down_count: int = 0


def _debug_accumulate(data, dt: float) -> None:
    """累积最近 1 秒的输入统计，并在满 1 秒时打印一次。"""
    global _dbg_time, _dbg_count, _dbg_sum_lx, _dbg_sum_ly
    global _dbg_sum_rx, _dbg_sum_ry, _dbg_tilt_count, _dbg_tilt_sum
    global _dbg_level_count, _dbg_down_count

    if not DEBUG_STATE:
        return

    command = data.get("command")
    _dbg_time += dt

    if command == "stick":
        _dbg_count += 1
        _dbg_sum_lx += float(data.get("lx", 0.0))
        _dbg_sum_ly += float(data.get("ly", 0.0))
        _dbg_sum_rx += float(data.get("rx", 0.0))
        _dbg_sum_ry += float(data.get("ry", 0.0))
    elif command == "tilt":
        _dbg_tilt_count += 1
        _dbg_tilt_sum += float(data.get("amount", 0.0))
    elif command == "tilt_level":
        _dbg_level_count += 1
    elif command == "tilt_down":
        _dbg_down_count += 1

    if _dbg_time < 1.0:
        return

    camera = bpy.context.scene.camera if bpy.context.scene else None

    if camera is not None:
        euler = camera.rotation_quaternion.to_euler('XYZ')
        rig_info = "cam loc=(%.2f,%.2f,%.2f) rotZ=%.1f°" % (
            camera.location.x, camera.location.y, camera.location.z,
            math.degrees(euler.z),
        )
    else:
        rig_info = "cam=无"

    cam_info = "cam 仰角=%.1f°" % (
        _camera_y_elevation_deg(camera) if camera is not None else 0.0
    )

    n = max(1, _dbg_count)
    print(
        "[Drone][1s] stick×%d 均值 lx=%+.2f ly=%+.2f rx=%+.2f ry=%+.2f | "
        "tilt×%d(均值 %+.2f) level×%d down×%d | %s %s"
        % (
            _dbg_count, _dbg_sum_lx / n, _dbg_sum_ly / n,
            _dbg_sum_rx / n, _dbg_sum_ry / n,
            _dbg_tilt_count,
            _dbg_tilt_sum / max(1, _dbg_tilt_count),
            _dbg_level_count, _dbg_down_count,
            rig_info, cam_info,
        )
    )

    _dbg_time = 0.0
    _dbg_count = 0
    _dbg_sum_lx = _dbg_sum_ly = _dbg_sum_rx = _dbg_sum_ry = 0.0
    _dbg_tilt_count = 0
    _dbg_tilt_sum = 0.0
    _dbg_level_count = 0
    _dbg_down_count = 0


def _speed_target(value) -> float:
    """手机传来的档位倍率（非法值回退 1.0）。"""
    try:
        target = float(value)
    except (TypeError, ValueError):
        return 1.0

    # 允许 addon_set 的「移动倍数」放大（0.01~100），这里只做安全上限
    return max(0.0, min(500.0, target))


def _ramp_speed_scale(current: float, target: float, dt: float) -> float:
    """把当前倍率向目标倍率平滑逼近：加速用 ACCEL、减速用 DECEL。"""
    if abs(target - current) < 1e-6:
        return target

    rate = SPEED_TIER_ACCEL if target > current else SPEED_TIER_DECEL
    step = rate * dt

    if abs(target - current) <= step:
        return target

    return current + math.copysign(step, target - current)


def _clamp_dt(data) -> float:
    """取出并夹紧时间步长。"""
    try:
        dt = float(data.get("dt", 0.033))
    except (TypeError, ValueError):
        dt = 0.033

    return max(0.0, min(MAX_DELTA, dt))


def _camera_axes(camera):
    """返回摄像机自身坐标轴在世界空间的方向 (x, y, z)。"""
    rotation = camera.rotation_quaternion

    return (
        rotation @ Vector((1.0, 0.0, 0.0)),
        rotation @ Vector((0.0, 1.0, 0.0)),
        rotation @ Vector((0.0, 0.0, 1.0)),
    )


def _camera_y_elevation_deg(camera) -> float:
    """摄像机 Y 轴的仰角（度）：0 = 水平，±90 = 竖直。"""
    _, up_axis, _ = _camera_axes(camera)
    value = max(-1.0, min(1.0, up_axis.z))

    return math.degrees(math.asin(value))


def _rotate_about_local_x(camera, angle_deg: float) -> None:
    camera.rotation_quaternion = (
        camera.rotation_quaternion
        @ Quaternion((1.0, 0.0, 0.0), math.radians(angle_deg))
    )


def _shortest_local_x_angle(camera, target_dir: Vector) -> float:
    """求：绕摄像机自身局部 X 轴旋转，让某个当前方向转到 target_dir 所需的角度（度）。"""
    _, _, forward_z = _camera_axes(camera)
    # 当前"待转方向"由调用方通过 target 与 current 传入；这里统一用 forward(-Z) 的镜像计算
    return 0.0


def _forward_z_after(camera, angle_deg: float) -> float:
    """旋转 angle_deg 后，视线方向的**世界** Z 分量（±1 表示正上/正下）。"""
    return _world_forward_after(camera, angle_deg).z


def _forward_horizontal(camera, angle_deg: float) -> Vector:
    """旋转 angle_deg 后，视线方向在水平面上的投影（z 归零）。"""
    forward = _world_forward_after(camera, angle_deg)

    return Vector((forward.x, forward.y, 0.0))


def _clamp_pitch_angle(camera, angle_deg: float) -> float:
    """把绕自身 X 的角度夹到"视线不越过正上/正下"的范围内。

    两个约束：
      1. |forward.z| 不能接近 1（不能正好竖直，姿态会退化）
      2. 一步之内不能**越过**竖直 —— 用"视线水平分量的方向是否翻转"判断：
         越过顶点时，水平投影会反向
    """
    if angle_deg == 0.0:
        return 0.0

    base_h = _forward_horizontal(camera, 0.0)

    def allowed(angle: float) -> bool:
        if abs(_forward_z_after(camera, angle)) > TILT_FORWARD_Z_LIMIT:
            return False

        # 水平投影反向 = 越过竖直
        if base_h.length > 1e-5:
            end_h = _forward_horizontal(camera, angle)

            if end_h.dot(base_h) < 0.0:
                return False

        return True

    if allowed(angle_deg):
        return angle_deg

    lo = 0.0
    hi = angle_deg

    for _ in range(24):
        mid = (lo + hi) * 0.5

        if allowed(mid):
            lo = mid
        else:
            hi = mid

    return lo


def _handle_tilt(command: str, data) -> None:
    camera = _get_camera()

    if camera is None:
        return

    _debug_accumulate(data, _clamp_dt(data))

    if command == "tilt":
        # ⚠️ 不再取消进行中的一次性过渡：需求是"一转到底"
        dt = _clamp_dt(data)
        amount = _shape(data.get("amount", 0.0))

        if dt <= 0.0 or amount == 0.0:
            return

        # 上滑(amount>0) = 正度数；下滑 = 负度数
        angle = TILT_RATE * dt * amount

        if not TILT_UP_IS_PITCH_UP:
            angle = -angle

        # 极限：视线不能越过"正上/正下"。
        # 用二分把角度夹到刚好到达极限处，避免"整步放弃"造成的卡顿感。
        angle = _clamp_pitch_angle(camera, angle)

        if abs(angle) > 1e-6:
            _rotate_about_local_x(camera, angle)

        return

    if command == "tilt_level":
        # 一上一下 → 视线转到 摄像机自身局部 +Y（"打平"方向）
        _start_tilt_snap("vertical")
        return

    if command == "tilt_down":
        # 一左一右 → 视线转到 摄像机自身局部 -Z
        _start_tilt_snap("horizontal")
        return


# ============================================================
# "打平 / 朝下" 的平滑过渡（用 bpy 定时器逐帧旋转，指数缓出）
# ============================================================

_snap_kind = None
_snap_last_time = 0.0


def _camera_frame_quaternion(camera):
    """摄像机父级的世界旋转（无父级时为单位四元数）。

    ⚠️ 相机的 rotation_quaternion 是**相对父级**的，所以做"世界方向"比较时
    必须先乘上父级的世界旋转 —— 否则挂到父级空物体下之后全部算错。
    """
    bpy.context.view_layer.update()

    if camera.parent is not None:
        return camera.parent.matrix_world.to_quaternion()

    return Quaternion()


def _camera_forward(camera) -> Vector:
    """摄像机视线在世界空间的朝向。"""
    frame = _camera_frame_quaternion(camera)

    return frame @ (camera.rotation_quaternion @ Vector((0.0, 0.0, -1.0)))


def _camera_x_axis_world(camera) -> Vector:
    frame = _camera_frame_quaternion(camera)

    return frame @ (camera.rotation_quaternion @ Vector((1.0, 0.0, 0.0)))


def _world_forward_after(camera, angle_deg: float) -> Vector:
    """绕自身局部 X 旋转 angle_deg 后，视线在世界空间的方向。"""
    frame = _camera_frame_quaternion(camera)
    local = camera.rotation_quaternion @ Quaternion(
        (1.0, 0.0, 0.0),
        math.radians(angle_deg),
    )

    return frame @ (local @ Vector((0.0, 0.0, -1.0)))


def _camera_forward_local(camera) -> Vector:
    _, _, z_axis = _camera_axes(camera)

    return -z_axis


def _target_direction(camera, kind: str) -> Vector:
    """目标视线方向（世界坐标）。

    保持原来 DRONE_EMPTY 版本的**语义**（那个空物体只做偏航，从不俯仰/横滚）：
      "horizontal"（一左一右）：世界 **-Z**（朝下看）
      "vertical"  （一上一下）：摄像机当前的**水平朝向**（把视线打平）
    """
    if kind == "horizontal":
        return Vector((0.0, 0.0, -1.0))

    camera.rotation_mode = 'QUATERNION'
    forward = camera.rotation_quaternion @ Vector((0.0, 0.0, -1.0))
    flat = Vector((forward.x, forward.y, 0.0))

    if flat.length > 1e-5:
        return flat.normalized()

    # 视线接近垂直时退回相机自身 +Y（此时"打平"没有唯一答案）
    return (camera.rotation_quaternion @ Vector((0.0, 1.0, 0.0))).normalized()


def _local_x_angle_to(camera, target: Vector) -> float:
    """绕相机自身 X 轴，把视线转到 target（世界方向）所需的带符号角度（度）。"""
    x_axis = _camera_x_axis_world(camera)
    current = _camera_forward(camera)

    cur_p = current - x_axis * current.dot(x_axis)
    tgt_p = target - x_axis * target.dot(x_axis)

    if cur_p.length < 1e-6 or tgt_p.length < 1e-6:
        return 0.0

    angle = math.degrees(cur_p.angle(tgt_p))
    sign = 1.0 if cur_p.cross(tgt_p).dot(x_axis) > 0.0 else -1.0

    return angle * sign


def _cancel_tilt_snap() -> None:
    global _snap_kind

    if _snap_kind is not None:
        print("[Blecaco][Drone] 过渡被手动倾斜打断（%s）" % _snap_kind)
        _snap_kind = None


def _start_tilt_snap(kind: str) -> None:
    global _snap_kind, _snap_last_time

    _snap_kind = kind
    _snap_last_time = time.monotonic()

    if not bpy.app.timers.is_registered(_snap_tick):
        bpy.app.timers.register(
            _snap_tick,
            first_interval=TILT_SNAP_INTERVAL,
            persistent=True,
        )

    print(
        "[Blecaco][Drone] 开始平滑过渡：%s（时间常数 %.2fs）"
        % (kind, TILT_SNAP_TAU)
    )


def _snap_tick():
    """过渡定时器：逐帧把视线转向目标；返回 None 表示结束。"""
    global _snap_kind, _snap_last_time

    if _snap_kind is None:
        return None

    now = time.monotonic()
    dt = max(1e-4, min(0.1, now - _snap_last_time))
    _snap_last_time = now

    scene = bpy.context.scene
    camera = scene.camera if scene is not None else None

    if camera is None:
        _snap_kind = None
        return None

    if camera.rotation_mode != 'QUATERNION':
        camera.rotation_mode = 'QUATERNION'

    target = _target_direction(camera, _snap_kind)
    remaining = _local_x_angle_to(camera, target)

    if abs(remaining) <= TILT_SNAP_EPS_DEG:
        if abs(remaining) > 1e-6:
            _rotate_about_local_x(camera, remaining)

        print("[Blecaco][Drone] 过渡完成（%s）" % _snap_kind)
        _snap_kind = None
        return None

    # 指数缓出：每帧走 remaining 的一个比例 → 起步快、收尾慢
    step = remaining * (1.0 - math.exp(-dt / TILT_SNAP_TAU))
    _rotate_about_local_x(camera, step)

    return TILT_SNAP_INTERVAL
