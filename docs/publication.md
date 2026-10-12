# 题库导出

```sh
python3 scripts/prepare_publication.py --out ./dist/current
```

在指定位置生成GitHub源代码目录和Hugging Face数据目录，包含70题及其评分资料。题面见[题目与评分](../data/questions-and-scoring.md)。

清单为[config/selections/current.json](../config/selections/current.json)。每次导出前校验内容哈希，目标目录必须不存在。

答卷及已有评分通过`export_public_answers.py`导出，见[答卷公开说明](experiment-publication.md)。

## 维护已发布的 HF 数据目录

当前 Hub 使用 `single-stage.jsonl`、`staged.jsonl`、`rubrics.jsonl` 三个加载配置。维护这个已审核版本时，使用完整的本地镜像和已批准的中英文说明：

```sh
python3 scripts/sync_public_release_metadata.py --hf-root /path/to/current70-hf-checkout
python3 scripts/prepare_publication.py --hf-source /path/to/approved-hf-snapshot \
  --hf-readmes /path/to/approved-bilingual-readmes --out ./dist/hf-current
```

同步脚本使用 `config/difficulty/current70-owner-approved.csv` 和当前 GitHub 70 题来源，更新 HF 投影中的负责人确认状态与难度，并重建对应的 JSONL、指纹和校验值；保留 HF 已审核的题面及评分正文。两端 rubric 不保证字节相同，HF manifest 记录 GitHub 选题清单来源哈希。随后导出模式检查来源文件哈希、当前稳定题目ID、阶段、评分材料及确认状态，再复制原有字节。输出只含当前数据材料，排除 `selection-20.json`、`selection-40.json`、`selection-75.json` 和 `CALIBRATION-STATUS.md`。目标目录必须是新目录；不会调用模型或上传。

中英文说明必须声明现有三个加载文件、保留屿智同行项目归属和保研岛致谢，不得引用已移除文件。纯题目、完整题目与评分、评分通则继续保留在根目录。文档更新不改变题面、评分规则、数据版本或旧结果的适用范围。

上面的通用题库导出和 `python3 -m benchmark hf-export` 是其他本地导出格式，不能直接覆盖这个 Hub 版本；发布前应核对文件结构和加载配置。
