"""
Election Models
===============
Authentication flow (3 factors):

  Factor 1 │ Student ID   (matric_number)  │ Something they KNOW — but guessable
  Factor 2 │ 6-digit token (sms_token)     │ Something they HAVE — the real secret
  Factor 3 │ Device fingerprint            │ Something they ARE  — hardware signature

The student ID alone is NOT sufficient for verification because IDs are
sequential and easily predicted (PHA/0023/20 → PHA/0024/20).
The 6-digit token is the primary secret. Without it, knowing the student
ID gives an attacker nothing.

Token lifecycle:
  1. Admin generates tokens → SHA-256 hashes stored in DB → plaintext
     sent to student's phone via SMS (via Arkesel).
  2. On election day: student submits ID + token.
     → Both checked together.
     → Token marked used on first successful verification (single-use).
  3. A short-lived session vote_token is issued for the ballot step.
  4. session vote_token consumed the moment ballot is submitted.

Migrations needed after this version:
  - python manage.py makemigrations election
  - python manage.py migrate
  Changes: new ElectionSettings table; Vote gains portfolio FK and updated
  constraints (unique_voter_aspirant + unique_voter_portfolio).
"""

import uuid
import secrets
import hashlib
import string

from django.db import models
from django.utils import timezone


# ── Token helpers ────────────────────────────────────────────────────────────

# 32-char alphabet: A-Z + 2-9, excluding I O 0 1 (visually ambiguous on screen)
_ALPHABET = ''.join(
    c for c in (string.ascii_uppercase + string.digits)
    if c not in ('I', 'O', '0', '1')
)
TOKEN_CHARS = 6   # 32^6 = 1,073,741,824 combinations — unguessable


def generate_sms_token() -> str:
    """
    Cryptographically random 6-character token.
    Example: K4R7QX
    Uses secrets.choice() backed by os.urandom() — NOT random.choice().
    """
    return ''.join(secrets.choice(_ALPHABET) for _ in range(TOKEN_CHARS))


def hash_token(token: str) -> str:
    """
    SHA-256 of the uppercase token.
    Only this hash is ever stored — plaintext never persists to the DB.
    """
    return hashlib.sha256(token.strip().upper().encode()).hexdigest()


# ── Models ───────────────────────────────────────────────────────────────────

