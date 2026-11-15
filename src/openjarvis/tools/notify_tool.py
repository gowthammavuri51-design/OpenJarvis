"""``notify_user`` — lets the assistant text or call its owner."""

from __future__ import annotations

from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

_TOOL_NAME = "notify_user"


@ToolRegistry.register(_TOOL_NAME)
class NotifyUserTool(BaseTool):
    """Reach the owner on their personal phone.

    Only ever contacts the configured ``[notify].owner_phone`` — the model
    cannot choose a destination number.
    """

    tool_id = _TOOL_NAME
    is_local = False

    def __init__(self, notifier: Optional[Any] = None) -> None:
        self._notifier = notifier

    def _get(self) -> Any:
        if self._notifier is None:
            from openjarvis.notify import build_default_notifier

            self._notifier = build_default_notifier()
        return self._notifier

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=_TOOL_NAME,
            description=(
                "Notify the user on their personal phone. Choose urgency "
                "carefully: 'low' = include in the evening summary text; "
                "'normal' = send a text now; 'high' = text and phone call; "
                "'critical' = call repeatedly until the user acknowledges "
                "(only for real emergencies, security problems or something "
                "that will be lost/missed within the hour)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short headline, e.g. 'Meeting in 10 minutes'.",
                    },
                    "message": {
                        "type": "string",
                        "description": "One to three plain sentences of detail.",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "normal", "high", "critical"],
                        "description": "How urgently to reach the user.",
                    },
                    "dedupe_key": {
                        "type": "string",
                        "description": (
                            "Optional stable id so the same problem is not "
                            "reported twice within a few hours."
                        ),
                    },
                },
                "required": ["title", "urgency"],
            },
            category="communication",
            timeout_seconds=30.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        title = str(params.get("title") or "").strip()
        if not title:
            return ToolResult(_TOOL_NAME, "A title is required.", success=False)
        try:
            notifier = self._get()
        except Exception as exc:
            return ToolResult(_TOOL_NAME, f"Notifier unavailable: {exc}", success=False)
        problems = notifier.cfg.problems()
        if problems:
            return ToolResult(
                _TOOL_NAME,
                "Phone notifications are not set up: " + "; ".join(problems),
                success=False,
            )
        try:
            result = notifier.notify(
                title,
                str(params.get("message") or ""),
                urgency=str(params.get("urgency") or "normal"),
                dedupe_key=str(params.get("dedupe_key") or ""),
            )
        except ValueError as exc:
            return ToolResult(_TOOL_NAME, str(exc), success=False)
        text = {
            "called": "Called and texted the user.",
            "texted": "Texted the user.",
            "held": f"Will text the user later ({result.detail}).",
            "digest": "Added to the user's daily summary text.",
            "suppressed": "Already notified the user about this recently.",
            "failed": "Could not reach the user's phone (Twilio error).",
        }.get(result.action, result.action)
        return ToolResult(
            _TOOL_NAME,
            text,
            success=result.action != "failed",
            metadata={"alert_id": result.alert_id, "action": result.action},
        )
