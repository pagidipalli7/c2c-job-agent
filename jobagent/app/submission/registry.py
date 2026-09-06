from __future__ import annotations

from .ashby import AshbyAdapter
from .base import Adapter
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter
from .smartrecruiters import SmartRecruitersAdapter

_ADAPTERS: dict[str, Adapter] = {a.ats_type: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), SmartRecruitersAdapter())}


def get_adapter(ats_type: str) -> Adapter:
    if ats_type == "workday" and "workday" not in _ADAPTERS:
        from .workday import WorkdayAdapter

        _ADAPTERS["workday"] = WorkdayAdapter()
    if ats_type not in _ADAPTERS:
        raise KeyError(f"no submission adapter for {ats_type!r}")
    return _ADAPTERS[ats_type]


def register_adapter(adapter: Adapter) -> None:
    _ADAPTERS[adapter.ats_type] = adapter


def known_adapters() -> list[str]:
    return sorted(set(_ADAPTERS) | {"workday"})