class ClassGroup(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class Voter(models.Model):
    # ── From class list ───────────────────────────────────────────────────
    matric_number = models.CharField(
        max_length=50, unique=True,
        verbose_name="Student ID",
        help_text="e.g. PHA/0003/20"
    )
    full_name    = models.CharField(max_length=255, blank=True)
    phone_number = models.CharField(max_length=20,  blank=True)
    email        = models.EmailField(max_length=254, blank=True,
                                     verbose_name="Email address",
                                     help_text="Used as the second verification factor at login.")
    class_group  = models.ForeignKey(ClassGroup, on_delete=models.CASCADE)

    # ── 6-digit SMS token (Factor 2 — the primary secret) ────────────────
    #
    # sms_token_hash : SHA-256 of the 6-char token. Plaintext never stored.
    # token_issued   : True once a token has been generated for this voter.
    # token_verified : True once the voter has successfully verified.
    #                  After this the token is consumed — cannot be replayed
    #                  even if the session is abandoned before voting.
    #
    sms_token_hash = models.CharField(
        max_length=64, blank=True, editable=False,
        verbose_name="Token Hash",
        help_text="SHA-256 of the 6-char SMS token. Plaintext never stored."
    )
    token_issued   = models.BooleanField(default=False, editable=False,
                                         verbose_name="Token Issued")
    token_verified = models.BooleanField(default=False, editable=False,
                                          verbose_name="Token Verified")

    # ── SMS delivery status ───────────────────────────────────────────────
    sms_sent          = models.BooleanField(default=False, editable=False,
                                             verbose_name="SMS Sent")
    sms_sent_at       = models.DateTimeField(null=True, blank=True,
                                              editable=False)
    sms_failed_reason = models.CharField(max_length=255, blank=True,
                                          editable=False)

    # ── Session vote-token (issued AFTER factor-2 verification) ──────────
    #
    # This is separate from the SMS token. It is a short-lived UUID
    # generated after the student verifies, stored in their session,
    # and consumed atomically when the ballot is submitted.
    #
    vote_session_token = models.UUIDField(null=True, blank=True,
                                           editable=False)

    # ── Voting state ──────────────────────────────────────────────────────
    has_voted = models.BooleanField(default=False, editable=False)
    voted_at  = models.DateTimeField(null=True, blank=True, editable=False)
    voted_ip  = models.GenericIPAddressField(null=True, blank=True,
                                              editable=False)

    def __str__(self):
        return f"{self.matric_number} — {self.full_name}"

    # ── SMS token methods ─────────────────────────────────────────────────

    def set_sms_token(self, plaintext: str):
        """Hash and store a new 6-char token. Resets verification state."""
        self.sms_token_hash = hash_token(plaintext)
        self.token_issued   = True
        self.token_verified = False
        self.save(update_fields=['sms_token_hash', 'token_issued', 'token_verified'])

    def verify_sms_token(self, plaintext: str) -> bool:
        """
        Validate the submitted token against the stored hash.
        Single-use: marks token_verified=True on first successful call.
        All subsequent calls return False even with the correct token.
        Uses secrets.compare_digest() to prevent timing attacks.
        """
        if not self.token_issued or self.token_verified:
            return False
        if secrets.compare_digest(hash_token(plaintext), self.sms_token_hash):
            self.token_verified = True
            self.save(update_fields=['token_verified'])
            return True
        return False

    # ── SMS tracking ──────────────────────────────────────────────────────

    def record_sms_sent(self):
        self.sms_sent          = True
        self.sms_sent_at       = timezone.now()
        self.sms_failed_reason = ''
        self.save(update_fields=['sms_sent', 'sms_sent_at', 'sms_failed_reason'])

    def record_sms_failed(self, reason: str):
        self.sms_sent          = False
        self.sms_failed_reason = str(reason)[:255]
        self.save(update_fields=['sms_sent', 'sms_failed_reason'])

    # ── Session vote-token methods ────────────────────────────────────────

    def issue_vote_session_token(self) -> uuid.UUID:
        """Issue a fresh UUID session token after SMS-token verification."""
        self.vote_session_token = uuid.uuid4()
        self.save(update_fields=['vote_session_token'])
        return self.vote_session_token

    def consume_vote_session_token(self, token) -> bool:
        """Validate and destroy the session token. Single-use."""
        if self.vote_session_token and \
                str(self.vote_session_token) == str(token):
            self.vote_session_token = None
            self.save(update_fields=['vote_session_token'])
            return True
        return False


class DeviceFingerprint(models.Model):
    """
    Hardware/browser fingerprint captured at verification time and
    re-validated when the ballot is submitted.

    Signals collected client-side by fingerprint.js:
      canvas_hash     — GPU + font rendering signature (most stable)
      webgl_vendor    — GPU vendor string
      webgl_renderer  — GPU model string
      screen_info     — resolution × colour depth × devicePixelRatio
      timezone        — Intl.DateTimeFormat timezone
      language        — navigator.language
      platform        — navigator.platform
      user_agent_hash — SHA-256 of raw UA string (we never store raw UA)
      composite_hash  — SHA-256 of all signals combined (primary lookup key)

    Two threat models addressed:
      1. Session hijacking   — composite_hash changes between verify + vote
      2. Shared-device abuse — same composite_hash seen on multiple voters
    """
    voter           = models.OneToOneField(
        Voter, on_delete=models.CASCADE, related_name='device_fingerprint'
    )
    canvas_hash     = models.CharField(max_length=64,  blank=True)
    webgl_vendor    = models.CharField(max_length=128, blank=True)
    webgl_renderer  = models.CharField(max_length=128, blank=True)
    screen_info     = models.CharField(max_length=64,  blank=True)
    timezone        = models.CharField(max_length=64,  blank=True)
    language        = models.CharField(max_length=32,  blank=True)
    platform        = models.CharField(max_length=64,  blank=True)
    user_agent_hash = models.CharField(max_length=64,  blank=True)
    composite_hash  = models.CharField(max_length=64,  db_index=True)
    captured_at     = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"FP({self.voter.matric_number}) {self.captured_at:%Y-%m-%d %H:%M}"


class Portfolio(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Aspirant(models.Model):
    name      = models.CharField(max_length=255)
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE,
                                  related_name='aspirants')
    image     = models.ImageField(upload_to='aspirant_images/',
                                  null=True, blank=True)

    def __str__(self):
        return f"{self.name} — {self.portfolio.name}"

    def vote_count(self):
        return self.vote_set.count()
    vote_count.short_description = 'Votes'


class Vote(models.Model):
    """
    One row per vote cast.

    DB-level constraints:
      unique_voter_aspirant  — prevents voting twice for the same aspirant
      unique_voter_portfolio — prevents voting for two aspirants in one
                               portfolio (belt-and-braces over app-layer check)

    `portfolio` is a denormalised FK purely to support the second constraint.
    It must always match aspirant.portfolio and is set by the view.
    """
    voter     = models.ForeignKey(Voter,     on_delete=models.CASCADE)
    aspirant  = models.ForeignKey(Aspirant,  on_delete=models.CASCADE)
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['voter', 'aspirant'],
                name='unique_voter_aspirant',
            ),
            models.UniqueConstraint(
                fields=['voter', 'portfolio'],
                name='unique_voter_portfolio',
            ),
        ]

    def __str__(self):
        return (f"{self.voter.matric_number} → "
                f"{self.aspirant} @ {self.timestamp}")


