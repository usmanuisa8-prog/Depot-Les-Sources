from pathlib import Path
from datetime import datetime
import re
from storage_config import (
    DATABASE_PATH,
    INVOICE_STORAGE_DIR,
    GENERATED_INVOICES_DIR,
)

import sqlite3

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


BASE_DIR = Path(__file__).resolve().parent


def get_db_connection():
    """
    Open the project SQLite database with the same foreign-key behavior
    expected by the Flask application.
    """
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def format_xaf(value):
    """
    Format an amount in FCFA using French-style thousands separation.
    """
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0

    return f"{amount:,.0f}".replace(",", " ") + " FCFA"


def normalize_whatsapp_number(phone):
    """
    Normalize a Cameroon phone number to international format.

    Examples:
        650731007  -> 237650731007
        0650731007 -> 237650731007
        +237650731007 -> 237650731007

    Returns:
        A normalized numeric string, or None if the number is invalid.
    """
    if phone is None:
        return None

    value = str(phone).strip()

    if not value:
        return None

    value = re.sub(r"[^\d+]", "", value)

    if value.startswith("+"):
        value = value[1:]

    if value.startswith("00237"):
        value = value[5:]
        value = "237" + value
    elif value.startswith("237"):
        pass
    elif value.startswith("0"):
        value = "237" + value[1:]
    elif len(value) == 9:
        value = "237" + value

    if not re.fullmatch(r"2376\d{8}", value):
        return None

    return value


