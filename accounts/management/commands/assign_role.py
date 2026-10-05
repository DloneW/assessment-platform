from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from authorization.roles import ALL_ROLES, assign_role


class Command(BaseCommand):
    help = (
        "Give a user exactly one application role (STUDENT, LECTURER or ADMIN), replacing any "
        "other. A trusted, server-side way to create privileged accounts. It does NOT touch "
        "is_staff or is_superuser."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("role", type=str.upper, choices=ALL_ROLES)

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError(f"No user with username {options['username']!r}.") from None
        assign_role(user, options["role"])
        self.stdout.write(self.style.SUCCESS(f"{user.username} now has role {options['role']}."))
