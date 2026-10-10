
import bpy

from . import streamer


_last_state = None
_last_clients = 0


def _get_state():
    scene = bpy.context.scene
    window = bpy.context.window

    if scene is None or window is None or window.screen is None:
        return False

    return bool(
        scene.tool_settings.use_keyframe_insert_auto
        and window.screen.is_animation_playing
    )


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

    # 新客户端连接时发送当前状态；任一功能状态变化时也发送
    if clients > 0 and (
        state != _last_state or clients != _last_clients
    ):
        _send_state(state)

    _last_state = state
    _last_clients = clients

    return 0.1


def register():
    global _last_state, _last_clients

    _last_state = None
    _last_clients = 0

    if not bpy.app.timers.is_registered(_watch_state):
        bpy.app.timers.register(
            _watch_state,
            first_interval=0.1,
            persistent=True,
        )

    print("[blecaco] recording 状态监测已启动")


def unregister():
    if bpy.app.timers.is_registered(_watch_state):
        bpy.app.timers.unregister(_watch_state)

    print("[blecaco] recording 状态监测已停止")
