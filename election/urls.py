from django.urls import path
from . import views

app_name = 'election'

urlpatterns = [
    path('', views.verify_voter, name='verify_voter'),
    path('vote/', views.vote, name='vote_candidates'),
    path('api/live-results/', views.live_results_api, name='live_results'),
    path('results/', views.results_page, name='results'),
]