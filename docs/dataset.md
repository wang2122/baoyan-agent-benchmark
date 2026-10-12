# 数据集

题面与答卷按现有参考处理；隐私复核范围、版本与来源哈希见[数据说明](privacy-display.md)。

[题目与评分](../data/questions-and-scoring.md)包含70道题，沿用原Q题号（不连续），共83个作答阶段。

| 类型 | 题数 | 作答阶段 |
| --- | ---: | ---: |
| 完整择校推荐 | 21 | 21 |
| 单阶段规划与决策 | 36 | 36 |
| 两阶段题 | 13 | 26 |
| 合计 | 70 | 83 |

每题独立评分，两阶段题合并为一道题。部分任务存在背景关联，题目数量不等同于独立案例数量。

## 文件对应

- `data/questions-and-scoring.md`：完整题面、评分项、参考项目及完整评分通则。
- `config/selections/current.json`：题库清单，`question_id`对应文档题号，`task_id`对应执行工具使用的ID。
- `data/dev/<task_id>/task.json`与`rubric.json`：57道单阶段题。
- `data/staged-dev/<task_id>/task.json`与`rubric.json`：13道两阶段题。
- `docs/scoring.md`：与文档附录一致的评分通则，供执行和评审工具读取。

清单记录题库文档、评分通则、集合及逐题内容哈希。修改题目或评分条件后，须同步对应文件和哈希。70题的教师校准已由维护者确认完成。

`data/longitudinal/episodes.json`将题库中的题目组合成连续咨询流程，单独报告结果。`examples/public-smoke/`包含合成的工具验证示例。

[当前70题答案与评分](../experiments/records70/README.md)按模型和题目保存；[18题执行记录](../experiments/records18/README.md)单独成套。工具提供合成流程示例，连续咨询结果单独报告。

[题库导出](publication.md) · [答卷公开范围](experiment-publication.md)
