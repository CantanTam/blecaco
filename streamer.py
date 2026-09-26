import bpy
import io
import os
import json
import socket
import asyncio
import tempfile
import threading
import traceback
import atexit

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

try:
    import qrcode
    from qrcode.image.pil import PilImage
    HAS_QR = True
except ImportError:
    HAS_QR = False


_TMP_CAPTURE = os.path.join(tempfile.gettempdir(), "blecaco_capture.png")

_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blecaco</title>
</head><body style="margin:0;background:#111">
<img id="v" style="display:block;width:100vw">
<script>
const img=document.getElementById('v');
let current=null;
function connect(){
    const ws=new WebSocket('ws://'+location.host+'/');
    ws.binaryType='arraybuffer';
    ws.onmessage=e=>{
        if(!(e.data instanceof ArrayBuffer))return;
        const url=URL.createObjectURL(new Blob([e.data],{type:'image/jpeg'}));
        const old=current;current=url;img.src=url;
        if(old)setTimeout(()=>URL.revokeObjectURL(old),500);
    };
    ws.onclose=()=>setTimeout(connect,3000);
}
connect();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# 相机检查
# ---------------------------------------------------------------------------
def get_active_camera():
    """返回 (camera, error_msg)。没有活动相机时 error_msg 非空。"""
    try:
        scene = bpy.context.scene
    except Exception:
        return None, "无场景"
    if scene is None:
        return None, "无场景"
    if scene.camera is None:
        return None, "没有活动 camera"
    return scene.camera, None


def _find_view3d():
    for window in bpy.context.window_manager.windows:
        screen = getattr(window, "screen", None)
        if not screen:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region:
                return window, area, region
    return None, None, None

# ---------------------------------------------------------------------------
# 视口着色 / 引擎同步
# ---------------------------------------------------------------------------
_SHADING_KEYS = (
    "type", "light", "color_type",
    "show_shadows", "show_cavity", "cavity_type",
    "show_object_outline", "show_specular_highlight",
    "studio_light", "background_type", "background_color",
    "studiolight_rotate_z",
)


def _find_view_shading():
    """找到第一个 3D 视口的 shading 和它的 render_engine 提示。"""
    for window in bpy.context.window_manager.windows:
        screen = getattr(window, "screen", None)
        if not screen:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    return space.shading, getattr(space, "render_engine", None)
    return None, None


def _sync_shading_from_viewport(scene):
    """把视口的着色设置同步到 scene，并临时切换渲染引擎以匹配。"""
    view_shading, view_engine_hint = _find_view_shading()
    if view_shading is None:
        return

    stype = getattr(view_shading, "type", "SOLID")
    r = scene.render

    if stype == "RENDERED":
        # 渲染预览：用视口当前的引擎（EEVEE 或 Cycles）
        if view_engine_hint:
            try:
                r.engine = view_engine_hint
            except Exception:
                pass
    elif stype == "MATERIAL":
        # 材质预览：必须用 EEVEE，否则 render.opengl 会回退到 Workbench
        try:
            r.engine = "BLENDER_EEVEE_NEXT"
        except Exception:
            try:
                r.engine = "BLENDER_EEVEE"
            except Exception:
                pass
    else:
        # SOLID / WIREFRAME：用 Workbench
        try:
            r.engine = "BLENDER_WORKBENCH"
        except Exception:
            pass

    # 同步 shading 参数（主要对 Workbench 生效）
    target = scene.display.shading
    for key in _SHADING_KEYS:
        try:
            setattr(target, key, getattr(view_shading, key))
        except Exception:
            pass


def _with_synced_shading(scene, fn):
    """临时同步着色/引擎，执行 fn，然后还原。"""
    r = scene.render
    orig_engine = r.engine

    target = scene.display.shading
    orig_shading = {}
    for key in _SHADING_KEYS:
        try:
            orig_shading[key] = getattr(target, key)
        except Exception:
            pass

    try:
        _sync_shading_from_viewport(scene)
        fn()
    finally:
        try:
            r.engine = orig_engine
        except Exception:
            pass
        for key, val in orig_shading.items():
            try:
                setattr(target, key, val)
            except Exception:
                pass

