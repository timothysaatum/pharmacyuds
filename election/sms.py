"""
Sends voting tokens directly via Arkesel SMS API.

Design decisions:
  1. API key in Authorization header, NOT the URL query string
     (prevents key leaking into server logs and proxies)
  2. Automatic retry with exponential back-off on transient failures
  3. Safe HTTPError handling — guards against missing response object
  4. Safe JSON parsing — handles HTML error pages from Arkesel/CDN
  5. Message length guard — warns before multi-part SMS is sent
  6. requests.Session reused across retries (connection pooling)
  7. attempts counter on SMSResult so admin can see how many tries it took
  8. BULK_TIMEOUT setting — allows a shorter per-attempt timeout when
     running inside a thread pool (avoids thread-pool starvation on
     slow networks during bulk sends)

Settings required in settings.py:
──────────────────────────────────────────────────────────────────────
    SMS_BACKEND          = 'arkesel'          # or 'dummy' for dev
    SMS_ARKESEL_API_KEY  = '<your-key>'
    SMS_ARKESEL_BASE_URL = 'https://sms.arkesel.com/sms/api'
    SMS_SENDER_ID        = 'GPSA-EC-UDS'
    SMS_BULK_TIMEOUT     = 10     # optional; shorter timeout for bulk sends
──────────────────────────────────────────────────────────────────────
"""

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────────────────
ARKESEL_TIMEOUT  = 15          # seconds per attempt (interactive / single send)
BULK_TIMEOUT     = 10          # seconds per attempt inside a thread pool
MAX_RETRIES      = 3           # total attempts before giving up
RETRY_BACKOFF    = (2, 5)      # wait before attempt 2 and attempt 3 (seconds)
SMS_MAX_CHARS    = 160         # single-part SMS — warn if message exceeds this

# These HTTP status codes are transient and worth retrying
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class SMSResult:
    success:  bool
    phone:    str
    provider: str = 'arkesel'
    error:    Optional[str] = None
    attempts: int = 1

    def __str__(self):
        if self.success:
            return (f"[{self.provider}] ✓ Sent to {self.phone} "
                    f"(attempt {self.attempts})")
        return (f"[{self.provider}] ✗ FAILED → {self.phone} "
                f"after {self.attempts} attempt(s): {self.error}")


# ── Phone normalisation ───────────────────────────────────────────────────────

def normalise_phone(raw: str) -> Optional[str]:
    """
    Converts any Ghanaian number to bare E.164 digits (no + prefix).

    Handles:
      0241234567    → 233241234567
      +233241234567 → 233241234567
      233241234567  → 233241234567
      241234567     → 233241234567
    """
    if not raw:
        return None
    digits = re.sub(r'[^\d]', '', str(raw))
    if len(digits) == 12 and digits.startswith('233'): return digits
    if len(digits) == 10 and digits.startswith('0'):   return '233' + digits[1:]
    if len(digits) == 9:                                return '233' + digits
    logger.warning(f"[SMS] Cannot normalise: {raw!r} → digits={digits!r}")
    return None


# ── Arkesel backend ───────────────────────────────────────────────────────────

