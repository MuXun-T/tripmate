class LLMError(Exception):
    """Base class for planner LLM failures."""


class LLMOutputError(LLMError):
    """The model output could not be parsed or validated after the corrective retry."""


class LLMRequestError(LLMError):
    """A non-retryable request failure (4xx) or retries were exhausted."""


class BudgetExceeded(LLMError):
    """A spending cap would be exceeded; callers must stop the whole run."""


class CacheMiss(LLMError):
    """replay_only mode found no cached response."""
