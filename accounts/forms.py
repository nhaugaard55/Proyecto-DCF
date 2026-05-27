from django import forms
from django.contrib.auth import authenticate, get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.utils.crypto import get_random_string
from django.utils.text import slugify


User = get_user_model()


class RegisterForm(forms.Form):
    first_name = forms.CharField(
        label='Nombre',
        max_length=150,
        error_messages={'required': 'Ingresá tu nombre.'},
    )
    last_name = forms.CharField(
        label='Apellido',
        max_length=150,
        error_messages={'required': 'Ingresá tu apellido.'},
    )
    email = forms.EmailField(
        label='Email',
        error_messages={'required': 'Ingresá tu email.', 'invalid': 'Ingresá un email válido.'},
    )
    password1 = forms.CharField(
        label='Contrasena',
        strip=False,
        widget=forms.PasswordInput,
    )
    password2 = forms.CharField(
        label='Confirmar contrasena',
        strip=False,
        widget=forms.PasswordInput,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'auth-input'})
        self.fields['first_name'].widget.attrs.update({'autocomplete': 'given-name'})
        self.fields['last_name'].widget.attrs.update({'autocomplete': 'family-name'})
        self.fields['email'].widget.attrs.update({'autocomplete': 'email'})
        self.fields['password1'].widget.attrs.update({'autocomplete': 'new-password'})
        self.fields['password2'].widget.attrs.update({'autocomplete': 'new-password'})

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError('Ya existe una cuenta registrada con este email.')
        return email

    def clean_first_name(self):
        first_name = self.cleaned_data['first_name'].strip()
        if not first_name:
            raise ValidationError('Ingresá tu nombre.')
        return first_name

    def clean_last_name(self):
        last_name = self.cleaned_data['last_name'].strip()
        if not last_name:
            raise ValidationError('Ingresá tu apellido.')
        return last_name

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('password1')
        password2 = cleaned_data.get('password2')
        if password1 and password2 and password1 != password2:
            self.add_error('password2', 'Las contrasenas no coinciden.')
        if password1:
            password_validation.validate_password(password1)
        return cleaned_data

    def save(self):
        email = self.cleaned_data['email']
        user = User.objects.create_user(
            username=self._make_username(email),
            email=email,
            password=self.cleaned_data['password1'],
            first_name=self.cleaned_data['first_name'],
            last_name=self.cleaned_data['last_name'],
        )
        return user

    @staticmethod
    def _make_username(email):
        base = slugify(email.split('@', 1)[0])[:30] or 'user'
        username = f'{base}-{get_random_string(8).lower()}'
        while User.objects.filter(username=username).exists():
            username = f'{base}-{get_random_string(8).lower()}'
        return username


class EmailLoginForm(forms.Form):
    email = forms.EmailField(label='Email')
    password = forms.CharField(
        label='Contrasena',
        strip=False,
        widget=forms.PasswordInput,
    )

    error_messages = {
        'invalid_login': 'Email o contrasena incorrectos.',
        'inactive': 'Esta cuenta esta inactiva.',
    }

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'auth-input'})
        self.fields['email'].widget.attrs.update({'autocomplete': 'email'})
        self.fields['password'].widget.attrs.update({'autocomplete': 'current-password'})

    def clean(self):
        cleaned_data = super().clean()
        email = cleaned_data.get('email')
        password = cleaned_data.get('password')

        if email and password:
            user = User.objects.filter(email__iexact=email.strip()).order_by('id').first()
            if user is None:
                raise ValidationError(self.error_messages['invalid_login'], code='invalid_login')

            self.user_cache = authenticate(
                self.request,
                username=user.get_username(),
                password=password,
            )
            if self.user_cache is None:
                raise ValidationError(self.error_messages['invalid_login'], code='invalid_login')
            if not self.user_cache.is_active:
                raise ValidationError(self.error_messages['inactive'], code='inactive')

        return cleaned_data

    def get_user(self):
        return self.user_cache
