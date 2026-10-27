<div align="center">
<h1><a href="https://arxiv.org/abs/2610.34467">DynaFlow: Learning Free-Form Multi-Agent Orchestration</a></h1>
<p>Ruhao Liu&emsp;Qi Li&emsp;Shizun Wang&emsp;Xinchao Wang</p>
<p><a href="https://arxiv.org/abs/2610.34467">论文</a> · <a href="https://amiya-special.github.io/DynaFlow-website/zh.html">项目主页</a> · <a href="https://huggingface.co/papers/2610.34467">Hugging Face</a></p>
</div>

[English](README.md) | 简体中文

DynaFlow 支持模型直接推理和模型生成的智能体工作流运行。

| 模式 | 行为 | 配置 |
|---|---|---|
| `direct` | 主模型直接回答，不使用工具 | [direct.json](configs/direct.json) |
| `dynamic` | 主模型自主选择直接回答或调用 Workflow | [dynamic.json](configs/dynamic.json) |
| `forced` | 主模型必须完成 Workflow，并取得子代理的有效交付 | [forced.json](configs/forced.json) |

## 环境要求

- Python 3.11+。
- `dynamic` 和 `forced` 另需 Node.js 24+、npm，以及支持 Linux 容器的 Docker。
- 兼容 OpenAI Chat Completions 的流式 API，需返回结束原因和用量信息。Workflow 模型需要支持工具调用。

## 快速开始

以下所有命令均在项目根目录执行。

### 1. 配置 API

复制 [.env.example](.env.example) 为 `.env`，填写实际的 API 地址、模型名称和密钥：

```dotenv
DYNAFLOW_BASE_URL=https://your-api-host/v1
DYNAFLOW_MODEL=your-model-name
DYNAFLOW_MODEL_ADAPTER=openai
DYNAFLOW_API_KEY=
```

DeepSeek 使用 `deepseek` 适配器。CLI 自动加载 `.env`，已有进程环境变量优先。

`dynamic` 和 `forced` 还需按照 `.env.example` 中的注释配置搜索与网页提取服务的凭证。demo 启动前会检查这些服务，即使 `dynamic` 最后直接回答也需要配置。子代理和页面摘要默认复用主模型 API。

### 2. 构建 Workflow 环境

`dynamic` 和 `forced` 需要此步骤，`direct` 可跳过。启动 Docker 并启用 Linux 容器，然后执行：

```bash
docker info
npm --prefix workflow_runtime ci --ignore-scripts
npm --prefix workflow_runtime run build
docker pull node:24.14.0-bookworm-slim
docker build --file tool_runtime/Dockerfile --tag dynaflow-tools:py311-v1 .
```

Docker 会按 [requirements.txt](requirements.txt) 安装 Workflow 工具所需的 Python 计算库。三种模式的宿主程序均使用 Python 标准库，无需在本机额外执行 `pip install`。

Windows PowerShell 若限制 `npm` 执行，可改用 `npm.cmd`；仓库和输出目录建议使用较短路径。

### 3. 运行示例

选择一种模式：

```bash
python scripts/dynaflow.py demo --mode direct
python scripts/dynaflow.py demo --mode dynamic
python scripts/dynaflow.py demo --mode forced
```

每条命令通过真实 API 运行 [examples/demo/questions.json](examples/demo/questions.json) 中的两个题目，无需额外数据集。运行成功表示所有题目均完成有效的答案交付，不对答案正确性评分。

结果保存在 `runs/demo-<mode>/`：

| 路径 | 内容 |
|---|---|
| `results/` | 模型回答 |
| `calls/` | API 调用记录 |
| `comparison/` | 运行汇总、token 用量和 CSV 格式的答案 |
| `workflows/`、`tools/` | 调用 Workflow 和工具时生成的执行记录 |

再次运行时，用 `--out runs/my-demo` 指定新的目录。

## 引用

如果本研究或代码对你有所帮助，欢迎引用我们的论文：

```bibtex
@article{liu2026dynaflow,
  title={DynaFlow: Learning Free-Form Multi-Agent Orchestration},
  author={Liu, Ruhao and Li, Qi and Wang, Shizun and Wang, Xinchao},
  journal={arXiv preprint arXiv:2610.34467},
  year={2026}
}
```

## 许可证

DynaFlow 的原创代码采用 [MIT License](LICENSE)。

`workflow_runtime/src` 中的工作流引擎保留原有的 [MIT 许可证和版权声明](workflow_runtime/src/LICENSE)。
