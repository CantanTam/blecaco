from __future__ import annotations

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
from dataclasses import dataclass

from . import _is_vendored_pil, _pil_missing_hint
from . import handler as _message_handler
try:
    from PIL import Image

    # 只认插件自带的 PIL（__init__ 已按当前 Python + 平台把它解压到插件目录，
    # 并置于 sys.path 最前面）。系统 site-packages 里的 Pillow 不具备跨平台
    # 通用性：一旦命中它就说明自带的那份不可用，这里按“缺 Pillow”处理，
    # 由面板把真正的原因（例如缺哪个平台标签的轮子）报出来。
    if not _is_vendored_pil(Image.__file__):
        raise ImportError(f"命中系统 Pillow：{Image.__file__}")

    HAS_PIL = True
    _PIL_ERROR = ""
except ImportError as exc:
    # Pillow 不可用时不会用到 Image：HAS_PIL 为 False 时启动阶段就直接返回了
    HAS_PIL = False
    _PIL_ERROR = str(exc)

try:
    import websockets
    # 播放页在 HTTP 握手阶段直接返回，需要这两者（websockets 自带 http11/datastructures）
    from websockets.datastructures import Headers as _WSHeaders
    from websockets.http11 import Response as _WSResponse
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


# 逐条打印收到的消息会把控制台刷爆（旋转 + 位移合计可达 100~200 条/秒），
# 而且每条都要多解析一次 JSON；排查时再改 True。
VERBOSE_INBOUND_LOG: bool = False


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
        const url=URL.createObjectURL(new Blob([e.data],{type:'image/webp'}));
        const old=current;current=url;img.src=url;
        if(old)setTimeout(()=>URL.revokeObjectURL(old),500);
    };
    ws.onclose=()=>setTimeout(connect,3000);
}
connect();
</script>
</body></html>
"""

# 播放页字节内容预先编码好：每次 HTTP 请求直接复用，无需重新 encode
_HTML_BYTES = _HTML.encode("utf-8")


# ---------------------------------------------------------------------------
# 相机检查
# ---------------------------------------------------------------------------
def get_active_camera() -> tuple[bpy.types.Object | None, str | None]:
    """返回 (camera, error_msg)。没有活动相机时 error_msg 非空。"""
    try:
        scene = bpy.context.scene
    except Exception:
        scene = None
    if scene is None:
        return None, "无场景"
    if scene.camera is None:
        return None, "没有活动 camera"
    return scene.camera, None


def _find_view3d() -> tuple[
    bpy.types.Area | None,
    bpy.types.Region | None,
    bpy.types.SpaceView3D | None,
]:
    """返回 (area, region, space)；找不到时对应项为 None。

    space 取自同一个 area；若该 area 没有 SpaceView3D，仍然把它返回出去，
    由调用方给出 "没有可用的 SpaceView3D" 的提示。
    """
    for window in bpy.context.window_manager.windows:
        screen = getattr(window, "screen", None)
        if not screen:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if not region:
                continue
            space = next((s for s in area.spaces if s.type == "VIEW_3D"), None)
            return area, region, space
    return None, None, None

# ---------------------------------------------------------------------------
# GPU Offscreen 复用（避免每帧创建/销毁显存资源）
# ---------------------------------------------------------------------------
_offscreen_cache = None  # (width, height, GPUOffScreen)


def _get_offscreen(width: int, height: int) -> gpu.types.GPUOffScreen:
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


def _free_offscreen() -> None:
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
def _draw_offscreen(
    offscreen: gpu.types.GPUOffScreen,
    scene,
    space,
    region,
    view_matrix,
    projection_matrix,
    width: int,
    height: int,
) -> bytes:
    """在 offscreen 上按 SpaceView3D 的规则绘制并回读 RGBA 字节。

    draw_view3d() 使用 SpaceView3D 的 viewport 绘制规则。在真正的 Camera
    View 中，当前 Camera 不会作为“场景对象”出现在相机画面里；但 Offscreen
    这里仍然是普通 View3D 绘制，因此必须临时关闭 Extras，否则 Camera /
    Light / Empty 等 viewport 辅助对象会被画进推流画面。
    """
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
                width,
                height,
                4,
                0,
                "UBYTE",
            )

            buffer.dimensions = width * height * 4

            # GPU Buffer → bytes
            return bytes(buffer)

    finally:
        # 无论绘制成功与否，都完全恢复用户当前 viewport 的 Extras 设置。
        overlay.show_extras = orig_show_extras


def _capture_camera_image(target_w: int = 640) -> tuple[Image.Image | None, str | None]:
    """以活动 Camera 为视角、以当前 3D View 的 shading 设置进行 Offscreen 绘制。"""
    if not HAS_GPU:
        return None, "Blender GPU API 不可用"

    cam, err = get_active_camera()
    if err:
        return None, err

    scene = bpy.context.scene

    # 找到一个可用的 3D View；这里只借用它的 SpaceView3D/shading 设置，
    # 不修改这个视口本身的 perspective，因此不会产生类似 Num 0 的跳转。
    area, region, space = _find_view3d()
    if not area:
        return None, "没有可用的 3D View"
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
        rgba = _draw_offscreen(
            offscreen,
            scene,
            space,
            region,
            view_matrix,
            projection_matrix,
            target_w,
            target_h,
        )

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


# ---------------------------------------------------------------------------
# 一期推流优化：WebP 编码 + 工作线程 + 自适应质量
# ---------------------------------------------------------------------------
# 编码格式：WebP（Pillow 自带 ✓ 同画质比 JPEG 小 25~35% ✓）
# 想切回 JPEG 调试：把 STREAM_CODEC 改成 "JPEG"，客户端会自动兼容两种
STREAM_CODEC: str = "WEBP"
# WebP 压缩档：0(最快) ~ 6(最小)。实测 2 是甜点（640x360 约 4ms/帧，体积和 4 几乎一样）
WEBP_METHOD: int = 2
# 自适应质量的下限（再低就太糊了）
WEBP_QUALITY_MIN: int = 20
# 超预算时快速降、有余量时慢慢升（避免画质来回抖）
QUALITY_STEP_DOWN: int = 6
QUALITY_STEP_UP: int = 1
# 每帧字节预算（640x360 / quality=70 的基准）。实际预算会按用户画质档位缩放
FRAME_BUDGET_BASE: int = 38000


def _img_to_webp(img: Image.Image, quality: int) -> bytes | None:
    """PIL Image -> WebP bytes（WebP 是帧内编码，无跨帧依赖，可直接替换 JPEG）。"""
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=quality, method=WEBP_METHOD)
        return buf.getvalue()
    except Exception as e:
        print(f"[blecaco] WebP 编码失败: {e}")
        return None


def _encode_frame(img: Image.Image, quality: int) -> bytes | None:
    """按当前格式编码一帧。"""
    if STREAM_CODEC == "WEBP":
        return _img_to_webp(img, quality)
    return _img_to_jpeg(img, quality)


class _EncoderThread(threading.Thread):
    """后台编码线程：主线程只负责抓帧并提交，编码不占 Blender 主线程。

    - 只保留**最新一帧**（旧帧直接丢 ✗ 不排队）
    - 自适应质量：帧太大就降 q，余量大就慢慢升
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="blecaco-encoder")
        self._lock = threading.Lock()
        self._latest = None            # (img, max_quality)
        self._wake = threading.Event()
        self._running = True
        self.quality = None            # 自适应状态（None = 用用户档位起步）
        self.frames = 0
        self.dropped = 0

    def submit(self, img, max_quality: int) -> None:
        with self._lock:
            if self._latest is not None:
                self.dropped += 1
            self._latest = (img, max_quality)   # 丢旧帧 ✓
        self._wake.set()

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def budget(self, max_quality: int) -> int:
        return int(FRAME_BUDGET_BASE * max(0.35, max_quality / 70.0))

    def run(self) -> None:
        while self._running:
            self._wake.wait(timeout=1.0)
            self._wake.clear()

            with self._lock:
                item = self._latest
                self._latest = None

            if item is None:
                continue

            img, max_quality = item

            max_quality = max(WEBP_QUALITY_MIN, min(100, int(max_quality)))
            q = self.quality if self.quality is not None else max_quality
            q = max(WEBP_QUALITY_MIN, min(max_quality, int(q)))

            data = _encode_frame(img, q)

            if data is None:
                continue

            # 自适应：超预算就降，余量大就慢慢升
            if len(data) > self.budget(max_quality):
                self.quality = max(WEBP_QUALITY_MIN, q - QUALITY_STEP_DOWN)
            else:
                self.quality = min(max_quality, q + QUALITY_STEP_UP)

            srv = _state.server

            if srv is not None:
                srv.broadcast(data)
                self.frames += 1
                _state.frames = self.frames


