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


def _instantiate(cls: type[Strategy], origin: str) -> Strategy:
    try:
        return cls()
    except TypeError as exc:
        raise StrategyLoadError(
            f"{origin}: {cls.__name__}() must be constructible with no arguments ({exc})"
        ) from exc


def _ensure_on_path(directory: Path) -> None:
    entry = str(directory)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _strategy_classes_in_file(path: Path) -> list[type[Strategy]]:
    if not path.is_file():
        raise StrategyLoadError(f"strategy file not found: {path}")
    resolved = path.resolve()
    module_name = f"hyperdata_user_strategy_{resolved.stem}_{abs(hash(str(resolved)))}"
    cached = sys.modules.get(module_name)
    if cached is None:
        spec = importlib.util.spec_from_file_location(module_name, resolved)
        if spec is None or spec.loader is None:
            raise StrategyLoadError(f"cannot import {path}")
        # Let the strategy import helper modules that sit next to it.
        _ensure_on_path(resolved.parent)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise StrategyLoadError(f"{path} failed to import: {exc!r}") from exc
        cached = module

    defined = [
        obj for obj in vars(cached).values()
        if inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy
        and obj.__module__ == module_name
    ]
    classes = [c for c in defined if not inspect.isabstract(c)]
    if not classes:
        if defined:
            missing = ", ".join(sorted(defined[0].__abstractmethods__))
            raise StrategyLoadError(f"{path}: {defined[0].__name__} is missing {missing}")
        raise StrategyLoadError(f"{path} defines no Strategy subclass")
    return classes


def _from_import_path(spec: str) -> list[Strategy]:
    module_name, _, attr = spec.partition(":")
    _ensure_on_path(Path.cwd())  # the console script does not put the working directory on sys.path
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise StrategyLoadError(f"cannot import module {module_name!r}: {exc}") from exc
    obj = getattr(module, attr, None)
    if not _is_concrete_strategy(obj):
        raise StrategyLoadError(f"{spec} is not a concrete Strategy subclass")
    return [_instantiate(obj, spec)]


def load_strategies(specs: list[str]) -> list[Strategy]:
    """Turn CLI specs into strategy instances, in order.

    Repeating a spec loads it once. Two *different* strategies with the same
    name are an error: the paper trader logs and reports by name.
    """
    builtins = _builtins()
    strategies: list[Strategy] = []
    for spec in specs:
        if spec in builtins:
            strategies.append(_instantiate(builtins[spec], spec))
        elif spec.endswith(".py") or Path(spec).is_file():
            strategies.extend(_instantiate(c, spec) for c in _strategy_classes_in_file(Path(spec).expanduser()))
        elif ":" in spec:
            strategies.extend(_from_import_path(spec))
        else:
            raise StrategyLoadError(
                f"unknown strategy {spec!r}. Built-ins: {', '.join(BUILTIN_NAMES)}; "
                "or pass a path to your own .py file, or module:ClassName"
            )
    by_name: dict[str, Strategy] = {}
    unique: list[Strategy] = []
    for s in strategies:
        existing = by_name.get(s.name)
        if existing is None:
            by_name[s.name] = s
            unique.append(s)
        elif type(existing) is not type(s):
            raise StrategyLoadError(
                f"two different strategies are named {s.name!r} "
                f"({type(existing).__module__}.{type(existing).__name__} and "
                f"{type(s).__module__}.{type(s).__name__}); rename one"
            )
    return unique
