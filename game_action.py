"""Game Action —— FPS 风格的平面移动（Quake / Source movement 模型）。

对应手机端 game_mode.tscn 的 MoveStickArea / MoveButton 摇杆。

移动方向定义（用户设定）：
    MoveButton 在区域中心【上方】 → 沿 Z' 正方向（Z' = 相机 -Z 在水平面的投影）
    MoveButton 在区域中心【下方】 → 沿 Z' 负方向
    MoveButton 在区域中心【左方】 → 沿相机局部 X 轴负方向
    MoveButton 在区域中心【右方】 → 沿相机局部 X 轴正方向

手感来自两个函数（Quake 的 PM_Accelerate / PM_Friction）：
    ① 摩擦：没输入时速度按指数衰减（约 0.2 秒停住）
    ② 加速：加速量受当前速度限制（约 0.1~0.15 秒到满速）
    → 快而不瞬时，像"有质量的人" ✓

参考：
    Quake 3      max=320ups accel=10 friction=6
    CS:GO        max=250ups accel=5.5 friction=5.2
    Apex         max=300ups accel=9  friction=6
"""

from __future__ import annotations

import json
import math
import time

import bpy
from mathutils import Quaternion, Vector

# ── game mode 的"贴地"标记（与 car_mode 的 is_ground 分开 ✗）──
GAME_GROUND_TAG: str = "is_game_ground"

# 被标记为游戏地面的物体
_game_ground_object = None

# 参考距离：相机到命中点的 Z 方向距离，代表 GAME_BODY_HEIGHT_M（1.8 米）✓
_ground_reference_distance: float = 0.0

# 是否已确定有效参考（False = 用兜底 GAME_MAX_SPEED）
_reference_valid: bool = False

# 抖动平滑后的振幅（0 = 不抖）
_shake_amp: float = 0.0
_shake_time: float = 0.0

# 上一帧施加的抖动偏移（必须先撤销，否则横向会累积漂移 ✗）
_shake_prev: Vector = Vector((0.0, 0.0, 0.0))


# ── 手感参数（参考 CS:GO / Apex）───────────────────────────
# 最大移动速度（单位/秒）
# 1.8 米身高对应的【米/秒】（主流 FPS：CS:GO 250ups≈4.76m/s，COD≈4.2m/s）
GAME_WALK_MPS: float = 4.5

# 参考身高（米）—— 用来把"实测相机高度"换算成米
GAME_BODY_HEIGHT_M: float = 1.8

# 兜底：没有有效参考距离时用的绝对速度（单位/秒）
GAME_MAX_SPEED: float = 6.0

# 加速度系数（越大起步越快；10 ≈ 0.1 秒到满速）
GAME_ACCEL: float = 10.0

# 摩擦系数（越大停得越快）
#   6 → 尾段太慢（0.5s）✗  8 → 约 0.3 秒停住 ✓ 更 snappy
GAME_FRICTION: float = 8.0

# 低速时的兜底摩擦速度（避免速度趋近 0 时永远停不下来）
GAME_STOP_SPEED: float = 0.5

# 冲刺时的加速度比例（Apex 约 80% ✓ 起步"沉"一下更有重量感 ✓）
GAME_SPRINT_ACCEL_RATIO: float = 0.8

# ── 视角旋转（FPS 式）───────────────────────────────
# 直接映射：每滑动 1 像素转多少度（三角洲 / 主流手机 FPS 手感 ✓）
#   横屏约 2400px 宽 → 0.15°/px × 2400 = 360°（一屏刚好转一圈 ✓）
# 业界主流（PUBG / 三角洲 / Apex 默认档）：
#   滑满一屏宽 1920px × 0.12 = 230°
#   滑满一屏高 1080px × 0.09 =  97°
GAME_LOOK_YAW_DEG_PER_PX: float = 0.12
GAME_LOOK_PITCH_DEG_PER_PX: float = 0.09
# 俯仰上限（±89° ✓）
GAME_LOOK_PITCH_LIMIT: float = 89.0