def get_order_invoice_data(command_id, conn=None):
    """
    Retrieve all immutable financial/customer/order data required
    to create an invoice.

    Prices and subtotals come directly from articlesCommandes.
    Current product prices and current promotions are deliberately not used.
    """
    own_connection = conn is None

    if own_connection:
        own_connection = conn is None

    if own_connection:
        own_connection = conn is None

    if own_connection:
        conn = get_db_connection()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                c.cmdId,
                c.cmdNum,
                c.cmdClientId,
                c.cmdStatut,
                c.cmdPayementMethod,
                c.cmdTotal,
                c.cmdLivAddress,
                c.cmdDate,
                c.cmdZoneId,
                c.cmdDepotId,

                u.userName,
                u.userSurname,
                u.userMail,
                u.userPhone,
                u.userAddress,
                u.invoiceDeliveryMethod,

                d.depotId,
                d.depotName,
                d.depotAddress,

                z.zoneId,
                z.zoneName,
                z.deliveryFee

            FROM Commandes c

            JOIN users u
                ON u.userId = c.cmdClientId

            LEFT JOIN depots d
                ON d.depotId = c.cmdDepotId

            LEFT JOIN delivery_zones z
                ON z.zoneId = c.cmdZoneId

            WHERE c.cmdId = ?
        """, (command_id,))

        order = cursor.fetchone()

        if not order:
            raise ValueError(f"Order {command_id} does not exist.")

        cursor.execute("""
            SELECT
                ac.artcmdId,
                ac.artcmdArticleId,
                ac.artcmdQuantity,
                ac.artcmdPrice,
                ac.artcmdSubTotal,

                a.articleVolume,
                a.articlePackage,

                p.productName

            FROM articlesCommandes ac

            JOIN Articles a
                ON a.articleId = ac.artcmdArticleId

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE ac.artcmdCmdId = ?

            ORDER BY ac.artcmdId ASC
        """, (command_id,))

        items = [dict(row) for row in cursor.fetchall()]

        if not items:
            raise ValueError(
                f"Order {command_id} has no order items."
            )

        order = dict(order)

        subtotal = sum(
            float(item["artcmdSubTotal"] or 0)
            for item in items
        )

        delivery_fee = float(order["deliveryFee"] or 0)

        stored_total = float(order["cmdTotal"] or 0)

        calculated_total = subtotal + delivery_fee

        return {
            "order": order,
            "items": items,
            "subtotal": subtotal,
            "delivery_fee": delivery_fee,
            "total": stored_total,
            "calculated_total": calculated_total,
            "total_matches": abs(
                stored_total - calculated_total
            ) < 0.01,
        }

    finally:
        if own_connection:
            conn.close()


def generate_invoice_number(conn):
    """
    Generate the next invoice number for the current year.

    Format:
        FACT-YYYY-0001
    """
    year = datetime.now().year

    prefix = f"FACT-{year}-"

    cursor = conn.cursor()

    cursor.execute("""
        SELECT invoiceNumber
        FROM invoices
        WHERE invoiceNumber LIKE ?
        ORDER BY invoiceId DESC
        LIMIT 1
    """, (prefix + "%",))

    row = cursor.fetchone()

    if not row:
        sequence = 1
    else:
        match = re.search(
            rf"^{re.escape(prefix)}(\d+)$",
            row["invoiceNumber"]
        )

        if match:
            sequence = int(match.group(1)) + 1
        else:
            sequence = 1

    return f"{prefix}{sequence:04d}"



def create_client_consolidated_invoice(client_id, conn=None):
    """
    Create one consolidated invoice for all delivered commands
    belonging to one client.

    BUSINESS RULE:
    - Include all delivered commands.
    - Sum all product subtotals across those commands.
    - Apply the delivery fee ONLY ONCE.
    - The delivery fee comes from the most recent delivered
      command's delivery zone.
    - Existing command-level invoices remain untouched.
    """

    own_connection = conn is None

    if own_connection:
        conn = get_db_connection()

    try:
        cursor = conn.cursor()

        # ============================================================
        # CLIENT
        # ============================================================

        client = cursor.execute("""
            SELECT
                userId,
                userName,
                userSurname,
                userMail,
                userPhone,
                userAddress,
                invoiceDeliveryMethod
            FROM Users
            WHERE userId = ?
              AND userRole = 2
              AND userActive = 1
            LIMIT 1
        """, (client_id,)).fetchone()

        if not client:
            raise ValueError(
                f"Client {client_id} does not exist or is inactive."
            )

        # ============================================================
        # ALL DELIVERED COMMANDS
        # ============================================================

        commands = cursor.execute("""
            SELECT
                c.cmdId,
                c.cmdNum,
                c.cmdClientId,
                c.cmdStatut,
                c.cmdPayementMethod,
                c.cmdTotal,
                c.cmdLivAddress,
                c.cmdDate,
                c.cmdZoneId,
                c.cmdDepotId,

                d.depotName,
                d.depotAddress,

                z.zoneName,
                z.deliveryFee

            FROM Commandes c

            LEFT JOIN depots d
                ON d.depotId = c.cmdDepotId

            LEFT JOIN delivery_zones z
                ON z.zoneId = c.cmdZoneId

            WHERE c.cmdClientId = ?
              AND c.cmdStatut = 'livree'

            ORDER BY
                c.cmdDate ASC,
                c.cmdId ASC
        """, (client_id,)).fetchall()

        if not commands:
            raise ValueError(
                "This client has no delivered commands to invoice."
            )

        commands = [dict(row) for row in commands]

        # ============================================================
        # COMMAND ITEMS
        # ============================================================

        command_ids = [
            command["cmdId"]
            for command in commands
        ]

        placeholders = ",".join(
            "?" for _ in command_ids
        )

        items = cursor.execute(f"""
            SELECT
                ac.artcmdId,
                ac.artcmdCmdId,
                ac.artcmdArticleId,
                ac.artcmdQuantity,
                ac.artcmdPrice,
                ac.artcmdSubTotal,

                a.articleVolume,
                a.articlePackage,

                p.productName

            FROM articlesCommandes ac

            JOIN Articles a
                ON a.articleId = ac.artcmdArticleId

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE ac.artcmdCmdId IN ({placeholders})

            ORDER BY
                ac.artcmdCmdId ASC,
                ac.artcmdId ASC
        """, command_ids).fetchall()

        items = [dict(row) for row in items]

        if not items:
            raise ValueError(
                "The delivered commands contain no invoice items."
            )

        # ============================================================
        # GROUP ITEMS BY COMMAND
        # ============================================================

        items_by_command = {}

        for item in items:
            command_id = item["artcmdCmdId"]

            items_by_command.setdefault(
                command_id,
                []
            ).append(item)

        # ============================================================
        # FINANCIAL VALIDATION
        #
        # Each command is validated individually against its stored
        # command total, but its delivery fee is NOT added to the
        # consolidated delivery total.
        # ============================================================

        subtotal = 0.0
        product_total_by_command = {}

        for command in commands:
            command_id = command["cmdId"]

            command_items = items_by_command.get(
                command_id,
                []
            )

            if not command_items:
                raise ValueError(
                    f"Command {command_id} has no invoice items."
                )

            command_subtotal = sum(
                float(item["artcmdSubTotal"] or 0)
                for item in command_items
            )

            command_delivery_fee = float(
                command["deliveryFee"] or 0
            )

            stored_command_total = float(
                command["cmdTotal"] or 0
            )

            calculated_command_total = (
                command_subtotal
                + command_delivery_fee
            )

            if abs(
                stored_command_total
                - calculated_command_total
            ) >= 0.01:
                raise ValueError(
                    f"Financial mismatch for command "
                    f"{command['cmdNum'] or command_id}: "
                    f"stored total = "
                    f"{stored_command_total:.2f}, "
                    f"calculated total = "
                    f"{calculated_command_total:.2f}."
                )

            product_total_by_command[command_id] = (
                command_subtotal
            )

            subtotal += command_subtotal

        # ============================================================
        # CONSOLIDATED DELIVERY FEE
        #
        # IMPORTANT:
        # Delivery is charged ONCE for the consolidated invoice.
        #
        # We use the MOST RECENT delivered command's delivery zone.
        # This preserves the current Centre Ville price of 500 FCFA.
        # ============================================================

        delivery_reference = commands[-1]

        delivery_fee = float(
            delivery_reference["deliveryFee"] or 0
        )

        delivery_zone_name = (
            delivery_reference["zoneName"]
            or "Zone de livraison"
        )

        total = subtotal + delivery_fee

        # ============================================================
        # DELIVERY PREFERENCE
        # ============================================================

        delivery_method = (
            str(
                client["invoiceDeliveryMethod"]
                or "email"
            )
            .strip()
            .lower()
        )

        if delivery_method not in {
            "email",
            "whatsapp"
        }:
            delivery_method = "email"

        email_address = (
            str(client["userMail"] or "").strip()
            or None
        )

        whatsapp_number = (
            str(client["userPhone"] or "").strip()
            or None
        )

        # ============================================================
        # INVOICE NUMBER
        # ============================================================

        year = datetime.now().year
        prefix = f"FACT-CLIENT-{year}-"

        row = cursor.execute("""
            SELECT invoiceNumber
            FROM client_invoices
            WHERE invoiceNumber LIKE ?
            ORDER BY clientInvoiceId DESC
            LIMIT 1
        """, (prefix + "%",)).fetchone()

        if not row:
            sequence = 1
        else:
            match = re.search(
                rf"^{re.escape(prefix)}(\d+)$",
                row["invoiceNumber"]
            )

            if match:
                sequence = int(match.group(1)) + 1
            else:
                sequence = 1

        invoice_number = (
            f"{prefix}{sequence:04d}"
        )

        # ============================================================
        # CREATE CLIENT CONSOLIDATED INVOICE
        # ============================================================

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO client_invoices (
                invoiceNumber,
                clientId,
                invoiceDate,
                subtotal,
                deliveryFee,
                total,
                deliveryMethod,
                emailAddress,
                whatsappNumber,
                status,
                sendStatus,
                createdAt,
                updatedAt
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'generated',
                'pending',
                ?, ?
            )
        """, (
            invoice_number,
            client["userId"],
            now,
            subtotal,
            delivery_fee,
            total,
            delivery_method,
            email_address,
            whatsapp_number,
            now,
            now,
        ))

        client_invoice_id = cursor.lastrowid

        if own_connection:
            conn.commit()

        invoice = cursor.execute("""
            SELECT *
            FROM client_invoices
            WHERE clientInvoiceId = ?
        """, (client_invoice_id,)).fetchone()

        return {
            "success": True,
            "created": True,
            "clientInvoice": dict(invoice),
            "client": dict(client),
            "commands": commands,
            "items": items,
            "subtotal": subtotal,
            "delivery_fee": delivery_fee,
            "delivery_zone_name": delivery_zone_name,
            "total": total,
            "product_total_by_command": product_total_by_command,
        }

    except Exception:
        if own_connection:
            conn.rollback()
        raise

    finally:
        if own_connection:
            conn.close()

