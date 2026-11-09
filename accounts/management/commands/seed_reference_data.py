"""
Give a fresh database starting departments and levels so student registration works.

    python manage.py seed_reference_data

Departments and levels are ordinary rows an administrator manages in the Administration Console
(Departments, Levels). A new database has none, so the registration dropdowns are empty. This
command only fills a table that is completely EMPTY, so it never adds anything to a database an
administrator has already set up, and running it twice is harmless.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Department, Level

DEPARTMENTS = [
    "Cybersecurity",
    "Computer Science",
    "Software Engineering",
    "Information Technology",
    "Data Science",
]
LEVELS = [100, 200, 300, 400, 500]


class Command(BaseCommand):
    help = "Create starter departments and levels in an empty database (no-op if they already exist)."

    def handle(self, *args, **options):
        with transaction.atomic():
            if Department.objects.exists():
                self.stdout.write("Departments already exist; left unchanged.")
            else:
                Department.objects.bulk_create([Department(name=n) for n in DEPARTMENTS])
                self.stdout.write(self.style.SUCCESS(f"Added {len(DEPARTMENTS)} departments."))
            if Level.objects.exists():
                self.stdout.write("Levels already exist; left unchanged.")
            else:
                Level.objects.bulk_create([Level(number=n) for n in LEVELS])
                self.stdout.write(self.style.SUCCESS(f"Added {len(LEVELS)} levels."))
