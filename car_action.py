import json
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

# ── 地面追踪（Level 2：跟随地形起伏）─────────────────────
# 开启后：
#   1) 前进方向压平到水平面（低头不会钻地、抬头不会飞天）
#   2) 用射线向下探测地面，把相机抬到地面上方 EYE_HEIGHT
CAR_GROUND_FOLLOW: bool = True

# 相机离地面的高度（米）—— 相当于人眼/车载相机高度
CAR_EYE_HEIGHT: float = 1.7

# 向下探测地面的最大距离（米）—— 太短会检测不到高处的路，太长会穿到地下层
CAR_RAY_DISTANCE: float = 200.0

# 高度平滑时间常数（秒）—— 越大越平滑但越高低起伏迟钝；0 表示不平滑
CAR_HEIGHT_SMOOTH_TAU: float = 0.08

# 射线起点额外抬高，避免起点已经在地下时穿透
CAR_RAY_START_LIFT: float = 10.0

# 探测失败（比如走出地面范围）时：True=保持当前高度，False=回退到初始高度
CAR_KEEP_HEIGHT_ON_MISS: bool = True

# ── 姿态贴合地形（相机局部 Y 轴 ⟂ 地面）──────────────────
# 开启后：相机俯仰/横滚跟随地形法线，局部 Y 轴始终垂直地面
#   → 上坡时相机自动抬头、横穿斜坡时自动横滚 ✓
CAR_ALIGN_TO_GROUND: bool = True

# 姿态贴合速度（秒）：越大越平滑但越迟钝；0 = 立即贴合
CAR_ALIGN_SMOOTH_TAU: float = 0.15

# 只在低于此坡度时才贴合（度）；超过则忽略（防止在悬崖峭壁上把相机翻转 ✗）
CAR_ALIGN_MAX_SLOPE_DEG: float = 75.0

# 调试：打印地形法线与相机实际 Y 轴的夹角
CAR_ALIGN_DEBUG: bool = False

# 调试：打印地面探测结果
CAR_GROUND_DEBUG: bool = False

# 运行时状态：平滑后的相机高度
_ground_z: float | None = None


# 没有标记地面时使用的水平面高度（世界 Z）
# HAS_GROUND == false 且射线打不到物体时，用这个平面当地面 ✓
CAR_FLAT_PLANE_Z: float = 0.0

# 当前被标记为地面的物体（None = 还没标记）
_ground_object = None


def _find_tagged_ground():
    """在场景里找带 is_ground 自定义属性的物体。"""
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        if obj.get("is_ground", False):
            return obj
    return None


def _clear_ground_tag() -> None:
    """删掉所有物体的 is_ground 属性。"""
    global _ground_object

    count = 0
    for obj in bpy.data.objects:
        if obj.get("is_ground", False):
            try:
                del obj["is_ground"]
                count += 1
            except Exception as exc:              # noqa: BLE001
                print(f"[Blecaco][Car] 删除 is_ground 失败（{obj.name}）：{exc}")

    _ground_object = None
    print(f"[Blecaco][Car] 已清除地面标记（{count} 个物体）")


def _notify_ground_status(has_ground: bool) -> None:
    """把地面标记状态推给手机。"""
    from . import streamer as _streamer

    server = getattr(_streamer._state, "server", None)

    if server is None:
        print("[Blecaco][Car] 无法推送 ground_status（没有服务器）")
        return

    try:
        server.send_text(json.dumps({
            "type": "ground_status",
            "has_ground": bool(has_ground),
            "object": _ground_object.name if _ground_object else "",
            "plane_z": CAR_FLAT_PLANE_Z,
        }))
    except Exception as exc:                       # noqa: BLE001
        print(f"[Blecaco][Car] ground_status 发送失败：{exc}")


def _set_ground_by_ray() -> bool:
    """沿相机局部 -Y（相机下方）打射线，把第一个命中的物体标记为地面。

    返回是否成功标记。
    """
    global _ground_object

    _clear_ground_tag()

    scene = bpy.context.scene
    camera = scene.camera

    if camera is None:
        print("[Blecaco][Car] 没有活动相机，无法设置地面")
        return False

    # 相机局部 -Y = 相机的"下方"
    direction = camera.rotation_quaternion @ Vector((0.0, -1.0, 0.0))
    origin = camera.location.copy()

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        hit, loc, normal, idx, obj, matrix = scene.ray_cast(
            depsgraph, origin, direction, distance=CAR_RAY_DISTANCE
        )
    except Exception as exc:                       # noqa: BLE001
        print(f"[Blecaco][Car] 地面射线异常：{exc}")
        return False

    if not hit or obj is None:
        print("[Blecaco][Car] 相机下方没有物体，地面标记失败")
        return False

    # 打标
    obj["is_ground"] = True
    _ground_object = obj

    print(
        f"[Blecaco][Car] 地面已标记：{obj.name} "
        f"（命中高度 z={loc.z:.2f} 法线=({normal.x:.2f},{normal.y:.2f},{normal.z:.2f})）"
    )
    return True