# ── 奔跑抖动（位移抖动，振幅按身体尺度比例 ✓）──────────
RUN_SHAKING_ENABLED: bool = True      # 默认开
SHAKE_VERTICAL_RATIO: float = 0.025   # 垂直振幅 = 参考距离 × 2.5%（1.8m → ±4.5cm）
SHAKE_LATERAL_RATIO: float = 0.015    # 横向振幅 = 参考距离 × 1.5%（1.8m → ±2.7cm）
SHAKE_STEP_HZ: float = 1.9            # 步频（人类跑步 ≈1.8~2 步/秒）
SHAKE_SMOOTH_TAU: float = 0.15        # 抖动淡入淡出

# ── 相机防撞（方案 B：6 方向射线 + 滑动）───────────────
WALL_COLLISION_ENABLED: bool = True
# 阈值：参考距离代表 1.8 米时，阈值为 10 厘米（按比例缩放 ✓）
WALL_COLLISION_CM: float = 10.0
GAME_BODY_HEIGHT_M_REF: float = 1.8

# 没做 SetBodyHeight 标定时的兜底阈值（当前场景单位）
WALL_COLLISION_FALLBACK_UNITS: float = 0.5

# 调试：打印每次命中的墙
WALL_DEBUG: bool = True

# 摇杆死区（0~1）：这个范围内视为无输入
GAME_DEADZONE: float = 0.15

# 摇杆曲线指数：>1 时"轻推慢、重推快"
#   1.5 太陡（50% 只给 26%）✗  1.2 更接近现代 FPS ✓
GAME_CURVE_POWER: float = 1.2

# 消息超时（超过这么久没收到就当作松手，只受摩擦）
GAME_MESSAGE_TIMEOUT: float = 0.5

# 单帧最大时间步长（防止卡顿后瞬移）
GAME_MAX_DT: float = 0.1

# 调试输出
GAME_MOVE_DEBUG: bool = False


# ── 状态 ────────────────────────────────────────────────────
# 当前速度（世界坐标向量，保留惯性 ✓）
_velocity: Vector = Vector((0.0, 0.0, 0.0))

# 上一次消息的时间
_last_message_time: float = 0.0


def _reset_state() -> None:
    global _velocity, _last_message_time
    _velocity = Vector((0.0, 0.0, 0.0))
    _last_message_time = 0.0


def _apply_friction(dt: float) -> None:
    """摩擦：没输入时速度按指数衰减。"""
    global _velocity

    speed = _velocity.length

    if speed < 1e-6:
        _velocity = Vector((0.0, 0.0, 0.0))
        return

    control = max(speed, GAME_STOP_SPEED)
    drop = control * GAME_FRICTION * dt
    new_speed = max(0.0, speed - drop)

    _velocity *= new_speed / speed


def _apply_acceleration(
    wishdir: Vector,
    wishspeed: float,
    dt: float,
    accel: float = GAME_ACCEL,
) -> None:
    """加速：加速量受当前速度限制（越接近目标速度加得越慢）。"""
    global _velocity

    current_speed = _velocity.dot(wishdir)
    add_speed = wishspeed - current_speed

    if add_speed <= 0.0:
        return

    accel_speed = accel * dt * wishspeed

    if accel_speed > add_speed:
        accel_speed = add_speed

    _velocity = _velocity + wishdir * accel_speed


def _stick_to_depth(magnitude: float) -> float:
    """摇杆量（0~1）→ 目标速度比例（带死区 + 指数曲线）。"""
    if magnitude <= GAME_DEADZONE:
        return 0.0

    depth = (magnitude - GAME_DEADZONE) / max(1.0 - GAME_DEADZONE, 1e-6)
    depth = max(0.0, min(1.0, depth))

    return depth**GAME_CURVE_POWER


# 脚下检测状态（100ms 节流 ✓）
_under_has_surface: bool = False
_under_is_game_ground: "bool | None" = None
_under_time: float = 0.0
UNDER_CHECK_INTERVAL: float = 0.1


