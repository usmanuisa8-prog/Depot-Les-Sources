# Depot Les Sources

A Flask-based depot and commerce management application.

## Technology
- Python and Flask
- SQLite
- ReportLab for PDF invoices
- HTML, CSS, and JavaScript

## Local setup
1. Create a virtual environment: `python -m venv .venv`
2. Activate it: `.\.venv\Scripts\Activate.ps1`
3. Install packages: `python -m pip install -r requirements.txt`
4. Create a local `.env` file with a private `FLASK_SECRET_KEY`.
5. Restore or initialize the database using the approved setup procedure.
6. Start the app with `python app.py`.

## Security
- Never commit `.env`, database files, backups, or customer documents.
- Use synthetic data for testing.
- Back up the database before migrations.
- The Flask development server is not intended for production.
