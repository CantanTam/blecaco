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

import math
import time

import bpy
from mathutils import Vector

# 复用 car_action 的"贴地"机制（is_ground 标记 / PCA 拟合 / 姿态对齐）
#   ⚠️ 两个模式不会同时激活 ✓ 共用一份状态是安全的 ✓
from . import car_action


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
        car_action.handle_message(message)
        return

    # 初始化：清零速度 + 记录时间基准
    if data.get("command") == "initialize":
        _reset_state()
        _last_message_time = time.monotonic()
        print("[Blecaco][Game] Game mode initialized（速度已清零）")
        return

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

    # ── 贴地（与 car_mode 完全一致 ✓）──────────────────
    if car_action._ground_object is not None:
        camera.location = car_action._apply_ground_height(
            camera, camera.location, dt
        )

        # 相机 Y 轴 ⟂ 拟合平面法线（car_action 里算好的 ✓）
        if car_action._ground_normal is not None:
            camera.rotation_quaternion = car_action._align_quaternion_to_normal(
                camera.rotation_quaternion, car_action._ground_normal
            )

    # 实时检测脚下物体是否带 is_ground（100ms 节流 ✓）
    car_action._check_under_camera()

    if GAME_MOVE_DEBUG:
        print(
            f"[Blecaco][Game] rx={rx:+.2f} ry={ry:+.2f} "
            f"depth={depth:.2f} mult={run_multiplier:.2f} |v|={_velocity.length:.3f} "
            f"v=({_velocity.x:+.3f},{_velocity.y:+.3f})"
        )
