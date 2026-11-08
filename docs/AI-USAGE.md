# AI Assistance and Attribution Log

Under the course policy on transparent AI usage (Section 8), this document records the AI engineering assistants employed during the development of RAHBAR, detailing specific components generated or shaped, post-generation modifications, and architectural reasoning.

| Tool | What It Wrote or Shaped | What We Changed Afterwards, and Why |
|---|---|---|
| **Antigravity AI Assistant (Google DeepMind)** | Initial Kubernetes manifest scaffolding (`Deployment`, `StatefulSet`, `NetworkPolicy`, `HPA`) and standard Prometheus metric boilerplate in `api/app/routes/metrics.py`. | Configured strict security constraints (`runAsNonRoot: true`, `readOnlyRootFilesystem: true`, dropping all capabilities, disabling ServiceAccount token automount). Customized readiness probe to check non-empty collection and embedding-model metadata pins. |
| **Antigravity AI Assistant (Google DeepMind)** | Algorithmic structure-aware chunking logic and sentence-boundary splitting regex in `indexer/pipeline/chunk.py`. | Refined heading detection patterns to support both Markdown headings (`#`) and FAST-NUCES clause notations (`1.21`, `5.4`). Tuned sliding overlap buffer to maintain consistent sentence context. |
| **Antigravity AI Assistant (Google DeepMind)** | Evaluation runner calculation formulas (`recall_at_k`, `citation_validity`, `summarise`, `gate`) in `eval/run_eval.py`. | Added substring matching tolerance for section headers and enforced a strict 100% threshold on must-abstain questions to eliminate hallucinated answers on missing corpus topics. |
| **Antigravity AI Assistant (Google DeepMind)** | Initial draft of Architecture Decision Records (`0001` to `0004`). | Tailored trade-off discussions and options considered to specifically match the FAST-NUCES case study constraints and Kubernetes resource profiles. |
