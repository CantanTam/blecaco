"""Game Mode —— 摄像机在水平面上的前后左右移动。

对应手机端 game_mode.tscn 的 MoveStickArea / MoveButton 摇杆。

移动方向定义（用户设定）：
    MoveButton 在区域中心【上方】 → 沿 Z' 正方向（Z' = 相机 -Z 在水平面的投影）
    MoveButton 在区域中心【下方】 → 沿 Z' 负方向
    MoveButton 在区域中心【左方】 → 沿相机局部 X 轴负方向
    MoveButton 在区域中心【右方】 → 沿相机局部 X 轴正方向

参考 drone_action.py 的右摇杆实现（x_axis_flat / y_axis_flat 投影）。
"""

from __future__ import annotations

import math
import time

import bpy
from mathutils import Quaternion, Vector


# ── 速度（单位/秒），后续会加档位 ──────────────────────────
GAME_MOVE_SPEED: float = 5.0

# 摇杆归一化半径（手机端除以这个值得到 -1..1）
GAME_STICK_LIMIT_RADIUS: float = 250.0

# 消息超时（超过这么久没收到就停止移动，防止断线后一直跑）
GAME_MESSAGE_TIMEOUT: float = 0.5

# 调试
GAME_MOVE_DEBUG: bool = False


# ── 状态 ────────────────────────────────────────────────────
_last_message_time: float = 0.0


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

    # 初始化：记录时间基准（相机姿态由用户自己预先摆好 ✓）
    if data.get("command") == "initialize":
        _last_message_time = time.monotonic()
        print("[Blecaco][Game] Game mode initialized")
        return

    # 摇杆向量（-1..1，ry 往下为正）
    try:
        rx = float(data.get("rx", 0.0))
        ry = float(data.get("ry", 0.0))
    except (TypeError, ValueError):
        return

    now = time.monotonic()
    dt = now - _last_message_time if _last_message_time > 0.0 else 0.0
    _last_message_time = now

    # 帧率无关的钳制，防止卡顿时瞬移
    dt = max(0.0, min(dt, 0.1))

    if dt <= 0.0:
        return

    length = math.hypot(rx, ry)

    if length < 1e-5:
        return

    # ① 左右 → 沿 X'（相机 X 轴在水平面的投影）
    # ② 上下 → 沿 Z'（相机 -Z 轴在水平面的投影）
    #    ry 在 Godot 屏幕坐标里"往下为正" ✗
    #    所以"按钮在上方" → ry < 0 → long_dir > 0 → 沿 Z' 正方向 ✓
    strafe_dir = rx / length
    long_dir = -ry / length

    quat = camera.rotation_quaternion
    camera_x = quat @ Vector((1.0, 0.0, 0.0))
    camera_forward = quat @ Vector((0.0, 0.0, -1.0))

    x_axis_flat = Vector((camera_x.x, camera_x.y, 0.0))
    z_axis_flat = Vector((camera_forward.x, camera_forward.y, 0.0))

    # 相机接近垂直俯视时的兜底（正常不会触发）
    if x_axis_flat.length < 1e-5:
        x_axis_flat = Vector((1.0, 0.0, 0.0))
    else:
        x_axis_flat.normalize()

    if z_axis_flat.length < 1e-5:
        z_axis_flat = Vector((0.0, 1.0, 0.0))
    else:
        z_axis_flat.normalize()

    strafe = GAME_MOVE_SPEED * dt * strafe_dir
    longitudinal = GAME_MOVE_SPEED * dt * long_dir

    camera.location = (
        camera.location
        + x_axis_flat * strafe
        + z_axis_flat * longitudinal
    )

    if GAME_MOVE_DEBUG:
        print(
            f"[Blecaco][Game] 移动 rx={rx:.2f} ry={ry:.2f} "
            f"X'=({x_axis_flat.x:.2f},{x_axis_flat.y:.2f}) "
            f"Z'=({z_axis_flat.x:.2f},{z_axis_flat.y:.2f}) "
            f"位移=({strafe:.4f},{longitudinal:.4f})"
        )