class VoterList(models.Model):
    file        = models.FileField(upload_to='voter_files/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.file.name} ({self.uploaded_at:%Y-%m-%d})"


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('vote_cast',             'Vote Cast'),
        ('voter_verified',        'Voter Verified'),
        ('voter_imported',        'Voter Imported'),
        ('aspirant_added',        'Aspirant Added'),
        ('token_generated',       'Tokens Generated'),
        ('token_sms_sent',        'Token SMS Sent'),
        ('token_sms_failed',      'Token SMS Failed'),
        ('bad_token',             'Wrong Token Submitted'),
        ('token_reuse',           'Token Reuse Attempt'),
        ('no_token_issued',       'Voter Has No Token Issued'),
        ('fp_mismatch',           'Fingerprint Mismatch at Vote Time'),
        ('fp_duplicate',          'Fingerprint Duplicate — possible multi-vote'),
        ('rate_limit_hit',        'Rate Limit Hit'),
        ('invalid_session_token', 'Invalid Session Vote Token'),
        ('election_closed',       'Vote Attempted While Election Closed'),
    ]
    action     = models.CharField(max_length=50, choices=ACTION_CHOICES)
    actor      = models.CharField(max_length=100,
                                   help_text="Student ID or admin username")
    detail     = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp  = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.action} — {self.actor}"


class ElectionSettings(models.Model):
    """
    Singleton model for election-wide configuration.

    Always access via ElectionSettings.get() — never instantiate directly.
    The record with pk=1 is auto-created on first access.

    Fields:
      start_time      — voting opens (None = open immediately)
      end_time        — voting closes (None = never closes automatically)
      results_visible — whether the results page is publicly accessible
    """
    start_time      = models.DateTimeField(
        null=True, blank=True,
        help_text="When voting opens. Leave blank to open immediately."
    )
    end_time        = models.DateTimeField(
        null=True, blank=True,
        help_text="When voting closes. Leave blank for no automatic close."
    )
    results_visible = models.BooleanField(
        default=False,
        help_text="Make the results page publicly visible."
    )

    class Meta:
        verbose_name        = 'Election settings'
        verbose_name_plural = 'Election settings'

    def __str__(self):
        status = 'open' if self.is_open() else 'closed'
        return f"Election settings ({status})"

    @classmethod
    def get(cls) -> 'ElectionSettings':
        """Return the singleton settings record, creating it if absent."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def is_open(self) -> bool:
        """
        True if the election is currently accepting votes.
        Checks both start_time and end_time boundaries.
        """
        now = timezone.now()
        if self.start_time and now < self.start_time:
            return False
        if self.end_time and now > self.end_time:
            return False
        return True

    def save(self, *args, **kwargs):
        """Enforce singleton — always use pk=1."""
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Prevent deletion of the singleton."""
        pass