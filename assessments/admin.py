from django.contrib import admin

from .models import Attempt, Choice, Question, StudentAnswer, Test


class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 0


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    __test__ = False
    list_display = ("title", "course", "creator", "status", "duration_minutes", "published_at")
    list_filter = ("status", "course")
    search_fields = ("title",)
    # Publication state is set by the publish workflow (which validates the test), not by hand.
    readonly_fields = ("status", "published_at", "created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        """Once a test exists its course and creator are fixed; once published, everything is."""
        fields = tuple(super().get_readonly_fields(request, obj))
        if obj is None:
            return fields
        if obj.is_draft:
            return fields + ("course", "creator")
        return fields + tuple(f.name for f in Test._meta.fields if f.name not in fields)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("test", "order", "text", "marks")
    inlines = [ChoiceInline]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Questions can only be attached to (or moved to) a draft test: a published test is frozen.
        if db_field.name == "test":
            kwargs["queryset"] = Test.objects.filter(status=Test.Status.DRAFT)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _locked(self, obj):
        return obj is not None and not obj.test.is_draft

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not self._locked(obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not self._locked(obj)


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    """Read-only audit view: attempts are created and scored only by the assessment workflow."""

    list_display = ("student", "test", "status", "score", "max_score", "started_at", "submitted_at")
    list_filter = ("status",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StudentAnswer)
class StudentAnswerAdmin(admin.ModelAdmin):
    list_display = ("attempt", "question", "choice", "awarded_marks", "answered_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
