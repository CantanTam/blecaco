import bpy
import importlib.machinery
import os
import platform
import sys
import tempfile
import zipfile

_addon_dir = os.path.dirname(os.path.abspath(__file__))
_wheels_dir = os.path.join(_addon_dir, "wheels")
# 解压落点：优先插件目录本身（和 websockets/、qrcode/ 一样属于插件自带依赖）；
# 插件目录不可写时（例如系统级安装）退到临时目录，仍然用自带的 Pillow，
# 而不是回头去用系统 site-packages 里那份。
_fallback_dir = os.path.join(tempfile.gettempdir(), "blecaco_deps")

# 当前生效的“里面放着 PIL/ 的那个目录”；None 表示没有可用的自带副本
_vendored_root = None


def _pil_abi_match(base: str) -> bool:
    """base/PIL 里的二进制扩展能不能被当前解释器加载。

    等价于“解释器找不找得到 PIL/_imaging<它认识的那个扩展后缀>”。解释器只会按
    自己的 EXTENSION_SUFFIXES 去拼文件名，比如 Python 3.14 只认 .so /
    .abi3.so / .cpython-314-x86_64-linux-gnu.so，所以别的 Python 版本或别的
    平台解压出来的 _imaging.*（cp311 / cp313、Windows 的 .pyd 等）会被判定为
    不匹配，从而重新解压正确的那一份。顺带确认关键纯 Python 文件也在，
    避免上次解压解到一半留下的残缺副本被当成可用。
    """
    pil_dir = os.path.join(base, "PIL")
    if not os.path.isdir(pil_dir):
        return False
    for name in ("__init__.py", "Image.py"):
        if not os.path.isfile(os.path.join(pil_dir, name)):
            return False
    return any(
        os.path.isfile(os.path.join(pil_dir, "_imaging" + suffix))
        for suffix in importlib.machinery.EXTENSION_SUFFIXES
    )


def _interpreter_tag() -> str:
    """当前解释器的 wheel python tag，例如 cp314。"""
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def _platform_tag_ok(tag: str) -> bool:
    """wheel 的平台标签是否匹配当前机器（按系统 + CPU 架构粗判）。"""
    if tag == "any":
        return True

    machine = platform.machine().lower()

    if sys.platform.startswith("linux"):
        # manylinux_2_28_x86_64 / musllinux_1_2_aarch64 / linux_x86_64
        return tag.startswith(("manylinux", "musllinux", "linux")) and tag.endswith(machine)

    if sys.platform == "win32":
        want = {
            "amd64": "win_amd64",
            "x86_64": "win_amd64",
            "arm64": "win_arm64",
            "aarch64": "win_arm64",
            "x86": "win32",
        }.get(machine)
        return want is not None and tag == want

    if sys.platform == "darwin":
        want = "_arm64" if machine in ("arm64", "aarch64") else "_x86_64"
        return tag.startswith("macosx") and tag.endswith(want)

    return False


def _wheel_matches(filename: str) -> bool:
    """wheel 文件名（name-version-pytag-abitag-plattag.whl）是否适用当前解释器 + 平台。"""
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return False

    py_tags = parts[-3].split(".")
    abi_tags = parts[-2].split(".")
    plat_tags = parts[-1].split(".")

    cp = _interpreter_tag()
    if cp not in py_tags and "py3" not in py_tags:
        return False
    if not ({cp, "none", "abi3"} & set(abi_tags)):
        return False
    return any(_platform_tag_ok(t) for t in plat_tags)


def _find_pillow_wheel() -> str | None:
    """在 wheels/ 里找出适配“当前 Python + 当前平台”的轮子，返回其路径。"""
    try:
        names = sorted(os.listdir(_wheels_dir))
    except OSError:
        return None
    for name in names:
        if name.endswith(".whl") and _wheel_matches(name):
            return os.path.join(_wheels_dir, name)
    return None


