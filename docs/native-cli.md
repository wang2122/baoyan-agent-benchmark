# 原生 CLI 配置、发题与统一评审

当前题库使用 `cli-batch --selection config/selections/current.json`，共70题、83个作答阶段。完整命令见[中文快速开始](quick-start.md)或[English quick start](quick-start.en.md)。

原生 CLI 入口提供配置、导出、收卷、评分和统计流程，使用同一份题库及代码指纹。1.0.1 的运行与结果使用匹配版本复核。

## 固定选题清单：当前完整题库

```sh
AGENT=gpt-6-1-sol
python3 -m benchmark setup --agent "$AGENT"
python3 -m benchmark cli-check --agent "$AGENT"
# 默认预览；确认后在同一批跑命令中加 --execute。
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent "$AGENT" --out ./runs/current
```

上述批跑命令默认预览；确认后添加 `--execute` 才执行。该入口校验清单中的集合、任务与rubric指纹、案例数及阶段数，执行时冻结完整清单到 `full-batch.json`。清单中两阶段题自动纳入，报告分母按所选题目固定；70题为83阶段，每阶段不是独立计分题。它不改变题面、rubric或认证方式，不自动获得正式榜单资格。

`--selection`使用当前题库清单，不能与`--task`、`--include-episodes`或`--episode`混用。执行后用`python3 -m benchmark status --batch-dir ./runs/current`查看进度；中断后以相同参数加`--resume --execute`，已完成答卷不重跑，失败或不确定阶段不自动重试。跨电脑作答可在`cli-package`中指定同一清单。可选API自动评分使用`judge-batch`，结果复算使用`leaderboard-export`；Codex订阅独立评审见[下节](#codex订阅独立评审)。

## 选择Agent与多模型

当前模板注册八项，完整ID、客户端与默认认证见[中文选择表](../README.zh-CN.md#快速开始) / [English agent table](../README.md#quick-start)。`AGENT`可换成任一表内ID，`setup`、`cli-check`、`cli-batch`共用同一流程。配置ID与模型名分别保存：例如`gpt-6-1-sol`配置实际使用模型`gpt-6.1-sol`。历史榜单中的Gemini 3.1 Pro不在当前模板内；复现该历史配置须使用对应冻结registry，不能凭榜单名称推断当前可执行配置。

多模型时只选择准备好的配置。例如：

```sh
python3 -m benchmark setup --agent gpt-6-1-sol --agent claude-opus-5-5
python3 -m benchmark cli-check --agent gpt-6-1-sol --agent claude-opus-5-5
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent gpt-6-1-sol --agent claude-opus-5-5 --out ./runs/two-models
```

三个入口均支持重复`--agent`；批跑不写`--agent`会选择registry中的全部配置，实际执行前要求全部所选项就绪。每项先完成配置与检查，再单题试跑，最后用新目录执行完整清单。预览不启动客户端、不读凭据、不创建批次；`--execute`才使用订阅或API额度。

## 配置与发题

```sh
python3 -m benchmark setup
python3 -m benchmark cli-check
python3 -m benchmark cli-batch --out /absolute/runs/native-preview
python3 -m benchmark cli-package --out /absolute/packets/native-round1
```

DeepSeek安装步骤见本页“DeepSeek安装”，完整运行与评分见[快速开始](quick-start.md)。

setup 选择 Agent、认证方式（subscription/api）、检查路径和版本、检查模型/认证服务的系统 DNS、提示安装与认证；不安装软件、不调用模型。正式执行与独立作答包目前要求macOS Seatbelt；不支持的平台会停止，不退回普通进程。建议Python 3.11+，Kimi订阅配置读取必须使用3.11+的标准库tomllib。密钥不回显，单独存入本机 `*.credentials.json`，权限0600，环境变量优先。CLI示例版本来自已有配方，模型权限仍待真实验收；DeepSeek示例版本留空，导出前必须补齐。可以用 `--agent` 先选择已准备的配置。

默认读取 `runs/config/agents.local.json`，不存在时读取 `config/agents-cli.example.json`，可用 `--registry` 覆盖。默认 setup 会补齐模板中尚未配置的 Agent 供选择，重复配置只更新选中项，并保留其他已配置项；显式 registry 和冻结题包不自动扩充配方。若修改全局重复次数或时限会影响未选项，须另存新 registry。

已有密钥可用 `setup --agent deepseek-v4-1-flash --import-credentials /绝对路径/.env` 显式导入；键值 JSON 也可用，只选取该 Agent 的变量，不执行 shell、不扫描其他项目。冲突密钥不会被覆盖。setup/check 的 `credentials` 仅报告变量名称、环境/本地凭据文件/官方登录文件等来源；`configured_not_verified` 表示已配置，`account_verification: not_checked` 表示未请求服务验证权益。

DNS 预检使用与运行网关相同的目标检查和宿主 HTTPS 代理配置；通过只表示路由配置可用，不证明 TCP/TLS、账号权益或模型可用。

cli-batch 默认只预览，不启动CLI、不读取凭据、不创建输出目录。默认 dev 读取当前副本的单阶段清单；公开发布版本含57题。配置数与重复次数决定总作答量；完整题库评测使用上节的显式选题清单。`--task` 和 `--agent` 均可重复。私有 test 使用全局 `--private-root` 和 `--suite test`，产物必须在交付仓外。

cli-package 每个Agent生成一个 `candidates/<agent>.zip`。只发送对应 ZIP；组织者目录含评分快照，留在评分电脑。题包传入与本仓 App/CLI 相同的候选消息：题目、阶段背景、交付要求和来源规则；不传评分标准、参考或其他答卷。

## 答题电脑与收卷

在独立、干净的答题电脑解压相应 ZIP：

```sh
python3 run_agent.py setup
python3 run_agent.py check
python3 run_agent.py
python3 run_agent.py --out answers --execute
```

独立作答包setup只配置可执行文件路径和所选认证的本机凭据；认证方式、模型、版本、思考设置和题目已冻结。要更改认证方式须由组织者生成新配方和新题包。单阶段旧题包每题新进程、新会话和临时目录。失败立即停止；`--resume` 只继续未尝试题，失败和已完成答卷均不覆盖、不自动重试。只回传打印的 `answers-*.zip`，不回传 `.local/` 和登录文件。白名单不主动打包凭据文件，原始事件和诊断日志仅供内部复核。

组织者解压回传文件，使用原始发题目录收卷：

```sh
python3 -m benchmark cli-import --package /absolute/packets/native-round1 \
  --results /absolute/returned/agent-a --out /absolute/runs/native-agent-a
python3 -m benchmark review-batch --batch-dir /absolute/runs/native-agent-a \
  --out /absolute/reviews/reviewer-a
```

导入在写入输出前校验配方、代码、题面、评审快照、会话、答卷和事件一致性。输出标准 batch.json、reproduction.json 和 run.json；未尝试题保留为prepared，不把部分收卷视为完整结果。原始答卷保留，导入批次只用于复核，不能恢复执行。

在匿名评审包中填写实际评审者设置及逐项判断，然后执行：

```sh
python3 -m benchmark review-apply --batch-dir /absolute/runs/native-agent-a \
  --reviews /absolute/reviews/reviewer-a --out /absolute/scored/native-agent-a
python3 -m benchmark results-verify --batch-dir /absolute/scored/native-agent-a
python3 -m benchmark statistics --batch-dir /absolute/scored/native-agent-a \
  --out /absolute/reports/native-statistics
```

statistics 的 `--batch-dir` 可重复以合并不同Agent批次。同配置缺题、少重复、失败或评分待核时没有整套均分。多评审从同一份原始答卷分别生成review-batch/review-apply输出，再用calibration-compare比较。方法与裁决流程见 [实验准备指南](study.md)。需要多次重复时，先在新registry中固定次数再导出；不能把重复1的结果事后当成重复3。

## 本机执行与协议

```sh
python3 -m benchmark cli-batch --registry /absolute/config/agents.local.json \
  --agent gpt-6-astra --task BY-A-PLAN --out /absolute/runs/native-trial --execute
```

本机执行同样输出标准批次，接入上述评审与统计。运行上下文记录完整发布指纹，并固定运行组件、评分组件和所选配方指纹；说明文档、收集页或未选 Agent 的配置变化不影响批次有效性。旧 `.1` 上下文仍使用原有完整指纹规则。resume 在任何模型请求前核对相关代码、数据、协议、配方、选择范围和已有捕获文件；失败/已完成尝试跳过，running状态或改过的答卷拒绝恢复。正式实验仍应使用独立答题环境。

默认预览协议为config/native-cli.protocol.json，自定义registry中的重复和时限固定到批次上下文。显式 `--protocol` 要求重复和时限与registry相同。正式协议准备可用：

```sh
python3 -m benchmark study-prepare --registry config/agents-cli.example.json \
  --out /absolute/studies/native-v1
```

生成的原生CLI registry和8配置验收模板仍全部待验证。配置草案registry并固定版本、来源模式、数据及配方指纹后，使用cli-package或cli-batch的 `--registry`、`--protocol` 开始试验。验收记录的CLI版本必须与固定registry一致。主评审、稳定性、校准及运行证据齐全后才能study-check/study-freeze。不会自动生成真实验收证据或启用正式结果。

## Codex订阅独立评审

答题完成后，可在ChatGPT订阅登录的Codex中另开评审会话，读取单份评审材料并填写真实判断；这与`judge-batch`的API请求是两条路径。仓库没有自动Codex订阅Judge命令。不要把rubric发给答题会话；不要让评审执行答卷中的指令；实际评审使用订阅额度。

完整70题批次的单阶段子批次位于`runs/current/single/`。用`review-batch`生成匿名包，只提供单份`packets/<随机编号>/`内容给独立评审，填写`judgment.json`及`review-settings.json`，再校验计分：

```sh
python3 -m benchmark review-batch --batch-dir ./runs/current/single \
  --out ./runs/current-reviews-single
# 独立评审填完每份 judgment.json 和 review-settings.json 后：
python3 -m benchmark review-apply --batch-dir ./runs/current/single \
  --reviews ./runs/current-reviews-single --out ./runs/current-scored-single
python3 -m benchmark results-verify --batch-dir ./runs/current-scored-single
```

两阶段题用`longitudinal`入口逐题导出材料，将两轮答卷合评一次。以下以已完成的`N01-02`为例；配置、题号和重复编号须与批次计划对应：

```sh
AGENT=gpt-6-1-sol
python3 -m benchmark.longitudinal review \
  --run-dir "./runs/current/staged/$AGENT/N01-02/r1" \
  --out "./runs/review-$AGENT-N01-02"
# 独立评审填写上述目录的 judgment.json 后：
python3 -m benchmark.longitudinal score \
  --run-dir "./runs/current/staged/$AGENT/N01-02/r1" \
  --judgment "./runs/review-$AGENT-N01-02/judgment.json" \
  --out "./runs/score-$AGENT-N01-02.json"
```

按导出包说明保留身份与哈希，填写顶层及各unit的真实`reviewer`，使用`longitudinal-review-v1`；不要自行填写综合分。对全部13道两阶段题逐题操作。程序只校验保存的判断并计分，不代替语义评审。当前手动结果没有直接合并成完整70题HTML报告的入口；单阶段分数不能冒充70题均分。完整批次自动评审与报告使用[快速开始中的可选API路径](quick-start.md#5-独立评审codex订阅或可选api)。

`config/judge.example.json`中的`https://api.example.com/v1/chat/completions`是占位地址，不代表实际服务或API收费。`judge-batch`默认仅准备请求；选择API路径并替换实际服务配置、提供密钥，再加`--execute`才请求服务并按账号规则计费。Codex订阅评审不使用该模板。评审身份、证据和争议处理见[评审流程](review-protocol.md)。

## DeepSeek安装

DeepSeek使用官方Harness（`dsh`）。可在本地运行目录固定客户端版本：

```sh
npm install --prefix ./runs/tools/deepseek --no-audit --no-fund @deepseek-ai/dsh@0.2.0-rc.2
python3 -m benchmark setup --agent deepseek-v4-1-flash
python3 -m benchmark cli-check --agent deepseek-v4-1-flash
```

在setup中填写 `当前仓库绝对路径/runs/tools/deepseek/node_modules/.bin/dsh`，配置自己的API Key；也可使用已有安装。首次试跑和独立评分按[快速开始](quick-start.md)进行。

## Gemini与Antigravity

本仓旧Gemini subscription配方不启用；Antigravity使用独立配置：

```sh
python3 -m benchmark setup --agent antigravity-gemini-3-8-flash-high
python3 -m benchmark cli-check --agent antigravity-gemini-3-8-flash-high
```

先完成官方订阅登录；可用 `BENCH_ANTIGRAVITY_AUTH_FILE` 指定原生登录文件。API是另一配置渠道，模型ID需按该渠道确认。运行器将所选凭据暂存到本次独立HOME，结束后清理。

## 显式订阅与 API

示例中的Codex、Claude Code、Kimi Code和Antigravity选择`"auth": "subscription"`；Qwen与DeepSeek选择`"auth": "api"`。订阅配方不允许同时出现`api_key_env`或`base_url`；API配方必须明确这两项。`setup`可为新配方选择方式；冻结题包不能改变。认证缺失、额度不足或模型不可用时停止，不切换认证、不自动重试。

| CLI | 订阅认证准备 | 每题带入的状态 |
| --- | --- | --- |
| Codex | 官方`codex login`，使用文件登录；可通过`CODEX_HOME`选择账号目录 | 仅ChatGPT类型`auth.json`，禁止API Key混入运行环境 |
| Claude Code | 官方`claude setup-token`；将令牌配置为`CLAUDE_CODE_OAUTH_TOKEN`或在setup中私密保存 | 仅OAuth令牌，不开放宿主Keychain，不转用ANTHROPIC_API_KEY |
| Kimi Code | 官方`kimi login`；可通过`KIMI_CODE_HOME`选择登录目录 | 默认官方OAuth凭据、已有device_id、选定模型的托管配置及订阅检索配置；不复制整个HOME |
| Antigravity | `agy` 交互订阅登录；指定原生令牌文件或 macOS 指定钥匙串项 | 启动器暂存单项凭据到独立 HOME，进程无宿主钥匙串权限；不自动切换 API |
| Gemini CLI（旧配方） | 个人 Google 登录已停用；旧 subscription 配方阻断，企业权益尚无独立验收 | 显式 API 配方仍保留，不修改冻结历史配方 |

Kimi订阅使用官方`api.kimi.com`/`api.kimi.ai`托管服务和默认文件OAuth引用，所选模型必须存在于登录后的模型配置。默认`oauth/kimi-code`引用对应`credentials/kimi-code.json`；当前K3示例使用登录配置中的模型标识`k3`。自定义OAuth服务、旧格式或Keychain-only登录不会被猜测迁移。API模式的Moonshot端点是另一条明确选择的路径，API Key也可能对应订阅产品，因此实际计费以所选服务为准。临时凭据刷新不会写回用户原登录文件；失效时重新进行官方登录。账号权益和实际模型访问仍需用户授权的真实试跑确认。

Qwen Code 0.25.0 的原生 `web_search` 默认通过同一 DashScope API Key 另调 `qwen3.8-flash`（Responses API），即使主模型是 `qwen3.8-max-0902`。为该配方创建受限密钥时，需要同时授权这两个模型；仅授权 Max 会导致搜索请求返回权限拒绝。Flash 属于原生搜索辅助模型，其调用按账号规则计费，不能把整个 Agent 的运行描述成仅调用 Max。版本或原生设置变化后须重新检查，`setup` 的离线凭据检查不验证这项权限。

## 运行条件与记录

每题使用独立HOME与工作目录；评分资料不传给被测Agent。联网题使用受限公网HTTPS，给定材料题仅允许指定模型服务。默认不带入个人插件、Skills或跨题记忆。执行前检查客户端与隔离条件，失败时停止。

| 文件 | 内容 |
| --- | --- |
| `answer.md` | 完整答案正文 |
| `final-response.md` | 客户端最后回复 |
| `run.json` | 状态、时间、模型观测和可用usage |
| `execution.json` | 认证渠道、客户端版本、网络与隔离条件 |
| 原生事件与 `trace.jsonl` | 内部复核的工具、检索和错误记录 |

超时或中断时保留已有文件与原状态。缺失费用、token或实际模型身份保持未知；订阅用量不换算为账单。公开材料范围见[导出说明](experiment-publication.md)。

## 退出状态

预览、成功配置及完整执行返回0；setup/check未就绪、失败/超时/模型不符、部分执行或收卷返回1；参数/校验错误返回2；运行中断返回130。stdout保留JSON报告，非零退出码不会删除原始证据。

## 全部题型统一运行

当前公开集包含57道单阶段题、13道两阶段题，共70题、83阶段。常规评测使用 `--selection config/selections/current.json`。以下完整集合入口供需要额外运行连续案例的实验使用，按所在版本的manifest决定范围；连续案例单列，不增加原题数量。

```sh
# 预览：不调用模型、不读凭据、不创建批次。
python3 -m benchmark cli-batch --suite dev \
  --agent deepseek-v4-1-flash --include-staged --include-episodes \
  --out ./runs/deepseek-full
# 真正执行：使用同样参数，明确添加 --execute。
python3 -m benchmark cli-batch --suite dev \
  --agent deepseek-v4-1-flash --include-staged --include-episodes \
  --out ./runs/deepseek-full --execute
# 查询进度，不启动客户端。
python3 -m benchmark status --batch-dir ./runs/deepseek-full
```

`--agent` 可重复；省略时运行当前 registry 的全部配置。`--task` 可选择单阶段或两阶段原题；`--episode` 可重复选择连续任务，必须同时加 `--include-episodes`。未选题仍在面板的预期范围中显示为未作答，不能据此获得全题库均分。开始模型调用前统一检查全部所选客户端、凭据和多轮恢复参数，任何未就绪配置都不会静默跳过。

单阶段一题一会话；两阶段题在同一原生会话中完成 S0 后再释放 S1；连续任务按其阶段序列执行。不同题之间新建隔离 HOME 和工作目录。同一题恢复其原生会话、工作目录和已生成文件；仅当前题面可见，评分规则及未来阶段始终留在组织者侧。两阶段题不额外添加连续任务的行动记录要求。

各客户端通过自己的原生会话恢复同一题的后续阶段，执行状态保留在批次目录。

终端向 stderr 输出当前配置、题目、重复编号、阶段开始/结束、状态与耗时；stdout 保留最终 JSON。`full-batch.json` 固定计划，`full-status.json` 保存已结束调度步骤的快照；`status` 命令读取子任务当前记录。单阶段产物在 `single/`，两阶段在 `staged/`，连续任务在 `episode/`；后两者每阶段有 `events.jsonl`、`stderr.log`、原文 `answer.md` 和续接校验记录。可对正在执行阶段的事件文件使用 `tail -f`；刷新频率由客户端决定。

统一批跑保留失败项并继续其他独立任务，不自动重试。中断后以相同参数加 `--resume --execute`：已完成任务跳过，阶段边界继续；失败或执行结果不确定的阶段不重跑。客户端、代码、题库、预算或选择变化需新建批次。旧批次使用原冻结版本复核，不转换成新范围成绩。

### 显式并行补跑

用户明确授权重试后，可从已停止的正式公开批次建立独立补跑集合。原 `formal-run`、`cli-batch --resume` 的策略保持不变。新集合冻结修复后的代码、原题目和配方，标记 `official: false`；不修改原正式登记，也不把跨运行器版本的补跑结果直接当作原协议正式成绩。

```sh
python3 -m benchmark parallel-prepare \
  --prior-batch ./runs/first-stopped-formal-batch \
  --prior-batch ./runs/second-stopped-formal-batch \
  --out ./runs/recovery
python3 -m benchmark parallel-audit --batch-dir ./runs/recovery --decisions ./capture-audit.json
python3 -m benchmark parallel-run --batch-dir ./runs/recovery \
  --credentials-registry ./runs/config/agents.local.json --execute
python3 -m benchmark parallel-status --batch-dir ./runs/recovery
```

- 每个独立题目由独立工作进程执行，各自使用原有沙箱；两阶段题、连续咨询的全部轮次作为一个调度单元顺序执行。默认每个产品最多 2 个工作进程，Codex 每个模型配置最多 1 个，Astra 和 Sol 合计不超过 2 个。多账号也保守共用该产品上限。
- 原失败/中断尝试加一次补跑合计最多 2 次；原未尝试题首次运行后，仅临时网络、过载或超时可再试一次，默认间隔 30 秒。多轮题重试时从第一轮建立新会话，原多轮尝试完整保留。普通单题失败（包括缺失答卷或未分类的任务失败）保留为失败、等待诊断，其他独立题继续，不自动重跑该题。额度、认证、环境准备或隔离检查错误暂停该产品的新任务；不轮换账号、切换模型或认证。用户停止后不自动重试被中断的新尝试。
- 原“已完成”记录先进入 `legacy_review`，不会自动视为完整。审计 JSON 为对象数组，每项有 `index`、`action` 和 `reason`：`retain` 还须提供原 `answer_sha256`，保留已确认包含完整正文的旧最终回复；`recover` 仅从完整且成功确认的 Kimi/Qwen 文件写入事件提取正文，并记录轨迹哈希与事件位置；`recapture` 用于已确认的采集缺陷，重新采集并记录原因。正文提取不包含思考和检索日志，不运行轨迹中的命令，不修改原文件。
- 新尝试在 `attempts/<job-index>/a1`、`a2` 中独立保存；恢复正文在 `recovered/`，原答卷保持原路径。`parallel-state.json` 记录选择的答卷、全部尝试与原批次关系；选择首次成功采集，不按评分选优。后续评审须分别披露首次失败、补跑次数、累计用量及旧版最终回复/轨迹恢复的来源差异。
- SIGINT/SIGTERM 或集合目录中的 `stop-request.json` 会停止派发并中断本调度器拥有的工作进程，保留已写正文与日志。正常退出后再次运行同一命令只继续排队任务；遇到未确认退出的进程或被修改的证据则拒绝恢复。它不会重启原冻结代码的批次。
- 调度器本身需要修复时，可显式运行 `parallel-rebase --previous-recovery <已停止集合> --out <新目录>` 冻结新代码。只允许迁移一次，复制并校验全部旧尝试；每题至多排除一次控制器停止造成的中断，或仅有客户端版本事件、尚未推理的授权拒绝。排除记录写入冻结清单，全部费用和失败记录仍须披露，物理尝试最多增加到 `a3`。旧集合及原正式批次保持不变。

### 持续巡检与恢复

启动后台进程不等于持续托管。需要持续托管时，另外配置宿主的定时任务：读取批次索引、状态、原始事件及进程身份；任务仍在执行时只读观察，不因状态文件暂未更新就判定卡死。调度器本身不会唤起诊断 Agent，也不会自动发送通知。

- 在运行目录外保存巡检记录，记录已处理问题和恢复目录，避免重复通知、重复重试。仅在完成、故障、恢复或需要用户操作时通知；没有变化时保持安静。
- 先判断是单题失败、采集缺陷、产品级阻塞还是执行结果未知。单题失败不阻止其他题；有完整成功写入回执且后续未修改正文时，可以在新目录中无损恢复原答卷，必须保留原始事件、哈希及修复审计。不得把摘要当答卷、执行模型轨迹中的命令，或为了通过核验重写旧哈希。
- 修改控制器、收卷器或运行组件时，保留当前进程与冻结目录；只有确认旧批次及其工作进程已退出、远端会话已终止本轮后，才在新目录冻结修复版本并恢复未完成工作。Managed Agent 批次的独立脚本也遵守同样规则。未知发送结果先回查，禁止直接重复发送。
- 自动网络重试仍受原批次预算限制。用尽预算、需要改变模型/账号/认证、证据损坏或必须由用户登录/付款时，暂停相关产品并明确通知；不通过新建目录重置次数。额度有明确重置时间时，在该时间后核对状态，仍需遵守已授权范围与剩余尝试预算。
- 巡检需有明确模型和题目范围，不自动扩大实验、改评分或发布结果。全部完成或只剩需要用户操作的阻塞后，停用该次巡检。已有冻结批次继续使用旧策略，新策略只随新建或显式恢复的批次生效。

## 全部题型统一评审与面板

配置独立 Judge 后：

```sh
python3 -m benchmark judge-batch --batch-dir ./runs/deepseek-full \
  --config config/judge.local.json --out ./runs/judge-full
python3 -m benchmark judge-batch --batch-dir ./runs/deepseek-full \
  --config config/judge.local.json --out ./runs/judge-full --resume --execute
python3 -m benchmark leaderboard-export \
  --batch-dir ./runs/judge-full --out ./runs/full-leaderboard
```

`judge-batch` 默认只冻结评审请求；`--execute` 才调用评审模型，之后自动生成 `runs/judge-full/leaderboard/index.html`。单阶段沿用原流程；两阶段合并两轮回答、按原题 rubric 评一次；连续任务按来源原题评分，另作跨阶段诊断。评早期原题的请求只含截至该题结束的历史和交付文件。失败/中断的评审请求不会因 resume 再次扣费。Judge 仍无外部浏览能力，证据不足保留 pending。

面板分母由批次记录固定：使用 `--selection` 时为清单题量；不使用清单时为所在版本的完整集合，不能将手动 `--task` 子集当成完整集合成绩。连续任务单列，不加入原题均分。全部预期题目和重复都有数值分后才显示完整均分。多个评审目录合并时须保证题库、协议、Judge配置相容且无重复答卷。

此统一入口支持本地公开 dev 批跑及下述完整跨电脑答题包。原 `submission-export/import`、不带完整范围参数的 `cli-package` 和私有 study/test 保留原单阶段契约。

## 完整题库跨电脑答题与统一收卷

使用70题公开发布副本准备题包，由答题者使用自己的Claude Code订阅作答，组织者保留题库版本并统一评审：

```sh
python3 -m benchmark cli-package --suite dev \
  --registry config/agents-cli.example.json --agent claude-opus-5-5 \
  --include-staged --include-episodes --out runs/claude-transfer
```

只发送 `runs/claude-transfer/candidates/claude-opus-5-5.zip`，保留组织者目录直到收卷与评审结束。题包范围由生成时的manifest决定：70题公开副本包含57道单阶段和13道两阶段题；`--include-episodes`额外加入2条连续案例，单独报告。也可用 `--selection <清单.json>` 固定任务范围与报告分母，自动包含清单内两阶段题；不能与 `--task` 或衍生连续任务混用。仅使用旧 `--task` 过滤仍不会缩小完整集合报告分母。运行与提交见[复现指南](reproduction.md)。

答题电脑需要 Python 3.11+、macOS 和冻结版本的原生 CLI。解压后按包内 README 执行 `setup`、`check`、预览，再执行 `python3 run_agent.py --out answers --execute`。使用答题者本机账号；无需共享账号、密钥或整个仓库。setup 只允许配置本机可执行路径和凭据，不改变冻结的认证方式、模型、版本或预算。check 不调用模型，也不证明订阅有该模型的权益。

未来阶段题面保存在答题电脑的脚本目录，模型沙箱每轮只得到当前题面；请通过脚本启动，不要让 Agent 直接在整个题包目录作答。同题逐轮恢复原生会话、目录及白名单交付文件，不同题新建会话。用 `--max-stages N` 可在阶段间暂停，再在同一电脑用 `--execute --resume` 继续。失败、超时、中断或结果不确定的阶段不自动重跑；该题后续阶段停止，其他未尝试题继续。未确认结果的 running 记录会拒绝恢复。同题不能在中途换电脑后继续。

结束或正常暂停会生成 `answers-*.zip`；也可用 `python3 run_agent.py export --out answers` 重新打包，费用为零。只回传该 ZIP；它包含所有计划项状态、原始回答字节、事件和隔离记录、会话检查点及任务交付文件，不打包 `.local/`、登录文件或个人 HOME。诊断日志与原生会话仅供内部复核。每文件最多 128 MiB、总计最多 2 GiB，超限明确拒绝，不删减证据。

组织者可直接导入 ZIP，无需手动解压：

```sh
python3 -m benchmark cli-import --package runs/claude-transfer \
  --results /absolute/returned/answers-xxxx.zip --out runs/claude-incoming
python3 -m benchmark judge-batch --batch-dir runs/claude-incoming \
  --config config/judge.example.json --out runs/claude-judged
# 核对并配置 Judge 后，显式执行评审：
python3 -m benchmark judge-batch --batch-dir runs/claude-incoming \
  --config config/judge.example.json --out runs/claude-judged --execute --resume
```

新传输格式为 `native-full-packet.1` / `native-full-return.1`，与旧 answer-only 包分开识别。导入校验原发题包、范围、配方、题面、运行组件、阶段顺序、检查点链、会话独立性、答卷与事件一致性及原始字节指纹；拒绝越界、链接、未知文件、缺项和篡改，全部校验通过后才创建收卷目录。不执行回传文件或在评审电脑恢复其会话。原始提交证据完整保留，导入只供评审，`isolation_verified=false`、`official=false`；内容指纹不能证明提交者身份或远端记录真实性。

统一评审生成 `runs/claude-judged/leaderboard/index.html`，按题包绑定的原题集合与连续任务分别统计。缺题不产生完整均分。合成测试不代表答题者账号或第二台电脑已完成真实模型验收。

## 单条连续任务与人工接口

`episode-run` 对六种已适配 CLI 共用：默认预览，`--execute` 才调用模型，`--max-stages` 限制本次阶段数，`--resume` 只从成功阶段边界续跑。

```sh
python3 -m benchmark episode-run --episode LONG-E \
  --agent deepseek-v4-1-flash --out ./runs/long-a
```

每阶段必须交付非空 UTF-8 `artifacts/action-record.md`；后续轮保留并修订。总执行预算为阶段数乘以配置时限。原生 session ID 改变、必要状态缺失或输出不确定即停止该条任务，不改成拼接历史的新会话。

手动中转仍可用 `python3 -m benchmark.longitudinal prepare/record/next`；完整答卷可用同模块 `review/score` 人工评审。原题等权预览均分不代表已校准的长期能力综合分。
