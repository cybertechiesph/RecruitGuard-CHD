"""Round F3: Position References — the administrator adds and edits the references every
vacancy is built on; nothing is deleted, a reference is retired instead
(docs/admin-area-rebuild-plan.md section 4.7).
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import redirect
from django.views.generic import CreateView, ListView, UpdateView

from ..forms import PositionReferenceForm
from ..models import PositionReference
from ..permissions import SystemAdministratorRequiredMixin
from ..services import position_references_with_usage, save_position_reference


class PositionReferenceListView(LoginRequiredMixin, SystemAdministratorRequiredMixin, ListView):
    template_name = "recruitment/admin/position_references.html"
    context_object_name = "references"
    paginate_by = 50

    def get_queryset(self):
        queryset = position_references_with_usage()
        self.q = (self.request.GET.get("q") or "").strip()
        if self.q:
            queryset = queryset.filter(
                Q(position_title__icontains=self.q)
                | Q(class_id__icontains=self.q)
                | Q(occupational_service__icontains=self.q)
            )
        self.status = self.request.GET.get("status") or ""
        if self.status == "active":
            queryset = queryset.filter(is_active=True)
        elif self.status == "retired":
            queryset = queryset.filter(is_active=False)
        else:
            self.status = ""
        return queryset.order_by("position_title", "salary_grade")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        everything = PositionReference.objects.all()
        context.update(
            {
                "q": self.q,
                "status": self.status,
                "is_filtered": bool(self.q or self.status),
                "total_count": everything.count(),
                "active_count": everything.filter(is_active=True).count(),
                "retired_count": everything.filter(is_active=False).count(),
            }
        )
        return context


class _PositionReferenceFormMixin:
    model = PositionReference
    form_class = PositionReferenceForm
    template_name = "recruitment/admin/position_reference_form.html"

    def form_valid(self, form):
        creating = form.instance.pk is None
        self.object = save_position_reference(form, self.request.user)
        if creating:
            messages.success(self.request, f"Added {self.object.position_title}. It is now offered when creating vacancies.")
        elif "is_active" in form.changed_data and not self.object.is_active:
            messages.success(self.request, f"Retired {self.object.position_title}. Existing vacancies keep it.")
        else:
            messages.success(self.request, f"Saved {self.object.position_title}.")
        return redirect("position-reference-list")


class PositionReferenceCreateView(
    LoginRequiredMixin, SystemAdministratorRequiredMixin, _PositionReferenceFormMixin, CreateView
):
    pass


class PositionReferenceUpdateView(
    LoginRequiredMixin, SystemAdministratorRequiredMixin, _PositionReferenceFormMixin, UpdateView
):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["vacancy_count"] = self.object.recruitment_entries.count()
        return context
