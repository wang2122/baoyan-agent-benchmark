# 答卷与评分公开范围

公开材料的隐私处理和仍待复核的范围见[数据说明](privacy-display.md)。

公开内容包括评测方法与配置、Agent答卷及已有逐题评分和依据。

| 内容 | 处理方式 |
| --- | --- |
| Agent产品、模型标签、客户端版本、认证方式、工具和运行限制 | 按已有记录说明；未知字段保留未知，不发布凭据 |
| 单阶段最终答案、两阶段各阶段答案 | 保留实际内容；必要隐私脱敏记录来源与导出哈希 |
| 已有逐题得分、待核项及评分依据 | 保留原有版本、评分状态及裁决；无评分不补成零分 |
| 失败、中断、重试或补跑 | 披露影响成绩解释的状态或方法差异，不包装为统一首次成功 |
| 完整对话轨迹、工具调用明细、搜索记录、终端日志、评审运行日志 | 内部保存，不纳入公开答卷包 |
| 账号凭据、会话检查点、私有题面与参考答案 | 不公开 |

公开材料包含答案和现有评分依据；完整轨迹内部保存。

[当前70题](../experiments/records70/README.md)与[18题记录](../experiments/records18/README.md)分别按模型和题目保存答卷与评分。原始完整材料在本地私有目录保存。

本地导出答卷包：

```sh
python3 scripts/export_public_answers.py --archive selected-historical-answers --out ./dist/historical-answers
```

当前公开选定结果使用 `models/<模型>/<题目>/`；逐题保存现有答卷、评分、评审和运行元数据。旧尝试在私有档案中保留。

目标目录必须不存在。导出仅包含当前70题清单与可用答卷的交集。题库导出见[导出说明](publication.md)。

## English

Exports include retained-task answers and available scoring evidence. Full traces stay private; manifests identify versions and coverage.
