# 固定版本运行、交卷与成绩复核

使用Python 3.10+，标准库即可运行。运行前固定所用发布包、题库清单及文件指纹；历史结果须使用其对应的冻结版本复核。以下命令在仓库根目录执行。

题库评测的完整流程见[中文使用指南](quick-start.md)或[English quick start](quick-start.en.md)。本文补充App手工收卷、跨电脑交卷及结果复核。

## 选择入口

| 入口 | 题目与使用方式 |
| --- | --- |
| cli-batch --selection config/selections/current.json | 70道公开原题、83阶段；另开实验与报告 |
| cli-batch --suite dev | 公开发布副本的57道单阶段dev题 |
| cli-batch --suite dev --include-staged --include-episodes | 公开发布副本的70道原题及2条单独报告的连续任务 |
| reproduce --profile config/reproduction/public-dev-apps.json | 当前副本单阶段dev题；App人工作答并导入 |

CLI与App统一使用规范题库和评分规则，但产品工具条件不同，须分别记录。

## CLI运行

安装、配置、单题试跑及70题批跑见[快速开始](quick-start.md)，其他客户端见[客户端指南](native-cli.md)。以下补充手工交卷和成绩复核。

## App作答

```sh
python3 -m benchmark reproduce \
  --profile config/reproduction/public-dev-apps.json \
  --baseline gemini-app --out /absolute/runs/app-dev
```

--baseline可重复，也可以选kimi-k3或qwen-app。该配置准备公开发布副本的57道单阶段题。只将workspace/app-prompt.md全文发给App，每题新会话，不追加提示或修改回答。保存原始答卷文件并填写会话记录，使用record-answer --answer --session导入，见下文“App作答与收集”。App不受CLI沙箱控制。

你也可以将题号、实际产品/模式和原始回复交给评审方，由评审方负责记录。未知耗时或模型身份必须保留为未知，此类答卷可评内容，但不能冒充已符合完整运行预算和工具条件；现有App计时收集路径需要实际开始、结束时间。

## 标准交卷：两台电脑可以分工