def create_invoice_for_order(command_id, conn=None):
    """
    Create the database invoice record for one order.

    The operation is idempotent:
    one commandId can have only one invoice.

    If conn is supplied, the caller owns the transaction.
    Otherwise this function creates and manages its own connection.
    """
    own_connection = conn is None

    if own_connection:
        conn = get_db_connection()

    try:
        cursor = conn.cursor()

        existing = cursor.execute("""
            SELECT *
            FROM invoices
            WHERE commandId = ?
        """, (command_id,)).fetchone()

        if existing:
            return {
                "success": True,
                "created": False,
                "invoice": dict(existing),
            }

        data = get_order_invoice_data(
            command_id,
            conn=conn
        )

        order = data["order"]

        if order["cmdStatut"] != "livree":
            raise ValueError(
                "An invoice can only be generated for a delivered order."
            )

        if not data["total_matches"]:
            raise ValueError(
                "Invoice totals do not match the stored order total."
            )

        delivery_method = (
            order["invoiceDeliveryMethod"] or "email"
        ).lower().strip()

        if delivery_method not in ("email", "whatsapp"):
            delivery_method = "email"

        email_address = (
            order["userMail"]
            if delivery_method == "email"
            else None
        )

        whatsapp_number = (
            normalize_whatsapp_number(order["userPhone"])
            if delivery_method == "whatsapp"
            else None
        )

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        invoice_number = generate_invoice_number(conn)

        cursor.execute("""
            INSERT INTO invoices (
                invoiceNumber,
                commandId,
                clientId,
                depotId,
                invoiceDate,
                subtotal,
                deliveryFee,
                total,
                deliveryMethod,
                emailAddress,
                whatsappNumber,
                status,
                sendStatus,
                createdAt,
                updatedAt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generated', 'pending', ?, ?)
        """, (
            invoice_number,
            order["cmdId"],
            order["cmdClientId"],
            order["cmdDepotId"],
            now,
            data["subtotal"],
            data["delivery_fee"],
            data["total"],
            delivery_method,
            email_address,
            whatsapp_number,
            now,
            now,
        ))

        invoice_id = cursor.lastrowid

        if own_connection:
            conn.commit()

        invoice = cursor.execute("""
            SELECT *
            FROM invoices
            WHERE invoiceId = ?
        """, (invoice_id,)).fetchone()

        return {
            "success": True,
            "created": True,
            "invoice": dict(invoice),
        }

    except sqlite3.IntegrityError:
        if own_connection:
            conn.rollback()

        existing = conn.execute("""
            SELECT *
            FROM invoices
            WHERE commandId = ?
        """, (command_id,)).fetchone()

        if existing:
            return {
                "success": True,
                "created": False,
                "invoice": dict(existing),
            }

        raise

    except Exception:
        if own_connection:
            conn.rollback()
        raise

    finally:
        if own_connection:
            conn.close()



