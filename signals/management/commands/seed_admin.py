"""Create a default admin/superuser account on first boot, if none exists yet.

Run automatically by the Docker entrypoint on every container start -- it is
idempotent (does nothing if the username already exists), so it never resets
a password someone has since changed. Credentials come from
DJANGO_SUPERUSER_USERNAME / _PASSWORD / _EMAIL (see settings.py / .env);
the defaults are a well-known placeholder, not a real secret -- change the
password after first login.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the default admin user from DJANGO_SUPERUSER_* settings if it doesn't exist yet."

    def handle(self, *args, **options):
        User = get_user_model()
        username = settings.DJANGO_SUPERUSER_USERNAME
        password = settings.DJANGO_SUPERUSER_PASSWORD
        email = settings.DJANGO_SUPERUSER_EMAIL

        if User.objects.filter(username=username).exists():
            self.stdout.write(f"seed_admin: user '{username}' already exists, skipping.")
            return

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(
            f"seed_admin: created default admin '{username}'. "
            f"CHANGE THE PASSWORD after logging in -- it is a well-known default."
        ))
