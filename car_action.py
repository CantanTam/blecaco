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


# ── 局部平面拟合（把"三角面法线"换成"邻域拟合平面"）────────
# 采样半径 = 眼高 × 此比例（单位无关 ✓ 自动适配任何场景尺度）
CAR_SAMPLE_RADIUS_RATIO: float = 0.6

# 采样点数（每边 N 个 → N×N 网格；3 = 9 点）
CAR_SAMPLE_GRID: int = 3

# 采样半径上限（眼高的倍数）——防止网格太粗时半径失控
CAR_SAMPLE_RADIUS_MAX_RATIO: float = 3.0

# 采样半径至少是网格平均边长的几倍（保证跨过多个面 ✓）
#   太小 → 9 个点落在同一个三角面内 ✗ 拟合退化成单面法线 ✗
CAR_SAMPLE_CELL_FACTOR: float = 3.0

# 最少有效采样点（少于此数就沿用上一次的法线）
CAR_MIN_VALID_SAMPLES: int = 3

# 相机移动超过这个距离才重新采样（性能优化；按眼高的比例算）
CAR_RESAMPLE_MIN_MOVE_RATIO: float = 0.05

# 拟合出的法线与竖直方向的夹角超过此值就不贴合（防悬崖翻车）
CAR_PLANE_MAX_SLOPE_DEG: float = 75.0

# 没有标记地面时是否强制相机贴"水平面"
#   False（默认）→ 完全不干涉高度，相机自由移动 ✓
#                   （之前是 True ✗ 会把相机拽到 EYE_HEIGHT，表现为"第一次前进沿 Z 轴跳一段"✗）
#   True          → 用下面的 CAR_FLAT_PLANE_Z + 眼高当虚拟地面
CAR_USE_FLAT_PLANE: bool = False

# 没有标记地面时使用的水平面高度（世界 Z）
# HAS_GROUND == false 且射线打不到物体时，用这个平面当地面 ✓
CAR_FLAT_PLANE_Z: float = 0.0

# 实测眼高：SetGround 时量出的"相机质心 → 地面命中点"距离 ✓
# 之后所有高度跟踪都用它（不再用固定的 CAR_EYE_HEIGHT ✗）
_measured_eye_height: "float | None" = None

# 目标位置（照搬 plane_action 的做法 ✓）
# 和 camera.location 分开：目标可以跳，相机平滑跟过去 ✓
_target_location: "Vector | None" = None

# 上一次消息间隔（位置平滑要用）
_last_dt: float = 0.0

# 位置平滑时间常数（秒）——0 表示不平滑
CAR_PATH_SMOOTH_TAU: float = 0.06

# 上一次成功对齐时用的"水平朝向"（世界系水平单位向量）
# 相机俯视接近垂直时，投影会退化 ✗ 用它兜底 → 不会 180° 掉头 ✓
_last_heading: "Vector | None" = None

# 当前拟合出的地面法线（None = 还没拟合出来）
# 转向逻辑要把它并进目标姿态，否则每帧都会把地形对齐覆盖掉 ✗
_ground_normal: "Vector | None" = None

# 当前被标记为地面的物体（None = 还没标记）
_ground_object = None


def _eye_height() -> float:
    """当前生效的眼高：优先用实测值 ✓，没量过才退回 CAR_EYE_HEIGHT。"""
    if _measured_eye_height is not None and _measured_eye_height > 1e-6:
        return _measured_eye_height

    return abs(CAR_EYE_HEIGHT)


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
    global _ground_object, _measured_eye_height

    count = 0
    for obj in bpy.data.objects:
        if obj.get("is_ground", False):
            try:
                del obj["is_ground"]
                count += 1
            except Exception as exc:              # noqa: BLE001
                print(f"[Blecaco][Car] 删除 is_ground 失败（{obj.name}）：{exc}")

    _measured_eye_height = None
    _ground_object = None
    print(f"[Blecaco][Car] 已清除地面标记（{count} 个物体）（实测眼高已清除）")