def _img_to_jpeg(img: Image.Image, quality: int) -> bytes | None:
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
    """WebSocket 推流服务器。

    线程模型（改动前务必先看这里）：
    - 主线程调用：`start()` / `stop()` / `broadcast()` / `client_count()`
    （即 Blender 的 UI 与 `_frame_timer` 所在线程）；
    - 后台线程（`self.loop`）：`_run()` / `_serve()` / `_broadcast_loop()` /
    `_handler()` / `_process_request()`，只有它们能直接操作 asyncio 对象；
    - `self.clients` 两边都会读写，因此**所有**访问都必须持有 `self._lock`；
    - 跨线程投递帧走 `loop.call_soon_threadsafe()`（见 `broadcast()`）。
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.loop = None
        self.server = None
        self.thread = None
        self.clients = set()
        self._lock = threading.Lock()
        self._running = False
        self._queue = None
        self._broadcast_task = None
        # 启动结果回传：后台线程绑定成功后 set()；失败则先写 _start_error 再 set()
        self._ready = threading.Event()
        self._start_error = None

    def start(self) -> str | None:
        """启动后台事件循环线程，并等待端口绑定结果。

        返回 None 表示服务器已就绪；返回字符串表示启动失败（例如端口被占用）。
        """
        self._ready.clear()
        self._start_error = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._running = True
        self.thread.start()
        if not self._ready.wait(timeout=5.0):
            return "启动超时（后台线程未就绪）"
        return self._start_error

    def _run(self) -> None:
        """后台线程入口：跑事件循环直到 _serve() 结束。"""
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as e:
            # 启动阶段（例如端口被占用）和运行阶段的异常都走这里；
            # 前者由 _start_error 回传给主线程，让面板能报出真正的原因。
            self._start_error = str(e) or e.__class__.__name__
            print(f"[blecaco] 服务器异常: {e}")
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        """后台线程：绑定端口、启动广播任务，然后一直等到服务器关闭。"""
        self._queue = asyncio.Queue(maxsize=2)
        # 先绑定端口：失败就直接抛出（不会留下未完成的广播 Task），由 _run() 回传错误。
        self.server = await websockets.serve(
            self._handler,
            "0.0.0.0",
            self.port,
            # 入站消息上限（客户端从不发送业务数据，这里只是防止超大消息占用内存）
            max_size=1 << 20,
            # 关闭 permessage-deflate：JPEG 已经是压缩数据，再 deflate 只是白烧两端 CPU
            compression=None,
            process_request=self._process_request,
        )
        # 端口已绑定成功，通知主线程继续（start_stream 据此判断是否启动成功）
        self._ready.set()
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        await self.server.wait_closed()

    async def _broadcast_loop(self) -> None:
        """后台线程：从队列取最新帧，逐个发给在线客户端。"""
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

    def _put_frame(self, data: bytes) -> None:
        """在事件循环线程里执行：丢弃旧帧，只保留最新帧。"""
        try:
            # full() 成立说明队列非空，get_nowait() 不会抛 QueueEmpty。
            while self._queue.full():
                self._queue.get_nowait()
            self._queue.put_nowait(data)
        except Exception:
            pass

    def broadcast(self, data: bytes) -> None:
        """主线程调用：把一帧交给后台广播循环（不阻塞，只保留最新帧）。"""
        if not self._running or not self.loop or not self._queue:
            return
        try:
            self.loop.call_soon_threadsafe(self._put_frame, data)
        except Exception:
            pass

    async def _broadcast_text(self, message: str) -> None:
        """在 WebSocket 事件循环中向客户端发送文本消息。"""
        with self._lock:
            clients = list(self.clients)

        for ws in clients:
            try:
                await ws.send(message)
            except Exception as exc:
                print(f"[blecaco] 文本消息发送失败: {exc}")
                with self._lock:
                    self.clients.discard(ws)

    def send_text(self, message: str) -> bool:
        """供 Blender 主线程调用，异步向客户端广播文本消息。"""
        if not self._running or self.loop is None or self.loop.is_closed():
            return False

        try:
            asyncio.run_coroutine_threadsafe(
                self._broadcast_text(message),
                self.loop,
            )
            return True
        except RuntimeError:
            return False

    def client_count(self) -> int:
        """供 Blender 主线程安全查询在线客户端数量。"""
        with self._lock:
            return len(self.clients)

    async def _process_request(self, connection, request) -> _WSResponse | None:
        """后台线程：普通 HTTP 请求直接返回播放页，WebSocket 握手交给框架。"""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        body = _HTML_BYTES
        return _WSResponse(200, "OK", _WSHeaders({
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        }), body)

    async def _handler(self, ws) -> None:
        """后台线程：接收文本消息，并转交给 handler.py。"""
        with self._lock:
            self.clients.add(ws)

        try:
            async for message in ws:
                if isinstance(message, str):
                    # 原始消息交给 handler.py，具体解析和分流由它负责
                    _message_handler.enqueue_message(message)

                    # 逐条打印会明显拖慢接收（可达 100~200 条/秒），
                    # JSON 解析失败由 handler._dispatch_message 负责报错。
                    if VERBOSE_INBOUND_LOG:
                        print(f"[blecaco] 收到: {message}")

        except Exception as exc:
            print(f"[blecaco] WebSocket 接收异常: {exc}")

        finally:
            with self._lock:
                self.clients.discard(ws)

    def stop(self) -> None:
        """主线程调用：关闭服务器、停掉事件循环并释放 loop（可安全重复调用）。"""
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
        # 释放在 _run() 里创建的 event loop（不 close 会导致反复 Start/Stop 泄漏 fd）
        if self.loop:
            try:
                self.loop.close()
            except Exception:
                pass
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
MAX_ERROR_STREAK = 60        # 连续出错帧数上限，超过则自动停止
FIRST_FRAME_INTERVAL = 0.1   # 帧定时器首次触发间隔（秒）
FALLBACK_INTERVAL = 0.1      # fps 暂时取不到时的兜底间隔（秒）


@dataclass
class _State:
    """推流运行时状态（仅 Blender 主线程读写）。"""
    server: "_Server | None" = None   # 当前服务器实例
    running: bool = False             # 是否已 Start（不代表有客户端在收帧）
    error_streak: int = 0             # 连续出错帧数，超过 MAX_ERROR_STREAK 自动停止
    frames: int = 0                   # 本次推流已发出的帧数
    encoder = None                    # 后台编码线程（_EncoderThread）
    camera_status: str = ""           # 最近一次的相机/渲染状态提示


_state = _State()

def send_setting_to_clients(name: str, value) -> bool:
    """向连接的客户端发送 setting 类型 JSON，不发送 action。"""
    server = _state.server

    if not _state.running or server is None:
        print("[blecaco] 无法发送设置：WebSocket 服务器未运行")
        return False

    try:
        message = json.dumps(
            {
                "type": "setting",
                "name": name,
                "value": value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        print(f"[blecaco] 设置 JSON 序列化失败: {exc}")
        return False

    return server.send_text(message)

def _frame_timer() -> float | None:
    """Blender 主线程定时器：抓帧 → 编码 → 投递到广播队列。

    返回下一次调用前的间隔（秒）；返回 None 表示停止该定时器。
    """
    if not _state.running:
        return None

    try:
        # 配合 persistent=True：文件加载/切换的瞬间 scene 或属性可能暂时取不到，
        # 这时只跳过本帧，不能让异常把定时器带走（那就变成静默断流了）。
        fps = max(bpy.context.scene.blecaco.fps, 1)
    except Exception:
        return FALLBACK_INTERVAL

    interval = 1.0 / fps

    srv = _state.server
    # 没有客户端时不抓帧：draw_view3d + 回读 + 编码是本流程最贵的部分（等于白渲一张图）。
    # 客户端连上后最多等一个周期就能收到第一帧。
    if srv is None or srv.client_count() == 0:
        return interval

    try:
        img, err = _capture_camera_image()
        _state.camera_status = err or ""
        if img is not None:
            # 编码交给后台线程（不占主线程 ✓），这里只提交
            props = bpy.context.scene.blecaco
            encoder = _state.encoder
            if encoder is not None:
                encoder.submit(img, props.quality)
        _state.error_streak = 0
    except Exception:
        traceback.print_exc()
        _state.error_streak += 1
        if _state.error_streak > MAX_ERROR_STREAK:
            print("[blecaco] 错误过多，自动停止")
            stop_stream()
            return None

    return interval


def start_stream() -> tuple[bool, str]:
    if _state.running:
        return False, "已在运行"

    # 依赖检查：顺序即报错优先级。PIL 的提示按需生成，
    # 避免 Pillow 正常时也去扫一遍 wheels/ 目录。
    if not HAS_PIL:
        return False, _pil_missing_hint(_PIL_ERROR)
    if not HAS_WS:
        return False, "缺少 websockets"
    if not HAS_GPU:
        return False, "Blender GPU API 不可用"

    _, cam_err = get_active_camera()
    if cam_err:
        print(f"[blecaco] 启动时警告: {cam_err}")

    props = bpy.context.scene.blecaco
    server = _Server(props.port)
    err_start = server.start()
    if err_start:
        # 绑定失败（例如端口被占用）：回收线程/loop，并把原因报到面板
        server.stop()
        return False, f"启动失败（端口 {props.port}）: {err_start}"
    _state.server = server
    _state.running = True

    # 启动后台编码线程
    encoder = _EncoderThread()
    encoder.start()
    _state.encoder = encoder
    _state.error_streak = 0
    _state.frames = 0
    _state.camera_status = cam_err or ""

    if not bpy.app.timers.is_registered(_frame_timer):
        # persistent=True：加载/新建 .blend 文件时不移除该定时器。
        # 否则会出现"面板显示运行中、URL/二维码都在，但客户端收不到帧"的静默断流。
        bpy.app.timers.register(
            _frame_timer,
            first_interval=FIRST_FRAME_INTERVAL,
            persistent=True,
        )
    return True, "已启动"


def stop_stream() -> None:
    _state.running = False
    if bpy.app.timers.is_registered(_frame_timer):
        try:
            bpy.app.timers.unregister(_frame_timer)
        except Exception:
            pass
    # 先停编码线程
    if _state.encoder is not None:
        _state.encoder.stop()
        _state.encoder = None

    # 停止后立即释放显存里的 offscreen（Start 时会按需重建）
    _free_offscreen()
    if _state.server:
        _state.server.stop()
        _state.server = None
    _state.camera_status = ""


def is_running() -> bool:
    return _state.running


def get_status() -> dict:
    s = _state.server
    return {
        "running": _state.running,
        "frames": _state.frames,
        "errors": _state.error_streak,
        "clients": s.client_count() if s else 0,
        "camera_status": _state.camera_status,
    }

# ---------------------------------------------------------------------------
# 二维码生成
# ---------------------------------------------------------------------------
_QR_CACHE = {"url": None, "path": None}

def get_qr_path(url: str) -> str | None:
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
    

def get_local_ips() -> list[str]:
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


def _on_exit() -> None:
    try:
        stop_stream()
    except Exception:
        pass


def register() -> None:
    atexit.register(_on_exit)


def unregister() -> None:
    stop_stream()
    try:
        atexit.unregister(_on_exit)
    except Exception:
        pass
