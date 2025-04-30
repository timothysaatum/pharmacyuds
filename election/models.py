from django.db import models


class ClassGroup(models.Model):
    name = models.CharField(max_length=10, unique=True)

    def __str__(self):
        return self.name

class Voter(models.Model):
    matric_number = models.CharField(max_length=50, unique=True)
    class_group = models.ForeignKey(ClassGroup, on_delete=models.CASCADE)
    has_voted = models.BooleanField(default=False, editable=False)

    def __str__(self):
        return self.matric_number

class Portfolio(models.Model):
    # image = models.ImageField(upload_to='portfolio_images/', null=True, blank=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name
        

class Aspirant(models.Model):
    name = models.CharField(max_length=255)
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='aspirants')
    image = models.ImageField(upload_to='aspirant_images/', null=True, blank=True)

    def __str__(self):
        return f"{self.name} - {self.portfolio.name}"
    

    def vote_count(self):
        return self.vote_set.count()

    vote_count.short_description = 'Votes'


class Vote(models.Model):
    voter = models.ForeignKey(Voter, on_delete=models.CASCADE)
    aspirant = models.ForeignKey(Aspirant, on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('voter', 'aspirant')

    
    # def vote_count(self):
    #     return self.vote_set.count()
    
    # vote_count.short_description = 'Votes'
 

class VoterList(models.Model):
    file = models.FileField(upload_to='voter_files/')
    uploaded_at = models.DateTimeField(auto_now_add=True)