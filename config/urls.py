from pathlib import Path

from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

TESLA_PUBLIC_KEY = (Path(__file__).parent / "well_known" / "com.tesla.3p.public-key.pem").read_text()


def tesla_public_key(request):
    # Tesla Fleet API: the app's public key must be served from the registered domain
    return HttpResponse(TESLA_PUBLIC_KEY, content_type="application/x-pem-file")


urlpatterns = [
    path(".well-known/appspecific/com.tesla.3p.public-key.pem", tesla_public_key),
    path("admin/", admin.site.urls),
    path("", include("prices.urls")),
]
