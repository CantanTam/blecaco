import math

import bpy

from . import streamer


_last_state = None
_last_clients = 0
_last_record_warning = None

# 上一次已经写入关键帧的帧号。
# 时间轴只按场景帧率前进（默认 24fps），而下面的定时器名义 100Hz；
# 没有这个记录的话，同一帧会被重复插入 3~4 次 —— 每次插入都要在
# F-Curve 里定位并标记 depsgraph，纯属浪费，录久了就变成「越录越卡」。
_last_frame = None

# 本次录制实际覆盖到的帧范围。
# 停止录制后只平滑这一段 —— 否则会动到摄像机原有的、不是这次录进去的动画，
# 而且反复录制时会把旧片段越平滑越糊。
_record_frame_min = None
_record_frame_max = None

# 录制时约每秒检查 100 次；未录制时降低检查频率
_RECORD_INTERVAL = 0.01
_IDLE_INTERVAL = 0.05

# 停止录制后，把本次生成的关键帧插值改成 LINEAR。
#   默认 False：保留 Blender 默认的 Bezier，与既有录像观感一致。
#   改成 True：回放更贴合真实运动（Bezier 会在采样点之间过冲/回摆），
#              而且 Linear 求值更快。只影响「本次录制之后」的关键帧。
_SET_LINEAR_INTERPOLATION = False

# 上一次同步给手机的输出帧率，用来检测「帧率被改了」
_last_output_fps = None

# 上一次同步给手机的推流参数（fps / quality / smooth 一组）。
# 新客户端接入时整组补发一次做「校准」（手机端以这里的值为准），
# 之后只在被改动时再发。
_last_stream_settings = None

# 上一次同步给手机的摄像机状态（列表 + 活动摄像机）。
# 新客户端接入时补发一次做基准，之后摄像机增删/改名/切活动都会再发。
_last_camera_state = None

# 上一次同步给手机的 3D 视图着色状态（X-Ray 开关 + 着色方式）。
# 同样：新客户端接入时补发一次做基准，之后只在这两个值被改动时再发 ——
# 手机按按钮、或者用户在 Blender 里自己点 X-Ray / 改着色方式，都会再发。
_last_shading_state = None


def _get_output_fps():
    """Blender 的实际输出帧率（fps / fps_base，例如 23.976）。

    取不到时返回 None。这个值就是「时间轴每秒前进几帧」，
    也就是录制关键帧的时间分辨率。
    """
    scene = bpy.context.scene

    if scene is None:
        return None

    render = scene.render

    try:
        if render.fps_base:
            return round(render.fps / render.fps_base, 3)

        return float(render.fps)
    except Exception:
        return None


def _send_output_fps(fps):
    """把输出帧率同步给手机，由它决定发送频率（并据此缩放前进步长）。"""
    success = streamer.send_setting_to_clients("output_fps", fps)

    if success:
        print(f"[blecaco] 已同步输出帧率给手机：{fps:g} fps")

    return success


# 手机能改的推流参数：setting 名 → (属性名, 类型, 最小值, 最大值)
# ⚠️ 必须和 blecaco/addon_property.py 里声明的范围保持一致。
_STREAM_SETTERS = {
    "fps": ("fps", int, 1, 30),
    "width": ("width", int, 160, 3840),
    "quality": ("quality", int, 10, 100),
    "smooth": ("smooth", float, 0.0, 1.0),
}


def _get_stream_settings():
    """读取插件面板上的四个推流参数，读不到返回 None。

    ⚠️ 顺序必须和 _send_stream_settings() 的 names 一一对应。
    """
    scene = bpy.context.scene

    if scene is None or not hasattr(scene, "blecaco"):
        return None

    props = scene.blecaco

    try:
        return (
            int(props.fps),
            int(props.width),
            int(props.quality),
            float(props.smooth),
        )
    except Exception:
        return None


def _send_stream_settings(settings):
    """把四个推流参数分别作为 fps / width / quality / smooth 发给手机。"""
    names = ("fps", "width", "quality", "smooth")
    sent = 0

    for name, value in zip(names, settings):
        if streamer.send_setting_to_clients(name, value):
            sent += 1

    if sent == len(names):
        print(
            f"[blecaco] 已同步推流参数给手机："
            f"fps={settings[0]}, width={settings[1]}, "
            f"quality={settings[2]}, smooth={settings[3]:g}"
        )

    return sent == len(names)


