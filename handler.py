
import bpy
import importlib
import json
import queue
import traceback


# streamer.py 接收的原始 JSON 消息队列
_inbound_queue: queue.Queue[str] = queue.Queue()

# handler 注册状态
_registered = False

# 每次定时器最多处理的消息数
MAX_MESSAGES_PER_TICK = 100

# Blender 主线程轮询间隔（秒）
TIMER_INTERVAL = 0.01

# Action 消息按 mode 分发到对应模块
ACTION_MODULES = {
    "game": "game",
    "plane": "plane_action",
    "car": "car_action",
    "free": "free_action",
    "drone": "drone_action",
    "camera":"camera",
}


def enqueue_message(message: str) -> None:
    """由 streamer.py 调用，将收到的原始 JSON 放入队列。"""
    if not isinstance(message, str):
        print("[blecaco] handler 收到非文本消息，已忽略")
        return

    _inbound_queue.put(message)


def _dispatch_message(raw_message: str) -> None:
    """在 Blender 主线程解析 JSON，并分发给对应业务模块。"""
    try:
        data = json.loads(raw_message)
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"[blecaco] JSON 解析失败: {exc}")
        return

    if not isinstance(data, dict):
        print("[blecaco] JSON 顶层必须是对象")
        return

    message_type = data.get("type")

    if message_type == "setting":
        module_name = "setting"

    elif message_type == "action":
        mode = str(data.get("mode", "")).lower()
        module_name = ACTION_MODULES.get(mode)

        if module_name is None:
            print(f"[blecaco] 不支持的 Action 模式: {mode}")
            return

    else:
        print(f"[blecaco] 不支持的消息类型: {message_type}")
        return

    # 业务模块必须提供 handle_message(data)
    try:
        module = importlib.import_module(
            f".{module_name}",
            package=__package__,
        )

        handler = getattr(module, "handle_message", None)

        if not callable(handler):
            print(
                f"[blecaco] {module_name}.py "
                "缺少 handle_message(data) 函数"
            )
            return

        handler(data)

    except ModuleNotFoundError as exc:
        print(f"[blecaco] 找不到业务模块 {module_name}.py: {exc}")

    except Exception:
        print(f"[blecaco] 分发消息到 {module_name}.py 时发生错误")
        traceback.print_exc()


def _process_queue() -> float | None:
    """通过 Blender 主线程定时器处理消息队列。"""
    if not _registered:
        return None

    for _ in range(MAX_MESSAGES_PER_TICK):
        try:
            message = _inbound_queue.get_nowait()
        except queue.Empty:
            break

        _dispatch_message(message)

    return TIMER_INTERVAL


def _clear_queue() -> None:
    """清除尚未处理的消息。"""
    while True:
        try:
            _inbound_queue.get_nowait()
        except queue.Empty:
            break


def register() -> None:
    """注册 Blender 主线程消息处理定时器。"""
    global _registered

    if _registered:
        return

    _registered = True

    if not bpy.app.timers.is_registered(_process_queue):
        bpy.app.timers.register(
            _process_queue,
            first_interval=TIMER_INTERVAL,
            persistent=True,
        )

    print("[blecaco] handler 已注册")


def unregister() -> None:
    """注销定时器并清除待处理消息。"""
    global _registered

    _registered = False

    if bpy.app.timers.is_registered(_process_queue):
        bpy.app.timers.unregister(_process_queue)

    _clear_queue()

    print("[blecaco] handler 已注销")
