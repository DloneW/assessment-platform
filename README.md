# Student Assessment Platform

A server-rendered Django web application for online student assessments. Students, lecturers and administrators will eventually take and manage timed tests with server-side scoring.

**Status: Day 5 deployment preparation complete and verified locally (gunicorn + PostgreSQL 16). Public deployment to a hosting platform has NOT been performed or verified yet; see "Deployment" below. Not claimed production-ready; see Security > Known limitations.**

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
- Assessment engine: tests, questions, choices, timed attempts, server-side scoring and results
- Security attack tests and hardening (Day 4)

**Not implemented yet** (later days): deployment configuration, lecturer result views, retakes, analytics.

## Tech stack

- Python 3.13, Django 5.2
- Django ORM with SQLite (development) and PostgreSQL (production, via `DATABASE_URL`)
- gunicorn (WSGI server) and WhiteNoise (static files) for deployment
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
python manage.py test assessments.test_security   # Day 4 attack simulation
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
| `/courses/<id>/tests/new/` | Lecturer assigned to the course (or ADMIN)    |
| `/tests/<id>/manage/`, `edit/`, `questions/...`, `publish/`, `archive/` | Lecturer assigned to the test's course (or ADMIN) |
| `/tests/<id>/`, `/tests/<id>/start/` | Eligible, enrolled STUDENT of a published, open test |
| `/tests/<id>/attempts/<id>/` (+ `save/`, `submit/`) | The student who owns the attempt |
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
- **Production flags** (HTTPS-only cookies, HSTS, SSL redirect) are configured for `DEBUG=False`; see "Deployment".

## Assessment engine (Day 3)

**Lifecycle.**

```
Lecturer:  Course -> Create test (DRAFT) -> add questions + choices -> validate -> Publish (frozen) -> Archive
Student:   eligible published test -> Start attempt -> Answer (save) -> Submit -> server-side score -> Result
```

**Core models** (`assessments/models.py`): `Test` (DRAFT/PUBLISHED/ARCHIVED, course, creator, duration, optional start window, `published_at`), `Question` (order, marks), `Choice` (order, `is_correct`), `Attempt` (student, test, status IN_PROGRESS/SUBMITTED/EXPIRED, `started_at`, stored `deadline`, `submitted_at`, `score`, `max_score`), `StudentAnswer` (one per attempt and question). There is no separate Result model: the finalized `Attempt` is the result.

**Authorization integration.** The Day 2 layers are extended, not duplicated. `rules.py` gains small rules (draft/published/open, attempt ownership and state); `services.py` gains `can_create_test`, `can_manage_test`, `can_edit_test`, `can_publish_test`, `can_archive_test`, `can_take_test`, `can_access_test`, `can_start_attempt`, `can_view_attempt`, `can_answer_attempt`, `can_submit_attempt`, plus `courses_managed_by` / `courses_accessible_to_student` for listings (a test asserts they agree with the per-object decisions). Lecturers manage tests only in courses they are assigned to; the course comes from the URL object and is checked by `can_create_test`, never taken from the form. Students reach a test only through the Day 2 course rules (active course, matching department and level, enrolled) AND only while it is published. Denied objects return 404, identical to missing ones.

**Publishing rules.** A test is publishable only with a valid duration, at least one question, every question having at least two choices and exactly one correct choice, and a window that has not already closed. Publishing is a compare-and-set from DRAFT, and a published test is frozen so scoring cannot shift under existing attempts.

**Server-side scoring.** The score is computed in `assessments/services.py` from stored answers and the answer key when the attempt is finalized, then stored. Nothing about score, status, correctness, student, test or timestamps is read from a request. Student-facing queries defer `Choice.is_correct`, so the answer key is never loaded into student pages, and the result page shows only the score.

**Server-side timing.** `deadline = started_at + duration` is stored when the attempt starts (later duration edits cannot move it). Every save and submit compares the server clock with it. A submit at or after the deadline finalizes the attempt as EXPIRED, scored on answers saved before the deadline; late answers are discarded. The page countdown is display only; it autosaves on change and auto-submits at zero, and the server decides regardless.

