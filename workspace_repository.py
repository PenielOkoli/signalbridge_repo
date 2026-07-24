"""
Database-backed workspace membership and OAuth identity repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import OAuthIdentity, User, Workspace, WorkspaceMembership, WorkspaceRole
from google_oauth import GoogleIdentityProfile


@dataclass(slots=True)
class WorkspacePrincipal:
    user: User
    workspace: Workspace
    membership: WorkspaceMembership
    identity: OAuthIdentity
    created_workspace: bool = False


class WorkspaceRepository:
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self.session_maker = session_maker

    async def resolve_google_identity(self, profile: GoogleIdentityProfile) -> WorkspacePrincipal:
        async with self.session_maker() as session:
            async with session.begin():
                identity = await self._get_identity(session, "google", profile.subject)
                if identity is not None:
                    return await self._hydrate_principal(session, identity)

                user = await self._get_user_by_email(session, profile.email)
                created_workspace = False
                if user is None:
                    user = User(
                        email=profile.email,
                        name=profile.name,
                        password_hash=None,
                        is_disabled=False,
                    )
                    session.add(user)
                    await session.flush()

                workspace = await self._get_primary_workspace_for_user(session, user.id)
                if workspace is None:
                    workspace = Workspace(
                        slug=self._build_workspace_slug(profile.email, user.id),
                        name=f"{profile.name}'s workspace",
                    )
                    session.add(workspace)
                    await session.flush()
                    created_workspace = True

                membership = await self._get_membership(session, workspace.id, user.id)
                if membership is None:
                    membership = WorkspaceMembership(
                        workspace_id=workspace.id,
                        user_id=user.id,
                        role=WorkspaceRole.OWNER if created_workspace else WorkspaceRole.OPERATOR,
                        invited_email=profile.email,
                        accepted_at=datetime.now(timezone.utc),
                    )
                    session.add(membership)
                    await session.flush()

                identity = OAuthIdentity(
                    provider="google",
                    provider_subject=profile.subject,
                    user_id=user.id,
                    workspace_id=workspace.id,
                    email=profile.email,
                    display_name=profile.name,
                    email_verified=profile.email_verified,
                    last_login_at=datetime.now(timezone.utc),
                )
                session.add(identity)
                await session.flush()
                return WorkspacePrincipal(user=user, workspace=workspace, membership=membership, identity=identity, created_workspace=created_workspace)

    async def workspace_id_for_user(self, user_id: str) -> str | None:
        async with self.session_maker() as session:
            result = await session.execute(
                select(WorkspaceMembership.workspace_id)
                .where(WorkspaceMembership.user_id == user_id)
                .order_by(WorkspaceMembership.created_at.asc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def _get_identity(self, session: AsyncSession, provider: str, provider_subject: str) -> OAuthIdentity | None:
        result = await session.execute(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == provider,
                OAuthIdentity.provider_subject == provider_subject,
            )
        )
        return result.scalar_one_or_none()

    async def _hydrate_principal(self, session: AsyncSession, identity: OAuthIdentity) -> WorkspacePrincipal:
        workspace = await session.get(Workspace, identity.workspace_id)
        user = await session.get(User, identity.user_id)
        membership = await self._get_membership(session, identity.workspace_id, identity.user_id)
        if workspace is None or user is None or membership is None:
            raise RuntimeError("Google OAuth identity points to a missing workspace membership")
        identity.last_login_at = datetime.now(timezone.utc)
        return WorkspacePrincipal(user=user, workspace=workspace, membership=membership, identity=identity)

    async def _get_user_by_email(self, session: AsyncSession, email: str) -> User | None:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def _get_primary_workspace_for_user(self, session: AsyncSession, user_id: str) -> Workspace | None:
        result = await session.execute(
            select(Workspace)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(WorkspaceMembership.user_id == user_id)
            .order_by(WorkspaceMembership.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_membership(self, session: AsyncSession, workspace_id: str, user_id: str) -> WorkspaceMembership | None:
        result = await session.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    def _build_workspace_slug(self, email: str, user_id: str) -> str:
        base = email.split("@")[0].lower().replace(".", "-").replace("_", "-")
        return f"{base}-{user_id[:8]}"
