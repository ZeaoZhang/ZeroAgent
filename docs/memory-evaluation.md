# ZeroAgent 记忆评测流程

这个评测器按历史会话逐次更新一份隔离的 ZeroAgent 文件记忆，然后在没有原始会话的新 Agent 中提问。它保存初始化记忆、每轮记忆快照、答题前快照和逐轮工具输入输出，用于定位事实在哪个阶段缺失。词面诊断提供线索，具体原因仍需结合原始工具结果和回答核对。

内置 `orion-tools-v2` 包含 9 个 OrionEval 合成样例，每层 3 个。需要记忆的项目事实不在初始化 SOP 中；历史包含合成的成功工具调用记录，作为基准规定的事实来源，并非当前工作区真实执行记录。完整对照基准沿用参考项目的 60 个用例，每层 20 个；只有完整覆盖并使用 Rubric 裁判的运行才会记为完整基准状态。

新版用例和 `source-aware-v2` 裁判修正了旧版输入与记忆策略的冲突、初始化规则被误判为幻觉等问题。旧版报告保持原样，旧版 2/9 通过率不能直接与新版比较。

## 运行

用例列表和输入校验可以离线执行，不调用模型：

```bash
python -m zero_agent.evaluation.memory --list
python -m zero_agent.evaluation.memory --validate-only
```

Rubric 模式要求裁判与被测模型不同。评测器会优先选择配置中的另一个后端；如果配置只有一个模型，需通过 `--judge-model` 指定不同模型 ID，也可以用 `--judge-backend` 指定另一个后端。单例运行示例：

```bash
python -m zero_agent.evaluation.memory \
  --case layer1_01_project_artifact_limit \
  --judge-model "model-id-different-from-agent"
```

可以按层运行，或给评测指定 ZeroAgent YAML 配置：

```bash
python -m zero_agent.evaluation.memory --layer layer2
python -m zero_agent.evaluation.memory --config config.yaml --layer layer3
```

`--metric keyword-recall` 会运行 Agent 的记忆读写流程并计算离线词面指标，不会额外调用裁判模型：

```bash
python -m zero_agent.evaluation.memory \
  --case layer1_01_project_artifact_limit \
  --metric keyword-recall
```

可加载参考项目的 YAML 测试集和 gold facts，先离线检查它们是否符合 ZeroAgent 的记忆策略：

```bash
python -m zero_agent.evaluation.memory \
  --dataset /path/to/ai-agent-book/chapter3/user-memory-evaluation/test_cases \
  --gold /path/to/ai-agent-book/chapter3/user-memory-evaluation/fixtures/gold_facts.json \
  --validate-only
```

评测集字段沿用 `test_id`、`category`、`conversation_histories`、`user_question`、`evaluation_criteria`、`expected_behavior`。`--dataset` 接受单个 JSON/YAML 文件或递归扫描目录；gold facts 使用参考项目的 `{test_id: {"required_facts": [...]}}` 结构。

原版纯文本会话可能不满足 ZeroAgent 的成功工具验证要求。校验报错时，需要补充可信的历史工具来源，或改用符合产品策略的用例；不能把模型的“我检查过”作为成功执行证据。完成适配后，可移除 `--validate-only` 并加上 `--full-reference-suite` 运行完整集。正常运行也会在调用模型前自动校验输入；离线校验检查整个输入集，并默认使用中文初始化记忆，正常运行则使用实际配置语言。

## 隔离与运行步骤

每个测试用例有自己的临时 `memory_dir`。对每段历史会话，评测器都会：

1. 新建 `ZeroAgent`、LLM session 和空工作区。
2. 只输入当前历史会话，并要求 Agent 按 `memory/sops/memory_management_sop.md` 执行长期记忆结算。
3. 保存本轮记忆文件哈希、记忆快照和逐轮 trace；检查本轮是否写入禁止事实。
4. 丢弃 Agent、LLM history 和工作区；下一段只复用这个测试用例的 `memory_dir`。
5. 保存答题前记忆，随后新建 Agent 和工作区提问。答题阶段不会收到历史会话、预期答案或评分标准；要求只检索和回答，检测到长期记忆文件发生变化时该次评测无效。
6. 使用独立裁判对照历史会话和初始化记忆评价回答。初始化 SOP 的规则也是权威来源；生成记忆只证明当前文件状态，不能自行证明其中项目事实真实。工具结果可证明实际读取内容和范围。

这样下一轮记忆更新只可能从现有记忆和当轮会话得到信息，最终问题也只可能从文件记忆取回信息。测试用例是合成的；输出 receipts 会保留评测 prompt 和回答，以便复盘，请勿把真实密钥或未脱敏的私人对话放进评测集。

