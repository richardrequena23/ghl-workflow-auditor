"""ghlaudit — static analysis for GoHighLevel workflows."""

from .model import Account, Workflow, Step, Trigger
from .rules import RULES, Finding, run
from .report import as_json, as_markdown, as_text

__version__ = "0.1.0"
__all__ = ["Account", "Workflow", "Step", "Trigger", "RULES", "Finding", "run",
           "as_text", "as_markdown", "as_json"]
