# Blecaco 外部依赖与 git clone 恢复指南

> **如果你安装的是发布 zip（`blecaco-0.1.0.zip`）**：包内已经包含 `wheels/`、`websockets/`、`qrcode/`，首次启用会自动从 `wheels/` 解压 Pillow，直接安装即可。  
> 下面的恢复步骤只用于 git clone、换平台或换 Blender 版本。

Blecaco 是一个 Blender 插件（`bl_info` 最低 Blender 4.5，实测 5.2.1）。  
它**不依赖 Blender 自带的 site-packages**，而是把需要的 Python 库放在插件目录里：

- **Pillow**：截图/JPEG 编码、二维码图片
- **websockets**：与手机通信的 WebSocket 服务（默认端口 12121）
- **qrcode**：生成二维码

其中有一部分是**二进制/体积较大**的依赖，被 `.gitignore` 排除，不会 push 到 GitHub。  
直接 `git clone` 下来后，插件能加载，但**推流/WebSocket 会不可用**，需要按本文恢复。

---

## 0. 先看结论

`blecaco/.gitignore`：

```gitignore
__pycache__/
wheels/
PIL/
pillow.libs/
websockets/
```

| 目录 | 是否进 git | 大小 | 作用 | 恢复方式 |
|---|---|---:|---|---|
| `wheels/` | ❌ | 24MB | 各平台 Pillow / websockets / qrcode 的 wheel 源文件 | 从原机器复制，或用 `pip download` 下载 |
| `PIL/` | ❌ | 7MB | Pillow 解压后的包 | **插件自动**从 `wheels/` 解压 |
| `pillow.libs/` | ❌ | 14MB | Pillow 的 Linux 原生库（libjpeg/libpng 等） | 随 Pillow wheel 一起自动解压 |
| `websockets/` | ❌ | 1.3MB | WebSocket 服务端 | **手动**从 wheel 解压（见第 4 节） |
| `qrcode/` | ✅ | 384KB | 二维码生成（34 个文件已跟踪） | 不需要处理 |
| `__pycache__/` | ❌ | 324KB | Python 字节码 | 自动生成 |

**最短恢复（假设你已经拿到 `wheels/`）**：

```bash
cd /path/to/blecaco
python3 -c "import zipfile; zipfile.ZipFile('wheels/websockets-17.1-py3-none-any.whl').extractall('.')"
# Pillow 不用手动解压：插件加载时会从 wheels/ 自动解压
# 然后重启 Blender（或重新加载插件）
```

---

## 1. 依赖版本

本仓库自带的 wheel（`wheels/`）是给 **Blender 5.2.x / CPython 3.14（cp314）** 准备的：

| 库 | 版本 | wheel 文件 |
|---|---|---|
| Pillow | 12.3.0 | `pillow-12.3.0-cp314-cp314-*.whl`（按平台） |
| websockets | 17.1 | `websockets-17.1-py3-none-any.whl` |
| qrcode | 8.2 | `qrcode-8.2-py3-none-any.whl`（可选；qrcode 已跟踪） |

Pillow 的四个平台 wheel：

- Linux x86_64：`pillow-12.3.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl`
- macOS Apple Silicon：`pillow-12.3.0-cp314-cp314-macosx_11_0_arm64.whl`
- macOS Intel：`pillow-12.3.0-cp314-cp314-macosx_10_15_x86_64.whl`
- Windows x64：`pillow-12.3.0-cp314-cp314-win_amd64.whl`

> `websockets` 和 `qrcode` 是纯 Python wheel（`py3-none-any`），换 Python 小版本也能用。  
> Pillow 是**按 Python ABI + 平台**编译的：如果你的 Blender 不是 5.2.x（例如 4.5/4.6），  
> 需要下载对应 `cp3xx` 的 Pillow wheel；否则插件的 ABI 检查会拒绝它。

---

## 2. 获取 `wheels/`

### 方式 A：从原开发机复制（最省事）

`wheels/` 只有 24MB，且不在 git 里。直接把整个 `wheels/` 目录复制到插件根目录：

