# Student Assessment Platform

A server-rendered Django web application for online student assessments. Students, lecturers and administrators will eventually take and manage timed tests with server-side scoring.

**Status: Day 2 complete and hardened.**

Implemented:
- Authentication (Django sessions; register, login, logout)
- Application roles: STUDENT, LECTURER, ADMIN (Django Groups, separate from `is_staff` / `is_superuser`)
- Student registration (always creates a STUDENT; creates the user and student profile atomically)
- Academic models: Department, Level, Course (with `is_active` and assigned lecturers), StudentProfile
- Course enrollment (explicit `Enrollment` model, one row per student and course)
- Attribute-based authorization (role + department + level + enrollment + course state)
- Object-level authorization (student profiles, courses)
- Protected dashboards for students, lecturers and administrators
- Authorization and security regression tests

**Not implemented yet** (later days): the assessment/test engine, questions, attempts, timers, submissions, scoring and results.

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
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test                  # everything
python manage.py test authorization    # authorization and security boundary tests
```

## URLs

| Path                   | Who may open it                                   |
|------------------------|---------------------------------------------------|
| `/register/`           | Anyone (always creates a STUDENT)                 |
| `/login/`, `/logout/`  | Anyone / logged-in users (logout is POST)         |
| `/dashboard/`          | Logged-in user holding a role; redirects to that role's dashboard (403 if the account has no role) |
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

**HTTP methods.** Nothing at the dashboard, profile or course URLs modifies data, so they accept GET and HEAD only; any other verb gets 405 before login, role or object checks run. That makes the response identical for every user and every object ID, so a probing verb cannot reveal whether an object exists. `/register/` accepts GET/HEAD/POST, `/login/` GET/HEAD/POST, and `/logout/` POST only. All of them are CSRF-protected.

**Django admin guard.** Only superusers can change `is_staff`, `is_superuser`, groups or permissions. In addition, a non-superuser staff member can edit or delete only ordinary accounts (and edit their own): they cannot edit, delete or reset the password of a superuser, an application ADMIN or another staff member, because Django's "change user" permission would otherwise allow taking over those accounts. Grant staff access and model permissions sparingly.

**Enrollment and lecturers.** There is no self-enrollment. Enrollments and lecturer assignments are managed in the Django admin by staff with the relevant permissions.

### Creating privileged accounts

Public registration can only ever create students. Privileged roles are assigned by trusted, server-side means only:

```bash
python manage.py createsuperuser            # Django admin access (no application role)
python manage.py assign_role <username> ADMIN      # or LECTURER / STUDENT; replaces any other role
```

Roles can also be changed in the Django admin (Users -> Groups), but only by superusers: for everyone else the staff, superuser, group and permission fields are read-only. Assign lecturers to courses in the admin under Courses -> Lecturers.

### Known limitations

- **Self-asserted identity.** A student chooses their own student ID, department and level at registration, and these are not verified. Enrollment (made by staff) is the second gate for course access; if self-enrollment is ever added, these attributes need verification first.
- **No login throttling.** Repeated password guesses are not rate-limited.
- **Not production-configured.** HTTPS-only cookies, HSTS and SSL redirect are not set (`python manage.py check --deploy` lists them). Set them when deploying; they would break plain-HTTP local runs if forced on now.

### Not implemented yet (later days)

Assessment/test engine (`Test`), `Question`, `Attempt`, timers, submissions, scoring and results. Day 3 adds `can_access_test`, `can_edit_test`, `can_publish_test` and `can_view_attempt` to `services.py` by composing the existing rules with publication state and availability windows.
