import bpy
from . import streamer

_qr_previews = None
_qr_loaded_url = None

def _load_qr_icon(url):
    """加载二维码为 previews 图标，返回 icon_id 或 None。"""
    global _qr_previews, _qr_loaded_url

    if not url:
        return None

    if _qr_previews is None:
        _qr_previews = bpy.utils.previews.new()

    # 命中缓存，直接返回
    if _qr_loaded_url == url and "qr" in _qr_previews:
        return _qr_previews["qr"].icon_id

    # URL 变了，清空整个 collection
    try:
        _qr_previews.clear()
    except Exception:
        pass

    path = streamer.get_qr_path(url)
    if not path:
        return None

    try:
        _qr_previews.load("qr", path, "IMAGE")
    except Exception as e:
        print(f"[blecaco] 二维码加载失败: {e}")
        return None

    _qr_loaded_url = url
    return _qr_previews["qr"].icon_id

def _clear_qr_previews():
    global _qr_previews, _qr_loaded_url
    if _qr_previews is not None:
        try:
            bpy.utils.previews.remove(_qr_previews)
        except Exception:
            pass
        _qr_previews = None
        _qr_loaded_url = None


def unregister_ui():
    _clear_qr_previews()


class BLECACO_OT_start(bpy.types.Operator):
    bl_idname = "blecaco.start"
    bl_label = ""
    bl_description = "开始推流"

    def execute(self, context):
        ok, msg = streamer.start_stream()
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"} if ok else {"CANCELLED"}


class BLECACO_OT_stop(bpy.types.Operator):
    bl_idname = "blecaco.stop"
    bl_label = ""
    bl_description = "停止推流"

    def execute(self, context):
        streamer.stop_stream()
        self.report({"INFO"}, "已停止")
        return {"FINISHED"}


class BLECACO_PT_main_panel(bpy.types.Panel):
    bl_label = "Blecaco"
    bl_idname = "BLECACO_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blecaco"

    def draw(self, context):
        layout = self.layout
        props = context.scene.blecaco
        status = streamer.get_status()

        # 上半部分：相机 / 地址 / 推流按钮 —— 一个对齐列就够。
        # ⚠️ Blender 是按「子布局被创建的先后」决定显示顺序的：
        #    这两处 box 都挂在 layout 上、且在 col 之后创建，
        #    所以它们一定排在列的下面。若以后要往 col 里补内容，
        #    注意别把 box 建在补内容之前，否则顺序会反。
        col = layout.column(align=True)

        # 相机状态（无论是否运行都显示）
        cam, err = streamer.get_active_camera()
        if err:
            col.label(text=err, icon="ERROR")
        else:
            col.label(text=f"Camera: {cam.name}", icon="CAMERA_DATA")

        if status["running"]:
            ips = streamer.get_local_ips()
            for ip in ips:
                col.label(text=f"http://{ip}:{props.port}/", icon="URL")
            col.operator("blecaco.stop", icon="SNAP_FACE")

            # 二维码（用第一个 IP 生成）
            if ips and status["clients"] == 0:
                url = f"http://{ips[0]}:{props.port}"
                icon_id = _load_qr_icon(url)
                if icon_id is not None:
                    qr_box = layout.box()
                    qr_box.template_icon(icon_value=icon_id, scale=8)
        else:
            col.operator("blecaco.start", icon="PLAY")

        # 下半部分：四个设置项单独包一个 box，
        # 和上面的推流按钮 / 二维码区隔开。
        options_box = layout.box()
        options = options_box.column(align=True)

        # 端口只在没推流时给改（推流中改它没有意义）
        if not status["running"]:
            options.prop(props, "port")

        # 三个推流参数：和端口同一种样式（都带滑条）
        options.prop(props, "fps")
        options.prop(props, "quality")
        options.prop(props, "smooth")