```text
blecaco/
├── __init__.py
├── wheels/
│   ├── pillow-12.3.0-cp314-cp314-manylinux_2_....whl
│   ├── websockets-17.1-py3-none-any.whl
│   └── ...
├── websockets/       ← 解压后生成（被 gitignore）
├── PIL/              ← 自动解压后生成（被 gitignore）
└── pillow.libs/      ← 自动解压后生成（被 gitignore）
```

### 方式 B：用 `pip download` 从 PyPI 下载

先确认 Blender 的 Python 版本（Blender 的 Python 控制台）：

```python
import sys
print(sys.version)
```

本仓库的 wheel 对应 3.14（cp314）。其他版本请把下面的 `3.14` / `cp314` 换成实际值。

在插件根目录创建 `wheels/`，然后按平台下载 Pillow（只需要你当前平台那一份）：

```bash
mkdir -p wheels

# Linux x86_64
python3 -m pip download --only-binary=:all: --no-deps \
  --platform manylinux_2_28_x86_64 --python-version 3.14 --implementation cp --abi cp314 \
  -d wheels pillow==12.3.0

# macOS Apple Silicon
python3 -m pip download --only-binary=:all: --no-deps \
  --platform macosx_11_0_arm64 --python-version 3.14 --implementation cp --abi cp314 \
  -d wheels pillow==12.3.0

# macOS Intel
python3 -m pip download --only-binary=:all: --no-deps \
  --platform macosx_10_15_x86_64 --python-version 3.14 --implementation cp --abi cp314 \
  -d wheels pillow==12.3.0

# Windows x64
python3 -m pip download --only-binary=:all: --no-deps \
  --platform win_amd64 --python-version 3.14 --implementation cp --abi cp314 \
  -d wheels pillow==12.3.0
```

再下载纯 Python 依赖：

```bash
python3 -m pip download --only-binary=:all: --no-deps -d wheels \
  websockets==17.1 qrcode==8.2
```

> `qrcode` 的 wheel 是可选的：`qrcode/` 已经作为源码提交在仓库里。  
> 只缺 websockets 的话，可以只下载 `websockets==17.1`。

**注意**：`wheels/` 被 `.gitignore` 忽略，下载/复制后 `git status` 不会显示它，这是预期的。

---

## 3. Pillow：插件会自动解压

相关逻辑在 [`__init__.py`](__init__.py)：

1. 插件导入时执行 `_setup_vendored_deps()`。
2. `_ensure_vendored_pil()` 先检查插件目录（以及临时回退目录）里的 `PIL/` 是否能被当前解释器加载：
   - `PIL/__init__.py`、`PIL/Image.py`
   - `PIL/_imaging<当前解释器的扩展后缀>`（例如 `_imaging.cpython-314-x86_64-linux-gnu.so`）
3. 不匹配时，从 `wheels/` 里找出 **Python tag + 平台 tag 都匹配**的 Pillow wheel。
4. 把 wheel 里的 `PIL/` 和顶层 `*.libs/`（Linux 的 `pillow.libs/`）解压到：
   - 插件目录（可写时），否则
   - `/tmp/blecaco_deps`（系统级只读安装的回退）
5. 把插件目录/解压目录插入 `sys.path` 最前面；如果 `sys.modules` 里已经有系统 Pillow，会先移除，确保用的是自带那份。
6. [`streamer.py`](streamer.py) 导入 `PIL.Image` 后还会检查 `_is_vendored_pil(Image.__file__)`；如果命中系统 Pillow，会当作“缺 Pillow”处理，并在面板/控制台给出原因。

**你只需要**：把匹配的 Pillow wheel 放进 `wheels/`，重启 Blender（或重新加载插件）。  
不需要手动解压 Pillow；也不要把 `PIL/`、`pillow.libs/` 提交到 git。

强制重新解压（比如换了 Blender 版本/平台）：

```bash
rm -rf PIL pillow.libs
# 重启 Blender，插件会重新从 wheels/ 解压
```

