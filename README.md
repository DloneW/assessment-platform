# Student Assessment Platform

A server-rendered Django web application for managing online student assessments, with role-based access, attribute-based authorization, timed examinations, server-side scoring, and lecturer-managed marking.

The platform is designed around a central principle: **the server, not the client, determines authorization, assessment timing, and final scores.**

## Project Status

**Deployment configuration prepared. End-to-end hosted verification remains outstanding.**

The application has completed five development days covering its academic models, authorization architecture, assessment engine, security hardening, and deployment preparation.

Local verification included Gunicorn, PostgreSQL 16, Django deployment checks, and HTTP security smoke tests. The Render deployment configuration was prepared separately. A successful local test run does not establish that every security control works correctly on the hosted service.

### Implemented

- Django authentication with registration, login, logout, and session management.
- Three application roles: `STUDENT`, `LECTURER`, and `ADMIN`.
- Attribute-based authorization using department, level, enrollment, ownership, and resource state.
- Academic management for departments, levels, courses, and lecturer assignments.
- Student enrollment and protected dashboards.
- Timed assessments with questions, choices, attempts, and server-side scoring.
- Multiple-choice, short-answer, and file-upload questions.
- Lecturer-managed marking and feedback for written answers and uploaded files.
- Assessment lifecycle controls, including draft, published, and archived states.
- Security regression tests covering authorization failures, forged inputs, CSRF, XSS, session handling, and assessment integrity.
- PostgreSQL-compatible database configuration, Gunicorn, WhiteNoise, and Render deployment preparation.

### Not yet implemented or fully verified

- End-to-end verification of the hosted deployment.
- Lecturer-facing result and analytics dashboards.
- Retakes and background processing for automatic expiry.
- Login throttling, MFA, email verification, and a self-service password-reset workflow.
- Malware scanning for uploaded files.
- Tamper-resistant audit logging and configured backups.

## Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.13 |
| Web framework | Django 5.2 |
| Database | SQLite for development; PostgreSQL for deployment |
| Application server | Gunicorn |
| Static file serving | WhiteNoise |
| User interface | Django templates and forms |
| Authentication | Django authentication and sessions |
| Testing | Django test framework |
| Deployment configuration | Render Blueprint |

## Getting Started

### Prerequisites

- Python 3.13
- Git
- A terminal or command prompt
- Internet access to install dependencies

PostgreSQL is required for the intended production configuration. Local development can use SQLite.

### 1. Clone the repository

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd assessment-platform
```

Replace the placeholder with your repository's actual URL.

### 2. Create a virtual environment

**Windows:**

```powershell
py -3.13 -m venv .venv
.venv\Scripts\activate
```

**Linux or macOS:**

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure environment variables

Copy `.env.example` to `.env` and generate a secret key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Set the generated value as `DJANGO_SECRET_KEY` in `.env`.

For local development, configure `DJANGO_DEBUG=True` and the appropriate local settings. Never commit `.env`, production credentials, or generated secrets.

### 5. Apply database migrations

```bash
python manage.py migrate
```

### 6. Create a Django superuser

```bash
python manage.py createsuperuser
```

This account accesses the Django administration site. It does not automatically receive the application's `ADMIN` role.

### 7. Create an application administrator

```bash
python manage.py create_app_admin
```

Follow the prompts to create an application administrator.

The application administrator manages academic configuration through the application's own administration console. The Django superuser manages the Django admin site.

### 8. Start the development server

```bash
python manage.py runserver
```

Open `http://127.0.0.1:8000/`.

## Application Routes

| Route | Access |
|---|---|
| `/register/` | Public; registration creates students only |
| `/login/` | Authentication |
| `/logout/` | Authenticated users; POST only |
| `/dashboard/` | Redirects users to their role-specific dashboard |
| `/student/dashboard/` | Students |
| `/lecturer/dashboard/` | Lecturers |
| `/admin/dashboard/` | Application administrators |
| `/manage/` | Academic and account administration |
| `/password/change/` | Authenticated users required to replace temporary passwords |
| `/students/<id>/` | Student owner or application administrator |
| `/courses/<id>/` | Authorized students, assigned lecturers, or application administrators |
| `/courses/<id>/tests/new/` | Assigned lecturer or application administrator |
| `/tests/<id>/manage/` | Authorized assessment manager |
| `/tests/<id>/` | Eligible students accessing published assessments |
| `/tests/<id>/start/` | Eligible students starting or resuming an attempt |
| `/tests/<id>/attempts/<id>/` | Attempt owner |
| `/admin/` | Authorized Django admin users |

See the Authorization and Assessment Engine sections for the complete access rules and assessment lifecycle.

## Running Tests

Run the Django system check:

```bash
python manage.py check
```

Check for uncommitted model changes:

```bash
python manage.py makemigrations --check --dry-run
```

Run the complete test suite:

```bash
python manage.py test
```

Run authorization boundary tests:

```bash
python manage.py test authorization
```

Run assessment security tests:

```bash
python manage.py test assessments.test_security
```

For deployment verification, also run `python manage.py check --deploy` with the intended production environment configured.

## Security Architecture

The platform separates application roles from Django's staff and superuser privileges.

Authorization decisions are centralized in `authorization/services.py`, with supporting role definitions, attribute checks, and enforcement helpers.

The principal access-control sequence is:

1. Verify the user's application role.
2. Check relevant attributes, including department, level, and enrollment.
3. Verify ownership or lecturer assignment.
4. Validate the resource's current state.
5. Allow or deny the operation.

Object-level access denials generally return a non-disclosing 404 response. Role-gated pages return 403 to authenticated users without the required role. Anonymous users are redirected to login where appropriate.

Security controls include:

- Server-side authorization and scoring.
- CSRF protection on state-changing requests.
- Django template autoescaping.
- Session invalidation on logout and session-key rotation at login.
- POST-only state-changing routes.
- Database constraints and transactional state transitions.
- Secure cookie and HTTPS settings for production.
- No-store caching for authenticated responses.
- Restricted upload formats, size limits, and download-only delivery.
- Regression tests for privilege escalation, IDOR, malformed inputs, and assessment tampering.

These controls reduce risk but do not establish that the application is vulnerability-free.

## Assessment Engine

Assessments follow a controlled lifecycle:

**Lecturer:** Create draft → Add questions → Validate → Publish → Archive.

**Student:** Access eligible published test → Start or resume attempt → Save answers → Submit → View result.

Published assessments are frozen through the application's supported editing interfaces. Attempt deadlines and scores are calculated and enforced server-side. Students cannot submit their own score, change their deadline, or reopen a finalized attempt.

The current version permits one attempt per student per assessment. Overdue attempts are expired lazily when the relevant assessment or dashboard is accessed; no background expiry worker is configured.

## Question Types and Marking

The platform supports three question types:

- **Multiple choice:** Automatically marked against the answer key.
- **Short answer:** Student responses of up to 2,000 characters, marked by a lecturer.
- **File upload:** One supported image or document per answer, subject to configured validation and size limits.

Short-answer and file-upload questions may include a lecturer-only marking guide. Written and uploaded answers initially contribute zero until marked. The result is recalculated on the server when marking is completed.

Supported uploads include PNG, JPEG, GIF, WebP, PDF, and DOCX files. Validation checks file signatures and extensions, with a maximum file size of 2 MB per file and a 10 MB request limit.

**Upload security limitation:** Uploaded files are not scanned for malware. File validation and forced-download handling reduce exposure but cannot establish that a file is safe.

## Academic Administration

Application administrators use `/manage/` to manage departments, levels, courses, lecturer accounts, and eligible account password resets.

Public registration always creates a student account. Lecturer accounts are created through the administration console, which generates a temporary password that must be replaced before normal application use.

Student enrollment is explicit. A student can access a course only when the course is active, the student's department and level match the course, and the student is enrolled.

Student identity details supplied during registration are not independently verified. Enrollment therefore acts as an additional access-control requirement, not proof of identity.

## Deployment

The project includes `render.yaml` and `build.sh` for Render deployment preparation.

The intended deployment uses:

- Python 3.13.
- Gunicorn to serve the Django application.
- PostgreSQL configured through `DATABASE_URL`.
- WhiteNoise for static files.
- Environment-based configuration for secrets, allowed hosts, trusted origins, and HTTPS-related settings.

### Deployment environment variables

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Required signing secret when debug mode is disabled |
| `DJANGO_DEBUG` | Enables or disables debug mode |
| `DJANGO_ALLOWED_HOSTS` | Permitted hostnames |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Trusted origins for CSRF validation |
| `DATABASE_URL` | PostgreSQL connection string |
| `DJANGO_BEHIND_TLS_PROXY` | Enables proxy-aware HTTPS handling when correctly configured |
| `DJANGO_SECURE_SSL_REDIRECT` | Controls HTTP-to-HTTPS redirects |
| `DJANGO_SESSION_COOKIE_SECURE` | Controls secure session-cookie transmission |
| `DJANGO_CSRF_COOKIE_SECURE` | Controls secure CSRF-cookie transmission |
| `DJANGO_HSTS_SECONDS` | Configures the HSTS duration |
| `DJANGO_DB_CONN_MAX_AGE` | Configures persistent database connections |
| `DJANGO_LOG_LEVEL` | Sets the application logging level |

Additional supported variables are documented in the deployment configuration. Production secrets must be set through the hosting provider's environment settings, not committed to source control.

### Deployment verification

Local deployment checks covered Gunicorn, PostgreSQL 16, migrations, static-file collection, Django deployment checks, and HTTP smoke tests.

Hosted deployment must be verified independently. The checklist includes:

- Confirm HTTPS works and HTTP redirects correctly.
- Inspect security headers and cookie attributes.
- Confirm migrations and static assets load.
- Create and authenticate application administrators.
- Exercise student registration and enrollment.
- Create, publish, take, and submit an assessment.
- Verify unauthorized object access is denied.
- Verify logout invalidates the session.
- Confirm published assessments remain read-only through the Django admin.
- Confirm database persistence and establish a backup strategy.

**Hosting note:** Render's free services and database offerings may have availability, sleep, storage, and retention limitations. Confirm the current plan terms before using the service for real student records or assessments.

## Known Limitations

The current implementation does not include:

- Login or registration rate limiting.
- Multi-factor authentication or email verification.
- A self-service password-reset flow.
- Malware scanning for uploaded files.
- Tamper-resistant audit logs.
- Configured automated database backups.
- Verified student identity.
- Question snapshots for assessment-version integrity.
- Background jobs for assessment expiry.
- Retakes, plagiarism detection, or automated marking of written responses.
- Lecturer-facing result analytics.
- Proctoring, AI question generation, public APIs, Redis, Celery, or WebSockets.

These are known gaps, not features that should be assumed to exist.

## Project Structure

```text
assessment-platform/
├── authorization/       # Roles, rules, access decisions and enforcement
├── assessments/         # Tests, questions, choices, attempts and marking
├── config/              # Django project and deployment settings
├── templates/           # Server-rendered HTML templates
├── staticfiles/         # Collected static assets (generated)
├── build.sh             # Deployment build script
├── render.yaml          # Render Blueprint configuration
├── requirements.txt     # Python dependencies
├── manage.py            # Django management entry point
└── README