本节 `submission-export/import` 用于旧单阶段批次。包含两阶段题和连续任务时，用 `cli-package --include-staged --include-episodes` 生成独立答题包、`cli-import` 直接导入答题者的回传 ZIP，再交给 `judge-batch` 统一评审。详见 [完整题库跨电脑流程](native-cli.md#完整题库跨电脑答题与统一收卷)；两种传输格式不能混用。指定子集可使用 `cli-package --selection <清单.json>`，报告分母随清单固定；运行与提交均按同一固定任务清单记录。

答题电脑导出答卷包：

```sh
python3 -m benchmark submission-export \
  --batch-dir /absolute/runs/app-dev --out /absolute/transfer/answers.zip
```

包包含预定尝试、失败/未完成状态、候选题面、已捕获的原始答案字节、版本指纹和明确的运行元数据；不包含rubric、参考答案、环境变量、凭证、客户端日志或启动命令。不能从包中删除失败项挑选成绩。私有test包只允许写到公开仓库外，评审电脑另需合法持有同版私有题库。

传输格式为带manifest.json的ZIP：schema_version分别是benchmark-submission.1和benchmark-results.1，文件清单记录原始字节的SHA-256。结构定义见schemas/submission.schema.json和schemas/reproduction.schema.json；实际导入器还校验完整尝试集合、版本指纹和算分，不需要安装JSON Schema库。

评审电脑使用同一个发布版本导入：

```sh
python3 -m benchmark submission-import \
  --package /absolute/transfer/answers.zip --out /absolute/review/incoming

python3 -m benchmark review-batch \
  --batch-dir /absolute/review/incoming --out /absolute/review/packets
```

导入会拒绝文件篡改、路径越界、未知文件、题目/协议/代码不一致及丢失的预定尝试。不执行包中的文件，不覆盖已有目录。校验的是内容指纹，不是提交者身份或远端运行的真实性；导入的隔离状态标为未经本机复测。

## 评审、计算和导出成绩

按review-protocol.md为每个已完成答卷填写judgment-template.json并另存为judgment.json，同时填写review-settings.json。模板放在review-batch生成的packets/随机编号目录中，失败或超时不用伪造评审。组织者保管顶层review-manifest.json及其来源映射，只向评审提供单份packet的内容；随机目录名隐藏配置身份，答卷原文若自述模型名称仍会保留。

```sh
python3 -m benchmark review-apply \
  --batch-dir /absolute/review/incoming --reviews /absolute/review/packets \
  --out /absolute/review/scored

python3 -m benchmark results-export \
  --batch-dir /absolute/review/scored --out /absolute/transfer/results.zip
```

原始答卷目录不被改写；结果目录保留原文、逐项判断、评审设置、score.json、summary.json及verification.json。不同评审设置不能混入同一结果集。未评分、待核、失败或覆盖不全时不报告完整均分；completed不等于成功，当前没有冻结成功率阈值。

trajectory_requirements单独报告必需轨迹的记录覆盖。App原文导入可以完成内容评分，但缺少必需轨迹时不能据此声称完成运行证据要求。失败答卷的原始字节可以随答卷包传输，非UTF-8失败产物不会被转成可评分正文。

公开材料包含答卷、配置与已有评分依据。原始交卷ZIP和运行日志内部存档，见[答卷公开说明](experiment-publication.md)。

第三方复核已有成绩无需访问模型账号：

```sh
python3 -m benchmark submission-import \
  --package /absolute/transfer/results.zip --out /absolute/recheck/results
python3 -m benchmark results-verify --batch-dir /absolute/recheck/results
```

退出码0表示保存的判断与计算可重现；1表示尚有待评分或未结束尝试；2表示内容、版本或格式校验失败。results-verify不是重新作一次语义评审。在线模型再次生成的答案可变化，不保证新实验分数与旧成绩相同。

## 构建70题公开发布副本

```sh
python3 scripts/prepare_publication.py --questions-only --out dist/public-questions-YYYYMMDD-N
```

输出70道当前公开题及评分资料，记录题目指纹和文件校验值，不包含答卷、轨迹或成绩。目标目录必须不存在，命令不调用模型或上传。结构和审查要求见[发布说明](publication.md)。

代码采用MIT，数据采用CC BY 4.0。贡献题库修订时，请注明题号、任务ID、受影响评分项及依据；公开答卷先做隐私检查。

## App作答与收集

支持配置中的 Gemini、Kimi 和千问 App 手动收卷，不自动操作产品界面，也不调用其模型 API。产品入口、实际模型标签、模式、思考设置和联网状态按界面如实记录；不把模型自述当作后端版本证明。

依次复制本题、在新会话提交、记录回答开始和结束时间、保存完整原文。每题只收集一次尝试，不追加提示、不润色、不用最好一次替换失败记录。只将本题提示交给被测Agent，不提供评分资料或整个运行目录。

填写 app-session.template.json 后，按原始字节导入答卷文件：

```sh
python3 -m benchmark record-answer --run-dir <单题运行目录> \
  --answer <原始UTF-8答卷> --session <填写后的会话记录>
```

记录实际 surface、app_version、selected_model_label、mode、reasoning_setting、web_search、memory、开始/结束时间及 outcome。完成项须为新会话、无额外用户消息、未观察到模型切换；材料题不能启用搜索。失败且无答案时可以省略 --answer。无法确认的字段按模板允许值记录 unknown 或 not_available。

导入拒绝覆盖已尝试记录。App时间、设置及身份属于操作者记录，isolation_verified=false；completed仅表示答卷交付完成，不等于能力达标。缺少必需轨迹时应单独披露。

## 单题与自定义适配器

```sh
python3 -m benchmark run --split dev --task BY-A-PLAN --out ./runs/example
python3 -m benchmark record-answer --run-dir ./runs/example \
  --answer <原始答卷> --adapter my-agent --adapter-version <版本> --model <模型>
python3 -m benchmark judge-package --run-dir ./runs/example --out ./runs/example-judge
python3 -m benchmark score --run-dir ./runs/example --judgment ./runs/example-judge/judgment.json
```

只将 workspace 交给被测 Agent；其外层目录保存评审材料。自定义命令可通过 BENCH_TASK、BENCH_QUERY、BENCH_ANSWER、BENCH_TRAJECTORY、BENCH_USAGE 读取输入和交付路径。通用入口仅控制进程时限，隔离、工具与网络权限由适配器负责；不能沿用原生CLI的隔离声明。

`batch` 为通用批量入口，`cli-batch` 为原生CLI入口。给定材料配置可使用 `scripts/baselines.py check --resolve-out <新registry>` 固定本地版本；联网题使用原生CLI对应配方。汇总可用 `aggregate --split dev --run-dirs <各运行目录> --out <新目录>`，覆盖不足、失败或待核时不报告完整总均分。
