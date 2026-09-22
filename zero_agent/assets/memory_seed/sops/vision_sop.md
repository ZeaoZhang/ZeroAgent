# Vision Capability SOP

## 前置规则

1. 先枚举窗口并确认目标窗口存在且已激活。
2. 禁止全屏截图；优先截取目标窗口或局部区域。
3. 能用窗口标题或本地 OCR 获取信息时，不调用视觉模型。

## 调用方式

通过内置 `vision` 工具调用已经配置的视觉 backend；不要导入独立的 vision API，也不要设置 `ZA_VISION_*` 环境变量。

可用 backend 由 `config.yaml` 中 `vision: true` 的配置决定。调用时省略 `backend`，先使用当前会话正在运行的模型；只有当前模型不支持视觉时，才回退到配置中排序后的第一个视觉 backend。当前 `shuai-deepseek-flash` 不支持视觉，图片请求可回退到 `shuai-gpt-5.6-luna` 或 `shuai-grok-4.6`。

工具参数：

- `image_path`：图片文件路径，必填。
- `prompt`：图片理解问题，可选。
- `backend`：可选的视觉 backend 名称；通常省略，让工具优先沿用当前会话模型。

视觉请求复用所选 backend 的 API key、API base、模型、重试、超时、代理、思考和输出参数。
