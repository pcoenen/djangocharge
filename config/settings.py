"""Django settings for DjangoCharge.

Configuration comes from environment variables (Appliku config vars in
production, a local `.env` file in development).
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="django-insecure-dev-only-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[f"https://{h}" for h in ALLOWED_HOSTS if h not in ("*", "localhost", "127.0.0.1")],
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "prices",
    "tesla",
    "energy",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Everything requires a login, except views marked @login_not_required
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres:///djangocharge"),
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=600)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Brussels"
USE_I18N = True
USE_TZ = True

STATIC_URL = env.str("STATIC_URL", default="/static/")
STATIC_ROOT = env.str("STATIC_ROOT", default=str(BASE_DIR / "staticfiles"))

WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "prices:index"
LOGOUT_REDIRECT_URL = "login"

# Appliku terminates TLS at nginx
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# --- Dyncharge settings ---
ENTSOE_API_URL = "https://web-api.tp.entsoe.eu/api"
ENTSOE_TOKEN = env("ENTSOE_TOKEN", default="")
ENTSOE_AREA = "10YBE----------2"  # Belgium bidding zone

# Car charging parameters (from the original PHP implementation)
CAR_BATTERY_KWH = 60
CAR_CHARGE_AMPS = 5
CAR_CHARGE_VOLTS = 230
CAR_CHARGE_PHASES = 3
CAR_CHARGE_EFFICIENCY = 0.92
CAR_CHARGE_LOSS_FACTOR = 1.10  # kWh drawn from grid per kWh in battery
GRID_FEE_PER_KWH = 0.13  # EUR/kWh on top of the energy price

# --- Tesla Fleet API ---
TESLA_CLIENT_ID = env("TESLA_CLIENT_ID", default="")
TESLA_CLIENT_SECRET = env("TESLA_CLIENT_SECRET", default="")
TESLA_REGION = "eu"
TESLA_API_URL = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TESLA_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
TESLA_SCOPES = "openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds"
# Must match an allowed redirect URI of the app on developer.tesla.com
TESLA_REDIRECT_URI = env("TESLA_REDIRECT_URI", default="https://dyncharge.oraston.be/tesla-api/redirect.php")
TESLA_DOMAIN = env("TESLA_DOMAIN", default="dyncharge.oraston.be")
# Private key of the pair whose public key is served at /.well-known/, base64-encoded PEM
TESLA_PRIVATE_KEY_B64 = env("TESLA_PRIVATE_KEY_B64", default="")
# Charge schedules only apply at this location (home)
TESLA_HOME_LAT = env.float("TESLA_HOME_LAT", default=0.0)
TESLA_HOME_LON = env.float("TESLA_HOME_LON", default=0.0)

# --- Energy manager ---------------------------------------------------------------
# .ics link whose events need the car at 40% half an hour before they start (phase 4)
CHARGE_CALENDAR_URL = env("CHARGE_CALENDAR_URL", default="")

# Notifications: Maileroo SMTP, same account as preference_planner. Without
# EMAIL_HOST_PASSWORD they're only logged. Nothing is sent without NOTIFY_EMAIL.
EMAIL_HOST = "smtp.maileroo.com"
EMAIL_PORT = 587  # 587 for TLS, 465 for SSL
EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
EMAIL_HOST_USER = env.str("EMAIL_HOST_USER", default="info@preferenceplanner.com")
EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD", default="")
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST_PASSWORD
    else "django.core.mail.backends.console.EmailBackend"
)
EMAIL_TIMEOUT = 30
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default=f"Dyncharge <{EMAIL_HOST_USER}>")
NOTIFY_EMAIL = env.list("NOTIFY_EMAIL", default=[])  # comma-separated
