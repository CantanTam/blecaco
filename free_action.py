import bpy
from math import cos, radians, sin
from mathutils import Matrix, Quaternion, Vector


# ============================================================
# Free Mode 参数
# ============================================================

# 平移比例：
# 1.0 = 默认比例
# 后续可以在 Blender 插件面板中添加可调选项。
move_scale_ratio: float = 10.0


#: OpenVINS 输出的是**米**。注意这里会和 move_scale_ratio 相乘：
#:   有效比例 = METER_TO_BLENDER_UNITS × move_scale_ratio
#: 默认 0.1 × 10 = **1.0** → 手机移动 1 米 = Blender 里移动 1 单位（1:1，手感直观）。
#: 想要更灵敏/更迟钝，改这个值或全局的 move_scale_ratio 都行。
#: 光流那条路仍走 FLOW_PIXEL_TO_BLENDER_UNITS（像素→Blender 单位），互不影响。
METER_TO_BLENDER_UNITS: float = 0.1

# ============================================================
# ★★ OpenVINS 相机系 → Blender 相机局部系 的 3x3 映射矩阵 ★★
# ============================================================
# 行 = Blender 相机局部轴（X 右 / Y 上 / Z 后）
# 列 = OpenVINS 相机系轴（x 右 / y 下 / z 前，单位米，机体系帧间增量）
#
# 每个元素只能是 0 / +1 / -1 之类，方便表达「换轴」和「翻符号」两种修正：
#   例：若发现 Blender 的 Y 应该来自 OpenVINS 的 z 且要取反 →
#       第一行不变，第二行改成 (0.0, 0.0, -1.0)
#
# 当前值 = 单位矩阵（等价于"原样透传"）。
# 说明：本 App **限定横屏**使用（横屏才能扫二维码），所以这里按你实测的
#      横屏持机方向标定一次即可，不需要支持竖屏。
METER_CAM_TO_BLENDER: tuple = (
    (1.0,  0.0, 0.0),     # Blender X ← +cam.x
    (0.0, -1.0, 0.0),     # Blender Y ← -cam.y
    (0.0,  0.0, -1.0),    # Blender Z ← -cam.z
)

# 怎么来的（推导，2026-10-17）：
#   ① 用户给的「手机屏幕系 → Blender 相机系」：
#        camera.x = -phone.y ; camera.y = +phone.x ; camera.z = +phone.z
#   ② OpenVINS 发来的是**图像(相机)系**，与手机屏幕系的关系：
#        图像 z 指向场景内  = -phone.z        （屏幕 +z 指向用户）
#       横屏(sensorOrientation=90)下 图像 x/y 与屏幕 x/y 交换并带符号
#   ③ 合成（det=+1 的纯旋转）得到上表：cam.x→+X, cam.y→-Y, cam.z→-Z
#      这也正好是「OpenCV 相机系 → Blender 相机局部系」的标准换算。
# ============================================================
# ★★★ 若方向仍不对：只改下面这一个数（0 / 90 / 180 / 270）★★★
# ============================================================
# 为什么需要它：手机侧发给 Blender 的是 **OpenVINS 相机(图像)系** 的位移
# （图像系：x 右 / y 下 / z 前），而上面那张表是 **手机屏幕系 → Blender 相机系**。
# 横屏使用时，图像系与屏幕系相差一个 90° 的整数倍（取决于相机 sensorOrientation
# 与当时的屏幕旋转方向），所以先绕 z 轴把这个角度转掉，再套上表。
#
# 用法：默认 0。若方向不对，依次试 90 / 180 / 270（改完在 Blender 里重载插件即可，
# 不需要重编/重装手机 App）。四个值覆盖全部可能。
METER_PRE_ROTATE_DEG: float = 0.0

# 只在第一条米制消息时提示一次当前映射，方便排查
_meter_map_logged = False

