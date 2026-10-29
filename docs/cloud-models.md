# 云端模型与提示词方案 · 0.5.0

2026-10-29 起，生产模型推理统一调用云端 API，不下载本地模型、不训练基础模型，也不在请求失败时用预设答案冒充模型结果。Python 本地程序负责状态、证据、预算、计划校验和串行动作；云模型负责语义理解与候选生成。

目前实现 HTTPS Chat Completions 兼容协议：向配置的 Base URL 追加 `/chat/completions`，使用 Bearer 认证。供应商、模型和模态能力由部署配置指定，不把某个品牌或模型名写死为默认值。原生非兼容协议需要另写传输适配器，不能仅更换域名。

## 配置并运行

```sh
cp .env.example .env
# 用本机编辑器填写 Base URL、模型 ID 和 API Key。
set -a
. ./.env
set +a

# 先检查提示词，不发网络请求、不需要密钥。
python3 -m consciousness prompts
python3 -m consciousness prompts plan

# 调用真实云接口，只保存校验通过的计划候选。
python3 -m consciousness cloud --purpose plan --request-id plan-demo-1 --input examples/cloud-plan.json --data-dir local-data/cloud-demo

# 云端生成计划，再经技能和原串行运行时执行模拟表达。
python3 -m consciousness simulate examples/cloud-scenario.jsonl --data-dir local-data/cloud-scenario
```

`.env` 已加入 Git 忽略；CLI 只读取环境变量，不自动解析或执行 `.env`。Base URL 不含 `/chat/completions` 尾部；若供应商要求 `/v1` 或 `/compatible-mode/v1`，将其包含在 Base URL 内。云接口会收到本次选择的证据、任务输入和显式媒体附件。程序不会自动遍历或上传工作目录。

| 配置 | 含义 |
| --- | --- |
| BOT_MODEL_BASE_URL / BOT_MODEL_NAME / BOT_MODEL_API_KEY | 必填：兼容服务地址、默认模型、密钥 |
| BOT_MODEL_PLAN / BOT_MODEL_PERCEPTION 等 | 按任务覆盖模型名，沿用同一服务地址与密钥；任务名见下表 |
| BOT_MODEL_TIMEOUT | 请求超时，默认 30 秒，上限 120 秒 |
| BOT_MODEL_MAX_TOKENS | 单次生成 token 上限，默认 2,048，上限 8,192 |
| BOT_MODEL_MAX_CALLS | 每个持久数据目录累计尝试预算，默认 20，上限 1,000；重启不清零 |
| BOT_MODEL_MAX_CONCURRENCY | 每个 CloudClient 的并发上限，默认 2，上限 8 |
| BOT_MODEL_JSON_MODE | 默认 true，发送 json_object；不支持该参数的服务可设 false，仍做本地严格校验 |
| BOT_MODEL_STREAM | 默认 false；仅支持流式输出的模型设 true，完整收集后才校验/采用 |
| BOT_MODEL_MODALITIES | 默认 text；仅在模型支持时启用 text,image,audio |
| BOT_MODEL_AUDIO_DATA_URL | 默认 true，音频使用 Data URI；要求纯 Base64 的兼容服务设 false |
| BOT_MODEL_EXTRA_BODY | 默认 `{}`，仅允许 reasoning_effort、thinking、modalities、temperature；不能覆盖消息、模型、token 预算或 tools |

额外 `modalities` 只能设为 `["text"]`，本版不生成语音。需要显式关闭推理或指定文本输出的服务，可按其官方文档填写额外参数。参数支持情况取决于具体模型；不支持时返回云端错误，不自动删除参数继续尝试。

这不是货币硬预算：token 上限和调用次数是客户端限制，费用仍取决于供应商计价。超时/取消可能发生在云端已开始生成之后，因此不承诺撤销计费。程序不自动重试 429、5xx、超时、坏格式或待确认请求。

## 复用已有凭据与配置检查

