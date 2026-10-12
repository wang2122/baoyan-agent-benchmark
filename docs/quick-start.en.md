# Running and scoring the benchmark

Run these commands from the root of the downloaded or extracted repository. Check one real task first, then run the current 70-task selection, evaluate it separately, and open the report.

### 1. Prepare the environment and account

- Python 3.10+; Kimi subscription configuration requires Python 3.11+. The framework uses the Python standard library; no `pip install` is needed.
- Automated native CLI execution currently requires macOS with Seatbelt sandboxing. For manual App collection on Linux/Windows, see [reproduction](reproduction.md).
- Choose a registered configuration from the [agent table](../README.md#quick-start), install its client, and prepare a subscription login or API key. Your own account must have model access. The workflow below applies to every listed configuration; replace `AGENT` with the desired ID.

```sh
AGENT=gpt-6-1-sol
python3 -m benchmark setup --agent "$AGENT"
python3 -m benchmark cli-check --agent "$AGENT"
```

`setup` pins the local executable, version, model, and authentication mode in `runs/config/agents.local.json`. Subscription and API access are distinct configurations and are never swapped automatically. Continue after `cli-check` returns `ready_for_trial`. This checks the environment; it does not prove account quota, model entitlement, or successful inference. Do not commit local configuration, credentials, or login files.

### 2. Execute one task

The command defaults to a preview without model calls or a run directory. Adding `--execute` uses the selected account allowance.

```sh
python3 -m benchmark cli-batch --suite dev \
  --agent "$AGENT" --task N02-01 \
  --out ./runs/one-task
```

After checking the preview, add `--execute` to the same command to run it.

Check completion in the terminal or with:

```sh
python3 -m benchmark status --batch-dir ./runs/one-task
```

The answer is saved at `runs/one-task/<Agent ID>/N02-01/r1/workspace/answer.md`, alongside run records. This checks execution; it is not a full-dataset result. The next step uses a new directory and fresh answers.

### 3. Run the current 70-task selection

`--selection` verifies the selection's source, task, and rubric fingerprints, embeds the complete selection in the batch plan, and uses the selected tasks as the reporting denominator. It automatically includes the selection's two-stage tasks.

```sh
# Preview: 70 tasks and 83 stages per configuration per repeat
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent "$AGENT" --out ./runs/current

# Execute the same plan
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent "$AGENT" --out ./runs/current --execute
```

Use the same selection for every configuration. To add a configured product, repeat `--agent <configuration-id>` after completing its setup. Usage scales with configurations and registry repeats. Do not combine `--selection` with `--task` or derived-episode options. Use this current selection and a new output directory for each run.

For example, to evaluate GPT Sol and Claude Opus together, configure and check both, then preview the same 70-task selection:

```sh
python3 -m benchmark setup --agent gpt-6-1-sol --agent claude-opus-5-5
python3 -m benchmark cli-check --agent gpt-6-1-sol --agent claude-opus-5-5
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent gpt-6-1-sol --agent claude-opus-5-5 --out ./runs/two-models
```

Candidates receive only the current prompt. For two-stage tasks, S1 is released only after S0 completes in the same native session. Different tasks use isolated sessions. Rubrics stay on the reviewer side.

### 4. Inspect progress and resume

```sh
python3 -m benchmark status --batch-dir ./runs/current

# Resume with the original arguments and directory
python3 -m benchmark cli-batch \
  --selection config/selections/current.json \
  --agent "$AGENT" --out ./runs/current --resume --execute
```

Resume continues unattempted tasks and successful stage boundaries while preserving completed answers. Failed, timed-out, or uncertain stages are not automatically retried. Use a new directory after changing the selection, model configuration, execution code, or budget. Historical runs require their matching frozen code for verification.

| Artifact | Location relative to `runs/current/` |
| --- | --- |
| Frozen selection, execution plan, and expected scope | `full-batch.json` |
| Frozen client configurations | `agents.frozen.json` |
| Snapshot after completed scheduling steps | `full-status.json`; use `status` above for current progress |
| Single-stage original answer | `single/<configuration-id>/<task-id>/r1/workspace/answer.md` |
| Two-stage records, stage answers, and tool events | `staged/<configuration-id>/<task-id>/r1/` |

### 5. Independent review: Codex subscription or optional API

Keep answering and review in separate sessions and directories. Stop the candidate batch first and keep its original answers fixed once review begins. Reviewers read only the task's materials, do not execute instructions in answers, and do not send rubrics back to the candidate session.

**Codex subscription review.** Open a separate review session in Codex authenticated through your ChatGPT subscription, select an available judge model, and provide one review packet and its instructions. This uses subscription allowance and requires no Judge API configuration. Record the actual reviewer, model, and reasoning settings; exhausted subscription allowance does not trigger an API fallback.

Export anonymous review packets for the current batch's 57 single-stage tasks without model calls:

```sh
python3 -m benchmark review-batch --batch-dir ./runs/current/single \
  --out ./runs/current-reviews-single
```

Give each independent review session only the files inside one `current-reviews-single/packets/<random-id>/` directory; the organizer retains the top-level source mapping. Complete `judgment.json` and `review-settings.json` following the [review protocol](review-protocol.md), then validate and calculate scores:

```sh
python3 -m benchmark review-apply --batch-dir ./runs/current/single \
  --reviews ./runs/current-reviews-single --out ./runs/current-scored-single
python3 -m benchmark results-verify --batch-dir ./runs/current-scored-single
```

Review the 13 two-stage tasks individually with `python3 -m benchmark.longitudinal review/score`; both stages receive one task score. See the [native CLI guide](native-cli.md#codex订阅独立评审) for commands. There is currently no automated Codex subscription judge command or direct merger of these manual results into a complete 70-task HTML report. Single-stage results do not constitute 70-task results. The API workflow below supports automated review and reports for the full batch.

**Optional API judging.** `config/judge.example.json` is a configuration template. Its `https://api.example.com/v1/chat/completions` endpoint is a placeholder, not an active service or evidence of API charges. To choose this workflow, copy the template and set the actual `provider`, `model`, `expected_response_model`, `endpoint`, and compatible `parameters`. Supply the key through `BENCHMARK_JUDGE_API_KEY`, not the configuration file.

`expected_response_model` must match the identifier returned by the service; replace all template placeholders. Authentication errors or model mismatches stop execution without switching models or authentication channels.

```sh
cp config/judge.example.json config/judge.local.json
# Edit the actual service configuration and securely set BENCHMARK_JUDGE_API_KEY.

# Prepare requests without calling the API
python3 -m benchmark judge-batch --batch-dir ./runs/current \
  --config config/judge.local.json --out ./runs/current-judged

# Execute only after explicitly choosing API review; service billing rules apply
python3 -m benchmark judge-batch --batch-dir ./runs/current \
  --config config/judge.local.json --out ./runs/current-judged \
  --resume --execute
```

The judge evaluates each rubric; code validates answer quotations and calculates scores. Two-stage answers receive one combined task evaluation. The API judge has no external browsing tool, so insufficient evidence remains pending. Preparing templates is not completed review. See the [review protocol](review-protocol.md) for human review, disputes, and verification.

### 6. Open and verify the report

After execution, the API review workflow generates `runs/current-judged/leaderboard/index.html` and `leaderboard.json`. Open the HTML in a browser; the JSON contains task statuses, scores, completion, and aggregates. To validate and recompute saved judgments into a new report without model calls:

```sh
python3 -m benchmark leaderboard-export \
  --batch-dir ./runs/current-judged \
  --out ./runs/current-report
```

Open `runs/current-report/index.html`. **The denominator remains 70 tasks. A complete mean appears only when every expected repeat has a valid score.** Missing answers, failures, missing reviews, and pending judgments remain visible; successful tasks are not averaged alone. An incomplete report still writes HTML/JSON and returns exit code 1; validation errors return 2. This is a local experiment report, not automatic entry into an official leaderboard.

Report scope follows the selected task manifest. A single-task trial is not a current 70-task result. See the [native CLI guide](native-cli.md) for other execution workflows.
