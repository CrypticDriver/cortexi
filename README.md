# 🧠 CortexI (副脑一代)

> A meeting second-brain for macOS. Records your meeting (system audio + mic),
> transcribes **locally** (audio never leaves your Mac), and a remote
> **Claude Code / Bedrock** brain does live analysis and post-meeting summaries.

CortexI 是常驻 Mac 的「会议副脑」：开会自动录音、本地转写、远程 AI 实时分析、会后自动出结构化纪要。音频永不离开你的电脑，只有文字上传 —— 对 NDA 场景友好。

```
Mac 菜单栏 App 🧠
  ├─ 系统音频 + 麦克风 (BlackHole 聚合设备)
  ├─ whisper.cpp 本地转写  ← 音频不出本机
  ├─ 本地界面 (实时转写 / 上传文件 / 实时问答 / 会后纪要)
  └─ HTTPS → CloudFront → ALB(仅CloudFront可达) → 私有EC2 → Claude Code(Bedrock)
```

## ✨ 功能

- 🎙 一键录音（能录到腾讯会议/Zoom 里对方的声音）
- 📝 本地 whisper.cpp 转写，隐私不出本机
- 📷 随时拖拽上传图片/文件进会议上下文
- 💬 开会中随时问「刚才决定了什么」
- 📋 会后一键生成结构化纪要（概述/讨论点/决定/待办/风险）
- 🔒 零直接公网暴露：EC2 私有，ALB 仅 CloudFront 可达 + 密钥头 + Bearer token

## 📦 下载客户端

到 [Releases](../../releases) 下载最新 `cortexi-mac-*.tar.gz`，解压后见 `mac-app/README.md`。

## 🚀 快速开始

**服务端**（一次性，AWS 一键部署）：见 [`deploy/README.md`](deploy/README.md) —— `aws cloudformation deploy` 一条命令。

**客户端**（Mac）：
```bash
cd mac-app
./install.sh                      # 装 ffmpeg / BlackHole / whisper.cpp / 模型
cp config.example.json config.json # 填 server_url + app_token（来自服务端部署输出）
python3 cortexi.py                # 菜单栏出现 🧠
```

## 🗺 Roadmap

- **CortexI** — 本代：Mac 采集 + 本地转写 + CC 分析
- **CortexII** — 下一代：加智能眼镜被动感知层

## License

MIT
