# 麦克风与扬声器语音对话

0.6.0 起推荐使用 `python3 -m consciousness` 同时启动 Web 管理台，界面可查看逐轮语音内容和控制收听；见 [管理台说明](dashboard.md)。以下 `voice` 为保留的独立命令。

运行入口：`python3 -m consciousness voice`。本版支持 Linux ALSA；需要系统命令 `arecord` 和 `aplay`（Debian/Ubuntu 的 `alsa-utils`）。无需本地模型或 Python 音频依赖。

## 配置与启动

先按 [云端模型说明](cloud-models.md) 配置模型服务和密钥。知觉模型必须支持当前兼容协议的 `input_audio` WAV 输入；设置 `BOT_MODEL_MODALITIES=text,audio`，必要时通过 `BOT_MODEL_PERCEPTION` 指定音频模型。表达任务可通过 `BOT_MODEL_EXPRESSION` 指定文本模型，两者使用同一个模型服务地址。

还需配置独立的云端语音合成接口：

```sh
export BOT_TTS_BASE_URL=https://your-speech-provider.example/v1
export BOT_TTS_MODEL=your-tts-model
export BOT_TTS_VOICE=your-voice-id
# 本机设置 BOT_TTS_API_KEY；或者显式引用已有密钥变量：
export BOT_TTS_API_KEY_ENV=BOT_MODEL_API_KEY
export BOT_MODEL_MODALITIES=text,audio

python3 -m consciousness voice --check
python3 -m consciousness voice --data-dir local-data/voice --turns 10
```

示例域名与模型名需要替换；`--check` 只检查 ALSA 命令可用性，不打开设备、不验证云端。若原模型密钥通过环境变量引用配置，TTS 引用应指向真正含密钥的变量，不支持多层引用。两个密钥入口不能同时设非空值。

TTS 使用兼容 `POST /audio/speech` 的接口，请求包含 `model`、`voice`、`input`、`response_format=wav`，参考 [Speech API 契约](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)。仅接受完整 PCM16 WAV，最长 120 秒、最大 16 MiB；其他格式或协议需要另加适配。模型和 TTS 的供应商可以不同，凭据需分别明确配置；程序不会把模型密钥自动发送给 TTS 地址。

## 使用过程

1. 所有云配置通过检查后显示 `listening`，打开麦克风。录音片段会发送至配置的云模型。
2. 16 kHz 单声道 PCM16 音频按 20 ms 帧检测能量；保留 200 ms 前置音频，至少 200 ms 超门限音频才视为有效片段。静音 0.8 秒或片段达到 15 秒即结束采集。
3. 关闭麦克风后调用知觉提示词，随后调用表达提示词生成中文短回答。听不清或存在不确定观察时请求重说。模型只做对话，不执行语音中提及的设备操作。
4. 调云端 TTS 合成回答，扬声器播放；显示 `played` 后再进入下一轮。播放期间不录音，不能语音打断，Ctrl+C 可停止并释放子进程。

默认最多 10 轮，无有效片段 30 秒后退出。可用参数：

- `--capture-device default` / `--playback-device default`：指定 ALSA 输入/输出；用 `arecord -L` / `aplay -L` 查看名称。
- `--threshold 600`：PCM16 RMS 门限，嘈杂时调高、声音过小时调低。
- `--silence 0.8`、`--max-utterance 15`、`--idle-timeout 30`、`--turns 10`：控制分段和会话时长。

这是能量门限分段，不是能区分语音与噪声的学习模型。持续噪声也可能触发上传；尚无唤醒词、回声消除、全双工打断或跨轮对话历史装配。问答使用当前语音观察，不能声称已经执行设备操作。

## 状态与故障

每轮通常消耗两次模型调用和一次 TTS 请求。模型调用沿用持久预算，TTS 另有持久尝试计数，上限同 `BOT_MODEL_MAX_CALLS`；该限制不是货币预算。失败和取消不自动重试；网络、合成或播放失败会退出，不显示虚假播放成功。

TTS 尝试前写入状态，播放前记录 `playback_pending`、成功后记录 `played`。如果崩溃留下 pending，重启不自动重播该条语音；无法保证播放中途崩溃时听者实际听到了多少内容。数据目录使用现有 Journal 独占锁，避免同一目录并行运行两个语音会话。

临时 WAV 在每轮结束或正常异常退出时删除，不纳入 Git；进程被强制杀死可能残留系统临时文件。云端观察、回复候选及调用记录仍按原日志规则持久保存。扬声器发出的是 AI 合成语音。

## 本机验证边界

2026-10-29：已在本机打开默认麦克风并采集 16 kHz PCM，验证停止后子进程退出；默认扬声器完成 200 ms 低音量测试音播放，进程返回成功。这不替代人工确认音质或准确率。

协议与故障测试使用本机 HTTP 模拟服务。未配置真实云模型和 TTS 密钥，尚未执行真实语音理解、中文合成质量和端到端延迟评估。程序完成接入并不表示服务账号已经就绪。

本轮全量 `python3 -m unittest discover -s tests`：141 项通过，30.342 秒，包含 13 项语音专项测试。
