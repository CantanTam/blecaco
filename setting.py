import bpy

from . import streamer


_last_state = None
_last_clients = 0
_last_record_warning = None

# 录制时约每秒检查 100 次；未录制时降低检查频率
_RECORD_INTERVAL = 0.01
_IDLE_INTERVAL = 0.05


def _get_state():
    scene = bpy.context.scene
    window = bpy.context.window

    if scene is None or window is None or window.screen is None:
        return False

    return bool(
        scene.tool_settings.use_keyframe_insert_auto
        and window.screen.is_animation_playing
    )


def _record_warning(message):
    """相同错误只打印一次，避免控制台刷屏。"""
    global _last_record_warning

    if message != _last_record_warning:
        print(f"[blecaco] 关键帧记录提示：{message}")
        _last_record_warning = message


def _record_camera_rotation():
    """录制期间持续将活动摄像机的旋转写入当前动画帧。"""
    global _last_record_warning

    # 必须同时开启 Auto Keying 和动画播放
    if not _get_state():
        return _IDLE_INTERVAL

    scene = bpy.context.scene

    if scene is None:
        return _IDLE_INTERVAL

    # 使用当前场景的活动摄像机
    camera = scene.camera

    if camera is None:
        _record_warning("当前场景没有活动摄像机")
        return _IDLE_INTERVAL

    try:
        # Blecaco 使用四元数记录旋转
        if camera.rotation_mode != 'QUATERNION':
            camera.rotation_mode = 'QUATERNION'

        # 在当前动画帧插入或更新旋转关键帧
        inserted = camera.keyframe_insert(
            data_path="rotation_quaternion",
            frame=scene.frame_current,
            group="Blecaco Free",
        )

        if not inserted:
            _record_warning(
                "摄像机旋转关键帧插入失败，请检查摄像机是否可编辑"
            )
        else:
            _last_record_warning = None

    except Exception:
        import traceback
        traceback.print_exc()

    return _RECORD_INTERVAL


def _send_state(value):
    print(f"[blecaco] 同步 recording 状态到手机：{value}")
    success = streamer.send_setting_to_clients("record", value)

    if not success:
        print("[blecaco] recording 状态发送失败")


def handle_message(data):
    """处理手机发来的 record 设置。"""
    global _last_state

    if data.get("type") != "setting" or data.get("name") != "record":
        return

    value = data.get("value")

    if not isinstance(value, bool):
        return

    scene = bpy.context.scene
    window = bpy.context.window

    if scene is None or window is None or window.screen is None:
        print("[blecaco] 无法处理 recording：没有可用窗口")
        return

    try:
        # Recording 按钮负责启停，不直接处理关键帧
        scene.tool_settings.use_keyframe_insert_auto = value

        if window.screen.is_animation_playing != value:
            with bpy.context.temp_override(
                window=window,
                screen=window.screen,
            ):
                bpy.ops.screen.animation_play()

    except Exception:
        import traceback
        traceback.print_exc()

    _last_state = _get_state()
    _send_state(_last_state)


def _watch_state():
    global _last_state, _last_clients

    state = _get_state()

    scene = bpy.context.scene
    window = bpy.context.window

    auto_key = (
        scene.tool_settings.use_keyframe_insert_auto
        if scene is not None else None
    )

    playing = (
        window.screen.is_animation_playing
        if window is not None and window.screen is not None
        else None
    )

    if state != _last_state:
        print(
            f"[blecaco] 状态诊断："
            f"auto_key={auto_key}, "
            f"playing={playing}, "
            f"scene={scene is not None}, "
            f"window={window is not None}"
        )

    try:
        clients = streamer.get_status()["clients"]
    except Exception:
        clients = 0

    # 新客户端连接或录制状态变化时，同步给手机
    if clients > 0 and (
        state != _last_state or clients != _last_clients
    ):
        _send_state(state)

    _last_state = state
    _last_clients = clients

    return 0.1


def register():
    global _last_state, _last_clients, _last_record_warning

    _last_state = None
    _last_clients = 0
    _last_record_warning = None

    # 录制状态监测
    if not bpy.app.timers.is_registered(_watch_state):
        bpy.app.timers.register(
            _watch_state,
            first_interval=0.1,
            persistent=True,
        )

    # 独立的摄像机关键帧记录器
    if not bpy.app.timers.is_registered(_record_camera_rotation):
        bpy.app.timers.register(
            _record_camera_rotation,
            first_interval=_RECORD_INTERVAL,
            persistent=True,
        )

    print("[blecaco] recording 状态监测已启动")
    print("[blecaco] 摄像机关键帧记录器已启动")


def unregister():
    if bpy.app.timers.is_registered(_watch_state):
        bpy.app.timers.unregister(_watch_state)

    if bpy.app.timers.is_registered(_record_camera_rotation):
        bpy.app.timers.unregister(_record_camera_rotation)

    print("[blecaco] recording 状态监测已停止")
    print("[blecaco] 摄像机关键帧记录器已停止")