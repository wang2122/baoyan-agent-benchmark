# 重复实验与评审校准

用于新增重复实验与多评审校准；当前榜单计分方法见[说明](result-methodology.md)。

## 先准备候选协议

```sh
python3 -m benchmark study-prepare --out /absolute/private/study-v1
python3 -m benchmark study-check \
  --protocol /absolute/private/study-v1/protocol.draft.json
```

第一条生成protocol.draft.json、readiness.template.json、baselines.draft.json、runtime-acceptance.template.json和protocol-decision.template.json。第二条列出待确定的规则和待提供的证据，未完成时退出码为1；格式或指纹错误为2。这些是候选设置，不能将生成模板当成已通过验收。

3次重复、900秒是dev试跑的起点，不是统一行业标准。沿用按题等权的均分，先平均同题的重复，再平均题目。覆盖不足、失败或待核使完整总均分保持null，同时报告完成率和状态数量。不把失败自动变成0分，不挑选最佳重试。

草案需固定题库、配置、联网条件、重复次数、时限和主评审设置。实际调用须显式 `--execute`。

## 按草案准备重复批次

对dev试跑先将草案evaluation_split设为dev；正式test使用另一个已审定的协议版本。不要为匹配协议而更改题面的联网要求。

```sh
# 仅准备；registry需已预检，repeats和timeout必须匹配协议。
python3 -m benchmark batch --split dev \
  --protocol /absolute/private/study-v1/protocol.draft.json \
  --registry /absolute/private/study-v1/baselines.local.json \
  --out /absolute/private/runs/pilot-v1
```

实际调用需要明确追加--execute。App条目仍等待人工交卷，每题每次重复使用新会话。设置、任务选择、协议或代码改变时创建新批次。现有batch命令不传--protocol时保留原行为；原有固定preview入口仍为每题1次，不会被本工具静默改成3次。

CLI自动路径仅支持provided_materials_only。如果新题要求联网或历史资料检索，需要先补执行与来源归档，并在真实验收中证明工具遵守题面条件；不能用给定材料路径冒充已支持联网任务。

## 重复运行统计

完成独立评审并生成scored结果目录后运行：

```sh
python3 -m benchmark statistics \
  --batch-dir /absolute/private/review/scored \
  --out /absolute/private/analysis/statistics-v1 \
  --bootstrap-samples 2000 --seed 0
```

输出statistics.json和statistics.md，包含每题重复均分与样本标准差、每次重复的总均分、完整总体均分、题型/难度分组、完成率、运行证据覆盖及配置之间的配对分差。两个配置的差值方向为left_minus_right。至少2次有效重复才报告重复标准差，1次时为null。

总均分要求完整规范suite及全部预期重复均有可比较的成绩。不能用只选的成功子集生成完整总均分。--expected-repeats可以补充尚未确定重复次数的旧协议，但不得低于已计划次数或覆盖已声明的协议次数。缺失重复不补0分，不在缺失的配对题目上计算完整分差。

--batch-dir可重复，用于同版协议、同版数据和同一主评审下的分开配置批次。相同配置/题目/重复的记录重复出现会被拒绝，不能把重试当作额外重复。相同配置设置必须一致；App实际模式变动形成独立条件组，覆盖不足时不报告完整均分。

95%区间通过对case_group_id对应的整个关联任务组进行有放回抽样计算，组内保留全部题目，每次抽样仍按题目数量加权。区间条件于已经观测到的重复均分，不涵盖所有重复生成、评审或题库选择的不确定性；重复波动另报。少于2个关联任务组不生成区间，组数少时区间也可能不稳定。配对分差使用相同关联任务组抽样，不用两个独立区间是否重叠来判定优劣。多个配对区间未经多重比较调整，只作描述，不自动生成显著性排名。

## 两位或多位评审校准

每位评审独立获取匿名packet，组织者保管来源映射。两位评审必须评相同原始答卷，分别填写真实review-settings.json；每个结果集保持一个实际评审配置。重复使用同一配置并改名字不证明独立性，工具只校验所记录的身份和设置。

```sh
# 每位评审从同一原始incoming生成自己的packet和scored目录。
python3 -m benchmark review-batch --batch-dir /absolute/private/review/incoming \
  --out /absolute/private/review/reviewer-a-packets
python3 -m benchmark review-batch --batch-dir /absolute/private/review/incoming \
  --out /absolute/private/review/reviewer-b-packets

# 分别填写后，各自用review-apply生成reviewer-a和reviewer-b结果目录。
python3 -m benchmark calibration-compare \
  --batch-dir /absolute/private/review/reviewer-a \
  --batch-dir /absolute/private/review/reviewer-b \
  --out /absolute/private/analysis/calibration-v1
```

输出总分平均绝对差、有向差、逐项一致数量、待核覆盖、质量档位的区间尺度Krippendorff α和覆盖标签的名义尺度α，以及候选提取、层次、评分项和总分的分歧清单。0/0.5/1在这里解释为等距的给分比例；不是声称所有语义判断均为等距量。α是跨评分单元的汇总诊断，可能为负；有效评分不足或所有边际评分相同导致分母为0时为null。pending视为缺失并单独报告，不能作为一致。

校准样本覆盖题型、产品配置、回答质量和边界案例。可先从10–20份dev答卷开始，随覆盖不足扩充；不规定通用α达标阈值。LLM主评审需要与领域教师对照，由教师查看分歧后决定评分口径是否可靠。正式test开始前冻结rubric和主评审配置。

## 保存人工裁决

将adjudication-template.json另存为decisions.json。每个已裁决条目填写status=resolved、resolution、reason、真实reviewer、reviewer_type=human及非空evidence_references；未裁决保持pending。

```sh
python3 -m benchmark calibration-adjudicate \
  --report-dir /absolute/private/analysis/calibration-v1 \
  --decisions /absolute/private/analysis/decisions.json \
  --out /absolute/private/analysis/adjudication-v1
```

## 真实验收与协议冻结

每个配置在代表题上验证原文保存、模型与版本记录、工具条件、必需轨迹、交卷导入导出和成绩重算。runtime-acceptance.template.json中的检查项只有得到实际证据后才能设为true；App缺少必需轨迹时不能宣称满足该项。报告是组织者记录，不能认证远端账号或执行真实性。

readiness.template.json另存为readiness.json。五项gate分别为dataset、calibration、stability、runtime和protocol_decisions。每项approved必须有实际approved_by、reason和证据文件；路径相对readiness.json所在目录或使用绝对路径。更新草案后同步其protocol_sha256，再检查证据是否仍支持新规则；运行规则实质变化时重新试跑。

每个artifact使用如下结构：

```json
{"kind": "statistics", "path": "analysis/statistics.json", "sha256": "文件原始字节的64位SHA-256"}
```

证据kind在整份readiness中唯一，分别使用dataset_manifest；calibration及必要的adjudication；statistics；runtime_acceptance；baseline_registry、primary_reviewer_settings及protocol_decision。所有引用证据为本地JSON文件，不包含凭证。dataset gate审批要覆盖题库完整性、教师校准及独立留出边界；protocol_decision文件记录校准验收口径、时间/重复设置和比较条件的最终决定。

```sh
python3 -m benchmark study-check \
  --protocol /absolute/private/study-v1/protocol.draft.json \
  --readiness /absolute/private/study-v1/readiness.json

# 全部检查通过后保存新快照，不覆盖草案或启用正式榜单。
python3 -m benchmark study-freeze \
  --protocol /absolute/private/study-v1/protocol.draft.json \
  --readiness /absolute/private/study-v1/readiness.json \
  --out /absolute/private/study-v1/frozen
```