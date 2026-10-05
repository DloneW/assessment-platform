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

| Path                   | Who may open it                                   |
|------------------------|---------------------------------------------------|
| `/register/`           | Anyone (always creates a STUDENT)                 |
| `/login/`, `/logout/`  | Anyone / logged-in users (logout is POST)         |
| `/dashboard/`          | Any logged-in user; redirects to their role's dashboard |
| `/student/dashboard/`   | STUDENT                                           |
| `/lecturer/dashboard/` | LECTURER                                          |
| `/admin/dashboard/`    | Application ADMIN (not the Django admin site)     |
| `/students/<id>/`      | The student themself, or an application ADMIN     |
| `/courses/<id>/`       | See "Course access" below                         |
| `/admin/`              | Django admin site (`is_staff` users)              |

## Authorization

All decisions live in the `authorization/` package. Views never inspect roles or attributes themselves; they call a function from `authorization/services.py`.

```
authorization/
  roles.py        role names, has_role(), assign_role()   (roles are Django Groups)
  rules.py        small single-purpose checks (department, level, enrolled, assigned lecturer, ...)
  services.py     the can_*() decisions views call; each reads as role -> attributes -> ownership -> state
  enforcement.py  @require(check) for role-gated pages, get_authorized_or_404() for objects
```

**Roles** are the Django Groups `STUDENT`, `LECTURER` and `ADMIN`, checked by explicit group membership. They are deliberately separate from Django's `is_staff` / `is_superuser`, which only control the Django admin site: an application ADMIN is not staff or a superuser, and a superuser is not an application ADMIN until given the role.

**Course access** (`can_access_course`) is allowed for:
- an application ADMIN (every course);
- a LECTURER assigned to the course (`Course.lecturers`);
- a STUDENT only if ALL hold: the course is active, the course's department and level match the student's, and the student is enrolled. Being enrolled is not enough on its own.

**Responses.** Anonymous users are redirected to login. A logged-in user opening a role-gated page they may not use (e.g. a student requesting `/lecturer/dashboard/`) gets **403**. A logged-in user requesting an *object* they may not access (another student's profile, a course they have no access to) gets **404**, identical to a nonexistent ID, because sequential IDs would otherwise let anyone enumerate what exists. Error pages never say why access was denied.

### Creating privileged accounts

Public registration can only ever create students. Privileged roles are assigned by trusted, server-side means only:

```bash
python manage.py createsuperuser            # Django admin access (no application role)
python manage.py assign_role <username> ADMIN      # or LECTURER / STUDENT; replaces any other role
```

Roles can also be changed in the Django admin (Users -> Groups), but only by superusers: for everyone else the staff, superuser, group and permission fields are read-only. Assign lecturers to courses in the admin under Courses -> Lecturers.

### Not yet implemented (later days)

`Test`, `Question`, `Attempt`, scoring and results. Day 3 adds `can_access_test`, `can_edit_test`, `can_publish_test` and `can_view_attempt` to `services.py` by composing the rules above with publication state and availability windows.
