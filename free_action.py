import bpy
from mathutils import Quaternion, Vector


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
                flow_x = float(data.get("x", 0.0))
                flow_y = float(data.get("y", 0.0))
                radial = float(data.get("z", 0.0))
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