def _notify_game_indicator(has_ground: bool) -> None:
    """推给手机：脚下是否有物体 + 该物体是否带 is_game_ground。"""
    from . import streamer as _streamer

    server = getattr(_streamer._state, "server", None)

    if server is None:
        return

    try:
        server.send_text(json.dumps({
            "type": "ground_indicator",
            "has_ground": bool(has_ground),
            "has_surface": bool(_under_has_surface),
        }))
    except Exception as exc:                       # noqa: BLE001
        print(f"[Blecaco][Game] ground_indicator 发送失败：{exc}")


def _check_under_game_camera() -> None:
    """沿相机 -Y 打射线，判断脚下是否有物体、是否带 is_game_ground。"""
    global _under_has_surface, _under_is_game_ground, _under_time

    now = time.monotonic()

    if now - _under_time < UNDER_CHECK_INTERVAL:
        return

    _under_time = now

    camera = bpy.context.scene.camera

    if camera is None:
        return

    direction = camera.rotation_quaternion @ Vector((0.0, -1.0, 0.0))

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        hit, loc, normal, idx, obj, matrix = bpy.context.scene.ray_cast(
            depsgraph, camera.location, direction, distance=1000.0
        )
    except Exception:                              # noqa: BLE001
        return

    has_surface = bool(hit and obj is not None)
    has_ground = False

    if has_surface:
        try:
            has_ground = bool(obj.get(GAME_GROUND_TAG, False))
        except ReferenceError:
            has_surface = False

    surface_changed = has_surface != _under_has_surface
    _under_has_surface = has_surface

    if (
        _under_is_game_ground is None
        or has_ground != _under_is_game_ground
        or surface_changed
    ):
        _under_is_game_ground = has_ground
        _notify_game_indicator(has_ground)


def _find_tagged_game_ground():
    """找带 is_game_ground 属性的 MESH 物体。"""
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        if obj.get(GAME_GROUND_TAG, False):
            return obj
    return None


def _clear_game_ground() -> None:
    """删掉所有 is_game_ground 属性。"""
    global _game_ground_object, _ground_reference_distance

    count = 0
    for obj in bpy.data.objects:
        if obj.get(GAME_GROUND_TAG, False):
            try:
                del obj[GAME_GROUND_TAG]
                count += 1
            except Exception:                      # noqa: BLE001
                pass

    _game_ground_object = None
    _ground_reference_distance = 0.0

    print(f"[Blecaco][Game] 已清除游戏地面标记（{count} 个物体）")


