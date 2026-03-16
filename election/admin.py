import csv
import logging
import concurrent.futures as cf
from django.db import connection
from django import forms as forms

from django.contrib import admin, messages
from django.http import HttpResponse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from election.sms import send_token_sms

from .models import (
    ClassGroup, Voter, Portfolio, Aspirant,
    Vote, VoterList, AuditLog, DeviceFingerprint,
    ElectionSettings, generate_sms_token,
)
from .utils import (
    extract_voters_from_excel, extract_voters_from_word,
    extract_voters_from_pdf, save_voter_list,
)

logger = logging.getLogger(__name__)

# Maximum threads for parallel SMS delivery.
# Each thread holds one HTTP connection to Arkesel, so keep this reasonable.
# At 20 workers, 200 SMS sends complete in roughly the time of one send (~15 s)
# instead of sequentially (200 × 15 s = 50 min).
_SMS_MAX_WORKERS = 20


# ── Thread-pool worker ────────────────────────────────────────────────────────

def _send_one_token(voter_id: int, plaintext: str) -> tuple[int, object]:
    """
    Executed inside a ThreadPoolExecutor thread.

    Each Django thread gets its own DB connection from the pool; closing it
    explicitly at the end of the worker returns it promptly rather than
    waiting for GC.

    Returns (voter_id, SMSResult).
    """
    try:
        voter  = Voter.objects.get(id=voter_id)
        result = send_token_sms(voter, plaintext, bulk=True)
        if result.success:
            voter.record_sms_sent()
        else:
            voter.record_sms_failed(result.error or 'Unknown error')
        return voter_id, result
    except Exception as exc:
        logger.error(f"Unexpected error sending token for voter {voter_id}: {exc}",
                     exc_info=True)
        # Return a minimal failure result so the caller can count it
        from election.sms import SMSResult
        return voter_id, SMSResult(success=False, phone='',
                                   error=str(exc), attempts=1)
    finally:
        # Return this thread's DB connection to the pool
        connection.close()


# ── Admins ────────────────────────────────────────────────────────────────────

@admin.register(ElectionSettings)
class ElectionSettingsAdmin(admin.ModelAdmin):
    """
    Singleton admin — the object is always pk=1.
    The changelist is hidden; the admin redirects straight to the change form.
    """
    fields = ('start_time', 'end_time', 'results_visible')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        """Skip the list view — go directly to the settings form."""
        from django.shortcuts import redirect
        from django.urls import reverse
        obj, _ = ElectionSettings.objects.get_or_create(pk=1)
        return redirect(
            reverse('admin:election_electionsettings_change', args=[obj.pk])
        )


@admin.register(ClassGroup)
class ClassGroupAdmin(admin.ModelAdmin):
    list_display = ('name',)