def _apply_stream_setting(name, value):
    """把手机发来的推流参数写进插件面板。处理成功返回 True。"""
    entry = _STREAM_SETTERS.get(name)

    if entry is None:
        return False

    attr, cast, low, high = entry
    scene = bpy.context.scene

    if scene is None or not hasattr(scene, "blecaco"):
        return False

    try:
        number = cast(value)
    except (TypeError, ValueError):
        print(f"[blecaco] {name} 的值无法解析：{value!r}")
        return False

    number = max(low, min(high, number))
    setattr(scene.blecaco, attr, number)

    print(f"[blecaco] 手机设置 {name} = {getattr(scene.blecaco, attr)}")
    return True


def _get_camera_state():
    """当前场景里所有摄像机 + 哪一台是活动的。

    返回 {"active": 名字或空串, "names": [名字...]}；取不到时返回 None。
    """
    scene = bpy.context.scene

    if scene is None:
        return None

    try:
        names = [obj.name for obj in scene.objects if obj.type == "CAMERA"]
        active = scene.camera.name if scene.camera is not None else ""
    except Exception:
        return None

    return {"active": active, "names": names}


def _send_camera_state(state):
    """把摄像机列表 + 活动摄像机同步给手机。"""
    success = streamer.send_setting_to_clients("cameras", state)

    if success:
        print(
            f"[blecaco] 已同步摄像机给手机："
            f"{len(state['names'])} 台，活动 = {state['active'] or '（无）'}"
        )

    return success


def _apply_active_camera(camera_name):
    """手机点了某台摄像机 → 把它设为活动摄像机。成功返回 True。"""
    scene = bpy.context.scene

    if scene is None or not camera_name:
        return False

    obj = scene.objects.get(str(camera_name))

    if obj is None or obj.type != "CAMERA":
        print(f"[blecaco] 找不到摄像机：{camera_name!r}")
        return False

    if scene.camera != obj:
        scene.camera = obj
        print(f"[blecaco] 活动摄像机切换为：{obj.name}")

    return True


# ==============================================================
# 3D 视图着色方式 / X-Ray
#
# 手机「着色设置页」底部那五个圆按钮对应这里：
#     XRay            → X-Ray 开关（Blender 是开关，所以走 toggle）
#     ShadingWire     → shading.type = 'WIREFRAME'
#     ShadingSolid    → shading.type = 'SOLID'
#     ShadingTexture  → shading.type = 'MATERIAL'   ← 按钮叫 Texture，
#                                                    对应 Blender 的材质预览
#     ShadingRendered → shading.type = 'RENDERED'
#
# 两个方向都是实时的：
#     手机按按钮  → handle_message → _apply_shading → 立刻回推（见那边）
#     Blender 里改 → _watch_state 每 0.1 秒发现 → 推给手机
# 新客户端（手机刚扫完二维码）接入时也会整组补发一次做基准。
# ==============================================================

# 手机能选的着色方式，取值就是 shading.type 自己的枚举名。
_SHADING_TYPES = ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED")

# 手机发的 value 用这个表示「切换 X-Ray」（和按钮名一致）。
_SHADING_XRAY = "XRAY"


def _view3d_areas():
    """当前窗口里所有 3D 视图区域。没有（例如后台跑）时返回空列表。"""
    window = bpy.context.window

    if window is None or window.screen is None:
        return []

    return [area for area in window.screen.areas if area.type == "VIEW_3D"]


def _get_shading_state():
    """当前 3D 视图的着色状态。

    返回 {"xray": 开关, "shading": "SOLID"} 这样的字典；取不到时返回 None。

    ⚠️ X-Ray 有两个开关，读哪个取决于当前着色方式（实测 Blender 5.2）：
        线框   WIREFRAME          → shading.show_xray_wireframe
        其它   SOLID/MATERIAL/…   → shading.show_xray
        bpy.ops.view3d.toggle_xray() 翻的就是这两者中「当前这个」，
        标准头部的 X-Ray 按钮显示的也是它 —— 所以手机那边要跟着读同一个，
        否则在线框模式下点 X-Ray，手机永远看不到变化。
    ⚠️ 多个 3D 视图时以第一个为准（手机按下按钮时是全都改，
       所以正常情况下它们是一致的）。
    """
    areas = _view3d_areas()

    if not areas:
        return None

    try:
        shading = areas[0].spaces.active.shading

        if str(shading.type) == "WIREFRAME":
            xray = bool(shading.show_xray_wireframe)
        else:
            xray = bool(shading.show_xray)

        return {
            "xray": xray,
            "shading": str(shading.type),
        }
    except Exception:
        return None


