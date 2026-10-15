# CodeBuddy CLI + hy4-preview · 70题逐题记录 / Per-task records

[所有模型 / All models](../../README.md) · [中文运行说明](README.zh-CN.md) · [Methodology](../../../../docs/result-methodology.md)

This model directory follows the existing `models/<agent>/<task>/` layout and covers a partial run of one Agent configuration: CodeBuddy CLI 2.164.0 + hy4-preview, launched with `effort_requested=high`, using input version `1.0.2+privacy-v16-public70`. The user stopped this run. It is unscored and is separate from the existing 70-task leaderboard.

| Item | Result |
| --- | --- |
| Planned tasks / stages | 70 / 83 |
| Attempted tasks | 46 |
| Completed tasks / stages | 41 / 41 |
| Failed tasks | 1 |
| Tasks interrupted by the user | 4 |
| Tasks not started | 24 |
| Scoring | Unscored |

The failed task is `TRY-B-03`. The user interrupted `N09-03`, `N10-01`, `N11-03`, and `N12-01`. Interrupted output is not a complete answer. Failures and interruptions receive no fabricated zero scores. See the [per-task records](#tasks) for statuses and complete answers, and [manifest.json](manifest.json) for the structured record.

For the 41 complete answers, file sizes, archived hashes, completion states, and consistency between sent prompts and frozen inputs have been checked. Their session records identify hy4-preview; launch records request high effort. These checks establish archive consistency, not answer quality or the model's internal reasoning budget.

The recorded selection-file digest identifies the full originating manifest, which has been located and verified. `selection-input.json` is a reduced summary with a different digest. All 70 source task hashes and 83 frozen stage prompts have been verified.

This record is publicly visible in [PR #2](https://github.com/yeyuxin777/baoyan-agent-benchmark/pull/2), unscored and pending merge and formal inclusion. It contains 41 redacted complete answers; originals remain private. Personal academic values and identifying undergraduate-policy details are generalized using existing rules and this review. This does not claim global anonymity. Prompts, raw logs, identity data, private runtime diagnostics, and interrupted output are excluded from the public materials. Privacy edits change presentation without correcting the original answer's assertions or errors.

Key runtime conditions and comparison limits (online/tool access, changing timeout policy, budget and session isolation) are summarized in [manifest.json](manifest.json#L1); detailed evidence remains private.

<a id="tasks"></a>
## 逐题记录 / Per-task records

| 题号 / Task | 分数 / Score | 答卷 / Answer | 评审 / Review | 执行信息 / Execution |
| --- | ---: | --- | --- | --- |
| Q001 · BY-A-PLAN | 未评分 / Unscored | [答卷 / Answer](BY-A-PLAN/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-A-PLAN/metadata.json) |
| Q002 · BY-A-APPLY | 未评分 / Unscored | [答卷 / Answer](BY-A-APPLY/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-A-APPLY/metadata.json) |
| Q003 · TRY-A-03 | 未评分 / Unscored | [答卷 / Answer](TRY-A-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-A-03/metadata.json) |
| Q004 · BY-B-APPLY | 未评分 / Unscored | [答卷 / Answer](BY-B-APPLY/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-B-APPLY/metadata.json) |
| Q005 · TRY-B-02 | 未评分 / Unscored | [答卷 / Answer](TRY-B-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-B-02/metadata.json) |
| Q006 · TRY-B-03 | 未评分 / Unscored | — | 未评审 / Not reviewed | 失败 / Failed · [元数据 / Metadata](TRY-B-03/metadata.json) |
| Q007 · BY-C-WINTER | 未评分 / Unscored | [答卷 / Answer](BY-C-WINTER/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-C-WINTER/metadata.json) |
| Q008 · BY-C-APPLY | 未评分 / Unscored | [答卷 / Answer](BY-C-APPLY/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-C-APPLY/metadata.json) |
| Q009 · TRY-C-03 | 未评分 / Unscored | [答卷 / Answer](TRY-C-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-C-03/metadata.json) |
| Q011 · TRY-D-02 | 未评分 / Unscored | [答卷 / Answer](TRY-D-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-D-02/metadata.json) |
| Q012 · BY-E-APPLY | 未评分 / Unscored | [答卷 / Answer](BY-E-APPLY/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-E-APPLY/metadata.json) |
| Q013 · TRY-E-02 | 未评分 / Unscored | [答卷 / Answer](TRY-E-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-E-02/metadata.json) |
| Q014 · TRY-E-03 | 未评分 / Unscored | [答卷 / Answer](TRY-E-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-E-03/metadata.json) |
| Q015 · BY-F-APPLY | 未评分 / Unscored | [答卷 / Answer](BY-F-APPLY/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-F-APPLY/metadata.json) |
| Q016 · TRY-F-02 | 未评分 / Unscored | [答卷 / Answer](TRY-F-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-F-02/metadata.json) |
| Q017 · TRY-G-01 | 未评分 / Unscored | [答卷 / Answer](TRY-G-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-G-01/metadata.json) |
| Q018 · TRY-G-02 | 未评分 / Unscored | [答卷 / Answer](TRY-G-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-G-02/metadata.json) |
| Q019 · BY-H-APPLY | 未评分 / Unscored | [答卷 / Answer](BY-H-APPLY/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-H-APPLY/metadata.json) |
| Q020 · TRY-H-02 | 未评分 / Unscored | [答卷 / Answer](TRY-H-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](TRY-H-02/metadata.json) |
| Q021 · BY-G-APPLY | 未评分 / Unscored | [答卷 / Answer](BY-G-APPLY/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-G-APPLY/metadata.json) |
| Q023 · BY-E-REPLAN | 未评分 / Unscored | [答卷 / Answer](BY-E-REPLAN/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-E-REPLAN/metadata.json) |
| Q024 · BY-E-PREP | 未评分 / Unscored | [答卷 / Answer](BY-E-PREP/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](BY-E-PREP/metadata.json) |
| Q025 · N01-01 | 未评分 / Unscored | [答卷 / Answer](N01-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N01-01/metadata.json) |
| Q026 · N01-03 | 未评分 / Unscored | [答卷 / Answer](N01-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N01-03/metadata.json) |
| Q027 · N02-01 | 未评分 / Unscored | [答卷 / Answer](N02-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N02-01/metadata.json) |
| Q028 · N02-02 | 未评分 / Unscored | [答卷 / Answer](N02-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N02-02/metadata.json) |
| Q029 · N02-03 | 未评分 / Unscored | [答卷 / Answer](N02-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N02-03/metadata.json) |
| Q030 · N03-01 | 未评分 / Unscored | [答卷 / Answer](N03-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N03-01/metadata.json) |
| Q031 · N03-02 | 未评分 / Unscored | [答卷 / Answer](N03-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N03-02/metadata.json) |
| Q032 · N03-03 | 未评分 / Unscored | [答卷 / Answer](N03-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N03-03/metadata.json) |
| Q033 · N04-01 | 未评分 / Unscored | [答卷 / Answer](N04-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N04-01/metadata.json) |
| Q034 · N04-03 | 未评分 / Unscored | [答卷 / Answer](N04-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N04-03/metadata.json) |
| Q035 · N05-01 | 未评分 / Unscored | [答卷 / Answer](N05-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N05-01/metadata.json) |
| Q036 · N05-03 | 未评分 / Unscored | [答卷 / Answer](N05-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N05-03/metadata.json) |
| Q037 · N06-01 | 未评分 / Unscored | [答卷 / Answer](N06-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N06-01/metadata.json) |
| Q038 · N06-02 | 未评分 / Unscored | [答卷 / Answer](N06-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N06-02/metadata.json) |
| Q039 · N06-03 | 未评分 / Unscored | [答卷 / Answer](N06-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N06-03/metadata.json) |
| Q042 · N08-01 | 未评分 / Unscored | [答卷 / Answer](N08-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N08-01/metadata.json) |
| Q043 · N08-03 | 未评分 / Unscored | [答卷 / Answer](N08-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N08-03/metadata.json) |
| Q044 · N09-01 | 未评分 / Unscored | [答卷 / Answer](N09-01/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N09-01/metadata.json) |
| Q045 · N09-03 | 未评分 / Unscored | — | 未评审 / Not reviewed | 用户中断 / Interrupted by user · [元数据 / Metadata](N09-03/metadata.json) |
| Q046 · N10-01 | 未评分 / Unscored | — | 未评审 / Not reviewed | 用户中断 / Interrupted by user · [元数据 / Metadata](N10-01/metadata.json) |
| Q047 · N10-02 | 未评分 / Unscored | [答卷 / Answer](N10-02/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N10-02/metadata.json) |
| Q048 · N10-03 | 未评分 / Unscored | [答卷 / Answer](N10-03/answer.md) | 未评审 / Not reviewed | 完成 / Completed · [元数据 / Metadata](N10-03/metadata.json) |
| Q049 · N11-03 | 未评分 / Unscored | — | 未评审 / Not reviewed | 用户中断 / Interrupted by user · [元数据 / Metadata](N11-03/metadata.json) |
| Q050 · N12-01 | 未评分 / Unscored | — | 未评审 / Not reviewed | 用户中断 / Interrupted by user · [元数据 / Metadata](N12-01/metadata.json) |
| Q051 · N12-03 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N12-03/metadata.json) |
| Q052 · N13-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N13-02/metadata.json) |
| Q053 · N13-03 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N13-03/metadata.json) |
| Q054 · N14-01 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N14-01/metadata.json) |
| Q055 · N14-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N14-02/metadata.json) |
| Q056 · N14-03 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N14-03/metadata.json) |
| Q057 · N15-01 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N15-01/metadata.json) |
| Q058 · N15-03 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N15-03/metadata.json) |
| Q059 · N16-01 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N16-01/metadata.json) |
| Q060 · N16-03 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N16-03/metadata.json) |
| Q061 · O08-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](O08-02/metadata.json) |
| Q062 · N01-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N01-02/metadata.json) |
| Q063 · N04-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N04-02/metadata.json) |
| Q064 · N05-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N05-02/metadata.json) |
| Q066 · N08-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N08-02/metadata.json) |
| Q067 · N09-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N09-02/metadata.json) |
| Q068 · N11-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N11-02/metadata.json) |
| Q069 · N12-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N12-02/metadata.json) |
| Q070 · N15-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N15-02/metadata.json) |
| Q071 · N16-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](N16-02/metadata.json) |
| Q072 · O01-04 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](O01-04/metadata.json) |
| Q073 · O03-04 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](O03-04/metadata.json) |
| Q074 · O04-04 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](O04-04/metadata.json) |
| Q075 · O06-02 | 未评分 / Unscored | — | 未评审 / Not reviewed | 未开始 / Not started · [元数据 / Metadata](O06-02/metadata.json) |
