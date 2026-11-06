# provider.iflyime.streaming

讯飞输入法（iFlytek IME）流式云端语音识别。通过非官方客户端协议连接 `100ime-iat-api.xfyun.cn`（讯飞输入法 App 内置听写通道），不是讯飞开放平台公共 ASR API。与 `provider.xfyun.streaming`（官方开放平台 WebAPI，需申请 AppID/APIKey/APISecret）互补：本提供商零 Key、零配置，装上即用。

## 使用建议：先试原始转写

讯飞识别质量较高，普通口述输入通常无需再接一层 LLM 整理。建议先选择 Vinput 的「原始」场景：

```bash
vinput provider use provider.iflyime.streaming
vinput scene use __raw__
```

需要翻译、改写语气或重组结构时再启用相应 LLM 场景。

## 入口与运行依赖

- 入口：`entry.py`；命令：`python3`。
- Python 仅使用标准库，无需 pip 包或随插件分发的二进制。
- 不需要 libopus：讯飞 IAT 通道接收 16 kHz 裸 PCM（base64），音频不经编码直接上传。
- 无需用户 API key 或账号登录：鉴权使用讯飞输入法 App 内嵌的固定应用参数（`100IME`），与用户账号无关，不做设备注册，不写任何缓存文件。
- 输入为 Vinput 流式 JSONL：16 kHz、单声道、S16LE PCM，`audio_base64` 携带音频。
- 直接建立 TLS WebSocket 连接，不读取 HTTP 代理环境变量。

## 环境变量

### 必填

无。

### 可选

- `VINPUT_ASR_TIMEOUT`：WebSocket 连接超时，默认 `15` 秒。
- `VINPUT_ASR_FINISH_GRACE_SECS`：`finish` 后等待服务端定稿的上限，默认 `5` 秒（上限 9 秒）。

## 流式行为

- 新录音建立新连接；鉴权签名（HMAC-SHA256）随 URL 携带，date 为当前 GMT 时间，依赖系统时钟准确。
- PCM 每 40 ms（1280 字节）封装为一个 JSON 文本帧上传；首帧携带 `common`/`business` 参数（`zh_cn`/`iat`/`mandarin`，`dwa=wpgs` 渐进式，`ptt=1` 标点）。
- 连接采用 IPv4 优先 + 单地址 2 秒独立超时，个别路由不可达时立即切换下一条，避免会话建立被阻塞。
- `session_started` 后输出累计 `partial`；`wpgs` 的 `pgs=rpl` 区间替换按 `sn` 有序合并后再输出。
- `finish` 后发送 `status=2` 结束帧并等待定稿（上限 finish grace），整个录音仅输出一次 `final`，随后 `closed`。
- `cancel` 或未发送 `finish` 的 EOF 均立即中止，不输出最终文本。
- 网络或协议错误通过 `error` 和 stderr 报告，不静默重传整段音频。
- stderr 输出单行诊断：`[iflyime] connected to ...` / `[iflyime] 上传完成: N帧 N字节 N秒` / `[iflyime] 识别定稿: ...`。

## 验证与协议来源

- Linux Mint 22.3 / Vinput 2.4.2 真机实测：HMAC 鉴权与握手一次通过；真实口述（中英混说、数字、标点）识别正确，流式 `partial` 渐进修正可见；`cancel` 干净退出；端点不可达 0.1s 内 `error`+`closed` 不挂死。
- 已知限制：连接无麦克风的蓝牙音频设备（HFP"免手操作"虚拟输入源）抢占系统默认录音源时，任何 provider 都只会收到静音音频，断开该设备即恢复。
- 协议参考：[VoiceKey 的讯飞实现](https://github.com/J3n5en/voicekey/blob/main/crates/core/src/ifly.rs)（Rust）。Python WebSocket 基础代码沿用本仓库豆包输入法提供商模板，讯飞协议为 Python 独立实现。

这是输入法客户端的非官方接口，服务端协议、凭证或可用性可能变化。仅供学习与个人使用，请勿用于商业用途。录音会发送到讯飞输入法云端识别服务。
