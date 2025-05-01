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
    list_display = ('name', 'portfolio', 'vote_count', 'image_preview')
    readonly_fields = ('name', 'portfolio', 'image', 'image_preview', 'vote_count')
    
    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="width: 40px; height: auto;" />', obj.image.url)
        return "-"
    image_preview.short_description = "Image"

    def vote_count(self, obj):
        return obj.vote_set.count()
    vote_count.short_description = 'Votes'

    # Prevent edits, additions, and deletions:
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return False


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