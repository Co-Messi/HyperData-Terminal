"""Resolve ``hyperdata paper --strategy`` arguments into Strategy instances.

A spec is one of:

* a built-in name: ``cvd_momentum``, ``funding_rate_arb``,
  ``liquidation_cascade``, ``whale_follow``, ``llm_agent``;
* a path to a ``.py`` file: every concrete Strategy subclass *defined in*
  that file is instantiated with no arguments, so your strategy can live
  anywhere, not inside the installed package;
* ``module:ClassName`` for a strategy on the import path.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path

from .base import Strategy


class StrategyLoadError(ValueError):
    """A --strategy spec that cannot be turned into a strategy."""


def _builtins() -> dict[str, type[Strategy]]:
    from .examples import CVDMomentum, FundingRateArb, LiquidationCascade, WhaleFollow
    from .llm_agent import LLMAgent

    return {
        "cvd_momentum": CVDMomentum,
        "funding_rate_arb": FundingRateArb,
        "liquidation_cascade": LiquidationCascade,
        "whale_follow": WhaleFollow,
        "llm_agent": LLMAgent,
    }


BUILTIN_NAMES = ("cvd_momentum", "funding_rate_arb", "liquidation_cascade", "whale_follow", "llm_agent")


def _is_concrete_strategy(obj: object) -> bool:
    return inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy and not inspect.isabstract(obj)


def _from_file(path: Path) -> list[Strategy]:
    if not path.is_file():
        raise StrategyLoadError(f"strategy file not found: {path}")
    module_name = f"hyperdata_user_strategy_{path.stem}_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise StrategyLoadError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise StrategyLoadError(f"{path} failed to import: {exc!r}") from exc

    classes = [
        obj for obj in vars(module).values()
        if _is_concrete_strategy(obj) and obj.__module__ == module_name
    ]
    if not classes:
        raise StrategyLoadError(f"{path} defines no Strategy subclass with evaluate() implemented")
    return [cls() for cls in classes]


def _from_import_path(spec: str) -> list[Strategy]:
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise StrategyLoadError(f"cannot import module {module_name!r}: {exc}") from exc
    obj = getattr(module, attr, None)
    if not _is_concrete_strategy(obj):
        raise StrategyLoadError(f"{spec} is not a concrete Strategy subclass")
    return [obj()]


def load_strategies(specs: list[str]) -> list[Strategy]:
    """Turn CLI specs into strategy instances, in order, without duplicates by name."""
    builtins = _builtins()
    strategies: list[Strategy] = []
    for spec in specs:
        if spec in builtins:
            strategies.append(builtins[spec]())
        elif spec.endswith(".py") or Path(spec).is_file():
            strategies.extend(_from_file(Path(spec).expanduser()))
        elif ":" in spec:
            strategies.extend(_from_import_path(spec))
        else:
            raise StrategyLoadError(
                f"unknown strategy {spec!r}. Built-ins: {', '.join(BUILTIN_NAMES)}; "
                "or pass a path to your own .py file, or module:ClassName"
            )
    seen: set[str] = set()
    unique: list[Strategy] = []
    for s in strategies:
        if s.name not in seen:
            seen.add(s.name)
            unique.append(s)
    return unique