@admin.register(Voter)
class VoterAdmin(admin.ModelAdmin):
    list_display = (
        'matric_number', 'full_name', 'phone_number', 'class_group',
        'token_issued', 'sms_status_col', 'token_verified', 'has_voted', 'voted_at',
    )
    list_filter  = (
        'class_group', 'has_voted',
        'token_issued', 'token_verified', 'sms_sent',
    )
    search_fields  = ('matric_number', 'full_name', 'phone_number')
    readonly_fields = (
        'sms_token_hash', 'token_issued', 'token_verified',
        'sms_sent', 'sms_sent_at', 'sms_failed_reason',
        'vote_session_token', 'has_voted', 'voted_at', 'voted_ip',
    )
    actions = [
        'generate_and_send_tokens',
        'retry_failed_sms',
        'export_voters_csv',
    ]

    # ── Coloured SMS status column ────────────────────────────────────────

    def sms_status_col(self, obj):
        if not obj.token_issued:
            return mark_safe('<span style="color:#aaa;">—</span>')
        if obj.sms_sent:
            ts = obj.sms_sent_at.strftime('%d %b %H:%M') if obj.sms_sent_at else ''
            return format_html(
                '<span style="color:#27ae60;font-weight:600;">✓ Sent {}</span>', ts
            )
        if obj.sms_failed_reason:
            return format_html(
                '<span style="color:#e74c3c;font-weight:600;" '
                'title="{}">✗ Failed</span>',
                obj.sms_failed_reason
            )
        return mark_safe('<span style="color:#f39c12;">⏳ Pending</span>')
    sms_status_col.short_description = 'SMS'

    # ── Action 1: Generate tokens + send SMS (parallel) ───────────────────

    def generate_and_send_tokens(self, request, queryset):
        """
        For each selected voter who does NOT yet have a token:
          1. Generate a cryptographically random 6-char token
          2. Store its SHA-256 hash (plaintext is NEVER saved to DB)
          3. Send plaintext token to voter's phone via SMS in a thread pool
             so all sends are dispatched in parallel rather than sequentially.
             At 20 workers, 200 sends complete in ~15 s instead of ~50 min.
          4. Record success / failure on the voter record and in AuditLog

        Voters who already have a token are skipped.
        Voters with no phone number are skipped and reported.
        """
        already = queryset.filter(token_issued=True)
        if already.exists():
            ids = ', '.join(v.matric_number for v in already[:5])
            self.message_user(
                request,
                f"{already.count()} voter(s) already have tokens and were "
                f"skipped: {ids}{'…' if already.count() > 5 else ''}. "
                "Use 'Retry failed SMS' to resend.",
                level=messages.WARNING,
            )

        eligible = queryset.filter(token_issued=False)
        if not eligible.exists():
            self.message_user(request, "No eligible voters in selection.",
                              level=messages.WARNING)
            return

        no_phone  = eligible.filter(phone_number='')
        has_phone = eligible.exclude(phone_number='')

        if no_phone.exists():
            self.message_user(
                request,
                f"{no_phone.count()} voter(s) skipped — no phone number: "
                + ', '.join(v.matric_number for v in no_phone[:5]),
                level=messages.WARNING,
            )

        # ── Phase 1: generate all tokens synchronously ────────────────────
        # Token generation is fast (crypto RNG + one DB write per voter).
        # All tokens are hashed and stored before any SMS is dispatched
        # so the DB is consistent even if SMS delivery partially fails.
        voter_jobs: list[tuple[int, str]] = []   # [(voter_id, plaintext), …]

        for voter in has_phone:
            plaintext = generate_sms_token()
            voter.set_sms_token(plaintext)
            voter_jobs.append((voter.id, plaintext))

        # ── Phase 2: send SMS in a thread pool ────────────────────────────
        sent_count = failed_count = 0
        failed_ids: list[str] = []

        with cf.ThreadPoolExecutor(max_workers=_SMS_MAX_WORKERS) as pool:
            future_map = {
                pool.submit(_send_one_token, vid, pt): vid
                for vid, pt in voter_jobs
            }
            voter_lookup = {v.id: v for v in has_phone}

            for future in cf.as_completed(future_map):
                voter_id, result = future.result()
                voter = voter_lookup[voter_id]

                if result.success:
                    AuditLog.objects.create(
                        action='token_sms_sent',
                        actor=request.user.username,
                        detail=(f"6-digit token SMS → "
                                f"{voter.matric_number} ({voter.phone_number}) "
                                f"[{result.attempts} attempt(s)]"),
                    )
                    sent_count += 1
                else:
                    AuditLog.objects.create(
                        action='token_sms_failed',
                        actor=request.user.username,
                        detail=f"{voter.matric_number}: {result.error}",
                    )
                    failed_count += 1
                    failed_ids.append(f"{voter.matric_number} ({result.error})")

        AuditLog.objects.create(
            action='token_generated',
            actor=request.user.username,
            detail=f"Generated tokens: {sent_count} sent, {failed_count} failed.",
        )

        if sent_count:
            self.message_user(
                request,
                f"✓ {sent_count} token(s) generated and sent via SMS.",
                level=messages.SUCCESS,
            )
        if failed_count:
            self.message_user(
                request,
                f"✗ {failed_count} SMS(es) failed: "
                f"{'; '.join(failed_ids[:5])}"
                f"{'…' if failed_count > 5 else ''}. "
                "Use 'Retry failed SMS' to try again.",
                level=messages.ERROR,
            )

    generate_and_send_tokens.short_description = \
        "① Generate 6-digit tokens & send via SMS"

    # ── Action 2: Retry failed SMS deliveries (parallel) ─────────────────

    def retry_failed_sms(self, request, queryset):
        """
        Re-generates and resends tokens for voters whose SMS failed.
        Sends in parallel using the same thread pool as the initial action.
        """
        eligible = queryset.filter(token_issued=True, sms_sent=False,
                                   token_verified=False)
        if not eligible.exists():
            self.message_user(
                request,
                "No voters with failed SMS in selection. "
                "Filter by 'SMS Sent: No' to find them.",
                level=messages.WARNING,
            )
            return

        # Re-generate tokens (old token was never received — safe to replace)
        voter_jobs: list[tuple[int, str]] = []
        for voter in eligible:
            plaintext = generate_sms_token()
            voter.set_sms_token(plaintext)
            voter_jobs.append((voter.id, plaintext))

        sent_count = still_failed = 0
        voter_lookup = {v.id: v for v in eligible}

        with cf.ThreadPoolExecutor(max_workers=_SMS_MAX_WORKERS) as pool:
            future_map = {
                pool.submit(_send_one_token, vid, pt): vid
                for vid, pt in voter_jobs
            }
            for future in cf.as_completed(future_map):
                voter_id, result = future.result()
                voter = voter_lookup[voter_id]

                if result.success:
                    AuditLog.objects.create(
                        action='token_sms_sent',
                        actor=request.user.username,
                        detail=f"Retry SMS → {voter.matric_number}",
                    )
                    sent_count += 1
                else:
                    AuditLog.objects.create(
                        action='token_sms_failed',
                        actor=request.user.username,
                        detail=(f"Retry still failing for "
                                f"{voter.matric_number}: {result.error}"),
                    )
                    still_failed += 1

        if sent_count:
            self.message_user(request, f"✓ {sent_count} retry SMS(es) sent.",
                              level=messages.SUCCESS)
        if still_failed:
            self.message_user(
                request,
                f"✗ {still_failed} still failing. "
                "Check phone numbers and SMS provider status.",
                level=messages.ERROR,
            )

    retry_failed_sms.short_description = "② Retry failed SMS deliveries"

    # ── Action 3: CSV export ──────────────────────────────────────────────

    def export_voters_csv(self, request, queryset):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename=voters.csv'
        w = csv.writer(response)
        w.writerow([
            'Student ID', 'Full Name', 'Phone', 'Level',
            'Token Issued', 'SMS Sent', 'SMS Sent At', 'SMS Failure',
            'Token Verified', 'Has Voted', 'Voted At',
        ])
        for v in queryset:
            w.writerow([
                v.matric_number, v.full_name, v.phone_number,
                str(v.class_group),
                'Yes' if v.token_issued   else 'No',
                'Yes' if v.sms_sent       else 'No',
                v.sms_sent_at.strftime('%Y-%m-%d %H:%M') if v.sms_sent_at else '',
                v.sms_failed_reason,
                'Yes' if v.token_verified else 'No',
                'Yes' if v.has_voted      else 'No',
                v.voted_at.strftime('%Y-%m-%d %H:%M') if v.voted_at else '',
            ])
        return response
    export_voters_csv.short_description = "Export selected voters as CSV"


