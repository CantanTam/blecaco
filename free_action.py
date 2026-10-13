import bpy
from mathutils import Quaternion, Vector


# ============================================================
# Free Mode 参数
# ============================================================

# 平移比例：手机真实位移（米）→ Blender 世界位移 的比例。
# 1.0 = 1:1（手机走 1 米，Blender 摄像机走 1 米）。
# 后续可以在 Blender 插件面板中添加可调选项。
move_scale_ratio: float = 1.0


# ============================================================
# Free Mode 状态
# ============================================================

# 进入 Free Mode 时记录的摄像机初始旋转。
_initial_camera_rotation = None


# ============================================================
# 坐标转换
# ============================================================

def _phone_body_to_camera_local(
    body_motion: Vector,
) -> Vector:
    """
    将「手机机体坐标系」下的位移向量（米，来自 OpenVINS）转换为
    Blender Camera 局部坐标系。

    手机机体坐标系 = Android 传感器坐标系：
        X 右 / Y 上 / Z 朝屏幕外

    根据 getrotation.gd 使用的六条轴向关系：

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
        -body_motion.y,
        body_motion.x,
        body_motion.z,
    ))


# ============================================================
# 消息入口
# ============================================================

def handle_message(message):
    global _initial_camera_rotation

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
    # 2. 光流平移
    # --------------------------------------------------------

    if data.get("command") == "translation":
        camera = bpy.context.scene.camera

        if camera is None:
            print(
                "[Blecaco][Free] No active camera in current scene"
            )
            return

        try:
            # 手机机体坐标系下的位移增量，单位：米
            # （由手机端 OpenVINS 提供，已是真实尺度）
            body_x = float(data["x"])
            body_y = float(data["y"])
            body_z = float(data.get("z", 0.0))

        except (KeyError, TypeError, ValueError):
            print(
                "[Blecaco][Free] Invalid translation data"
            )
            return

        # ----------------------------------------------------
        # 2.1 手机机体坐标系 -> Blender Camera 局部坐标系
        # ----------------------------------------------------

        camera_local_motion = _phone_body_to_camera_local(
            Vector((body_x, body_y, body_z))
        )

        # ----------------------------------------------------
        # 2.2 缩放位移（单位已是米，默认 1:1）
        # ----------------------------------------------------

        camera_local_motion *= move_scale_ratio

        # ----------------------------------------------------
        # 2.3 将摄像机局部位移转换到世界坐标系
        # ----------------------------------------------------

        camera_world_rotation = (
            camera.matrix_world.to_quaternion()
        )

        world_motion = (
            camera_world_rotation
            @ camera_local_motion
        )

        # 更新世界坐标位置。
        # 使用 matrix_world，兼容摄像机存在父对象的情况。
        world_matrix = camera.matrix_world.copy()
        world_matrix.translation += world_motion
        camera.matrix_world = world_matrix

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

    print(
        "[Blecaco][Free] Camera rotation updated: "
        f"X={camera.rotation_quaternion.x:.4f}, "
        f"Y={camera.rotation_quaternion.y:.4f}, "
        f"Z={camera.rotation_quaternion.z:.4f}, "
        f"W={camera.rotation_quaternion.w:.4f}"
    )