评测遵守 ZeroAgent 现有记忆策略：长期保存必须由成功的操作或工具结果验证；忽略临时状态、猜测、密钥和无复用价值的对话信息。它不是个人画像记忆能力的全量 benchmark。若要评测用户偏好记忆，需要先在产品策略中明确定义哪些用户信息允许保存、如何处理撤回/更新，再扩充用例。

## 指标和通过规则

Rubric 裁判给出四个 1–4 分维度：事实精确性、事实完整性、跨会话推理、适时主动帮助。每项包含判断理由、证据摘录和边界案例。另有幻觉检测一票否决：重要事实与适用的权威来源冲突或完全无依据时触发 veto，最终 reward 归零。遗漏历史事实、准确报告当前记忆缺失、引用初始化规则，不应仅因此触发幻觉否决；召回不足仍会降低完整性得分。

一个案例通过要求：输入有效、所有历史维护与答题阶段都完成、答题未改变长期记忆、每轮都未写入禁止事实，且幻觉未触发、事实精确性/完整性/推理三项都至少为 3/4。主动帮助分单独报告，因为有些问题只需简洁直接回答。Reward 是四维分数映射到 `[0, 1]` 后的均值；触发 veto 时为 `0`。

无裁判的词面指标只做诊断：分别统计预期事实在答题前记忆、回答中的词面召回，以及被错误写入记忆的禁止事实。它无法识别同义改写或完整语义，因此不能单独宣称 Rubric 通过。

阶段诊断记录初始化已含的事实、答题前未保存的事实、答题读取中观察到的事实和回答缺失的事实。读取观察来自自动注入的 L1 内容及工具输出，不包含模型自行声称读到的内容。没有词面匹配不能直接证明检索失败；执行输出也不一定来自文件读取。

## 证据产物

默认写入被 Git 忽略的 `workspace/memory_eval/validation/`：

- `runs/<run-id>/evidence.json`：逐例、逐层分数，答案，记忆文件哈希，token 数和延迟。
- `runs/<run-id>/receipts.json`：输入哈希、每阶段 prompt/回答、终态和用量；不包含 API 凭据。
- `runs/<run-id>/artifacts/<test_id>/`：初始化、每段会话后、答题前/后与最终记忆树；`trace_session_*.json` 和 `trace_question.json` 保存逐轮模型输入、工具调用、工具返回和流式事件。
- `runs/<run-id>/cases/<test_id>.json`：每例结束立即保存结果与收据，便于后续运行中断时复盘已完成案例。
- `runs/<run-id>/manifest.json`：模型标识、裁判版本、数据输入哈希，以及报告/收据/快照/trace 的文件哈希。
- `runs/<run-id>/report.md`：可读汇总。
- `latest.json`：最近一次运行的规范摘要和 manifest 哈希。

本地 9 例、单层/单例、关键词指标或缺少独立裁判的运行都标记为 `partial`。只有明确传入 `--full-reference-suite`，并且数据实际包含三层各 20 例的完整 60 例 Rubric campaign，状态才会是 `passed` 或 `failed`。该参数应只用于参考项目的完整 3-1 数据集。

## 扩展用例

添加用例时为每个关键答案事实给出 `required_answer_facts`，为必须写入记忆的事实给出 `required_memory_facts`，为不得记忆的值给出 `forbidden_memory_facts`。每个事实可用字符串，或用 `{"label": "...", "any_of": ["可接受变体", "...approx"]}` 表示同义形式。

用例默认为 `evaluation_kind: retention`，校验器拒绝“答案已在初始化记忆中”或“需要保存但没有成功历史工具结果”的输入。仅检查已有架构规则时可显式设置 `evaluation_kind: architecture`，避免混入新事实记忆能力的结论。每段历史可设置 `memory_action: retain|update|discard`，并为需要保存的历史设置 `requires_tool_evidence: true`；assistant 的工具调用 ID 必须与后续 tool 消息的 `tool_call_id` 唯一关联，结果必须明确成功。

时间更新用例应在评分标准中判断新旧值是否被正确标为当前/失效。已经失效且标注清楚的旧值允许保留，不能把它放入 `forbidden_memory_facts`，否则会把合理历史记录误判为错误。

原始 transcript 只在对应历史的维护阶段和裁判阶段提供；预期答案和评分 rubric 只提供给裁判。答题阶段不注入它们。

## 参考

- [User Memory Evaluation README](https://github.com/bojieli/ai-agent-book/blob/main/chapter3/user-memory-evaluation/README.md)
- [Experiment ledger](https://github.com/bojieli/ai-agent-book/blob/main/chapter3/EXPERIMENT_LEDGER.md)
- [Chapter 7: User Memory](https://github.com/bojieli/ai-agent-book/blob/main/book/chapter7.md)
