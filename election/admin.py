from django.contrib import admin
from .models import ClassGroup, Voter, Portfolio, Aspirant, Vote, VoterList
from .utils import extract_voters_from_excel, extract_voters_from_word, extract_voters_from_pdf, save_voter_list
from django.utils.html import format_html
from django.http import HttpResponse
from django.utils.html import format_html
from reportlab.pdfgen import canvas
from django.utils import timezone
import csv
from io import BytesIO



@admin.register(ClassGroup)
class ClassGroupAdmin(admin.ModelAdmin):
    list_display = ('name',)

@admin.register(Voter)
class VoterAdmin(admin.ModelAdmin):
    list_display = ('matric_number', 'class_group', 'has_voted')
    list_filter = ('class_group', 'has_voted')


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ('name',)

    # def image_preview(self, obj):
    #     if obj.image:
    #         return format_html('<img src="{}" style="width: 50px; height:auto;" />', obj.image.url)
    #     return "-"
    # image_preview.short_description = "Image"


@admin.register(Aspirant)
class AspirantAdmin(admin.ModelAdmin):
    list_display = ('name', 'portfolio', 'vote_count', 'vote_percentage', 'image_preview')
    list_filter = ('portfolio',)
    actions = ['export_as_csv', 'export_as_pdf']
    readonly_fields = ('image_preview', 'vote_count', 'vote_percentage')
    
    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="width: 40px; height: 40px; object-fit: cover; border-radius: 50%;" />', obj.image.url)
        return format_html('<div style="width: 40px; height: 40px; background-color: #f0f0f0; border-radius: 50%; display: flex; align-items: center; justify-content: center;"><i class="fas fa-user"></i></div>')
    image_preview.short_description = "Image Preview"

    def vote_count(self, obj):
        return obj.vote_set.count()
    vote_count.short_description = 'Total Votes'
    vote_count.admin_order_field = 'vote_set__count'

    def vote_percentage(self, obj):
        total_votes = Vote.objects.filter(aspirant__portfolio=obj.portfolio).count()
        if total_votes > 0:
            percentage = (obj.vote_set.count() / total_votes) * 100
            return f"{percentage:.1f}%"
        return "0%"
    vote_percentage.short_description = 'Vote %'
    
    def export_as_csv(self, request, queryset):
        meta = self.model._meta
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename={meta.verbose_name_plural}.csv'
        
        writer = csv.writer(response)
        
        # Write headers
        writer.writerow([
            'Name', 
            'Portfolio', 
            'Total Votes', 
            'Vote Percentage',
            'Image URL'
        ])
        
        # Write data
        for obj in queryset:
            total_portfolio_votes = Vote.objects.filter(aspirant__portfolio=obj.portfolio).count()
            percentage = (obj.vote_set.count() / total_portfolio_votes * 100) if total_portfolio_votes > 0 else 0
            
            writer.writerow([
                obj.name,
                str(obj.portfolio),
                obj.vote_set.count(),
                f"{percentage:.1f}%",
                obj.image.url if obj.image else ''
            ])
        
        return response
    export_as_csv.short_description = "Export Selected as CSV"
    
    def export_as_pdf(self, request, queryset):
        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename=aspirants_report.pdf'
        
        buffer = BytesIO()
        p = canvas.Canvas(buffer)
        
        # Set up PDF document
        p.setTitle("Aspirants Voting Report")
        p.setFont("Helvetica-Bold", 16)
        p.drawString(100, 800, "Aspirants Voting Report")
        p.setFont("Helvetica", 12)
        p.drawString(100, 780, f"Generated on: {timezone.now().strftime('%Y-%m-%d %H:%M')}")
        
        # Table headers
        p.setFont("Helvetica-Bold", 10)
        headers = ["Name", "Portfolio", "Votes", "Percentage"]
        x_positions = [100, 250, 350, 450]
        
        for i, header in enumerate(headers):
            p.drawString(x_positions[i], 750, header)
        
        # Table content
        p.setFont("Helvetica", 10)
        y_position = 730
        
        for obj in queryset:
            if y_position < 50:  # Add new page if we're at the bottom
                p.showPage()
                y_position = 750
                # Redraw headers on new page
                p.setFont("Helvetica-Bold", 10)
                for i, header in enumerate(headers):
                    p.drawString(x_positions[i], y_position, header)
                y_position = 730
                p.setFont("Helvetica", 10)
            
            total_portfolio_votes = Vote.objects.filter(aspirant__portfolio=obj.portfolio).count()
            percentage = (obj.vote_set.count() / total_portfolio_votes * 100) if total_portfolio_votes > 0 else 0
            
            p.drawString(x_positions[0], y_position, obj.name)
            p.drawString(x_positions[1], y_position, str(obj.portfolio))
            p.drawString(x_positions[2], y_position, str(obj.vote_set.count()))
            p.drawString(x_positions[3], y_position, f"{percentage:.1f}%")
            
            y_position -= 20
        
        p.showPage()
        p.save()
        
        pdf = buffer.getvalue()
        buffer.close()
        response.write(pdf)
        
        return response
    export_as_pdf.short_description = "Export Selected as PDF"
    
    def get_actions(self, request):
        actions = super().get_actions(request)
        if 'delete_selected' in actions:
            del actions['delete_selected']
        return actions

    # # Prevent edits, additions, and deletions:
    # def has_add_permission(self, request):
    #     return False
    
    # def has_change_permission(self, request, obj=None):
    #     return False
    
    # def has_delete_permission(self, request, obj=None):
    #     return False


@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    list_display = ('aspirant', 'timestamp')


@admin.register(VoterList)
class VoterListAdmin(admin.ModelAdmin):
    list_display = ('file', 'uploaded_at')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        file = obj.file
        path = file.path
        # print('file =',file, 'path =',path)
        if file.name.endswith('.xlsx') or file.name.endswith('.xls'):
            data = extract_voters_from_excel(path)
        elif file.name.endswith('.docx'):
            # print("It's me")
            data = extract_voters_from_word(path)
            # print(data)
        elif file.name.endswith('.pdf'):
            data = extract_voters_from_pdf(path)
        else:
            data = []

        # print(f"Importing {len(data)} voters from {file.name}...")
        save_voter_list(data)