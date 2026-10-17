"""
Invoice email delivery service.

SMTP credentials are read exclusively from environment variables.
No credentials are stored in source code.
"""

import os
import ssl
import smtplib

from email.message import EmailMessage
from pathlib import Path


SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
SMTP_FROM_NAME = os.getenv(
    "SMTP_FROM_NAME",
    "D?p?t Les Sources"
).strip()

SMTP_USE_TLS = (
    os.getenv("SMTP_USE_TLS", "true").strip().lower()
    in {"1", "true", "yes", "on"}
)

SMTP_USE_SSL = (
    os.getenv("SMTP_USE_SSL", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)


def smtp_is_configured():
    """
    Return True only when the minimum SMTP configuration exists.
    """

    return bool(
        SMTP_HOST
        and SMTP_PORT > 0
        and SMTP_FROM
    )


def build_invoice_email(
    recipient,
    invoice_number,
    pdf_path,
    client_name=None,
):
    """
    Build an invoice email with the generated PDF attached.
    """

    recipient = str(recipient or "").strip()
    invoice_number = str(invoice_number or "").strip()

    if not recipient:
        raise ValueError("Invoice recipient email is required.")

    if not invoice_number:
        raise ValueError("Invoice number is required.")

    pdf = Path(pdf_path)

    if not pdf.is_file():
        raise FileNotFoundError(
            f"Invoice PDF not found: {pdf}"
        )

    msg = EmailMessage()

    msg["Subject"] = (
        f"Facture {invoice_number} ? D?p?t Les Sources"
    )

    if SMTP_FROM_NAME:
        msg["From"] = (
            f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
        )
    else:
        msg["From"] = SMTP_FROM

    msg["To"] = recipient

    name = str(client_name or "").strip()

    if name:
        greeting = f"Bonjour {name},"
    else:
        greeting = "Bonjour,"

    msg.set_content(
        f"""{greeting}

Votre facture {invoice_number} est disponible.

Vous trouverez votre facture en pi?ce jointe au format PDF.

Vous pouvez ?galement consulter votre facture depuis votre espace client.

Merci de votre confiance.

D?p?t Les Sources
"""
    )

    with pdf.open("rb") as file:
        pdf_data = file.read()

    msg.add_attachment(
        pdf_data,
        maintype="application",
        subtype="pdf",
        filename=pdf.name,
    )

    return msg


def send_invoice_email(
    recipient,
    invoice_number,
    pdf_path,
    client_name=None,
):
    """
    Send an invoice PDF by SMTP.

    Returns a dictionary describing the result.
    """

    if not smtp_is_configured():
        return {
            "success": False,
            "status": "not_configured",
            "message": "SMTP email delivery is not configured.",
        }

    if SMTP_USE_SSL and SMTP_USE_TLS:
        raise ValueError(
            "SMTP_USE_SSL and SMTP_USE_TLS cannot both be enabled."
        )

    msg = build_invoice_email(
        recipient=recipient,
        invoice_number=invoice_number,
        pdf_path=pdf_path,
        client_name=client_name,
    )

    context = ssl.create_default_context()

    if SMTP_USE_SSL:
        smtp_class = smtplib.SMTP_SSL

        with smtp_class(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30,
            context=context,
        ) as smtp:

            if SMTP_USERNAME:
                smtp.login(
                    SMTP_USERNAME,
                    SMTP_PASSWORD,
                )

            smtp.send_message(msg)

    else:
        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30,
        ) as smtp:

            smtp.ehlo()

            if SMTP_USE_TLS:
                smtp.starttls(context=context)
                smtp.ehlo()

            if SMTP_USERNAME:
                smtp.login(
                    SMTP_USERNAME,
                    SMTP_PASSWORD,
                )

            smtp.send_message(msg)

    return {
        "success": True,
        "status": "sent",
        "recipient": recipient,
        "invoiceNumber": invoice_number,
    }
