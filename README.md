<div align="center">
  <h1>麦麦 MaiBot <sub><small>MaiCore</small></sub></h1>
  <p>本仓库是基于 <a href="https://github.com/MaiM-with-u/MaiBot">MaiBot</a> 二次开发的个人定制版本。</p>
  
  <!-- Badges Row -->
  <p>
    <img src="https://img.shields.io/badge/Python-3.15-blue" alt="Python Version">
    <img src="https://img.shields.io/github/license/Mai-with-u/MaiBot?label=%E5%8D%8F%E8%AE%AE" alt="License">
    <img src="https://img.shields.io/badge/状态-开发中-yellow" alt="Status">
  </p>
</div>

<br>

<!-- Mascot on the Right (Float) -->
<img src="depends-data/maimai-v2.png" align="right" width="40%" alt="MaiBot Character" style="margin-left: 20px; margin-bottom: 20px;">

## 🎉 介绍

**🍔 MaiCore 是一个基于大语言模型的可交互智能体**

MaiBot 不仅仅是一个机器人，她致力于成为一个活跃在 QQ 群聊中的“生命体”。她不追求完美，但追求真实。

- 💭 **拟人构建**：使用自然语言风格构建 Prompt，回复贴近人类习惯。
- 🎭 **行为规划**：懂得在合适的时间说话，使用合适的动作。
- 🧠 **表达学习**：模仿群友的说话风格，学习黑话，不断进化。
- 🔌 **插件系统**：提供强大的 API 和事件系统，无限扩展可能。
- 💝 **情感表达**：拥有独立的情绪系统和表情包互动能力。

### 🚀 快速导航
<p>
  <a href="https://www.bilibili.com/video/BV1amAneGE3P">🌟 演示视频</a> &nbsp;|&nbsp; 
  <a href="#-更新和安装">📦 快速入门</a>
</p>

<!-- Clear float to ensure subsequent content starts below the image area if text is short -->
<br clear="both">

<div align="center">
  <br>
  <h3>🎥 精彩演示</h3>
  <a href="https://www.bilibili.com/video/BV1amAneGE3P" target="_blank">
    <picture>
      <source media="(max-width: 600px)" srcset="depends-data/video.png" width="100%">
      <img src="depends-data/video.png" width="60%" alt="麦麦演示视频" style="border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1);">
    </picture>
    <br>
    <small>👆 点击观看麦麦演示视频 👆</small>
  </a>
</div>

---

## 🔥 更新和安装

需要 Python 3.15，推荐使用 uv：

```bash
uv python install 3.15
uv sync --locked --no-dev --python 3.15
uv run --locked --no-dev python bot.py
```

### 图片与后续消息

群聊和私聊默认等待图片处理完成后，再回答后面的消息。多张图片仍会并行识别，插件命令可以立即执行。

如果希望后续消息先处理，在 `config/bot_config.toml` 的 `[chat]` 中设置：

```toml
wait_for_image_recognition = false
```

修改后重启 MaiBot 生效。设回 `true` 即可恢复等待。

---

## 📌 注意事项 & License

> [!IMPORTANT]
> 使用前请阅读 [用户协议 (EULA)](EULA.md) 和 [隐私协议](PRIVACY.md)。AI 生成内容请仔细甄别。

**License**: GPL-3.0
