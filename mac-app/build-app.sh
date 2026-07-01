#!/usr/bin/env bash
# CortexI —— 打包成原生 CortexI.app（在 Mac 上运行）
#
# 产物: dist/CortexI.app  （双击即开，菜单栏出现 🧠）
#
# 前置: 已跑过 install.sh（装好 whisper.cpp / ffmpeg / BlackHole / 模型）。
#       打包本身只需要 python3 + py2app。
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 1/3 安装打包依赖 (py2app + 运行时依赖)"
python3 -m pip install -q -r requirements.txt

echo "==> 2/3 清理旧产物"
rm -rf build dist

echo "==> 3/3 py2app 打包"
# --semi-standalone 更小、依赖系统 python；想完全独立可去掉该参数（体积更大但不依赖本机 python）
python3 setup.py py2app

APP="dist/CortexI.app"
if [ -d "$APP" ]; then
  cat <<DONE

==> 打包完成 ✅  ->  $APP

用法:
  1. 双击 dist/CortexI.app 打开（或拖进「应用程序」文件夹长期使用）
  2. 菜单栏出现 🧠，点开菜单 / 或浏览器打开 http://127.0.0.1:8787
  3. 首次使用点「⚙️ 设置」填 server_url 和 app_token，保存即用

首次打开若提示「无法验证开发者」:
  右键 App -> 打开 -> 打开；或 系统设置 -> 隐私与安全性 -> 仍要打开
录音需在 系统设置 -> 隐私与安全性 -> 麦克风 里给 CortexI 打钩。
DONE
else
  echo "!! 打包失败，未生成 $APP" >&2
  exit 1
fi
