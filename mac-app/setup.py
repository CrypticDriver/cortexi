"""
py2app 打包配置 —— 把 CortexI 封装成原生 CortexI.app

用法（在 Mac 上）：
    pip3 install -r requirements.txt      # 含 py2app
    python3 setup.py py2app               # 产出 dist/CortexI.app

产物 dist/CortexI.app 双击即开，菜单栏出现 🧠。
把它拖进「应用程序」文件夹即可长期使用。

说明：
- LSUIElement=True  -> app 以「仅菜单栏」模式运行，不在 Dock 显示图标、
  不抢占前台，菜单栏才能稳定挂上 🧠（裸 python3 跑挂不上的根因）。
- NSMicrophoneUsageDescription -> 录音权限弹窗文案（macoS 强制要求）。
- ui/ 与 config.example.json 作为资源打进 app 包内。
"""
from setuptools import setup

APP = ["cortexi.py"]
DATA_FILES = [
    ("", ["config.example.json"]),
    ("ui", ["ui/index.html"]),
]
OPTIONS = {
    "argv_emulation": False,
    "packages": ["rumps", "requests", "certifi", "charset_normalizer", "idna", "urllib3"],
    "includes": ["remote", "audio"],
    "iconfile": None,  # 可选：放一个 icon.icns 后改成 "icon.icns"
    "plist": {
        "CFBundleName": "CortexI",
        "CFBundleDisplayName": "CortexI",
        "CFBundleIdentifier": "ai.cortexi.menubar",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,  # 仅菜单栏模式（关键）
        "NSMicrophoneUsageDescription": "CortexI 需要麦克风与系统音频权限，用于本地实时转写会议内容（音频不出本机）。",
        "NSHighResolutionCapable": True,
    },
}

setup(
    app=APP,
    name="CortexI",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
