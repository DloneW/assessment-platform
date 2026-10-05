from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Course, Department, Level, StudentProfile, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "is_staff", "is_active")
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("username", "email", "first_name", "last_name")
    # Make email part of the add form too (it is required and unique).
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Contact", {"fields": ("email", "first_name", "last_name")}),
    )


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "created_at")
    search_fields = ("code", "name")


@admin.register(Level)
class LevelAdmin(admin.ModelAdmin):
    list_display = ("number", "created_at")


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "department", "level")
    list_filter = ("department", "level")
    search_fields = ("code", "title")


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("student_id", "user", "department", "level")
    list_filter = ("department", "level")
    search_fields = ("student_id", "user__username", "user__email", "user__first_name", "user__last_name")
    raw_id_fields = ("user",)
