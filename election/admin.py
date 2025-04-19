from django.contrib import admin
from .models import ClassGroup, Voter, Portfolio, Aspirant, Vote, VoterList
from .utils import extract_voters_from_excel, extract_voters_from_word, extract_voters_from_pdf, save_voter_list
from django.utils.html import format_html



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
    list_display = ('name', 'portfolio', 'image_preview')

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="width: 40px; height: auto;" />', obj.image.url)
        return "-"
    image_preview.short_description = "Image"


@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    list_display = ('voter', 'aspirant', 'timestamp')


@admin.register(VoterList)
class VoterFileUploadAdmin(admin.ModelAdmin):
    list_display = ('file', 'uploaded_at')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        file = obj.file
        if file.name.endswith('.xlsx') or file.name.endswith('.xls'):
            data = extract_voters_from_excel(file)
        elif file.name.endswith('.docx'):
            data = extract_voters_from_word(file)
        elif file.name.endswith('.pdf'):
            data = extract_voters_from_pdf(file)
        else:
            data = []
        save_voter_list(data)