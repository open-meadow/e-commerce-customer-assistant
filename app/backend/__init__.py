"""Backend package. The frontend imports from here (docs/03 Section 4)."""

from app.backend.api import (Backend, check_backend, end_session, get_backend, handle_message,
                             list_login_options, set_backend, start_session)
from app.backend.config import Settings
from app.llm.base import LLMConfigError
from app.backend.types import (BackendHealth, ChatReply, ChoiceOption, ChoiceSelection, ChoiceView,
                               Citation, ConfirmationDecision, ConversationState, LoginOption,
                               PendingView, StartSessionResult)

__all__ = [
    "Backend", "BackendHealth", "LLMConfigError", "ChatReply", "ChoiceOption", "ChoiceSelection", "ChoiceView", "Citation",
    "ConfirmationDecision", "ConversationState", "LoginOption", "PendingView", "Settings", "StartSessionResult",
    "check_backend", "end_session", "get_backend", "handle_message", "list_login_options", "set_backend",
    "start_session",
]
