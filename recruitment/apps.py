from django.apps import AppConfig


class RecruitmentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "recruitment"
    verbose_name = "RecruitGuard Recruitment"

    def ready(self):
        from . import signals  # noqa: F401
