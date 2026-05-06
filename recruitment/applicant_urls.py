from django.urls import path

from .portal_views import (
    ApplicantHelpView,
    ApplicantOTPView,
    ApplicantPortalIntakeView,
    ApplicantPortalView,
    ApplicantReceiptView,
    ApplicantStatusLookupView,
    ApplicantVacancyDetailView,
)


urlpatterns = [
    path("", ApplicantPortalView.as_view(), name="applicant-portal"),
    path("status/", ApplicantStatusLookupView.as_view(), name="applicant-status-lookup"),
    path("help/", ApplicantHelpView.as_view(), name="applicant-help"),
    path("entries/<int:pk>/", ApplicantVacancyDetailView.as_view(), name="applicant-vacancy-detail"),
    path("entries/<int:pk>/apply/", ApplicantPortalIntakeView.as_view(), name="applicant-intake"),
    path("<uuid:token>/otp/", ApplicantOTPView.as_view(), name="applicant-otp"),
    path("<uuid:token>/receipt/", ApplicantReceiptView.as_view(), name="applicant-receipt"),
]
