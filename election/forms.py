import re
from django import forms
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from .models import ClassGroup, Aspirant, Portfolio


class AspirantRadioSelect(forms.Widget):
    """
    Custom radio widget that renders each aspirant option using
    the project template election/widgets/aspirant_option.html.

    Template context:
      name            - HTML field name
      value           - option value (aspirant ID string)
      choice_instance - live Aspirant model instance (.image, .name)
      choice_label    - aspirant display name
      checked         - True if this option is selected
    """

    def render(self, name, value, attrs=None, renderer=None):
        fragments = []
        for option_value, _option_label in self.choices:
            try:
                aspirant = Aspirant.objects.get(id=option_value)
            except Aspirant.DoesNotExist:
                continue
            html = render_to_string(
                'election/widgets/aspirant_option.html',
                {
                    'name':            name,
                    'value':           str(option_value),
                    'choice_instance': aspirant,
                    'choice_label':    aspirant.name,
                    'checked':         str(option_value) == str(value),
                },
            )
            fragments.append(html)
        return mark_safe(''.join(fragments))


class VoterVerificationForm(forms.Form):
    """
    3-field verification form.

    Field 1: matric_number — Student ID (sequential, guessable — NOT the secret)
    Field 2: email         — Registered email address (second identity factor)
    Field 3: sms_token     — 6-char token received via SMS (the primary secret)

    The student ID alone is insufficient for verification because IDs are
    sequential. Email + token together are what make verification secure.
    class_group is no longer required here because the level is uniform
    across all voters in this deployment.
    """
    matric_number = forms.CharField(
        label="Student ID",
        max_length=50,
        strip=True,
        help_text="e.g. PHA/0003/20",
        widget=forms.TextInput(attrs={
            'autocomplete': 'off',
            'placeholder':  'e.g. PHA/0003/20',
        }),
    )
    email = forms.EmailField(
        label="Email address",
        max_length=254,
        widget=forms.EmailInput(attrs={
            'autocomplete':   'off',
            'autocapitalize': 'none',
            'placeholder':    'your.name@uds.edu.gh',
        }),
    )
    sms_token = forms.CharField(
        label="Voting Token",
        max_length=6,
        min_length=6,
        strip=True,
        help_text="6-character token sent to your phone",
        widget=forms.TextInput(attrs={
            'autocomplete':   'off',
            'autocorrect':    'off',
            'autocapitalize': 'characters',
            'spellcheck':     'false',
            'maxlength':      '6',
            'placeholder':    'e.g. K4R7QX',
        }),
    )

    def clean_matric_number(self):
        value = self.cleaned_data['matric_number'].strip().upper()
        if not re.match(r'^[A-Z0-9/_\-]+$', value):
            raise forms.ValidationError(
                "Student ID contains invalid characters."
            )
        if len(value) < 3:
            raise forms.ValidationError("Student ID is too short.")
        return value

    def clean_sms_token(self):
        """Normalise to uppercase, reject anything not alphanumeric."""
        value = self.cleaned_data['sms_token'].strip().upper()
        if not re.match(r'^[A-Z0-9]{6}$', value):
            raise forms.ValidationError(
                "Voting token must be exactly 6 letters/digits (e.g. K4R7QX)."
            )
        return value

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()


class VoteForm(forms.Form):
    """
    Dynamically builds one radio-choice field per portfolio.
    Single-candidate portfolios get a yes/no endorsement widget.
    Uses AspirantRadioSelect to render aspirant photos inline.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for portfolio in Portfolio.objects.all().order_by('id'):
            aspirants = list(Aspirant.objects.filter(portfolio=portfolio))
            count     = len(aspirants)

            if count == 0:
                continue

            if count == 1:
                single = aspirants[0]
                field  = forms.ChoiceField(
                    label=portfolio.name,
                    choices=[
                        ('yes', f"Endorse {single.name}"),
                        ('no',  f"Do not endorse {single.name}"),
                    ],
                    widget=forms.RadioSelect,
                    required=True,
                )
                # Attach Aspirant object so template can render the photo
                field.single_aspirant = single
                field.aspirants       = []
            else:
                field = forms.ChoiceField(
                    label=portfolio.name,
                    choices=[(str(a.id), a.name) for a in aspirants],
                    widget=AspirantRadioSelect,
                    required=True,
                )
                # Attach full Aspirant list so template can render photos
                field.aspirants       = aspirants
                field.single_aspirant = None

            self.fields[f"portfolio_{portfolio.id}"] = field

    def clean(self):
        cleaned = super().clean()
        for key, value in cleaned.items():
            if key.startswith('portfolio_') and not value:
                self.add_error(key, "Please make a selection for this position.")
        return cleaned