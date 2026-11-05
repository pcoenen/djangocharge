from pathlib import Path

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse
from django.urls import include, path

from tesla import views as tesla_views

TESLA_PUBLIC_KEY = (Path(__file__).parent / "well_known" / "com.tesla.3p.public-key.pem").read_text()


@login_not_required
def tesla_public_key(request):
    # Tesla Fleet API: the app's public key must be served from the registered domain
    return HttpResponse(TESLA_PUBLIC_KEY, content_type="application/x-pem-file")


urlpatterns = [
    path(".well-known/appspecific/com.tesla.3p.public-key.pem", tesla_public_key),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
    path("tesla/", include("tesla.urls")),
    path("energy/", include("energy.urls")),
    # The OAuth redirect URI registered on developer.tesla.com (from the PHP version)
    path("tesla-api/redirect.php", tesla_views.callback),
    path("", include("prices.urls")),
]