def _send_shading_state(state):
    """把着色状态同步给手机。"""
    success = streamer.send_setting_to_clients("shading", state)

    if success:
        print(
            f"[blecaco] 已同步着色方式给手机：{state['shading']}，"
            f"X-Ray {'开' if state['xray'] else '关'}"
        )

    return success


def _toggle_xray():
    """切换 X-Ray。成功返回 True。"""
    areas = _view3d_areas()

    if not areas:
        print("[blecaco] 当前窗口没有 3D 视图，X-Ray 没切")
        return False

    window = bpy.context.window
    toggled = 0

    # 逐个 3D 视图调用，多个视图时一起跟着手机走（和改着色方式一致）。
    for area in areas:
        region = next(
            (r for r in area.regions if r.type == "WINDOW"), None
        )

        try:
            # ⚠️ 必须补 area / region：
            #    这个操作符是从定时器里调的（不在 3D 视图自己的事件/绘制流程里），
            #    Blender 算出来的 context 里没有 3D 视图，操作符会直接 CANCELLED。
            with bpy.context.temp_override(
                window=window,
                screen=window.screen,
                area=area,
                region=region,
            ):
                result = bpy.ops.view3d.toggle_xray()
        except Exception:
            import traceback
            traceback.print_exc()
            continue

        if "FINISHED" in result:
            toggled += 1
        else:
            print(f"[blecaco] X-Ray 切换被取消：{result}")

    if toggled:
        print("[blecaco] X-Ray 已切换")

    return toggled > 0


def _apply_shading(value):
    """手机按了着色按钮 → 改 Blender 的 3D 视图。成功返回 True。"""
    if not isinstance(value, str):
        print(f"[blecaco] 着色方式的取值不对：{value!r}")
        return False

    key = value.upper()

    if key == _SHADING_XRAY:
        return _toggle_xray()

    if key not in _SHADING_TYPES:
        print(f"[blecaco] 不认识的着色方式：{value!r}")
        return False

    areas = _view3d_areas()

    if not areas:
        print("[blecaco] 当前窗口没有 3D 视图，着色方式没改")
        return False

    for area in areas:
        area.spaces.active.shading.type = key

    print(f"[blecaco] 着色方式切换为：{key}")
    return True


def _push_shading_state():
    """读一次当前着色状态并推给手机。取不到状态时什么都不做。"""
    global _last_shading_state

    state = _get_shading_state()

    if state is None:
        return

    _send_shading_state(state)
    _last_shading_state = state


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


def _note_recorded_frame(frame):
    """记录本次录制实际覆盖到的帧范围（用于只平滑这一段）。"""
    global _record_frame_min, _record_frame_max

    if _record_frame_min is None or frame < _record_frame_min:
        _record_frame_min = frame

    if _record_frame_max is None or frame > _record_frame_max:
        _record_frame_max = frame


def _record_camera_rotation():
    """录制期间把活动摄像机的姿态写入当前动画帧。

    只在「帧号发生变化」时才真正插入关键帧：
    时间轴按场景帧率前进（默认 24fps），而这个定时器名义 100Hz，
    不做这个判断的话同一帧会被重复插入 3~4 次 —— 每次插入都要在
    F-Curve 里定位并标记 depsgraph，纯属浪费；录久了曲线越来越大，
    还会让每一帧都重新求解动画，形成「越录越卡」。
    """
    global _last_record_warning, _last_frame

    # 必须同时开启 Auto Keying 和动画播放
    if not _get_state():
        return _IDLE_INTERVAL

    scene = bpy.context.scene

    if scene is None:
        return _IDLE_INTERVAL

    # 同一帧只插一次
    frame = scene.frame_current

    if frame == _last_frame:
        return _RECORD_INTERVAL

    _last_frame = frame
    _note_recorded_frame(frame)

    # 使用当前场景的活动摄像机
    camera = scene.camera

    if camera is None:
        _record_warning("当前场景没有活动摄像机")
        return _IDLE_INTERVAL

    try:
        # Blecaco 使用四元数记录旋转
        if camera.rotation_mode != 'QUATERNION':
            camera.rotation_mode = 'QUATERNION'

        # 在当前动画帧插入或更新关键帧
        rotation_inserted = camera.keyframe_insert(
            data_path="rotation_quaternion",
            frame=frame,
            group="Blecaco Camera",
        )

        location_inserted = camera.keyframe_insert(
            data_path="location",
            frame=frame,
            group="Blecaco Camera",
        )

        if not rotation_inserted or not location_inserted:
            _record_warning(
                "摄像机旋转关键帧插入失败，请检查摄像机是否可编辑"
            )
        else:
            _last_record_warning = None

    except Exception:
        import traceback
        traceback.print_exc()

    return _RECORD_INTERVAL


