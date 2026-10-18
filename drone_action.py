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

# ── 摇杆驱动 DRONE_EMPTY（无人机本体）的参数 ──
# 左杆左右：绕 DRONE_EMPTY 自身局部 Z 轴旋转（度/秒）
RIG_YAW_RATE: float = 90.0
# 左杆上下：沿 DRONE_EMPTY 自身局部 Z 轴移动（米/秒）
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
TILT_FORWARD_Z_LIMIT: float = 0.9995
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
DRONE_EMPTY_NAME: str = "DRONE_EMPTY"


def _make_empty():
    """创建 PLAIN_AXES 空物体并命名为 DRONE_EMPTY。

    先用 bpy.ops.object.empty_add（与用户给的命令一致），
    若当前上下文不允许操作（例如非 OBJECT 模式 / 没有活动视图层），
    自动回退到数据 API（等价效果，不依赖上下文）。
    """
    # 方式一：操作器
    try:
        before = set(bpy.data.objects)
        bpy.ops.object.empty_add(type='PLAIN_AXES', location=(0, 0, 0))

        created = bpy.context.object

        if created is None or created.name not in bpy.data.objects:
            created = next(
                (obj for obj in bpy.data.objects if obj not in before),
                None,
            )

        if created is not None:
            created.name = DRONE_EMPTY_NAME
            created.empty_display_type = 'PLAIN_AXES'
            print("[Blecaco][Drone] DRONE_EMPTY 已创建（empty_add）")
            return created
    except Exception as exc:
        print("[Blecaco][Drone] empty_add 失败，改用数据 API：", exc)

    # 方式二：数据 API（不依赖上下文）
    try:
        empty = bpy.data.objects.new(DRONE_EMPTY_NAME, None)
        empty.empty_display_type = 'PLAIN_AXES'
        empty.location = (0.0, 0.0, 0.0)

        scene = bpy.context.scene
        collection = (
            scene.collection
            if scene is not None and scene.collection is not None
            else bpy.context.view_layer.active_layer_collection.collection
        )
        collection.objects.link(empty)
        print("[Blecaco][Drone] DRONE_EMPTY 已创建（数据 API）")
        return empty
    except Exception as exc:
        print("[Blecaco][Drone] 数据 API 也失败：", exc)
        return None


def _enable_drone_rig() -> None:
    """进入 Drone 控制：创建 DRONE_EMPTY 并把活动摄像机挂到它下面。

    等价于用户给的三条命令：
        empty_add(type='PLAIN_AXES', location=(0,0,0)); 命名为 DRONE_EMPTY
        DRONE_EMPTY.location = scene.camera.location.copy()
        cam.parent = empty; cam.matrix_parent_inverse = empty.matrix_world.inverted()

    ⚠️ 没有活动摄像机时什么都不做。
    ⚠️ 已有 DRONE_EMPTY 时直接复用（幂等，不会重复创建）。
    """
    scene = bpy.context.scene
    camera = scene.camera if scene is not None else None

    if camera is None:
        print("[Blecaco][Drone] 没有活动摄像机，跳过创建 DRONE_EMPTY")
        return

    empty = bpy.data.objects.get(DRONE_EMPTY_NAME)

    if empty is None:
        empty = _make_empty()

        if empty is None:
            print("[Blecaco][Drone] 创建 DRONE_EMPTY 失败")
            return

    # 先取摄像机"世界"位置（camera.location 是相对父级的局部值，不能用）
    bpy.context.view_layer.update()
    world = camera.matrix_world.copy()

    # 摄像机如果挂着旧父级，先解除（保持世界变换不变）
    if camera.parent is not None:
        camera.parent = None
        camera.matrix_world = world
        bpy.context.view_layer.update()
        print("[Blecaco][Drone] 摄像机原有父级已解除（保持世界变换）")

    # 空物体放到摄像机世界位置（清掉它自己可能残留的父级）
    empty.parent = None
    empty.matrix_world = Matrix.Translation(world.translation)

    # 挂父级：显式构造逆矩阵（Translation 的逆 = 平移取负），
    # 不读 empty.matrix_world —— 它可能还是刷新前的值（这正是之前"质心越飘越远"的原因）
    camera.parent = empty
    camera.matrix_parent_inverse = Matrix.Translation(-world.translation)

    bpy.context.view_layer.update()

    print(
        "[Blecaco][Drone] 已创建并绑定 %s（摄像机=%s）"
        % (DRONE_EMPTY_NAME, camera.name)
    )


