from django.urls import include, path


urlpatterns = [
    path("apply/", include("recruitment.applicant_urls")),
    path("internal/", include("recruitment.internal_urls")),
]