def _convert_to_linear():
    """把活动摄像机的关键帧插值改成 LINEAR（由 _SET_LINEAR_INTERPOLATION 控制）。

    只在停止录制时调用一次。
    注意：Blender 4.4+ 的「分层 Action」可能让 action.fcurves 取不到东西，
    所以整体包在 try 里，失败也只是打印，不影响录制流程。
    """
    scene = bpy.context.scene

    if scene is None:
        return

    camera = scene.camera

    if camera is None or camera.animation_data is None:
        return

    action = camera.animation_data.action

    if action is None:
        return

    try:
        count = 0

        for fcurve in action.fcurves:
            for point in fcurve.keyframe_points:
                point.interpolation = 'LINEAR'
                count += 1

        print(f"[blecaco] 已将 {count} 个关键帧改为 LINEAR 插值")
    except Exception:
        import traceback
        traceback.print_exc()


# ============================================================
# 关键帧曲线平滑（由 UI 的「平滑」设置项控制，停止录制时执行一次）
# ============================================================
# 用「二项式核（Pascal 三角）」做加权滑动平均：
#   radius=1 → [1,2,1]      radius=2 → [1,4,6,4,1]
#   radius=3 → [1,6,15,20,15,6,1]
# 它等价于反复做 [1,2,1] 平滑 —— 单调、无过冲，不会像等权平均那样在拐点附近振铃。
#
# 注意：对称归一化核会**完整保留线性变化**，所以 plane 模式那种「匀速前进」
# 的位移不会被改慢，只有起步/停止那几帧会被柔化。

_MAX_SMOOTH_RADIUS = 4


def _binomial_weights(radius):
    """半径 radius 的归一化二项式权重，长度 2*radius+1。"""
    weights = [math.comb(2 * radius, k) for k in range(2 * radius + 1)]
    total = sum(weights)

    return [w / total for w in weights]


def _align_quaternion_signs(curves):
    """让 4 条四元数曲线在相邻帧之间同号。

    q 与 -q 表示同一个旋转。不做这一步，直接对 4 条曲线各自平滑，
    会在符号翻转处平均出接近 0 的四元数 → 画面瞬间乱转。
    """
    if len(curves) != 4:
        return

    count = len(curves[0].keyframe_points)

    for i in range(1, count):
        dot = 0.0

        for curve in curves:
            dot += (
                curve.keyframe_points[i - 1].co.y
                * curve.keyframe_points[i].co.y
            )

        if dot < 0.0:
            for curve in curves:
                curve.keyframe_points[i].co.y *= -1.0


def _normalize_quaternion_curves(curves, radius, frame_lo, frame_hi):
    """平滑后把本次录制区间内的四元数重新归一化。"""
    count = len(curves[0].keyframe_points)

    for i in range(radius, count - radius):
        frame = curves[0].keyframe_points[i].co.x

        if frame < frame_lo - 0.5 or frame > frame_hi + 0.5:
            continue

        length = 0.0

        for curve in curves:
            length += curve.keyframe_points[i].co.y ** 2

        length = math.sqrt(length)

        if length < 1e-9:
            continue

        for curve in curves:
            curve.keyframe_points[i].co.y /= length


def _smooth_fcurve(fcurve, weights, radius, frame_lo, frame_hi):
    """对一条 F-Curve 做加权滑动平均，返回实际处理点数。

    只写「本次录制覆盖到的帧」（frame_lo ~ frame_hi），其余关键帧一律不碰 ——
    否则会破坏摄像机原有的、不是这次录进去的动画，反复录制时还会把旧片段
    越平滑越糊。另外首尾各 radius 帧因为邻居不足也不处理。
    """
    points = fcurve.keyframe_points
    count = len(points)

    if count < radius * 2 + 1:
        return 0

    values = [point.co.y for point in points]
    frames = [point.co.x for point in points]
    processed = 0

    for i in range(radius, count - radius):
        if frames[i] < frame_lo - 0.5 or frames[i] > frame_hi + 0.5:
            continue

        total = 0.0

        for offset, weight in enumerate(weights):
            total += values[i - radius + offset] * weight

        points[i].co.y = total
        processed += 1

    return processed


