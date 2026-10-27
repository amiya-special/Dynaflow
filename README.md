<div align="center">
<h1><a href="https://arxiv.org/abs/2610.34467">DynaFlow: Learning Free-Form Multi-Agent Orchestration</a></h1>
<p>Ruhao Liu&emsp;Qi Li&emsp;Shizun Wang&emsp;Xinchao Wang</p>
<p><a href="https://arxiv.org/abs/2610.34467">Paper</a> · <a href="https://amiya-special.github.io/DynaFlow-website/">Project Page</a> · <a href="https://huggingface.co/papers/2610.34467">Hugging Face</a></p>
</div>

English | [简体中文](README.zh-CN.md)

DynaFlow supports direct model reasoning and model-generated agent workflows.

| Mode | Behavior | Configuration |
|---|---|---|
| `direct` | The main model answers without tools. | [direct.json](configs/direct.json) |
| `dynamic` | The main model chooses a direct answer or a Workflow. | [dynamic.json](configs/dynamic.json) |
| `forced` | The main model must complete a Workflow with successful subagent delivery. | [forced.json](configs/forced.json) |

## Requirements

- Python 3.11+.
- For `dynamic` and `forced`: Node.js 24+, npm, and Docker with Linux containers.
- An OpenAI-compatible streaming Chat Completions API that returns finish reasons and usage. Workflow models must support tool calls.

## Quick start

Run all commands from the project root.

### 1. Configure the APIs

Copy [.env.example](.env.example) to `.env` and fill in your API URL, model name, and key:

```dotenv
DYNAFLOW_BASE_URL=https://your-api-host/v1
DYNAFLOW_MODEL=your-model-name
DYNAFLOW_MODEL_ADAPTER=openai
DYNAFLOW_API_KEY=
```

Use adapter `deepseek` for DeepSeek. The CLI loads `.env` automatically; existing environment variables take precedence.

For `dynamic` and `forced`, also configure the web search and page retrieval credentials described in `.env.example`. These services are checked before the demo starts, even if `dynamic` later answers directly. Subagents and page summaries reuse the main model API by default.

### 2. Build the Workflow environment

Required for `dynamic` and `forced`; skip this step for `direct`. Start Docker with Linux containers, then run:

```bash
docker info
npm --prefix workflow_runtime ci --ignore-scripts
npm --prefix workflow_runtime run build
docker pull node:24.14.0-bookworm-slim
docker build --file tool_runtime/Dockerfile --tag dynaflow-tools:py311-v1 .
```

Docker installs the Workflow tools' Python calculation libraries from [requirements.txt](requirements.txt). The host program uses the Python standard library for all three modes; no additional `pip install` is needed on your machine.

On Windows, use `npm.cmd` if PowerShell blocks `npm`, and keep checkout and output paths short.

### 3. Run a demo

Choose a mode:

```bash
python scripts/dynaflow.py demo --mode direct
python scripts/dynaflow.py demo --mode dynamic
python scripts/dynaflow.py demo --mode forced
```

Each command runs the two questions in [examples/demo/questions.json](examples/demo/questions.json) using real APIs. No external dataset is needed. A successful run means all questions completed with valid answer delivery; answer correctness is not scored.

Results are saved under `runs/demo-<mode>/`:

| Path | Contents |
|---|---|
| `results/` | Model answers |
| `calls/` | API call records |
| `comparison/` | Run summary, token usage totals, and answers in CSV format |
| `workflows/`, `tools/` | Workflow and tool execution records, when used |

To run again, select a new directory with `--out runs/my-demo`.

## Citation

If you find our work or this codebase helpful, please cite our paper:

```bibtex
@article{liu2026dynaflow,
  title={DynaFlow: Learning Free-Form Multi-Agent Orchestration},
  author={Liu, Ruhao and Li, Qi and Wang, Shizun and Wang, Xinchao},
  journal={arXiv preprint arXiv:2610.34467},
  year={2026}
}
```

## License

DynaFlow's original code is licensed under the [MIT License](LICENSE).

The workflow engine in `workflow_runtime/src` retains its original [MIT license and copyright notice](workflow_runtime/src/LICENSE).
