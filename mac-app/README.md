# CortexI — Mac 端

常驻 Mac 的会议副脑。菜单栏小工具 + 本地界面。

- 🎙 录制**系统音频 + 麦克风**（通过 BlackHole 聚合设备，能录到腾讯会议/Zoom 里对方的声音）
- 📝 **本地 whisper.cpp 转写**（音频永远不离开你的 Mac，只有文字上传，NDA 友好）
- 📷 随时拖拽上传图片/文件
- 🤖 远程 **Claude Code（Bedrock）** 实时分析 + 会后总结
- 🔒 通道：Mac → CloudFront(HTTPS) → ALB(仅 CloudFront 可达) → 私有 EC2 → CC

## 目录

```
mac-app/
├── config.example.json     # 复制成 config.json 填你的服务地址+token
├── cortexi.py       # 菜单栏 App 主程序 (rumps)
├── audio.py                 # 录音 + whisper.cpp 转写 worker
├── remote.py                # 远程服务客户端
├── ui/index.html            # 本地界面 (localhost)
├── requirements.txt
├── install.sh               # 一键安装 (BlackHole/whisper.cpp/依赖)
└── README.md
```

## 快速开始

```bash
cd mac-app
./install.sh                 # 装 BlackHole、whisper.cpp、python 依赖
cp config.example.json config.json
# 编辑 config.json 填 server_url + app_token
python3 cortexi.py   # 菜单栏出现 🧠
```

安装脚本会引导你建一个「聚合设备」把系统音频+麦克风混到一起给录音用。详见 README 里的 BlackHole 部分。