@admin.register(DeviceFingerprint)
class DeviceFingerprintAdmin(admin.ModelAdmin):
    list_display  = ('voter', 'fp_short', 'webgl_renderer',
                     'platform', 'screen_info', 'captured_at')
    list_filter   = ('platform',)
    search_fields = ('voter__matric_number', 'voter__full_name',
                     'composite_hash', 'webgl_renderer')
    readonly_fields = (
        'voter', 'canvas_hash', 'webgl_vendor', 'webgl_renderer',
        'screen_info', 'timezone', 'language', 'platform',
        'user_agent_hash', 'composite_hash', 'captured_at',
    )

    def fp_short(self, obj):
        return obj.composite_hash[:16] + '…'
    fp_short.short_description = 'Fingerprint'

    def has_add_permission(self, request):              return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ('name',)


@admin.register(Aspirant)
class AspirantAdmin(admin.ModelAdmin):
    list_display = ('image_preview', 'name', 'portfolio')
    # list_display    = ('name', 'portfolio', 'vote_count')
    list_filter     = ('portfolio',)
    readonly_fields = ('vote_count',)

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="width:40px;height:40px;'
                'border-radius:50%;object-fit:cover;">',
                obj.image.url
            )
        return "—"

    image_preview.short_description = "Photo"


@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    list_display    = ('aspirant', 'voter_id_col', 'timestamp')
    list_filter     = ('aspirant__portfolio',)
    readonly_fields = ('voter', 'aspirant', 'portfolio', 'timestamp')

    def voter_id_col(self, obj):
        return obj.voter.matric_number
    voter_id_col.short_description = 'Student ID'

    def has_add_permission(self, request):              return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


class VoterUploadForm(forms.Form):
    """Simple file-upload form used by the custom voter-list upload view."""
    file = forms.FileField(
        label="Voter list file",
        help_text="Accepted formats: .xlsx, .xls, .docx, .pdf",
    )