如果插件目录只读，解压会落到 `/tmp/blecaco_deps`；这在当前 Blender 会话内有效，重启后可能重新解压。

---

## 4. websockets：需要手动解压

[`streamer.py`](streamer.py) 里：

```python
try:
    import websockets
    from websockets.datastructures import Headers as _WSHeaders
    from websockets.http11 import Response as _WSResponse
    HAS_WS = True
except ImportError:
    HAS_WS = False
```

所以：

- `websockets/` 不在 git 里；
- 插件**不会**自动从 wheel 解压 websockets；
- 如果没有它，插件仍能加载，但启动服务时会返回 **“缺少 websockets”**。

从 wheel 解压（纯 Python，任意 Python 3 都可以执行）：

```bash
cd /path/to/blecaco

# 方式 1：用 Python 的 zipfile
python3 -c "import zipfile; zipfile.ZipFile('wheels/websockets-17.1-py3-none-any.whl').extractall('.')"

# 方式 2：用 unzip
unzip -q wheels/websockets-17.1-py3-none-any.whl -d .
```

会在插件根目录生成：

```text
websockets/
websockets-17.1.dist-info/   ← 可选，运行时不需要，可以删
```

`websockets/` 被 `.gitignore` 忽略；解压后不要提交。  
**版本必须用 17.1**：代码用到了 `websockets.datastructures` 和 `websockets.http11`，老版本（例如 10.x）没有这些模块。

> 如果插件目录只读、无法解压：把 `websockets/` 解压到别处，或安装到 Blender 自带 Python 的 site-packages；  
> 但最推荐还是让插件目录可写，和 Pillow 的 vendored 方式保持一致。

---

## 5. qrcode：已跟踪，不需要恢复

`qrcode/` 是**纯 Python**，已经作为源码提交在仓库里（34 个文件），直接可用。  
它生成图片时会用到 Pillow 的 `PilImage`，所以只要 Pillow 恢复好，二维码功能就正常。

`wheels/qrcode-8.2-py3-none-any.whl` 只是备用/完整打包，不是运行必需。

---

## 6. 安装到 Blender

插件文件夹名建议保持 `blecaco`。

### 6.1 路径

把整个插件目录放到 Blender 的 addons 目录：

- **Linux**：`~/.config/blender/5.2/scripts/addons/blecaco`
- **Windows**：`%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\blecaco`
- **macOS**：`~/Library/Application Support/Blender/5.2/scripts/addons/blecaco`

> 把 `5.2` 换成你的 Blender 版本（4.5/4.6/5.2…）。  
> 插件 `bl_info` 最低要求 Blender 4.5。

### 6.2 开发用软链接（Linux/macOS）

```bash
ln -s /path/to/blecaco \
  ~/.config/blender/5.2/scripts/addons/blecaco
```

这样插件目录就是 git 仓库本身；`wheels/`、`PIL/`、`websockets/` 的忽略规则也直接生效。

### 6.3 普通安装（复制/zip）

- 直接复制整个文件夹（包含恢复好的 `wheels/`、`websockets/`）到 addons 目录；或
- 用 `Edit → Preferences → Add-ons → Install...` 选择 zip。

⚠️ 如果打成 zip，**要先把 `wheels/` 和 `websockets/` 恢复好再打包**；  
否则安装后同样缺依赖。

### 6.4 启用

`Edit → Preferences → Add-ons`，搜索 **Blecaco**，勾选启用。

插件导入时会执行 `_setup_vendored_deps()`；如果之后才补 `wheels/`/`websockets/`，需要**重启 Blender** 或重新加载插件。

---

## 7. 验证

### 7.1 Blender Python 控制台

```python
import sys
print(sys.version)

import PIL, websockets, qrcode
print("PIL       ", PIL.__version__, PIL.__file__)
print("websockets", websockets.__version__, websockets.__file__)
print("qrcode    ", qrcode.__file__)
```

期望：

- `PIL.__file__` 在插件目录或 `/tmp/blecaco_deps` 下，**不是** Blender 的 site-packages；
- `websockets.__file__` 在插件目录下；
- `qrcode.__file__` 在插件目录下。

