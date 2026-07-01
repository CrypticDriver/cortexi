# CortexI — Mac 端

常驻 Mac 的会议副脑。原生 App / 菜单栏小工具 + 本地界面。

- 🎙 录制**系统音频 + 麦克风**（通过 BlackHole 聚合设备，能录到腾讯会议/Zoom 里对方的声音）
- 📝 **本地 whisper.cpp 转写**（音频永远不离开你的 Mac，只有文字上传，NDA 友好）
- 📷 随时拖拽上传图片/文件
- 🤖 远程 **Claude Code（Bedrock）** 实时分析 + 会后总结
- 🔒 通道：Mac → CloudFront(HTTPS) → ALB(仅 CloudFront 可达) → 私有 EC2 → CC

## 目录

```
mac-app/
├── config.example.json     # 参考格式（真正的配置存到 ~/.meeting-copilot/config.json）
├── cortexi.py              # 主程序 (rumps 菜单栏 + 本地 HTTP UI)
├── audio.py                # 录音 + whisper.cpp 转写 worker
├── remote.py               # 远程服务客户端
├── ui/index.html           # 本地界面 (localhost，含录音/总结/设置按钮)
├── setup.py                # py2app 打包配置
├── build-app.sh            # 一键打包成 CortexI.app
├── requirements.txt
├── install.sh              # 一键安装 (BlackHole/whisper.cpp/依赖)
└── README.md
```

## 快速开始

### 第 1 步：装依赖（一次性）

```bash
cd mac-app
./install.sh                 # 装 BlackHole、whisper.cpp、模型、python 依赖
```

安装脚本会引导你建一个「聚合设备」把系统音频+麦克风混到一起给录音用（见下方 BlackHole 部分）。

### 第 2 步：打包成原生 App（推荐）

```bash
./build-app.sh               # 产出 dist/CortexI.app
```

双击 `dist/CortexI.app` 即开（可拖进「应用程序」文件夹长期使用），菜单栏出现 🧠。

> **为什么打包成 App？** 裸 `python3 cortexi.py` 在部分 Python 版本（如 3.13 + 旧 rumps）下，
> 进程正常运行但**菜单栏 🧠 图标挂不上也不报错**。打包成 `.app` 后，bundle 有正确的 GUI 身份
> （`LSUIElement`），菜单栏图标就能稳定显示。这是最靠谱的用法。

### 第 3 步：首次配置（在界面上填，无需改文件）

1. 打开浏览器访问 `http://127.0.0.1:8787`（或菜单栏 🧠 →「打开本地界面」）
2. 点右上角 **⚙️ 设置**
3. 填 **Server URL**（CloudFront 域名）和 **App Token**，点「保存并测连接」
4. 看到「✅ 已保存，连接正常」即可开始用

配置会存到 `~/.meeting-copilot/config.json`，App 升级不丢。

## 界面按钮

网页界面右上角：
- **● 开始录音 / ■ 停止录音** —— 开/关一场会议（本地转写实时出字）
- **📝 会后总结** —— 生成结构化纪要
- **⚙️ 设置** —— 配置 server_url / app_token

> 所有操作在网页上都能完成，**不依赖菜单栏图标**。就算图标没显示，功能照常。

## 也可以直接裸跑（调试用）

```bash
python3 cortexi.py           # 菜单栏可能出现 🧠；浏览器开 127.0.0.1:8787 一定可用
```

## 常见问题

- **菜单栏没有 🧠 图标？** 用 `./build-app.sh` 打包成 App 跑；或直接用浏览器界面（`127.0.0.1:8787`），功能完整。
- **点录音没出字？** 检查 BlackHole 聚合设备是否已建、命名为 `MeetingCopilot-Aggregate`；并在
  系统设置 → 隐私与安全性 → 麦克风 里给 App/终端授权。
- **首次打开 App 提示「无法验证开发者」？** 右键 App → 打开 → 打开；或系统设置 → 隐私与安全性 → 仍要打开。

## BlackHole 聚合设备（录系统音频必需）

1. 打开「音频 MIDI 设置」(Audio MIDI Setup)
2. 左下 `+` → 「创建聚合设备」，勾选：你的麦克风 + BlackHole 2ch
3. 重命名为 `MeetingCopilot-Aggregate`（要和配置里的 capture_device 一致）
4. 再建一个「多输出设备」勾选 BlackHole 2ch + 你的扬声器/耳机；开会时把系统输出选到这个多输出设备
   —— 这样你能听到声音，同时 BlackHole 也拿到系统音频。