参考 [OpenClaw 的环境变量与 SecretRef 接入](https://github.com/openclaw/openclaw/blob/main/docs/gateway/authentication.md)，新增显式环境变量引用，避免重复复制已有供应商密钥。若部署环境已经注入相应密钥，可配置：

```sh
unset BOT_MODEL_API_KEY
export BOT_MODEL_API_KEY_ENV=OPENAI_API_KEY
# 同时显式设置与该密钥匹配的 BOT_MODEL_BASE_URL、BOT_MODEL_NAME。
python3 -m consciousness cloud-status
```

`BOT_MODEL_API_KEY_ENV` 仅保存变量名，值由进程环境解析；也可引用其他供应商变量。不自动猜测密钥、不从浏览器或其他工具的登录文件导入、不自动回退。直接密钥与引用同时设置时拒绝启动。`cloud-status` 仅校验配置，成功不代表网络、余额或模型可用；真实请求仍使用上文 `cloud` 命令。

本机本轮检查：Codex CLI 为 ChatGPT 登录，项目与常见供应商 API Key 环境变量均未配置，因此没有执行真实云端调用。ChatGPT 登录与 API Key 是不同认证方式，通用 API 调用需要适用的凭据，见 [官方认证说明](https://learn.chatgpt.com/docs/auth)。本实现没有 OAuth 登录或刷新令牌适配，不把 Codex 会话令牌当作通用 API Key。

## 13 类提示词与模块映射

提示词在 `consciousness/prompts/`，系统约束和任务模板分开，版本为 1.0.0。运行时把完整 JSON Schema 附在模板后，记录完整提示词 SHA256；模板修改需要更新版本并重跑固定测试集。

| purpose | 主要模块 | 输出职责 |
| --- | --- | --- |
| perception | M02 | 文本、图像、音频、视频采样帧的观察候选与缺失信息 |
| summarize | M04 | 保留约束、证据、未知状态和争议的工作记忆摘要 |
| memory_extract | M05 | 带来源及 observed/reported/hypothesis 分类的结构主张 |
| scene | M06 | 场景地点、轨迹候选及实体关联不确定性 |
| goal | M07 | 可观察完成条件与目标描述 |
| appraisal | M10 | success/failure/emergency/calm 刺激候选，本地仍控制衰减和饱和 |
| expression | M10/M12/M14 | 有依据的表达文本，不声称未完成的效果 |
| motivation | M11 | 最多 5 项有证据的目标候选 |
| role | M12 | 有范围和反例的表达偏好假设，不自动发布身份 |
| plan | M13/M16 | 绑定注册目标的 1–8 步受限计划、前置条件和分支 |
| learning | M15/M16/M17 | 依据注册经验提出技能候选、适用范围及反例 |
| conflict | M18 | 事实冲突/时间变化/单位差异/不确定分类及复核问题 |
| association | M19 | 最多 5 个可验证问题，和事实隔离 |

M01/M08 的事务与调度、M09 的传感器有效性、M03/M17 的确定性紧急规则、M20 的测量保持本地确定逻辑，避免等待模型才停止动作。这里的“学习”改为云端提示词生成候选并由本机验证/发布，不引入本地神经网络训练。

## 输出和采用边界

成功响应：`{"status":"ok","data":{...},"questions":[]}`。缺失关键条件：`{"status":"needs_clarification","data":null,"questions":[...]}`。

每个 purpose 有独立字段类型、枚举、长度和条数限制；未知字段、未知证据、非法能力、循环步骤、超出目标剩余预算或改变目标 ID 都会拒绝。JSON 模式不是信任边界：重复 JSON 键、非有限数字、空内容、截断、拒绝、工具调用和未结束流式响应也拒绝。模型内部推理内容不保存、不用于执行。

CloudCognition.infer 是全部模型任务的统一入口，CognitiveSystem.infer 和场景 `cloud` 操作调用它。ModelGateway 未注入测试替身时默认使用云端规划提供器。接受的结果保存在 cloud_requests 的候选记录，不会自动写成正式事实、角色或已发布技能。知识写入和候选发布继续走各模块既有证据/配置流程。

`cloud` CLI 只生成候选；显式配置的 `run_cloud_plan` 场景操作可以采用尚未过期的 plan，再经 Skills / Runtime / SimulatedDriver 执行。它不会绕过单动作槽位、停止确认、动作预算或目标完成证据。其余 purpose 的结果也可由应用层读取并提交给对应模块审核。

每次调用绑定 request_id、purpose、模型、提示词哈希、输入、证据、模态数据摘要、生成参数、上下文修订和 generation。同一有效请求可以复用已接受结果，不再次付费调用；换内容必须换 request_id。知识、目标、遥测、场景、身份或学习状态变化会使迟到结果失效；同一 scope/purpose 的新代次使旧代次不能采用。

失败、取消、pending 恢复状态不会自动重发。需要显式的新 request_id 才能发起新尝试，仍扣持久预算。Token usage 使用供应商返回的整数值；缺失时不伪造用量。审计不记录 API Key、认证头、上游错误正文或媒体 Base64。

## 图像、音频和视频

输入 JSON 可以加入 attachments：

```json
{
  "input": {"instruction": "描述两个采样时刻的可见变化，保留不确定性。"},
  "attachments": [
    {"id": "frame-1", "path": "local-data/frame1.png", "type": "image", "at": 0},
    {"id": "frame-2", "path": "local-data/frame2.png", "type": "image", "at": 1}
  ]
}
```

使用 `--purpose perception` 调用，先配置支持对应输入的云模型。图片支持 PNG/JPEG/WebP；音频支持 WAV/MP3，type 为 audio；一次最多 8 项，原始数据总量最多 8 MiB，整个 HTTP 请求最多 12 MiB。附件路径相对运行目录，不写入云端上下文；发送内容和 SHA256 标识绑定同一 evidence ID。

视频按提供的带时间图像帧理解，不自动解码任意视频文件，不把未采样时间段当作已经观察。当前输出都是文本 JSON；语音合成与真实扬声器输出不在本次接口范围内。

## 离线回归与真实联调

`examples/cognitive.jsonl` 中旧的预设模型事件已明确改名为 fixture_model，它只验证静态样本，不调用模型网关。`model` / `cloud` 事件一律走云端；没有配置就报错，绝不降级到 fixture。原离线场景内容哈希发生变化，复跑应使用新的数据目录。

本机 HTTP 测试验证认证、请求结构、所有 purpose、流式拼接、上下文过期、超时取消、预算恢复和真实调度链路；CI 不发送收费请求。它不能证明真实云模型的理解质量。部署验收还需使用选定模型运行固定提示词样本，记录模型版本、语义正确率、澄清率、错误证据率、延迟和实际 token 用量。

协议实现核对了 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/) 和 [阿里云多模态兼容接口](https://www.alibabacloud.com/help/en/model-studio/qwen-omni)。这些是协议参考，并不表示所有供应商或模型均已实测。
