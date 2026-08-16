"""Jarvis — a personal chief-of-staff agent.

Public surface:
  * :class:`~jarvis.models.Item` — the unit of memory.
  * :class:`~jarvis.storage.JSONStore` / :class:`~jarvis.storage.Store` — persistence.
  * :func:`~jarvis.agenda.build_agenda` — overdue/today/upcoming from dated items.
  * :func:`~jarvis.orchestrator.build_orchestrator_options` — the orchestrator config.
"""

from .models import Item, KINDS, STATUSES, format_item
from .storage import JSONStore, Store, StoreError
from .agenda import Agenda, build_agenda, render_agenda
from .dumps import DumpLog, DumpLogError, DumpRecord
from .registry import AgentRecord, AgentRegistry, RegistryError
from .orchestrator import available_agents, build_orchestrator_options

__all__ = [
    "Item",
    "KINDS",
    "STATUSES",
    "format_item",
    "JSONStore",
    "Store",
    "StoreError",
    "Agenda",
    "build_agenda",
    "render_agenda",
    "DumpLog",
    "DumpLogError",
    "DumpRecord",
    "AgentRecord",
    "AgentRegistry",
    "RegistryError",
    "available_agents",
    "build_orchestrator_options",
]
