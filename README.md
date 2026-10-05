# Student Assessment Platform

A server-rendered Django web application for online student assessments. Students, lecturers and administrators will eventually take and manage timed tests with server-side scoring.

**Status: Day 1 foundation.** Custom user model, academic models (Department, Level, Course, StudentProfile), student registration, login/logout and a student dashboard. Roles, attribute-based authorization, tests, questions, attempts and results come in later days.

## Tech stack

- Python 3.13, Django 5.2
- Django ORM with SQLite (development)
- Django templates and Forms
- Django's built-in authentication and sessions

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
# paste the output into DJANGO_SECRET_KEY in .env

# 4. Create the database
python manage.py migrate

# 5. Create an administrator (interactive; no default credentials exist)
python manage.py createsuperuser

# 6. Run the development server
python manage.py runserver
```

Then open <http://127.0.0.1:8000/admin/> and add at least one **Department** and one **Level**; students pick from these when registering at `/register/`.

## Tests and checks

```bash
python manage.py test
python manage.py check
```

## URLs

| Path                 | Purpose                          |
|----------------------|----------------------------------|
| `/register/`         | Student registration             |
| `/login/`, `/logout/`| Authentication (logout is POST)  |
| `/student/dashboard/`| Logged-in student's own profile  |
| `/admin/`            | Django admin                     |
