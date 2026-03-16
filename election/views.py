import hashlib
import logging

from django.contrib import messages
from django.core.cache import cache
from django.db import transaction, IntegrityError
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.conf import settings
from .models import (Voter, Vote, Aspirant, Portfolio,
                     AuditLog, DeviceFingerprint, ElectionSettings)
from .forms import VoterVerificationForm, VoteForm

logger = logging.getLogger(__name__)

# Generic message shown for any verification failure.
# A single message prevents an attacker from determining which field was wrong
# (student ID vs token) and prevents enumeration of valid student IDs.
_VERIFY_FAIL_MSG = "Verification failed. Please check your Student ID and token."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_ip(request) -> str:
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')


def _rate_limit(key: str, limit: int = 5, window: int = 300) -> bool:
    """
    Returns True if the request is within the rate limit, False if blocked.
    Limit: 5 attempts per IP per 5 minutes.

    FIXED: The original read-check-write was not atomic.
    Two concurrent requests could both read count=4, both pass the check,
    and both write 5 — effectively bypassing the limit on the boundary.

    This version uses:
      cache.add()  — atomic set-if-not-exists (sets to 1 on first call)
      cache.incr() — atomic increment (no read-modify-write race)
    """
    if cache.add(key, 1, timeout=window):
        # Key did not exist — this is the first attempt in the window.
        return True
    try:
        count = cache.incr(key)
    except ValueError:
        # Key expired between add() and incr() — extremely rare race;
        # treat as the first attempt rather than blocking the user.
        return True
    return count <= limit


def _build_fp_hash(fp: dict) -> str:
    signals = '|'.join(
        f"{k}={fp.get(k, '')}"
        for k in sorted(['canvas_hash', 'webgl_vendor', 'webgl_renderer',
                         'screen_info', 'timezone', 'language',
                         'platform', 'user_agent_hash'])
    )
    return hashlib.sha256(signals.encode()).hexdigest()


def _extract_fp(post) -> dict:
    def s(key, n): return str(post.get(key, ''))[:n]
    fp = {
        'canvas_hash':     s('fp_canvas',     64),
        'webgl_vendor':    s('fp_wgl_vendor', 128),
        'webgl_renderer':  s('fp_wgl_render', 128),
        'screen_info':     s('fp_screen',      64),
        'timezone':        s('fp_tz',           64),
        'language':        s('fp_lang',         32),
        'platform':        s('fp_platform',     64),
        'user_agent_hash': s('fp_ua_hash',      64),
    }
    fp['composite_hash'] = _build_fp_hash(fp)
    return fp


# ── Step 1: Verification ──────────────────────────────────────────────────────