def _disable_drone_rig() -> None:
    """退出 Drone 控制：删除 DRONE_EMPTY（存在才删）。"""
    empty = bpy.data.objects.get(DRONE_EMPTY_NAME)

    if empty is None:
        return

    # 子级的世界变换保持不变（删除父级前后位置不跳）
    bpy.context.view_layer.update()

    for child in list(empty.children):
        world = child.matrix_world.copy()
        child.parent = None
        child.matrix_world = world

    bpy.data.objects.remove(empty, do_unlink=True)
    print("[Blecaco][Drone] 已删除 %s" % DRONE_EMPTY_NAME)


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

    # 进入 Drone 控制：创建 DRONE_EMPTY 并挂载活动摄像机
    if command == "enable":
        print("[Blecaco][Drone] 收到 enable")

        _enable_drone_rig()
        return

    # 退出 Drone 控制：删除 DRONE_EMPTY
    if command == "disable":
        print("[Blecaco][Drone] 收到 disable")

        _disable_drone_rig()
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

    rig = bpy.data.objects.get(DRONE_EMPTY_NAME)

    if rig is None:
        _warn_missing_rig()
        return

    dt = _clamp_dt(data)

    if dt <= 0.0:
        return

    _debug_accumulate(data, dt)

    lx = _shape(data.get("lx", 0.0))
    ly = _shape(data.get("ly", 0.0))
    rx = _shape(data.get("rx", 0.0))
    ry = _shape(data.get("ry", 0.0))

    # 档位倍率（手机按摇杆距离分档发送）→ 平滑加减速逼近
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

    rotation = _rig_quaternion(rig)

    # 方向单位化：档位决定"总速度"，方向决定各轴分量的比例
    left_dir = Vector((lx, ly))
    left_len = left_dir.length
    right_dir = Vector((rx, ry))
    right_len = right_dir.length

    # ① 左杆左右 → 绕自身局部 Z 轴旋转（左推 = 左转）
    if lx != 0.0:
        yaw_scale = scale_l if SCALE_YAW_WITH_TIER else 1.0
        yaw = RIG_YAW_RATE * dt * (-lx) * yaw_scale
        rotation = rotation @ Quaternion(
            (0.0, 0.0, 1.0),
            math.radians(yaw),
        )
        _set_rig_quaternion(rig, rotation)

    # ② 左杆上下 → 沿自身局部 Z 轴上下移动（上推 = +Z）
    # ③ 右杆左右 → 沿自身局部 X 轴（右推 = +X）
    # ④ 右杆上下 → 沿自身局部 Y 轴（上推 = +Y，可用常量翻转）
    # 左杆：档位决定总速度，方向按单位向量分配
    lift_dir = (-ly / left_len) if left_len > 1e-5 else 0.0
    lift = RIG_LIFT_SPEED * dt * lift_dir * scale_l

    # 右杆同理
    strafe_dir = (rx / right_len) if right_len > 1e-5 else 0.0
    long_dir = (
        (-ry / right_len) if right_len > 1e-5 else 0.0
    )
    if not RIGHT_STICK_UP_POSITIVE_Y:
        long_dir = -long_dir

    strafe = RIG_STRAFE_SPEED * dt * strafe_dir * scale_r
    longitudinal = RIG_LONG_SPEED * dt * long_dir * scale_r

    if lift != 0.0 or strafe != 0.0 or longitudinal != 0.0:
        local_step = Vector((strafe, longitudinal, lift))
        rig.location = rig.location + (rotation @ local_step)


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

    rig = bpy.data.objects.get(DRONE_EMPTY_NAME)
    camera = bpy.context.scene.camera if bpy.context.scene else None

    if rig is not None:
        euler = _rig_quaternion(rig).to_euler('XYZ')
        rig_info = "rig loc=(%.2f,%.2f,%.2f) rotZ=%.1f°" % (
            rig.location.x, rig.location.y, rig.location.z,
            math.degrees(euler.z),
        )
    else:
        rig_info = "rig=无"

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


def _rig_quaternion(rig):
    """读取 DRONE_EMPTY 当前姿态（统一成四元数）。

    ⚠️ empty 默认是 Euler 旋转模式：直接写 rotation_quaternion 是**无效**的，
    必须按 rotation_mode 分别处理。
    """
    if rig.rotation_mode == 'QUATERNION':
        return rig.rotation_quaternion.copy()

    return rig.rotation_euler.to_quaternion()


def _set_rig_quaternion(rig, quat) -> None:
    """按对象当前的旋转模式写回姿态。"""
    if rig.rotation_mode == 'QUATERNION':
        rig.rotation_quaternion = quat
    else:
        rig.rotation_euler = quat.to_euler(rig.rotation_mode)


def _speed_target(value) -> float:
    """手机传来的档位倍率（非法值回退 1.0）。"""
    try:
        target = float(value)
    except (TypeError, ValueError):
        return 1.0

    return max(0.0, min(4.0, target))


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


def _warn_missing_rig() -> None:
    global _rig_warned

    if not _rig_warned:
        _rig_warned = True
        print(
            "[Blecaco][Drone] 没有 %s（请先打开 Drone 开关），摇杆数据已忽略"
            % DRONE_EMPTY_NAME
        )


# ============================================================
# 双指倾斜：绕活动摄像机自身局部 X 轴旋转
# ============================================================

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
        # 一上一下 → 视线转到 DRONE_EMPTY 自身局部 +Y（"打平"方向）
        _start_tilt_snap("vertical")
        return

    if command == "tilt_down":
        # 一左一右 → 视线转到 DRONE_EMPTY 自身局部 -Z
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
    必须先乘上父级的世界旋转 —— 否则挂到 DRONE_EMPTY 下之后全部算错。
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

    "horizontal"（一左一右）：DRONE_EMPTY 自身局部 **-Z**
    "vertical"  （一上一下）：DRONE_EMPTY 自身局部 **+Y**
    （没有 DRONE_EMPTY 时退回世界坐标系的 -Z / +Y）
    """
    rig = bpy.data.objects.get(DRONE_EMPTY_NAME)

    if kind == "horizontal":
        if rig is not None:
            return -(rig.matrix_world.to_quaternion() @ Vector((0.0, 0.0, 1.0)))

        return Vector((0.0, 0.0, -1.0))

    if rig is not None:
        return rig.matrix_world.to_quaternion() @ Vector((0.0, 1.0, 0.0))

    return Vector((0.0, 1.0, 0.0))


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