**Major security decisions.**
- One attempt per student per test, enforced by a unique constraint (never two active attempts; no retakes in this version). `get_or_create` turns a lost race into a plain "get".
- Starting is state-aware: no attempt creates one, an IN_PROGRESS attempt is resumed, and a SUBMITTED or EXPIRED attempt is never reopened (the student is sent to the result and no second attempt is created). An overdue in-progress attempt is expired first.
- Archiving is PUBLISHED -> ARCHIVED only: the row is re-read and locked in a transaction, checked, then updated with a compare-and-set.
- Finalization is a compare-and-set (`UPDATE ... WHERE status = 'IN_PROGRESS'`), so a double submit changes nothing.
- Answer chain: attempt owner -> attempt belongs to the test in the URL -> in progress and before deadline -> question belongs to that test -> choice belongs to that question. A bad pair rejects the whole request (400) and writes nothing.
- Database constraints cover duration bounds, window order, publication state, one correct choice per question, unique ordering, attempt state/score consistency and one answer per question per attempt. Two cross-table rules (question in the attempt's test, choice in the question) are enforced in `StudentAnswer.save()` and the service.
- State-changing routes are POST-only and CSRF-protected; templates autoescape and never use `|safe`.

### Known limitations

- **Self-asserted identity.** A student chooses their own student ID, department and level at registration, and these are not verified. Enrollment (made by staff) is the second gate for course access; if self-enrollment is ever added, these attributes need verification first.
- **No login throttling.** Repeated password guesses are not rate-limited.
- **Production flags** are configured for `DEBUG=False`; see "Deployment".
- **Lazy expiry.** No background job: an overdue attempt becomes EXPIRED the next time it or its student's dashboard is opened. Nothing can be written to it meanwhile.
- **One attempt, no retakes**; lecturers cannot yet view students' attempts or results (only the owner can; admins can audit read-only in the Django admin).
- `bulk_create` and queryset `.update()` bypass `StudentAnswer.save()` checks; application writes go through the services.
- SQLite serializes writers; on another database the draft/publish race relies on `select_for_update` row locks.

### Not implemented yet (later days)

Deployment configuration, lecturer result views and analytics, retakes. Proctoring, AI question generation, leaderboards, certificates, APIs, Redis, Celery and WebSockets are out of scope.

## Security (Day 4)

This section describes what the application enforces and how it was tested. It is not a claim that the application is production-ready or free of vulnerabilities; see "Known limitations" below.

**Authentication.** Django's built-in sessions and auth. Passwords are hashed by Django and checked by its validators; logout is POST-only and destroys the server-side session (a copied session cookie is dead after logout); the session id changes at login; `next=` redirects are restricted to this host. Usernames are NFKC-normalized (matching the login form) and unique case-insensitively, as are emails and student IDs.

**Authorization and attribute-based access control.** Every decision goes through `authorization/services.py`: role -> attributes (department, level, enrollment, active course) -> ownership/assignment -> resource state. Views never decide access themselves. Public registration only ever creates a STUDENT; role, group, staff and superuser values in a request are ignored.

**Object ownership and information disclosure.** Objects the user may not access (another lecturer's test, another student's attempt or profile, a course outside their attributes) return the same bare 404 as a missing id, tested by comparing response bodies. Role-gated pages return 403, anonymous users are redirected to login. Ids beyond the database range are a clean 404/400, not a 500.

**State transition protection.** Test: DRAFT -> PUBLISHED -> ARCHIVED only. Attempt: IN_PROGRESS -> SUBMITTED or EXPIRED only. Status, score, `started_at`, `deadline`, `submitted_at` and similar fields are never read from a request; transitions run in services as transactions with compare-and-set updates, and CHECK constraints in the database reject impossible combinations. The Django admin respects the same freeze: a published test and its questions cannot be edited there, and status/publication time are read-only.

**Attempt integrity.** One attempt per student per test (unique constraint); finalized attempts are never reopened; answers are accepted only from the owner, before the deadline, while IN_PROGRESS, for a question of the attempt's test and a choice of that question; any bad pair rejects the whole request. Answer ids are parsed strictly (ASCII digits, bounded length).

**Server-side scoring and timing.** The score is computed from stored answers and the answer key when the attempt is finalized and then stored. The deadline is `started_at + duration`, stored at start and compared with the server clock on every save and submit; the JavaScript countdown is display only.

**CSRF and XSS.** CSRF middleware is on and every state-changing route was tested to return 403 without a token. Templates rely on Django autoescaping; a test scans for `|safe`, `autoescape off` and `mark_safe`, and script/attribute payloads in registration, course, test, question and choice fields are verified as escaped on every page that shows them. All queries use the ORM (a test scans for raw SQL). Authenticated responses are sent `Cache-Control: no-store` so results and the answer key are not recoverable from the browser cache after logout.

**Database constraints.** Case-insensitive unique username, email and student ID; unique enrollment; unique attempt per student/test; unique answer per attempt/question; one correct choice per question; unique question/choice order; non-blank titles and texts; positive marks and orders; duration bounds; attempt state, score, and timestamp consistency (a SUBMITTED attempt is submitted after it started and before its deadline).

**Security testing.** `assessments/test_security.py` attacks the application with HTTP requests (IDOR and privilege escalation in both directions, every edge of both state machines, forged protected fields, answer/score/time tampering, malformed and oversized input, CSRF, XSS, admin-site bypass, constraint violations), alongside `authorization/test_boundary.py` and the Day 2/3 suites. Key guards were also checked by mutation: removing each made at least one test fail.

### Findings fixed in Day 4

| Finding | Fix |
|---|---|
| Django admin could add questions to, or change the course/creator/duration of, a published test | Admin forms limit questions to draft tests; published tests are fully read-only; course/creator fixed after creation |
| Usernames unique case-sensitively; fullwidth/compatibility characters stored un-normalized | NFKC normalization, form check and a `Lower(username)` unique constraint |
| Answer-id parsing accepted `1_0`, `+1`, non-ASCII digits | Strict ASCII-digit regex with bounded length |
| Authenticated pages (results, answer key) cacheable by the browser | `Cache-Control: no-store` middleware |
| Database accepted blank titles/texts, order 0, and impossible `submitted_at` values | New CHECK constraints |

### Known limitations (not solved in Day 4)

- **No question snapshot.** Scores are computed from the live question/choice rows at finalization. The application and admin freeze published tests, so this cannot be changed through them, but direct database/shell edits to a published test could alter the scoring of attempts still in progress. A finalized attempt keeps its stored score.
- **Attempt rights follow ownership.** A student already in an attempt may finish it if later unenrolled or if the test is archived; new starts are refused.
- **No login or registration throttling**, no account lockout, no MFA, no email verification. Registration reveals whether a username, email or student ID is taken.
- **Self-asserted identity** (student ID, department, level) is not verified; enrollment by staff is the second gate.
- **Staff with Django-admin permissions are trusted** for the data those permissions cover (for example enrollments and lecturer assignments).
- **Lazy expiry**, no background job; no audit log; no password-reset flow.
- **Hosted deployment not yet verified.** The production configuration exists and was tested locally, but real TLS, the hosting platform's proxy and its managed database have not been exercised (see "Deployment").

## Deployment (Day 5)

### What is and is not verified

Verified locally with `DEBUG=False`, gunicorn and PostgreSQL 16 (a local server, not a hosted one): migrations, `collectstatic`, `check --deploy`, the full 284-test suite on PostgreSQL, a clean-checkout build in a fresh virtualenv, and a 60-check HTTP smoke test (student, lecturer and admin flows, authorization, CSRF, cookies, caching, logout) run against gunicorn with `X-Forwarded-Proto: https` standing in for the platform's TLS proxy.

**Not verified:** a public deployment. The hosting platforms could not be reached from the development sandbox, so the Render blueprint (`render.yaml`) is untested on Render itself, real HTTPS/certificates and the browser's behaviour with `Secure` cookies and HSTS have not been exercised end to end, and no hosted PostgreSQL has been used. Treat the first real deployment as the verification step and run the checklist below.

### Prerequisites

Python 3.13, PostgreSQL (production), a host that runs a WSGI process and terminates HTTPS. No Docker or frontend build is needed.

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `DJANGO_SECRET_KEY` | Signing key. **Required when `DJANGO_DEBUG` is off**; the app refuses to start without it. | none |
| `DJANGO_DEBUG` | `True` only for local development | `False` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames | `localhost,127.0.0.1` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated `https://host` origins | empty |
| `RENDER_EXTERNAL_HOSTNAME` | Set by Render; added to allowed hosts and trusted origins | unset |
| `DATABASE_URL` | `postgres://user:pass@host:5432/name?sslmode=require`; without it SQLite is used | unset |
| `DJANGO_BEHIND_TLS_PROXY` | Trust `X-Forwarded-Proto` from the platform's proxy. **Only enable behind a proxy that sets it on every request**; otherwise leave off or clients could spoof HTTPS. Required on platforms that terminate TLS, or the HTTPS redirect loops. | `False` |
| `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SESSION_COOKIE_SECURE`, `DJANGO_CSRF_COOKIE_SECURE` | Override the secure defaults | on when `DEBUG` is off |
| `DJANGO_HSTS_SECONDS` | HSTS lifetime. Starts at one day; raise (e.g. `31536000`) once HTTPS is verified. | `86400` when `DEBUG` is off |
| `DJANGO_HSTS_INCLUDE_SUBDOMAINS`, `DJANGO_HSTS_PRELOAD` | HSTS options; preload is a deliberate opt-in | on / off |
| `DJANGO_DB_CONN_MAX_AGE` | Persistent DB connection seconds | `60` |
| `DJANGO_LOG_LEVEL` | Console log level | `INFO` |
| `DJANGO_SKIP_DOTENV` | Ignore a local `.env` (used by the deployment tests) | unset |

Real environment variables always override `.env`. Never commit secrets; `.env` is git-ignored and `render.yaml` generates the secret key on the host.

### Local setup (development)

As in "Setup" above: `.env` with `DJANGO_DEBUG=True`, SQLite, plain HTTP, no secure-cookie or redirect settings. Running with production settings locally needs HTTPS or the proxy header; the quickest check is `DJANGO_DEBUG=False`, `DJANGO_BEHIND_TLS_PROXY=True` and requests carrying `X-Forwarded-Proto: https` and a matching `Host`.

### Production configuration (what `DEBUG=False` switches on)

- `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, HttpOnly session cookie, `SameSite=Lax`.
- `SECURE_SSL_REDIRECT`, HSTS (one day by default, with subdomains, no preload), `SECURE_PROXY_SSL_HEADER` only when `DJANGO_BEHIND_TLS_PROXY` is set.
- `nosniff`, `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy: same-origin`, `X-Frame-Options: DENY`.
- Console logging, so 500 tracebacks reach the platform's log viewer (403s from permission checks also appear as warnings).
- WhiteNoise serves `collectstatic` output from `staticfiles/` (`CompressedStaticFilesStorage`, no manifest, so a missing `collectstatic` degrades to the app's own static handling rather than crashing templates).
- `python manage.py check --deploy` reports a single warning, `security.W021` (HSTS preload). It is intentionally not enabled: preload submission is hard to undo and should be chosen after the domain has run HTTPS-only. With `DJANGO_HSTS_PRELOAD=True` and `DJANGO_HSTS_SECONDS=31536000` the check reports no issues (tested).

### Database, migrations and static files

- Production uses PostgreSQL through `DATABASE_URL`; the ORM and migrations are unchanged. `python manage.py makemigrations --check` reports no changes, and the full test suite passes on PostgreSQL 16 as well as SQLite.
- Apply migrations on every deploy: `python manage.py migrate --noinput` (`render.yaml` runs it at start).
- `python manage.py collectstatic --noinput` (run by `build.sh`) copies the 127 admin files into `staticfiles/`. There is no frontend build; the app's own pages use inline CSS.

### Deploying on Render (prepared, not yet executed)

1. Push this repository to GitHub.
2. Render dashboard -> **New -> Blueprint**, select the repository. `render.yaml` defines the web service (`build.sh`, then `migrate` + gunicorn) and a free PostgreSQL database, and generates `DJANGO_SECRET_KEY`.
3. After the first deploy, open `https://<service>.onrender.com/login/`. The service's hostname is allowed automatically; for a custom domain also set `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.
4. Create the first administrator. Render's free tier has no shell, so either set `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD` once and run `python manage.py createsuperuser --noinput` (for example temporarily prefixed to the start command), then remove them; or run the command from a paid-plan shell. Never commit these values.
5. In `/admin/` create a Department, a Level and Courses, assign lecturers (`python manage.py assign_role <username> LECTURER` then Courses -> Lecturers) and enrol students.

Free-tier caveats: the web service sleeps when idle, and Render's free PostgreSQL expires after a time limit; use a paid database for anything that must persist.

Other hosts work the same way: a Python 3.13 build running `./build.sh`, a start command of `python manage.py migrate --noinput && gunicorn config.wsgi:application`, the environment variables above, and `DJANGO_BEHIND_TLS_PROXY=True` if the platform terminates TLS.

### First-deployment checklist

`check --deploy` on the host; open the site over HTTPS and confirm HTTP redirects; inspect the `Set-Cookie` flags and security headers; register a student; enrol them in the admin; publish a test as a lecturer; take it, submit, view the result; log out and confirm the result URL redirects to login; confirm a second lecturer and a second student get 404 on the first one's objects; confirm the admin shows a published test read-only. Then raise `DJANGO_HSTS_SECONDS`.

### Known limitations (unchanged by Day 5)

Day 5 added deployment configuration only. It did **not** address: question snapshots, MFA, email verification, login/registration throttling, audit logging, self-asserted identity, or lazy expiry (see the Security section). In addition: HSTS preload is off, there is no error-monitoring service, no backups are configured, WhiteNoise and gunicorn run without a CDN or separate static host, and the free-tier Render settings are suitable for demonstration rather than load.
