from pydantic import BaseModel, ConfigDict

SUPPORT_URL = "https://github.com/ktanino10/talkvideo-mcp/issues"


class Problem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    next_action: str
    needs_user_action: bool = False
    support_url: str = SUPPORT_URL


class TalkVideoError(Exception):
    """A safe, public error: never include provider bodies, scripts, or absolute paths."""

    def __init__(
        self,
        code: str,
        message: str,
        next_action: str,
        *,
        needs_user_action: bool = False,
    ) -> None:
        self.problem = Problem(
            code=code,
            message=message,
            next_action=next_action,
            needs_user_action=needs_user_action,
        )
        super().__init__(code)


def unavailable(component: str) -> TalkVideoError:
    return TalkVideoError(
        "production_unavailable",
        f"Production {component} is not implemented or authorized in this release.",
        "Use prepare/inspection only. Separately establish provider, voice, media and model "
        "rights and validate an implemented backend. Do not substitute another voice or service.",
        needs_user_action=True,
    )
