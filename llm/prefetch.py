"""Concurrent prefetch of the generative stages (OPUS_REVIEW.md bug 2).

Streamlit runs every tab body on each rerun, which used to fire ~7 serial Gemini
calls. ``prefetch`` warms the same stage results once, in a thread pool, so the
tabs hit the stage cache instead of the network.

Contract:
- ``prefetch`` never raises. Every failed stage is listed in ``errors`` and its
  result key is left out (stages already return fallbacks, so this is a guard).
- A global deadline (``timeout_s``) bounds the whole call. Work still needed for
  the internal note runs first; advisories come after, so a slow advisory can
  never starve the note.
- ``llm.stages`` is imported lazily inside ``_calls()``, the single place that
  names the stage functions and their argument order.

Thread safety found while writing this: the stage functions share no mutable
state. ``stages.load_prompt`` is an ``lru_cache`` (thread safe in CPython), the
fallback helpers are pure, and ``demo_cache`` writes one file per key, so each
job below has a distinct key. ``llm.client`` keeps the circuit breaker in two
module globals, guarded by a lock. The only shared-write risk is two jobs with
the same cache key, which is why personas/languages/levels are de-duplicated.
"""
from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable, Hashable, Sequence

from core.models import Brief, DiffResult

DEFAULT_MAX_WORKERS = 6
DEFAULT_TIMEOUT_S = 45.0

Job = tuple[Hashable, str, Callable[..., Any], tuple[Any, ...]]


def _calls() -> dict[str, Callable[..., Any]]:
    """The one place that names stage functions and their argument order.

    Keep the call sites here: if a stage signature changes, only this function
    and the calls made from ``prefetch`` below need to change.
    """
    from llm.stages import (  # local import: keeps this module importable offline
        interpret,
        plan_actions,
        write_advisory,
        write_internal_note,
    )

    return {
        "interpret": interpret,
        "plan_actions": plan_actions,
        "write_internal_note": write_internal_note,
        "write_advisory": write_advisory,
    }


def prefetch(
    brief: Brief,
    diff: DiffResult,
    personas: Sequence[str],
    languages: Sequence[str],
    levels: Sequence[str],
    area_label: str,
    max_workers: int = DEFAULT_MAX_WORKERS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Warm interpret, plan_actions, write_internal_note and write_advisory.

    Returns ``{'interpretation', 'plans', 'note', 'advisories', 'errors',
    'elapsed_s'}``; ``plans`` is keyed by persona, ``advisories`` by
    ``(language, level)``. ``note`` uses the first persona's plan.
    """
    started = time.monotonic()
    deadline = started + max(float(timeout_s), 0.0)
    errors: list[str] = []
    result: dict[str, Any] = {
        "interpretation": None,
        "plans": {},
        "note": None,
        "advisories": {},
        "errors": errors,
        "elapsed_s": 0.0,
    }
    try:
        calls = _calls()
        persona_list = list(dict.fromkeys(str(item) for item in personas))
        language_list = list(dict.fromkeys(str(item) for item in languages))
        level_list = list(dict.fromkeys(str(item) for item in levels))

        pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
        try:
            first: list[Job] = [("interpret", "interpret", calls["interpret"], (brief, diff))]
            first += [
                (("plan", persona), f"plan_actions[{persona}]", calls["plan_actions"], (brief, diff, persona))
                for persona in persona_list
            ]
            warm = _run(pool, first, deadline, errors)
            result["interpretation"] = warm.get("interpret")
            for persona in persona_list:
                plan = warm.get(("plan", persona))
                if plan is not None:
                    result["plans"][persona] = plan

            interpretation = result["interpretation"]
            anchor = result["plans"].get(persona_list[0]) if persona_list else None
            follow: list[Job] = []
            if interpretation is not None and anchor is not None:
                follow.append(
                    ("note", "write_internal_note", calls["write_internal_note"],
                     (brief, diff, interpretation, anchor))
                )
            follow += [
                (("advisory", language, level), f"write_advisory[{language}/{level}]",
                 calls["write_advisory"], (brief, language, level, area_label))
                for language in language_list
                for level in level_list
            ]
            warm = _run(pool, follow, deadline, errors)
            result["note"] = warm.get("note")
            for language in language_list:
                for level in level_list:
                    advisory = warm.get(("advisory", language, level))
                    if advisory is not None:
                        result["advisories"][(language, level)] = advisory
        finally:
            # Do not block past the deadline on a stuck SDK call; the client's own
            # request timeout bounds how long an abandoned worker can live.
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:  # noqa: BLE001 - prefetch must never raise
        errors.append(f"prefetch: {type(exc).__name__}: {exc}")
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    return result


def _run(
    pool: ThreadPoolExecutor,
    jobs: Sequence[Job],
    deadline: float,
    errors: list[str],
) -> dict[Hashable, Any]:
    """Run the jobs in the pool, wait no later than the deadline, collect results."""
    if not jobs:
        return {}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        errors.extend(f"{label}: skipped, prefetch deadline reached" for _, label, _, _ in jobs)
        return {}

    futures: dict[Hashable, Future] = {}
    labels: dict[Hashable, str] = {}
    for key, label, fn, args in jobs:
        futures[key] = pool.submit(fn, *args)
        labels[key] = label

    _done, pending = wait(list(futures.values()), timeout=remaining)
    results: dict[Hashable, Any] = {}
    for key, future in futures.items():
        if future in pending:
            future.cancel()
            errors.append(f"{labels[key]}: timed out")
            continue
        try:
            results[key] = future.result()
        except Exception as exc:  # noqa: BLE001 - one stage failing must not stop the rest
            errors.append(f"{labels[key]}: {type(exc).__name__}: {exc}")
    return results