def _set_game_ground() -> bool:
    """确定追踪地面 + 记录参考距离。

    命中 → 旧物体取消标记，新物体打标，D = camera.z - hit.z
    未命中 + camera.z >= 0 → D = camera.z（用世界 X-Y 平面当"地面"）
    未命中 + camera.z <  0 → 标记无效（移动/旋转禁用）
    """
    global _game_ground_object, _ground_reference_distance, _reference_valid

    scene = bpy.context.scene
    camera = scene.camera

    if camera is None:
        print("[Blecaco][Game] 没有活动相机")
        return False

    direction = camera.rotation_quaternion @ Vector((0.0, -1.0, 0.0))

    hit = False
    loc = None
    obj = None

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()

        hit, loc, normal, idx, obj, matrix = scene.ray_cast(
            depsgraph, camera.location, direction, distance=1000.0
        )

        # 打空 → 退回世界 -Z
        if not hit or obj is None:
            hit, loc, normal, idx, obj, matrix = scene.ray_cast(
                depsgraph, camera.location, Vector((0.0, 0.0, -1.0)),
                distance=2000.0,
            )
            if hit and obj is not None:
                print("[Blecaco][Game] 相机 -Y 未命中，改用竖直向下命中 ✓")
    except Exception as exc:                       # noqa: BLE001
        print(f"[Blecaco][Game] 地面射线异常：{exc}")
        hit = False

    # ── 未命中 ─────────────────────────────────────
    if not hit or obj is None:
        if camera.location.z >= 0.0:
            # 用世界 X-Y 平面当"地面" ✓
            _clear_game_ground()

            _ground_reference_distance = camera.location.z
            _reference_valid = _ground_reference_distance > 1e-4

            print(
                f"[Blecaco][Game] 相机下方无物体 → 用 X-Y 平面，"
                f"参考距离 = {_ground_reference_distance:.3f}"
            )
            return _reference_valid

        # 相机在 X-Y 平面之下 → 无效 ✗
        _clear_game_ground()
        _reference_valid = False
        print("[Blecaco][Game] 相机在 X-Y 平面之下 → 移动/旋转禁用 ✗")
        return False

    # ── 命中 ───────────────────────────────────────
    # 只清除旧标记（其他物体的 is_game_ground）
    for o in bpy.data.objects:
        if o.get(GAME_GROUND_TAG, False) and o != obj:
            try:
                del o[GAME_GROUND_TAG]
            except Exception:                      # noqa: BLE001
                pass

    obj[GAME_GROUND_TAG] = True
    _game_ground_object = obj

    # D = Z 方向距离 ✓
    _ground_reference_distance = camera.location.z - loc.z
    if _ground_reference_distance < 0.0:
        _ground_reference_distance = abs(_ground_reference_distance)

    _reference_valid = _ground_reference_distance > 1e-4

    print(
        f"[Blecaco][Game] 游戏地面已标记：{obj.name} "
        f"参考距离 = {_ground_reference_distance:.3f}"
        f"（代表 {GAME_BODY_HEIGHT_M} 米）"
    )
    return _reference_valid


## 当前有效的基础速度（单位/秒）
def _walk_speed_units() -> float:
    """把 GAME_WALK_MPS（米/秒）换算成当前场景的单位/秒。

    1 单位 = GAME_BODY_HEIGHT_M / D 米  →  速度(单位) = 速度(米) × D / 1.8
    """
    if not _reference_valid or _ground_reference_distance <= 1e-6:
        return GAME_MAX_SPEED

    return GAME_WALK_MPS * _ground_reference_distance / GAME_BODY_HEIGHT_M


