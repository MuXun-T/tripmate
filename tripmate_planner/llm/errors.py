class LLMError(Exception):
    """Base class for planner LLM failures."""


class LLMOutputError(LLMError):
    """The model output could not be parsed or validated after the corrective retry."""


class LLMRequestError(LLMError):
    """A non-retryable request failure (4xx) or retries were exhausted."""


class BudgetExceeded(LLMError):
    """A spending cap would be exceeded; callers must stop the whole run."""


class CacheMiss(LLMRequestError):
    """replay_only mode found no cached response.

    A failed original request (e.g. retries exhausted on a network error) leaves no cache entry, so in a replay a
    miss is handled exactly like that failure (callers fall back the same way); replay checks compare plans and
    metrics, so a genuinely missing entry still shows up as a difference."""
