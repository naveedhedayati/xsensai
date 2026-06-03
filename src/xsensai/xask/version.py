"""Version constants for /xask bisect tracking.

PROMPT_TEMPLATE_VERSION bumps when the synthesis prompt or output template
changes. SERVICE_VERSION bumps when service.py orchestration semantics
change (re-rank logic, branch table, override vocabulary).

Both are logged on every /xask run so a future investigator can answer:
"why did the same question yield different output today?" Bump = template
or service moved. Constant = model behavior changed (or randomness).
"""

PROMPT_TEMPLATE_VERSION = "1.2.0"
SERVICE_VERSION = "1.0.0"
