# Student Assessment Platform

A Django-based web application for managing university assessments, examinations, submissions, and lecturer-managed grading.

The platform supports students, lecturers, and administrators through role-based access control, course-level authorization, timed examinations, question banks, bulk assessment imports, and server-side scoring.

**Core principle:** The server controls authorization, examination timing, submissions, and final scores. The client cannot independently determine assessment outcomes.

---

## Project Status

**Current stage: Feature development and integration. Hosted production verification outstanding.**

The platform includes the core assessment lifecycle, academic administration, assessment imports, question bank management, and lecturer marking.

### Implemented Features

**Authentication and Authorization**
- Django authentication, registration, login, logout, and session management.
- Three application roles: `STUDENT`, `LECTURER`, and `ADMIN`.
- Role-based and attribute-based authorization.
- Course ownership, lecturer assignment, department, level, and enrollment checks.
- Protected dashboards and application administration.

**Academic Administration**
- Department, level, and course management.
- Lecturer account management and course assignments.
- Student enrollment and eligibility validation.
- Application administration console.

**Assessment Management**
- Assessment creation and lifecycle management.
- Draft, published, and archived states.
- Multiple-choice, short-answer, and file-upload questions.
- Configurable examination duration and availability.
- Timed student examinations.
- Server-side answer processing and scoring.
- Student attempts and submission management.

**Assessment Import and Question Bank**
- Excel (`.xlsx`, `.xls`) imports.
- CSV imports.
- Word (`.docx`) imports.
- Text-based PDF imports.
- Moodle XML imports.
- Drag-and-drop file upload.
- Automatic question extraction.
- Editable question previews and validation.
- Duplicate detection and error reporting.
- Downloadable Excel import template.
- Import history and processing status.
- Reusable course-scoped question banks.
- Question search, category filtering, and bulk selection.
- Atomic assessment creation and question-bank persistence.

**Lecturer Marking**
- Review student submissions.
- Manually mark short-answer questions.
- Review uploaded student files.
- Provide marks and feedback.
- Recalculate assessment results server-side.

**Security**
- Centralized authorization enforcement.
- CSRF protection.
- Session security.
- Input validation and template escaping.
- Restricted upload formats and file-size limits.
- Transactional database operations.
- Security regression tests.

### Outstanding Work

- Final post-integration regression verification.
- End-to-end hosted deployment verification.
- OCR for scanned PDF documents.
- Malware scanning for uploaded files.
- Retakes and automated background assessment expiry.
- Login throttling, MFA, and email verification.
- Self-service password recovery.
- Tamper-resistant audit logging.
- Automated database backups.
- Assessment analytics and reporting.
- Question snapshots for assessment-version integrity.

---

## Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.13 |
| Backend | Django 5.2 |
| Database | SQLite (development), PostgreSQL (deployment) |
| Application server | Gunicorn |
| Static files | WhiteNoise |
| Frontend | Django Templates, HTML, CSS, JavaScript |
| Authentication | Django Authentication |
| Excel processing | openpyxl, xlrd |
| Word processing | python-docx |
| PDF processing | pypdf |
| Testing | Django Test Framework |
| Deployment | Render Blueprint |

---

## Getting Started

### Prerequisites

- Python 3.13
- Git
- PowerShell, Command Prompt, or a Unix-based terminal
- Internet connection for dependency installation

PostgreSQL is intended for production. SQLite is supported for local development.

### 1. Clone the Repository

```bash
git clone https://github.com/DloneW/assessment-platform.git
cd assessment-platform
```

### 2. Create a Virtual Environment

**Windows PowerShell:**

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS:**

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Copy `.env.example` to `.env`.

Generate a Django secret key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Configure the generated key as `DJANGO_SECRET_KEY`.

For local development, configure:

```env
DJANGO_DEBUG=True
DJANGO_SECRET_KEY=your-generated-secret-key
```

Configure other environment variables as required by your environment.

Never commit `.env`, production credentials, or generated secrets.

### 5. Apply Database Migrations

```bash
python manage.py migrate
```

### 6. Load Starter Academic Data

New databases require departments and levels before student registration.

```bash
python manage.py seed_reference_data
```

This command loads starter reference data into empty tables.

### 7. Create a Django Superuser

```bash
python manage.py createsuperuser
```

The Django superuser accesses Django administration. It does not automatically receive the application's `ADMIN` role.

### 8. Create an Application Administrator

```bash
python manage.py create_app_admin
```

Follow the interactive prompts.

### 9. Start the Development Server

```bash
python manage.py runserver
```

Open:

http://127.0.0.1:8000/

---

## Application Architecture

The application uses Django's server-rendered architecture.