def _smooth_camera_curves(scene):
    """按「平滑」设置项，对活动摄像机的关键帧曲线做一次加权滑动平均。"""
    props = getattr(scene, "blecaco", None)

    if props is None:
        return

    strength = float(getattr(props, "smooth", 0.0))

    if strength <= 0.0:
        return

    # 只平滑「本次录制真正覆盖到的帧」。没有记录到范围说明这次没录进任何关键帧。
    if _record_frame_min is None or _record_frame_max is None:
        print("[blecaco] 本次录制没有写入关键帧，跳过平滑")
        return

    frame_lo = _record_frame_min
    frame_hi = _record_frame_max

    camera = scene.camera

    if camera is None or camera.animation_data is None:
        return

    action = camera.animation_data.action

    if action is None:
        return

    # 强度 → 平滑半径（1 ~ _MAX_SMOOTH_RADIUS）
    radius = 1 + int(round(strength * (_MAX_SMOOTH_RADIUS - 1)))
    weights = _binomial_weights(radius)

    try:
        rotation_curves = {}
        others = []

        for fcurve in action.fcurves:
            if fcurve.data_path == "rotation_quaternion":
                rotation_curves[fcurve.array_index] = fcurve
            else:
                others.append(fcurve)

        processed = 0

        # 旋转：符号对齐 → 4 条用同一组权重平滑 → 重新归一化
        if len(rotation_curves) == 4:
            curves = [rotation_curves[i] for i in range(4)]

            _align_quaternion_signs(curves)

            for curve in curves:
                processed += _smooth_fcurve(
                    curve, weights, radius, frame_lo, frame_hi
                )

            _normalize_quaternion_curves(curves, radius, frame_lo, frame_hi)

        # 其它通道（location 等）直接平滑
        for fcurve in others:
            processed += _smooth_fcurve(
                fcurve, weights, radius, frame_lo, frame_hi
            )

        # 让 Blender 重算贝塞尔手柄，否则曲线看起来没变化
        for fcurve in action.fcurves:
            fcurve.update()

        print(
            f"[blecaco] 曲线平滑完成：强度 {strength:.2f} → "
            f"半径 {radius}（{len(weights)} 点），"
            f"帧范围 {frame_lo:.0f} ~ {frame_hi:.0f}，"
            f"处理 {processed} 个关键帧"
        )
    except Exception:
        import traceback
        traceback.print_exc()


def _send_state(value):
    print(f"[blecaco] 同步 recording 状态到手机：{value}")
    success = streamer.send_setting_to_clients("record", value)

    if not success:
        print("[blecaco] recording 状态发送失败")


