"""Synthesis package — output template + validator + injection fixtures helper.

The synthesis itself happens in the host Claude Code session (not here) per
the Slice 3 CEO reshape. This package provides the structural pieces the
slash command and tests use:

- template.py: locked output template constant + validate() function +
  `python -m xsensai.synthesis.template validate` CLI entrypoint
- injection_fixtures.py: helpers for loading the adversarial fixture corpus
"""

from xsensai.synthesis.template import (
    OUTPUT_TEMPLATE,
    HARD_RULES,
    TemplateValidationResult,
    validate,
)

__all__ = [
    "OUTPUT_TEMPLATE",
    "HARD_RULES",
    "TemplateValidationResult",
    "validate",
]