```text
Student Assessment Platform
│
├── accounts/
│   ├── Authentication
│   ├── Academic administration
│   ├── Departments and levels
│   ├── Courses and enrollment
│   └── User dashboards
│
├── authorization/
│   ├── Application roles
│   ├── Access policies
│   ├── Attribute checks
│   └── Authorization enforcement
│
├── assessments/
│   ├── Assessment management
│   ├── Questions and choices
│   ├── Assessment imports
│   ├── Question bank
│   ├── Student attempts
│   ├── Submission processing
│   └── Lecturer marking
│
├── config/
│   ├── Django settings
│   ├── URL configuration
│   └── Deployment configuration
│
├── templates/
│   ├── accounts/
│   └── assessments/
│
├── static/
├── requirements.txt
├── manage.py
├── build.sh
└── render.yaml
```

---

## Application Routes

| Route | Access |
|---|---|
| `/register/` | Public registration |
| `/login/` | Authentication |
| `/logout/` | Authenticated users, POST only |
| `/dashboard/` | Role-based dashboard redirect |
| `/student/dashboard/` | Students |
| `/lecturer/dashboard/` | Lecturers |
| `/admin/dashboard/` | Application administrators |
| `/manage/` | Academic administration |
| `/manage/courses/<id>/enrolment/` | Application administrators |
| `/password/change/` | Authenticated users |
| `/courses/<id>/` | Authorized course members |
| `/courses/<id>/tests/new/` | Assigned lecturers and administrators |
| `/courses/<id>/tests/import/` | Assigned lecturers and administrators |
| `/courses/<id>/question-bank/` | Assigned lecturers and administrators |
| `/imports/template.xlsx` | Authenticated users |
| `/tests/<id>/manage/` | Assessment managers |
| `/tests/<id>/submissions/` | Assessment managers |
| `/tests/<id>/submissions/<attempt-id>/` | Assessment managers |
| `/tests/<id>/` | Eligible students |
| `/tests/<id>/start/` | Eligible students |
| `/tests/<id>/attempts/<id>/` | Attempt owner |
| `/admin/` | Authorized Django administrators |

---

## Assessment Import System

The import system eliminates the need for lecturers to manually enter every examination question.

### Supported Formats

| Format | Extension | Support |
|---|---|---|
| Excel | `.xlsx` | Supported |
| Excel 97-2003 | `.xls` | Supported |
| CSV | `.csv` | Supported |
| Microsoft Word | `.docx` | Supported |
| PDF | `.pdf` | Selectable text only |
| Moodle | `.xml` | Supported question types only |

### Import Workflow

1. Lecturer selects a course.
2. Lecturer uploads an assessment file.
3. The application extracts questions and answer options.
4. Extracted questions are displayed in an editable preview.
5. Validation checks identify missing answers, malformed questions, and duplicates.
6. Lecturer configures assessment details.
7. The application creates a draft assessment.
8. Questions are saved to the assessment and reusable question bank.
9. Import history records the operation.

### Excel Template Structure

| Column | Required |
|---|---|
| Question | Yes |
| A | Optional |
| B | Optional |
| C | Optional |
| D | Optional |
| E | Optional |
| Correct Answer | Yes |
| Question Type | Optional |
| Marks | Optional |
| Category | Optional |

Empty answer-option cells are ignored.

The import system supports up to 80 questions per upload and a 10 MB upload limit.

### Import Limitations

- Scanned PDFs are not supported because OCR is not implemented.
- Legacy `.doc` files are not supported.
- Moodle import currently supports multiple-choice and true/false questions.
- AI-generated question extraction is not implemented.

---

## Question Bank Management

Lecturers can maintain reusable question collections for individual courses.

Features include:

- Course-scoped question storage.
- Question search.
- Category filtering.
- Bulk question selection.
- Reusing questions in existing drafts.
- Import history and status tracking.

Questions can be imported once and reused in subsequent assessments.

---

## Assessment Engine

The assessment lifecycle is divided into lecturer and student operations.

### Lecturer Workflow

```text
Create Assessment
       |
       v
Add or Import Questions
       |
       v
Validate Assessment
       |
       v
Save Draft
       |
       v
Publish Assessment
       |
       v
Review Submissions
       |
       v
Mark Written Answers
       |
       v
Archive Assessment
```

### Student Workflow

```text
Access Student Dashboard
       |
       v
View Available Assessments
       |
       v
Read Instructions
       |
       v
Start or Resume Attempt
       |
       v
Answer Questions
       |
       v
Submit Assessment
       |
       v
Server-side Scoring
       |
       v
View Available Results
```

### Assessment Integrity

- Assessment deadlines are enforced server-side.
- Students cannot submit their own scores.
- Finalized attempts cannot be reopened by students.
- Published assessments are protected from normal editing.
- Assessment scoring is performed on the server.

The current implementation permits one attempt per student per assessment.

Overdue attempts are expired lazily when the relevant assessment or dashboard is accessed. No background expiry worker is configured.

---

## Question Types and Marking

Three assessment response types are supported.

**1. Multiple Choice**

Automatically marked against the lecturer-defined answer key.

**2. Short Answer**

Students submit text responses of up to 2,000 characters. Lecturers review and award marks.

**3. File Upload**

Students submit one supported image or document per answer.

Supported formats:

- PNG
- JPEG
- GIF
- WebP
- PDF
- DOCX

File restrictions:

- Maximum file size: 2 MB per file.
- Maximum request size: 10 MB.
- File extension and signature validation.
- Forced-download handling.

Short-answer and file-upload responses initially contribute zero marks until lecturer marking is completed.

**Security limitation:** Uploaded files are not scanned for malware.

---

## Security Architecture

The application separates Django administrative privileges from application-level roles.

### Application Roles

| Role | Responsibilities |
|---|---|
| STUDENT | Access eligible courses and take assessments |
| LECTURER | Manage assigned courses and assessments |
| ADMIN | Manage academic configuration and application accounts |

### Authorization Model

Authorization decisions are centralized in `authorization/services.py`.

Access decisions consider:

1. Application role.
2. Department and level.
3. Course enrollment.
4. Lecturer assignment.
5. Resource ownership.
6. Resource state.

Object-level authorization failures generally return non-disclosing 404 responses. Role restrictions return 403 where appropriate.

### Security Controls

- Server-side authorization.
- Server-side examination scoring.
- CSRF protection.
- Django template autoescaping.
- Secure session management.
- POST-only state-changing routes.
- Database constraints.
- Transactional assessment operations.
- Production HTTPS and secure-cookie configuration.
- Restricted upload formats and sizes.
- Authorization and assessment-integrity regression tests.

These controls reduce security risks but do not establish that the application is vulnerability-free.

---

## Running Tests

Run the Django system check:

```bash
python manage.py check
```

Check migration consistency:

```bash
python manage.py makemigrations --check --dry-run
```

Run the complete test suite:

```bash
python manage.py test
```

Run authorization tests:

```bash
python manage.py test authorization
```

Run assessment security tests:

```bash
python manage.py test assessments.test_security
```

Run assessment import tests:

```bash
python manage.py test assessments.test_imports
```

For production configuration, also run:

```bash
python manage.py check --deploy
```

The latest recorded pre-integration acceptance audit reported 347 passing tests. Repeat the full suite after upstream integration to verify the merged codebase.

---

## Deployment

The project includes:

- `render.yaml`
- `build.sh`

The intended deployment architecture uses:

- Python 3.13.
- Django 5.2.
- Gunicorn.
- PostgreSQL.
- WhiteNoise.
- Environment-based configuration.

### Environment Variables

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django signing secret |
| `DJANGO_DEBUG` | Debug configuration |
| `DJANGO_ALLOWED_HOSTS` | Allowed hostnames |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Trusted CSRF origins |
| `DATABASE_URL` | PostgreSQL connection |
| `DJANGO_BEHIND_TLS_PROXY` | Proxy-aware HTTPS handling |
| `DJANGO_SECURE_SSL_REDIRECT` | HTTPS redirects |
| `DJANGO_SESSION_COOKIE_SECURE` | Secure session cookies |
| `DJANGO_CSRF_COOKIE_SECURE` | Secure CSRF cookies |
| `DJANGO_HSTS_SECONDS` | HTTP Strict Transport Security |
| `DJANGO_DB_CONN_MAX_AGE` | Persistent database connections |
| `DJANGO_LOG_LEVEL` | Application logging level |

Production secrets must be configured through the hosting provider.

### Deployment Verification Checklist

Before accepting a production deployment:

- Verify HTTPS and HTTP redirects.
- Verify security headers and cookie attributes.
- Confirm database migrations.
- Confirm static assets load.
- Test student registration.
- Test lecturer and administrator authentication.
- Test student enrollment.
- Create and publish an assessment.
- Import questions from supported formats.
- Complete a student examination.
- Verify automatic and manual grading.
- Test unauthorized object access.
- Verify session invalidation after logout.
- Establish database backups and recovery procedures.

**Deployment status:** Configuration is prepared, but successful hosted end-to-end verification remains outstanding.

Do not use an unverified deployment for real student records or high-stakes examinations.

---

## Known Limitations

The current implementation does not include:

- Multi-factor authentication.
- Email verification.
- Login rate limiting.
- Self-service password recovery.
- Malware scanning.
- Tamper-resistant audit logging.
- Automated database backups.
- Independent student identity verification.
- OCR for scanned documents.
- Background assessment expiry.
- Multiple examination attempts.
- Plagiarism detection.
- Automated marking of written responses.
- Lecturer-facing analytics dashboards.
- Proctoring.
- AI question generation.
- Public APIs.
- Redis, Celery, or WebSockets.

These are planned or outstanding capabilities, not existing features.

---

## Development Guidelines

When contributing to the project:

1. Preserve the existing Django architecture.
2. Reuse established models and services.
3. Enforce authorization at the server level.
4. Never trust client-submitted scores or deadlines.
5. Use database transactions for critical operations.
6. Add regression tests for new functionality.
7. Maintain migration consistency.
8. Never commit credentials or production secrets.
9. Verify changes before merging into `main`.

---

## License

Specify the project's license before distributing or accepting external contributions.

---

**Student Assessment Platform**

Built with Django and PostgreSQL, with a focus on examination integrity, secure assessment management, and academic administration.
