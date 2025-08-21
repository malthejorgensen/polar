"""Authentication requirements for time travel endpoints."""

from typing import Annotated

from fastapi import Depends, HTTPException

from polar.auth.dependencies import Authenticator
from polar.auth.models import AuthSubject, User
from polar.auth.scope import Scope

_AdminAuthenticator = Authenticator(
    allowed_subjects={User}, required_scopes={Scope.web_write, Scope.web_read}
)


async def require_admin_user(
    auth_subject: Annotated[AuthSubject[User], Depends(_AdminAuthenticator)],
) -> AuthSubject[User]:
    """Check if user is admin."""
    if not auth_subject.subject.is_admin:
        raise HTTPException(
            status_code=403, detail="Admin access required for time travel operations"
        )
    return auth_subject


AdminUser = Annotated[AuthSubject[User], Depends(require_admin_user)]
