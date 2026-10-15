import bpy


class BLECACO_SceneProperties(bpy.types.PropertyGroup):
    port: bpy.props.IntProperty(name="通讯端口", default=12121, min=1024, max=65535)
    fps: bpy.props.IntProperty(name="推流帧率", default=10, min=1, max=30)
    quality: bpy.props.IntProperty(name="推流画质", default=70, min=10, max=100)

    # 停止录制后，对活动摄像机的关键帧曲线做一次平滑（0 = 关闭，不改变任何东西）
    smooth: bpy.props.FloatProperty(
        name="平滑程度",
        description=(
            "停止录制后，对活动摄像机的关键帧曲线做加权滑动平均。"
            "0 = 关闭；越大越平滑（会削弱快速运动的峰值）"
        ),
        default=0.5,
        min=0.0,
        max=1.0,
        precision=2,
        # ⚠️ 这里**不要**加 subtype="FACTOR"：
        #    那会让面板把它显示成 "50.00 %" 的百分比滑条，
        #    和上面两个 IntProperty（推流帧率 / 推流画质）的样式就不一致了。
    )


def register_properties():
    bpy.utils.register_class(BLECACO_SceneProperties)
    bpy.types.Scene.blecaco = bpy.props.PointerProperty(type=BLECACO_SceneProperties)


def unregister_properties():
    if hasattr(bpy.types.Scene, "blecaco"):
        del bpy.types.Scene.blecaco
    bpy.utils.unregister_class(BLECACO_SceneProperties)
