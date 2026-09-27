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

try:
    import gpu
    HAS_GPU = True
except ImportError:
    gpu = None
    HAS_GPU = False


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
# GPU Offscreen 复用（避免每帧创建/销毁显存资源）
# ---------------------------------------------------------------------------
_offscreen_cache = None  # (width, height, GPUOffScreen)


def _get_offscreen(width, height):
    """按尺寸复用 GPUOffScreen；尺寸变化时释放旧的重建。"""
    global _offscreen_cache
    if _offscreen_cache is not None:
        cw, ch, off = _offscreen_cache
        if (cw, ch) == (width, height):
            return off
        _free_offscreen()
    off = gpu.types.GPUOffScreen(width, height)
    _offscreen_cache = (width, height, off)
    return off


def _free_offscreen():
    global _offscreen_cache
    if _offscreen_cache is None:
        return
    off = _offscreen_cache[2]
    _offscreen_cache = None      # 先断开引用，再释放，避免悬垂引用
    try:
        off.free()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 渲染活动相机视图
# ---------------------------------------------------------------------------
def _capture_camera_image(target_w=640):
    """以活动 Camera 为视角、以当前 3D View 的 shading 设置进行 Offscreen 绘制。"""
    if not HAS_GPU:
        return None, "Blender GPU API 不可用"

    cam, err = get_active_camera()
    if err:
        return None, err

    scene = bpy.context.scene

    # 找到一个可用的 3D View；这里只借用它的 SpaceView3D/shading 设置，
    # 不修改这个视口本身的 perspective，因此不会产生类似 Num 0 的跳转。
    window, area, region = _find_view3d()
    if not area or not region:
        return None, "没有可用的 3D View"

    space = next(
        (s for s in area.spaces if s.type == "VIEW_3D"),
        None,
    )
    if space is None:
        return None, "没有可用的 SpaceView3D"

    # 推流画幅必须与 Camera View(Numpad 0) 里的"相机框"一致：
    r = scene.render
    aspect = (r.resolution_x * r.pixel_aspect_x) / max(r.resolution_y * r.pixel_aspect_y, 1e-6)
    target_h = max(2, int(round(target_w / aspect)))
    target_w -= target_w % 2
    target_h -= target_h % 2

    depsgraph = bpy.context.evaluated_depsgraph_get()

    # Camera 决定“从哪里看”
    view_matrix = cam.matrix_world.inverted()

    # Camera 决定投影/焦距/裁剪等
    projection_matrix = cam.calc_matrix_camera(
        depsgraph,
        x=target_w,
        y=target_h,
    )

    try:
        offscreen = _get_offscreen(target_w, target_h)

        # draw_view3d() 使用 SpaceView3D 的 viewport 绘制规则。
        # 在真正的 Camera View 中，当前 Camera 不会作为“场景对象”
        # 出现在相机画面里；但 Offscreen 这里仍然是普通 View3D 绘制，
        # 因此必须临时关闭 Extras，否则 Camera / Light / Empty 等
        # viewport 辅助对象会被画进推流画面。
        overlay = space.overlay
        orig_show_extras = overlay.show_extras

        try:
            overlay.show_extras = False

            with offscreen.bind():
                offscreen.draw_view3d(
                    scene,
                    bpy.context.view_layer,
                    space,
                    region,
                    view_matrix,
                    projection_matrix,
                    do_color_management=True,
                )

                fb = gpu.state.active_framebuffer_get()

                buffer = fb.read_color(
                    0,
                    0,
                    target_w,
                    target_h,
                    4,
                    0,
                    "UBYTE",
                )

                buffer.dimensions = target_w * target_h * 4

                # GPU Buffer → bytes
                rgba = bytes(buffer)

        finally:
            # 完全恢复用户当前 viewport 的 Extras 设置。
            overlay.show_extras = orig_show_extras

        # GPU framebuffer 原点位于左下，PIL 需要上下翻转。
        # 这里直接把 PIL Image 交给上层编码 JPEG，不再经过 PNG 中转。
        img = Image.frombytes(
            "RGBA",
            (target_w, target_h),
            rgba,
        ).transpose(Image.Transpose.FLIP_TOP_BOTTOM)

        return img, None

    except Exception as e:
        print(f"[blecaco] Offscreen 渲染异常: {e}")
        traceback.print_exc()
        # 出错时丢弃缓存，下一帧按需重建，避免坏对象被反复复用
        _free_offscreen()
        return None, None


def _img_to_jpeg(img, quality):
    """PIL Image -> JPEG bytes（不再经过 PNG 编解码中转）。"""
    try:
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
        img, err = _capture_camera_image()
        _state["camera_status"] = err or ""
        if img is not None:
            props = bpy.context.scene.blecaco
            jpeg = _img_to_jpeg(img, props.quality)
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

    if not HAS_GPU:
        return False, "Blender GPU API 不可用"

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
    # 停止后立即释放显存里的 offscreen（Start 时会按需重建）
    _free_offscreen()
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