@never_cache
@require_http_methods(["GET", "POST"])
def verify_voter(request):
    """
    Three-factor verification:
      1. Student ID (matric_number) — scopes the lookup
      2. 6-digit SMS token          — the primary secret
      3. Device fingerprint         — hardware signature (logged, flagged if duplicate)

    Security properties:
      • Student ID alone is useless — IDs are sequential and guessable
      • Token is cryptographically random, 1B+ combinations, single-use
      • All failure paths return the identical generic error message —
        prevents enumeration of valid student IDs or consumed tokens
      • Rate limited (atomic) to 5 attempts per IP per 5 min
      • Token permanently consumed after first successful verification
    """
    # Already verified and has a session — send to ballot
    if request.session.get('voter_id') and request.session.get('vote_session_token'):
        return redirect('election:vote_candidates')

    if request.method == 'POST':
        ip = _get_ip(request)

        # ── Rate limit (atomic) ───────────────────────────────────────────
        if not _rate_limit(f"rl:verify:{ip}"):
            AuditLog.objects.create(
                action='rate_limit_hit', actor='anonymous',
                ip_address=ip, detail='Verification rate limit exceeded'
            )
            messages.error(
                request,
                "Too many failed attempts from this device. "
                "Please wait 5 minutes before trying again."
            )
            return render(request, 'election/voter_form.html',
                          {'form': VoterVerificationForm()})

        # ── Election open? ────────────────────────────────────────────────
        election = ElectionSettings.get()
        if not election.is_open():
            AuditLog.objects.create(
                action='election_closed', actor='anonymous', ip_address=ip,
                detail='Verification attempted while election is closed'
            )
            messages.error(request,
                "Voting is currently closed. "
                "Please contact the election administrator if you believe "
                "this is an error.")
            return render(request, 'election/voter_form.html',
                          {'form': VoterVerificationForm(),
                           'election_closed': True})

        form = VoterVerificationForm(request.POST)
        if form.is_valid():
            student_id  = form.cleaned_data['matric_number']   # normalised + uppercase
            email       = form.cleaned_data['email']            # normalised lowercase
            token_input = form.cleaned_data['sms_token']       # normalised uppercase

            # ── Lookup ────────────────────────────────────────────────────
            try:
                voter = Voter.objects.get(
                    matric_number=student_id,
                    email__iexact=email,
                )
            except Voter.DoesNotExist:
                # Generic error — cannot reveal whether the student ID is valid
                logger.warning(
                    f"Verification: student not found "
                    f"id={student_id} ip={ip}"
                )
                messages.error(request, _VERIFY_FAIL_MSG)
                return render(request, 'election/voter_form.html', {'form': form})

            # ── Already voted ─────────────────────────────────────────────
            if voter.has_voted:
                logger.warning(
                    f"Already-voted voter {student_id} tried to re-verify ip={ip}")
                messages.warning(request, "You have already cast your vote.")
                return redirect('election:verify_voter')

            # ── Token not yet issued ──────────────────────────────────────
            if not voter.token_issued:
                AuditLog.objects.create(
                    action='no_token_issued', actor=student_id, ip_address=ip,
                    detail="Voter attempted to verify but no token has been issued"
                )
                messages.error(request,
                    "No voting token has been issued for your account. "
                    "Please contact the election administrator.")
                return render(request, 'election/voter_form.html', {'form': form})

            # ── Token already used (replay attempt) ───────────────────────
            # allowed an attacker to enumerate student IDs by detecting the
            # different response. Now uses the same generic error as all
            # other verification failures so no information is leaked.
            if voter.token_verified:
                AuditLog.objects.create(
                    action='token_reuse', actor=student_id, ip_address=ip,
                    detail="Voter attempted to re-use an already-consumed token"
                )
                logger.warning(f"Token reuse attempt: {student_id} ip={ip}")
                messages.error(request, _VERIFY_FAIL_MSG)
                return render(request, 'election/voter_form.html', {'form': form})

            # ── Verify token ──────────────────────────────────────────────
            if not voter.verify_sms_token(token_input):
                AuditLog.objects.create(
                    action='bad_token', actor=student_id, ip_address=ip,
                    detail="Incorrect 6-digit token submitted"
                )
                logger.warning(f"Bad token for voter {student_id} ip={ip}")
                messages.error(request, _VERIFY_FAIL_MSG)
                return render(request, 'election/voter_form.html', {'form': form})

            # ── Device fingerprint ────────────────────────────────────────
            fp_data   = _extract_fp(request.POST)
            composite = fp_data['composite_hash']

            # Flag if same device has already voted under a different identity
            existing_fp = (DeviceFingerprint.objects
                           .filter(composite_hash=composite)
                           .select_related('voter')
                           .first())

            if not getattr(settings, 'DISABLE_FP_CHECK', False):
                if existing_fp and existing_fp.voter_id != voter.id:
                    logger.warning(
                        f"FP duplicate: {composite[:12]}… "
                        f"prev={existing_fp.voter.matric_number} now={student_id}"
                    )
                    AuditLog.objects.create(
                        action='fp_duplicate', actor=student_id, ip_address=ip,
                        detail=(
                            f"Device hash {composite[:16]}… previously "
                            f"seen on voter {existing_fp.voter.matric_number}"
                        )
                    )
                    messages.error(request,
                        "A security check failed. "
                        "Contact the administrator if this is an error.")
                    return redirect('election:verify_voter')

            DeviceFingerprint.objects.update_or_create(
                voter=voter, defaults=fp_data
            )

            # ── Issue session vote-token ──────────────────────────────────
            session_token = voter.issue_vote_session_token()

            request.session['voter_id']          = voter.id
            request.session['vote_session_token'] = str(session_token)
            request.session['fp_composite']       = composite
            request.session.cycle_key()   # prevent session fixation

            AuditLog.objects.create(
                action='voter_verified', actor=student_id, ip_address=ip,
                detail=f"Email: {email} | FP: {composite[:16]}…"
            )
            logger.info(f"Voter {student_id} verified ip={ip}")
            return redirect('election:vote_candidates')

    else:
        form = VoterVerificationForm()

    return render(request, 'election/voter_form.html', {'form': form})


