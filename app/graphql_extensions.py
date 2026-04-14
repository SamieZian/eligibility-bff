"""Strawberry schema extensions + DataLoader wiring for the BFF.

Three concerns live here so `app/schema.py` stays focused on the GraphQL
surface:

1. ``ErrorEnvelopeExtension`` — maps exceptions thrown from resolvers onto the
   canonical envelope used everywhere else in the platform
   (``extensions.code``, ``extensions.retryable``, ``extensions.correlation_id``).
   See ``eligibility_common.errors``.
2. ``DepthLimitExtension`` — Strawberry 0.231 / graphql-core 3.2 ship no
   built-in depth-limit, so we register a custom AST validation rule
   (max depth = 8) via ``AddValidationRules``.
3. ``build_loaders`` / ``GroupAdminLoaders`` — DataLoader bootstrap so
   ``Query.group_admin`` no longer does N+1 HTTP calls against the group svc.
   Concurrency is bounded by an ``asyncio.Semaphore`` to protect the downstream.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import structlog
from eligibility_common.errors import AppError, InfraError
from eligibility_common.logging import get_logger
from graphql import GraphQLError
from graphql.language import FieldNode, FragmentDefinitionNode, FragmentSpreadNode, InlineFragmentNode, OperationDefinitionNode
from graphql.validation import ValidationContext, ValidationRule
from strawberry.dataloader import DataLoader
from strawberry.extensions import AddValidationRules, SchemaExtension

log = get_logger(__name__)


# ─────────────────────────── Error envelope ───────────────────────────────────


def _current_correlation_id() -> str:
    """Pull the correlation id out of structlog's contextvars (bound by the
    ``CorrelationIdMiddleware`` on every inbound request)."""
    try:
        ctx = structlog.contextvars.get_contextvars()
    except Exception:  # pragma: no cover — structlog always has this
        return ""
    return str(ctx.get("correlation_id", "") or "")


def _envelope_for(err: GraphQLError) -> dict[str, Any]:
    """Return the ``extensions`` dict for a single GraphQL error.

    * Known AppError subclasses → use their stable ``code`` / ``retryable``.
    * Anything else → generic INTERNAL_ERROR, scrub the message so we never
      leak stack traces / SQL fragments to the client.
    """
    cid = _current_correlation_id()
    original = err.original_error
    if isinstance(original, AppError):
        base = dict(err.extensions or {})
        base.update(
            {
                "code": original.code,
                "retryable": bool(original.retryable),
                "correlation_id": cid,
            }
        )
        if original.details:
            base.setdefault("details", original.details)
        return base

    # Unknown exception → opaque envelope, log the real thing server-side.
    # Use `log.error` (not `log.exception`) because we're not inside an
    # `except` block here — structlog's traceback processor would blow up
    # on ``sys.exc_info() == (None, None, None)``.
    log.error(
        "graphql.unhandled",
        error=str(original) if original else err.message,
        error_type=type(original).__name__ if original else "GraphQLError",
        path=[str(p) for p in (err.path or [])],
    )
    return {
        "code": "INTERNAL_ERROR",
        "retryable": False,
        "correlation_id": cid,
    }


class ErrorEnvelopeExtension(SchemaExtension):
    """Walk ``result.errors`` after execution and rewrite each one onto the
    standard envelope. Messages for unknown errors are replaced with
    ``"Internal error"`` to prevent leaking internals."""

    def on_operation(self):  # type: ignore[override]
        # Correlation id is bound by middleware before we ever run; this hook
        # is here so future code has a clean place to attach per-op context.
        yield None

    def on_execute(self):  # type: ignore[override]
        yield None
        result = self.execution_context.result
        if result is None or not getattr(result, "errors", None):
            return
        rewritten: list[GraphQLError] = []
        for err in result.errors:
            ext = _envelope_for(err)
            original = err.original_error
            message = err.message
            if not isinstance(original, AppError):
                message = "Internal error"
            rewritten.append(
                GraphQLError(
                    message=message,
                    nodes=err.nodes,
                    source=err.source,
                    positions=err.positions,
                    path=err.path,
                    original_error=original,
                    extensions=ext,
                )
            )
        result.errors = rewritten


# ─────────────────────────── Depth limit ──────────────────────────────────────


MAX_QUERY_DEPTH = 8


def _depth_limit_rule(max_depth: int) -> type[ValidationRule]:
    """Build an AST validation rule that rejects operations deeper than
    ``max_depth`` selection levels. Fragment spreads are inlined so queries
    can't hide depth behind named fragments."""

    class DepthLimitRule(ValidationRule):
        def __init__(self, context: ValidationContext) -> None:
            super().__init__(context)
            self._fragments: dict[str, FragmentDefinitionNode] = {
                defn.name.value: defn
                for defn in context.document.definitions
                if isinstance(defn, FragmentDefinitionNode)
            }

        def enter_operation_definition(
            self, node: OperationDefinitionNode, *_: Any
        ) -> None:
            depth = self._field_depth(node, seen_fragments=set())
            if depth > max_depth:
                op_name = node.name.value if node.name else "<anonymous>"
                self.report_error(
                    GraphQLError(
                        f"Query '{op_name}' exceeds maximum depth of {max_depth} (got {depth})",
                        nodes=[node],
                    )
                )

        def _field_depth(self, node: Any, seen_fragments: set[str]) -> int:
            selection_set = getattr(node, "selection_set", None)
            if selection_set is None:
                return 0
            max_child = 0
            for sel in selection_set.selections:
                if isinstance(sel, FieldNode):
                    max_child = max(max_child, 1 + self._field_depth(sel, seen_fragments))
                elif isinstance(sel, InlineFragmentNode):
                    max_child = max(max_child, self._field_depth(sel, seen_fragments))
                elif isinstance(sel, FragmentSpreadNode):
                    name = sel.name.value
                    if name in seen_fragments:
                        continue
                    frag = self._fragments.get(name)
                    if frag is None:
                        continue
                    max_child = max(
                        max_child,
                        self._field_depth(frag, seen_fragments | {name}),
                    )
            return max_child

    DepthLimitRule.__name__ = f"DepthLimit{max_depth}Rule"
    return DepthLimitRule


