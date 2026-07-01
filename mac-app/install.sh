#!/usr/bin/env bash
# Meeting Copilot — Mac 端一键安装
# 装: Homebrew 依赖(ffmpeg, BlackHole, cmake) + whisper.cpp(编译) + 模型 + python 依赖
set -euo pipefail

MC_HOME="${HOME}/.meeting-copilot"
WHISPER_DIR="${MC_HOME}/whisper.cpp"
MODEL_DIR="${MC_HOME}/whisper"
MODEL="${MC_WHISPER_MODEL:-medium}"   # tiny/base/small/medium/large-v3；中文建议 medium 起

echo "==> Meeting Copilot 安装开始"
mkdir -p "${MC_HOME}" "${MODEL_DIR}"

# 1) Homebrew
if ! command -v brew >/dev/null 2>&1; then
  echo "!! 未检测到 Homebrew。请先安装: https://brew.sh 然后重跑本脚本"
  exit 1
fi

# 2) 基础依赖
echo "==> brew 安装 ffmpeg / cmake / blackhole-2ch"
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list cmake  >/dev/null 2>&1 || brew install cmake
# BlackHole 虚拟音频（录系统音频必需）
if ! system_profiler SPAudioDataType 2>/dev/null | grep -qi "BlackHole"; then
  echo "==> 安装 BlackHole 2ch (虚拟音频设备)"
  brew install blackhole-2ch || echo "!! BlackHole 安装可能需要在系统设置里授权，稍后手动确认"
fi

# 3) whisper.cpp
if [ ! -x "${WHISPER_DIR}/build/bin/whisper-cli" ]; then
  echo "==> 克隆并编译 whisper.cpp"
  rm -rf "${WHISPER_DIR}"
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp "${WHISPER_DIR}"
  cmake -S "${WHISPER_DIR}" -B "${WHISPER_DIR}/build" -DGGML_METAL=ON >/dev/null
  cmake --build "${WHISPER_DIR}/build" --config Release -j >/dev/null
fi

# 4) 模型
MODEL_FILE="${MODEL_DIR}/ggml-${MODEL}.bin"
if [ ! -f "${MODEL_FILE}" ]; then
  echo "==> 下载 whisper 模型: ${MODEL}"
  bash "${WHISPER_DIR}/models/download-ggml-model.sh" "${MODEL}"
  # download script drops model inside whisper.cpp/models; move it
  SRC="${WHISPER_DIR}/models/ggml-${MODEL}.bin"
  [ -f "${SRC}" ] && cp "${SRC}" "${MODEL_FILE}"
fi

# 5) python 依赖
echo "==> 安装 python 依赖 (rumps, requests)"
python3 -m pip install --user -q -r "$(dirname "$0")/requirements.txt"

# 6) config
CFG="$(dirname "$0")/config.json"
if [ ! -f "${CFG}" ]; then
  cp "$(dirname "$0")/config.example.json" "${CFG}"
  # 填入本机 whisper 路径
  python3 - "$CFG" "$WHISPER_DIR" "$MODEL_FILE" <<'PY'
import json,sys
cfg,wd,mf=sys.argv[1],sys.argv[2],sys.argv[3]
d=json.load(open(cfg))
d["whisper"]["whisper_cli"]=wd+"/build/bin/whisper-cli"
d["whisper"]["model_path"]=mf
json.dump(d,open(cfg,"w"),ensure_ascii=False,indent=2)
print("已写入 whisper 路径到 config.json")
PY
  echo "!! 请编辑 ${CFG} 填入 server_url 和 app_token"
fi

cat <<'NEXT'

==> 安装完成 ✅

还差两步（手动，一次性）：

【A】建聚合设备（录系统音频+麦克风）
  1. 打开「音频 MIDI 设置」(Audio MIDI Setup)
  2. 左下 + → 「创建聚合设备」，勾选：你的麦克风 + BlackHole 2ch
  3. 重命名为：MeetingCopilot-Aggregate  （要和 config.json 里的 capture_device 一致）
  4. 再建一个「多输出设备」勾选 BlackHole 2ch + 你的扬声器/耳机，
     开会时把系统输出选到这个多输出设备 —— 这样你能听到声音，同时 BlackHole 也拿到系统音频。

【B】填 config.json
  server_url = 你的 CloudFront 域名 (https://xxx.cloudfront.net)
  app_token  = 部署输出的 token

然后启动：
  python3 cortexi.py
  菜单栏出现 🧠 即可。

NEXT