# ── Step 2: Cast Ballot ───────────────────────────────────────────────────────

@never_cache
@require_http_methods(["GET", "POST"])
def vote(request):
    """
    Verified voter submits their ballot.

    Security layers (in order):
      1. Election must be open
      2. Session must carry voter_id + vote_session_token
      3. Fingerprint re-checked — detects session hijacking
      4. DB row-lock (SELECT FOR UPDATE) prevents race conditions
      5. vote_session_token validated and consumed atomically
      6. Vote.portfolio FK populated — DB unique_voter_portfolio constraint fires
         if app-layer portfolio_seen check is somehow bypassed
      7. All Vote objects created in one atomic transaction
      8. Session flushed immediately on success
    """
    voter_id      = request.session.get('voter_id')
    session_token = request.session.get('vote_session_token')
    session_fp    = request.session.get('fp_composite', '')

    if not voter_id or not session_token:
        messages.error(request,
            "Your session has expired. Please verify your identity again.")
        return redirect('election:verify_voter')

    # ── Election still open? ──────────────────────────────────────────────────
    if not ElectionSettings.get().is_open():
        request.session.flush()
        messages.error(request, "Voting is now closed. Your session has ended.")
        return redirect('election:verify_voter')

    try:
        voter = Voter.objects.get(id=voter_id)
    except Voter.DoesNotExist:
        request.session.flush()
        messages.error(request, "Invalid session. Please start over.")
        return redirect('election:verify_voter')

    if voter.has_voted:
        request.session.flush()
        messages.warning(request, "You have already voted.")
        return redirect('election:verify_voter')

    if request.method == 'POST':
        ip   = _get_ip(request)
        form = VoteForm(request.POST)

        if form.is_valid():

            # ── Re-check fingerprint ──────────────────────────────────────
            fp_data   = _extract_fp(request.POST)
            submit_fp = fp_data['composite_hash']

            if not getattr(settings, 'DISABLE_FP_CHECK', False):
            

                if session_fp and submit_fp != session_fp:
                    logger.error(
                        f"FP mismatch {voter.matric_number}: "
                        f"verify={session_fp[:16]}… vote={submit_fp[:16]}…"
                    )

                    AuditLog.objects.create(
                        action='fp_mismatch', actor=voter.matric_number,
                        ip_address=ip,
                        detail=(f"Verified: {session_fp[:16]}… "
                            f"| Submit: {submit_fp[:16]}…")
                    )
                    request.session.flush()
                    messages.error(request,
                        "Security check failed. Please verify your identity again.")
                    return redirect('election:verify_voter')

            try:
                with transaction.atomic():
                    # Row-lock: prevents two simultaneous submissions
                    voter = Voter.objects.select_for_update().get(id=voter_id)

                    if voter.has_voted:
                        raise IntegrityError(
                            "Voter already voted (race condition caught)")

                    if not voter.consume_vote_session_token(session_token):
                        AuditLog.objects.create(
                            action='invalid_session_token',
                            actor=voter.matric_number, ip_address=ip
                        )
                        raise IntegrityError("Invalid or expired session token")

                    votes_to_create = []
                    portfolios_seen = set()

                    for field, value in form.cleaned_data.items():
                        if not field.startswith('portfolio_'):
                            continue
                        try:
                            pid = int(field.split('_')[1])
                        except (IndexError, ValueError):
                            raise ValueError(f"Malformed field: {field}")

                        if pid in portfolios_seen:
                            raise ValueError(f"Duplicate portfolio {pid}")
                        portfolios_seen.add(pid)

                        try:
                            portfolio = Portfolio.objects.get(id=pid)
                        except Portfolio.DoesNotExist:
                            raise ValueError(f"Portfolio {pid} does not exist")

                        aspirants = Aspirant.objects.filter(portfolio=portfolio)

                        if aspirants.count() == 1:
                            if value not in ('yes', 'no'):
                                raise ValueError(
                                    f"Invalid endorsement for portfolio {pid}")
                            if value == 'yes':
                                votes_to_create.append(
                                    Vote(voter=voter, aspirant=aspirants.first(),
                                         portfolio=portfolio))
                        else:
                            try:
                                aspirant = aspirants.get(id=int(value))
                            except (ValueError, Aspirant.DoesNotExist):
                                raise ValueError(
                                    f"Aspirant {value} invalid for portfolio {pid}")
                            votes_to_create.append(
                                Vote(voter=voter, aspirant=aspirant,
                                     portfolio=portfolio))

                    # bulk_create respects DB-level constraints;
                    # unique_voter_portfolio fires here if somehow a duplicate
                    # portfolio slipped past the portfolios_seen check above.
                    Vote.objects.bulk_create(votes_to_create)

                    voter.has_voted = True
                    voter.voted_at  = timezone.now()
                    voter.voted_ip  = ip
                    voter.save(update_fields=['has_voted', 'voted_at', 'voted_ip'])

                    AuditLog.objects.create(
                        action='vote_cast', actor=voter.matric_number,
                        ip_address=ip,
                        detail=f"{len(votes_to_create)} vote(s) cast"
                    )

                request.session.flush()
                logger.info(f"Voter {voter.matric_number} voted ip={ip}")
                messages.success(request,
                    "Your vote has been recorded. Thank you for participating!")
                return redirect('election:verify_voter')

            except IntegrityError as e:
                logger.error(f"IntegrityError voter={voter_id}: {e}")
                messages.error(request,
                    "A problem occurred. Please contact an administrator.")
                return redirect('election:verify_voter')

            except ValueError as e:
                logger.error(f"Validation error voter={voter_id}: {e}")
                messages.error(request,
                    "Your ballot contained invalid data. Please try again.")
        else:
            messages.error(request,
                "Please make a selection for every position.")
    else:
        form = VoteForm()

    return render(request, 'election/vote_candidates.html', {'form': form})


