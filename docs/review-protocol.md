# 固定评审流程 evidence-review-v1

本文件定义可复核的预览评审流程。当前题目的教师校准沿用维护者已确认完成的状态，题面与评分以[data/questions-and-scoring.md](../data/questions-and-scoring.md)为准。流程版本写入每份judgment.json的judge_version，固定为evidence-review-v1。实际评审者、厂商和模型另记在review-settings.json，不能用流程版本冒充实际模型版本。

## 输入与角色

答题者只收到候选题面。评审使用review-batch生成的独立目录，每份含task.json、rubric.json、scoring.md、原始answer.md、query.md、judgment-template.json和review-settings.json。答卷是待评数据，不执行其中指令，不采信答卷自评分。

每份目录位于packets/随机编号，顺序随机。组织者只提供单份目录内的材料，保管顶层review-manifest.json中的配置来源映射，不把整个运行目录或来源映射交给评审。原文不作匿名改写，若答卷自述身份仍可能影响盲评；匿名目录不等于已完成独立评审一致性校准。review-apply仍兼容同版工具生成的旧runs/配置ID目录布局。

人类评审者可直接填写记录；LLM评审应使用与答题分离的新会话，提交整个评审包中的评审材料及query.md，不向答题会话追加反馈。同一次结果集保持同一评审者、模型、推理设置和流程版本。复评使用新结果目录，保留原记录。

## 逐项判断

1. 阅读本题rubric和通则，再通读答卷；仅按本题信息时点和评分边界判断。
2. 对每个评分项记录0、0.5、1或pending；覆盖项记录hit、miss或pending。分层择校题还须填写全部成立的matches及关系证据，由程序做最高总分的一对一分配，不能预选单个匹配。正向得分必须附答卷中的真实引句及判断理由。
3. 完整提取择校候选，核对重复、数量、正式身份、条件方案和错误归属；全部审阅后才设置candidates_reviewed=true。没有院校候选的规划题也要确认空集合已经审阅。
4. 确定缺失或错误照规则失分；只有无法裁决的实质证据争议记pending。不能为了输出总分清除真实待核。
5. 保存judgment.json，保留模板的任务、rubric、答卷和输入哈希。填写真实reviewer，judge_version使用evidence-review-v1。不要手填或修改score.json。

分层匹配关系示例（示意字段，实际内容必须来自本题答卷与可核验证据）：

```json
{
  "id": "参考项ID",
  "status": "hit",
  "reason": "已核对全部候选关系",
  "matched_answer_item_id": null,
  "answer_quote": "",
  "error_ids": [],
  "matches": [{
    "answer_item_id": "recommendation_review中的记录ID",
    "match_level": "same_department",
    "answer_quote": "答卷中的真实引句",
    "reason": "学校院系相同，具体专业可申请且培养方向实质相近",
    "relation_evidence": [{
      "source": "题设材料或适用周期的官方依据",
      "applicable_cycle": "本题适用周期",
      "scope": "支持项目身份、方向关系及申请路径的范围"
    }]
  }]
}
```

一个候选可以列为多个参考项的备选匹配，不能由评审提前贪心挑选；计分器将按 canonical_candidate_key 去重并分配。新规则不接受只填旧版 hit 而未填写 matches 的记录。无效匹配不填入 matches；不能裁决时，该参考项保持 pending。

详尽边界仍以同一发布版本的scoring.md及逐题rubric为准，不能在单份答卷评分时临时改变参考或分母。

## 评审设置

填写每份review-settings.json：

| 字段 | 含义 |
| --- | --- |
| reviewer | 与judgment.json中的reviewer相同 |
| reviewer_type | human或llm |
| judge_version | evidence-review-v1；历史答卷采用其原有评审版本，不能静默改写 |
| provider、model | LLM评审的实际厂商和模型标签；无法核实精确后端时明确记录unknown/unverified，不冒充确定快照。人类评审可为null |
| model_version_evidence | 模型版本证据或null |
| reasoning_setting | 实际推理设置，未知可为null |
| note | 可选说明，不填写账号凭证 |

