# provider.wetype.streaming

微信输入法（WeType）流式云端语音识别。通过非官方客户端协议连接 `wetype.weixin.qq.com`，不是腾讯云公共 ASR API。

## 使用建议：先试原始转写

本地中文样例和实际口述体验显示识别质量较高，普通口述输入通常无需再接一层 LLM 整理。建议先选择 Vinput 的「原始」场景：

```bash
vinput provider use provider.wetype.streaming
vinput scene use __raw__
```

原始场景仍会保留微信 ASR 服务端返回的标点和数字格式等定稿修正，只跳过 Vinput 后续的 LLM。需要翻译、改写语气或重组结构时再启用相应 LLM 场景。该建议基于实测体验，不代表所有口音、语言、噪声环境的准确率保证。安装本提供商不会自动修改场景。

**English:** Local sample tests and live dictation feedback indicate high-quality ASR output. For everyday dictation, try Vinput's Original/raw scene first: a separate LLM cleanup step is generally unnecessary in our experience. Server-side transcript finalization is retained; use an LLM scene when you explicitly want rewriting, translation, or restructuring. This is a practical recommendation, not a general accuracy benchmark.

## 入口与运行依赖

- 入口：`entry.py`；命令：`python3`。
- Python 仅使用标准库，无需 pip 包或随插件分发的二进制。
- 系统需要 `libopus` 和 OpenSSL `libcrypto`（通过 `ctypes` 调用）；Ubuntu/Debian 对应 `libopus0` 和系统 OpenSSL 运行库。
- 输入为 Vinput 流式 JSONL：16 kHz、单声道、S16LE PCM，`audio_base64` 携带音频。
- 无需用户 API key 或微信账号登录。插件自动注册协议设备身份并以 `0600` 权限缓存；会话密钥仅保存在内存。
- 直接建立 TLS WebSocket 连接，不读取 HTTP 代理环境变量。

## 环境变量

### 必填

无。注册表不写入可选变量占位值。

### 可选

- `VINPUT_ASR_CREDENTIAL_PATH`：设备身份缓存，默认 `~/.cache/vinput/wetype/identity.json`。
- `VINPUT_ASR_TIMEOUT`：连接和单次服务端响应超时，默认 `15` 秒。
- `VINPUT_ASR_FINISH_GRACE_SECS`：处理结束事件后的定稿等待上限，默认 `5` 秒；已有临时文本时，定稿超时会保留最新文本。
- `VINPUT_ASR_LIBOPUS_PATH`：系统 `libopus` 的自定义动态库路径；默认自动查找。

不要提交设备身份缓存或将其放入注册表。

## 流式行为

- 新录音建立新连接，复用缓存的设备身份；失效身份重新注册。
- PCM 每 20 ms 编码为 Opus，6 帧一批上传（120 ms）。
- `session_started` 后输出累计 `partial`；中途服务端修正不会触发提前上屏。
- `finish` 后上传余音并轮询定稿，整个录音仅输出一次 `final`，随后 `closed`。
- `cancel` 或未发送 `finish` 的 EOF 均中止，不输出最终文本；网络等待中的取消也可及时退出。
- 网络或协议错误通过 `error` 和 stderr 报告，不静默重新上传整段音频。

## 验证与协议来源

- 本地 5.592 秒中文公开样例，两次实时推送均识别为「开放时间，早上9点至下午5点。」；原型 finish 后约 0.8–1.1 秒定稿。
- 最初临时结果可能不准确（样例出现过「开饭时间」），服务端定稿会继续修正。
- 用户实际口述确认效果很好；这不是系统性多语言准确率评测。
- 自动测试：`python3 -m unittest discover -s tests -p 'test_wetype_streaming.py'`，覆盖密码学已知向量、协议编解码、会话结束和取消。
- 协议参考：[VoiceKey 的 WeType 实现](https://github.com/J3n5en/voicekey/blob/main/crates/core/src/wetype.rs)，客户端参数 `2.2.3(657)`。Python WebSocket/Opus 基础代码沿用本仓库豆包输入法提供商，WeType 协议使用 Python 独立实现。

这是输入法客户端的非官方接口，服务端协议或可用性可能变化。录音会发送到微信输入法云端识别服务。
