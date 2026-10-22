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

# 实测的"相机到地面"的 Z 方向距离（= 用户摆的高度 ✓）
_game_ground_height: float = 0.0


# ── 手感参数（参考 CS:GO / Apex）───────────────────────────
# 最大移动速度（单位/秒）
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
# 满偏时的角速度（度/秒）
GAME_LOOK_YAW_SPEED: float = 120.0      # 世界 Z 轴（左右）
GAME_LOOK_PITCH_SPEED: float = 90.0     # 相机局部 X 轴（上下）
# 俯仰上限（±89° ✓）
GAME_LOOK_PITCH_LIMIT: float = 89.0

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
    global _game_ground_object, _game_ground_height

    count = 0
    for obj in bpy.data.objects:
        if obj.get(GAME_GROUND_TAG, False):
            try:
                del obj[GAME_GROUND_TAG]
                count += 1
            except Exception:                      # noqa: BLE001
                pass

    _game_ground_object = None
    _game_ground_height = 0.0

    print(f"[Blecaco][Game] 已清除游戏地面标记（{count} 个物体）")


def _set_game_ground() -> bool:
    """沿相机局部 -Y 打射线，把第一个命中的物体标记为游戏地面。

    同时记录相机到命中点的【Z 方向距离】→ 之后一直保持这个高度 ✓
    """
    global _game_ground_object, _game_ground_height

    _clear_game_ground()

    scene = bpy.context.scene
    camera = scene.camera

    if camera is None:
        print("[Blecaco][Game] 没有活动相机")
        return False

    direction = camera.rotation_quaternion @ Vector((0.0, -1.0, 0.0))

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
    except Exception as exc:                       # noqa: BLE001
        print(f"[Blecaco][Game] 地面射线异常：{exc}")
        return False

    if not hit or obj is None:
        print("[Blecaco][Game] 相机下方没有物体，标记失败")
        return False

    obj[GAME_GROUND_TAG] = True
    _game_ground_object = obj

    # 记录 Z 方向的距离（正是用户摆的离地高度 ✓）
    _game_ground_height = camera.location.z - loc.z

    print(
        f"[Blecaco][Game] 游戏地面已标记：{obj.name} "
        f"离地高度 = {_game_ground_height:.3f}"
    )
    return True


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
        world_loc.z + _game_ground_height,
    ))


def _apply_look(camera, look_x: float, look_y: float, dt: float) -> None:
    """FPS 式视角旋转。

    look_y > 0（手指往下）→ 绕【相机局部 X 轴】向下转 ✓
    look_x > 0（手指往右）→ 绕【世界 Z 轴】向右转 ✓
    俯仰限制在 ±GAME_LOOK_PITCH_LIMIT 度内 ✓
    """
    if abs(look_x) < 1e-4 and abs(look_y) < 1e-4:
        return

    quat = camera.rotation_quaternion

    # ① 左右：绕世界 Z 轴（前乘 ✓）
    if abs(look_x) > 1e-4:
        yaw = math.radians(GAME_LOOK_YAW_SPEED * dt * look_x)
        quat = Quaternion((0.0, 0.0, 1.0), yaw) @ quat

    # ② 上下：绕相机局部 X 轴（后乘 ✓）
    if abs(look_y) > 1e-4:
        pitch = math.radians(GAME_LOOK_PITCH_SPEED * dt * look_y)
        quat = quat @ Quaternion((1.0, 0.0, 0.0), pitch)

        # 俯仰限位：看前方向量的 Z 分量不能超过 sin(limit) ✓
        forward = quat @ Vector((0.0, 0.0, -1.0))
        limit_sin = math.sin(math.radians(GAME_LOOK_PITCH_LIMIT))

        if abs(forward.z) > limit_sin:
            # 超限 → 撤销这次 pitch
            quat = camera.rotation_quaternion

            if abs(look_x) > 1e-4:
                yaw = math.radians(GAME_LOOK_YAW_SPEED * dt * look_x)
                quat = Quaternion((0.0, 0.0, 1.0), yaw) @ quat

    camera.rotation_quaternion = quat


def handle_message(message) -> None:
    global _last_message_time

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Game] Invalid data")
        return

    camera = bpy.context.scene.camera

    if camera is None:
        print("[Blecaco][Game] No active camera")
        return

    camera.rotation_mode = 'QUATERNION'

    # 地面标记命令 → 直接转交 car_action（与 car_mode 完全一致 ✓）
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

        # 行走速度对所有档位一致 ✓
        # 冲刺速度 = 行走速度 × 档位倍率 ✓
        base_speed = GAME_MAX_SPEED * depth

        if sprinting:
            wishspeed = base_speed * run_multiplier
        else:
            wishspeed = base_speed

    # ── 摩擦 + 加速 ─────────────────────────────────────
    _apply_friction(dt)

    if wishdir.length > 1e-6 and wishspeed > 0.0:
        # 方式 3：冲刺时加速度更低（更重、更"沉"）✓
        accel = GAME_ACCEL * GAME_SPRINT_ACCEL_RATIO if sprinting else GAME_ACCEL

        # 加速量用 wishspeed（Quake 原版 ✓）
        #   加速度系数降为 80% 已经足够让冲刺起步变"沉" ✓
        _apply_acceleration(wishdir, wishspeed, dt, accel)

    # ── 速度积分位置（保持 z 不变，纯水平移动 ✓）────────
    camera.location = camera.location + Vector((_velocity.x, _velocity.y, 0.0)) * dt

    # ── 贴地（简化版：只保持 Z 高度，不改旋转 ✓）────────
    _follow_game_ground(camera)

    # ── 视角旋转（FPS 式）──────────────────────────────
    try:
        look_x = float(data.get("look_x", 0.0))
        look_y = float(data.get("look_y", 0.0))
    except (TypeError, ValueError):
        look_x = look_y = 0.0

    _apply_look(camera, look_x, look_y, dt)

    if GAME_MOVE_DEBUG:
        print(
            f"[Blecaco][Game] rx={rx:+.2f} ry={ry:+.2f} "
            f"depth={depth:.2f} mult={run_multiplier:.2f} |v|={_velocity.length:.3f} "
            f"v=({_velocity.x:+.3f},{_velocity.y:+.3f})"
        )