# ---------------------------------------------------------------------------
# 渲染活动相机视图
# ---------------------------------------------------------------------------
def _capture_camera_png(target_w=640):
    """渲染活动相机视图到临时 PNG，返回 (png_bytes, error_msg)。"""
    cam, err = get_active_camera()
    if err:
        return None, err

    scene = bpy.context.scene

    # 按视口宽高比决定输出尺寸
    window, area, region = _find_view3d()
    if area:
        aspect = area.width / max(area.height, 1)
    else:
        aspect = 16.0 / 9.0
    target_h = max(2, int(target_w / aspect))
    target_w -= target_w % 2
    target_h -= target_h % 2

    r = scene.render
    orig = {
        "filepath": r.filepath,
        "res_x": r.resolution_x,
        "res_y": r.resolution_y,
        "pct": r.resolution_percentage,
        "fmt": r.image_settings.file_format,
    }

    tmp = _TMP_CAPTURE
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass

    r.filepath = tmp
    r.resolution_x = target_w
    r.resolution_y = target_h
    r.resolution_percentage = 100
    r.image_settings.file_format = "PNG"

    try:
        _with_synced_shading(
            scene,
            lambda: bpy.ops.render.opengl(view_context=False, write_still=True),
        )
    except Exception as e:
        print(f"[blecaco] render.opengl 异常: {e}")
        return None, None
    finally:
        r.filepath = orig["filepath"]
        r.resolution_x = orig["res_x"]
        r.resolution_y = orig["res_y"]
        r.resolution_percentage = orig["pct"]
        r.image_settings.file_format = orig["fmt"]

    if not os.path.exists(tmp):
        return None, None
    try:
        if os.path.getsize(tmp) < 100:
            return None, None
        with open(tmp, "rb") as f:
            data = f.read()
    except OSError:
        return None, None

    if len(data) < 8 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        print("[blecaco] PNG 数据损坏，跳过这一帧")
        return None, None

    return data, None


def _png_to_jpeg(png, quality):
    if not HAS_PIL:
        return png
    try:
        img = Image.open(io.BytesIO(png))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    except Exception as e:
        print(f"[blecaco] JPEG 编码失败: {e}")
        return None


# ---------------------------------------------------------------------------
# 服务器（队列式广播）
# ---------------------------------------------------------------------------
class _Server:
    def __init__(self, port):
        self.port = port
        self.loop = None
        self.server = None
        self.thread = None
        self.clients = set()
        self._lock = threading.Lock()
        self._running = False
        self._queue = None
        self._broadcast_task = None

    def start(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._running = True
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as e:
            print(f"[blecaco] 服务器异常: {e}")

    async def _serve(self):
        self._queue = asyncio.Queue(maxsize=2)
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        self.server = await websockets.serve(
            self._handler,
            "0.0.0.0",
            self.port,
            max_size=None,
            process_request=self._process_request,
        )
        await self.server.wait_closed()

    async def _broadcast_loop(self):
        try:
            while self._running:
                try:
                    data = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                with self._lock:
                    clients = list(self.clients)
                for ws in clients:
                    try:
                        await ws.send(data)
                    except Exception:
                        with self._lock:
                            self.clients.discard(ws)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[blecaco] 广播循环异常: {e}")

    def _put_frame(self, data):
        """在事件循环线程里执行：丢弃旧帧，只保留最新帧。"""
        try:
            while self._queue.full():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._queue.put_nowait(data)
        except Exception:
            pass

    def broadcast(self, data):
        if not self._running or not self.loop or not self._queue:
            return
        try:
            self.loop.call_soon_threadsafe(self._put_frame, data)
        except Exception:
            pass

    async def _process_request(self, connection, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        try:
            from websockets.http11 import Response
            from websockets.datastructures import Headers
            body = _HTML.encode("utf-8")
            return Response(200, "OK", Headers({
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
            }), body)
        except Exception:
            return None

    async def _handler(self, ws):
        with self._lock:
            self.clients.add(ws)
        try:
            async for message in ws:
                if isinstance(message, str):
                    try:
                        print(f"[blecaco] 收到: {json.loads(message)}")
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            with self._lock:
                self.clients.discard(ws)

    def stop(self):
        self._running = False
        if self._broadcast_task and self.loop:
            self.loop.call_soon_threadsafe(self._broadcast_task.cancel)
        if self.server and self.loop:
            async def _shutdown():
                self.server.close()
                await self.server.wait_closed()
            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), self.loop).result(timeout=2)
            except Exception:
                pass
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=2)
        self.loop = None
        self.thread = None
        self.server = None
        self._queue = None
        self._broadcast_task = None
        with self._lock:
            self.clients.clear()


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
_state = {
    "server": None,
    "running": False,
    "error_streak": 0,
    "frames": 0,
    "camera_status": "",
}