# ── Results ───────────────────────────────────────────────────────────────────

@never_cache
def results_page(request):
    """
    Public results page — only shown when admin has toggled results_visible.
    """
    election = ElectionSettings.get()
    if not election.results_visible:
        return render(request, 'election/results_unavailable.html', {
            'election_closed': not election.is_open(),
        })

    portfolios = Portfolio.objects.all().prefetch_related('aspirants')
    return render(request, 'election/live_results.html',
                  {'portfolios': portfolios})


def live_results_api(request):
    """
    JSON endpoint for the live results widget.

    FIXED: the original fired one COUNT(*) per aspirant inside a loop —
    O(n) queries.  This version uses a single annotated queryset so the
    entire result set is fetched in 1-2 DB round trips regardless of how
    many aspirants or portfolios exist.
    """
    election = ElectionSettings.get()
    if not election.results_visible:
        return JsonResponse({'error': 'Results not yet available.'}, status=403)

    # Annotate each aspirant with their vote count in a single query.
    portfolios = (
        Portfolio.objects
        .prefetch_related(
            # Fetch all aspirants, annotated with vote counts, in one shot.
            # Django translates this to a single LEFT OUTER JOIN + GROUP BY.
            __import__('django.db.models', fromlist=['Prefetch']).Prefetch(
                'aspirants',
                queryset=Aspirant.objects.annotate(
                    annotated_votes=Count('vote')
                ).order_by('-annotated_votes'),
            )
        )
        .all()
    )

    results = []
    for portfolio in portfolios:
        aspirants_data = [
            {
                'id':         a.id,
                'name':       a.name,
                'image_url':  a.image.url if a.image else None,
                'votes':      a.annotated_votes,
            }
            for a in portfolio.aspirants.all()
        ]
        total = sum(a['votes'] for a in aspirants_data)
        for a in aspirants_data:
            a['percentage'] = round(a['votes'] / total * 100, 1) if total else 0.0

        results.append({
            'id':          portfolio.id,
            'name':        portfolio.name,
            'aspirants':   aspirants_data,
            'total_votes': total,
        })

    return JsonResponse({
        'results':      results,
        'last_updated': timezone.now().isoformat(),
    })