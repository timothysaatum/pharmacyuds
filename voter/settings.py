from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent


# ── Security ─────────────────────────────────────────────────────────────────
SECRET_KEY = 'a-$yeo1ydhsge192enstq6283o#-eurowhs[e:dosu=7e+c+t(l0==f%^u9dyfsv2ok#njg-#+oqy2r)^rfp5^nqq#5'
DEBUG       = True
ALLOWED_HOSTS = ['*']


# ── Installed apps ────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'election',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'voter.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'voter.wsgi.application'


# ── Database (SQLite for now — swap to Postgres for production) ───────────────
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# ── Password validation ───────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# ── Internationalisation ──────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'Africa/Accra'   # Ghana time (GMT+0, no DST)
USE_I18N = True
USE_TZ   = True


# ── Static & media ────────────────────────────────────────────────────────────
STATIC_URL  = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'static')
MEDIA_URL   = '/media/'
MEDIA_ROOT  = os.path.join(BASE_DIR, 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ── Session security ──────────────────────────────────────────────────────────
# Voters are authenticated with a short-lived session token.
# These settings prevent fixation, hijacking, and overly long sessions.
SESSION_COOKIE_HTTPONLY         = True
SESSION_COOKIE_SECURE           = False   # ← set True when served over HTTPS
SESSION_COOKIE_SAMESITE         = 'Lax'
SESSION_COOKIE_AGE              = 1800    # 30 minutes — enough time to vote
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE   = False   # ← set True when served over HTTPS


# ── Cache — required for rate limiting in views.py ───────────────────────────
# Uses the Django database cache so no Redis or Memcached is needed.
# Run once after first migration:  python manage.py createcachetable
CACHES = {
    'default': {
        'BACKEND':  'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'django_cache_table',
    }
}


# ── Security headers ──────────────────────────────────────────────────────────
X_FRAME_OPTIONS             = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True


# ── File upload limits ────────────────────────────────────────────────────────
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024   # 5 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024   # 5 MB


# ── SMS — Arkesel (direct, same credentials as FastAPI service) ───────────────
#
# How it works:
#   Django calls Arkesel directly using the same GET-request pattern
#   as ArkeselSMSClient.send_sms() in your FastAPI background_tasks.py.
#   No FastAPI intermediary — both apps share the same Arkesel account.
#
# To go live on election day:
#   1. Change SMS_BACKEND to 'arkesel'
#   2. Confirm SMS_ARKESEL_API_KEY and SMS_SENDER_ID match your .env
#
SMS_BACKEND          = 'dummy'                           # → 'arkesel' on election day
SMS_ARKESEL_API_KEY  = 'ekV0ZGp1YmFIQUViQkphR3JxaWY'   # = ARKESEL_API_KEY in FastAPI .env
SMS_ARKESEL_BASE_URL = 'https://sms.arkesel.com/sms/api' # = ARKESEL_BASE_URL in FastAPI .env
SMS_SENDER_ID        = 'GPSA-EC-UDS'                    # = ARKESEL_DEFAULT_SENDER_ID in FastAPI .env

DISABLE_FP_CHECK = False  # Set True to disable fingerprint mismatch logging (for testing only)
# ── Logging ───────────────────────────────────────────────────────────────────
# Creates logs/election.log — run  mkdir logs  in your project root first.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'class':     'logging.FileHandler',
            'filename':  BASE_DIR / 'logs' / 'election.log',
            'formatter': 'verbose',
        },
        'console': {
            'class':     'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'election': {
            'handlers':  ['file', 'console'],
            'level':     'INFO',
            'propagate': False,
        },
    },
}