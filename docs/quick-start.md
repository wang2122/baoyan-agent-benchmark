# 运行与评分指南

以下命令在下载或解压后的仓库根目录执行。先跑一道题确认客户端可用，再运行当前70题，最后独立评分并查看报告。

### 1. 准备环境与账号

- Python 3.10+；使用 Kimi 订阅配置时需要 Python 3.11+。框架只使用 Python 标准库，无需 `pip install`。
- 原生 CLI 自动执行目前需要 macOS 的 Seatbelt 沙箱。Linux/Windows 的 App 手工收卷见[复现指南](reproduction.md)。
- 按[Agent选择表](../README.zh-CN.md#快速开始)选择当前注册的配置，安装对应客户端并准备订阅登录或API Key。模型访问权限由自己的账号决定。以下流程适用于表内全部配置，把`AGENT`换成目标ID即可。

```sh
AGENT=gpt-6-1-sol
python3 -m benchmark setup --agent "$AGENT"
python3 -m benchmark cli-check --agent "$AGENT"
```

`setup` 固定本机客户端路径、版本、模型和认证方式，保存到 `runs/config/agents.local.json`。订阅与 API 是不同参评配置，不自动切换。`cli-check` 返回 `ready_for_trial` 才进入下一步；它检查环境，不证明账号额度、模型权限或端到端调用成功。不要提交本机配置、凭据或登录文件。

### 2. 实际运行一道题

下面默认只预览，不调用模型、不创建运行目录。添加`--execute`后才使用所选账号额度。

```sh
python3 -m benchmark cli-batch --suite dev \
  --agent "$AGENT" --task N02-01 \
  --out ./runs/one-task
```

确认预览后，以相同参数添加`--execute`实际执行。

终端与 `status` 可查看是否完成：

```sh
python3 -m benchmark status --batch-dir ./runs/one-task
```

这道题的答卷位于 `runs/one-task/<Agent ID>/N02-01/r1/workspace/answer.md`，同目录保存运行记录。单题试跑用于检查执行，不是全量成绩；下一步使用新目录重新答题。

### 3. 运行当前70题

`--selection` 校验选题清单、题面和 rubric 的指纹，将完整清单写入批次计划，并以所选题目作为报告分母。自动包含清单中的两阶段题，无需逐题列出 `--task`。

```sh
# 先预览：每个配置、每次重复为70题、83阶段
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent "$AGENT" --out ./runs/current

# 确认预览后实际执行
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent "$AGENT" --out ./runs/current --execute
```

所有参评配置使用相同选题清单。增加其他配置时，先完成对应 `setup`，再重复 `--agent <配置ID>`；总调用量随配置数和 registry 中的重复次数增加。`--selection` 不能与 `--task`、衍生连续任务参数混用。每次运行选择新的输出目录。

例如同时评测GPT Sol与Claude Opus，先配置并检查两项，再预览同一70题清单：

```sh
python3 -m benchmark setup --agent gpt-6-1-sol --agent claude-opus-5-5
python3 -m benchmark cli-check --agent gpt-6-1-sol --agent claude-opus-5-5
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent gpt-6-1-sol --agent claude-opus-5-5 --out ./runs/two-models
```

被测 Agent 只接收当前题面。两阶段题在同一原生会话中完成 S0 后才释放 S1，不同题使用隔离会话；rubric 留在评审端。

### 4. 查看进度与恢复

```sh
python3 -m benchmark status --batch-dir ./runs/current

# 中断后使用原来的参数和目录恢复
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent "$AGENT" --out ./runs/current --resume --execute
```

`--resume` 继续未尝试任务及成功的阶段边界，保留已完成答卷；失败、超时或结果不确定的阶段不自动重跑。改变题集、模型配置、执行代码或预算时，使用新目录。历史批次要配合当时冻结的运行代码复核。

| 产物 | 位置，相对于 `runs/current/` |
| --- | --- |
| 固定选题、执行计划与预期范围 | `full-batch.json` |
| 固定客户端配置 | `agents.frozen.json` |
| 已完成调度步骤的状态快照 | `full-status.json`；实时状态使用上述 `status` 命令 |
| 单阶段原始答卷 | `single/<配置ID>/<题号>/r1/workspace/answer.md` |
| 两阶段记录、各阶段答卷和工具事件 | `staged/<配置ID>/<题号>/r1/` |

### 5. 独立评审：Codex订阅或可选API

答题与评审使用分开的会话及目录。先停止答题批次；开始评审后不要继续写入原答卷。评审只读取本题材料，不执行答卷指令，不把rubric传回答题会话。

**Codex订阅评审。** 在使用ChatGPT订阅登录的Codex中另开评审会话，选择有权限的评审模型，提交单份评审材料及其要求。它使用订阅额度，不需要配置Judge API；记录实际评审模型、推理设置和评审者，订阅不足时不自动转API。

当前70题批次的57道单阶段题可先导出匿名评审包（不调用模型）：

```sh
python3 -m benchmark review-batch --batch-dir ./runs/current/single \
  --out ./runs/current-reviews-single
```

只把`current-reviews-single/packets/<随机编号>/`内的材料提供给每个独立评审会话，组织者保管顶层来源映射。按[评审流程](review-protocol.md)填好`judgment.json`和`review-settings.json`后，校验并计分：

```sh
python3 -m benchmark review-apply --batch-dir ./runs/current/single \
  --reviews ./runs/current-reviews-single --out ./runs/current-scored-single
python3 -m benchmark results-verify --batch-dir ./runs/current-scored-single
```

13道两阶段题分别使用`python3 -m benchmark.longitudinal review/score`导出材料与计分，两轮合评一次；具体命令见[原生CLI指南](native-cli.md#codex订阅独立评审)。当前没有自动Codex订阅Judge入口，也没有将这些手动结果直接合并为完整70题HTML报告的入口；不要把单阶段结果当作70题成绩。以下API路径支持完整批次自动评审与报告。

**可选API自动评审。** `config/judge.example.json`是配置模板，`https://api.example.com/v1/chat/completions`是占位地址，不代表已接通或发生API收费。选择这条路径时，复制模板并填写实际`provider`、`model`、`expected_response_model`、`endpoint`及兼容的`parameters`；API Key通过`BENCHMARK_JUDGE_API_KEY`环境变量提供，不写进配置。

`expected_response_model`须匹配服务实际返回的模型标识；不能保留模板占位值。认证失败或模型不符时停止，不自动换模型或认证渠道。

```sh
cp config/judge.example.json config/judge.local.json
# 用编辑器填写实际服务配置；在终端安全设置 BENCHMARK_JUDGE_API_KEY

# 仅生成评审请求，不调用API
python3 -m benchmark judge-batch --batch-dir ./runs/current \
  --config config/judge.local.json --out ./runs/current-judged

# 明确选择实际API评审后才执行；按所配置服务的规则计费
python3 -m benchmark judge-batch --batch-dir ./runs/current \
  --config config/judge.local.json --out ./runs/current-judged \
  --resume --execute
```

Judge逐题输出判断，程序校验答卷引句并计算分数；两阶段合评一次。API Judge没有外部浏览能力，证据不足保留待核；生成模板不等于完成评审。人工评审、争议与复核见[评审流程](review-protocol.md)。

### 6. 查看与复核报告

上述API评审执行后自动生成 `runs/current-judged/leaderboard/index.html` 和 `leaderboard.json`。浏览器打开 HTML；JSON 包含逐题状态、得分、完成率及汇总。也可以从已保存的评审重新校验、复算并导出到新目录，不再调用模型：

```sh
python3 -m benchmark leaderboard-export \
  --batch-dir ./runs/current-judged \
  --out ./runs/current-report
```

打开 `runs/current-report/index.html`。**报告分母固定为70题；全部预期重复都有有效评分才显示完整均分。** 缺答、失败、缺评审或待核均单列，不能只平均成功项。报告不完整时，导出仍保留 HTML/JSON，退出码为1；校验错误为2。它是本地实验报告，不自动成为正式榜单。

报告范围以选题清单为准。单题试跑不作为当前70题实验成绩；其他执行入口见[原生CLI指南](native-cli.md)。