def _raycast_ground(origin: Vector):
    """在「被标记为地面」的物体上打射线（只测那一个物体 ✓）。

    返回 (命中点高度, 地面法线) 或 (None, None)。
    """
    global _ground_object

    obj = _ground_object

    if obj is None:
        obj = _find_tagged_ground()
        _ground_object = obj

    if obj is None:
        # 没有标记物体 → 用水平面兜底（HAS_GROUND == false 的情况）
        if CAR_GROUND_DEBUG:
            print(f"[Blecaco][Car] 用水平面 z={CAR_FLAT_PLANE_Z}")
        return CAR_FLAT_PLANE_Z, Vector((0.0, 0.0, 1.0))

    if obj.name not in bpy.context.scene.objects:
        # 物体被删了 → 清空引用
        _ground_object = None
        return None, None

    ray_start = origin.copy()
    ray_start.z += CAR_RAY_START_LIFT

    try:
        inv = obj.matrix_world.inverted()
        local_origin = inv @ ray_start
        local_dir = (inv.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()

        hit, loc, normal, idx = obj.ray_cast(
            local_origin, local_dir, distance=CAR_RAY_DISTANCE
        )
    except Exception as exc:                       # noqa: BLE001
        print(f"[Blecaco][Car] 地面探测异常：{exc}")
        return None, None

    if not hit:
        if CAR_GROUND_DEBUG:
            print(f"[Blecaco][Car] 在 {obj.name} 上未命中")
        return None, None

    # 局部 → 世界
    world_loc = obj.matrix_world @ loc
    normal_matrix = obj.matrix_world.to_3x3().inverted().transposed()
    world_normal = (normal_matrix @ normal).normalized()

    # 法线可能朝下（背面），统一翻成朝上
    if world_normal.z < 0.0:
        world_normal = -world_normal

    if CAR_GROUND_DEBUG:
        print(
            f"[Blecaco][Car] 命中 {obj.name} z={world_loc.z:.3f} "
            f"法线=({world_normal.x:.2f},{world_normal.y:.2f},{world_normal.z:.2f})"
        )

    return float(world_loc.z), world_normal


def _raycast_ground_height(origin: Vector):
    """只要高度（兼容旧调用）。"""
    z, _normal = _raycast_ground(origin)
    return z


def _align_rotation_to_normal(camera, normal: Vector, delta: float) -> None:
    """让相机局部 Y 轴垂直于地面（跟随地形俯仰/横滚），保留朝向。

    做法：
      1) 把当前朝向(-Z)投影到地面平面 → 得到新的前方向
      2) 用 normal 作为上方向（局部 +Y）
      3) 叉乘得到右方向（局部 +X），重新正交化
      4) 构造旋转矩阵 → 转四元数
    地形太陡（超过 CAR_ALIGN_MAX_SLOPE_DEG）时不做处理，避免相机翻过去 ✗
    """
    q = camera.rotation_quaternion

    # 坡度太陡 → 不动
    slope_deg = math.degrees(math.acos(max(-1.0, min(1.0, normal.z))))
    if slope_deg > CAR_ALIGN_MAX_SLOPE_DEG:
        return

    # 当前前方（-Z）
    forward = q @ Vector((0.0, 0.0, -1.0))

    # 去掉法线分量，投影到地面平面
    forward = forward - normal * forward.dot(normal)

    if forward.length_squared < 1e-8:
        # 正对着地面 → 用世界方向兜底
        forward = Vector((0.0, 1.0, 0.0)) - normal * normal.y
        if forward.length_squared < 1e-8:
            return

    forward.normalize()

    # 右方向 = 前 × 上（Blender 右手系：X = Y × Z，Z = -forward）
    right = forward.cross(normal)
    if right.length_squared < 1e-8:
        return
    right.normalize()

    # 重新正交化前方向，保证严格正交
    forward = normal.cross(right).normalized()

    # 构造目标旋转：局部 X=right, Y=normal, Z=-forward
    from mathutils import Matrix
    mat = Matrix((
        (right.x, normal.x, -forward.x),
        (right.y, normal.y, -forward.y),
        (right.z, normal.z, -forward.z),
    ))
    target = mat.to_quaternion()

    # 平滑：和帧率无关的指数逼近
    if CAR_ALIGN_SMOOTH_TAU <= 0.0:
        camera.rotation_quaternion = target
    else:
        alpha = 1.0 - math.exp(-max(delta, 0.0) / CAR_ALIGN_SMOOTH_TAU)
        camera.rotation_quaternion = q.slerp(target, alpha)

    if CAR_ALIGN_DEBUG:
        now_y = camera.rotation_quaternion @ Vector((0.0, 1.0, 0.0))
        ang = math.degrees(now_y.angle(normal))
        print(f"[Blecaco][Car] 贴合地形：坡度={slope_deg:.1f}° Y轴与法线夹角={ang:.1f}°")


def _apply_ground_height(camera, new_location: Vector, delta: float) -> Vector:
    """把 new_location 的 z 调整到地面 + EYE_HEIGHT（带平滑），
    并按需要让相机姿态贴合地形法线。"""
    global _ground_z

    ground, normal = _raycast_ground(new_location)

    if ground is None:
        if CAR_KEEP_HEIGHT_ON_MISS:
            new_location.z = camera.location.z
        return new_location

    target_z = ground + CAR_EYE_HEIGHT

    # 平滑：避免台阶/噪声导致高度突跳
    if CAR_HEIGHT_SMOOTH_TAU <= 0.0 or _ground_z is None:
        _ground_z = target_z
    else:
        alpha = 1.0 - math.exp(-max(delta, 0.0) / CAR_HEIGHT_SMOOTH_TAU)
        _ground_z = _ground_z + (target_z - _ground_z) * alpha

    new_location.z = _ground_z

    # 姿态贴合地形
    if CAR_ALIGN_TO_GROUND and normal is not None:
        _align_rotation_to_normal(camera, normal, delta)

    return new_location


def _move_forward(raw_distance: float) -> None:
    """前进。

    地面追踪关闭时：沿相机自身局部 -Z 方向自由移动（旧行为）。
    地面追踪开启时：
      1) 方向压平到水平面（低头不钻地、抬头不飞天）
      2) 射线探测地面，把相机高度贴到地面上方 EYE_HEIGHT

    只修改 location，不写 matrix_world，
    避免位置与旋转数据互相覆盖。
    """
    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Car] No active camera")
        return

    distance = raw_distance * CAR_MOVE_SPEED_MULTIPLIER

    # 相机朝向（-Z）→ 世界方向
    forward = camera.rotation_quaternion @ Vector((0.0, 0.0, -1.0))

    if CAR_GROUND_FOLLOW:
        # 压平到水平面
        forward.z = 0.0

        # 正对着地面看时水平分量为零，用一个兜底方向避免卡死
        if forward.length_squared < 1e-10:
            forward = Vector((0.0, 1.0, 0.0))

        forward.normalize()

    new_location = camera.location + forward * distance

    if CAR_GROUND_FOLLOW:
        # 用移动距离粗略估计时间（速度恒定时距离正比于时间）
        speed = max(abs(CAR_MOVE_SPEED_MULTIPLIER), 1e-3)
        delta = abs(distance) / speed
        new_location = _apply_ground_height(camera, new_location, delta)

    camera.location = new_location


