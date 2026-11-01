from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    # set_language: the English / Filipino switch on applicant pages (POST, CSRF-protected).
    path("i18n/", include("django.conf.urls.i18n")),
    path("", RedirectView.as_view(pattern_name="applicant-portal", permanent=False)),
    path("", include("recruitment.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
