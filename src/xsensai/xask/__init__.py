"""x-sensai /xask orchestration package.

The slash command (commands/xask.md) is THIN — it invokes one Python
entrypoint here and renders the result. All orchestration (override
parsing, retrieval, web fork, deterministic re-rank, branch table,
question logging) lives in service.py per Eng review EC1.

See SLICE_3_PLAN_v2.2 for design rationale.
"""

from xsensai.xask.version import PROMPT_TEMPLATE_VERSION, SERVICE_VERSION

__all__ = ["PROMPT_TEMPLATE_VERSION", "SERVICE_VERSION"]