def depth_limit_extension(max_depth: int = MAX_QUERY_DEPTH) -> SchemaExtension:
    """Return a Strawberry extension instance that enforces the configured depth.

    Strawberry 0.231 / graphql-core 3.2 ship no built-in depth-limit rule, so
    we hand-roll an AST validation rule and plug it in via ``AddValidationRules``.
    """
    return AddValidationRules([_depth_limit_rule(max_depth)])


# ─────────────────────────── DataLoaders ──────────────────────────────────────


@dataclass
class GroupAdminLoaders:
    """Bundle of loaders used by ``Query.group_admin`` — one per downstream
    sub-resource. Keyed by employer_id so duplicate ids in one GraphQL request
    collapse to a single HTTP call."""

    subgroups: DataLoader[str, list[dict[str, Any]]]
    visible_plans: DataLoader[str, list[str]]


def build_loaders(
    group_client: Any, concurrency: int = 10
) -> GroupAdminLoaders:
    """Create per-request DataLoaders around the group service client.

    Without a batch endpoint on the group svc we still issue one HTTP call per
    unique employer_id, but:
    * duplicates in the same GraphQL request hit the loader cache instead of
      the network;
    * the semaphore caps simultaneous in-flight requests at ``concurrency`` so
      a 1,000-employer query cannot DOS the group svc.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(coro_factory: Callable[[], Any]) -> Any:
        async with sem:
            return await coro_factory()

    async def load_subgroups(keys: list[str]) -> list[list[dict[str, Any]]]:
        async def one(emp_id: str) -> list[dict[str, Any]]:
            try:
                r = await _bounded(
                    lambda: group_client.get(f"/employers/{emp_id}/subgroups")
                )
                r.raise_for_status()
                return list(r.json())
            except Exception as e:  # noqa: BLE001 — logged, swallowed
                log.warning("bff.loader.subgroups.error", employer_id=emp_id, error=str(e))
                return []

        return list(await asyncio.gather(*(one(k) for k in keys)))

    async def load_visible_plans(keys: list[str]) -> list[list[str]]:
        async def one(emp_id: str) -> list[str]:
            try:
                r = await _bounded(
                    lambda: group_client.get(f"/employers/{emp_id}/plans")
                )
                r.raise_for_status()
                return [str(pid) for pid in r.json().get("plan_ids", [])]
            except Exception as e:  # noqa: BLE001
                log.warning("bff.loader.plans.error", employer_id=emp_id, error=str(e))
                return []

        return list(await asyncio.gather(*(one(k) for k in keys)))

    return GroupAdminLoaders(
        subgroups=DataLoader(load_fn=load_subgroups),
        visible_plans=DataLoader(load_fn=load_visible_plans),
    )