@admin.register(VoterList)
class VoterListAdmin(admin.ModelAdmin):
    """
    Two upload paths:
      A) Standard Django admin "Add" form (VoterList model add page) — unchanged.
      B) Custom /upload/ view linked from the changelist via the
         "Upload Voter List" button — mirrors the original upload_voterlist.html
         interface and registers the admin:upload-voters URL.
    """
    list_display         = ('file', 'uploaded_at')
    change_list_template = 'admin/election/voterlist/change_list.html'
    ALLOWED_EXT          = {'.xlsx', '.xls', '.docx', '.pdf'}

    # ── Custom URL: /admin/election/voterlist/upload/ ─────────────────────

    def get_urls(self):
        from django.urls import path
        urls        = super().get_urls()
        custom_urls = [
            path(
                'upload/',
                self.admin_site.admin_view(self.upload_voter_list_view),
                name='upload-voters',          # resolves as admin:upload-voters
            ),
        ]
        return custom_urls + urls

    def upload_voter_list_view(self, request):
        """
        Standalone upload view rendered by upload_voterlist.html.
        Handles both GET (show form) and POST (process file).
        Identical processing logic to save_model — kept in one place here.
        """
        import os
        from django.shortcuts import render, redirect

        if request.method == 'POST':
            upload_form = VoterUploadForm(request.POST, request.FILES)
            if upload_form.is_valid():
                uploaded = request.FILES['file']
                _, ext   = os.path.splitext(uploaded.name)

                if ext.lower() not in self.ALLOWED_EXT:
                    messages.error(
                        request,
                        f"Unsupported file type '{ext}'. "
                        f"Please upload .xlsx, .xls, .docx, or .pdf."
                    )
                else:
                    # Persist the file via the VoterList model so it lands in
                    # MEDIA_ROOT/voter_files/ and appears in the changelist.
                    voter_list_obj = VoterList(file=uploaded)
                    voter_list_obj.save()

                    try:
                        path_ = voter_list_obj.file.path
                        if ext.lower() in ('.xlsx', '.xls'):
                            data = extract_voters_from_excel(path_)
                        elif ext.lower() == '.docx':
                            data = extract_voters_from_word(path_)
                        else:
                            data = extract_voters_from_pdf(path_)

                        count = save_voter_list(data)
                        messages.success(request, f"Imported {count} new voter(s).")
                        AuditLog.objects.create(
                            action='voter_imported',
                            actor=request.user.username,
                            detail=f"Imported {count} voters from {uploaded.name}",
                        )
                    except Exception as e:
                        logger.error(
                            f"Import error {uploaded.name}: {e}", exc_info=True
                        )
                        messages.error(request, f"Error processing file: {e}")

                return redirect('admin:election_voterlist_changelist')
        else:
            upload_form = VoterUploadForm()

        context = {
            **self.admin_site.each_context(request),
            'form':        upload_form,
            'title':       'Upload Voter List',
            'opts':        self.model._meta,
        }
        return render(request, 'admin/election/voterlist/upload.html', context)

    # ── Standard Django admin "Add" path (save_model) ─────────────────────

    def save_model(self, request, obj, form, change):
        import os
        _, ext = os.path.splitext(obj.file.name)
        if ext.lower() not in self.ALLOWED_EXT:
            messages.error(request, f"Unsupported file type '{ext}'.")
            return
        super().save_model(request, obj, form, change)
        try:
            path_ = obj.file.path
            if ext.lower() in ('.xlsx', '.xls'):
                data = extract_voters_from_excel(path_)
            elif ext.lower() == '.docx':
                data = extract_voters_from_word(path_)
            else:
                data = extract_voters_from_pdf(path_)
            count = save_voter_list(data)
            messages.success(request, f"Imported {count} new voter(s).")
            AuditLog.objects.create(
                action='voter_imported', actor=request.user.username,
                detail=f"Imported {count} voters from {obj.file.name}",
            )
        except Exception as e:
            logger.error(f"Import error {obj.file.name}: {e}", exc_info=True)
            messages.error(request, f"Error processing file: {e}")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display    = ('timestamp', 'action', 'actor', 'ip_address', 'detail_short')
    list_filter     = ('action',)
    search_fields   = ('actor', 'ip_address', 'detail')
    readonly_fields = ('action', 'actor', 'detail', 'ip_address', 'timestamp')

    def detail_short(self, obj):
        return obj.detail[:80] + ('…' if len(obj.detail) > 80 else '')
    detail_short.short_description = 'Detail'

    def has_add_permission(self, request):              return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False