def _extract_pillow_wheel(wheel_path: str, dest: str) -> None:
    """把轮子里的 PIL/ 与 *.libs/ 解压到 dest。

    只解压这两类目录：pillow.libs/ 必须与 PIL/ 同级（Linux 上 _imaging 的
    RPATH 就是 $ORIGIN/../pillow.libs）；dist-info / licenses 对运行没用。
    """
    with zipfile.ZipFile(wheel_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/") or "__pycache__" in name:
                continue
            top = name.split("/", 1)[0]
            if top == "PIL" or top.endswith(".libs"):
                zf.extract(info, dest)


def _ensure_vendored_pil() -> str | None:
    """拿到自带 PIL 所在目录：已有的直接复用，没有就从匹配的轮子里解压出来。"""
    for base in (_addon_dir, _fallback_dir):
        if _pil_abi_match(base):
            return base

    wheel = _find_pillow_wheel()
    if not wheel:
        return None

    for base in (_addon_dir, _fallback_dir):
        try:
            _extract_pillow_wheel(wheel, base)
        except OSError as exc:
            print(f"[blecaco] 解压 {os.path.basename(wheel)} 到 {base} 失败: {exc}")
            continue
        if _pil_abi_match(base):
            print(f"[blecaco] 已从 {os.path.basename(wheel)} 解压自带 Pillow 到 {base}")
            return base
    return None


def _is_vendored_pil(module_file: str) -> bool:
    """module_file（某个模块的 __file__）是否来自插件自带的 PIL。"""
    norm = os.path.normcase(os.path.abspath(module_file or ""))
    return any(
        norm.startswith(os.path.normcase(os.path.abspath(base)) + os.sep)
        for base in (_addon_dir, _fallback_dir)
    )


def _pil_missing_hint(import_error: str = "") -> str:
    """HAS_PIL 为 False 时给出的提示（供 streamer / 加载日志使用）。"""
    detail = import_error or "未安装"
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    if _vendored_root is None and _find_pillow_wheel() is None:
        return (
            f"缺少 Pillow：wheels/ 里没有适配 Python {ver} + 本平台"
            f"（{sys.platform} / {platform.machine()}）的轮子，"
            f"需要 {_interpreter_tag()} 且平台标签匹配的那份（{detail}）"
        )
    return f"缺少 Pillow（{detail}）"


def _setup_vendored_deps() -> None:
    """把插件目录挂到 sys.path 最前面，确保用的是插件自带的依赖。

    插件只使用自带的 Pillow：导入时按“当前 Python 版本 + 当前平台”去 wheels/
    里挑出匹配的轮子，把里面的 PIL/ 与 pillow.libs/ 解压到插件目录（Blender 自己
    装扩展轮子也是这种做法），再把该目录插到 sys.path 最前面。于是 Windows /
    macOS / Linux 上是同一套代码、同一份依赖：既不依赖系统 site-packages 里有没有
    Pillow，也不会出现"本机能跑、换台机器却缺 Pillow"的情况。没有可用副本时由
    _pil_missing_hint() 报清楚原因（缺哪个 Python 版本 / 平台标签的轮子）。
    """
    global _vendored_root

    _vendored_root = _ensure_vendored_pil()

    if _vendored_root is not None:
        # 万一别的插件先把系统的 Pillow 塞进了 sys.modules，先把它请出去，
        # 否则下面的 from PIL import Image 会命中它，而不是插件自带的 PIL/。
        for name in [m for m in sys.modules if m == "PIL" or m.startswith("PIL.")]:
            path = getattr(sys.modules[name], "__file__", "") or ""
            if not _is_vendored_pil(path):
                del sys.modules[name]

    if _addon_dir not in sys.path:
        sys.path.insert(0, _addon_dir)
    if _vendored_root is not None and _vendored_root not in sys.path:
        sys.path.insert(0, _vendored_root)


_setup_vendored_deps()

bl_info = {
    "name": "Blecaco",
    "author": "Canta Tam",
    "version": (0, 1, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Blecaco",
    "description": "把 Blender 视口通过 WebSocket + JPEG 推到浏览器/Godot",
    "category": "3D View",
    "support": "COMMUNITY",
}

from .addon_property import (
    BLECACO_SceneProperties,
    register_properties,
    unregister_properties,
)
from .streamer import (
    register as streamer_register,
    unregister as streamer_unregister,
)
from .ui import (
    BLECACO_OT_start,
    BLECACO_OT_stop,
    BLECACO_PT_main_panel,
    unregister_ui,
)


_classes = (
    BLECACO_OT_start,
    BLECACO_OT_stop,
    BLECACO_PT_main_panel,
)


def register():
    register_properties()
    for cls in _classes:
        bpy.utils.register_class(cls)
    streamer_register()


def unregister():
    streamer_unregister()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    unregister_properties()
    unregister_ui()
