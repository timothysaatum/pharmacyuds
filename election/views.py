from django.shortcuts import render, redirect
from django.contrib import messages
from .models import Voter, Vote, Aspirant, Portfolio
from .forms import VoterVerificationForm, VoteForm
from django.utils.timezone import now
from django.http import JsonResponse
from django.utils import timezone



def verify_voter(request):
    if request.method == 'POST':
        form = VoterVerificationForm(request.POST)
        if form.is_valid():
            matric_number = form.cleaned_data['matric_number']
            class_group = form.cleaned_data['class_group']
            try:
                voter = Voter.objects.get(matric_number=matric_number, class_group=class_group)
                if voter.has_voted:
                    messages.warning(request, "You have already voted.")
                    return redirect('election:verify_voter')
                request.session['voter_id'] = voter.id
                return redirect('election:vote_candidates')
            except Voter.DoesNotExist:
                messages.error(request, "You are not a registered voter for this class.")
    else:
        form = VoterVerificationForm()

    return render(request, 'election/voter_form.html', {'form': form})



def vote(request):
    voter_id = request.session.get('voter_id')
    if not voter_id:
        return redirect('verify_voter')

    voter = Voter.objects.get(id=voter_id)
    if voter.has_voted:
        messages.warning(request, "You have already voted.")
        return redirect('election:verify_voter')

    if request.method == 'POST':
        form = VoteForm(request.POST)
        if form.is_valid():
            for key, aspirant in form.cleaned_data.items():
                print(f"Voting for aspirant ID: {aspirant}")
                aspirant = Aspirant.objects.get(id=aspirant)
                Vote.objects.create(voter=voter, aspirant=aspirant, timestamp=now())
            voter.has_voted = True
            voter.save()
            messages.success(request, "Your vote has been recorded.")
            return redirect('election:verify_voter')
        else:
            print(f"Form errors: {form.errors}")  # Print form errors if any
    else:
        form = VoteForm()
    
    return render(request, 'election/vote_candidates.html', {'form': form})



# def live_results(request):
#     portfolios = Portfolio.objects.all()
#     results = []
#     for portfolio in portfolios:
#         aspirants = Aspirant.objects.filter(portfolio=portfolio)
#         aspirant_results = []
#         for asp in aspirants:
#             vote_count = Vote.objects.filter(aspirant=asp).count()
#             aspirant_results.append({'aspirant': asp.name, 'votes': vote_count})
#         results.append({'portfolio': portfolio.name, 'aspirants': aspirant_results})
#     return JsonResponse({'results': results})
def results_page(request):
    """Renders the initial results HTML page"""
    portfolios = Portfolio.objects.all().prefetch_related('aspirants')
    return render(request, 'election/live_results.html', {
        'portfolios': portfolios,
    })

def live_results_api(request):
    """API endpoint that returns JSON data for live results"""
    portfolios = Portfolio.objects.all().prefetch_related('aspirants')
    
    results = []
    for portfolio in portfolios:
        # Get aspirants for this portfolio
        aspirants = portfolio.aspirants.all()
        
        # Calculate total votes for percentage calculation
        total_votes = Vote.objects.filter(aspirant__portfolio=portfolio).count()
        
        # Get results for each aspirant
        aspirant_results = []
        for aspirant in aspirants:
            vote_count = Vote.objects.filter(aspirant=aspirant).count()
            
            # Calculate percentage of votes
            percentage = 0
            if total_votes > 0:
                percentage = (vote_count / total_votes) * 100
            
            aspirant_results.append({
                'id': aspirant.id,
                'name': aspirant.name,
                'image_url': aspirant.image.url if aspirant.image else None,
                'votes': vote_count,
                'percentage': round(percentage, 1)
            })
        
        # Sort aspirants by vote count (descending)
        aspirant_results = sorted(aspirant_results, key=lambda x: x['votes'], reverse=True)
        
        results.append({
            'id': portfolio.id,
            'name': portfolio.name,
            'aspirants': aspirant_results,
            'total_votes': total_votes
        })
    
    return JsonResponse({
        'results': results,
        'last_updated': timezone.now().isoformat()
    })