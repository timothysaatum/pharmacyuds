from django import forms
from .models import ClassGroup, Portfolio, Aspirant
from django.utils.safestring import mark_safe

class AspirantRadioSelect(forms.Widget):
    def render(self, name, value, attrs=None, renderer=None):
        output = []
        # Assuming the field 'choices' is being passed in the form field
        for option_value, option_label in self.choices:
            aspirant = Aspirant.objects.get(id=option_value)
            img_tag = f'<img src="{aspirant.image.url}" alt="{aspirant.name}" style="width: 50px; height: 50px; margin-right: 10px;" />' if aspirant.image else ''
            output.append(f'''
                <div class="form-check">
                    <input type="radio" name="{name}" value="{option_value}" class="form-check-input" id="aspirant_{option_value}">
                    <label class="form-check-label" for="aspirant_{option_value}">
                        {img_tag}
                        {aspirant.name}
                    </label>
                </div>
            ''')
        return mark_safe(''.join(output))

class VoterVerificationForm(forms.Form):
    class_group = forms.ModelChoiceField(queryset=ClassGroup.objects.none(), label="Your Class")
    matric_number = forms.CharField(label="Your Student ID")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['class_group'].queryset = ClassGroup.objects.all()

class VoteForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import Portfolio, Aspirant  # import here to avoid circular imports

        for portfolio in Portfolio.objects.all():
            aspirants = Aspirant.objects.filter(portfolio=portfolio)
            
            # For portfolios with only one candidate, use yes/no endorsement
            if aspirants.count() == 1:
                single_aspirant = aspirants.first()
                self.fields[f"portfolio_{portfolio.id}"] = forms.ChoiceField(
                    label=portfolio.name,
                    choices=[
                        ('yes', single_aspirant),  # Store 'yes' as value and aspirant object for display
                        ('no', single_aspirant),   # Store 'no' as value and aspirant object for display
                    ],
                    widget=forms.RadioSelect,
                    required=True
                )
            else:
                # Multiple candidates - regular voting
                choices = [(aspirant.id, aspirant) for aspirant in aspirants]  # pass aspirant object
                self.fields[f"portfolio_{portfolio.id}"] = forms.ChoiceField(
                    label=portfolio.name,
                    choices=choices,
                    widget=forms.RadioSelect,
                    required=True
                )