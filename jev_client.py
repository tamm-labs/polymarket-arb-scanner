"""Client for TypeSafe's Jev System One decision model via the direct TypeSafe API.

Provides low-latency, typed evaluations over structured states using
calibrated probabilities (noul), discrete selections (choice), and
rubric scoring (score).

Conventions:
- Target Python 3.11+
- "AI-powered software, not autonomous agents"
- Deterministic code maintains 100% control over workflow and safety.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import stat
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------

TYPESAFE_DECISIONS_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-1.13.0"
DEFAULT_TIMEOUT_SEC = 15


# ---------------------------------------------------------------------------
# Jev Decision Client
# ---------------------------------------------------------------------------


class JevError(Exception):
    """Base exception for Jev decision errors."""


class JevRateLimitError(JevError):
    """Raised when Jev API returns HTTP 429."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise JevError("TypeSafe API redirects are not allowed")


def _urlopen(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def load_api_key() -> str:
    """Load only explicitly configured TypeSafe secrets; never use OpenRouter keys."""
    key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    filename = os.getenv("TYPESAFE_API_KEY_FILE", "")
    if not filename:
        return ""
    try:
        fd = os.open(Path(filename).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise JevError("TypeSafe key file must be owned by this user with mode 600")
            key = stream.read(4097).strip()
            if not key or len(key) > 4096:
                raise JevError("Invalid TypeSafe key file")
            return key
    except JevError:
        raise
    except OSError:
        raise JevError("Cannot read configured TypeSafe key file") from None


def _number(value, low=0.0, high=1.0):
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def validate_answers(result: dict, questions: dict) -> None:
    """Fail closed on missing, invalid, or out-of-schema provider answers."""
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise JevError("TypeSafe response is missing typed answers")
    for name, question in questions.items():
        answer = result["answers"].get(name)
        kind = question.get("type")
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise JevError("TypeSafe response has a missing or mismatched answer type")
        if kind == "noul":
            if not _number(answer.get("noul")):
                raise JevError("TypeSafe returned an invalid probability")
            continue
        criteria = question.get("criteria", {})
        options = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
        probs = answer.get("probabilities")
        if (not _number(answer.get("confidence")) or not isinstance(probs, dict)
                or set(probs) != options or not all(_number(p) for p in probs.values())
                or not math.isclose(sum(probs.values()), 1.0, abs_tol=0.001)):
            raise JevError("TypeSafe returned an invalid decision distribution")
        if kind == "choice" and answer.get("choice") not in options:
            raise JevError("TypeSafe returned an unknown choice")
        if kind == "score" and not _number(answer.get("score"), 0, len(criteria) - 1):
            raise JevError("TypeSafe returned an invalid rubric score")


class JevClient:
    """Client for querying TypeSafe's Jev-1.13 decision model."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = TYPESAFE_DECISIONS_URL,
        timeout: int = DEFAULT_TIMEOUT_SEC,
    ):
        """Initialize Jev client.

        Args:
            api_key: TypeSafe key. Otherwise use TYPESAFE_API_KEY or TYPESAFE_API_KEY_FILE.
            model: Direct TypeSafe model identifier, pinned to jev-1.13.0 by default.
            base_url: Decisions API endpoint URL.
            timeout: Request timeout in seconds.
        """
        if base_url != TYPESAFE_DECISIONS_URL:
            raise JevError("TypeSafe keys may only be sent to the official decision endpoint")
        self.api_key = load_api_key() if api_key is None else api_key
        self.model = model or os.getenv("TYPESAFE_MODEL", DEFAULT_JEV_MODEL)
        if self.model.startswith("typesafe/"):
            raise JevError("Use a direct TypeSafe model ID such as jev-1.13.0")
        self.base_url = base_url
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if client has an API key configured."""
        return bool(self.api_key)

    def query_decisions(
        self,
        state: dict[str, Any] | str,
        questions: dict[str, dict[str, Any]],
        model: str | None = None,
    ) -> dict[str, Any]:
        """Send a structured decision request to the Jev model.

        Args:
            state: The context or data being evaluated (dict or string).
            questions: Dict of question_id -> question definition dict.
            model: Optional model override.

        Returns:
            Raw response dict containing answers, usage, and model metadata.

        Raises:
            JevError: On API or network failures.
        """
        if not self.api_key:
            raise JevError("TYPESAFE_API_KEY or TYPESAFE_API_KEY_FILE is not configured")

        selected_model = model or self.model
        payload = {
            "model": selected_model,
            "state": state,
            "questions": questions,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with _urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A response can echo sensitive request data. Do not log or retain its body.
            code = exc.code
            exc.close()
            if code in (429, 529):
                raise JevRateLimitError(f"TypeSafe temporarily unavailable (HTTP {code})") from None
            raise JevError(f"TypeSafe API HTTP {code}") from None
        except Exception:
            raise JevError("TypeSafe transport or JSON decoding failed") from None
        validate_answers(result, questions)
        return result

    def evaluate_noul(
        self,
        state: dict[str, Any] | str,
        instructions: str,
        criteria: dict[str, str] | None = None,
    ) -> float:
        """Evaluate a single yes/no question to a calibrated probability.

        Args:
            state: State context to evaluate.
            instructions: Question prompt to evaluate.
            criteria: Optional clarifying criteria for 'true' and 'false'.

        Returns:
            Calibrated probability float in range [0.0, 1.0].
        """
        question_def: dict[str, Any] = {
            "type": "noul",
            "instructions": instructions,
        }
        if criteria:
            question_def["criteria"] = criteria

        resp = self.query_decisions(state, {"q": question_def})
        answers = resp.get("answers", {})
        q_ans = answers.get("q", {})
        return float(q_ans["noul"])

    def evaluate_choice(
        self,
        state: dict[str, Any] | str,
        instructions: str,
        criteria: dict[str, str],
    ) -> tuple[str, float, dict[str, float]]:
        """Evaluate a discrete choice question from given options.

        Args:
            state: State context to evaluate.
            instructions: Question prompt to evaluate.
            criteria: Mapping of option_key -> descriptive criteria.

        Returns:
            Tuple of (selected_choice, confidence, probabilities_dict).
        """
        question_def = {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        }
        resp = self.query_decisions(state, {"q": question_def})
        answers = resp.get("answers", {})
        q_ans = answers.get("q", {})
        choice = str(q_ans.get("choice", ""))
        conf = float(q_ans.get("confidence", 0.0))
        probs = {str(k): float(v) for k, v in q_ans.get("probabilities", {}).items()}
        return choice, conf, probs

    def evaluate_score(
        self,
        state: dict[str, Any] | str,
        instructions: str,
        criteria: list[str],
    ) -> tuple[float, float, dict[str, float]]:
        """Evaluate an ordered rubric score along descriptive levels.

        Args:
            state: State context to evaluate.
            instructions: Question prompt to evaluate.
            criteria: Ordered list of level descriptions (0, 1, 2...).

        Returns:
            Tuple of (score_float, confidence, probabilities_dict).
        """
        question_def = {
            "type": "score",
            "instructions": instructions,
            "criteria": criteria,
        }
        resp = self.query_decisions(state, {"q": question_def})
        answers = resp.get("answers", {})
        q_ans = answers.get("q", {})
        score = float(q_ans.get("score", 0.0))
        conf = float(q_ans.get("confidence", 0.0))
        probs = {str(k): float(v) for k, v in q_ans.get("probabilities", {}).items()}
        return score, conf, probs


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_client: JevClient | None = None


def get_jev_client() -> JevClient:
    """Get or create the module-level JevClient singleton."""
    global _default_client
    if _default_client is None:
        _default_client = JevClient()
    return _default_client
