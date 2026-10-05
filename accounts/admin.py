from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Course, Department, Enrollment, Level, StudentProfile, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "is_staff", "is_active")
    list_filter = ("is_staff", "is_superuser", "is_active", "groups")
    search_fields = ("username", "email", "first_name", "last_name")
    # Make email part of the add form too (it is required and unique).
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Contact", {"fields": ("email", "first_name", "last_name")}),
    )

    # Application roles are Django groups, and staff/superuser flags control Django admin
    # itself. Only superusers may change any of these; otherwise a staff member with plain
    # "change user" permission could promote themselves (or anyone) to ADMIN.
    PRIVILEGE_FIELDS = ("is_staff", "is_superuser", "groups", "user_permissions")

    def get_readonly_fields(self, request, obj=None):
        readonly = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser:
            readonly = (*readonly, *self.PRIVILEGE_FIELDS)
        return readonly


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "created_at")
    search_fields = ("code", "name")


@admin.register(Level)
class LevelAdmin(admin.ModelAdmin):
    list_display = ("number", "created_at")


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "department", "level", "is_active")
    list_filter = ("department", "level", "is_active")
    search_fields = ("code", "title")
    filter_horizontal = ("lecturers",)


class EnrollmentInline(admin.TabularInline):
    model = Enrollment
    extra = 0


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("student_id", "user", "department", "level")
    list_filter = ("department", "level")
    search_fields = ("student_id", "user__username", "user__email", "user__first_name", "user__last_name")
    raw_id_fields = ("user",)
    inlines = [EnrollmentInline]


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ("student", "course", "enrolled_at")
    list_filter = ("course__department", "course__level")
    search_fields = ("student__student_id", "course__code")
    raw_id_fields = ("student",)