# 光流像素位移转换为 Blender 位移单位的基础比例。
# 最终移动量 = 光流位移 × 此值 × move_scale_ratio
FLOW_PIXEL_TO_BLENDER_UNITS: float = 0.005

# 打开后会以约 100Hz 往控制台刷每条四元数，明显拖慢 Blender 主线程；
# 与 GDScript 侧的 VERBOSE_ORIENTATION_LOG / VERBOSE_SEND_LOG 同理，排查时再改 True。
VERBOSE_ROTATION_LOG: bool = False


# ============================================================
# Free Mode 状态
# ============================================================

# 进入 Free Mode 时记录的摄像机初始旋转。
_initial_camera_rotation = None

#: LinkButton 切到 unlink 时保存的**相机**旋转四元数。
#: 重新 link 时把它作为新的「初始相机旋转」基准，这样手机在被暂停期间怎么转，
#: 恢复后相机都不会跳 —— 只把手机「当前姿态」当成新的零点。
_saved_camera_rotation = None


# ============================================================
# 坐标转换
# ============================================================

def _phone_motion_to_camera_local(
    phone_motion: Vector,
) -> Vector:
    """
    将手机坐标系中的位移向量转换为 Blender Camera 局部坐标系。

    根据确定的六条轴向关系：

    Camera -X -> Phone +Y
    Camera +X -> Phone -Y
    Camera +Y -> Phone +X
    Camera -Y -> Phone -X
    Camera +Z -> Phone +Z
    Camera -Z -> Phone -Z

    因此：

    Camera X = -Phone Y
    Camera Y =  Phone X
    Camera Z =  Phone Z
    """

    return Vector((
        -phone_motion.y,
        phone_motion.x,
        phone_motion.z,
    ))


# ============================================================
# 消息入口
# ============================================================