def handle_message(data):
    """处理手机发来的设置：record 开关 + 推流参数 + 活动摄像机。"""
    global _last_state, _last_frame
    global _record_frame_min, _record_frame_max
    global _last_stream_settings, _last_camera_state

    if data.get("type") != "setting":
        return

    name = data.get("name")

    # 推流参数（fps / quality / smooth）：先写进插件面板。
    # 顺手清掉「上次发过什么」的记录，下一轮 _watch_state 就会把新值
    # 回执给手机 —— 一方面确认 Blender 收到了，另一方面两边不至于跑偏。
    if _apply_stream_setting(name, data.get("value")):
        _last_stream_settings = None
        return

    # 手机打开摄像机页时会来要一份列表（value 是 null）→ 立刻回推，
    # 不等下一次 _watch_state 轮询，这样「打开页面就能看到」是确定的。
    if name == "cameras":
        camera_state = _get_camera_state()

        if camera_state is not None:
            _send_camera_state(camera_state)
            _last_camera_state = camera_state

        return

    # 活动摄像机：手机在摄像机列表里点了某一台
    if name == "active_camera":
        if _apply_active_camera(data.get("value")):
            # 同样清掉记录，下一轮把新的摄像机状态回执给手机
            _last_camera_state = None

        return

    # 着色方式 / X-Ray：手机按了着色设置页底部的圆按钮。
    #
    # ⚠️ 处理完**立刻**回推一次（不等 _watch_state 下一轮）：
    #    手机那边不做乐观更新，按钮状态完全跟着 Blender 走，
    #    立刻回推才能保证「按下去之后按钮确实是这个状态」是确定的
    #    （例如没有 3D 视图、操作被取消时，手机也能马上回到真实状态）。
    if name == "shading":
        value = data.get("value")

        # value 为 null = 手机打开着色页时来要一份当前状态 → 只回推，不改任何东西
        if value is not None:
            _apply_shading(value)

        _push_shading_state()
        return

    if name != "record":
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

        if value:
            # 关键帧的时间分辨率 = 场景的输出帧率，而不是上面那个 100Hz 的定时器。
            # 24fps 去录 60Hz 的手持姿态，快速转动时会明显一跳一跳。
            _last_frame = None

            # 重置本次录制的帧范围，停止时只平滑这一段
            _record_frame_min = None
            _record_frame_max = None

            render = scene.render
            effective_fps = (
                render.fps / render.fps_base
                if render.fps_base else float(render.fps)
            )

            print(
                f"[blecaco] 开始录制：输出帧率 {effective_fps:g} fps"
                "（关键帧的时间分辨率就是它；手持快速转动建议提到 60）"
            )
        else:
            # 停止录制 → 对本次生成的曲线做后处理。
            # 先平滑（改数值），再改插值类型（不改数值）。
            _smooth_camera_curves(scene)

            if _SET_LINEAR_INTERPOLATION:
                _convert_to_linear()

    except Exception:
        import traceback
        traceback.print_exc()

    _last_state = _get_state()
    _send_state(_last_state)


def _watch_state():
    # ⚠️ 下面每一条被赋值的全局都要在这里声明，漏一个就会在运行时抛
    #    UnboundLocalError —— Blender 会把出错定时器直接注销，
    #    于是所有「主动推送」集体静默失效（请求-响应因为不走这里，看起来还是正常的）。
    global _last_state, _last_clients, _last_output_fps
    global _last_stream_settings, _last_camera_state, _last_shading_state

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

    # 输出帧率：新客户端接入时补发一次，之后只在它被改动时发。
    # 手机端据此决定发送频率，所以必须在开始操作前就同步过去。
    output_fps = _get_output_fps()

    if clients > 0 and output_fps is not None and (
        output_fps != _last_output_fps or clients != _last_clients
    ):
        _send_output_fps(output_fps)
        _last_output_fps = output_fps

    # 推流参数（fps / quality / smooth）：新客户端接入时整组补发一次，
    # 这就是手机端的「校准」——它收到后会覆盖自己那份设置。
    stream_settings = _get_stream_settings()

    if clients > 0 and stream_settings is not None and (
        stream_settings != _last_stream_settings or clients != _last_clients
    ):
        _send_stream_settings(stream_settings)
        _last_stream_settings = stream_settings

    # 摄像机（列表 + 活动摄像机）：同样是新客户端接入时补发一次做基准。
    # 摄像机被增删 / 改名 / 换活动，这个 state 都会变，所以手机那边
    # 不需要额外请求就能实时更新。
    camera_state = _get_camera_state()

    if clients > 0 and camera_state is not None and (
        camera_state != _last_camera_state or clients != _last_clients
    ):
        _send_camera_state(camera_state)
        _last_camera_state = camera_state

    # 3D 视图着色方式 / X-Ray：同样是新客户端接入时补发一次做基准。
    # 这就是需求里那句「手机扫完二维码先以 Blender 状态为准同步一次」。
    #
    # 之后用户在 Blender 里自己点 X-Ray / 改着色方式，这个 state 就会变，
    # 手机那边不需要额外请求就能实时跟上（0.1 秒一轮）。
    shading_state = _get_shading_state()

    if clients > 0 and shading_state is not None and (
        shading_state != _last_shading_state or clients != _last_clients
    ):
        _send_shading_state(shading_state)
        _last_shading_state = shading_state

    _last_state = state
    _last_clients = clients

    return 0.1


def register():
    global _last_state, _last_clients, _last_record_warning, _last_frame
    global _last_output_fps, _last_stream_settings, _last_camera_state
    global _last_shading_state

    _last_state = None
    _last_clients = 0
    _last_record_warning = None
    _last_frame = None
    _last_output_fps = None
    _last_stream_settings = None
    _last_camera_state = None
    _last_shading_state = None

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