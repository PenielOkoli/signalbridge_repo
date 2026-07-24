"""
Workspace resolution helpers.

These helpers deliberately derive the workspace from authenticated identity
claims instead of accepting a browser-supplied workspace_id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from auth_manager import SessionClaims


class WorkspaceLookup(Protocol):
    async def workspace_id_for_user(self, user_id: str) -> str | None:
        """Return the active workspace for a user, or None if not assigned."""


@dataclass(slots=True)
class WorkspaceContext:
    workspace_id: str
    user_id: str
    email: str
    name: str


class WorkspaceResolutionError(RuntimeError):
    """Raised when an authenticated session cannot be mapped to a workspace."""


async def resolve_workspace_context(
    claims: SessionClaims,
    lookup: WorkspaceLookup | None = None,
) -> WorkspaceContext:
    """Resolve the active workspace from the authenticated session.

    The browser never supplies a workspace ID. If the session already carries a
    workspace_id, use it. Otherwise, ask the lookup layer for the user's active
    workspace membership.
    """

    if claims.workspace_id:
        return WorkspaceContext(
            workspace_id=claims.workspace_id,
            user_id=claims.sub,
            email=claims.email,
            name=claims.name,
        )

    if lookup is None:
        raise WorkspaceResolutionError("authenticated session is not bound to a workspace")

    workspace_id = await lookup.workspace_id_for_user(claims.sub)
    if not workspace_id:
        raise WorkspaceResolutionError("no workspace membership was found for the authenticated session")

    return WorkspaceContext(
        workspace_id=workspace_id,
        user_id=claims.sub,
        email=claims.email,
        name=claims.name,
    )