def generate_client_consolidated_invoice_pdf(
    client_invoice_id,
    conn=None
):
    """
    Generate a professional PDF for a consolidated client invoice.

    BUSINESS RULE:
    - One invoice covers ALL delivered commands of the client.
    - Every delivered command is displayed individually.
    - Every product belonging to those commands is displayed.
    - Product subtotals are consolidated.
    - Delivery is charged only ONCE.
    - Existing command-level PDF generation is untouched.
    """

    own_connection = conn is None

    if own_connection:
        conn = get_db_connection()

    try:
        cursor = conn.cursor()

        # ============================================================
        # CONSOLIDATED INVOICE
        # ============================================================

        invoice = cursor.execute("""
            SELECT *
            FROM client_invoices
            WHERE clientInvoiceId = ?
            LIMIT 1
        """, (client_invoice_id,)).fetchone()

        if not invoice:
            raise ValueError(
                f"Client consolidated invoice "
                f"{client_invoice_id} does not exist."
            )

        invoice = dict(invoice)

        # ============================================================
        # CLIENT
        # ============================================================

        client = cursor.execute("""
            SELECT
                userId,
                userName,
                userSurname,
                userMail,
                userPhone,
                userAddress,
                invoiceDeliveryMethod
            FROM Users
            WHERE userId = ?
              AND userRole = 2
            LIMIT 1
        """, (invoice["clientId"],)).fetchone()

        if not client:
            raise ValueError(
                f"Client {invoice['clientId']} does not exist."
            )

        client = dict(client)

        # ============================================================
        # ALL DELIVERED COMMANDS
        # ============================================================

        commands = cursor.execute("""
            SELECT
                c.cmdId,
                c.cmdNum,
                c.cmdClientId,
                c.cmdStatut,
                c.cmdPayementMethod,
                c.cmdTotal,
                c.cmdLivAddress,
                c.cmdDate,
                c.cmdZoneId,
                c.cmdDepotId,

                d.depotName,
                d.depotAddress,

                z.zoneName,
                z.deliveryFee

            FROM Commandes c

            LEFT JOIN depots d
                ON d.depotId = c.cmdDepotId

            LEFT JOIN delivery_zones z
                ON z.zoneId = c.cmdZoneId

            WHERE c.cmdClientId = ?
              AND c.cmdStatut = 'livree'

            ORDER BY
                c.cmdDate ASC,
                c.cmdId ASC
        """, (invoice["clientId"],)).fetchall()

        commands = [dict(row) for row in commands]

        if not commands:
            raise ValueError(
                "This client has no delivered commands."
            )

        # ============================================================
        # ITEMS FROM ALL COMMANDS
        # ============================================================

        command_ids = [
            command["cmdId"]
            for command in commands
        ]

        placeholders = ",".join(
            "?" for _ in command_ids
        )

        items = cursor.execute(f"""
            SELECT
                ac.artcmdId,
                ac.artcmdCmdId,
                ac.artcmdArticleId,
                ac.artcmdQuantity,
                ac.artcmdPrice,
                ac.artcmdSubTotal,

                a.articleVolume,
                a.articlePackage,

                p.productName

            FROM articlesCommandes ac

            JOIN Articles a
                ON a.articleId = ac.artcmdArticleId

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE ac.artcmdCmdId IN ({placeholders})

            ORDER BY
                ac.artcmdCmdId ASC,
                ac.artcmdId ASC
        """, command_ids).fetchall()

        items = [dict(row) for row in items]

        if not items:
            raise ValueError(
                "The consolidated invoice has no items."
            )

        items_by_command = {}

        for item in items:
            items_by_command.setdefault(
                item["artcmdCmdId"],
                []
            ).append(item)

        # ============================================================
        # FINANCIAL VALUES
        # ============================================================

        subtotal = sum(
            float(item["artcmdSubTotal"] or 0)
            for item in items
        )

        delivery_fee = float(
            invoice["deliveryFee"] or 0
        )

        total = subtotal + delivery_fee

        stored_subtotal = float(
            invoice["subtotal"] or 0
        )

        stored_total = float(
            invoice["total"] or 0
        )

        if abs(subtotal - stored_subtotal) >= 0.01:
            raise ValueError(
                "Consolidated invoice subtotal mismatch."
            )

        if abs(total - stored_total) >= 0.01:
            raise ValueError(
                "Consolidated invoice total mismatch."
            )

        # ============================================================
        # REPORTLAB
        # ============================================================

        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
            KeepTogether,
        )

        # ============================================================
        # PDF DIRECTORY
        # ============================================================

        pdf_dir = GENERATED_INVOICES_DIR
        pdf_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        safe_number = re.sub(
            r"[^A-Za-z0-9_.-]",
            "_",
            invoice["invoiceNumber"]
        )

        pdf_path = (
            pdf_dir
            / f"{safe_number}.pdf"
        )

        # ============================================================
        # DOCUMENT
        # ============================================================

        document = SimpleDocTemplate(
            str(pdf_path),
            pagesize=A4,
            rightMargin=15 * mm,
            leftMargin=15 * mm,
            topMargin=14 * mm,
            bottomMargin=15 * mm,
            title=invoice["invoiceNumber"],
            author="Depot Les Sources",
        )

        styles = getSampleStyleSheet()

        company_style = ParagraphStyle(
            "ClientInvoiceCompany",
            parent=styles["Normal"],
            fontSize=15,
            leading=18,
            alignment=TA_LEFT,
            spaceAfter=1 * mm,
        )

        invoice_title_style = ParagraphStyle(
            "ClientInvoiceTitle",
            parent=styles["Normal"],
            fontSize=21,
            leading=24,
            alignment=TA_RIGHT,
            spaceAfter=2 * mm,
        )

        invoice_number_style = ParagraphStyle(
            "ClientInvoiceNumber",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            alignment=TA_RIGHT,
        )

        section_style = ParagraphStyle(
            "ClientInvoiceSection",
            parent=styles["Heading2"],
            fontSize=11,
            leading=14,
            spaceBefore=5 * mm,
            spaceAfter=2.5 * mm,
        )

        normal_style = ParagraphStyle(
            "ClientInvoiceNormal",
            parent=styles["Normal"],
            fontSize=8.5,
            leading=11,
        )

        small_style = ParagraphStyle(
            "ClientInvoiceSmall",
            parent=styles["Normal"],
            fontSize=7.5,
            leading=9.5,
        )

        small_center_style = ParagraphStyle(
            "ClientInvoiceSmallCenter",
            parent=small_style,
            alignment=TA_CENTER,
        )

        right_style = ParagraphStyle(
            "ClientInvoiceRight",
            parent=normal_style,
            alignment=TA_RIGHT,
        )

        total_label_style = ParagraphStyle(
            "ClientInvoiceTotalLabel",
            parent=normal_style,
            fontSize=10,
            leading=13,
        )

        total_value_style = ParagraphStyle(
            "ClientInvoiceTotalValue",
            parent=normal_style,
            fontSize=14,
            leading=17,
            alignment=TA_RIGHT,
        )

        signature_style = ParagraphStyle(
            "ClientInvoiceSignature",
            parent=normal_style,
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
        )

        story = []

        # ============================================================
        # PROFESSIONAL HEADER
        # ============================================================

        company_name = "DEPOT LES SOURCES"

        header_left = [
            Paragraph(
                f"<b>{company_name}</b>",
                company_style
            ),
            Paragraph(
                "Gestion de stock et distribution",
                small_style
            ),
        ]

        header_right = [
            Paragraph(
                "<b>FACTURE CLIENT</b>",
                invoice_title_style
            ),
            Paragraph(
                f"<b>N? {invoice['invoiceNumber']}</b>",
                invoice_number_style
            ),
            Paragraph(
                f"Date : {invoice['invoiceDate']}",
                invoice_number_style
            ),
        ]

        header_table = Table(
            [
                [
                    header_left,
                    header_right,
                ]
            ],
            colWidths=[90 * mm, 80 * mm],
        )

        header_table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
            ])
        )

        story.append(header_table)
        story.append(Spacer(1, 5 * mm))

        # ============================================================
        # CLIENT INFORMATION
        # ============================================================

        client_name = (
            f"{client['userName'] or ''} "
            f"{client['userSurname'] or ''}"
        ).strip()

        client_name = client_name or "Client"

        client_phone = (
            str(client["userPhone"])
            if client["userPhone"] is not None
            else "Non renseign?"
        )

        client_email = (
            client["userMail"]
            or "Non renseign?"
        )

        client_address = (
            client["userAddress"]
            or "Non renseign?e"
        )

        client_data = [
            [
                Paragraph(
                    "<b>FACTUR? ?</b>",
                    normal_style
                ),
                Paragraph(
                    "<b>COORDONN?ES</b>",
                    normal_style
                ),
            ],
            [
                Paragraph(
                    f"<b>{client_name}</b>",
                    normal_style
                ),
                Paragraph(
                    f"Email : {client_email}",
                    normal_style
                ),
            ],
            [
                Paragraph(
                    f"Adresse : {client_address}",
                    normal_style
                ),
                Paragraph(
                    f"T?l?phone : {client_phone}",
                    normal_style
                ),
            ],
        ]

        client_table = Table(
            client_data,
            colWidths=[85 * mm, 85 * mm],
        )

        client_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ])
        )

        story.append(client_table)

        # ============================================================
        # ALL COMMANDS - SINGLE CONSOLIDATED TABLE
        # ============================================================

        story.append(
            Paragraph(
                "D?TAIL CONSOLID? DES COMMANDES",
                section_style
            )
        )

        # One single table contains every product from every
        # delivered command belonging to this client.
        #
        # The command number and command date identify the
        # command to which each product belongs.
        consolidated_table_data = [
            [
                Paragraph(
                    "<b>Commande</b>",
                    small_style
                ),
                Paragraph(
                    "<b>Date</b>",
                    small_style
                ),
                Paragraph(
                    "<b>D?signation</b>",
                    small_style
                ),
                Paragraph(
                    "<b>Qt?</b>",
                    small_center_style
                ),
                Paragraph(
                    "<b>Prix unitaire</b>",
                    right_style
                ),
                Paragraph(
                    "<b>Sous-total</b>",
                    right_style
                ),
            ]
        ]

        for item in items:

            command = next(
                (
                    command
                    for command in commands
                    if command["cmdId"] == item["artcmdCmdId"]
                ),
                None
            )

            if not command:
                continue

            command_number = (
                command["cmdNum"]
                or f"CMD-{command['cmdId']}"
            )

            command_date = (
                str(command["cmdDate"])
                if command["cmdDate"]
                else "-"
            )

            quantity = float(
                item["artcmdQuantity"] or 0
            )

            unit_price = float(
                item["artcmdPrice"] or 0
            )

            item_subtotal = float(
                item["artcmdSubTotal"] or 0
            )

            description = (
                item["productName"]
                or "Article"
            )

            details = []

            if item["articleVolume"]:
                details.append(
                    str(item["articleVolume"])
                )

            if item["articlePackage"]:
                details.append(
                    str(item["articlePackage"])
                )

            if details:
                description += (
                    " - "
                    + " - ".join(details)
                )

            consolidated_table_data.append([
                Paragraph(
                    command_number,
                    small_style
                ),
                Paragraph(
                    command_date,
                    small_style
                ),
                Paragraph(
                    description,
                    small_style
                ),
                Paragraph(
                    f"{quantity:g}",
                    small_center_style
                ),
                Paragraph(
                    format_xaf(unit_price),
                    right_style
                ),
                Paragraph(
                    format_xaf(item_subtotal),
                    right_style
                ),
            ])

        consolidated_item_table = Table(
            consolidated_table_data,
            colWidths=[
                28 * mm,
                24 * mm,
                55 * mm,
                15 * mm,
                24 * mm,
                24 * mm,
            ],
            repeatRows=1,
        )

        consolidated_item_table.setStyle(
            TableStyle([
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 0),
                    colors.whitesmoke
                ),
                (
                    "BOX",
                    (0, 0),
                    (-1, -1),
                    0.6,
                    colors.grey
                ),
                (
                    "INNERGRID",
                    (0, 0),
                    (-1, -1),
                    0.3,
                    colors.lightgrey
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "TOP"
                ),
                (
                    "LEFTPADDING",
                    (0, 0),
                    (-1, -1),
                    4
                ),
                (
                    "RIGHTPADDING",
                    (0, 0),
                    (-1, -1),
                    4
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    4
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    4
                ),
                (
                    "ALIGN",
                    (3, 1),
                    (3, -1),
                    "CENTER"
                ),
                (
                    "ALIGN",
                    (4, 1),
                    (5, -1),
                    "RIGHT"
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, 0),
                    "MIDDLE"
                ),
            ])
        )

        story.append(
            consolidated_item_table
        )

        story.append(
            Spacer(1, 3 * mm)
        )

        story.append(
            Paragraph(
                (
                    f"<b>{len(commands)}</b> commande(s) livr?e(s) "
                    f"- <b>{len(items)}</b> ligne(s) produit"
                ),
                ParagraphStyle(
                    "ConsolidatedCommandSummary",
                    parent=small_style,
                    alignment=TA_RIGHT,
                )
            )
        )

        # ============================================================
        # CONSOLIDATED TOTALS
        # ============================================================

        story.append(
            Paragraph(
                "R?CAPITULATIF FINANCIER",
                section_style
            )
        )

        delivery_zone = (
            commands[-1]["zoneName"]
            or "Zone de livraison"
        )

        totals_data = [
            [
                Paragraph(
                    "Total des produits",
                    normal_style
                ),
                Paragraph(
                    format_xaf(subtotal),
                    right_style
                ),
            ],
            [
                Paragraph(
                    (
                        "Frais de livraison ? "
                        f"{delivery_zone}"
                    ),
                    normal_style
                ),
                Paragraph(
                    format_xaf(delivery_fee),
                    right_style
                ),
            ],
            [
                Paragraph(
                    "<b>TOTAL TTC</b>",
                    total_label_style
                ),
                Paragraph(
                    f"<b>{format_xaf(total)}</b>",
                    total_value_style
                ),
            ],
        ]

        totals_table = Table(
            totals_data,
            colWidths=[105 * mm, 65 * mm],
            hAlign="RIGHT",
        )

        totals_table.setStyle(
            TableStyle([
                (
                    "BOX",
                    (0, 0),
                    (-1, -1),
                    0.7,
                    colors.grey
                ),
                (
                    "INNERGRID",
                    (0, 0),
                    (-1, 1),
                    0.3,
                    colors.lightgrey
                ),
                (
                    "BACKGROUND",
                    (0, 2),
                    (-1, 2),
                    colors.whitesmoke
                ),
                (
                    "LINEABOVE",
                    (0, 2),
                    (-1, 2),
                    1.2,
                    colors.black
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "MIDDLE"
                ),
                (
                    "LEFTPADDING",
                    (0, 0),
                    (-1, -1),
                    7
                ),
                (
                    "RIGHTPADDING",
                    (0, 0),
                    (-1, -1),
                    7
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    7
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    7
                ),
            ])
        )

        story.append(totals_table)

        # ============================================================
        # BILLING / DELIVERY INFORMATION
        # ============================================================

        story.append(
            Spacer(1, 5 * mm)
        )

        delivery_method = (
            invoice["deliveryMethod"]
            or "email"
        )

        if delivery_method.lower() == "whatsapp":
            delivery_method_label = "WhatsApp"
        else:
            delivery_method_label = "Email"

        billing_info = Table(
            [
                [
                    Paragraph(
                        "<b>Mode de r?ception :</b>",
                        small_style
                    ),
                    Paragraph(
                        delivery_method_label,
                        small_style
                    ),
                ],
                [
                    Paragraph(
                        "<b>Nombre de commandes :</b>",
                        small_style
                    ),
                    Paragraph(
                        str(len(commands)),
                        small_style
                    ),
                ],
                [
                    Paragraph(
                        "<b>R?gle de livraison :</b>",
                        small_style
                    ),
                    Paragraph(
                        "Frais appliqu?s une seule fois",
                        small_style
                    ),
                ],
            ],
            colWidths=[55 * mm, 115 * mm],
        )

        billing_info.setStyle(
            TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ])
        )

        story.append(billing_info)

        # ============================================================
        # INVOICE NOTE
        # ============================================================

        story.append(
            Spacer(1, 4 * mm)
        )

        story.append(
            Paragraph(
                (
                    "<b>Note :</b> Cette facture consolid?e regroupe "
                    "l'ensemble des commandes livr?es du client "
                    "mentionn?es dans le pr?sent document. "
                    "Les produits de toutes les commandes sont "
                    "additionn?s et les frais de livraison sont "
                    "appliqu?s une seule fois."
                ),
                small_style
            )
        )

        # ============================================================
        # SIGNATURES
        # ============================================================

        story.append(
            Spacer(1, 9 * mm)
        )

        story.append(
            Paragraph(
                "VALIDATION ET SIGNATURES",
                section_style
            )
        )

        accountant_signature = [
            Paragraph(
                "<b>SIGNATURE DU COMPTABLE</b>",
                signature_style
            ),
            Spacer(1, 14 * mm),
            Paragraph(
                "Signature / Cachet",
                small_center_style
            ),
            Spacer(1, 5 * mm),
            Paragraph(
                "Date : ____________________",
                small_center_style
            ),
        ]

        client_signature = [
            Paragraph(
                "<b>SIGNATURE DU CLIENT</b>",
                signature_style
            ),
            Spacer(1, 14 * mm),
            Paragraph(
                "Signature",
                small_center_style
            ),
            Spacer(1, 5 * mm),
            Paragraph(
                "Date : ____________________",
                small_center_style
            ),
        ]

        signature_table = Table(
            [
                [
                    accountant_signature,
                    client_signature,
                ]
            ],
            colWidths=[85 * mm, 85 * mm],
            rowHeights=[42 * mm],
        )

        signature_table.setStyle(
            TableStyle([
                (
                    "BOX",
                    (0, 0),
                    (-1, -1),
                    0.6,
                    colors.grey
                ),
                (
                    "INNERGRID",
                    (0, 0),
                    (-1, -1),
                    0.4,
                    colors.lightgrey
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "TOP"
                ),
                (
                    "LEFTPADDING",
                    (0, 0),
                    (-1, -1),
                    7
                ),
                (
                    "RIGHTPADDING",
                    (0, 0),
                    (-1, -1),
                    7
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    7
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    7
                ),
            ])
        )

        story.append(signature_table)

        # ============================================================
        # FOOTER
        # ============================================================

        story.append(
            Spacer(1, 5 * mm)
        )

        story.append(
            Paragraph(
                (
                    f"Document g?n?r? le "
                    f"{datetime.now().strftime('%d/%m/%Y ? %H:%M')} "
                    f"? {company_name}"
                ),
                ParagraphStyle(
                    "InvoiceFooter",
                    parent=small_style,
                    alignment=TA_CENTER,
                )
            )
        )

        # ============================================================
        # BUILD PDF
        # ============================================================

        document.build(story)

        # ============================================================
        # SAVE PDF PATH
        # ============================================================

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            UPDATE client_invoices
            SET
                pdfPath = ?,
                status = 'generated',
                updatedAt = ?
            WHERE clientInvoiceId = ?
        """, (
            str(pdf_path),
            now,
            client_invoice_id,
        ))

        if own_connection:
            conn.commit()

        return str(pdf_path)

    except Exception:
        if own_connection:
            conn.rollback()
        raise

    finally:
        if own_connection:
            conn.close()


def generate_invoice_pdf(invoice_id):
    """
    Generate a professional PDF for an existing invoice.

    PDF files are stored outside the static directory so that invoices
    are never exposed as ordinary public static assets.
    """
    conn = get_db_connection()

    try:
        invoice = conn.execute("""
            SELECT
                i.*,

                c.cmdNum,
                c.cmdDate,
                c.cmdStatut,
                c.cmdPayementMethod,
                c.cmdLivAddress,

                u.userName,
                u.userSurname,
                u.userMail,
                u.userPhone,

                d.depotName,
                d.depotAddress,

                z.zoneName

            FROM invoices i

            JOIN Commandes c
                ON c.cmdId = i.commandId

            JOIN users u
                ON u.userId = i.clientId

            LEFT JOIN depots d
                ON d.depotId = i.depotId

            LEFT JOIN delivery_zones z
                ON z.zoneId = c.cmdZoneId

            WHERE i.invoiceId = ?
        """, (invoice_id,)).fetchone()

        if not invoice:
            raise ValueError(
                f"Invoice {invoice_id} does not exist."
            )

        items = conn.execute("""
            SELECT
                ac.artcmdQuantity,
                ac.artcmdPrice,
                ac.artcmdSubTotal,

                a.articleVolume,
                a.articlePackage,

                p.productName

            FROM articlesCommandes ac

            JOIN Articles a
                ON a.articleId = ac.artcmdArticleId

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE ac.artcmdCmdId = ?

            ORDER BY ac.artcmdId ASC
        """, (invoice["commandId"],)).fetchall()

        INVOICE_STORAGE_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        filename = (
            f"{invoice['invoiceNumber']}.pdf"
        )

        pdf_path = INVOICE_STORAGE_DIR / filename

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "InvoiceTitle",
            parent=styles["Title"],
            fontSize=20,
            leading=24,
            alignment=TA_CENTER,
            spaceAfter=8,
        )

        normal_style = ParagraphStyle(
            "InvoiceNormal",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
        )

        right_style = ParagraphStyle(
            "InvoiceRight",
            parent=normal_style,
            alignment=TA_RIGHT,
        )

        small_style = ParagraphStyle(
            "InvoiceSmall",
            parent=normal_style,
            fontSize=8,
            leading=10,
        )

        doc = SimpleDocTemplate(
            str(pdf_path),
            pagesize=A4,
            rightMargin=15 * mm,
            leftMargin=15 * mm,
            topMargin=15 * mm,
            bottomMargin=15 * mm,
            title=f"Facture {invoice['invoiceNumber']}",
            author="Dépôt Les Sources",
        )

        story = []

        story.append(
            Paragraph(
                "DÉPÔT LES SOURCES",
                title_style
            )
        )

        story.append(
            Paragraph(
                "FACTURE",
                ParagraphStyle(
                    "FactureLabel",
                    parent=title_style,
                    fontSize=13,
                    leading=16,
                )
            )
        )

        story.append(Spacer(1, 6))

        company_name = (
            invoice["depotName"]
            or "Dépôt Les Sources"
        )

        company_address = (
            invoice["depotAddress"]
            or ""
        )

        client_name = " ".join(
            part
            for part in [
                invoice["userName"] or "",
                invoice["userSurname"] or "",
            ]
            if part
        ).strip()

        if not client_name:
            client_name = "Client"

        invoice_info = [
            [
                Paragraph(
                    f"<b>Facture :</b><br/>{invoice['invoiceNumber']}",
                    normal_style
                ),
                Paragraph(
                    f"<b>Commande :</b><br/>{invoice['cmdNum'] or invoice['commandId']}",
                    normal_style
                ),
            ],
            [
                Paragraph(
                    f"<b>Date :</b><br/>{invoice['invoiceDate']}",
                    normal_style
                ),
                Paragraph(
                    f"<b>Statut :</b><br/>{invoice['cmdStatut']}",
                    normal_style
                ),
            ],
        ]

        info_table = Table(
            invoice_info,
            colWidths=[85 * mm, 85 * mm]
        )

        info_table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ])
        )

        story.append(info_table)
        story.append(Spacer(1, 12))

        customer_data = [
            [
                Paragraph(
                    "<b>CLIENT</b>",
                    normal_style
                ),
                Paragraph(
                    "<b>DÉPÔT</b>",
                    normal_style
                ),
            ],
            [
                Paragraph(
                    f"{client_name}<br/>"
                    f"{invoice['userMail'] or ''}<br/>"
                    f"{invoice['userPhone'] or ''}<br/>"
                    f"{invoice['cmdLivAddress'] or ''}",
                    normal_style
                ),
                Paragraph(
                    f"{company_name}<br/>"
                    f"{company_address}<br/>"
                    f"{invoice['zoneName'] or ''}",
                    normal_style
                ),
            ],
        ]

        customer_table = Table(
            customer_data,
            colWidths=[85 * mm, 85 * mm]
        )

        customer_table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ])
        )

        story.append(customer_table)
        story.append(Spacer(1, 14))

        table_data = [
            [
                Paragraph("<b>Désignation</b>", normal_style),
                Paragraph("<b>Qté</b>", right_style),
                Paragraph("<b>Prix unitaire</b>", right_style),
                Paragraph("<b>Sous-total</b>", right_style),
            ]
        ]

        for item in items:
            description = item["productName"] or "Article"

            if item["articleVolume"]:
                description += (
                    f" — {item['articleVolume']}"
                )

            if item["articlePackage"]:
                description += (
                    f" ({item['articlePackage']})"
                )

            table_data.append([
                Paragraph(
                    description,
                    small_style
                ),
                Paragraph(
                    str(item["artcmdQuantity"]),
                    right_style
                ),
                Paragraph(
                    format_xaf(item["artcmdPrice"]),
                    right_style
                ),
                Paragraph(
                    format_xaf(item["artcmdSubTotal"]),
                    right_style
                ),
            ])

        items_table = Table(
            table_data,
            colWidths=[
                80 * mm,
                18 * mm,
                36 * mm,
                36 * mm,
            ],
            repeatRows=1,
        )

        items_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ])
        )

        story.append(items_table)
        story.append(Spacer(1, 12))

        totals_data = [
            [
                Paragraph("<b>Sous-total produits</b>", normal_style),
                Paragraph(
                    f"<b>{format_xaf(invoice['subtotal'])}</b>",
                    right_style
                ),
            ],
            [
                Paragraph("<b>Livraison</b>", normal_style),
                Paragraph(
                    f"<b>{format_xaf(invoice['deliveryFee'])}</b>",
                    right_style
                ),
            ],
            [
                Paragraph("<b>TOTAL TTC</b>", normal_style),
                Paragraph(
                    f"<b>{format_xaf(invoice['total'])}</b>",
                    right_style
                ),
            ],
        ]

        totals_table = Table(
            totals_data,
            colWidths=[120 * mm, 50 * mm]
        )

        totals_table.setStyle(
            TableStyle([
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#eeeeee")),
            ])
        )

        story.append(totals_table)
        story.append(Spacer(1, 16))

        story.append(
            Paragraph(
                f"<b>Mode de paiement :</b> "
                f"{invoice['cmdPayementMethod'] or 'Non précisé'}",
                normal_style
            )
        )

        story.append(
            Paragraph(
                f"<b>Mode d'envoi de facture :</b> "
                f"{invoice['deliveryMethod']}",
                normal_style
            )
        )

        story.append(Spacer(1, 18))

        story.append(
            Paragraph(
                "Merci pour votre confiance.",
                ParagraphStyle(
                    "Footer",
                    parent=normal_style,
                    alignment=TA_CENTER,
                    fontSize=9,
                )
            )
        )

        doc.build(story)

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        conn.execute("""
            UPDATE invoices
            SET
                pdfPath = ?,
                status = 'generated',
                updatedAt = ?
            WHERE invoiceId = ?
        """, (
            str(pdf_path),
            now,
            invoice_id,
        ))

        conn.commit()

        return {
            "success": True,
            "invoiceId": invoice_id,
            "invoiceNumber": invoice["invoiceNumber"],
            "pdfPath": str(pdf_path),
            "pdfExists": pdf_path.exists(),
            "pdfSize": pdf_path.stat().st_size
                if pdf_path.exists()
                else 0,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()