### 7.2 插件面板/控制台

- 第一次解压 Pillow 时会打印类似：
  `[blecaco] 已从 pillow-...whl 解压自带 Pillow 到 ...`
- 启动服务失败时，面板会显示具体原因：
  - `缺少 Pillow：wheels/ 里没有适配 Python ...` → 缺 wheel / 平台或 Python 版本不匹配
  - `缺少 websockets` → 第 4 节没做
  - `Blender GPU API 不可用` → 用了 `--background` 或没有 GPU 上下文

### 7.3 启动服务

在 3D 视图侧栏 `Blecaco` 面板点 **Start**；默认端口 **12121**。  
手机端连接同一端口的 WebSocket 服务。

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `缺少 Pillow：wheels/ 里没有适配 Python 3.14 + 本平台` | `wheels/` 没有对应 `cp314` + 平台标签的 Pillow wheel；按第 2 节下载 |
| `命中系统 Pillow` / 一直提示缺 Pillow | 插件目录里的 `PIL/` ABI 不匹配；删掉 `PIL/`、`pillow.libs/` 后重启，让它重新解压 |
| `缺少 websockets` | 执行第 4 节的解压命令；确认 `websockets/http11.py` 存在 |
| `ImportError: cannot import name 'Response' from 'websockets.http11'` | websockets 版本不对；用 17.1 |
| `ModuleNotFoundError: No module named 'qrcode'` | 仓库的 `qrcode/` 被删了；重新 clone 或恢复该目录（它是 tracked） |
| Blender 启动报 `No module named PIL` / 插件加载失败 | 先用 Blender 的 Python 控制台按第 7 节检查；确认 `PIL/` 或 `wheels/` 在插件目录 |
| `wheels/` 在 `git status` 里看不到 | 正常，`.gitignore` 忽略了它 |
| `PIL/`/`websockets/` 在 `git status` 里看不到 | 正常，都是解压产物，被忽略 |
| 换 Blender 版本后 Pillow 失效 | Pillow 是 ABI 相关的；下载对应 `cp3xx` 的 wheel，删 `PIL/` 后重启 |
| 插件目录只读，Pillow 解压到 `/tmp` | 可以正常工作；但 `websockets/` 仍需放在可导入的位置 |
| 端口 12121 启动失败 | 端口被占用；在插件面板改端口，或关掉占用进程 |

---

## 9. 为什么这些文件不 push

- `wheels/`：24MB，包含多平台/多 Python 版本的二进制 wheel；放 git 会让仓库变大，而且大部分人只需要自己平台的那一份。
- `PIL/` + `pillow.libs/`：21MB，是 Pillow wheel 的解压产物，可随时从 `wheels/` 重新生成。
- `websockets/`：1.3MB，纯 Python，可从 wheel 解压。
- `__pycache__/`：字节码，自动生成。

因此仓库只保留源码 + 纯 Python 的 `qrcode/`。  
如果你希望“clone 下来零配置”，可以选择：

- 把 `wheels/` 一起提交（24MB，GitHub 单文件限制 100MB 内，完全可行）；或
- 写一个 `setup_deps` 脚本，从 PyPI 下载并解压。

目前本仓库采用前者之外的方案：**源码进 git，wheel 和解压产物本地恢复**。  
本文件就是恢复步骤。

---

## 10. 最短恢复清单

```bash
# 1. 拿到 wheels/（从原机器复制，或按第 2 节 pip download）
# 2. 解压 websockets
cd /path/to/blecaco
python3 -c "import zipfile; zipfile.ZipFile('wheels/websockets-17.1-py3-none-any.whl').extractall('.')"

# 3. 安装/软链接到 Blender addons 目录
ln -s /path/to/blecaco ~/.config/blender/5.2/scripts/addons/blecaco

# 4. 启动 Blender → Preferences → Add-ons → 启用 Blecaco
# 5. 点 Start；Pillow 会自动从 wheels/ 解压
```
