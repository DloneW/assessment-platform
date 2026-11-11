import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_department_no_code_must_change_password"),
        ("assessments", "0002_attempt_assessments_attempt_submitted_after_start_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="question",
            name="question_type",
            field=models.CharField(
                choices=[("MULTIPLE_CHOICE", "Multiple choice"), ("TRUE_FALSE", "True / false")],
                default="MULTIPLE_CHOICE",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="QuestionBankQuestion",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                ("text", models.TextField()),
                (
                    "question_type",
                    models.CharField(
                        choices=[("MULTIPLE_CHOICE", "Multiple choice"), ("TRUE_FALSE", "True / false")],
                        default="MULTIPLE_CHOICE",
                        max_length=20,
                    ),
                ),
                ("category", models.CharField(blank=True, max_length=100)),
                (
                    "marks",
                    models.PositiveSmallIntegerField(
                        default=1,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                ("choices", models.JSONField()),
                ("correct_choice", models.PositiveSmallIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "course",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bank_questions",
                        to="accounts.course",
                    ),
                ),
                (
                    "creator",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bank_questions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["category", "text"]},
        ),
        migrations.AddConstraint(
            model_name="questionbankquestion",
            constraint=models.CheckConstraint(
                condition=models.Q(("marks__gte", 1)),
                name="assessments_bank_marks_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="questionbankquestion",
            constraint=models.CheckConstraint(
                condition=models.Q(("correct_choice__gte", 1)),
                name="assessments_bank_correct_choice_positive",
            ),
        ),
        migrations.CreateModel(
            name="ImportHistory",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                (
                    "file_name",
                    models.CharField(max_length=255),
                ),
                ("file_hash", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PREVIEWED", "Preview ready"),
                            ("COMPLETED", "Imported"),
                            ("FAILED", "Failed"),
                        ],
                        default="PREVIEWED",
                        max_length=10,
                    ),
                ),
                ("question_count", models.PositiveSmallIntegerField(default=0)),
                ("details", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "course",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assessment_imports",
                        to="accounts.course",
                    ),
                ),
                (
                    "creator",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assessment_imports",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="importhistory",
            constraint=models.UniqueConstraint(
                fields=("course", "creator", "file_hash"),
                name="assessments_import_file_once",
            ),
        ),
    ]