def _frame_timer():
    if not _state["running"]:
        return None
    try:
        png, err = _capture_camera_png()
        _state["camera_status"] = err or ""
        if png:
            props = bpy.context.scene.blecaco
            jpeg = _png_to_jpeg(png, props.quality)
            if jpeg:
                _state["server"].broadcast(jpeg)
                _state["frames"] += 1
        _state["error_streak"] = 0
    except Exception:
        traceback.print_exc()
        _state["error_streak"] += 1
        if _state["error_streak"] > 60:
            print("[blecaco] 错误过多，自动停止")
            stop_stream()
            return None
    return 1.0 / max(bpy.context.scene.blecaco.fps, 1)


def start_stream():
    if _state["running"]:
        return False, "已在运行"
    if not HAS_PIL:
        return False, "缺少 Pillow"
    if not HAS_WS:
        return False, "缺少 websockets"

    _, err = get_active_camera()
    if err:
        print(f"[blecaco] 启动时警告: {err}")

    props = bpy.context.scene.blecaco
    server = _Server(props.port)
    server.start()
    _state["server"] = server
    _state["running"] = True
    _state["error_streak"] = 0
    _state["frames"] = 0
    _state["camera_status"] = err or ""

    if not bpy.app.timers.is_registered(_frame_timer):
        bpy.app.timers.register(_frame_timer, first_interval=0.1)
    return True, "已启动"


def stop_stream():
    _state["running"] = False
    if bpy.app.timers.is_registered(_frame_timer):
        try:
            bpy.app.timers.unregister(_frame_timer)
        except Exception:
            pass
    if _state["server"]:
        _state["server"].stop()
        _state["server"] = None
    if os.path.exists(_TMP_CAPTURE):
        try:
            os.remove(_TMP_CAPTURE)
        except Exception:
            pass
    _state["camera_status"] = ""


def is_running():
    return _state["running"]


def get_status():
    s = _state["server"]
    return {
        "running": _state["running"],
        "frames": _state["frames"],
        "errors": _state["error_streak"],
        "clients": len(s.clients) if s else 0,
        "camera_status": _state["camera_status"],
    }

# ---------------------------------------------------------------------------
# 二维码生成
# ---------------------------------------------------------------------------
_QR_CACHE = {"url": None, "path": None}

def get_qr_path(url):
    """生成 url 对应的二维码 PNG，返回文件路径（带缓存）。"""
    if not HAS_QR or not HAS_PIL or not url:
        return None

    cached_path = _QR_CACHE.get("path")
    if _QR_CACHE.get("url") == url and cached_path and os.path.exists(cached_path):
        return cached_path

    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=8,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(
            image_factory=PilImage,
            fill_color="black",
            back_color="white",
        )

        path = os.path.join(tempfile.gettempdir(), "blecaco_qr.png")
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        img.save(path)

        _QR_CACHE["url"] = url
        _QR_CACHE["path"] = path
        return path
    except Exception as e:
        print(f"[blecaco] 二维码生成失败: {e}")
        return None
    

def get_local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            ips.append(ip)
        s.close()
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def _on_exit():
    try:
        stop_stream()
    except Exception:
        pass


def register():
    atexit.register(_on_exit)


def unregister():
    stop_stream()
    try:
        atexit.unregister(_on_exit)
    except Exception:
        pass
