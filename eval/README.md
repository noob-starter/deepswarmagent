# Offline evaluation harness

This directory holds a **repeatable methodology** for comparing the multi-agent
swarm against a single LLM baseline. It is intentionally lightweight:
adjust scoring (`run_eval.py`) to match your reviewer rubric.

## Files

- `questions.sample.jsonl` — tiny public example (replace with your private 50–200Q set).
- `run_eval.py` — orchestrates calls into the running API + optional baseline.

## Prerequisite

- API running locally (`docker compose up`) with `DATABASE_URL` configured.
- For automated runs, export `EVAL_API_BASE=http://localhost:8000` (default).

## Running

```bash
cd backend
source .venv/bin/activate
pip install httpx  # if not already installed
python ../eval/run_eval.py --questions ../eval/questions.sample.jsonl --out ../eval/results.jsonl
```

`results.jsonl` contains one JSON object per question with fields:

- `question`
- `swarm_session_id` / `swarm_status`
- `baseline_answer` (if `--baseline-cmd` provided or stubbed)

## Methodology notes

Document in your README / paper draft:

1. **Dataset construction** — domain mix, difficulty, time-sensitive vs timeless.
2. **Grading** — human rubric, LLM-as-judge with frozen prompt, or keyword overlap.
3. **Leakage controls** — held-out questions, no training on eval.

The codebase does **not** ship a 50-question gold set (licensing); copy the format
in `questions.sample.jsonl` and add your own items.
