import getpass
import os
import sys
import unicodedata

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from authorization.roles import ADMIN, assign_role


class Command(BaseCommand):
    help = (
        "Create the first application ADMIN (the in-app console user). It is NOT staff and NOT a "
        "superuser, so day-to-day administration does not need the superuser account. Values come "
        "from --username/--email, else DJANGO_APP_ADMIN_USERNAME / _EMAIL / _PASSWORD, else a prompt. "
        "Does nothing (exit 0) if the username already exists or nothing was supplied, so it is safe "
        "to run on every start. The account must change its password at first login."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username")
        parser.add_argument("--email")

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"] or os.environ.get("DJANGO_APP_ADMIN_USERNAME", "")
        email = options["email"] or os.environ.get("DJANGO_APP_ADMIN_EMAIL", "")
        password = os.environ.get("DJANGO_APP_ADMIN_PASSWORD", "")

        interactive = sys.stdin.isatty() and not (username and email and password)
        if interactive:
            username = username or input("Username: ")
            email = email or input("Email: ")
            if not password:
                password = getpass.getpass("Password: ")
                # Typing is hidden, so ask twice: a typo would otherwise create an account
                # whose password nobody knows (and this command never overwrites an account).
                if getpass.getpass("Password (again): ") != password:
                    raise CommandError("The two passwords did not match. Nothing was created; run the command again.")
        if not (username and email and password):
            self.stdout.write("create_app_admin: nothing to do (no admin details supplied).")
            return

        username = unicodedata.normalize("NFKC", username).strip()
        email = User.objects.normalize_email(email).strip()
        if User.objects.filter(username__iexact=username).exists():
            self.stdout.write(f"create_app_admin: user {username!r} already exists; left unchanged.")
            return
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError("A user with that email already exists.")
        candidate = User(username=username, email=email)
        try:
            validate_password(password, candidate)
        except ValidationError as exc:
            raise CommandError("Password rejected: " + " ".join(exc.messages)) from None

        with transaction.atomic():
            user = User(username=username, email=email, is_staff=False, is_superuser=False, must_change_password=True)
            user.set_password(password)
            user.save()
            assign_role(user, ADMIN)
        self.stdout.write(self.style.SUCCESS(f"Application admin {username!r} created (must change password at first login)."))