def _follow_game_ground(camera) -> None:
    """把相机 Z 保持在"地面高度 + 实测距离"上。

    不改变旋转 ✓（相机 X 轴始终平行于世界 X-Y 平面 ✓）
    """
    if _game_ground_object is None:
        return

    try:
        obj = _game_ground_object
        obj_name = obj.name
    except ReferenceError:
        _clear_game_ground()
        return

    # 用世界 -Z 在这个物体上打射线，取它当前正下方的高度
    try:
        inv = obj.matrix_world.inverted()
        local_origin = inv @ camera.location
        local_dir = (inv.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()

        hit, loc, normal, idx = obj.ray_cast(local_origin, local_dir, distance=1000.0)
    except Exception:                              # noqa: BLE001
        return

    if not hit:
        return

    world_loc = obj.matrix_world @ loc

    camera.location = Vector((
        camera.location.x,
        camera.location.y,
        world_loc.z + _ground_reference_distance,
    ))


def _apply_shake(camera, speed_ratio: float, dt: float) -> None:
    """位移抖动：模拟跑步时的头部上下/左右运动。

    振幅按【参考距离】比例缩放 ✓（尺度无关）
    垂直 = 2×步频，横向 = 1×步频（错开相位 ✓）
    """
    global _shake_amp, _shake_time, _shake_prev

    # ★ 只撤销【横向】分量 ✓
    #   Z 已经被 _follow_game_ground 重置过，不能再减 ✗（会重复抵消 ✗）
    camera.location = camera.location - Vector((_shake_prev.x, _shake_prev.y, 0.0))
    _shake_prev = Vector((0.0, 0.0, 0.0))

    if not RUN_SHAKING_ENABLED or _ground_reference_distance <= 1e-6:
        target = 0.0
    else:
        # sqrt 让慢跑也有明显起伏 ✓
        target = math.sqrt(max(0.0, min(1.0, speed_ratio)))

    k = 1.0 - math.exp(-max(dt, 0.0) / SHAKE_SMOOTH_TAU)
    _shake_amp += (target - _shake_amp) * k

    if _shake_amp < 1e-4:
        return

    _shake_time += dt

    amp_v = _ground_reference_distance * SHAKE_VERTICAL_RATIO * _shake_amp
    amp_h = _ground_reference_distance * SHAKE_LATERAL_RATIO * _shake_amp

    w = 2.0 * math.pi * SHAKE_STEP_HZ

    dv = math.sin(_shake_time * w * 2.0) * amp_v
    dh = math.sin(_shake_time * w * 1.0) * amp_h

    # 横向沿相机右方向 ✓
    right = camera.rotation_quaternion @ Vector((1.0, 0.0, 0.0))
    right_flat = Vector((right.x, right.y, 0.0))
    if right_flat.length > 1e-5:
        right_flat.normalize()
    else:
        right_flat = Vector((1.0, 0.0, 0.0))

    offset = Vector((0.0, 0.0, dv)) + right_flat * dh
    camera.location = camera.location + offset
    _shake_prev = offset


def _wall_threshold_units() -> float:
    """防撞阈值（当前场景单位）。

    有标定 → 10cm 换算；没标定 → 用兜底绝对值 ✓
    """
    if _ground_reference_distance <= 1e-6:
        # ⚠️ 没做 SetBodyHeight 时也要能防撞 ✗ 用兜底值 ✓
        return WALL_COLLISION_FALLBACK_UNITS

    meters_per_unit = GAME_BODY_HEIGHT_M_REF / _ground_reference_distance
    return (WALL_COLLISION_CM / 100.0) / meters_per_unit


def _apply_wall_collision(camera, wishdir: Vector, velocity: Vector) -> tuple:
    """6 方向短射线 + 把 wishdir 投影到表面（沿墙滑动 ✓）。

    只检查【朝向墙面】的方向，跳过地面物体本身 ✗
    """
    if not WALL_COLLISION_ENABLED:
        return wishdir, velocity

    threshold = _wall_threshold_units()

    if threshold <= 1e-6 or wishdir.length_squared < 1e-12:
        return wishdir, velocity

    scene = bpy.context.scene
    quat = camera.rotation_quaternion

    # 相机局部 6 个方向
    axes = [
        quat @ Vector((1.0, 0.0, 0.0)),
        quat @ Vector((-1.0, 0.0, 0.0)),
        quat @ Vector((0.0, 0.0, -1.0)),
        quat @ Vector((0.0, 0.0, 1.0)),
        quat @ Vector((0.0, 1.0, 0.0)),
        quat @ Vector((0.0, -1.0, 0.0)),
    ]

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:                              # noqa: BLE001
        return wishdir, velocity

    result = wishdir.copy()
    vel = velocity.copy()

    for d in axes:
        d = d.normalized()

        # 只在"朝着这个方向走"时才检测 ✓
        if result.dot(d) <= 1e-4:
            continue

        try:
            hit, loc, normal, idx, obj, matrix = scene.ray_cast(
                depsgraph, camera.location, d, distance=threshold
            )
        except Exception:                          # noqa: BLE001
            continue

        if not hit or obj is None:
            continue

        # ⚠️ 不再跳过地面物体 ✗
        #   移动速度是【水平】的 → 向下的射线根本不会被检测（dot <= 0 ✓）
        #   而"撞山坡"撞的就是地面物体 ✓ 跳过它就等于没防撞 ✗

        if WALL_DEBUG:
            print(
                f"[Blecaco][Game] 防撞命中 {obj.name} "
                f"距离={threshold:.3f} 法线=({normal.x:.2f},{normal.y:.2f},{normal.z:.2f})"
            )

        # 把【输入方向】投影到表面（滑过去 ✓）
        n = Vector(normal).normalized()
        result = result - n * result.dot(n)

        # ★ 把【当前速度】也投影掉 ✓
        #   否则 snappy 的惯性会继续把相机推进墙里 ✗
        vel = vel - n * vel.dot(n)

    return result, vel


def _sweep_camera_move(start: Vector, step: Vector) -> Vector:
    """从【移动前】位置扫掠本帧位移，返回实际允许的位移 ✓

    ⚠️ 只从移动后位置打 threshold 长的射线是不够的 ✗
       推流 10fps 时单帧位移可达 0.4 单位，整面墙会被跨过去 ✗
       这里扫掠 step + threshold，10/30/60fps 都不会穿 ✓

    撞墙 → 停在墙前 threshold，并把 _velocity 投影到墙面（沿墙滑 ✓）
    地面/缓坡 → 不挡水平移动（高度由 _follow_game_ground 负责 ✓）
    """
    global _velocity

    if step.length_squared <= 1e-18:
        return step

    threshold = _wall_threshold_units()

    if threshold <= 1e-6:
        return step

    direction = step.normalized()

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        hit, loc, normal, idx, obj, matrix = bpy.context.scene.ray_cast(
            depsgraph, start, direction, distance=step.length + threshold
        )
    except Exception:                              # noqa: BLE001
        return step

    if not hit or obj is None:
        return step

    n = Vector(normal).normalized()

    # 地面 / 缓坡不挡水平移动 ✓
    #   竖直面（墙）|n.z|≈0 → 挡；水平面（地面）|n.z|≈1 → 放行 ✓
    if abs(n.z) > 0.7:
        return step

    # 撞墙：速度投影到墙面，下一帧沿墙滑 ✓
    #   ⚠️ 只要扫掠命中就推出 ✗ 不能因为"墙在位移终点之外"就放行 ✗
    #      否则终点已经进入阈值内时会一帧一帧蹭近，最后贴到墙面 ✗
    _velocity = _velocity - n * _velocity.dot(n)

    # 沿【墙面法线】退到 threshold 外 ✓
    #   ⚠️ 只沿移动方向退 threshold 的话，斜撞时法线间距会被 cos 缩小 ✗
    moved = Vector((
        loc.x + n.x * threshold - start.x,
        loc.y + n.y * threshold - start.y,
        0.0,
    ))

    # 不允许倒退（起点已在阈值内时）✓
    if moved.dot(direction) <= 0.0:
        return Vector((0.0, 0.0, 0.0))

    return moved


def _apply_look(
    camera, look_dx: float, look_dy: float, multiplier: float = 1.0
) -> None:
    """FPS 式视角旋转（直接映射 ✓ 拖多少转多少，松手即停）。

    look_dx > 0（手指右滑）→ 绕【世界 Z 轴】向右转 ✓
    look_dy > 0（手指下滑）→ 绕【相机局部 X 轴】低头 ✓
    俯仰限制在 ±GAME_LOOK_PITCH_LIMIT 度内 ✓
    """
    if abs(look_dx) < 1e-4 and abs(look_dy) < 1e-4:
        return

    quat = camera.rotation_quaternion

    # ① 左右：绕世界 Z 轴（前乘 ✓）
    #   手指右 → 相机右转 → 绕 +Z 顺时针 = 负角 ✓
    if abs(look_dx) > 1e-4:
        yaw = math.radians(-GAME_LOOK_YAW_DEG_PER_PX * look_dx * multiplier)
        quat = Quaternion((0.0, 0.0, 1.0), yaw) @ quat

    # ② 上下：绕相机局部 X 轴（后乘 ✓）
    if abs(look_dy) > 1e-4:
        # 手指上（look_dy < 0）→ 抬头 → 局部 X 正角 ✓
        pitch = math.radians(-GAME_LOOK_PITCH_DEG_PER_PX * look_dy * multiplier)
        candidate = quat @ Quaternion((1.0, 0.0, 0.0), pitch)

        # 俯仰限位：看前方向量的 Z 分量不能超过 sin(limit) ✓
        forward = candidate @ Vector((0.0, 0.0, -1.0))
        limit_sin = math.sin(math.radians(GAME_LOOK_PITCH_LIMIT))

        if abs(forward.z) <= limit_sin:
            quat = candidate

    camera.rotation_quaternion = quat


def handle_message(message) -> None:
    global _last_message_time, RUN_SHAKING_ENABLED, _velocity

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Game] Invalid data")
        return

    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Game] No active camera")
        return

    camera.rotation_mode = 'QUATERNION'

    # ★ 参考无效（相机在 X-Y 平面之下且下方无物体）→ 移动/旋转全部禁用 ✗
    #   统一在开头 return，避免后面任何计算 ✗
    if not _reference_valid and camera.location.z < 0.0:
        return

    # 地面标记命令
    if data.get("command") in ("set_ground", "toggle_ground"):
        enabled = bool(data.get("enabled", False))

        if enabled:
            _set_game_ground()
        else:
            _clear_game_ground()

        return

    # 初始化：清零速度 + 记录时间基准
    if data.get("command") == "initialize":
        _reset_state()
        _last_message_time = time.monotonic()
        print("[Blecaco][Game] Game mode initialized（速度已清零）")
        return

    # 每条消息都检测脚下（100ms 节流 ✓）→ 手机据此判断能否长按 ✓
    _check_under_game_camera()

    try:
        rx = float(data.get("rx", 0.0))
        ry = float(data.get("ry", 0.0))
    except (TypeError, ValueError):
        return

    # RUN_SHAKING 开关（同时控制"抖动"和"snappy 加减速"✓）
    #   ⚠️ 必须在速度计算之前读 ✗
    try:
        RUN_SHAKING_ENABLED = bool(data.get("run_shaking", True))
    except (TypeError, ValueError):
        RUN_SHAKING_ENABLED = True

    # WALL_COLLISION 开关
    global WALL_COLLISION_ENABLED
    WALL_COLLISION_ENABLED = bool(data.get("wall_collision", True))

    # 冲刺状态 + 倍率（由手机端按 RUN_SPEED 档位 + 迟滞算好）
    sprinting = bool(data.get("sprint", False))

    try:
        run_multiplier = float(data.get("run_multiplier", 1.0))
    except (TypeError, ValueError):
        run_multiplier = 1.0

    run_multiplier = max(1.0, min(2.0, run_multiplier))

    now = time.monotonic()
    dt = now - _last_message_time if _last_message_time > 0.0 else 0.0
    _last_message_time = now

    dt = max(0.0, min(dt, GAME_MAX_DT))

    if dt <= 0.0:
        return

    # ── 摇杆 → 目标方向 + 目标速度 ──────────────────────
    magnitude = math.hypot(rx, ry)
    depth = _stick_to_depth(magnitude)

    wishdir = Vector((0.0, 0.0, 0.0))
    wishspeed = 0.0
    base_speed = 0.0

    if depth > 0.0:
        # 相机局部轴投影到水平面（Z' = 相机 -Z 的水平投影）
        quat = camera.rotation_quaternion
        camera_x = quat @ Vector((1.0, 0.0, 0.0))
        camera_forward = quat @ Vector((0.0, 0.0, -1.0))

        x_axis_flat = Vector((camera_x.x, camera_x.y, 0.0))
        z_axis_flat = Vector((camera_forward.x, camera_forward.y, 0.0))

        if x_axis_flat.length < 1e-5:
            x_axis_flat = Vector((1.0, 0.0, 0.0))
        else:
            x_axis_flat.normalize()

        if z_axis_flat.length < 1e-5:
            z_axis_flat = Vector((0.0, 1.0, 0.0))
        else:
            z_axis_flat.normalize()

        # ry 在 Godot 屏幕坐标里"往下为正" ✗
        # → "按钮在上方" → ry < 0 → 沿 Z' 正方向 ✓
        direction = x_axis_flat * rx + z_axis_flat * (-ry)

        # 归一化：斜向不会快 41% ✓
        if direction.length > 1e-6:
            wishdir = direction.normalized()

        # ── 相机防撞（6 方向射线 + 滑动 ✓）────────────────
        #   ⚠️ 必须同时挡 _velocity ✗ 否则 snappy 惯性会继续推进 ✗
        blocked_dir, blocked_vel = _apply_wall_collision(camera, wishdir, _velocity)

        wishdir = blocked_dir
        _velocity = blocked_vel

        if wishdir.length > 1e-6:
            wishdir = wishdir.normalized()

        # 行走速度对所有档位一致 ✓
        # 冲刺速度 = 行走速度 × 档位倍率 ✓
        base_speed = _walk_speed_units() * depth

        if sprinting:
            wishspeed = base_speed * run_multiplier
        else:
            wishspeed = base_speed

    # ── 摩擦 + 加速 ─────────────────────────────────────
    if not RUN_SHAKING_ENABLED:
        # 关闭 snappy：直接等于目标速度（立即启停 ✓ 无惯性 ✓）
        if wishdir.length > 1e-6 and wishspeed > 0.0:
            _velocity = wishdir * wishspeed
        else:
            _velocity = Vector((0.0, 0.0, 0.0))
    else:
        _apply_friction(dt)

    if RUN_SHAKING_ENABLED and wishdir.length > 1e-6 and wishspeed > 0.0:
        # 方式 3：冲刺时加速度更低（更重、更"沉"）✓
        accel = GAME_ACCEL * GAME_SPRINT_ACCEL_RATIO if sprinting else GAME_ACCEL

        # 加速量用 wishspeed（Quake 原版 ✓）
        #   加速度系数降为 80% 已经足够让冲刺起步变"沉" ✓
        _apply_acceleration(wishdir, wishspeed, dt, accel)

    # ── 速度积分位置（保持 z 不变，纯水平移动 ✓）────────
    prev_location = camera.location.copy()
    step = Vector((_velocity.x, _velocity.y, 0.0)) * dt

    # ★ 防穿透：从【移动前】位置扫掠本帧位移（step + threshold）✓
    #   ⚠️ 旧版从移动后位置只打 threshold → 10fps 单帧 0.4 会直接跨墙 ✗
    if WALL_COLLISION_ENABLED and step.length_squared > 1e-18:
        step = _sweep_camera_move(prev_location, step)

    camera.location = prev_location + step

    # ── 贴地（简化版：只保持 Z 高度，不改旋转 ✓）────────
    _follow_game_ground(camera)

    # ── 奔跑抖动（位移 ✓ 在贴地之后加，下一帧会被覆盖 ✓ 不漂移 ✓）──
    speed_ratio = 0.0
    if wishspeed > 1e-6:
        speed_ratio = min(1.0, _velocity.length / wishspeed)

    _apply_shake(camera, speed_ratio, dt)

    # ── 视角旋转（FPS 式）──────────────────────────────
    try:
        look_dx = float(data.get("look_dx", 0.0))
        look_dy = float(data.get("look_dy", 0.0))
        look_mult = float(data.get("look_multiplier", 1.0))
    except (TypeError, ValueError):
        look_dx = look_dy = 0.0
        look_mult = 1.0

    look_mult = max(0.3, min(3.0, look_mult))



    _apply_look(camera, look_dx, look_dy, look_mult)

    if GAME_MOVE_DEBUG:
        print(
            f"[Blecaco][Game] rx={rx:+.2f} ry={ry:+.2f} "
            f"depth={depth:.2f} mult={run_multiplier:.2f} |v|={_velocity.length:.3f} "
            f"v=({_velocity.x:+.3f},{_velocity.y:+.3f})"
        )
