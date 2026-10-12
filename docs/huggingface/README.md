---
language:
- zh
license: cc-by-4.0
task_categories:
- text-generation
---

# Baoyan Benchmark Public Question Bank

This is 屿智同行's benchmark project for graduate admissions agents.

Question text is anonymized; exported manifests record the version and content hashes.

This dataset contains 70 tasks and 83 answer stages: 57 single-stage tasks and 13 two-stage tasks, with each two-stage task scored as one task. Some tasks share background context. Full questions, scoring criteria and reference projects are provided in [Questions and scoring](questions-and-scoring.md).

[Complete questions and scoring](questions-and-scoring.md) · [Current selection](selection.json) · [General scoring policy](scoring.md)

`tasks.jsonl` contains task records with separate stages where applicable. `rubrics.jsonl` contains publicly downloadable scoring criteria. Keep rubrics and reference projects outside the tested agent's inputs.

Each task is worth 100 points. Recommendation tasks allocate 70 points to project matching and 30 to recommendation quality. Pending disputes remain pending. This dataset contains no agent answers, scores or execution records.

Data: CC BY 4.0. Record the document hash and agent configuration when reporting an evaluation.
