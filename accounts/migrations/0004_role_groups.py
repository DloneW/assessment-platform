from django.db import migrations

ROLES = ("STUDENT", "LECTURER", "ADMIN")


def create_role_groups_and_backfill_students(apps, schema_editor):
    """Create the three role groups and give existing Day 1 students the STUDENT role."""
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("accounts", "User")

    groups = {name: Group.objects.get_or_create(name=name)[0] for name in ROLES}

    # Only users that have a student profile and no application role yet.
    legacy_students = User.objects.filter(student_profile__isnull=False).exclude(
        groups__name__in=ROLES
    )
    for user in legacy_students:
        user.groups.add(groups["STUDENT"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_enrollment_lecturers_course_state"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        # Reverse is a no-op: removing groups on rollback could silently drop role assignments.
        migrations.RunPython(create_role_groups_and_backfill_students, migrations.RunPython.noop),
    ]