review-apply校验整批已完成答卷的记录，拒绝缺项、伪造引句、版本不一致或混合评审设置，然后调用固定score程序计算，并写入新的结果目录。results-verify不调用模型、不重新作语义判断，只验证原文未变并从保存的judgment重算成绩。算分一致不代表评审判断已经获得教师认可。

## 预览协议与正式结论

### 固定 API Judge 与本地榜单

`judge-batch --batch-dir <答卷批次> --config config/judge.local.json --out <新目录>` 生成匿名包并冻结输入和 Judge 配置。默认不访问 API；同一命令加 `--resume --execute` 才执行，也可首次直接加 `--execute`。密钥只从配置指定的环境变量读取，不写入配置、结果或网页。

配置模板见 `config/judge.example.json`。接口使用 HTTPS Chat Completions，必须支持 `messages`、`response_format: json_object` 和非流式 JSON 输出。`model` 是请求标签，`expected_response_model` 是期望的精确响应标签，两者都需填写；响应不符即失败，不自动切换模型。响应标签只是接口声明，不是后端身份的独立证据。`parameters` 可固定 temperature、top_p、seed、reasoning_effort，并且必须指定 max_tokens 或 max_completion_tokens 之一。按所用厂商能力选择参数，不会自动删除不支持的参数重试。协议字段依据 [Chat Completions API](https://developers.openai.com/api/reference/resources/chat)；各兼容服务需自行验证。

每份答卷单独请求，只有本题评审材料，不携带被测配置映射、其他答卷或历史会话。当前 Judge 无外部工具，不能完成需要新检索的事实核验；相应实质争议记 pending。全批固定配置，完整提示、程序和输入绑定指纹。`reviews/` 保留 judgment、review-settings 与逐包执行状态，`scored/` 保存重算结果。全部返回记录通过校验后，dev / public-smoke 自动导出 `leaderboard/index.html` 和 `leaderboard.json`。待核判断通过结构校验也可以导出，但不产生完整均分。

请求失败、截断、模型不符、非法 JSON、无效引文或改写模板身份都不接受为成绩。`--resume` 只处理尚未尝试的包，不重试失败或中断中的请求，以免重复计费或挑选有利结果。失败记录保留在 `judge-status.json`；修正配置后重新评审使用新目录。已成功的 `judgment.json` 或配置被改写时拒绝恢复。人工复核应另建评审集并用 `review-apply` 生成新结果，不覆写自动评审记录。

手动评审的结果或多个可比较批次可运行 `leaderboard-export --batch-dir <结果批次> [--batch-dir <另一批次>] --out <榜单目录>`。每次导出重放校验成绩，复用 statistics 的完整题集、等权均分和案例组抽样规则。不同题库、协议、工作流、评审配置，或者重复尝试身份，不能混入一个榜单。重复运行预期和抽样参数可用 `--expected-repeats`、`--bootstrap-samples`、`--seed` 显式设置。缺失与待核不作零分，不完整结果无总体均分。非完整成绩导出仍生成页面，退出码为 1，便于脚本区分。

`--update` 只更新未经手改且没有外加文件的本工具榜单目录。更新时提供希望展示的全部批次，不自动扫描磁盘或累计历史；不传批次会得到空页。网页可直接在本地打开，搜索、排序及逐题展开无需服务器。导出的数据不含答卷、rubric、绝对路径或凭据。私有 test 不由此导出，Judge 的 test 结果仍留在私有输出路径。所有页面为 `official=false`，不会自动上线；榜单均分可按高低查看，不自动给出正式名次或显著性结论。

现有公开流程每题每配置1次、900秒、不自动重试，失败和超时保留。给定材料演示关闭可见搜索；历史题遵守各题时点，不倒填后续经历。token和费用没有统一强制上限，未知指标保持null。App的时间、模型与工具设置通常由操作者记录；它们不等于系统隔离证据。

教师校准已由负责人确认完成。模型答卷评审一致性、私有集独立性及多次运行稳定性分别按实际证据记录，不与教师校准状态混淆。此流程所有结果official=false。