class ArkeselBackend:
    """
    Calls Arkesel v1 SMS API.

    Key design decisions:
    - API key goes in the 'api-key' request header, not the URL.
    - Retries transient failures with back-off.
    - Does NOT retry API-level failures (invalid number, low balance).
    - Uses requests.Session so TCP connections are reused across retries.
    - Supports a shorter BULK_TIMEOUT for thread-pool sends to prevent
      one slow network call from starving other workers.
    """

    def __init__(self):
        self.api_key   = getattr(settings, 'SMS_ARKESEL_API_KEY',  '').strip()
        self.base_url  = getattr(settings, 'SMS_ARKESEL_BASE_URL',
                                 'https://sms.arkesel.com/sms/api').rstrip('/')
        self.sender_id = getattr(settings, 'SMS_SENDER_ID', 'GPSA-EC-UDS').strip()
        # Allow a shorter timeout for bulk/parallel sends
        self._bulk_timeout = int(getattr(settings, 'SMS_BULK_TIMEOUT',
                                         BULK_TIMEOUT))

        if not self.api_key:
            raise ValueError(
                "SMS_ARKESEL_API_KEY is not set in settings.py."
            )
        if not self.sender_id:
            raise ValueError("SMS_SENDER_ID is not set in settings.py.")

    def send(self, phone: str, message: str,
             bulk: bool = False) -> SMSResult:
        """
        Send an SMS.

        Args:
            phone:   Recipient phone number (any Ghanaian format accepted).
            message: Message text.
            bulk:    If True, uses the shorter BULK_TIMEOUT so thread-pool
                     workers don't block each other on slow network calls.
        """
        normalised = normalise_phone(phone)
        if not normalised:
            return SMSResult(
                success=False, phone=phone,
                error=f"Cannot normalise phone number: {phone!r}"
            )

        timeout = self._bulk_timeout if bulk else ARKESEL_TIMEOUT

        if len(message) > SMS_MAX_CHARS:
            logger.warning(
                f"[Arkesel] Message is {len(message)} chars — exceeds "
                f"{SMS_MAX_CHARS}. Will send as multi-part SMS "
                "(costs double, may arrive out of order on some networks)."
            )

        url = (
            f"{self.base_url}"
            f"?action=send-sms"
            f"&to={normalised}"
            f"&from={urllib.parse.quote(self.sender_id)}"
            f"&sms={urllib.parse.quote(message)}"
        )
        headers = {'api-key': self.api_key}

        last_error = "Unknown error"
        attempt    = 0

        with requests.Session() as session:
            while attempt < MAX_RETRIES:
                attempt += 1
                try:
                    resp = session.get(url, headers=headers, timeout=timeout)

                    if resp.status_code in RETRYABLE_STATUS:
                        last_error = f"HTTP {resp.status_code}"
                        logger.warning(
                            f"[Arkesel] {last_error} for {normalised} "
                            f"(attempt {attempt}/{MAX_RETRIES})"
                        )
                        self._backoff(attempt)
                        continue

                    resp.raise_for_status()

                    try:
                        data = resp.json()
                    except ValueError:
                        last_error = f"Non-JSON response: {resp.text[:120]!r}"
                        logger.error(
                            f"[Arkesel] {last_error} for {normalised} "
                            f"(attempt {attempt}/{MAX_RETRIES})"
                        )
                        self._backoff(attempt)
                        continue

                    if (str(data.get('status', '')).lower() == 'success'
                            or str(data.get('code', '')).lower() == 'ok'):
                        logger.info(
                            f"[Arkesel] ✓ Sent to {normalised} "
                            f"(attempt {attempt}, bulk={bulk})"
                        )
                        return SMSResult(
                            success=True, phone=normalised, attempts=attempt
                        )

                    last_error = data.get('message', str(data))
                    logger.error(
                        f"[Arkesel] API error for {normalised}: {last_error}"
                    )
                    return SMSResult(
                        success=False, phone=normalised,
                        error=last_error, attempts=attempt
                    )

                except requests.Timeout:
                    last_error = f"Timeout after {timeout}s"
                    logger.warning(
                        f"[Arkesel] {last_error} for {normalised} "
                        f"(attempt {attempt}/{MAX_RETRIES})"
                    )
                    self._backoff(attempt)

                except requests.HTTPError as e:
                    status = (e.response.status_code
                              if e.response is not None else 'no-response')
                    last_error = f"HTTP error {status}: {e}"
                    logger.error(f"[Arkesel] {last_error} for {normalised}")
                    return SMSResult(
                        success=False, phone=normalised,
                        error=last_error, attempts=attempt
                    )

                except requests.ConnectionError as e:
                    last_error = f"Connection error: {e}"
                    logger.warning(
                        f"[Arkesel] {last_error} for {normalised} "
                        f"(attempt {attempt}/{MAX_RETRIES})"
                    )
                    self._backoff(attempt)

                except Exception as e:
                    last_error = f"Unexpected error: {e}"
                    logger.error(
                        f"[Arkesel] {last_error} for {normalised}",
                        exc_info=True
                    )
                    return SMSResult(
                        success=False, phone=normalised,
                        error=last_error, attempts=attempt
                    )

        logger.error(
            f"[Arkesel] All {MAX_RETRIES} attempts failed for "
            f"{normalised}: {last_error}"
        )
        return SMSResult(
            success=False, phone=normalised, attempts=attempt,
            error=f"Failed after {MAX_RETRIES} attempts: {last_error}",
        )

    @staticmethod
    def _backoff(attempt: int) -> None:
        if attempt <= len(RETRY_BACKOFF):
            delay = RETRY_BACKOFF[attempt - 1]
            logger.debug(f"[Arkesel] Waiting {delay}s before retry {attempt + 1}")
            time.sleep(delay)


# ── Dummy backend ─────────────────────────────────────────────────────────────

class DummyBackend:
    """
    Prints the token to the Django console — no API call, no balance consumed.
    Set SMS_BACKEND = 'dummy' in settings.py during development.
    """
    def send(self, phone: str, message: str,
             bulk: bool = False) -> SMSResult:
        border = '─' * 54
        print(f"\n┌{border}┐")
        print(f"│  📱 DUMMY SMS  →  {phone:<34}│")
        for line in message.split('\n'):
            print(f"│  {line:<52}│")
        print(f"└{border}┘\n")
        logger.info(f"[DummySMS] Would send to {phone}")
        return SMSResult(success=True, phone=phone, provider='dummy')


# ── Factory ───────────────────────────────────────────────────────────────────

_BACKENDS = {
    'arkesel': ArkeselBackend,
    'dummy':   DummyBackend,
}

def _get_backend():
    name = getattr(settings, 'SMS_BACKEND', 'dummy').lower().strip()
    cls  = _BACKENDS.get(name)
    if not cls:
        raise ValueError(
            f"Unknown SMS_BACKEND: {name!r}. "
            f"Valid options: {list(_BACKENDS)}"
        )
    return cls()


# ── Public entry point ────────────────────────────────────────────────────────

def send_token_sms(voter, plaintext_token: str,
                   bulk: bool = False) -> SMSResult:
    """
    Send the 6-digit voting token to a voter's registered phone number.

    Args:
        voter:           Voter model instance (must have phone_number set)
        plaintext_token: The 6-char token e.g. 'K4R7QX'
        bulk:            Pass True when called from a thread pool to use
                         the shorter BULK_TIMEOUT and avoid thread starvation.

    Returns:
        SMSResult — check .success, .error, and .attempts
    """
    if not voter.phone_number:
        return SMSResult(
            success=False, phone='',
            error=f"Voter {voter.matric_number} has no phone number on record"
        )

    message = (
        f"UDS Elections\n"
        f"ID: {voter.matric_number}\n"
        f"Token: {plaintext_token}\n"
        f"Single-use. Do not share."
    )

    if len(message) > SMS_MAX_CHARS:
        logger.warning(
            f"[SMS] Message for {voter.matric_number} is {len(message)} chars. "
            f"Exceeds {SMS_MAX_CHARS} — will be multi-part SMS."
        )

    return _get_backend().send(voter.phone_number, message, bulk=bulk)