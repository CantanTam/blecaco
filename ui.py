import bpy
from . import streamer


class BLECACO_OT_start(bpy.types.Operator):
    bl_idname = "blecaco.start"
    bl_label = "开始推流"

    def execute(self, context):
        ok, msg = streamer.start_stream()
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"} if ok else {"CANCELLED"}


class BLECACO_OT_stop(bpy.types.Operator):
    bl_idname = "blecaco.stop"
    bl_label = "停止推流"

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

        col = layout.column(align=True)

        # 相机状态（无论是否运行都显示）
        cam, err = streamer.get_active_camera()
        if err:
            col.label(text=err, icon="ERROR")
        else:
            col.label(text=f"Camera: {cam.name}", icon="CAMERA_DATA")

        if status["running"]:
            col.label(text="● 推流中", icon="PLAY")
            col.label(text=f"帧: {status['frames']}  客户端: {status['clients']}")
            for ip in streamer.get_local_ips():
                col.label(text=f"http://{ip}:{props.port}/", icon="URL")
            col.operator("blecaco.stop", icon="PAUSE")
        else:
            col.label(text="○ 未启动", icon="PAUSE")
            col.operator("blecaco.start", icon="PLAY")

        box = layout.box()
        box.prop(props, "port")
        box.prop(props, "fps")
        box.prop(props, "quality")