def handle_message(message):
    global _initial_camera_rotation
    global _saved_camera_rotation

    data = message.get("data", {})

    if not isinstance(data, dict):
        print("[Blecaco][Free] Invalid data")
        return

    # --------------------------------------------------------
    # 1. 初始化：记录当前活动摄像机的初始旋转
    # --------------------------------------------------------

    if data.get("command") == "initialize":
        camera = bpy.context.scene.camera

        if camera is None:
            print("[Blecaco][Free] No active camera")
            return

        camera.rotation_mode = 'QUATERNION'

        _initial_camera_rotation = (
            camera.rotation_quaternion.copy()
        )

        print(
            "[Blecaco][Free] Initial camera rotation captured"
        )
        return

    # --------------------------------------------------------
    # 1.1 LinkButton 切到 unlink：先记下相机当前的旋转
    #     （手机端会在**暂停发送之前**发这条，否则会被 action 门控丢掉）
    # --------------------------------------------------------

    if data.get("command") == "unlink":
        camera = bpy.context.scene.camera

        if camera is None:
            print("[Blecaco][Free] No active camera")
            return

        camera.rotation_mode = 'QUATERNION'
        _saved_camera_rotation = camera.rotation_quaternion.copy()

        print(
            "[Blecaco][Free] Unlink: saved camera rotation = "
            f"X={_saved_camera_rotation.x:.4f}, "
            f"Y={_saved_camera_rotation.y:.4f}, "
            f"Z={_saved_camera_rotation.z:.4f}, "
            f"W={_saved_camera_rotation.w:.4f}"
        )
        return

    # --------------------------------------------------------
    # 1.2 LinkButton 切回 link：用保存的相机旋转当新的初始基准
    #     （手机端此刻已重新校准，之后发来的旋转是「相对当前姿态」的）
    # --------------------------------------------------------

    if data.get("command") == "relink":
        camera = bpy.context.scene.camera

        if camera is None:
            print("[Blecaco][Free] No active camera")
            return

        camera.rotation_mode = 'QUATERNION'

        if _saved_camera_rotation is None:
            # 没保存过（例如没走 unlink 就直接恢复）：退化成以相机当前旋转为基准
            _initial_camera_rotation = camera.rotation_quaternion.copy()
            print(
                "[Blecaco][Free] Relink: no saved rotation, "
                "using current camera rotation as baseline"
            )
            return

        _initial_camera_rotation = _saved_camera_rotation.copy()
        camera.rotation_quaternion = _initial_camera_rotation.copy()

        print(
            "[Blecaco][Free] Relink: camera rotation restored to saved baseline "
            f"X={_initial_camera_rotation.x:.4f}, "
            f"Y={_initial_camera_rotation.y:.4f}, "
            f"Z={_initial_camera_rotation.z:.4f}, "
            f"W={_initial_camera_rotation.w:.4f}"
        )
        return

    # --------------------------------------------------------
    # 2. 光流平移
    # --------------------------------------------------------

    if data.get("command") == "translation":
        camera = bpy.context.scene.camera

        if camera is None:
            print(
                "[Blecaco][Free] No active camera in current scene"
            )
            return

        # 位姿来源：光流（像素，默认） / OpenVINS（米）
        # 手机端切到 OpenVINS 时会带 "unit": "meter"；不带该字段 = 老的光流路径。
        unit = str(data.get("unit", "pixel"))

        try:
            if unit == "meter":
                # OpenVINS：机体系下的米制**相对**位移（x 右 / y 下 / z 前）
                meter_x = float(data.get("x", 0.0))
                meter_y = float(data.get("y", 0.0))
                meter_z = float(data.get("z", 0.0))
            else:
                # 光流图像中的特征运动量，单位为像素。
                flow_x = float(data["x"])
                flow_y = float(data["y"])

                # 径向光流：
                # 正值表示画面整体扩张；
                # 负值表示画面整体收缩。
                radial = float(data["radial"])

        except (KeyError, TypeError, ValueError):
            print(
                "[Blecaco][Free] Invalid optical flow data"
            )
            return

        # ----------------------------------------------------
        # 2.1 将图像运动近似转换为手机的平移方向
        # ----------------------------------------------------
        #
        # 此处假设：
        # - flow_x > 0：图像特征向右移动
        # - flow_y > 0：图像特征向下移动
        # - radial > 0：图像向外扩张
        #
        # 对静止场景而言，图像运动方向通常与相机平移方向相反。
        #
        # 手机横屏且图像已经 auto_upright 时，
        # 这里暂按输出图像 X/Y 对应手机 X/Y 的方式估算。
        # 实际方向仍需通过手机移动测试验证。

        if unit == "meter":
            # 米制路径：直接逐轴映射到 Blender 相机局部系。
            # 注意**不能**套用上面光流那套「图像运动与相机平移相反」的处理 ——
            # 光流给的是图像特征位移，OpenVINS 给的是相机本身的位移，语义完全不同。
            global _meter_map_logged
            if not _meter_map_logged:
                _meter_map_logged = True
                print(
                    "[Blecaco][Free] meter 映射 = "
                    f"{METER_CAM_TO_BLENDER} 预旋转 = {METER_PRE_ROTATE_DEG}°"
                )

            v_cam = Vector((meter_x, meter_y, meter_z))

            # 图像系 → 屏幕系：绕 z 轴预旋转（METER_PRE_ROTATE_DEG）
            if METER_PRE_ROTATE_DEG:
                rad = radians(METER_PRE_ROTATE_DEG)
                c, s_ = cos(rad), sin(rad)
                v_cam = Vector((
                    v_cam.x * c - v_cam.y * s_,
                    v_cam.x * s_ + v_cam.y * c,
                    v_cam.z,
                ))

            # 屏幕系 → Blender 相机局部系（用户实测的固定对应关系）
            camera_local_motion = Matrix(METER_CAM_TO_BLENDER) @ v_cam

            camera_local_motion *= (
                METER_TO_BLENDER_UNITS
                * move_scale_ratio
            )

            camera_world_rotation = (
                camera.matrix_world.to_quaternion()
            )

            world_motion = (
                camera_world_rotation
                @ camera_local_motion
            )

            if camera.parent is not None:
                parent_3x3 = (
                    camera.parent.matrix_world
                    @ camera.matrix_parent_inverse
                ).to_3x3()
                camera.location = (
                    camera.location
                    + (parent_3x3.inverted() @ world_motion)
                )
            else:
                camera.location = camera.location + world_motion

            return

        phone_motion = Vector((
            flow_y,
            flow_x,
            -radial,
        ))

        # ----------------------------------------------------
        # 2.2 按指定的轴向关系转换到 Blender Camera 坐标系
        # ----------------------------------------------------

        camera_local_motion = (
            _phone_motion_to_camera_local(phone_motion)
        )

        # ----------------------------------------------------
        # 2.3 缩放位移
        # ----------------------------------------------------

        camera_local_motion *= (
            (
                METER_TO_BLENDER_UNITS
                if unit == "meter"
                else FLOW_PIXEL_TO_BLENDER_UNITS
            )
            * move_scale_ratio
        )

        # ----------------------------------------------------
        # 2.4 将摄像机局部位移转换到世界坐标系
        # ----------------------------------------------------

        camera_world_rotation = (
            camera.matrix_world.to_quaternion()
        )

        world_motion = (
            camera_world_rotation
            @ camera_local_motion
        )

        # 只改 location，绝不写 matrix_world。
        #
        # matrix_world = ... 会把 location / rotation / scale 三个通道一起重写，
        # 而 rotation 正被另一条约 100Hz 的旋转消息流写入；且 matrix_world 是
        # depsgraph 求值时才刷新的缓存，读到的旋转是滞后的，于是位移消息会把
        # 刚写入的旋转覆盖回旧值 —— 两条流互相覆盖，旋转就会来回跳。
        # （plane_action.py 的 forward 分支是同一处修复。）
        if camera.parent is not None:
            # 有父级：把世界位移换算回 location 所在的父空间
            parent_3x3 = (
                camera.parent.matrix_world
                @ camera.matrix_parent_inverse
            ).to_3x3()

            camera.location = (
                camera.location
                + (parent_3x3.inverted() @ world_motion)
            )
        else:
            # 无父级：location 就是世界坐标，直接加世界位移
            camera.location = camera.location + world_motion

        return

    # --------------------------------------------------------
    # 3. 手机相对旋转
    # --------------------------------------------------------

    try:
        x = float(data["x"])
        y = float(data["y"])
        z = float(data["z"])
        w = float(data["w"])

    except (KeyError, TypeError, ValueError):
        print(
            "[Blecaco][Free] Missing or invalid quaternion values"
        )
        return

    # 避免无效的零四元数。
    if (w * w + x * x + y * y + z * z) < 1e-16:
        print("[Blecaco][Free] Invalid zero quaternion")
        return

    rotation = Quaternion((w, x, y, z))
    rotation.normalize()

    camera = bpy.context.scene.camera

    if camera is None:
        print(
            "[Blecaco][Free] No active camera in current scene"
        )
        return

    camera.rotation_mode = 'QUATERNION'

    # 兼容尚未收到初始化指令的情况。
    if _initial_camera_rotation is None:
        _initial_camera_rotation = (
            camera.rotation_quaternion.copy()
        )

    # 保持已经测试正常的旋转公式不变。
    camera.rotation_quaternion = (
        _initial_camera_rotation @ rotation
    )

    if VERBOSE_ROTATION_LOG:
        print(
            "[Blecaco][Free] Camera rotation updated: "
            f"X={camera.rotation_quaternion.x:.4f}, "
            f"Y={camera.rotation_quaternion.y:.4f}, "
            f"Z={camera.rotation_quaternion.z:.4f}, "
            f"W={camera.rotation_quaternion.w:.4f}"
        )