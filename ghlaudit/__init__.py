"""ghlaudit — static analysis for GoHighLevel workflows."""

from .config import AuditConfig
from .model import Account, Inventory, Workflow, Step, Trigger
from .rules import RULES, Finding, Skip, run, run_all
from .report import as_html, as_json, as_markdown, as_text
from .score import HealthScore, health

__version__ = "0.2.0"
__all__ = ["Account", "AuditConfig", "Inventory", "Workflow", "Step", "Trigger",
           "RULES", "Finding", "Skip", "run", "run_all", "health", "HealthScore",
           "as_text", "as_markdown", "as_json", "as_html"]