def handle_message(message):
    global _initial_camera_rotation, _steer_angle
    global _car_yaw, _last_message_time, _steer_lock
    global _steer_in_deadzone

    # ground 消息是扁平结构（{"type":"ground","enabled":true}）
    # action 消息才是嵌套的（{"type":"action","data":{...}}）
    if message.get("type") == "ground":
        data = message
    else:
        data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Car] Invalid data")
        return

    # 兼容单独发送的 forward 消息
    if data.get("command") == "forward":
        _move_forward(float(data.get("distance", 0.1)))
        return

    # 地面标记开关：手机长按 SetGround 切换
    # 支持两种消息形态：
    #   {"type": "ground", "enabled": true}              ← 手机当前用法
    #   {"command": "set_ground", "enabled": true}        ← 兼容旧写法
    if message.get("type") == "ground" or data.get("command") == "set_ground":
        enabled = bool(data.get("enabled", False))

        if enabled:
            # 沿相机局部 -Y 打射线
            #   命中物体 → 打标，HAS_GROUND = true
            #   没命中   → 不打标，HAS_GROUND = false（用水平面兜底）
            has_ground = _set_ground_by_ray()
        else:
            _clear_ground_tag()
            has_ground = False

        # 把最终结果回传手机（手机据此更新 HAS_GROUND 与图标）
        _notify_ground_status(has_ground)
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