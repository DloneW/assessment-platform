import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def copy_m2m_rows_to_enrollments(apps, schema_editor):
    """Preserve existing student<->course rows when switching to the explicit Enrollment model."""
    StudentProfile = apps.get_model("accounts", "StudentProfile")
    Enrollment = apps.get_model("accounts", "Enrollment")
    OldThrough = StudentProfile.courses.through
    Enrollment.objects.bulk_create(
        [
            Enrollment(student_id=row.studentprofile_id, course_id=row.course_id)
            for row in OldThrough.objects.all()
        ]
    )


def copy_enrollments_back_to_m2m_rows(apps, schema_editor):
    StudentProfile = apps.get_model("accounts", "StudentProfile")
    Enrollment = apps.get_model("accounts", "Enrollment")
    OldThrough = StudentProfile.courses.through
    OldThrough.objects.bulk_create(
        [
            OldThrough(studentprofile_id=e.student_id, course_id=e.course_id)
            for e in Enrollment.objects.all()
        ]
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_studentprofile_courses_ci_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="course",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="course",
            name="lecturers",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"groups__name": "LECTURER"},
                related_name="taught_courses",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="Enrollment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("enrolled_at", models.DateTimeField(auto_now_add=True)),
                (
                    "course",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="enrollments",
                        to="accounts.course",
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="enrollments",
                        to="accounts.studentprofile",
                    ),
                ),
            ],
            options={
                "ordering": ["course__code"],
            },
        ),
        migrations.AddConstraint(
            model_name="enrollment",
            constraint=models.UniqueConstraint(
                fields=("student", "course"),
                name="accounts_enrollment_student_course_unique",
            ),
        ),
        # Django cannot ALTER an auto M2M into a `through` M2M, so: copy the rows,
        # drop the old auto field (and its table), then add the field back with `through`.
        migrations.RunPython(copy_m2m_rows_to_enrollments, copy_enrollments_back_to_m2m_rows),
        migrations.RemoveField(
            model_name="studentprofile",
            name="courses",
        ),
        migrations.AddField(
            model_name="studentprofile",
            name="courses",
            field=models.ManyToManyField(
                blank=True,
                related_name="students",
                through="accounts.Enrollment",
                to="accounts.course",
            ),
        ),
    ]