def _notify_ground_status(has_ground: bool) -> None:
    """把地面标记状态推给手机。"""
    from . import streamer as _streamer

    server = getattr(_streamer._state, "server", None)

    if server is None:
        print("[Blecaco][Car] 无法推送 ground_status（没有服务器）")
        return

    # 附带诊断信息（手机日志里能直接看到，不用看 Blender 终端 ✓）
    diag = {}

    try:
        obj = _ground_object

        if obj is not None:
            diag["cell"] = round(_estimate_mesh_cell_size(obj), 3)
            diag["dims"] = [round(v, 2) for v in obj.dimensions]

            cam = bpy.context.scene.camera

            if cam is not None:
                diag["cam_z"] = round(cam.location.z, 3)

                height, normal = _raycast_ground(cam.location)

                if height is not None:
                    diag["fit_z"] = round(height, 3)
                    diag["normal"] = [round(v, 3) for v in normal]
    except Exception as exc:                       # noqa: BLE001
        diag["err"] = str(exc)

    try:
        server.send_text(json.dumps({
            "type": "ground_status",
            "has_ground": bool(has_ground),
            "object": _ground_object.name if _ground_object else "",
            "plane_z": CAR_FLAT_PLANE_Z,
            "diag": diag,
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

    hit = False
    loc = None
    normal = None
    obj = None

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()

        # ① 先沿相机局部 -Y 打
        hit, loc, normal, idx, obj, matrix = scene.ray_cast(
            depsgraph, origin, direction, distance=CAR_RAY_DISTANCE
        )

        # ② 打空就退回世界 -Z（相机抬头/看平了也能标记 ✓）
        if not hit or obj is None:
            hit, loc, normal, idx, obj, matrix = scene.ray_cast(
                depsgraph, origin, Vector((0.0, 0.0, -1.0)),
                distance=CAR_RAY_DISTANCE * 2.0
            )
            if hit and obj is not None:
                print("[Blecaco][Car] 相机 -Y 未命中，改用竖直向下命中 ✓")
    except Exception as exc:                       # noqa: BLE001
        print(f"[Blecaco][Car] 地面射线异常：{exc}")
        return False

    if not hit or obj is None:
        print("[Blecaco][Car] 相机下方没有物体，地面标记失败")
        return False

    # 打标
    obj["is_ground"] = True
    _ground_object = obj

    # ★ 实测眼高：相机质心 → 命中点的距离 ✓
    #   用户自己决定相机放多高，代码不猜 ✗
    global _measured_eye_height
    # 取【垂直高度】（相机 z - 命中点 z）✓ 而不是 3D 距离 ✗
    # 斜着看时 3D 距离会大于真实离地高度 ✗
    _measured_eye_height = camera.location.z - loc.z

    print(
        f"[Blecaco][Car] 地面已标记：{obj.name} "
        f"（命中高度 z={loc.z:.2f} 法线=({normal.x:.2f},{normal.y:.2f},{normal.z:.2f})）"
    )
    if _measured_eye_height is not None and _measured_eye_height < 1e-6:
        # 相机在命中点下方（比如俯视时打到了头顶的物体）→ 取绝对值兜底
        _measured_eye_height = abs(_measured_eye_height)
        print("[Blecaco][Car] 实测眼高为负，已取绝对值")

    print(f"[Blecaco][Car] 实测垂直高度 = {_measured_eye_height:.3f} 单位")
    return True


def _raycast_ground(origin: Vector):
    """探测地面：中心点 + 邻域采样 → 拟合平面。

    返回 (相机处地面高度, 拟合平面法线) 或 (None, None)。

    流程：
      ① 沿相机局部 -Y 找到中心命中点
      ② 在中心周围按「眼高 × 比例」采样一片点（世界 -Z 平行射线）
      ③ PCA 拟合平面 → 得到连续的法线 ✓
      ④ 用平面方程算出相机所在 (x, y) 处的高度 ✓
    """
    global _ground_object

    obj = _ground_object

    if obj is None:
        obj = _find_tagged_ground()
        _ground_object = obj

    if obj is None:
        # 没有标记地面物体
        if not CAR_USE_FLAT_PLANE:
            # 默认：完全不干涉高度 ✓ 相机自由移动 ✓
            # ⚠️ 之前返回 0 会把相机拽到 EYE_HEIGHT ✗
            #    表现为"第一次前进沿世界 Z 轴跳一段" ✗
            if CAR_GROUND_DEBUG:
                print("[Blecaco][Car] 无地面标记，不干涉高度")
            return None, None

        # 可选：用固定水平面当虚拟地面
        if CAR_GROUND_DEBUG:
            print(f"[Blecaco][Car] 用水平面 z={CAR_FLAT_PLANE_Z}")
        return CAR_FLAT_PLANE_Z, Vector((0.0, 0.0, 1.0))

    if obj.name not in bpy.context.scene.objects:
        _ground_object = None
        return None, None

    # ① 中心点：沿相机局部 -Y
    scene = bpy.context.scene
    camera = scene.camera

    center_hit = None

    if camera is not None:
        try:
            direction = camera.rotation_quaternion @ Vector((0.0, -1.0, 0.0))

            inv = obj.matrix_world.inverted()
            local_origin = inv @ camera.location
            local_dir = (inv.to_3x3() @ direction).normalized()

            hit, loc, normal, idx = obj.ray_cast(
                local_origin, local_dir, distance=CAR_RAY_DISTANCE
            )

            if hit:
                center_hit = obj.matrix_world @ loc
        except Exception:
            center_hit = None

    # 中心点没打到 → 退回世界 -Z 从相机正下方找
    if center_hit is None:
        h = _raycast_object_z(obj, origin)
        if h is None:
            if CAR_GROUND_DEBUG:
                print(f"[Blecaco][Car] 在 {obj.name} 上未命中")
            return None, None
        center_hit = Vector((origin.x, origin.y, h))

    # ② 邻域采样 + ③ 拟合平面 + ④ 算相机处高度
    _center, fitted_normal, cam_height = _sample_and_fit_plane(center_hit)

    if fitted_normal is None or cam_height is None:
        # 拟合失败：用中心点的高度 + 世界竖直法线
        # ⚠️ 绝不能返回 0（那会把相机拽到地面 ✗）
        if CAR_GROUND_DEBUG:
            print(
                f"[Blecaco][Car] 平面拟合失败，用中心点高度 {center_hit.z:.3f}"
            )
        return float(center_hit.z), Vector((0.0, 0.0, 1.0))

    if CAR_GROUND_DEBUG:
        print(
            f"[Blecaco][Car] 拟合平面 高度={cam_height:.3f} "
            f"法线=({fitted_normal.x:.3f},{fitted_normal.y:.3f},{fitted_normal.z:.3f})"
        )

    return float(cam_height), fitted_normal


def _raycast_ground_height(origin: Vector):
    """只要高度（兼容旧调用）。"""
    z, _normal = _raycast_ground(origin)
    return z


def _align_quaternion_to_normal(q, normal: Vector):
    """返回「局部 Y 轴垂直于 normal」的对齐四元数（纯函数，不改物体）。

    做法：
      1) 把当前朝向(-Z)投影到地面平面 → 得到新的前方向
      2) 用 normal 作为上方向（局部 +Y）
      3) 叉乘得到右方向（局部 +X），重新正交化
      4) 构造旋转矩阵 → 转四元数
    地形太陡（超过 CAR_ALIGN_MAX_SLOPE_DEG 或法线退化）时返回原四元数。
    """
    if normal is None:
        return q

    slope_deg = math.degrees(math.acos(max(-1.0, min(1.0, normal.z))))

    if slope_deg > CAR_ALIGN_MAX_SLOPE_DEG:
        return q

    global _last_heading

    # 当前前方（-Z）
    forward = q @ Vector((0.0, 0.0, -1.0))

    # 去掉法线分量，投影到地面平面
    flat = forward - normal * forward.dot(normal)

    # ⚠️ 只有「投影退化」（相机几乎垂直俯视，投影长度趋近 0）才兜底
    #    绝不能用一个固定的世界方向 ✗ 那会造成 180° 掉头 ✗
    #    也不能用 dot<0 判定 ✗ 那会杀掉合法的掉头 ✗
    if flat.length_squared < 0.04:
        if _last_heading is not None:
            # 把上次的水平朝向再投影一次（保证在平面上）
            flat = _last_heading - normal * _last_heading.dot(normal)

        if flat.length_squared < 1e-8:
            if _last_heading is not None:
                flat = _last_heading.copy()
            else:
                flat = Vector((0.0, 1.0, 0.0)) - normal * normal.y

        if flat.length_squared < 1e-8:
            return q

    forward = flat.normalized()

    # 记录这次有效的水平朝向（下次退化时兜底用 ✓）
    _last_heading = forward.copy()

    right = forward.cross(normal)
    if right.length_squared < 1e-8:
        return q
    right.normalize()

    forward = normal.cross(right).normalized()

    from mathutils import Matrix
    mat = Matrix((
        (right.x, normal.x, -forward.x),
        (right.y, normal.y, -forward.y),
        (right.z, normal.z, -forward.z),
    ))
    target = mat.to_quaternion()

    if CAR_ALIGN_DEBUG:
        now_y = target @ Vector((0.0, 1.0, 0.0))
        ang = math.degrees(now_y.angle(normal))
        print(f"[Blecaco][Car] 贴合地形：坡度={slope_deg:.1f}° Y轴与法线夹角={ang:.2f}°")

    return target


def _apply_ground_height(camera, new_location: Vector, delta: float) -> Vector:
    """把 new_location 的 z 调整到地面 + EYE_HEIGHT（带平滑），
    并按需要让相机姿态贴合地形法线。"""
    global _ground_z

    ground, normal = _raycast_ground(new_location)

    if ground is None:
        # 探测不到地面 → 完全不改高度 ✓ 相机保持原样 ✓
        if CAR_GROUND_DEBUG:
            print("[Blecaco][Car] 无地面数据，保持当前高度")
        new_location.z = camera.location.z
        return new_location

    target_z = ground + _eye_height()

    # 平滑：避免台阶/噪声导致高度突跳
    if CAR_HEIGHT_SMOOTH_TAU <= 0.0 or _ground_z is None:
        _ground_z = target_z
    else:
        alpha = 1.0 - math.exp(-max(delta, 0.0) / CAR_HEIGHT_SMOOTH_TAU)
        _ground_z = _ground_z + (target_z - _ground_z) * alpha

    new_location.z = _ground_z

    # 记录拟合出的法线，交给转向逻辑并进目标姿态 ✓
    # （原来这里是直接改 camera 旋转 ✗ 但会被 60~120Hz 的转向覆盖 ✗）
    if CAR_ALIGN_TO_GROUND and normal is not None:
        global _ground_normal
        _ground_normal = normal

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

    global _target_location, _last_dt

    # 目标位置的起点：第一次用相机当前位置（照搬 plane 的做法 ✓）
    if _target_location is None:
        _target_location = camera.location.copy()

    new_location = _target_location + forward * distance

    if CAR_GROUND_FOLLOW:
        speed = max(abs(CAR_MOVE_SPEED_MULTIPLIER), 1e-3)
        delta = abs(distance) / speed
        _last_dt = delta
        new_location = _apply_ground_height(camera, new_location, delta)

    _target_location = new_location

    # ── 轨迹防抖：相机位置平滑逼近目标位置 ✓（照搬 plane ✓）
    #    这样即使目标位置有突变（首帧 / 拟合跳变），相机也是平滑过去的 ✓
    if CAR_PATH_SMOOTH_TAU > 0.0 and _last_dt > 1e-5:
        k = 1.0 - math.exp(-_last_dt / CAR_PATH_SMOOTH_TAU)
        camera.location = camera.location.lerp(_target_location, k)
    else:
        camera.location = _target_location.copy()


def handle_message(message):
    global _initial_camera_rotation, _steer_angle
    global _car_yaw, _last_message_time, _steer_lock
    global _steer_in_deadzone
    global _last_heading, _ground_normal, _ground_z

    data = message.get("data", {})

    # setting 通道来的地面命令是扁平结构（由 setting.py 转发）
    if not data and message.get("type") == "setting":
        data = message

    if not isinstance(data, dict):
        print("[Blecaco][Car] Invalid data")
        return

    # 兼容单独发送的 forward 消息
    if data.get("command") == "forward":
        _move_forward(float(data.get("distance", 0.1)))
        return

    # 地面标记开关：手机长按 SetGround 切换（由 setting.py 转发）
    if data.get("command") == "set_ground":
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

        # ⚠️ 必须一并清空"地形对齐"的状态 ✗
        # 否则 unlink → link 后，上一次的朝向/法线会被带过来 ✗
        # 表现就是"上次的转向数据被转给了相机" ✗
        _last_heading = None
        _ground_normal = None
        _ground_z = None

        global _target_location, _last_dt
        _target_location = camera.location.copy()   # ← 用当前相机位置做起点 ✓
        _last_dt = 0.0

        print("[Blecaco][Car] Initial camera rotation captured（转向/地形状态已清零）")
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

    # ★ 关键：把地形贴合并进目标姿态
    #   原来只在前进时对齐 ✗ 但转向每帧都把旋转重置成 base@steer ✗
    #   → 地形倾斜被覆盖 ✗ 相机永远水平 ✗
    #   所以在这里（转向之后）再对齐一次 ✓ 就没东西会覆盖它了 ✓
    if CAR_ALIGN_TO_GROUND and _ground_normal is not None:
        target = _align_quaternion_to_normal(target, _ground_normal)

    camera.rotation_quaternion = target

    # 处理同一条姿态消息中的前进量
    if forward != 0.0:
        _move_forward(forward)

# ============================================================
# 局部平面拟合（把"单个三角面法线"换成"邻域拟合平面" ✓）
#
# 为什么需要：
#   单个三角面的法线是"平的"✗ 跨过面就突变 ✗ 相机一顿一顿的
#   在命中点周围采样一片点，拟合出一个平面 → 法线连续 ✓
# ============================================================


def _raycast_object_z(obj, world_xy: Vector):
    """在世界坐标 (x, y) 处沿世界 -Z 打射线，返回命中高度（或 None）。"""
    if obj is None:
        return None

    try:
        inv = obj.matrix_world.inverted()
        world_origin = Vector((world_xy.x, world_xy.y, 0.0))
        # 起点抬到很高，保证能从上往下扫到
        world_origin.z = _eye_height() * 100.0 + CAR_RAY_DISTANCE * 0.5

        local_origin = inv @ world_origin
        local_dir = (inv.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()

        hit, loc, normal, idx = obj.ray_cast(
            local_origin, local_dir, distance=CAR_RAY_DISTANCE * 10.0
        )

        if not hit:
            return None

        return float((obj.matrix_world @ loc).z)
    except Exception:
        return None


def _fit_plane_pca(points):
    """对一组点做 PCA 平面拟合，返回 (质心, 单位法线) 或 (None, None)。

    最小特征值对应的特征向量就是平面法线（垂直距离平方和最小）。
    """
    n = len(points)

    if n < 3:
        return None, None

    # 质心
    cx = sum(p.x for p in points) / n
    cy = sum(p.y for p in points) / n
    cz = sum(p.z for p in points) / n
    center = Vector((cx, cy, cz))

    # 协方差矩阵（3×3，对称）
    xx = xy = xz = yy = yz = zz = 0.0

    for p in points:
        dx = p.x - cx
        dy = p.y - cy
        dz = p.z - cz
        xx += dx * dx
        xy += dx * dy
        xz += dx * dz
        yy += dy * dy
        yz += dy * dz
        zz += dz * dz

    # 用幂迭代法找最小特征向量（避免引入 numpy 依赖）
    # 技巧：对 (trace*I - C) 做幂迭代，最大特征向量 = C 的最小特征向量
    trace = xx + yy + zz
    axx, axy, axz = trace - xx, -xy, -xz
    ayy, ayz = trace - yy, -yz
    azz = trace - zz

    v = Vector((1.0, 1.0, 1.0))
    for _ in range(24):
        nx = axx * v.x + axy * v.y + axz * v.z
        ny = axy * v.x + ayy * v.y + ayz * v.z
        nz = axz * v.x + ayz * v.y + azz * v.z
        v = Vector((nx, ny, nz))

        length = v.length
        if length < 1e-12:
            return None, None
        v /= length

    normal = v.normalized()

    # 朝上
    if normal.z < 0.0:
        normal = -normal

    return center, normal


def _estimate_mesh_cell_size(obj) -> float:
    """粗估网格的平均边长（用来保证采样半径跨过若干个面）。"""
    try:
        mesh = obj.data

        if mesh is None or len(mesh.polygons) == 0:
            return 0.0

        # 抽样最多 200 个面，避免大网格卡顿
        step = max(1, len(mesh.polygons) // 200)

        total = 0.0
        count = 0

        for i in range(0, len(mesh.polygons), step):
            poly = mesh.polygons[i]
            verts = [mesh.vertices[vi].co for vi in poly.vertices]
            n = len(verts)

            for k in range(n):
                edge = (verts[(k + 1) % n] - verts[k]).length
                total += edge
                count += 1

        if count == 0:
            return 0.0

        # 用对角线尺度换算到世界空间（近似）
        scale = max(obj.scale.x, obj.scale.y, obj.scale.z)
        return (total / count) * scale
    except Exception:
        return 0.0


def _sample_and_fit_plane(center_world: Vector):
    """在 center_world 周围采样并拟合平面。

    返回 (平面质心, 法线, 相机处高度) 或 (None, None, None)。
    """
    obj = _ground_object

    if obj is None:
        return None, None, None

    # 采样半径 = max(眼高 × 比例, 网格平均边长 × 倍数)
    #   → 既单位无关 ✓ 又保证跨过若干个三角面 ✓（否则拟合退化成单面法线 ✗）
    radius = max(_eye_height() * CAR_SAMPLE_RADIUS_RATIO, 1e-6)

    cell = _estimate_mesh_cell_size(obj)

    if cell > 0.0:
        radius = max(radius, cell * CAR_SAMPLE_CELL_FACTOR)

    # 上限 = 眼高 × 此倍数（防止网格特别粗时半径失控 ✗）
    radius = min(radius, _eye_height() * CAR_SAMPLE_RADIUS_MAX_RATIO)

    if CAR_GROUND_DEBUG:
        print(f"[Blecaco][Car] 网格边长≈{cell:.3f} 采样半径={radius:.3f}")

    grid = max(3, int(CAR_SAMPLE_GRID))
    half = (grid - 1) * 0.5
    step = radius / half if half > 0.0 else radius

    points = []

    for ix in range(grid):
        for iy in range(grid):
            dx = (ix - half) * step
            dy = (iy - half) * step
            xy = Vector((center_world.x + dx, center_world.y + dy, 0.0))

            h = _raycast_object_z(obj, xy)

            if h is None:
                continue

            points.append(Vector((xy.x, xy.y, h)))

    if CAR_GROUND_DEBUG:
        print(f"[Blecaco][Car] 采样 {len(points)}/{grid * grid} 点 半径={radius:.3f}")

    if len(points) < CAR_MIN_VALID_SAMPLES:
        return None, None, None

    center, normal = _fit_plane_pca(points)

    if center is None:
        return None, None, None

    # 平面方程：normal · (P - center) = 0
    # → 在相机 (x, y) 处的高度：z = center.z - (n.x*dx + n.y*dy) / n.z
    if abs(normal.z) < 1e-6:
        return None, None, None

    dx = center_world.x - center.x
    dy = center_world.y - center.y
    height = center.z - (normal.x * dx + normal.y * dy) / normal.z

    return center, normal, height
