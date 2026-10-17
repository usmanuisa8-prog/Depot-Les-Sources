from pathlib import Path
import email
import os
from dotenv import load_dotenv

from flask import (
    Flask,
    flash, 
    render_template, 
    request, 
    redirect, 
    url_for, 
    session, 
    jsonify,
    send_file
)

import sqlite3
import re
import traceback
from flask import jsonify, request, session
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from storage_config import DATABASE_PATH, resolve_invoice_pdf_path
from invoice_service import (
    create_invoice_for_order,
    generate_invoice_pdf,
    normalize_whatsapp_number,
    create_client_consolidated_invoice,
    generate_client_consolidated_invoice_pdf,
)
LOYALTY_XAF_PER_POINT = 10000
LOYALTY_POINT_VALUE = 100

load_dotenv()

app = Flask(__name__)


# ============================================================
# ADMIN SESSION PROTECTION
# ============================================================
# Admin pages require a valid session in the current browser.
# A copied admin URL opened in another browser therefore cannot
# reuse the administrator's authenticated session.

@app.before_request
def protect_admin_session():
    """
    Protect administrator pages from access without a valid
    authenticated administrator session.

    A browser that does not contain the administrator's session
    cookie is redirected to the login page.
    """

    # Admin URLs
    is_admin_area = (
        request.path == "/dashboard"
        or request.path.startswith("/admin/")
    )

    if not is_admin_area:
        return None

    # No session in this browser
    user_id = session.get("user_id")

    if not user_id:
        session.clear()
        return redirect("/login")

    # Verify the session against the database.
    conn = get_connexion()

    try:
        user = conn.execute(
            """
            SELECT userId, userRole
            FROM Users
            WHERE userId = ?
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()
    finally:
        conn.close()

    # Session refers to a user that no longer exists
    if not user:
        session.clear()
        return redirect("/login")

    # Only administrators can access admin pages
    if int(user["userRole"]) != 1:
        session.clear()
        return redirect("/login")

    # Disabled account
    if "userState" in user.keys() and not user["userState"]:
        session.clear()
        return redirect("/login")

    return None

secret_key = os.environ.get('FLASK_SECRET_KEY')
if not secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is missing. Configure it in your local .env file."
    )
app.secret_key = secret_key

def get_connexion():
    connexion = sqlite3.connect(DATABASE_PATH)
    connexion.row_factory = sqlite3.Row

    connexion.execute("PRAGMA foreign_keys = ON")

    return connexion

# ---------------------------------------------------------
# DELIVERY HISTORY
# ---------------------------------------------------------

def record_delivery_history(
    commandId,
    old_status,
    new_status,
    changed_by,
    cursor
):
    """
    Record a delivery/order status transition.

    The history record is written using the same database
    cursor/transaction as the order status update.
    """

    if old_status == new_status:
        return

    changed_at = datetime.now().isoformat(
        timespec="seconds"
    )

    cursor.execute("""
        INSERT INTO delivery_history (
            cmdId,
            old_status,
            new_status,
            changed_by,
            changed_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        commandId,
        old_status,
        new_status,
        changed_by,
        changed_at
    ))


# ---------------------------------------------------------
# ---------------------------------------------------------
# DELIVERY TIMING
# ---------------------------------------------------------

def get_delivery_timing(commandId, cursor):
    """
    Calculate actual order timing from delivery history.
    """

    cursor.execute("""
        SELECT
            old_status,
            new_status,
            changed_at
        FROM delivery_history
        WHERE cmdId = ?
        ORDER BY id ASC
    """, (commandId,))

    history = cursor.fetchall()

    preparation_start = None
    preparation_end = None
    delivery_start = None
    delivery_end = None
    first_timestamp = None

    for row in history:
        try:
            timestamp = datetime.fromisoformat(row['changed_at'])
        except (TypeError, ValueError):
            continue

        if first_timestamp is None:
            first_timestamp = timestamp

        if row['old_status'] == 'en_attente' and row['new_status'] == 'en_preparation':
            preparation_start = timestamp

        elif row['old_status'] == 'en_preparation' and row['new_status'] == 'en_livraison':
            preparation_end = timestamp
            delivery_start = timestamp

        elif row['old_status'] == 'en_attente' and row['new_status'] == 'en_livraison':
            delivery_start = timestamp

        elif row['old_status'] == 'en_livraison' and row['new_status'] == 'livree':
            delivery_end = timestamp

    preparation_seconds = None
    if preparation_start and preparation_end:
        preparation_seconds = int((preparation_end - preparation_start).total_seconds())

    delivery_seconds = None
    if delivery_start and delivery_end:
        delivery_seconds = int((delivery_end - delivery_start).total_seconds())

    total_seconds = None
    if first_timestamp and delivery_end:
        total_seconds = int((delivery_end - first_timestamp).total_seconds())

    return {
        'preparation_seconds': preparation_seconds,
        'delivery_seconds': delivery_seconds,
        'total_seconds': total_seconds
    }


def format_delivery_duration(seconds):
    """
    Convert a duration in seconds into a readable format.
    """

    if seconds is None:
        return None

    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return None

    if seconds < 0:
        return None

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []

    if hours:
        parts.append(f'{hours}h')

    if minutes:
        parts.append(f'{minutes}m')

    if seconds or not parts:
        parts.append(f'{seconds}s')

    return ' '.join(parts)


def get_delivery_zone_statistics(cursor):
    """Calculate delivery performance statistics by zone."""

    # ---------------------------------------------------------
    # Get all configured delivery zones
    # ---------------------------------------------------------

    cursor.execute("""
        SELECT
            zoneId,
            zoneName,
            estimatedMinutes
        FROM delivery_zones
        ORDER BY zoneId ASC
    """)

    zones = cursor.fetchall()

    zone_stats = {}

    # ---------------------------------------------------------
    # Initialize every configured zone
    # ---------------------------------------------------------

    for zone in zones:

        zone_stats[zone["zoneId"]] = {
            "zone_id": zone["zoneId"],
            "zone_name": zone["zoneName"],
            "delivery_count": 0,
            "preparation_seconds": [],
            "delivery_seconds": [],
            "total_seconds": [],
            "estimated_minutes": (
                float(zone["estimatedMinutes"])
                if zone["estimatedMinutes"] is not None
                else None
            )
        }

    # ---------------------------------------------------------
    # Get completed orders
    # ---------------------------------------------------------

    cursor.execute("""
        SELECT
            c.cmdId,
            c.cmdZoneId
        FROM Commandes c
        WHERE c.cmdStatut = 'livree'
        ORDER BY c.cmdId ASC
    """)

    orders = cursor.fetchall()

    # ---------------------------------------------------------
    # Calculate statistics for completed deliveries
    # ---------------------------------------------------------

    unknown_zone = {
        "zone_id": None,
        "zone_name": "Zone inconnue",
        "delivery_count": 0,
        "preparation_seconds": [],
        "delivery_seconds": [],
        "total_seconds": [],
        "estimated_minutes": None
    }

    for order in orders:

        timing = get_delivery_timing(
            order["cmdId"],
            cursor
        )

        # Ignore deliveries without timing information
        if timing["delivery_seconds"] is None:
            continue

        zone_id = order["cmdZoneId"]

        # -----------------------------------------------------
        # Known zone
        # -----------------------------------------------------

        if zone_id in zone_stats:

            stats = zone_stats[zone_id]

        # -----------------------------------------------------
        # Missing / unknown zone
        # -----------------------------------------------------

        else:

            stats = unknown_zone

        stats["delivery_count"] += 1

        # Preparation time
        if timing["preparation_seconds"] is not None:

            stats["preparation_seconds"].append(
                timing["preparation_seconds"]
            )

        # Delivery time
        stats["delivery_seconds"].append(
            timing["delivery_seconds"]
        )

        # Total time
        if timing["total_seconds"] is not None:

            stats["total_seconds"].append(
                timing["total_seconds"]
            )

    # ---------------------------------------------------------
    # Calculate averages
    # ---------------------------------------------------------

    def average(values):

        if not values:
            return None

        return round(
            sum(values) / len(values),
            1
        )

    results = []

    # ---------------------------------------------------------
    # Return configured zones
    # ---------------------------------------------------------

    for stats in zone_stats.values():

        results.append({
            "zone_id": stats["zone_id"],
            "zone_name": stats["zone_name"],
            "delivery_count": stats["delivery_count"],
            "average_preparation_seconds": average(
                stats["preparation_seconds"]
            ),
            "average_delivery_seconds": average(
                stats["delivery_seconds"]
            ),
            "average_total_seconds": average(
                stats["total_seconds"]
            ),
            "average_estimated_minutes": (
                stats["estimated_minutes"]
            )
        })

    # ---------------------------------------------------------
    # Add unknown-zone statistics only when necessary
    # ---------------------------------------------------------

    if unknown_zone["delivery_count"] > 0:

        results.append({
            "zone_id": None,
            "zone_name": "Zone inconnue",
            "delivery_count": unknown_zone["delivery_count"],
            "average_preparation_seconds": average(
                unknown_zone["preparation_seconds"]
            ),
            "average_delivery_seconds": average(
                unknown_zone["delivery_seconds"]
            ),
            "average_total_seconds": average(
                unknown_zone["total_seconds"]
            ),
            "average_estimated_minutes": None
        })

    return results

# LOYALTY FUNCTIONS
# ---------------------------------------------------------

def get_loyalty_account(user_id, cursor):
    """
    Get the loyalty account for a customer.

    If the customer does not yet have an account,
    create one automatically.
    """

    cursor.execute("""
        SELECT
            id,
            user_id,
            points_balance,
            created_at,
            updated_at
        FROM loyalty_accounts
        WHERE user_id = ?
    """, (user_id,))

    account = cursor.fetchone()

    if account:
        return account

    now = datetime.now().isoformat(timespec="seconds")

    cursor.execute("""
        INSERT INTO loyalty_accounts (
            user_id,
            points_balance,
            created_at,
            updated_at
        )
        VALUES (?, 0, ?, ?)
    """, (
        user_id,
        now,
        now
    ))

    cursor.execute("""
        SELECT
            id,
            user_id,
            points_balance,
            created_at,
            updated_at
        FROM loyalty_accounts
        WHERE user_id = ?
    """, (user_id,))

    return cursor.fetchone()


def calculate_loyalty_points(total_amount):
    """
    Calculate the number of loyalty points earned
    from a completed purchase.
    """

    if total_amount is None:
        return 0

    try:
        total_amount = float(total_amount)
    except (TypeError, ValueError):
        return 0

    if total_amount <= 0:
        return 0

    return int(total_amount // LOYALTY_XAF_PER_POINT)


def award_loyalty_points(
    user_id,
    commandId,
    total_amount,
    cursor
):
    """
    Award loyalty points for a completed order.

    Only customer accounts (userRole = 2) can earn loyalty points.
    The same order can never award points twice.
    """

    # -----------------------------------------------------
    # Loyalty eligibility
    # -----------------------------------------------------
    # Only actual customers can earn loyalty points.
    # Admins and Livreurs must never receive rewards.
    cursor.execute("""
        SELECT userRole
        FROM users
        WHERE userId = ?
        LIMIT 1
    """, (user_id,))

    user = cursor.fetchone()

    if not user or user["userRole"] != 2:
        return 0

    # -----------------------------------------------------
    # Prevent duplicate reward
    # -----------------------------------------------------

    cursor.execute("""
        SELECT id
        FROM loyalty_transactions
        WHERE command_id = ?
          AND type = 'PURCHASE'
        LIMIT 1
    """, (commandId,))

    existing_transaction = cursor.fetchone()

    if existing_transaction:
        return 0

    # -----------------------------------------------------
    # Calculate points
    # -----------------------------------------------------

    points = calculate_loyalty_points(total_amount)

    if points <= 0:
        return 0

    # -----------------------------------------------------
    # Get/create loyalty account
    # -----------------------------------------------------

    account = get_loyalty_account(
        user_id,
        cursor
    )

    now = datetime.now().isoformat(
        timespec="seconds"
    )

    # -----------------------------------------------------
    # Update balance
    # -----------------------------------------------------

    new_balance = (
        account["points_balance"] + points
    )

    cursor.execute("""
        UPDATE loyalty_accounts
        SET
            points_balance = ?,
            updated_at = ?
        WHERE user_id = ?
    """, (
        new_balance,
        now,
        user_id
    ))

    # -----------------------------------------------------
    # Record transaction
    # -----------------------------------------------------

    cursor.execute("""
        INSERT INTO loyalty_transactions (
            user_id,
            command_id,
            type,
            points,
            description,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        commandId,
        "PURCHASE",
        points,
        f"Loyalty points earned from order #{commandId}",
        now
    ))

    return points

@app.route("/")
def index():
    connexion = sqlite3.connect(DATABASE_PATH)
    connexion.row_factory = sqlite3.Row
    curseur = connexion.cursor()
    curseur.execute("SELECT *  FROM categories WHERE categoryState = 1 ORDER BY displayOrder ASC LIMIT 3")
    lignes = curseur.fetchall()
    categories = [dict(ligne) for ligne in lignes]

    products = []

    for category in categories:
        curseur.execute("SELECT * FROM products WHERE productCategoryId = ? ORDER BY productName ASC LIMIT 3", (category["categoryId"],))
        category["products"] = ([dict(ligne) for ligne in curseur.fetchall()])
    connexion.close()

    return render_template("index.html", categories=categories, products=products)

def get_categories():
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute("SELECT * FROM categories ORDER BY displayOrder ASC")
    categories = [dict(ligne) for ligne in curseur.fetchall()]

    connexion.close()
    return categories

def get_top_products(categoryId):
    conn = get_connexion()
    curseur = conn.cursor()

    query = "SELECT p.productName as articleName, a.articleId, c.categoryName, a.articleImage, a.articleVolume, ac.artcmdArticleId, a.articlePrice, SUM(ac.artcmdQuantity) as totalSelled FROM articlesCommandes ac JOIN Articles a ON ac.artcmdArticleId = a.articleId JOIN products p ON p.productId = a.articleproductId JOIN categories c ON c.categoryId = p.productCategoryId"
    params = []

    if categoryId :
        query += " WHERE c.categoryId = ?"
        params.append(categoryId)

    query += " GROUP BY ac.artcmdArticleId ORDER BY totalSelled DESC LIMIT 10"
    curseur.execute(query, params)
    topProducts= [dict(ligne) for ligne in curseur.fetchall()]
    
    return topProducts

def get_new_products(categoryId):

    conn = get_connexion()
    curseur = conn.cursor()

    query = "SELECT * FROM products WHERE productState = 1 AND productCreateDate >= date('now', '-10 days')"
    params = []

    if categoryId :
        query += "AND productCategoryId = ?"
        params.append(categoryId)

    query += " ORDER BY productCreateDate DESC"
    curseur.execute(query, params)
    newProducts= [dict(ligne) for ligne in curseur.fetchall()]
    
    return newProducts

def get_on_sale_products(categoryId):

    conn = get_connexion()
    curseur = conn.cursor()

    query = "SELECT a.articleId, a.articleOnSalePerc, c.categoryName, p.productName, a.articleStock, a.articlePrice, (a.articlePrice - (a.articlePrice * a.articleOnSalePerc / 100)) AS articleEndPrice, a.articleImage FROM Articles a, categories c, products p  WHERE a.articleProductId = p.productId AND p.productCategoryId = c.categoryId AND a.articleOnSale = 1"
    params = []

    if categoryId :
        query += (" AND c.categoryId = ? ")
        params.append(categoryId)

    curseur.execute(query, params)
    newProducts= [dict(ligne) for ligne in curseur.fetchall()]
    
    return newProducts

def get_suggested_products(category):
    suggestedProducts= []
    return suggestedProducts

def get_category_id_by_slug(selectedCategory):
    connexion = get_connexion()
    curseur = connexion.cursor()
    curseur.execute("SELECT categoryId FROM categories WHERE categorySlug = ?", (selectedCategory,))
    categoryId = curseur.fetchone()
    connexion.close()
    if categoryId:
        return categoryId[0]
    else:
        return None

def get_cart_details(ids):

    placeholders = ','.join('?' * len(ids))
    connexion = get_connexion()
    curseur = connexion.cursor()

    curseur.execute(f"""
        SELECT a.articleId, a.articlebulkLimit, a.articlebulkPrice, p.productName, a.articlePrice, a.articleOnSale, a.articleOnSalePerc, a.articleImage, a.articleVolume
        FROM articles a
        JOIN products p ON p.productId = a.articleProductId
        WHERE a.articleId IN ({placeholders})
    """, ids)
    resultats = curseur.fetchall()
    connexion.close()

    articles = []
    for r in resultats:
        id, bulkLimit, bulkPrice, name, price, onSale, percentage, image, volume = r
        finalPrice = price - (price * percentage / 100) if onSale else price
        bulkFinalPrice = bulkPrice - (bulkPrice * percentage / 100) if onSale else bulkPrice
        articles.append({
            "articleId": id,
            "articleName": name,
            "articlePrice": price,
            "articleFinalPrice": finalPrice,
            "articleOnSale": bool(onSale),
            "articleImage": image,
            "articleVolume": volume,
            "articleBulkLimit": bulkLimit,
            "articleBulkPrice": bulkPrice ,
            "articleBulkFinalPrice": bulkFinalPrice
        })
    return articles

def calculer_prix_article(prix_detail, prix_gros, quantite, seuil_gros) :
    if quantite >= seuil_gros :
        return quantite * prix_gros
    else : 
        return prix_detail * quantite
    
def determinate_price(prix_detail, prix_gros, quantity, bulk_limit) :
    if quantity >= bulk_limit :
        return prix_gros
    else : 
        return prix_detail

        
        
@app.route("/api/cart/details", methods=["POST"])
def cart_details():
    data = request.get_json(silent=True) or {}
    ids = data.get("article_ids", [])

    if not isinstance(ids, list) or not ids:
        return jsonify([])

    try:
        ids = list({
            int(article_id)
            for article_id in ids
        })
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "Invalid article IDs."
        }), 400

    articles = get_cart_details(ids)

    if not articles:
        return jsonify([])

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        today = datetime.now().strftime("%Y-%m-%d")

        for article in articles:

            article_id = article["articleId"]

            # -------------------------------------------------
            # Determine quantity requested by the cart.
            # The frontend may send quantities separately.
            # Default to one for display purposes.
            # -------------------------------------------------

            quantity = 1

            requested_quantities = data.get("quantities", {})

            if isinstance(requested_quantities, dict):
                try:
                    quantity = int(
                        requested_quantities.get(
                            str(article_id),
                            requested_quantities.get(article_id, 1)
                        )
                    )
                except (TypeError, ValueError):
                    quantity = 1

            quantity = max(1, quantity)

            # -------------------------------------------------
            # Normal price including existing bulk pricing
            # -------------------------------------------------

            normal_unit_price = float(
                determinate_price(
                    article["articleFinalPrice"],
                    article["articleBulkFinalPrice"],
                    quantity,
                    article["articleBulkLimit"]
                )
            )

            normal_subtotal = (
                normal_unit_price * quantity
            )

            # -------------------------------------------------
            # Find active promotions
            # -------------------------------------------------

            cursor.execute("""
                SELECT
                    p.promotionId,
                    p.promotionName,
                    p.promotionType,
                    p.discountValue
                FROM promotions p
                JOIN promotion_articles pa
                    ON pa.promotionId = p.promotionId
                WHERE pa.articleId = ?
                  AND p.promotionActive = 1
                  AND date(p.startDate) <= date('now')
                  AND date(p.endDate) >= date('now')
                  AND date(?) >= date(p.startDate)
                  AND date(?) <= date(p.endDate)
                ORDER BY p.promotionId ASC
            """, (
                article_id,
                today,
                today
            ))

            promotions = cursor.fetchall()

            best_promotion = None
            best_discount = 0.0

            for promotion in promotions:

                if promotion["promotionType"] == "percentage":

                    discount = (
                        normal_subtotal
                        * float(promotion["discountValue"])
                        / 100.0
                    )

                elif promotion["promotionType"] == "fixed":

                    discount = (
                        float(promotion["discountValue"])
                        * quantity
                    )

                else:
                    continue

                discount = min(
                    discount,
                    normal_subtotal
                )

                if discount > best_discount:

                    best_discount = discount
                    best_promotion = promotion

            # -------------------------------------------------
            # Final promotion-adjusted price
            # -------------------------------------------------

            discounted_subtotal = max(
                0.0,
                normal_subtotal - best_discount
            )

            discounted_unit_price = (
                discounted_subtotal / quantity
                if quantity > 0
                else normal_unit_price
            )

            # -------------------------------------------------
            # Additional cart pricing information
            # -------------------------------------------------

            article["cartQuantity"] = quantity
            article["cartNormalUnitPrice"] = normal_unit_price
            article["cartNormalSubtotal"] = normal_subtotal
            article["promotionId"] = (
                best_promotion["promotionId"]
                if best_promotion else None
            )
            article["promotionName"] = (
                best_promotion["promotionName"]
                if best_promotion else None
            )
            article["promotionType"] = (
                best_promotion["promotionType"]
                if best_promotion else None
            )
            article["promotionDiscount"] = best_discount
            article["cartUnitPrice"] = discounted_unit_price
            article["cartSubtotal"] = discounted_subtotal

        return jsonify(articles)

    finally:
        conn.close()
# ---------------------------------------------------------
# DELIVERY ZONES
# ---------------------------------------------------------

@app.route("/api/delivery-zones", methods=["GET"])
def get_delivery_zones():

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT
                zoneId,
                zoneName,
                deliveryFee,
                estimatedMinutes
            FROM delivery_zones
            WHERE active = 1
            ORDER BY zoneId ASC
        """)

        zones = cursor.fetchall()

        return jsonify([
            {
                "zoneId": zone["zoneId"],
                "zoneName": zone["zoneName"],
                "deliveryFee": zone["deliveryFee"],
                "estimatedMinutes": zone["estimatedMinutes"]
            }
            for zone in zones
        ])

    finally:
        conn.close()

@app.route("/api/cart/save_command", methods=["POST"])
def save_command():

    # ---------------------------------------------------------
    # Make sure the customer is logged in
    # ---------------------------------------------------------

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "success": False,
            "error": "You must be logged in to place an order."
        }), 401

    # ---------------------------------------------------------
    # Read request data
    # ---------------------------------------------------------

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Invalid order data."
        }), 400

    cart = data.get("cart", [])
    delivery_address = data.get("deliveryAddress", "").strip()
    payment_method = (
        data.get("paymentMethod")
        or data.get("payementMethod")
        or ""
    ).strip()
    zone_id = data.get("zoneId")

    # ---------------------------------------------------------
    # Basic validation
    # ---------------------------------------------------------

    if not cart:
        return jsonify({
            "success": False,
            "error": "Your cart is empty."
        }), 400

    if not delivery_address:
        return jsonify({
            "success": False,
            "error": "Delivery address is required."
        }), 400

    if not payment_method:
        return jsonify({
            "success": False,
            "error": "Payment method is required."
        }), 400

    # ---------------------------------------------------------
    # Validate delivery zone ID
    # ---------------------------------------------------------

    try:
        zone_id = int(zone_id)
    except (TypeError, ValueError):

        return jsonify({
            "success": False,
            "error": "A valid delivery zone is required."
        }), 400

    # ---------------------------------------------------------
    # Extract article IDs
    # ---------------------------------------------------------

    ids = []

    for item in cart:

        try:
            article_id = int(item["articleId"])
            quantity = int(item["quantity"])

        except (KeyError, TypeError, ValueError):

            return jsonify({
                "success": False,
                "error": "Invalid cart item."
            }), 400

        if quantity <= 0:

            return jsonify({
                "success": False,
                "error": "Invalid product quantity."
            }), 400

        ids.append(article_id)

    # ---------------------------------------------------------
    # Remove duplicate IDs
    # ---------------------------------------------------------

    ids = list(set(ids))

    # ---------------------------------------------------------
    # Get current product information
    # ---------------------------------------------------------

    articles = get_cart_details(ids)

    if not articles:

        return jsonify({
            "success": False,
            "error": "The selected products could not be found."
        }), 400

    # ---------------------------------------------------------
    # Prepare article map
    # ---------------------------------------------------------

    article_map = {
        article["articleId"]: article
        for article in articles
    }

    # ---------------------------------------------------------
    # Database transaction
    # ---------------------------------------------------------

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -----------------------------------------------------
        # Get active delivery zone
        # -----------------------------------------------------

        cursor.execute("""
            SELECT
                zoneId,
                zoneName,
                deliveryFee,
                estimatedMinutes
            FROM delivery_zones
            WHERE zoneId = ?
              AND active = 1
        """, (zone_id,))

        delivery_zone = cursor.fetchone()

        if not delivery_zone:

            return jsonify({
                "success": False,
                "error": "The selected delivery zone is not available."
            }), 400

        delivery_fee = float(
            delivery_zone["deliveryFee"]
        )

        # -----------------------------------------------------
        # Calculate prices and active promotions
        # -----------------------------------------------------

        order_items = []
        products_total = 0.0
        transaction_promotions = []

        today = datetime.now().strftime("%Y-%m-%d")

        for item in cart:

            article_id = int(item["articleId"])
            quantity = int(item["quantity"])

            article = article_map.get(article_id)

            if not article:

                return jsonify({
                    "success": False,
                    "error": "One of the selected products is no longer available."
                }), 400

            # -------------------------------------------------
            # Normal price, including existing bulk pricing
            # -------------------------------------------------

            normal_unit_price = float(
                determinate_price(
                    article["articleFinalPrice"],
                    article["articleBulkFinalPrice"],
                    quantity,
                    article["articleBulkLimit"]
                )
            )

            normal_subtotal = (
                normal_unit_price * quantity
            )

            # -------------------------------------------------
            # Find active promotions for this article
            # -------------------------------------------------

            cursor.execute("""
                SELECT
                    p.promotionId,
                    p.promotionType,
                    p.discountValue
                FROM promotions p
                JOIN promotion_articles pa
                    ON pa.promotionId = p.promotionId
                WHERE pa.articleId = ?
                  AND p.promotionActive = 1
                  AND date(p.startDate) <= date('now')
                  AND date(p.endDate) >= date('now')
                  AND date(?) >= date(p.startDate)
                  AND date(?) <= date(p.endDate)
                ORDER BY p.promotionId ASC
            """, (
                article_id,
                today,
                today
            ))

            promotions_for_article = cursor.fetchall()

            best_promotion = None
            best_discount = 0.0

            for promotion in promotions_for_article:

                if promotion["promotionType"] == "percentage":

                    discount = (
                        normal_subtotal
                        * float(promotion["discountValue"])
                        / 100.0
                    )

                elif promotion["promotionType"] == "fixed":

                    discount = (
                        float(promotion["discountValue"])
                        * quantity
                    )

                else:

                    continue

                # Never discount below zero.
                discount = min(
                    discount,
                    normal_subtotal
                )

                # Apply only the best promotion.
                if discount > best_discount:

                    best_discount = discount
                    best_promotion = promotion

            # -------------------------------------------------
            # Apply selected promotion
            # -------------------------------------------------

            discounted_subtotal = (
                normal_subtotal - best_discount
            )

            if quantity > 0:

                discounted_unit_price = (
                    discounted_subtotal / quantity
                )

            else:

                discounted_unit_price = normal_unit_price

            discounted_unit_price = max(
                0.0,
                discounted_unit_price
            )

            discounted_subtotal = max(
                0.0,
                discounted_subtotal
            )

            products_total += discounted_subtotal

            order_items.append({
                "articleId": article_id,
                "quantity": quantity,
                "price": discounted_unit_price,
                "subtotal": discounted_subtotal
            })

            if best_promotion and best_discount > 0:

                transaction_promotions.append({
                    "promotionId": best_promotion["promotionId"],
                    "articleId": article_id,
                    "quantity": quantity,
                    "discountAmount": best_discount
                })

        # -----------------------------------------------------
        # Calculate final order total
        # -----------------------------------------------------

        total = products_total + delivery_fee

        # -----------------------------------------------------
        # CREDIT PAYMENT VALIDATION
        # -----------------------------------------------------

        credit_customer = None
        credit_account = None

        normalized_payment_method = (
            str(payment_method or "")
            .strip()
            .upper()
        )

        if normalized_payment_method == "CREDIT":

            (
                credit_allowed,
                credit_error,
                credit_customer,
                credit_account
            ) = validate_customer_credit(
                user_id,
                total,
                cursor
            )

            if not credit_allowed:

                return jsonify({
                    "success": False,
                    "error": credit_error
                }), 400

            # Store the normalized payment method.
            payment_method = "CREDIT"

        # -----------------------------------------------------
        # Assign the active depot
        # -----------------------------------------------------

        cursor.execute("""
            SELECT
                depotId,
                depotName
            FROM depots
            WHERE depotActive = 1
            ORDER BY depotId ASC
            LIMIT 1
        """)

        active_depot = cursor.fetchone()

        if not active_depot:
            conn.rollback()
            conn.close()

            return jsonify({
                "success": False,
                "error": (
                    "No active depot is available to process "
                    "this order."
                )
            }), 400

        cmd_depot_id = active_depot["depotId"]

        # -----------------------------------------------------
        # Generate order number
        # -----------------------------------------------------

        cursor.execute("""
            SELECT COUNT(*) AS cmdNumber
            FROM Commandes
        """)

        cmd_number = (
            cursor.fetchone()["cmdNumber"] + 1
        )

        year = datetime.now().year

        cmd_num = (
            f"CMD-{year}-{cmd_number:04d}"
        )

        order_date = datetime.now().strftime(
            "%Y-%m-%d"
        )

        # -----------------------------------------------------
        # Create order
        # -----------------------------------------------------

        cursor.execute("""
            INSERT INTO Commandes (
                cmdClientId,
                cmdDate,
                cmdLivAddress,
                cmdNum,
                cmdPayementMethod,
                cmdStatut,
                cmdTotal,
                cmdZoneId,
                cmdDepotId
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            order_date,
            delivery_address,
            cmd_num,
            payment_method,
            "en_attente",
            total,
            zone_id,
            cmd_depot_id
        ))

        commandId = cursor.lastrowid

        # -----------------------------------------------------
        # CREATE CREDIT DEBT
        # -----------------------------------------------------

        credit_due_date = None

        if payment_method == "CREDIT":

            credit_due_date = create_credit_transaction(
                user_id=user_id,
                command_id=commandId,
                amount=total,
                cursor=cursor,
                credit_customer=credit_customer
            )

        # -----------------------------------------------------
        # Create order items
        # -----------------------------------------------------

        for order_item in order_items:

            cursor.execute("""
                INSERT INTO articlescommandes (
                    artcmdCmdId,
                    artcmdArticleId,
                    artcmdQuantity,
                    artcmdPrice,
                    artcmdSubTotal
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                commandId,
                order_item["articleId"],
                order_item["quantity"],
                order_item["price"],
                order_item["subtotal"]
            ))

        # -----------------------------------------------------
        # Record applied promotions
        # -----------------------------------------------------

        for promotion in transaction_promotions:

            cursor.execute("""
                INSERT INTO promotion_transactions (
                    promotionId,
                    commandId,
                    articleId,
                    quantity,
                    discountAmount,
                    createdAt
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                promotion["promotionId"],
                commandId,
                promotion["articleId"],
                promotion["quantity"],
                promotion["discountAmount"],
                datetime.now().isoformat(
                    timespec="seconds"
                )
            ))

        # -----------------------------------------------------
        # Commit everything together
        # -----------------------------------------------------

        conn.commit()

    except Exception as e:

        conn.rollback()

        print(
            "Order creation error:",
            repr(e)
        )

        return jsonify({
            "success": False,
            "error": "Unable to create the order."
        }), 500

    finally:

        conn.close()

    # ---------------------------------------------------------
    # Successful response
    # ---------------------------------------------------------

    return jsonify({
        "success": True,
        "commandId": commandId,
        "commandNumber": cmd_num,
        "productsTotal": products_total,
        "deliveryFee": delivery_fee,
        "total": total,
        "zoneId": zone_id
    })

@app.route("/products")
def products():
    selectedCategory = request.args.get("selectedCategory", default="tout")
    categories = get_categories()

    categoryId = None
    if selectedCategory != "tout":
        categoryId = get_category_id_by_slug(selectedCategory)
        if categoryId is None:
            selectedCategory = "tout"

    newProducts = get_new_products(categoryId)
    onSaleProducts = get_on_sale_products(categoryId)
    topProducts = get_top_products(categoryId)
    suggestedProducts = get_suggested_products(categoryId)

    return render_template(
        "products.html", 
        categories=categories, 
        newProducts= newProducts, 
        onSaleProducts= onSaleProducts,
        topProducts= topProducts,
        suggestedProducts= suggestedProducts,
        selectedCategory= selectedCategory,
        categoryId= categoryId
    )

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/login")
def login():
    return render_template("/auth/login.html")

@app.route("/create_account")
def create_account():
    return render_template("/auth/create_account.html")
@app.route("/register", methods=["POST"])
def register():

    name = request.form.get("name", "").strip()
    surname = request.form.get("surname", "").strip()
    email = request.form.get("email", "").strip().lower()
    # ---------------------------------------------------------
    # Email validation
    # ---------------------------------------------------------

    email_pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"

    if not re.fullmatch(email_pattern, email):
        return render_template(
            "/auth/create_account.html",
            error="Enter a valid email address."
        )
    
    gender = request.form.get("gender", "").strip()
    phone = request.form.get("phone", "").strip()

    # ---------------------------------------------------------
    # Cameroon phone validation
    # ---------------------------------------------------------

    phone = phone.replace(" ", "").replace("-", "")

    if phone.startswith("+237"):
     phone = phone[4:]
    elif phone.startswith("237"):
        phone = phone[3:]

    if not phone.isdigit() or len(phone) != 9 or not phone.startswith("6"):
     return render_template(
        "/auth/create_account.html",
        error="Enter a valid Cameroon phone number with exactly 9 digits."
    )

    address = request.form.get("address", "").strip()
    password = request.form.get("psw", "")
    password_confirmation = request.form.get("pswConfirm", "")

    # ---------------------------------------------------------
    # Validation
    # ---------------------------------------------------------

    if not all([
        name,
        surname,
        email,
        gender,
        phone,
        address,
        password,
        password_confirmation
    ]):
        return render_template(
            "/auth/create_account.html",
            error="All fields are required."
        )

    if password != password_confirmation:
        return render_template(
            "/auth/create_account.html",
            error="Passwords do not match."
        )

    if len(password) < 6:
        return render_template(
            "/auth/create_account.html",
            error="Password must contain at least 6 characters."
        )

    if gender not in ("M", "F"):
        return render_template(
            "/auth/create_account.html",
            error="Invalid gender selection."
        )

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -----------------------------------------------------
        # Check duplicate email
        # -----------------------------------------------------

        cursor.execute("""
            SELECT userId
            FROM users
            WHERE userMail = ?
        """, (email,))

        existing_user = cursor.fetchone()

        if existing_user:
            conn.close()

            return render_template(
                "/auth/create_account.html",
                error="An account with this email already exists."
            )

        # -----------------------------------------------------
        # Verify customer role
        # -----------------------------------------------------

        cursor.execute("""
            SELECT idRole
            FROM roles
            WHERE idRole = 2
              AND nomRole = 'Client'
        """)

        customer_role = cursor.fetchone()

        if not customer_role:
            conn.close()

            return render_template(
                "/auth/create_account.html",
                error="The customer role is not configured correctly."
            )

 # -----------------------------------------------------
# Hash password before storing it
# -----------------------------------------------------

        password_hash = generate_password_hash(password)

        # -----------------------------------------------------
        # Create customer
        # -----------------------------------------------------

        cursor.execute("""
            INSERT INTO users (
                userName,
                userSurname,
                userMail,
                userPsw,
                userPhone,
                userRole,
                userGender,
                userAddress
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            surname,
            email,
            password_hash,
            phone,
            customer_role["idRole"],
            gender,
            address
        ))

        conn.commit()

        user_id = cursor.lastrowid

        conn.close()

        # -----------------------------------------------------
        # Log customer in automatically
        # -----------------------------------------------------

        session["user_id"] = user_id

        return redirect(url_for("account"))

    except sqlite3.IntegrityError as error:

        conn.rollback()
        conn.close()

        return render_template(
            "/auth/create_account.html",
            error=f"Unable to create account: {error}"
        )

    except sqlite3.Error as error:

        conn.rollback()
        conn.close()

        return render_template(
            "/auth/create_account.html",
            error=f"Database error: {error}"
        )



@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("index"))

@app.route("/account")
def account():
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # Get logged-in customer
    cursor.execute("""
     SELECT
        userId,
        userName,
        userSurname,
        userMail,
        userPhone,
        userGender,
        userPicture,
        userBirthDate,
        userAddress,
        invoiceDeliveryMethod
    FROM users
        WHERE userId = ?
    """, (user_id,))

    customer = cursor.fetchone()

    if not customer:
        conn.close()
        session.pop("user_id", None)
        return redirect(url_for("login"))

    # Get customer's orders
    cursor.execute("""
        SELECT
            cmdId,
            cmdNum,
            cmdDate,
            cmdStatut,
            cmdPayementMethod,
            cmdTotal,
            cmdLivAddress
        FROM Commandes
        WHERE cmdClientId = ?
        ORDER BY cmdId DESC
    """, (user_id,))

    orders = [dict(row) for row in cursor.fetchall()]

    # Customer statistics
    cursor.execute("""
        SELECT
            COUNT(*) AS total_orders,
            COALESCE(SUM(cmdTotal), 0) AS total_spent
        FROM Commandes
        WHERE cmdClientId = ?
    """, (user_id,))

    statistics = dict(cursor.fetchone())

    # Loyalty account
    cursor.execute("""
        SELECT
            points_balance
        FROM loyalty_accounts
        WHERE user_id = ?
        LIMIT 1
    """, (user_id,))

    loyalty_account = cursor.fetchone()

    loyalty_balance = (
        loyalty_account["points_balance"]
        if loyalty_account
        else 0
    )

    # Loyalty transaction history
    cursor.execute("""
        SELECT
            lt.id,
            lt.command_id,
            lt.type,
            lt.points,
            lt.description,
            lt.created_at,
            c.cmdNum
        FROM loyalty_transactions lt
        LEFT JOIN Commandes c
            ON c.cmdId = lt.command_id
        WHERE lt.user_id = ?
        ORDER BY lt.id DESC
    """, (user_id,))

    loyalty_transactions = [
        dict(row)
        for row in cursor.fetchall()
    ]


    # Loyalty statistics
    cursor.execute("""
        SELECT
            COALESCE(SUM(points), 0) AS total_points_earned,
            COUNT(*) AS rewarded_orders_count,
            MAX(created_at) AS last_reward_date
        FROM loyalty_transactions
        WHERE user_id = ?
          AND type = 'PURCHASE'
    """, (user_id,))

    loyalty_statistics = dict(cursor.fetchone())


    conn.close()

    return render_template(
        "account.html",
        customer=dict(customer),
        orders=orders,
        statistics=statistics,
        loyalty_balance=loyalty_balance,
        loyalty_transactions=loyalty_transactions,
        loyalty_statistics=loyalty_statistics
    )


@app.route("/account/invoice-preferences", methods=["POST"])
def account_invoice_preferences():
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    delivery_method = (
        request.form.get("delivery_method", "")
        .strip()
        .lower()
    )

    if delivery_method not in {"email", "whatsapp"}:
        flash(
            "Invalid invoice delivery method.",
            "error"
        )
        return redirect(url_for("account"))

    conn = get_connexion()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                userId,
                userMail,
                userPhone
            FROM users
            WHERE userId = ?
            LIMIT 1
        """, (user_id,))

        customer = cursor.fetchone()

        if not customer:
            conn.rollback()
            flash(
                "Customer account not found.",
                "error"
            )
            return redirect(url_for("account"))

        # ------------------------------------------------------------
        # Validate the contact method selected by the customer
        # ------------------------------------------------------------
        if delivery_method == "email":

            email_address = (
                str(customer["userMail"] or "")
                .strip()
            )

            email_pattern = (
                r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
            )

            if not re.match(email_pattern, email_address):
                conn.rollback()

                flash(
                    "A valid email address is required "
                    "for invoice delivery by email.",
                    "error"
                )

                return redirect(url_for("account"))

        else:

            phone = (
                str(customer["userPhone"] or "")
                .strip()
            )

            if not phone:
                conn.rollback()

                flash(
                    "A phone number is required "
                    "for WhatsApp invoice delivery.",
                    "error"
                )

                return redirect(url_for("account"))

            normalized_phone = normalize_whatsapp_number(
                phone
            )

            if not normalized_phone:
                conn.rollback()

                flash(
                    "The phone number is not valid "
                    "for WhatsApp invoice delivery.",
                    "error"
                )

                return redirect(url_for("account"))

        # ------------------------------------------------------------
        # Save the customer's preference.
        #
        # This preference applies only to invoices generated in the
        # future. Existing invoices are historical records and must
        # never be modified when the customer changes preferences.
        # ------------------------------------------------------------
        cursor.execute("""
            UPDATE users
            SET invoiceDeliveryMethod = ?
            WHERE userId = ?
        """, (
            delivery_method,
            user_id
        ))

        if cursor.rowcount != 1:
            conn.rollback()

            flash(
                "Unable to update invoice delivery preference.",
                "error"
            )

            return redirect(url_for("account"))

        conn.commit()

        if delivery_method == "email":

            flash(
                "Invoice delivery preference saved: Email. "
                "This will apply to future invoices.",
                "success"
            )

        else:

            flash(
                "Invoice delivery preference saved: WhatsApp. "
                "This will apply to future invoices.",
                "success"
            )

        return redirect(url_for("account"))

    except Exception:
        conn.rollback()

        flash(
            "Unable to save invoice delivery preference.",
            "error"
        )

        return redirect(url_for("account"))

    finally:
        conn.close()


@app.route("/account/edit", methods=["GET", "POST"])
def account_edit():
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # Get current customer information
    cursor.execute("""
        SELECT
            userId,
            userName,
            userSurname,
            userMail,
            userPhone,
            userGender,
            userPicture,
            userBirthDate,
            userAddress
        FROM users
        WHERE userId = ?
    """, (user_id,))

    customer = cursor.fetchone()

    if not customer:
        conn.close()
        session.pop("user_id", None)
        return redirect(url_for("login"))

    if request.method == "POST":

        user_name = request.form.get("userName", "").strip()
        user_surname = request.form.get("userSurname", "").strip()
        user_mail = request.form.get("userMail", "").strip().lower()
        user_phone = request.form.get("userPhone", "").strip()
        user_gender = request.form.get("userGender", "").strip()
        user_birth_date = request.form.get("userBirthDate", "").strip()
        user_address = request.form.get("userAddress", "").strip()

        # ---------------------------------------------------------
        # Email validation
        # ---------------------------------------------------------

        email_pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"

        if not re.fullmatch(email_pattern, user_mail):
            conn.close()

            updated_customer = dict(customer)
            updated_customer.update({
                "userName": user_name,
                "userSurname": user_surname,
                "userMail": user_mail,
                "userPhone": user_phone,
                "userGender": user_gender,
                "userBirthDate": user_birth_date,
                "userAddress": user_address
            })

            return render_template(
                "account_edit.html",
                customer=updated_customer,
                error="Enter a valid email address."
            )

        # ---------------------------------------------------------
        # Cameroon phone validation
        # ---------------------------------------------------------

        user_phone = (
            user_phone
            .replace(" ", "")
            .replace("-", "")
        )

        if user_phone.startswith("+237"):
            user_phone = user_phone[4:]
        elif user_phone.startswith("237"):
            user_phone = user_phone[3:]

        if (
            not user_phone.isdigit()
            or len(user_phone) != 9
            or not user_phone.startswith("6")
        ):
            conn.close()

            updated_customer = dict(customer)
            updated_customer.update({
                "userName": user_name,
                "userSurname": user_surname,
                "userMail": user_mail,
                "userPhone": user_phone,
                "userGender": user_gender,
                "userBirthDate": user_birth_date,
                "userAddress": user_address
            })

            return render_template(
                "account_edit.html",
                customer=updated_customer,
                error="Enter a valid Cameroon phone number with exactly 9 digits."
            )

        # Required fields
        if not user_name or not user_mail or not user_phone:
            conn.close()

            updated_customer = dict(customer)
            updated_customer.update({
                "userName": user_name,
                "userSurname": user_surname,
                "userMail": user_mail,
                "userPhone": user_phone,
                "userGender": user_gender,
                "userBirthDate": user_birth_date,
                "userAddress": user_address
            })

            return render_template(
                "account_edit.html",
                customer=updated_customer,
                error="First name, email and phone are required."
            )

        # Check duplicate email
        cursor.execute("""
            SELECT userId
            FROM users
           WHERE LOWER(userMail) = LOWER(?)
            AND userId != ?
        """, (user_mail, user_id))

        existing_user = cursor.fetchone()

        if existing_user:
            conn.close()

            updated_customer = dict(customer)
            updated_customer.update({
                "userName": user_name,
                "userSurname": user_surname,
                "userMail": user_mail,
                "userPhone": user_phone,
                "userGender": user_gender,
                "userBirthDate": user_birth_date,
                "userAddress": user_address
            })

            return render_template(
                "account_edit.html",
                customer=updated_customer,
                error="This email address is already in use."
            )

        # Update customer profile
        cursor.execute("""
            UPDATE users
            SET
                userName = ?,
                userSurname = ?,
                userMail = ?,
                userPhone = ?,
                userGender = ?,
                userBirthDate = ?,
                userAddress = ?
            WHERE userId = ?
        """, (
            user_name,
            user_surname,
            user_mail,
            user_phone,
            user_gender,
            user_birth_date,
            user_address,
            user_id
        ))

        conn.commit()
        conn.close()

        return redirect(url_for("account"))

    conn.close()

    return render_template(
        "account_edit.html",
        customer=dict(customer)
    )

@app.route("/account/change-password", methods=["GET", "POST"])
def account_change_password():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # Get current user
    cursor.execute("""
        SELECT
            userId,
            userPsw
        FROM users
        WHERE userId = ?
    """, (user_id,))

    customer = cursor.fetchone()

    if not customer:
        conn.close()
        session.pop("user_id", None)
        return redirect(url_for("login"))

    if request.method == "POST":

        current_password = request.form.get(
            "current_password",
            ""
        )

        new_password = request.form.get(
            "new_password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        stored_password = customer["userPsw"]

        # -----------------------------------------------------
        # Validate current password
        # -----------------------------------------------------

        current_password_valid = False

        try:
            current_password_valid = check_password_hash(
                stored_password,
                current_password
            )
        except (ValueError, TypeError):
            current_password_valid = False

        # -----------------------------------------------------
        # Backward compatibility for legacy plaintext passwords
        # -----------------------------------------------------

        if (
            not current_password_valid
            and stored_password == current_password
        ):
            current_password_valid = True

        if not current_password_valid:

            conn.close()

            return render_template(
                "account_change_password.html",
                error="Current password is incorrect."
            )

        # -----------------------------------------------------
        # Validate new password
        # -----------------------------------------------------

        if len(new_password) < 6:

            conn.close()

            return render_template(
                "account_change_password.html",
                error="New password must contain at least 6 characters."
            )

        # -----------------------------------------------------
        # Confirm new password
        # -----------------------------------------------------

        if new_password != confirm_password:

            conn.close()

            return render_template(
                "account_change_password.html",
                error="New passwords do not match."
            )

        # -----------------------------------------------------
        # Prevent using the same password
        # -----------------------------------------------------

        try:
            same_password = check_password_hash(
                stored_password,
                new_password
            )
        except (ValueError, TypeError):
            same_password = (
                stored_password == new_password
            )

        if same_password:

            conn.close()

            return render_template(
                "account_change_password.html",
                error="The new password must be different from the current password."
            )

        # -----------------------------------------------------
        # Hash the new password
        # -----------------------------------------------------

        new_password_hash = generate_password_hash(
            new_password
        )

        cursor.execute("""
            UPDATE users
            SET userPsw = ?
            WHERE userId = ?
        """, (
            new_password_hash,
            user_id
        ))

        conn.commit()
        conn.close()

        # -----------------------------------------------------
        # Force re-login after password change
        # -----------------------------------------------------

        session.pop("user_id", None)

        return redirect(url_for("login"))

    conn.close()

    return render_template(
        "account_change_password.html"
    )

@app.route("/account/orders/<int:order_id>")
def account_order_details(order_id):
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # Make sure this order belongs to the logged-in customer
    cursor.execute("""
        SELECT
            cmdId,
            cmdNum,
            cmdDate,
            cmdStatut,
            cmdPayementMethod,
            cmdTotal,
            cmdLivAddress
        FROM Commandes
        WHERE cmdId = ?
          AND cmdClientId = ?
    """, (order_id, user_id))

    order = cursor.fetchone()

    if not order:
        conn.close()
        return redirect(url_for("account"))

    cursor.execute("""
        SELECT
            ac.artcmdQuantity,
            ac.artcmdPrice,
            ac.artcmdSubTotal,
            a.articleId,
            a.articleVolume,
            a.articleImage,
            p.productName,
            pt.promotionId,
            pr.promotionName,
            pr.promotionType,
            pr.discountValue,
            pt.discountAmount
        FROM articlesCommandes ac
        JOIN Articles a
            ON a.articleId = ac.artcmdArticleId
        JOIN products p
            ON p.productId = a.articleProductId
        LEFT JOIN promotion_transactions pt
            ON pt.commandId = ac.artcmdCmdId
            AND pt.articleId = ac.artcmdArticleId
        LEFT JOIN promotions pr
            ON pr.promotionId = pt.promotionId
        WHERE ac.artcmdCmdId = ?
        ORDER BY ac.artcmdId ASC
    """, (order_id,))

    items = [dict(row) for row in cursor.fetchall()]

    # Loyalty points earned for this order
    cursor.execute("""
        SELECT
            id,
            points,
            description,
            created_at
        FROM loyalty_transactions
        WHERE user_id = ?
          AND command_id = ?
          AND type = 'PURCHASE'
        LIMIT 1
    """, (user_id, order_id))

    loyalty_transaction = cursor.fetchone()

    # Invoice belonging to this order AND this customer
    cursor.execute("""
        SELECT
            invoiceId,
            invoiceNumber,
            invoiceDate,
            subtotal,
            deliveryFee,
            total,
            deliveryMethod,
            emailAddress,
            whatsappNumber,
            pdfPath,
            status,
            sendStatus,
            sentAt
        FROM invoices
        WHERE commandId = ?
          AND clientId = ?
        LIMIT 1
    """, (order_id, user_id))

    invoice = cursor.fetchone()

    conn.close()

    return render_template(
        "order_details.html",
        order=dict(order),
        items=items,
        loyalty_transaction=(
            dict(loyalty_transaction)
            if loyalty_transaction
            else None
        ),
        invoice=(
            dict(invoice)
            if invoice
            else None
        )
    )


# ---------------------------------------------------------
# CLIENT - INVOICE PDF ACCESS
# ---------------------------------------------------------

@app.route("/account/invoices/<int:invoice_id>/pdf")
def account_invoice_pdf(invoice_id):
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # Security: invoice must belong to the logged-in customer
    cursor.execute("""
        SELECT
            invoiceNumber,
            pdfPath
        FROM invoices
        WHERE invoiceId = ?
          AND clientId = ?
        LIMIT 1
    """, (invoice_id, user_id))

    invoice = cursor.fetchone()
    conn.close()

    if not invoice:
        return redirect(url_for("account"))

    pdf_path = resolve_invoice_pdf_path(invoice["pdfPath"])

    if not pdf_path or not os.path.isfile(pdf_path):
        return "Invoice PDF not available.", 404

    return send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f'{invoice["invoiceNumber"]}.pdf'
    )


@app.route("/account/invoices/<int:invoice_id>/download")
def account_invoice_download(invoice_id):
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # Security: invoice must belong to the logged-in customer
    cursor.execute("""
        SELECT
            invoiceNumber,
            pdfPath
        FROM invoices
        WHERE invoiceId = ?
          AND clientId = ?
        LIMIT 1
    """, (invoice_id, user_id))

    invoice = cursor.fetchone()
    conn.close()

    if not invoice:
        return redirect(url_for("account"))

    pdf_path = resolve_invoice_pdf_path(invoice["pdfPath"])

    if not pdf_path or not os.path.isfile(pdf_path):
        return "Invoice PDF not available.", 404

    return send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f'{invoice["invoiceNumber"]}.pdf'
    )


# ---------------------------------------------------------
# ADMIN - ORDER MANAGEMENT
# ---------------------------------------------------------

@app.route("/admin/orders")
def admin_orders():

    # ---------------------------------------------------------
    # Authentication
    # ---------------------------------------------------------

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # ---------------------------------------------------------
    # Verify administrator
    # ---------------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userName,
            userSurname,
            userMail,
            userPhone,
            userRole
        FROM users
        WHERE userId = ?
    """, (user_id,))

    current_user = cursor.fetchone()

    if not current_user or current_user["userRole"] != 1:

        conn.close()

        return jsonify({
            "success": False,
            "error": "Unauthorized."
        }), 403

    # ---------------------------------------------------------
    # Get orders
    #
    # Includes:
    # - customer
    # - delivery zone
    # - assigned delivery person
    # ---------------------------------------------------------

    cursor.execute("""
        SELECT
            c.cmdId,
            c.cmdClientId,
            c.cmdNum,
            c.cmdStatut,
            c.cmdPayementMethod,
            c.cmdTotal,
            c.cmdLivAddress,
            c.cmdDate,
            c.cmdZoneId,
            c.cmdLivreurId,
            c.cmdLivreurConfirmed,
            c.cmdLivreurConfirmedAt,

            u.userName,
            u.userSurname,
            u.userMail,
            u.userPhone,

            dz.zoneName,
            dz.deliveryFee,
            dz.estimatedMinutes,

            d.userName AS livreurName,
            d.userSurname AS livreurSurname,
            d.userPhone AS livreurPhone

        FROM Commandes c

        JOIN users u
            ON u.userId = c.cmdClientId

        LEFT JOIN delivery_zones dz
            ON dz.zoneId = c.cmdZoneId

        LEFT JOIN users d
            ON d.userId = c.cmdLivreurId
           AND d.userRole = 3

        ORDER BY c.cmdId DESC
    """)

    orders = [
        dict(row)
        for row in cursor.fetchall()
    ]

    # ---------------------------------------------------------
    # Delivery timing
    # ---------------------------------------------------------

    for order in orders:

        timing = get_delivery_timing(
            order["cmdId"],
            cursor
        )

        order["preparation_seconds"] = timing["preparation_seconds"]
        order["delivery_seconds"] = timing["delivery_seconds"]
        order["total_seconds"] = timing["total_seconds"]

        order["preparation_duration"] = format_delivery_duration(
            timing["preparation_seconds"]
        )

        order["delivery_duration"] = format_delivery_duration(
            timing["delivery_seconds"]
        )

        order["total_duration"] = format_delivery_duration(
            timing["total_seconds"]
        )

    # ---------------------------------------------------------
    # Delivery statistics by zone
    # ---------------------------------------------------------

    delivery_zone_statistics = get_delivery_zone_statistics(
        cursor
    )

    # ---------------------------------------------------------
    # Get active delivery personnel
    # ---------------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userName,
            userSurname,
            userPhone
        FROM users
        WHERE userRole = 3
          AND userActive = 1
        ORDER BY userName, userSurname
    """)

    delivery_persons = [
        dict(row)
        for row in cursor.fetchall()
    ]

    conn.close()

    # ---------------------------------------------------------
    # Render admin orders page
    # ---------------------------------------------------------

    return render_template(
        "admin_orders.html",
        orders=orders,
        delivery_persons=delivery_persons,
        delivery_zone_statistics=delivery_zone_statistics,
        current_user=dict(current_user)
    )


# =========================================================
# ADMIN - DELIVERY HISTORY
# =========================================================

@app.route("/admin/delivery-history")
def admin_delivery_history():

    # -----------------------------------------------------
    # Authentication
    # -----------------------------------------------------

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # -----------------------------------------------------
    # Verify Administrator
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userName,
            userSurname,
            userMail,
            userPhone,
            userRole
        FROM users
        WHERE userId = ?
    """, (user_id,))

    current_user = cursor.fetchone()

    if not current_user or current_user["userRole"] != 1:

        conn.close()

        return redirect(url_for("dashboard"))

       # -----------------------------------------------------
    # Delivery history filters
    # -----------------------------------------------------

    order_search = request.args.get(
        "order",
        ""
    ).strip()

    status_filter = request.args.get(
        "status",
        ""
    ).strip()

    changed_by_filter = request.args.get(
        "changed_by",
        ""
    ).strip()


    # -----------------------------------------------------
    # Build delivery history query
    # -----------------------------------------------------

    history_query = """
        SELECT
            dh.id,
            dh.cmdId,
            dh.old_status,
            dh.new_status,
            dh.changed_by,
            dh.changed_at,

            c.cmdNum,
            c.cmdStatut,
            c.cmdTotal,

            customer.userName AS customerName,
            customer.userSurname AS customerSurname,

            changer.userName AS changerName,
            changer.userSurname AS changerSurname,
            changer.userRole AS changerRole

        FROM delivery_history dh

        JOIN Commandes c
            ON c.cmdId = dh.cmdId

        LEFT JOIN users customer
            ON customer.userId = c.cmdClientId

        LEFT JOIN users changer
            ON changer.userId = dh.changed_by

        WHERE 1 = 1
    """

    history_params = []


    # -----------------------------------------------------
    # Order filter
    # -----------------------------------------------------

    if order_search:

        history_query += """
            AND (
                c.cmdNum LIKE ?
                OR CAST(c.cmdId AS TEXT) LIKE ?
            )
        """

        search_value = f"%{order_search}%"

        history_params.extend([
            search_value,
            search_value
        ])


    # -----------------------------------------------------
    # Status filter
    # -----------------------------------------------------

    if status_filter:

        history_query += """
            AND dh.new_status = ?
        """

        history_params.append(
            status_filter
        )


    # -----------------------------------------------------
    # Changed-by filter
    # -----------------------------------------------------

    if changed_by_filter:

        try:

            changed_by_id = int(
                changed_by_filter
            )

        except ValueError:

            changed_by_id = None


        if changed_by_id is not None:

            history_query += """
                AND dh.changed_by = ?
            """

            history_params.append(
                changed_by_id
            )


    # -----------------------------------------------------
    # Order results
    # -----------------------------------------------------

    history_query += """
        ORDER BY
            dh.changed_at DESC,
            dh.id DESC
    """


    cursor.execute(
        history_query,
        history_params
    )


    delivery_history = [
        dict(row)
        for row in cursor.fetchall()
    ]


    # -----------------------------------------------------
    # Get users who have changed delivery status
    # -----------------------------------------------------

    cursor.execute("""
        SELECT DISTINCT
            u.userId,
            u.userName,
            u.userSurname

        FROM users u

        INNER JOIN delivery_history dh
            ON dh.changed_by = u.userId

        ORDER BY
            u.userName,
            u.userSurname
    """)

    history_users = [
        dict(row)
        for row in cursor.fetchall()
    ]
    conn.close()

    # -----------------------------------------------------
    # Render history page
    # -----------------------------------------------------

    return render_template(
    "admin_delivery_history.html",
    delivery_history=delivery_history,
    history_users=history_users,
    order_search=order_search,
    status_filter=status_filter,
    changed_by_filter=changed_by_filter,
    current_user=dict(current_user)
)
# =========================================================
# ADMIN - ASSIGN LIVREUR TO ORDER
# =========================================================

@app.route(
    "/admin/orders/<int:order_id>/assign-livreur",
    methods=["POST"]
)
def assign_order_livreur(order_id):

    # -----------------------------------------------------
    # Authentication
    # -----------------------------------------------------

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                userId,
                userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:

            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Get selected Livreur
        #
        # Empty value means remove assignment.
        # -------------------------------------------------

        livreur_id_raw = request.form.get(
            "livreur_id",
            ""
        ).strip()

        if livreur_id_raw == "":

            livreur_id = None

        else:

            try:
                livreur_id = int(livreur_id_raw)

            except (TypeError, ValueError):

                return jsonify({
                    "success": False,
                    "error": "Invalid delivery person."
                }), 400

        # -------------------------------------------------
        # Verify order
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                cmdId,
                cmdNum,
                cmdStatut,
                cmdLivreurId,
                cmdDepotId
            FROM Commandes
            WHERE cmdId = ?
        """, (order_id,))

        order = cursor.fetchone()

        if not order:

            return jsonify({
                "success": False,
                "error": "Order not found."
            }), 404

        # -------------------------------------------------
        # If removing assignment
        # -------------------------------------------------

        if livreur_id is None:

            cursor.execute("""
                UPDATE Commandes
                SET
                    cmdLivreurId = NULL,
                    cmdLivreurConfirmed = 0,
                    cmdLivreurConfirmedAt = NULL
                WHERE cmdId = ?
            """, (
                order_id,
            ))

            conn.commit()

            return redirect(
                url_for("admin_orders")
            )

        # -------------------------------------------------
        # Verify selected user is an ACTIVE Livreur
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                userId,
                userName,
                userSurname,
                userPhone,
                userActive
            FROM users
            WHERE userId = ?
              AND userRole = 3
        """, (livreur_id,))

        livreur = cursor.fetchone()

        if not livreur:

            return jsonify({
                "success": False,
                "error": "The selected delivery person does not exist."
            }), 404

        # -------------------------------------------------
        # Check active status
        # -------------------------------------------------

        if not livreur["userActive"]:

            return jsonify({
                "success": False,
                "error": "The selected delivery person is inactive."
            }), 400

        # -------------------------------------------------
        # STOCK VALIDATION BEFORE LIVREUR ASSIGNMENT
        # -------------------------------------------------
        #
        # A delivery person must not be assigned to an order
        # that cannot currently be fulfilled from inventory.
        #
        # We validate both:
        #   1. Global article stock
        #   2. Stock in the order's assigned depot
        #
        # The final stock validation during delivery completion
        # remains in place as a safety check because stock can
        # change after assignment.
        # -------------------------------------------------

        depot_id = order["cmdDepotId"]

        if not depot_id:
            flash(
                f"Commande {order['cmdNum']} : aucun d?p?t n'est "
                "assign? ? cette commande. Le Livreur ne peut pas "
                "?tre assign? avant l'affectation d'un d?p?t.",
                "error"
            )
            return redirect(url_for("admin_orders"))

        cursor.execute("""
            SELECT
                ac.artcmdArticleId AS articleId,
                SUM(ac.artcmdQuantity) AS requiredQuantity,
                p.productName AS articleName
            FROM articlesCommandes ac
            LEFT JOIN Articles a
                ON a.articleId = ac.artcmdArticleId
            LEFT JOIN products p
                ON p.productId = a.articleProductId
            WHERE ac.artcmdCmdId = ?
            GROUP BY
                ac.artcmdArticleId,
                p.productName
        """, (order_id,))

        order_items = cursor.fetchall()

        if not order_items:
            flash(
                f"Commande {order['cmdNum']} : aucun article "
                "n'est associ? ? cette commande. Le Livreur "
                "n'a pas ?t? assign?.",
                "error"
            )
            return redirect(url_for("admin_orders"))

        stock_errors = []

        for item in order_items:
            article_id = int(item["articleId"])
            required_quantity = int(
                item["requiredQuantity"] or 0
            )
            article_name = (
                item["articleName"]
                or f"Article {article_id}"
            )

            if required_quantity <= 0:
                continue

            # Global stock
            cursor.execute("""
                SELECT articleStock
                FROM Articles
                WHERE articleId = ?
            """, (article_id,))

            article_row = cursor.fetchone()

            if not article_row:
                stock_errors.append(
                    f"{article_name} (ID {article_id}) : article introuvable."
                )
                continue

            global_stock = int(
                article_row["articleStock"] or 0
            )

            if global_stock < required_quantity:
                stock_errors.append(
                    f"{article_name} : stock global disponible "
                    f"{global_stock}, requis {required_quantity}."
                )
                continue

            # Depot stock
            cursor.execute("""
                SELECT quantity
                FROM depot_stocks
                WHERE depotId = ?
                  AND articleId = ?
            """, (
                depot_id,
                article_id
            ))

            depot_row = cursor.fetchone()

            if not depot_row:
                stock_errors.append(
                    f"{article_name} : aucun stock disponible "
                    f"dans le d?p?t {depot_id}."
                )
                continue

            depot_stock = int(
                depot_row["quantity"] or 0
            )

            if depot_stock < required_quantity:
                stock_errors.append(
                    f"{article_name} : stock du d?p?t disponible "
                    f"{depot_stock}, requis {required_quantity}."
                )

        if stock_errors:
            flash(
                f"Stock insuffisant pour la commande {order['cmdNum']} : "
                + " | ".join(stock_errors)
                + " Le Livreur n'a pas ?t? assign?.",
                "error"
            )

            return redirect(
                url_for("admin_orders")
            )

        # -------------------------------------------------
        # Assign Livreur
        # -------------------------------------------------

        cursor.execute("""
            UPDATE Commandes
            SET
                cmdLivreurId = ?,
                cmdLivreurConfirmed = 0,
                cmdLivreurConfirmedAt = NULL
            WHERE cmdId = ?
        """, (
            livreur_id,
            order_id
        ))

        conn.commit()

        return redirect(
            url_for("admin_orders")
        )

    except Exception as e:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to assign the delivery person."
        }), 500

    finally:

        conn.close()
# ---------------------------------------------------------
# ADMIN - UPDATE ORDER STATUS
# ---------------------------------------------------------

@app.route(
    "/admin/orders/<int:order_id>/status",
    methods=["POST"]
)
def update_order_status(order_id):

    # -----------------------------------------------------
    # Authentication
    # -----------------------------------------------------

    user_id = session.get("user_id")

    print("DEBUG STATUS UPDATE - session:", dict(session))
    print("DEBUG STATUS UPDATE - user_id:", user_id)

    if not user_id:
        return redirect(url_for("login"))
    conn = get_connexion()
    cursor = conn.cursor()

    # -----------------------------------------------------
    # Verify administrator
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userRole
        FROM users
        WHERE userId = ?
    """, (user_id,))

    current_user = cursor.fetchone()

    if not current_user or current_user["userRole"] != 1:
        conn.close()

        return jsonify({
            "success": False,
            "error": "Unauthorized."
        }), 403

    # -----------------------------------------------------
    # Get requested status
    # -----------------------------------------------------

    new_status = request.form.get(
        "status",
        ""
    ).strip().lower()

    allowed_statuses = {
        "en_attente",
        "en_preparation",
        "en_livraison",
        "livree",
        "annulee"
    }

    if new_status not in allowed_statuses:
        conn.close()

        return jsonify({
            "success": False,
            "error": "Invalid order status."
        }), 400

    # -----------------------------------------------------
    # Get order
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            cmdId,
            cmdClientId,
            cmdNum,
            cmdStatut,
            cmdTotal,
            cmdLivreurConfirmed
        FROM Commandes
        WHERE cmdId = ?
    """, (order_id,))

    order = cursor.fetchone()

    if not order:
        conn.close()

        return jsonify({
            "success": False,
            "error": "Order not found."
        }), 404

    old_status = order["cmdStatut"]

    # -----------------------------------------------------
    # Delivery stages are controlled by the Livreur
    #
    # The administrator may prepare or cancel an order,
    # but must not bypass the Livreur confirmation workflow.
    # -----------------------------------------------------

    if new_status in {"en_livraison", "livree"}:

        conn.close()

        return jsonify({
            "success": False,
            "error": (
                "The administrator cannot move an order "
                "directly into delivery or mark it as delivered. "
                "The assigned Livreur must confirm the delivery "
                "first and then mark it as delivered."
            )
        }), 403

    # -----------------------------------------------------
    # Nothing to change
    # -----------------------------------------------------

    if old_status == new_status:
        conn.close()

        return jsonify({
            "success": True,
            "message": "Order status is already set."
        })

    # -----------------------------------------------------
    # Prevent changing a delivered order
    # -----------------------------------------------------

    if old_status == "livree":

        conn.close()

        return jsonify({
            "success": False,
            "error": "A delivered order cannot be changed."
        }), 400

    # -----------------------------------------------------
    # Validate status transition
    # -----------------------------------------------------

    allowed_transitions = {
        "en_attente": {
            "en_attente",
            "en_preparation",
            "annulee"
        },

        "en_preparation": {
            "en_preparation",
            "en_livraison",
            "annulee"
        },

        "en_livraison": {
            "en_livraison",
            "livree",
            "annulee"
        },

        "livree": {
            "livree"
        },

        "annulee": {
            "annulee"
        }
    }

    if new_status not in allowed_transitions.get(
        old_status,
        set()
    ):

        conn.close()

        return jsonify({
            "success": False,
            "error": (
                f"Invalid status transition: "
                f"{old_status} -> {new_status}."
            )
        }), 400

       # -----------------------------------------------------
    # Require an active Livreur for delivery stages
    # -----------------------------------------------------

    if new_status in {
        "en_livraison",
        "livree"
    }:

        cursor.execute("""
            SELECT
                d.userId,
                d.userName,
                d.userSurname,
                d.userActive
            FROM Commandes c
            LEFT JOIN users d
                ON d.userId = c.cmdLivreurId
               AND d.userRole = 3
            WHERE c.cmdId = ?
        """, (order_id,))

        assigned_livreur = cursor.fetchone()

        # -------------------------------------------------
        # No Livreur assigned
        # -------------------------------------------------

        if (
            not assigned_livreur
            or assigned_livreur["userId"] is None
        ):

            conn.close()

            return jsonify({
                "success": False,
                "error": (
                    "An active Livreur must be assigned "
                    "before the order can enter delivery."
                )
            }), 400

        # -------------------------------------------------
        # Livreur is inactive
        # -------------------------------------------------

        if not assigned_livreur["userActive"]:

            conn.close()

            return jsonify({
                "success": False,
                "error": (
                    "The assigned Livreur is inactive. "
                    "Please assign an active Livreur."
                )
            }), 400

    # -----------------------------------------------------
    # Deduct inventory when order becomes delivered
    # -----------------------------------------------------

    if new_status == "livree":

        cursor.execute("""
            SELECT cmdDepotId
            FROM Commandes
            WHERE cmdId = ?
        """, (order_id,))

        order_depot_row = cursor.fetchone()

        if (
            not order_depot_row
            or not order_depot_row["cmdDepotId"]
        ):
            conn.rollback()
            conn.close()

            return jsonify({
                "success": False,
                "error": (
                    "The order is not assigned to a depot."
                )
            }), 400

        process_delivery_stock_out(
            command_id=order_id,
            depot_id=order_depot_row["cmdDepotId"],
            user_id=user_id,
            cursor=cursor
        )

    # -----------------------------------------------------
    # Update status
    # -----------------------------------------------------

    cursor.execute("""
        UPDATE Commandes
        SET cmdStatut = ?
        WHERE cmdId = ?
    """, (
        new_status,
        order_id
    ))

    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()

        return jsonify({
            "success": False,
            "error": "Unable to update the order."
        }), 409

    # -----------------------------------------------------
    # Delivery history
    # -----------------------------------------------------

    record_delivery_history(
        commandId=order["cmdId"],
        old_status=old_status,
        new_status=new_status,
        changed_by=user_id,
        cursor=cursor
    )
    # -----------------------------------------------------
    # Loyalty reward
    # -----------------------------------------------------

    loyalty_points = 0

    if new_status == "livree":

        loyalty_points = award_loyalty_points(
            user_id=order["cmdClientId"],
            commandId=order["cmdId"],
            total_amount=order["cmdTotal"],
            cursor=cursor
        )

    # -----------------------------------------------------
    # Invoice database record
    #
    # IMPORTANT:
    # Use the SAME SQLite connection/transaction as the
    # delivery workflow. The invoice must commit atomically
    # with stock, order status, history and loyalty.
    # -----------------------------------------------------

    invoice_result = None

    if new_status == "livree":
        try:
            invoice_result = create_invoice_for_order(
                order["cmdId"],
                conn=conn
            )
        except Exception as invoice_error:
            conn.rollback()
            conn.close()

            print(
                "INVOICE CREATION ERROR:",
                repr(invoice_error)
            )

            return jsonify({
                "success": False,
                "error": (
                    "Unable to create the invoice. "
                    "The delivery transaction was rolled back."
                )
            }), 500

    # -----------------------------------------------------
    # Commit complete delivery transaction
    # -----------------------------------------------------

    conn.commit()
    conn.close()

    # -----------------------------------------------------
    # Generate invoice PDF AFTER database commit
    # -----------------------------------------------------

    invoice_pdf_error = None

    if invoice_result and invoice_result.get("invoice"):
        try:
            generate_invoice_pdf(
                invoice_result["invoice"]["invoiceId"]
            )
        except Exception as invoice_pdf_exception:
            invoice_pdf_error = str(invoice_pdf_exception)

            print(
                "INVOICE PDF GENERATION ERROR:",
                repr(invoice_pdf_exception)
            )

    response = {
        "success": True,
        "order_id": order_id,
        "old_status": old_status,
        "new_status": new_status,
        "loyalty_points": loyalty_points
    }

    if invoice_result:
        response["invoice"] = {
            "invoiceId": invoice_result["invoice"]["invoiceId"],
            "invoiceNumber": invoice_result["invoice"]["invoiceNumber"],
            "created": invoice_result.get("created", False)
        }

    if invoice_pdf_error:
        response["invoice_pdf_error"] = invoice_pdf_error

    return jsonify(response)


# =========================================================
# DELIVERY - STOCK OUT PROCESSING
# =========================================================

def process_delivery_stock_out(
    command_id,
    depot_id,
    user_id,
    cursor
):
    """
    Deduct delivered quantities from global and depot stock.

    Lot-tracked inventory is consumed FEFO where available.
    If the Lots table does not contain enough stock, the
    remaining quantity is treated as legacy/untracked stock.

    All operations occur inside the caller's transaction.
    """

    if not depot_id:
        raise ValueError(
            "Order is not assigned to a depot."
        )

    now = datetime.now().isoformat(
        timespec="seconds"
    )

    # -----------------------------------------------------
    # Get order items
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            artcmdArticleId AS articleId,
            SUM(artcmdQuantity) AS quantity
        FROM articlesCommandes
        WHERE artcmdCmdId = ?
        GROUP BY artcmdArticleId
    """, (command_id,))

    order_items = cursor.fetchall()

    if not order_items:
        raise ValueError(
            "Delivered order has no items."
        )

    for order_item in order_items:

        article_id = int(order_item["articleId"])
        required_quantity = int(order_item["quantity"])

        if required_quantity <= 0:
            continue

        # -------------------------------------------------
        # Global stock
        # -------------------------------------------------

        cursor.execute("""
            SELECT articleStock
            FROM Articles
            WHERE articleId = ?
        """, (article_id,))

        article_row = cursor.fetchone()

        if not article_row:
            raise ValueError(
                f"Article {article_id} not found."
            )

        article_stock_before = int(
            article_row["articleStock"] or 0
        )

        if article_stock_before < required_quantity:
            raise ValueError(
                f"Insufficient global stock for article "
                f"{article_id}. Available: "
                f"{article_stock_before}, required: "
                f"{required_quantity}."
            )

        # -------------------------------------------------
        # Depot stock
        # -------------------------------------------------

        cursor.execute("""
            SELECT quantity
            FROM depot_stocks
            WHERE depotId = ?
              AND articleId = ?
        """, (
            depot_id,
            article_id
        ))

        depot_row = cursor.fetchone()

        if not depot_row:
            raise ValueError(
                f"No depot stock exists for article "
                f"{article_id} in depot {depot_id}."
            )

        depot_stock_before = int(
            depot_row["quantity"] or 0
        )

        if depot_stock_before < required_quantity:
            raise ValueError(
                f"Insufficient depot stock for article "
                f"{article_id}. Available: "
                f"{depot_stock_before}, required: "
                f"{required_quantity}."
            )

        # -------------------------------------------------
        # Consume available Lots using FEFO
        # -------------------------------------------------

        remaining = required_quantity

        cursor.execute("""
            SELECT
                lotId,
                lotQuantity,
                lotExpDate,
                lotRecepDate
            FROM Lots
            WHERE lotArticleId = ?
              AND lotQuantity > 0
              AND lotState = 1
            ORDER BY
                lotExpDate ASC,
                lotRecepDate ASC,
                lotId ASC
        """, (article_id,))

        lots = cursor.fetchall()

        for lot in lots:

            if remaining <= 0:
                break

            lot_id = int(lot["lotId"])
            lot_before = int(
                lot["lotQuantity"] or 0
            )

            consume = min(
                remaining,
                lot_before
            )

            lot_after = lot_before - consume

            cursor.execute("""
                UPDATE Lots
                SET
                    lotQuantity = ?,
                    lotState = ?
                WHERE lotId = ?
                  AND lotQuantity >= ?
            """, (
                lot_after,
                1 if lot_after > 0 else 0,
                lot_id,
                consume
            ))

            if cursor.rowcount != 1:
                raise ValueError(
                    f"Lot {lot_id} could not be updated."
                )

            # -------------------------------------------------
            # Legacy stock movement for lot-tracked quantity
            # -------------------------------------------------

            movement_stock_before = (
                article_stock_before
                - (required_quantity - remaining)
            )

            movement_stock_after = (
                movement_stock_before - consume
            )

            cursor.execute("""
                INSERT INTO stock_movements (
                    articleId,
                    lotId,
                    movementType,
                    quantity,
                    referenceType,
                    referenceId,
                    stockBefore,
                    stockAfter,
                    createdBy,
                    createdAt,
                    depotId
                )
                VALUES (
                    ?, ?, 'OUT', ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                article_id,
                lot_id,
                consume,
                "delivery_order",
                command_id,
                movement_stock_before,
                movement_stock_after,
                user_id,
                now,
                depot_id
            ))

            remaining -= consume

        # -------------------------------------------------
        # Remaining quantity = legacy/untracked stock
        # -------------------------------------------------

        if remaining > 0:

            movement_stock_before = (
                article_stock_before
                - (required_quantity - remaining)
            )

            movement_stock_after = (
                movement_stock_before - remaining
            )

            cursor.execute("""
                INSERT INTO stock_movements (
                    articleId,
                    lotId,
                    movementType,
                    quantity,
                    referenceType,
                    referenceId,
                    stockBefore,
                    stockAfter,
                    createdBy,
                    createdAt,
                    depotId
                )
                VALUES (
                    ?, NULL, 'OUT', ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                article_id,
                remaining,
                "delivery_order",
                command_id,
                movement_stock_before,
                movement_stock_after,
                user_id,
                now,
                depot_id
            ))

        # -------------------------------------------------
        # Update global stock
        # -------------------------------------------------

        article_stock_after = (
            article_stock_before
            - required_quantity
        )

        cursor.execute("""
            UPDATE Articles
            SET articleStock = ?
            WHERE articleId = ?
              AND articleStock >= ?
        """, (
            article_stock_after,
            article_id,
            required_quantity
        ))

        if cursor.rowcount != 1:
            raise ValueError(
                f"Global stock for article {article_id} "
                f"could not be updated."
            )

        # -------------------------------------------------
        # Update depot stock
        # -------------------------------------------------

        depot_stock_after = (
            depot_stock_before
            - required_quantity
        )

        cursor.execute("""
            UPDATE depot_stocks
            SET
                quantity = ?,
                updatedAt = ?
            WHERE depotId = ?
              AND articleId = ?
              AND quantity >= ?
        """, (
            depot_stock_after,
            now,
            depot_id,
            article_id,
            required_quantity
        ))

        if cursor.rowcount != 1:
            raise ValueError(
                f"Depot stock for article {article_id} "
                f"could not be updated."
            )

        # -------------------------------------------------
        # Depot stock audit
        # -------------------------------------------------

        cursor.execute("""
            INSERT INTO depot_stock_movements (
                depotId,
                articleId,
                movementType,
                quantity,
                stockBefore,
                stockAfter,
                referenceType,
                referenceId,
                createdBy,
                createdAt
            )
            VALUES (
                ?, ?, 'OUT', ?, ?, ?, ?, ?, ?, ?
            )
        """, (
            depot_id,
            article_id,
            required_quantity,
            depot_stock_before,
            depot_stock_after,
            "delivery_order",
            command_id,
            user_id,
            now
        ))


# =========================================================
# LIVREUR - CONFIRM ASSIGNED ORDER
# =========================================================

@app.route(
    "/livreur/orders/<int:order_id>/confirm",
    methods=["POST"]
)
def livreur_confirm_order(order_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify active Livreur
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT
                userId,
                userActive,
                userRole
            FROM users
            WHERE userId = ?
              AND userRole = 3
            """,
            (user_id,)
        )

        livreur = cursor.fetchone()

        if not livreur:

            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        if not livreur["userActive"]:

            return jsonify({
                "success": False,
                "error": "Your delivery account is inactive."
            }), 403

        # -------------------------------------------------
        # Verify assigned order
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT
                cmdId,
                cmdNum,
                cmdStatut,
                cmdLivreurId,
                cmdLivreurConfirmed
            FROM Commandes
            WHERE cmdId = ?
              AND cmdLivreurId = ?
            """,
            (
                order_id,
                user_id
            )
        )

        order = cursor.fetchone()

        if not order:

            return jsonify({
                "success": False,
                "error": (
                    "Order not found or the order "
                    "is not assigned to you."
                )
            }), 404

        # -------------------------------------------------
        # Prevent duplicate confirmation
        # -------------------------------------------------

        if order["cmdLivreurConfirmed"] == 1:

            return jsonify({
                "success": True,
                "message": "Order has already been confirmed."
            })

        # -------------------------------------------------
        # Only pending/preparation orders can be confirmed
        # -------------------------------------------------

        old_status = order["cmdStatut"]

        if old_status not in {
            "en_attente",
            "en_preparation"
        }:

            return jsonify({
                "success": False,
                "error": (
                    "Only an order awaiting delivery "
                    "can be confirmed."
                )
            }), 400

        # -------------------------------------------------
        # Confirm delivery assignment
        # -------------------------------------------------

        confirmed_at = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute(
            """
            UPDATE Commandes
            SET
                cmdLivreurConfirmed = 1,
                cmdLivreurConfirmedAt = ?,
                cmdStatut = 'en_livraison'
            WHERE cmdId = ?
              AND cmdLivreurId = ?
              AND cmdLivreurConfirmed = 0
              AND cmdStatut IN (
                  'en_attente',
                  'en_preparation'
              )
            """,
            (
                confirmed_at,
                order_id,
                user_id
            )
        )

        if cursor.rowcount != 1:

            conn.rollback()

            return jsonify({
                "success": False,
                "error": (
                    "The order could not be confirmed. "
                    "It may have already been updated."
                )
            }), 409

        # -------------------------------------------------
        # Record delivery history
        # -------------------------------------------------

        record_delivery_history(
            commandId=order["cmdId"],
            old_status=old_status,
            new_status="en_livraison",
            changed_by=user_id,
            cursor=cursor
        )

        conn.commit()

        return redirect(
            url_for("livreur_dashboard")
        )

    except Exception as e:

        conn.rollback()

        print(
            "LIVREUR CONFIRMATION ERROR:",
            repr(e)
        )

        return jsonify({
            "success": False,
            "error": "Unable to confirm the delivery."
        }), 500

    finally:

        conn.close()


# =========================================================
# LIVREUR - UPDATE ASSIGNED ORDER STATUS
# =========================================================

@app.route(
    "/livreur/orders/<int:order_id>/status",
    methods=["POST"]
)
def livreur_update_order_status(order_id):

    # -----------------------------------------------------
    # Authentication
    # -----------------------------------------------------

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify ACTIVE Livreur
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                userId,
                userName,
                userSurname,
                userActive,
                userRole
            FROM users
            WHERE userId = ?
              AND userRole = 3
        """, (user_id,))

        livreur = cursor.fetchone()

        if not livreur:

            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        if not livreur["userActive"]:

            return jsonify({
                "success": False,
                "error": "Your delivery account is inactive."
            }), 403

        # -------------------------------------------------
        # Requested status
        # -------------------------------------------------

        new_status = request.form.get(
            "status",
            ""
        ).strip().lower()

        # Livreur is intentionally restricted to delivery
        # completion.
        if new_status != "livree":

            return jsonify({
                "success": False,
                "error": (
                    "A Livreur can only mark an assigned "
                    "order as delivered."
                )
            }), 400

        # -------------------------------------------------
        # Get ONLY an order assigned to this Livreur
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                cmdId,
                cmdClientId,
                cmdNum,
                cmdStatut,
                cmdTotal,
                cmdLivreurId,
                cmdLivreurConfirmed
            FROM Commandes
            WHERE cmdId = ?
              AND cmdLivreurId = ?
        """, (
            order_id,
            user_id
        ))

        order = cursor.fetchone()

        if not order:

            return jsonify({
                "success": False,
                "error": (
                    "Order not found or the order "
                    "is not assigned to you."
                )
            }), 404

        old_status = order["cmdStatut"]

        # -------------------------------------------------
        # Livreur confirmation is mandatory
        #
        # An order cannot be delivered until the assigned
        # Livreur has explicitly confirmed the assignment.
        # -------------------------------------------------

        if order["cmdLivreurConfirmed"] != 1:

            return jsonify({
                "success": False,
                "error": (
                    "You must confirm the assigned delivery "
                    "before marking the order as delivered."
                )
            }), 403


        # -------------------------------------------------
        # Already delivered
        # -------------------------------------------------

        if old_status == "livree":

            return jsonify({
                "success": True,
                "message": "Order is already delivered."
            })

        # -------------------------------------------------
        # Livreur may only complete an order that is
        # currently in delivery.
        # -------------------------------------------------

        if old_status != "en_livraison":

            return jsonify({
                "success": False,
                "error": (
                    "Only an order currently in delivery "
                    "can be marked as delivered."
                )
            }), 400

        # -------------------------------------------------
        # Confirm assigned Livreur is still active
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                userId,
                userActive
            FROM users
            WHERE userId = ?
              AND userRole = 3
        """, (order["cmdLivreurId"],))

        assigned_livreur = cursor.fetchone()

        if not assigned_livreur:

            return jsonify({
                "success": False,
                "error": "Assigned Livreur no longer exists."
            }), 400

        if not assigned_livreur["userActive"]:

            return jsonify({
                "success": False,
                "error": "Assigned Livreur is inactive."
            }), 400

        # -------------------------------------------------
        # Verify order depot
        # -------------------------------------------------

        cursor.execute("""
            SELECT cmdDepotId
            FROM Commandes
            WHERE cmdId = ?
              AND cmdLivreurId = ?
        """, (
            order_id,
            user_id
        ))

        order_depot_row = cursor.fetchone()

        if (
            not order_depot_row
            or not order_depot_row["cmdDepotId"]
        ):
            conn.rollback()

            return jsonify({
                "success": False,
                "error": (
                    "The order is not assigned to a depot."
                )
            }), 400

        # -------------------------------------------------
        # Deduct inventory
        # -------------------------------------------------

        process_delivery_stock_out(
            command_id=order_id,
            depot_id=order_depot_row["cmdDepotId"],
            user_id=user_id,
            cursor=cursor
        )

        # -------------------------------------------------
        # Update order
        # -------------------------------------------------

        cursor.execute("""
            UPDATE Commandes
            SET cmdStatut = ?
            WHERE cmdId = ?
              AND cmdLivreurId = ?
              AND cmdLivreurConfirmed = 1
              AND cmdStatut = 'en_livraison'
        """, (
            "livree",
            order_id,
            user_id
        ))

        if cursor.rowcount != 1:

            conn.rollback()

            return jsonify({
                "success": False,
                "error": "Unable to update the order."
            }), 409

        # -------------------------------------------------
        # Delivery history
        # -------------------------------------------------

        record_delivery_history(
            commandId=order["cmdId"],
            old_status=old_status,
            new_status="livree",
            changed_by=user_id,
            cursor=cursor
        )

        # -------------------------------------------------
        # Loyalty reward
        #
        # Uses the same existing loyalty function as the
        # administrator status workflow.
        # -------------------------------------------------

        loyalty_points = award_loyalty_points(
            user_id=order["cmdClientId"],
            commandId=order["cmdId"],
            total_amount=order["cmdTotal"],
            cursor=cursor
        )

        # -------------------------------------------------
        # Invoice database record
        #
        # Use the SAME transaction as delivery completion.
        # If invoice creation fails, the existing exception
        # handler rolls back stock/status/history/loyalty.
        # -------------------------------------------------

        invoice_result = create_invoice_for_order(
            order["cmdId"],
            conn=conn
        )

        # -------------------------------------------------
        # Commit complete delivery transaction
        # -------------------------------------------------

        conn.commit()

        # -------------------------------------------------
        # Generate PDF AFTER database commit
        #
        # A PDF failure must not undo a successfully
        # completed delivery transaction.
        # -------------------------------------------------

        if invoice_result and invoice_result.get("invoice"):
            try:
                generate_invoice_pdf(
                    invoice_result["invoice"]["invoiceId"]
                )
            except Exception as invoice_pdf_error:
                print(
                    "INVOICE PDF GENERATION ERROR:",
                    repr(invoice_pdf_error)
                )

        return redirect(
            url_for("livreur_dashboard")
        )

    except ValueError as exc:

        conn.rollback()

        print(
            "LIVREUR DELIVERY VALIDATION ERROR:",
            repr(exc)
        )

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 400

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to update the order."
        }), 500

    finally:

        conn.close()

# =========================================================
# ADMIN - LIVREUR MANAGEMENT
# =========================================================

@app.route("/admin/delivery-persons")
def admin_delivery_persons():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # -----------------------------------------------------
    # Verify administrator
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userRole
        FROM users
        WHERE userId = ?
    """, (user_id,))

    current_user = cursor.fetchone()

    if not current_user or current_user["userRole"] != 1:

        conn.close()

        return jsonify({
            "success": False,
            "error": "Unauthorized."
        }), 403

    # -----------------------------------------------------
    # Get delivery personnel
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userName,
            userSurname,
            userMail,
            userPhone,
            userGender,
            userAddress,
            userActive
        FROM users
        WHERE userRole = 3
        ORDER BY userName, userSurname
    """)

    delivery_persons = cursor.fetchall()

    conn.close()

    return render_template(
        "admin_delivery_persons.html",
        delivery_persons=delivery_persons
    )


# =========================================================
# ADMIN - CREATE LIVREUR
# =========================================================

@app.route(
    "/admin/delivery-persons/create",
    methods=["POST"]
)
def create_delivery_person():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # -----------------------------------------------------
    # Verify administrator
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userRole
        FROM users
        WHERE userId = ?
    """, (user_id,))

    current_user = cursor.fetchone()

    if not current_user or current_user["userRole"] != 1:

        conn.close()

        return jsonify({
            "success": False,
            "error": "Unauthorized."
        }), 403

    # -----------------------------------------------------
    # Read form data
    # -----------------------------------------------------

    name = request.form.get(
        "name",
        ""
    ).strip()

    surname = request.form.get(
        "surname",
        ""
    ).strip()

    email = request.form.get(
        "email",
        ""
    ).strip().lower()

    phone = request.form.get(
        "phone",
        ""
    ).strip()

    gender = request.form.get(
        "gender",
        ""
    ).strip()

    address = request.form.get(
        "address",
        ""
    ).strip()

    password = request.form.get(
        "password",
        ""
    )

    password_confirmation = request.form.get(
        "password_confirmation",
        ""
    )

    # -----------------------------------------------------
    # Required fields
    # -----------------------------------------------------

    if not all([
        name,
        surname,
        email,
        phone,
        gender,
        address,
        password,
        password_confirmation
    ]):

        conn.close()

        return redirect(
            url_for("admin_delivery_persons")
        )

    # -----------------------------------------------------
    # Email validation
    # -----------------------------------------------------

    email_pattern = (
        r"^[A-Za-z0-9._%+-]+"
        r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
    )

    if not re.fullmatch(email_pattern, email):

        conn.close()

        return redirect(
            url_for("admin_delivery_persons")
        )

    # -----------------------------------------------------
    # Cameroon phone validation
    # -----------------------------------------------------

    phone = (
        phone
        .replace(" ", "")
        .replace("-", "")
    )

    if phone.startswith("+237"):

        phone = phone[4:]

    elif phone.startswith("237"):

        phone = phone[3:]

    if (
        not phone.isdigit()
        or len(phone) != 9
        or not phone.startswith("6")
    ):

        conn.close()

        return redirect(
            url_for("admin_delivery_persons")
        )

    # -----------------------------------------------------
    # Gender validation
    # -----------------------------------------------------

    if gender not in ("M", "F"):

        conn.close()

        return redirect(
            url_for("admin_delivery_persons")
        )

    # -----------------------------------------------------
    # Password validation
    # -----------------------------------------------------

    if len(password) < 6:

        conn.close()

        return redirect(
            url_for("admin_delivery_persons")
        )

    if password != password_confirmation:

        conn.close()

        return redirect(
            url_for("admin_delivery_persons")
        )

    try:

        # -------------------------------------------------
        # Check duplicate email
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId
            FROM users
            WHERE userMail = ?
        """, (email,))

        existing_user = cursor.fetchone()

        if existing_user:

            conn.close()

            return redirect(
                url_for("admin_delivery_persons")
            )

        # -------------------------------------------------
        # Verify Livreur role
        # -------------------------------------------------

        cursor.execute("""
            SELECT idRole
            FROM roles
            WHERE idRole = 3
              AND nomRole = 'Livreur'
        """)

        delivery_role = cursor.fetchone()

        if not delivery_role:

            conn.close()

            return jsonify({
                "success": False,
                "error": "The Livreur role is not configured."
            }), 500

        # -------------------------------------------------
        # Hash password
        # -------------------------------------------------

        password_hash = generate_password_hash(
            password
        )

        # -------------------------------------------------
        # Create Livreur
        # -------------------------------------------------

        cursor.execute("""
            INSERT INTO users (
                userName,
                userSurname,
                userMail,
                userPsw,
                userPhone,
                userRole,
                userGender,
                userAddress,
                userActive
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            name,
            surname,
            email,
            password_hash,
            phone,
            delivery_role["idRole"],
            gender,
            address
        ))

        conn.commit()

    except sqlite3.IntegrityError:

        conn.rollback()

    except Exception:

        conn.rollback()

    finally:

        conn.close()

    return redirect(
        url_for("admin_delivery_persons")
    )


# =========================================================
# ADMIN - TOGGLE LIVREUR ACTIVE STATUS
# =========================================================

@app.route(
    "/admin/delivery-persons/<int:delivery_person_id>/toggle",
    methods=["POST"]
)
def toggle_delivery_person(delivery_person_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    # -----------------------------------------------------
    # Verify administrator
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userRole
        FROM users
        WHERE userId = ?
    """, (user_id,))

    current_user = cursor.fetchone()

    if not current_user or current_user["userRole"] != 1:

        conn.close()

        return jsonify({
            "success": False,
            "error": "Unauthorized."
        }), 403

    # -----------------------------------------------------
    # Get Livreur
    # -----------------------------------------------------

    cursor.execute("""
        SELECT
            userId,
            userActive
        FROM users
        WHERE userId = ?
          AND userRole = 3
    """, (delivery_person_id,))

    delivery_person = cursor.fetchone()

    if not delivery_person:

        conn.close()

        return jsonify({
            "success": False,
            "error": "Delivery person not found."
        }), 404

    # -----------------------------------------------------
    # Toggle status
    # -----------------------------------------------------

    new_status = (
        0
        if delivery_person["userActive"]
        else 1
    )

    cursor.execute("""
        UPDATE users
        SET userActive = ?
        WHERE userId = ?
          AND userRole = 3
    """, (
        new_status,
        delivery_person_id
    ))

    conn.commit()
    conn.close()

    return redirect(
        url_for("admin_delivery_persons")
    )

# =========================================================
# ADMIN - PURCHASE ORDER MANAGEMENT
# =========================================================

@app.route("/admin/purchase-orders")
def admin_purchase_orders():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Get purchase orders
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                po.purchaseOrderId,
                po.purchaseOrderNumber,
                po.supplierId,
                s.supplierName,
                po.orderDate,
                po.expectedDate,
                po.status,
                po.notes,
                po.createdBy,
                po.createdAt,
                po.updatedAt,

                COALESCE(
                    SUM(poi.orderedQuantity),
                    0
                ) AS totalOrderedQuantity,

                COALESCE(
                    SUM(poi.receivedQuantity),
                    0
                ) AS totalReceivedQuantity,

                COUNT(poi.purchaseOrderItemId)
                    AS itemCount

            FROM purchase_orders po

            JOIN suppliers s
                ON s.supplierId = po.supplierId

            LEFT JOIN purchase_order_items poi
                ON poi.purchaseOrderId =
                   po.purchaseOrderId

            GROUP BY
                po.purchaseOrderId,
                po.purchaseOrderNumber,
                po.supplierId,
                s.supplierName,
                po.orderDate,
                po.expectedDate,
                po.status,
                po.notes,
                po.createdBy,
                po.createdAt,
                po.updatedAt

            ORDER BY
                po.purchaseOrderId DESC
        """)

        purchase_orders = [
            dict(row)
            for row in cursor.fetchall()
        ]

        return render_template(
            "admin_purchase_orders.html",
            purchase_orders=purchase_orders
        )

    finally:
        conn.close()


# =========================================================
# ADMIN - CREATE PURCHASE ORDER
# =========================================================

@app.route(
    "/admin/purchase-orders/create",
    methods=["GET", "POST"]
)
def create_purchase_order():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Display purchase-order creation form
        # -------------------------------------------------

        if request.method == "GET":

            cursor.execute("""
                SELECT
                    supplierId,
                    supplierName,
                    supplierContact,
                    supplierPhone,
                    supplierEmail
                FROM suppliers
                WHERE supplierActive = 1
                ORDER BY supplierName
            """)

            suppliers = [
                dict(row)
                for row in cursor.fetchall()
            ]

            # -------------------------------------------------
            # Load active receiving depots
            # -------------------------------------------------

            cursor.execute("""
                SELECT
                    depotId,
                    depotName,
                    depotCode,
                    depotAddress
                FROM depots
                WHERE depotActive = 1
                ORDER BY depotName, depotId
            """)

            depots = [
                dict(row)
                for row in cursor.fetchall()
            ]

            return render_template(
                "admin_purchase_order_create.html",
                suppliers=suppliers,
                depots=depots,
                current_date=datetime.now().strftime("%Y-%m-%d")
            )

        # -------------------------------------------------
        # Form data
        # -------------------------------------------------

        supplier_id = request.form.get(
            "supplierId",
            ""
        ).strip()

        depot_id = request.form.get(
            "depotId",
            ""
        ).strip()

        order_date = request.form.get(
            "orderDate",
            ""
        ).strip()

        expected_date = request.form.get(
            "expectedDate",
            ""
        ).strip()

        notes = request.form.get(
            "notes",
            ""
        ).strip()

        # -------------------------------------------------
        # Validate supplier ID
        # -------------------------------------------------

        try:
            supplier_id = int(supplier_id)
        except (ValueError, TypeError):

            return jsonify({
                "success": False,
                "error": "Invalid supplier."
            }), 400

        # -------------------------------------------------
        # Verify supplier
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                supplierId,
                supplierActive
            FROM suppliers
            WHERE supplierId = ?
        """, (supplier_id,))

        supplier = cursor.fetchone()

        if not supplier:
            return jsonify({
                "success": False,
                "error": "Supplier not found."
            }), 404

        if not supplier["supplierActive"]:
            return jsonify({
                "success": False,
                "error": "Supplier is inactive."
            }), 400

        # -------------------------------------------------
        # Dates
        # -------------------------------------------------

        if not order_date:
            order_date = datetime.now().strftime(
                "%Y-%m-%d"
            )

        # -------------------------------------------------
        # Validate receiving depot
        # -------------------------------------------------

        try:
            depot_id = int(depot_id)
        except (ValueError, TypeError):

            return jsonify({
                "success": False,
                "error": "Please select a valid receiving depot."
            }), 400

        cursor.execute("""
            SELECT
                depotId,
                depotName,
                depotCode,
                depotActive
            FROM depots
            WHERE depotId = ?
        """, (depot_id,))

        selected_depot = cursor.fetchone()

        if not selected_depot:
            return jsonify({
                "success": False,
                "error": "Selected depot was not found."
            }), 404

        if not selected_depot["depotActive"]:
            return jsonify({
                "success": False,
                "error": "Selected depot is inactive."
            }), 400

        # -------------------------------------------------
        # Create PO using a temporary unique number
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        temporary_number = (
            f"TEMP-{now.replace(':', '').replace('-', '')}-"
            f"{user_id}"
        )

        cursor.execute("""
            INSERT INTO purchase_orders (
                purchaseOrderNumber,
                supplierId,
                orderDate,
                expectedDate,
                status,
                notes,
                createdBy,
                createdAt,
                updatedAt,
                depotId
            )
            VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)
        """, (
            temporary_number,
            supplier_id,
            order_date,
            expected_date or None,
            notes or None,
            user_id,
            now,
            now,
            depot_id
        ))

        purchase_order_id = cursor.lastrowid

        # -------------------------------------------------
        # Generate final PO number from database ID
        # -------------------------------------------------

        year = datetime.now().year

        purchase_order_number = (
            f"PO-{year}-{purchase_order_id:04d}"
        )

        cursor.execute("""
            UPDATE purchase_orders
            SET
                purchaseOrderNumber = ?,
                updatedAt = ?
            WHERE purchaseOrderId = ?
        """, (
            purchase_order_number,
            now,
            purchase_order_id
        ))

        conn.commit()

        return redirect(
            url_for(
                "view_purchase_order",
                purchase_order_id=purchase_order_id
            )
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to create purchase order."
        }), 400

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to create purchase order."
        }), 500

    finally:
        conn.close()


# =========================================================
# ADMIN - VIEW PURCHASE ORDER
# =========================================================

@app.route(
    "/admin/purchase-orders/<int:purchase_order_id>"
)
def view_purchase_order(purchase_order_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Purchase order
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                po.*,

                s.supplierName,
                s.supplierContact,
                s.supplierPhone,
                s.supplierEmail,

                d.depotName,
                d.depotCode,
                d.depotAddress

            FROM purchase_orders po

            JOIN suppliers s
                ON s.supplierId = po.supplierId

            LEFT JOIN depots d
                ON d.depotId = po.depotId

            WHERE po.purchaseOrderId = ?
        """, (purchase_order_id,))

        purchase_order = cursor.fetchone()

        if not purchase_order:
            return jsonify({
                "success": False,
                "error": "Purchase order not found."
            }), 404

        # -------------------------------------------------
        # Items
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                poi.purchaseOrderItemId,
                poi.purchaseOrderId,
                poi.articleId,
                poi.supplierArticleId,
                poi.orderedQuantity,
                poi.receivedQuantity,
                poi.purchasePrice,
                poi.createdAt,
                poi.updatedAt,

                a.articleVolume,
                a.articlePackage,
                a.articleStock,

                p.productName,

                sa.supplierReference,
                sa.minimumOrderQuantity,
                sa.leadTimeDays,
                sa.isPreferred

            FROM purchase_order_items poi

            JOIN Articles a
                ON a.articleId = poi.articleId

            JOIN products p
                ON p.productId = a.articleProductId

            LEFT JOIN supplier_articles sa
                ON sa.supplierArticleId =
                   poi.supplierArticleId

            WHERE poi.purchaseOrderId = ?

            ORDER BY poi.purchaseOrderItemId
        """, (purchase_order_id,))

        items = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Receipt history
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                pr.receiptId,
                pr.receiptNumber,
                pr.purchaseOrderId,
                pr.receivedDate,
                pr.receivedBy,
                pr.notes,
                pr.createdAt,

                u.userName,
                u.userSurname

            FROM purchase_receipts pr

            LEFT JOIN users u
                ON u.userId = pr.receivedBy

            WHERE pr.purchaseOrderId = ?

            ORDER BY pr.receiptId DESC
        """, (purchase_order_id,))

        receipts = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Receipt item details
        # -------------------------------------------------

        for receipt in receipts:

            cursor.execute("""
                SELECT
                    pri.receiptItemId,
                    pri.receiptId,
                    pri.purchaseOrderItemId,
                    pri.articleId,
                    pri.receivedQuantity,
                    pri.purchasePrice,
                    pri.lotId,
                    pri.createdAt,

                    a.articleVolume,
                    a.articlePackage,

                    p.productName,

                    l.lotQuantity,
                    l.lotExpDate,
                    l.lotRecepDate,
                    l.lotState

                FROM purchase_receipt_items pri

                JOIN Articles a
                    ON a.articleId = pri.articleId

                JOIN products p
                    ON p.productId = a.articleProductId

                LEFT JOIN Lots l
                    ON l.lotId = pri.lotId

                WHERE pri.receiptId = ?

                ORDER BY pri.receiptItemId
            """, (receipt["receiptId"],))

            receipt["items"] = [
                dict(row)
                for row in cursor.fetchall()
            ]

        # -------------------------------------------------
        # Available supplier articles
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                sa.supplierArticleId,
                sa.supplierId,
                sa.articleId,
                sa.supplierReference,
                sa.purchasePrice,
                sa.minimumOrderQuantity,
                sa.leadTimeDays,
                sa.isPreferred,

                a.articleVolume,
                a.articlePackage,
                a.articleStock,

                p.productName

            FROM supplier_articles sa

            JOIN Articles a
                ON a.articleId = sa.articleId

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE sa.supplierId = ?
              AND sa.supplierArticleActive = 1

            ORDER BY
                p.productName,
                a.articleVolume
        """, (purchase_order["supplierId"],))

        available_articles = [
            dict(row)
            for row in cursor.fetchall()
        ]

        return render_template(
    "admin_purchase_order_detail.html",
    purchase_order=dict(purchase_order),
    items=items,
    receipts=receipts,
    available_articles=available_articles,
    current_date=datetime.now().strftime("%Y-%m-%d")
)

    finally:
        conn.close()


# =========================================================
# ADMIN - ADD PURCHASE ORDER ITEM
# =========================================================

@app.route(
    "/admin/purchase-orders/<int:purchase_order_id>/items/create",
    methods=["POST"]
)
def create_purchase_order_item(purchase_order_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Verify draft PO
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                purchaseOrderId,
                supplierId,
                status
            FROM purchase_orders
            WHERE purchaseOrderId = ?
        """, (purchase_order_id,))

        purchase_order = cursor.fetchone()

        if not purchase_order:
            return jsonify({
                "success": False,
                "error": "Purchase order not found."
            }), 404

        if purchase_order["status"] != "draft":
            return jsonify({
                "success": False,
                "error": "Only draft purchase orders can be modified."
            }), 400

        # -------------------------------------------------
        # Form data
        # -------------------------------------------------

        supplier_article_id = request.form.get(
            "supplierArticleId",
            ""
        ).strip()

        ordered_quantity = request.form.get(
            "orderedQuantity",
            ""
        ).strip()

        purchase_price = request.form.get(
            "purchasePrice",
            ""
        ).strip()

        # -------------------------------------------------
        # Validate values
        # -------------------------------------------------

        try:

            supplier_article_id = int(
                supplier_article_id
            )

            ordered_quantity = int(
                ordered_quantity
            )

            purchase_price = float(
                purchase_price
            )

        except (ValueError, TypeError):

            return jsonify({
                "success": False,
                "error": "Invalid item data."
            }), 400

        if ordered_quantity <= 0:
            return jsonify({
                "success": False,
                "error": "Ordered quantity must be greater than zero."
            }), 400

        if purchase_price < 0:
            return jsonify({
                "success": False,
                "error": "Purchase price cannot be negative."
            }), 400

        # -------------------------------------------------
        # Verify supplier article
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                supplierArticleId,
                supplierId,
                articleId,
                purchasePrice,
                supplierArticleActive
            FROM supplier_articles
            WHERE supplierArticleId = ?
        """, (supplier_article_id,))

        supplier_article = cursor.fetchone()

        if not supplier_article:
            return jsonify({
                "success": False,
                "error": "Supplier article not found."
            }), 404

        if (
            supplier_article["supplierId"]
            != purchase_order["supplierId"]
        ):
            return jsonify({
                "success": False,
                "error": "Supplier article does not belong to this supplier."
            }), 400

        if not supplier_article["supplierArticleActive"]:
            return jsonify({
                "success": False,
                "error": "Supplier article is inactive."
            }), 400

        article_id = supplier_article["articleId"]

        # -------------------------------------------------
        # Prevent duplicate article
        # -------------------------------------------------

        cursor.execute("""
            SELECT purchaseOrderItemId
            FROM purchase_order_items
            WHERE purchaseOrderId = ?
              AND articleId = ?
        """, (
            purchase_order_id,
            article_id
        ))

        if cursor.fetchone():

            return jsonify({
                "success": False,
                "error": "This article is already on the purchase order."
            }), 400

        # -------------------------------------------------
        # Insert item
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO purchase_order_items (
                purchaseOrderId,
                articleId,
                supplierArticleId,
                orderedQuantity,
                receivedQuantity,
                purchasePrice,
                createdAt,
                updatedAt
            )
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
        """, (
            purchase_order_id,
            article_id,
            supplier_article_id,
            ordered_quantity,
            purchase_price,
            now,
            now
        ))

        conn.commit()

        return redirect(
            url_for(
                "view_purchase_order",
                purchase_order_id=purchase_order_id
            )
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to add purchase order item."
        }), 400

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to add purchase order item."
        }), 500

    finally:
        conn.close()


# =========================================================
# ADMIN - EDIT PURCHASE ORDER ITEM
# =========================================================

@app.route(
    "/admin/purchase-orders/<int:purchase_order_id>/items/<int:item_id>/edit",
    methods=["POST"]
)
def edit_purchase_order_item(
    purchase_order_id,
    item_id
):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Verify draft PO and item
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                po.purchaseOrderId,
                po.status,
                poi.purchaseOrderItemId,
                poi.articleId,
                poi.receivedQuantity
            FROM purchase_orders po

            JOIN purchase_order_items poi
                ON poi.purchaseOrderId =
                   po.purchaseOrderId

            WHERE po.purchaseOrderId = ?
              AND poi.purchaseOrderItemId = ?
        """, (
            purchase_order_id,
            item_id
        ))

        item = cursor.fetchone()

        if not item:
            return jsonify({
                "success": False,
                "error": "Purchase order item not found."
            }), 404

        if item["status"] != "draft":
            return jsonify({
                "success": False,
                "error": "Only draft purchase orders can be modified."
            }), 400

        # -------------------------------------------------
        # Form data
        # -------------------------------------------------

        ordered_quantity = request.form.get(
            "orderedQuantity",
            ""
        ).strip()

        purchase_price = request.form.get(
            "purchasePrice",
            ""
        ).strip()

        try:

            ordered_quantity = int(
                ordered_quantity
            )

            purchase_price = float(
                purchase_price
            )

        except (ValueError, TypeError):

            return jsonify({
                "success": False,
                "error": "Invalid item data."
            }), 400

        if ordered_quantity <= 0:
            return jsonify({
                "success": False,
                "error": "Ordered quantity must be greater than zero."
            }), 400

        if purchase_price < 0:
            return jsonify({
                "success": False,
                "error": "Purchase price cannot be negative."
            }), 400

        # -------------------------------------------------
        # Update
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            UPDATE purchase_order_items
            SET
                orderedQuantity = ?,
                purchasePrice = ?,
                updatedAt = ?
            WHERE purchaseOrderItemId = ?
              AND purchaseOrderId = ?
        """, (
            ordered_quantity,
            purchase_price,
            now,
            item_id,
            purchase_order_id
        ))

        conn.commit()

        return redirect(
            url_for(
                "view_purchase_order",
                purchase_order_id=purchase_order_id
            )
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to update purchase order item."
        }), 400

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to update purchase order item."
        }), 500

    finally:
        conn.close()


# =========================================================
# ADMIN - DELETE PURCHASE ORDER ITEM
# =========================================================

@app.route(
    "/admin/purchase-orders/<int:purchase_order_id>/items/<int:item_id>/delete",
    methods=["POST"]
)
def delete_purchase_order_item(
    purchase_order_id,
    item_id
):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Verify draft PO and item
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                po.status,
                poi.purchaseOrderItemId
            FROM purchase_orders po

            JOIN purchase_order_items poi
                ON poi.purchaseOrderId =
                   po.purchaseOrderId

            WHERE po.purchaseOrderId = ?
              AND poi.purchaseOrderItemId = ?
        """, (
            purchase_order_id,
            item_id
        ))

        item = cursor.fetchone()

        if not item:
            return jsonify({
                "success": False,
                "error": "Purchase order item not found."
            }), 404

        if item["status"] != "draft":
            return jsonify({
                "success": False,
                "error": "Only draft purchase orders can be modified."
            }), 400

        cursor.execute("""
            DELETE FROM purchase_order_items
            WHERE purchaseOrderItemId = ?
              AND purchaseOrderId = ?
        """, (
            item_id,
            purchase_order_id
        ))

        conn.commit()

        return redirect(
            url_for(
                "view_purchase_order",
                purchase_order_id=purchase_order_id
            )
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to delete purchase order item."
        }), 400

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to delete purchase order item."
        }), 500

    finally:
        conn.close()


# =========================================================
# ADMIN - SUBMIT PURCHASE ORDER
# =========================================================

@app.route(
    "/admin/purchase-orders/<int:purchase_order_id>/submit",
    methods=["POST"]
)
def submit_purchase_order(purchase_order_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Verify PO
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                purchaseOrderId,
                status
            FROM purchase_orders
            WHERE purchaseOrderId = ?
        """, (purchase_order_id,))

        purchase_order = cursor.fetchone()

        if not purchase_order:
            return jsonify({
                "success": False,
                "error": "Purchase order not found."
            }), 404

        if purchase_order["status"] != "draft":
            return jsonify({
                "success": False,
                "error": "Only draft purchase orders can be submitted."
            }), 400

        # -------------------------------------------------
        # Require at least one item
        # -------------------------------------------------

        cursor.execute("""
            SELECT COUNT(*) AS itemCount
            FROM purchase_order_items
            WHERE purchaseOrderId = ?
        """, (purchase_order_id,))

        item_count = cursor.fetchone()["itemCount"]

        if item_count < 1:
            return jsonify({
                "success": False,
                "error": "A purchase order must contain at least one item."
            }), 400

        # -------------------------------------------------
        # Submit
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            UPDATE purchase_orders
            SET
                status = 'ordered',
                updatedAt = ?
            WHERE purchaseOrderId = ?
              AND status = 'draft'
        """, (
            now,
            purchase_order_id
        ))

        if cursor.rowcount != 1:
            conn.rollback()

            return jsonify({
                "success": False,
                "error": "Purchase order could not be submitted."
            }), 400

        conn.commit()

        return redirect(
            url_for(
                "view_purchase_order",
                purchase_order_id=purchase_order_id
            )
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to submit purchase order."
        }), 400

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to submit purchase order."
        }), 500

    finally:
        conn.close()



# =========================================================
# ADMIN - RECEIVE PURCHASE ORDER
# =========================================================

@app.route(
    "/admin/purchase-orders/<int:purchase_order_id>/receive",
    methods=["POST"]
)
def receive_purchase_order(purchase_order_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Verify purchase order
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                purchaseOrderId,
                purchaseOrderNumber,
                status,
                depotId
            FROM purchase_orders
            WHERE purchaseOrderId = ?
        """, (purchase_order_id,))

        purchase_order = cursor.fetchone()

        if not purchase_order:
            return jsonify({
                "success": False,
                "error": "Purchase order not found."
            }), 404

        if purchase_order["status"] not in (
            "ordered",
            "partially_received"
        ):
            return jsonify({
                "success": False,
                "error": (
                    "Only ordered or partially received "
                    "purchase orders can be received."
                )
            }), 400

        # -------------------------------------------------
        # Validate purchase-order depot
        # -------------------------------------------------

        depot_id = purchase_order["depotId"]

        if not depot_id:
            return jsonify({
                "success": False,
                "error": "Purchase order is not assigned to a depot."
            }), 400

        cursor.execute("""
            SELECT
                depotId,
                depotName,
                depotActive
            FROM depots
            WHERE depotId = ?
              AND depotActive = 1
        """, (depot_id,))

        depot = cursor.fetchone()

        if not depot:
            return jsonify({
                "success": False,
                "error": (
                    "The assigned depot is not active "
                    "or does not exist."
                )
            }), 400

        # -------------------------------------------------
        # Receipt date
        # -------------------------------------------------

        received_date = request.form.get(
            "receivedDate",
            ""
        ).strip()

        if not received_date:
            received_date = datetime.now().strftime(
                "%Y-%m-%d"
            )

        try:
            datetime.strptime(
                received_date,
                "%Y-%m-%d"
            )
        except ValueError:

            return jsonify({
                "success": False,
                "error": "Invalid receipt date."
            }), 400

        notes = request.form.get(
            "notes",
            ""
        ).strip()

        # -------------------------------------------------
        # Get purchase-order items
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                purchaseOrderItemId,
                articleId,
                orderedQuantity,
                receivedQuantity,
                purchasePrice
            FROM purchase_order_items
            WHERE purchaseOrderId = ?
            ORDER BY purchaseOrderItemId
        """, (purchase_order_id,))

        items = cursor.fetchall()

        if not items:
            return jsonify({
                "success": False,
                "error": "Purchase order has no items."
            }), 400

        # -------------------------------------------------
        # Validate submitted quantities and expiration dates
        # -------------------------------------------------

        receipt_items = []
        total_received = 0

        for item in items:

            item_id = item["purchaseOrderItemId"]

            field_name = (
                f"receivedQuantity_{item_id}"
            )

            quantity_raw = request.form.get(
                field_name,
                "0"
            ).strip()

            try:
                quantity = int(quantity_raw or 0)
            except (ValueError, TypeError):

                return jsonify({
                    "success": False,
                    "error": (
                        f"Invalid quantity for "
                        f"purchase order item {item_id}."
                    )
                }), 400

            if quantity < 0:

                return jsonify({
                    "success": False,
                    "error": (
                        f"Received quantity cannot "
                        f"be negative for item {item_id}."
                    )
                }), 400

            remaining = (
                item["orderedQuantity"]
                - item["receivedQuantity"]
            )

            if quantity > remaining:

                return jsonify({
                    "success": False,
                    "error": (
                        f"Received quantity for item "
                        f"{item_id} exceeds the remaining "
                        f"quantity of {remaining}."
                    )
                }), 400

            if quantity == 0:
                continue

            expiration_field = (
                f"lotExpDate_{item_id}"
            )

            expiration_date = request.form.get(
                expiration_field,
                ""
            ).strip()

            if not expiration_date:

                return jsonify({
                    "success": False,
                    "error": (
                        f"Expiration date is required "
                        f"for item {item_id}."
                    )
                }), 400

            try:
                expiration = datetime.strptime(
                    expiration_date,
                    "%Y-%m-%d"
                )
            except ValueError:

                return jsonify({
                    "success": False,
                    "error": (
                        f"Invalid expiration date "
                        f"for item {item_id}."
                    )
                }), 400

            if expiration < datetime.strptime(
                received_date,
                "%Y-%m-%d"
            ):

                return jsonify({
                    "success": False,
                    "error": (
                        f"Expiration date cannot be "
                        f"before the receipt date "
                        f"for item {item_id}."
                    )
                }), 400

            receipt_items.append({
                "purchaseOrderItemId": item_id,
                "articleId": item["articleId"],
                "quantity": quantity,
                "purchasePrice": item["purchasePrice"],
                "expirationDate": expiration_date
            })

            total_received += quantity

        # -------------------------------------------------
        # Require at least one received item
        # -------------------------------------------------

        if total_received <= 0:

            return jsonify({
                "success": False,
                "error": (
                    "Enter a received quantity for "
                    "at least one item."
                )
            }), 400

        # -------------------------------------------------
        # Create receipt
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        temporary_number = (
            f"TEMP-REC-"
            f"{now.replace(':', '').replace('-', '')}-"
            f"{user_id}"
        )

        cursor.execute("""
            INSERT INTO purchase_receipts (
                receiptNumber,
                purchaseOrderId,
                receivedDate,
                receivedBy,
                notes,
                createdAt,
                depotId
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            temporary_number,
            purchase_order_id,
            received_date,
            user_id,
            notes or None,
            now,
            depot_id
        ))

        receipt_id = cursor.lastrowid

        # -------------------------------------------------
        # Generate final receipt number
        # -------------------------------------------------

        year = datetime.now().year

        receipt_number = (
            f"REC-{year}-{receipt_id:04d}"
        )

        cursor.execute("""
            UPDATE purchase_receipts
            SET receiptNumber = ?
            WHERE receiptId = ?
        """, (
            receipt_number,
            receipt_id
        ))

        # -------------------------------------------------
        # Process each received item
        # -------------------------------------------------

        for received_item in receipt_items:

            item_id = (
                received_item["purchaseOrderItemId"]
            )

            article_id = (
                received_item["articleId"]
            )

            quantity = (
                received_item["quantity"]
            )

            purchase_price = (
                received_item["purchasePrice"]
            )

            expiration_date = (
                received_item["expirationDate"]
            )

            # ---------------------------------------------
            # Receipt item
            # ---------------------------------------------

            cursor.execute("""
                INSERT INTO purchase_receipt_items (
                    receiptId,
                    purchaseOrderItemId,
                    articleId,
                    receivedQuantity,
                    lotId,
                    purchasePrice,
                    createdAt
                )
                VALUES (?, ?, ?, ?, NULL, ?, ?)
            """, (
                receipt_id,
                item_id,
                article_id,
                quantity,
                purchase_price,
                now
            ))

            receipt_item_id = cursor.lastrowid

            # ---------------------------------------------
            # Create lot
            # ---------------------------------------------

            cursor.execute("""
                INSERT INTO Lots (
                    lotArticleId,
                    lotQuantity,
                    lotExpDate,
                    lotRecepDate,
                    lotState
                )
                VALUES (?, ?, ?, ?, 1)
            """, (
                article_id,
                quantity,
                expiration_date,
                received_date
            ))

            lot_id = cursor.lastrowid

            # ---------------------------------------------
            # Link receipt item to lot
            # ---------------------------------------------

            cursor.execute("""
                UPDATE purchase_receipt_items
                SET lotId = ?
                WHERE receiptItemId = ?
            """, (
                lot_id,
                receipt_item_id
            ))

            # ---------------------------------------------
            # Increase article stock
            # ---------------------------------------------

            cursor.execute("""
                SELECT articleStock
                FROM Articles
                WHERE articleId = ?
            """, (
                article_id,
            ))

            article_stock_row = cursor.fetchone()

            if not article_stock_row:

                raise ValueError(
                    f"Article {article_id} not found."
                )

            stock_before = int(
                article_stock_row["articleStock"]
            )

            stock_after = (
                stock_before + quantity
            )

            cursor.execute("""
                UPDATE Articles
                SET articleStock = ?
                WHERE articleId = ?
            """, (
                stock_after,
                article_id
            ))

            if cursor.rowcount != 1:

                raise ValueError(
                    f"Article {article_id} stock could not be updated."
                )

            # ---------------------------------------------
            # Increase depot stock
            # ---------------------------------------------

            cursor.execute("""
                INSERT INTO depot_stocks (
                    depotId,
                    articleId,
                    quantity,
                    createdAt,
                    updatedAt
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(depotId, articleId)
                DO UPDATE SET
                    quantity = quantity + excluded.quantity,
                    updatedAt = excluded.updatedAt
            """, (
                depot_id,
                article_id,
                quantity,
                now,
                now
            ))

            if cursor.rowcount != 1:
                raise ValueError(
                    f"Depot stock for article {article_id} "
                    f"could not be updated."
                )

            # ---------------------------------------------
            # Read updated depot stock
            # ---------------------------------------------

            cursor.execute("""
                SELECT quantity
                FROM depot_stocks
                WHERE depotId = ?
                  AND articleId = ?
            """, (
                depot_id,
                article_id
            ))

            depot_stock_row = cursor.fetchone()

            if not depot_stock_row:
                raise ValueError(
                    f"Depot stock for article {article_id} "
                    f"could not be found."
                )

            depot_stock_after = int(
                depot_stock_row["quantity"]
            )

            depot_stock_before = (
                depot_stock_after - quantity
            )

            # ---------------------------------------------
            # Record depot stock IN movement
            # ---------------------------------------------

            cursor.execute("""
                INSERT INTO depot_stock_movements (
                    depotId,
                    articleId,
                    movementType,
                    quantity,
                    stockBefore,
                    stockAfter,
                    referenceType,
                    referenceId,
                    createdBy,
                    createdAt
                )
                VALUES (
                    ?, ?, 'IN', ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                depot_id,
                article_id,
                quantity,
                depot_stock_before,
                depot_stock_after,
                "purchase_receipt",
                receipt_id,
                user_id,
                now
            ))

            # ---------------------------------------------
            # Record legacy stock IN movement
            # ---------------------------------------------

            cursor.execute("""
                INSERT INTO stock_movements (
                    articleId,
                    lotId,
                    movementType,
                    quantity,
                    referenceType,
                    referenceId,
                    stockBefore,
                    stockAfter,
                    createdBy,
                    createdAt,
                    depotId
                )
                VALUES (?, ?, 'IN', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                article_id,
                lot_id,
                quantity,
                "purchase_receipt",
                receipt_id,
                stock_before,
                stock_after,
                user_id,
                now,
                depot_id
            ))
            # ---------------------------------------------
            # Increase received quantity
            # ---------------------------------------------

            cursor.execute("""
                UPDATE purchase_order_items
                SET
                    receivedQuantity =
                        receivedQuantity + ?,
                    updatedAt = ?
                WHERE purchaseOrderItemId = ?
                  AND purchaseOrderId = ?
                  AND receivedQuantity + ? <= orderedQuantity
            """, (
                quantity,
                now,
                item_id,
                purchase_order_id,
                quantity
            ))

            if cursor.rowcount != 1:

                raise ValueError(
                    f"Purchase order item {item_id} "
                    f"could not be updated."
                )

        # -------------------------------------------------
        # Determine final PO status
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                orderedQuantity,
                receivedQuantity
            FROM purchase_order_items
            WHERE purchaseOrderId = ?
        """, (purchase_order_id,))

        final_items = cursor.fetchall()

        all_received = all(
            row["receivedQuantity"]
            >= row["orderedQuantity"]
            for row in final_items
        )

        any_received = any(
            row["receivedQuantity"] > 0
            for row in final_items
        )

        if all_received:

            new_status = "received"

        elif any_received:

            new_status = "partially_received"

        else:

            new_status = "ordered"
        # -------------------------------------------------
        # Update purchase order status
        # -------------------------------------------------

        cursor.execute("""
            UPDATE purchase_orders
            SET
                status = ?,
                updatedAt = ?
            WHERE purchaseOrderId = ?
        """, (
            new_status,
            now,
            purchase_order_id
        ))

        # -------------------------------------------------
        # Commit everything atomically
        # -------------------------------------------------

        conn.commit()

        return redirect(
            url_for(
                "view_purchase_order",
                purchase_order_id=purchase_order_id
            )
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to receive purchase order."
        }), 400

    except Exception:
        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to receive purchase order."
        }), 500

    finally:
        conn.close()



# =========================================================
# ADMIN - PROMOTION MANAGEMENT
# =========================================================

@app.route("/admin/promotions")
def admin_promotions():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole, userActive
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if (
            not current_user
            or current_user["userRole"] != 1
            or not current_user["userActive"]
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Get promotions
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                p.promotionId,
                p.promotionName,
                p.promotionDescription,
                p.promotionType,
                p.discountValue,
                p.startDate,
                p.endDate,
                p.promotionActive,
                p.createdAt,

                COUNT(pa.promotionArticleId)
                    AS articleCount,

                COALESCE(pt.usageCount, 0)
                    AS usageCount,

                COALESCE(pt.usageQuantity, 0)
                    AS usageQuantity,

                COALESCE(pt.totalDiscount, 0)
                    AS totalDiscount,

                pt.lastUsedAt

            FROM promotions p

            LEFT JOIN promotion_articles pa
                ON pa.promotionId = p.promotionId

            LEFT JOIN (
                SELECT
                    promotionId,
                    COUNT(*) AS usageCount,
                    SUM(quantity) AS usageQuantity,
                    SUM(discountAmount) AS totalDiscount,
                    MAX(createdAt) AS lastUsedAt
                FROM promotion_transactions
                GROUP BY promotionId
            ) pt
                ON pt.promotionId = p.promotionId

            GROUP BY
                p.promotionId,
                p.promotionName,
                p.promotionDescription,
                p.promotionType,
                p.discountValue,
                p.startDate,
                p.endDate,
                p.promotionActive,
                p.createdAt

            ORDER BY
                p.startDate DESC,
                p.promotionId DESC
        """)

        promotions = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Get active articles
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                a.articleId,
                p.productName,
                a.articleVolume,
                a.articlePackage,
                a.articlePrice

            FROM Articles a

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE a.articleState = 1

            ORDER BY
                p.productName,
                a.articleVolume
        """)

        articles = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Get articles assigned to each promotion
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                pa.promotionId,
                pa.articleId,
                p.productName,
                a.articleVolume,
                a.articlePackage

            FROM promotion_articles pa

            JOIN Articles a
                ON a.articleId = pa.articleId

            JOIN products p
                ON p.productId = a.articleProductId

            ORDER BY
                p.productName,
                a.articleVolume
        """)

        promotion_articles = {}

        for row in cursor.fetchall():

            promotion_id = row["promotionId"]

            promotion_articles.setdefault(
                promotion_id,
                []
            ).append(dict(row))

        return render_template(
            "admin_promotions.html",
            promotions=promotions,
            articles=articles,
            promotion_articles=promotion_articles
        )

    finally:
        conn.close()


# =========================================================
# ADMIN - CREATE PROMOTION
# =========================================================

@app.route(
    "/admin/promotions/create",
    methods=["POST"]
)
def create_promotion():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole, userActive
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if (
            not current_user
            or current_user["userRole"] != 1
            or not current_user["userActive"]
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Read form
        # -------------------------------------------------

        promotion_name = request.form.get(
            "promotionName",
            ""
        ).strip()

        promotion_description = request.form.get(
            "promotionDescription",
            ""
        ).strip()

        promotion_type = request.form.get(
            "promotionType",
            ""
        ).strip().lower()

        discount_value = request.form.get(
            "discountValue",
            ""
        ).strip()

        start_date = request.form.get(
            "startDate",
            ""
        ).strip()

        end_date = request.form.get(
            "endDate",
            ""
        ).strip()

        article_ids = request.form.getlist(
            "articleIds"
        )

        # -------------------------------------------------
        # Validate basic data
        # -------------------------------------------------

        if not promotion_name:
            return redirect(
                url_for("admin_promotions")
            )

        if promotion_type not in (
            "percentage",
            "fixed"
        ):
            return redirect(
                url_for("admin_promotions")
            )

        try:
            discount_value = float(
                discount_value
            )
        except (ValueError, TypeError):

            return redirect(
                url_for("admin_promotions")
            )

        if discount_value <= 0:
            return redirect(
                url_for("admin_promotions")
            )

        if promotion_type == "percentage":
            if discount_value > 100:
                return redirect(
                    url_for("admin_promotions")
                )

        if not start_date or not end_date:
            return redirect(
                url_for("admin_promotions")
            )

        if end_date < start_date:
            return redirect(
                url_for("admin_promotions")
            )

        if not article_ids:
            return redirect(
                url_for("admin_promotions")
            )

        # -------------------------------------------------
        # Validate articles
        # -------------------------------------------------

        valid_article_ids = []

        for article_id in article_ids:

            try:
                article_id = int(article_id)
            except (ValueError, TypeError):
                continue

            cursor.execute("""
                SELECT articleId
                FROM Articles
                WHERE articleId = ?
                  AND articleState = 1
            """, (article_id,))

            if cursor.fetchone():
                valid_article_ids.append(
                    article_id
                )

        valid_article_ids = list(
            dict.fromkeys(valid_article_ids)
        )

        if not valid_article_ids:
            return redirect(
                url_for("admin_promotions")
            )

        # -------------------------------------------------
        # Create promotion
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO promotions (
                promotionName,
                promotionDescription,
                promotionType,
                discountValue,
                startDate,
                endDate,
                promotionActive,
                createdBy,
                createdAt,
                updatedAt
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """, (
            promotion_name,
            promotion_description,
            promotion_type,
            discount_value,
            start_date,
            end_date,
            user_id,
            now,
            now
        ))

        promotion_id = cursor.lastrowid

        # -------------------------------------------------
        # Assign articles
        # -------------------------------------------------

        for article_id in valid_article_ids:

            cursor.execute("""
                INSERT INTO promotion_articles (
                    promotionId,
                    articleId,
                    createdAt
                )
                VALUES (?, ?, ?)
            """, (
                promotion_id,
                article_id,
                now
            ))

        conn.commit()

        return redirect(
            url_for("admin_promotions")
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return redirect(
            url_for("admin_promotions")
        )

    except Exception as e:

        conn.rollback()

        print(
            "Create promotion error:",
            repr(e)
        )

        return jsonify({
            "success": False,
            "error": "Unable to create promotion."
        }), 500

    finally:
        conn.close()


# =========================================================
# ADMIN - PROMOTION USAGE HISTORY
# =========================================================

@app.route(
    "/admin/promotions/history"
)
def admin_promotion_history():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT
                userId,
                userRole,
                userActive
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if (
            not current_user
            or current_user["userRole"] != 1
            or not current_user["userActive"]
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # History filters
        # -------------------------------------------------

        promotion_filter = request.args.get(
            "promotionId",
            ""
        ).strip()

        customer_filter = request.args.get(
            "customerId",
            ""
        ).strip()

        order_filter = request.args.get(
            "orderNumber",
            ""
        ).strip()

        date_from = request.args.get(
            "dateFrom",
            ""
        ).strip()

        date_to = request.args.get(
            "dateTo",
            ""
        ).strip()

        # -------------------------------------------------
        # Build history query
        # -------------------------------------------------

        history_query = """
            SELECT
                pt.promotionTransactionId,
                pt.promotionId,
                pt.commandId,
                pt.articleId,
                pt.quantity,
                pt.discountAmount,
                pt.createdAt,

                pr.promotionName,
                pr.promotionType,
                pr.discountValue,

                c.cmdNum,
                c.cmdDate,
                c.cmdTotal,

                u.userId AS customerId,
                u.userName,
                u.userSurname,

                a.articleVolume,
                a.articlePackage,

                p.productName

            FROM promotion_transactions pt

            JOIN promotions pr
                ON pr.promotionId = pt.promotionId

            JOIN Commandes c
                ON c.cmdId = pt.commandId

            JOIN users u
                ON u.userId = c.cmdClientId

            JOIN Articles a
                ON a.articleId = pt.articleId

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE 1 = 1
        """

        history_params = []

        if promotion_filter:

            try:
                promotion_id = int(
                    promotion_filter
                )

                history_query += """
                    AND pt.promotionId = ?
                """

                history_params.append(
                    promotion_id
                )

            except ValueError:

                promotion_filter = ""

        if customer_filter:

            try:
                customer_id = int(
                    customer_filter
                )

                history_query += """
                    AND c.cmdClientId = ?
                """

                history_params.append(
                    customer_id
                )

            except ValueError:

                customer_filter = ""

        if order_filter:

            history_query += """
                AND c.cmdNum LIKE ?
            """

            history_params.append(
                "%" + order_filter + "%"
            )

        if date_from:

            history_query += """
                AND date(pt.createdAt) >= date(?)
            """

            history_params.append(
                date_from
            )

        if date_to:

            history_query += """
                AND date(pt.createdAt) <= date(?)
            """

            history_params.append(
                date_to
            )

        history_query += """
            ORDER BY
                pt.createdAt DESC,
                pt.promotionTransactionId DESC
        """

        cursor.execute(
            history_query,
            history_params
        )

        history = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Filter dropdown data
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                promotionId,
                promotionName
            FROM promotions
            ORDER BY promotionName
        """)

        history_promotions = [
            dict(row)
            for row in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT DISTINCT
                u.userId,
                u.userName,
                u.userSurname
            FROM users u

            JOIN Commandes c
                ON c.cmdClientId = u.userId

            JOIN promotion_transactions pt
                ON pt.commandId = c.cmdId

            ORDER BY
                u.userName,
                u.userSurname
        """)

        history_customers = [
            dict(row)
            for row in cursor.fetchall()
        ]

        return render_template(
            "admin_promotion_history.html",
            history=history,
            history_promotions=history_promotions,
            history_customers=history_customers,
            promotion_filter=promotion_filter,
            customer_filter=customer_filter,
            order_filter=order_filter,
            date_from=date_from,
            date_to=date_to
        )

    finally:
        conn.close()


# =========================================================
# ADMIN - TOGGLE PROMOTION
# =========================================================


@app.route(
    "/admin/promotions/<int:promotion_id>/toggle",
    methods=["POST"]
)
def toggle_promotion(promotion_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT userId, userRole, userActive
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if (
            not current_user
            or current_user["userRole"] != 1
            or not current_user["userActive"]
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        cursor.execute("""
            SELECT promotionActive
            FROM promotions
            WHERE promotionId = ?
        """, (promotion_id,))

        promotion = cursor.fetchone()

        if not promotion:
            return redirect(
                url_for("admin_promotions")
            )

        new_status = (
            0
            if promotion["promotionActive"]
            else 1
        )

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            UPDATE promotions

            SET
                promotionActive = ?,
                updatedAt = ?

            WHERE promotionId = ?
        """, (
            new_status,
            now,
            promotion_id
        ))

        conn.commit()

        return redirect(
            url_for("admin_promotions")
        )

    finally:
        conn.close()


# =========================================================
# ADMIN - DELETE PROMOTION
# =========================================================

@app.route(
    "/admin/promotions/<int:promotion_id>/delete",
    methods=["POST"]
)
def delete_promotion(promotion_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT userId, userRole, userActive
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if (
            not current_user
            or current_user["userRole"] != 1
            or not current_user["userActive"]
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Do not delete a promotion that has sales history
        # -------------------------------------------------

        cursor.execute("""
            SELECT promotionTransactionId
            FROM promotion_transactions
            WHERE promotionId = ?
            LIMIT 1
        """, (promotion_id,))

        if cursor.fetchone():

            return redirect(
                url_for("admin_promotions")
            )

        cursor.execute("""
            DELETE FROM promotions
            WHERE promotionId = ?
        """, (promotion_id,))

        conn.commit()

        return redirect(
            url_for("admin_promotions")
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return redirect(
            url_for("admin_promotions")
        )

    finally:
        conn.close()



# =========================================================
# ADMIN - SUPPLIER MANAGEMENT
# =========================================================

@app.route("/admin/suppliers")
def admin_suppliers():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        cursor.execute("""
            SELECT
                supplierId,
                supplierName,
                supplierContact,
                supplierPhone,
                supplierEmail,
                supplierAddress,
                supplierNotes,
                supplierActive,
                createdAt,
                updatedAt
            FROM suppliers
            ORDER BY supplierName
        """)

        suppliers = [
            dict(row)
            for row in cursor.fetchall()
        ]

        return render_template(
            "admin_suppliers.html",
            suppliers=suppliers
        )

    finally:
        conn.close()


# =========================================================
# ADMIN - CREATE SUPPLIER
# =========================================================

@app.route(
    "/admin/suppliers/create",
    methods=["POST"]
)
def create_supplier():
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        supplier_name = request.form.get(
            "supplierName",
            ""
        ).strip()

        supplier_contact = request.form.get(
            "supplierContact",
            ""
        ).strip()

        supplier_phone = request.form.get(
            "supplierPhone",
            ""
        ).strip()

        supplier_email = request.form.get(
            "supplierEmail",
            ""
        ).strip().lower()

        supplier_address = request.form.get(
            "supplierAddress",
            ""
        ).strip()

        supplier_notes = request.form.get(
            "supplierNotes",
            ""
        ).strip()

        if not supplier_name:
            return redirect(url_for("admin_suppliers"))

        cursor.execute("""
            SELECT supplierId
            FROM suppliers
            WHERE supplierName = ?
        """, (supplier_name,))

        if cursor.fetchone():
            return redirect(url_for("admin_suppliers"))

        now = datetime.now().isoformat(timespec="seconds")

        cursor.execute("""
            INSERT INTO suppliers (
                supplierName,
                supplierContact,
                supplierPhone,
                supplierEmail,
                supplierAddress,
                supplierNotes,
                supplierActive,
                createdAt,
                updatedAt
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """, (
            supplier_name,
            supplier_contact,
            supplier_phone,
            supplier_email,
            supplier_address,
            supplier_notes,
            now,
            now
        ))

        conn.commit()

        return redirect(url_for("admin_suppliers"))

    except sqlite3.IntegrityError:

        conn.rollback()

        return redirect(url_for("admin_suppliers"))

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to create supplier."
        }), 500

    finally:
        conn.close()

# =========================================================
# ADMIN - EDIT SUPPLIER
# =========================================================

@app.route(
    "/admin/suppliers/<int:supplier_id>/edit",
    methods=["POST"]
)
def edit_supplier(supplier_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Get form data
        # -------------------------------------------------

        supplier_name = request.form.get(
            "supplierName",
            ""
        ).strip()

        supplier_contact = request.form.get(
            "supplierContact",
            ""
        ).strip()

        supplier_phone = request.form.get(
            "supplierPhone",
            ""
        ).strip()

        supplier_email = request.form.get(
            "supplierEmail",
            ""
        ).strip().lower()

        supplier_address = request.form.get(
            "supplierAddress",
            ""
        ).strip()

        supplier_notes = request.form.get(
            "supplierNotes",
            ""
        ).strip()

        if not supplier_name:
            return redirect(url_for("admin_suppliers"))

        # -------------------------------------------------
        # Verify supplier exists
        # -------------------------------------------------

        cursor.execute("""
            SELECT supplierId
            FROM suppliers
            WHERE supplierId = ?
        """, (supplier_id,))

        supplier = cursor.fetchone()

        if not supplier:
            return jsonify({
                "success": False,
                "error": "Supplier not found."
            }), 404

        # -------------------------------------------------
        # Prevent duplicate supplier name
        # -------------------------------------------------

        cursor.execute("""
            SELECT supplierId
            FROM suppliers
            WHERE supplierName = ?
              AND supplierId != ?
        """, (
            supplier_name,
            supplier_id
        ))

        if cursor.fetchone():
            return redirect(url_for("admin_suppliers"))

        # -------------------------------------------------
        # Update supplier
        # -------------------------------------------------

        now = datetime.now().isoformat(timespec="seconds")

        cursor.execute("""
            UPDATE suppliers
            SET
                supplierName = ?,
                supplierContact = ?,
                supplierPhone = ?,
                supplierEmail = ?,
                supplierAddress = ?,
                supplierNotes = ?,
                updatedAt = ?
            WHERE supplierId = ?
        """, (
            supplier_name,
            supplier_contact,
            supplier_phone,
            supplier_email,
            supplier_address,
            supplier_notes,
            now,
            supplier_id
        ))

        conn.commit()

        return redirect(
            url_for("admin_suppliers")
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return redirect(
            url_for("admin_suppliers")
        )

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to update supplier."
        }), 500

    finally:
        conn.close()

# =========================================================
# ADMIN - TOGGLE SUPPLIER ACTIVE STATUS
# =========================================================

@app.route(
    "/admin/suppliers/<int:supplier_id>/toggle",
    methods=["POST"]
)
def toggle_supplier(supplier_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        cursor.execute("""
            SELECT
                supplierId,
                supplierActive
            FROM suppliers
            WHERE supplierId = ?
        """, (supplier_id,))

        supplier = cursor.fetchone()

        if not supplier:
            return jsonify({
                "success": False,
                "error": "Supplier not found."
            }), 404

        new_status = (
            0
            if supplier["supplierActive"]
            else 1
        )

        now = datetime.now().isoformat(timespec="seconds")

        cursor.execute("""
            UPDATE suppliers
            SET
                supplierActive = ?,
                updatedAt = ?
            WHERE supplierId = ?
        """, (
            new_status,
            now,
            supplier_id
        ))

        conn.commit()

        return redirect(url_for("admin_suppliers"))

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to update supplier."
        }), 500

    finally:
        conn.close()

# =========================================================
# ADMIN - SUPPLIER ARTICLES
# =========================================================

@app.route(
    "/admin/suppliers/<int:supplier_id>/articles"
)
def supplier_articles(supplier_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Get supplier
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                supplierId,
                supplierName,
                supplierActive
            FROM suppliers
            WHERE supplierId = ?
        """, (supplier_id,))

        supplier = cursor.fetchone()

        if not supplier:
            return jsonify({
                "success": False,
                "error": "Supplier not found."
            }), 404

        # -------------------------------------------------
        # Get assigned articles
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                sa.supplierArticleId,
                sa.supplierId,
                sa.articleId,
                sa.supplierReference,
                sa.purchasePrice,
                sa.minimumOrderQuantity,
                sa.leadTimeDays,
                sa.isPreferred,
                sa.supplierArticleActive,

                a.articleVolume,
                a.articlePackage,
                a.articleStock,
                a.articlePrice,

                p.productName

            FROM supplier_articles sa

            JOIN Articles a
                ON a.articleId = sa.articleId

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE sa.supplierId = ?

            ORDER BY
                p.productName,
                a.articleVolume
        """, (supplier_id,))

        assigned_articles = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Get available articles
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                a.articleId,
                a.articleVolume,
                a.articlePackage,
                a.articleStock,
                a.articlePrice,
                p.productName

            FROM Articles a

            JOIN products p
                ON p.productId = a.articleProductId

            WHERE a.articleState = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM supplier_articles sa
                  WHERE sa.supplierId = ?
                    AND sa.articleId = a.articleId
              )

            ORDER BY
                p.productName,
                a.articleVolume
        """, (supplier_id,))

        available_articles = [
            dict(row)
            for row in cursor.fetchall()
        ]

        return render_template(
            "supplier_articles.html",
            supplier=dict(supplier),
            assigned_articles=assigned_articles,
            available_articles=available_articles
        )

    finally:
        conn.close()


# =========================================================
# ADMIN - ADD SUPPLIER ARTICLE
# =========================================================

@app.route(
    "/admin/suppliers/<int:supplier_id>/articles/create",
    methods=["POST"]
)
def create_supplier_article(supplier_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Verify supplier
        # -------------------------------------------------

        cursor.execute("""
            SELECT supplierId
            FROM suppliers
            WHERE supplierId = ?
        """, (supplier_id,))

        if not cursor.fetchone():
            return jsonify({
                "success": False,
                "error": "Supplier not found."
            }), 404

        # -------------------------------------------------
        # Form data
        # -------------------------------------------------

        article_id = request.form.get(
            "articleId",
            ""
        ).strip()

        supplier_reference = request.form.get(
            "supplierReference",
            ""
        ).strip()

        purchase_price = request.form.get(
            "purchasePrice",
            ""
        ).strip()

        minimum_order_quantity = request.form.get(
            "minimumOrderQuantity",
            "1"
        ).strip()

        lead_time_days = request.form.get(
            "leadTimeDays",
            ""
        ).strip()

        is_preferred = (
            1
            if request.form.get("isPreferred")
            else 0
        )

        # -------------------------------------------------
        # Basic validation
        # -------------------------------------------------

        if not article_id or not purchase_price:
            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        try:
            article_id = int(article_id)
            purchase_price = float(purchase_price)
            minimum_order_quantity = int(
                minimum_order_quantity or 1
            )

            lead_time_days = (
                int(lead_time_days)
                if lead_time_days
                else None
            )

        except (ValueError, TypeError):

            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        if purchase_price < 0:
            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        if minimum_order_quantity < 1:
            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        if lead_time_days is not None and lead_time_days < 0:
            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        # -------------------------------------------------
        # Verify article
        # -------------------------------------------------

        cursor.execute("""
            SELECT articleId
            FROM Articles
            WHERE articleId = ?
              AND articleState = 1
        """, (article_id,))

        if not cursor.fetchone():
            return jsonify({
                "success": False,
                "error": "Article not found."
            }), 404

        # -------------------------------------------------
        # Prevent duplicate relationship
        # -------------------------------------------------

        cursor.execute("""
            SELECT supplierArticleId
            FROM supplier_articles
            WHERE supplierId = ?
              AND articleId = ?
        """, (
            supplier_id,
            article_id
        ))

        if cursor.fetchone():
            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        # -------------------------------------------------
        # If preferred, remove preferred flag from
        # other articles for this supplier
        # -------------------------------------------------

        if is_preferred:

            cursor.execute("""
                UPDATE supplier_articles
                SET
                    isPreferred = 0,
                    updatedAt = ?
                WHERE supplierId = ?
            """, (
                datetime.now().isoformat(
                    timespec="seconds"
                ),
                supplier_id
            ))

        # -------------------------------------------------
        # Insert
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO supplier_articles (
                supplierId,
                articleId,
                supplierReference,
                purchasePrice,
                minimumOrderQuantity,
                leadTimeDays,
                isPreferred,
                supplierArticleActive,
                createdAt,
                updatedAt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """, (
            supplier_id,
            article_id,
            supplier_reference,
            purchase_price,
            minimum_order_quantity,
            lead_time_days,
            is_preferred,
            now,
            now
        ))

        conn.commit()

        return redirect(
            url_for(
                "supplier_articles",
                supplier_id=supplier_id
            )
        )

    except sqlite3.IntegrityError:

        conn.rollback()

        return redirect(
            url_for(
                "supplier_articles",
                supplier_id=supplier_id
            )
        )

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to add supplier article."
        }), 500

    finally:
        conn.close()


# =========================================================
# ADMIN - EDIT SUPPLIER ARTICLE
# =========================================================

@app.route(
    "/admin/suppliers/<int:supplier_id>/articles/<int:supplier_article_id>/edit",
    methods=["POST"]
)
def edit_supplier_article(
    supplier_id,
    supplier_article_id
):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Verify relationship
        # -------------------------------------------------

        cursor.execute("""
            SELECT supplierArticleId
            FROM supplier_articles
            WHERE supplierArticleId = ?
              AND supplierId = ?
        """, (
            supplier_article_id,
            supplier_id
        ))

        if not cursor.fetchone():
            return jsonify({
                "success": False,
                "error": "Supplier article not found."
            }), 404

        # -------------------------------------------------
        # Form data
        # -------------------------------------------------

        supplier_reference = request.form.get(
            "supplierReference",
            ""
        ).strip()

        purchase_price = request.form.get(
            "purchasePrice",
            ""
        ).strip()

        minimum_order_quantity = request.form.get(
            "minimumOrderQuantity",
            "1"
        ).strip()

        lead_time_days = request.form.get(
            "leadTimeDays",
            ""
        ).strip()

        is_preferred = (
            1
            if request.form.get("isPreferred")
            else 0
        )

        try:

            purchase_price = float(
                purchase_price
            )

            minimum_order_quantity = int(
                minimum_order_quantity or 1
            )

            lead_time_days = (
                int(lead_time_days)
                if lead_time_days
                else None
            )

        except (ValueError, TypeError):

            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        if purchase_price < 0:
            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        if minimum_order_quantity < 1:
            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        if lead_time_days is not None and lead_time_days < 0:
            return redirect(
                url_for(
                    "supplier_articles",
                    supplier_id=supplier_id
                )
            )

        # -------------------------------------------------
        # Preferred supplier article
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        if is_preferred:

            cursor.execute("""
                UPDATE supplier_articles
                SET
                    isPreferred = 0,
                    updatedAt = ?
                WHERE supplierId = ?
            """, (
                now,
                supplier_id
            ))

        # -------------------------------------------------
        # Update
        # -------------------------------------------------

        cursor.execute("""
            UPDATE supplier_articles
            SET
                supplierReference = ?,
                purchasePrice = ?,
                minimumOrderQuantity = ?,
                leadTimeDays = ?,
                isPreferred = ?,
                updatedAt = ?
            WHERE supplierArticleId = ?
              AND supplierId = ?
        """, (
            supplier_reference,
            purchase_price,
            minimum_order_quantity,
            lead_time_days,
            is_preferred,
            now,
            supplier_article_id,
            supplier_id
        ))

        conn.commit()

        return redirect(
            url_for(
                "supplier_articles",
                supplier_id=supplier_id
            )
        )

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to update supplier article."
        }), 500

    finally:
        conn.close()


# =========================================================
# ADMIN - TOGGLE SUPPLIER ARTICLE
# =========================================================

@app.route(
    "/admin/suppliers/<int:supplier_id>/articles/<int:supplier_article_id>/toggle",
    methods=["POST"]
)
def toggle_supplier_article(
    supplier_id,
    supplier_article_id
):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT userId, userRole
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if not current_user or current_user["userRole"] != 1:
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Get relationship
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                supplierArticleId,
                supplierArticleActive
            FROM supplier_articles
            WHERE supplierArticleId = ?
              AND supplierId = ?
        """, (
            supplier_article_id,
            supplier_id
        ))

        supplier_article = cursor.fetchone()

        if not supplier_article:
            return jsonify({
                "success": False,
                "error": "Supplier article not found."
            }), 404

        new_status = (
            0
            if supplier_article["supplierArticleActive"]
            else 1
        )

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            UPDATE supplier_articles
            SET
                supplierArticleActive = ?,
                updatedAt = ?
            WHERE supplierArticleId = ?
              AND supplierId = ?
        """, (
            new_status,
            now,
            supplier_article_id,
            supplier_id
        ))

        conn.commit()

        return redirect(
            url_for(
                "supplier_articles",
                supplier_id=supplier_id
            )
        )

    except Exception:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": "Unable to update supplier article."
        }), 500

    finally:
        conn.close()






#LOGIN INFORMATION

# =========================================================
# LOGIN
# =========================================================

@app.route("/getlogin", methods=["POST"])
def getlogin():

    email = request.form.get(
        "email",
        ""
    ).strip().lower()

    password = request.form.get(
        "password",
        ""
    )

    # ---------------------------------------------------------
    # Validate input
    # ---------------------------------------------------------

    if not email or not password:

        return render_template(
            "/auth/login.html",
            error="Email and password are required."
        )

    connexion = get_connexion()
    curseur = connexion.cursor()

    # ---------------------------------------------------------
    # Find user
    # ---------------------------------------------------------

    curseur.execute("""
        SELECT *
        FROM users
        WHERE userMail = ?
    """, (email,))

    resultat = curseur.fetchone()

    if not resultat:

        connexion.close()

        return render_template(
            "/auth/login.html",
            error="Incorrect email or password."
        )

    # ---------------------------------------------------------
    # Check account active status
    #
    # Existing accounts are considered active by default.
    # ---------------------------------------------------------

    user_active = resultat["userActive"]

    if user_active is None:
        user_active = 1

    if int(user_active) != 1:

        connexion.close()

        return render_template(
            "/auth/login.html",
            error="Your account has been deactivated. Please contact the administrator."
        )

    # ---------------------------------------------------------
    # Stored password
    # ---------------------------------------------------------

    stored_password = resultat["userPsw"]

    password_valid = False
    old_plain_password = False

    # ---------------------------------------------------------
    # Try secure hashed password
    # ---------------------------------------------------------

    try:

        password_valid = check_password_hash(
            stored_password,
            password
        )

    except (ValueError, TypeError):

        password_valid = False

    # ---------------------------------------------------------
    # Backward compatibility
    #
    # Existing accounts may still contain plain-text passwords.
    # ---------------------------------------------------------

    if (
        not password_valid
        and stored_password == password
    ):

        password_valid = True
        old_plain_password = True

    # ---------------------------------------------------------
    # Invalid password
    # ---------------------------------------------------------

    if not password_valid:

        connexion.close()

        return render_template(
            "/auth/login.html",
            error="Incorrect email or password."
        )

    # ---------------------------------------------------------
    # Automatically migrate old plain-text password
    # ---------------------------------------------------------

    if old_plain_password:

        new_password_hash = generate_password_hash(
            password
        )

        curseur.execute("""
            UPDATE users
            SET userPsw = ?
            WHERE userId = ?
        """, (
            new_password_hash,
            resultat["userId"]
        ))

        connexion.commit()

    connexion.close()

    # ---------------------------------------------------------
# Create session
# ---------------------------------------------------------

    session["user_id"] = resultat["userId"]

# ---------------------------------------------------------
# Redirect according to user role
# ---------------------------------------------------------

    user_role = int(resultat["userRole"])

    if user_role == 1:
    # Administrator
        return redirect(
        url_for("dashboard")
    )

    elif user_role == 3:
    # Livreur
        return redirect(
        url_for("livreur_dashboard")
    )

    else:
    # Customer
        return redirect(
        url_for("account")
    )


# =========================================================
# CREDIT MANAGEMENT HELPERS
# =========================================================

def get_credit_customer_for_user(user_id, cursor):
    """
    Return the credit configuration for a customer.

    credit_customers is the authoritative credit-account
    configuration table.
    """
    cursor.execute("""
        SELECT
            creditCustomerId,
            userId,
            customerType,
            creditEnabled,
            creditLimit,
            paymentTermsDays,
            creditStatus,
            notes,
            createdAt,
            updatedAt
        FROM credit_customers
        WHERE userId = ?
        LIMIT 1
    """, (user_id,))

    return cursor.fetchone()


def get_customer_credit_balance(user_id, cursor):
    """
    Return the authoritative customer credit balance.

    The credit_customers table stores the credit configuration.
    The credit_transactions table stores the credit ledger.

    Repayments are matched chronologically against credit purchases.
    A repayment can reduce debt that already exists, but an excess
    repayment never becomes a negative balance or future credit.
    """

    credit_customer = get_credit_customer_for_user(
        user_id,
        cursor
    )

    if not credit_customer:
        return {
            "credit_limit": 0.0,
            "balance": 0.0,
            "available_credit": 0.0,
            "status": "inactive",
            "credit_enabled": 0,
            "credit_customer": None
        }

    # ---------------------------------------------------------
    # Calculate outstanding debt chronologically.
    #
    # Historical repayments that occurred while there was no
    # recorded debt are preserved in the ledger but do not
    # create a negative balance or offset future purchases.
    # ---------------------------------------------------------
    cursor.execute("""
        SELECT
            creditTransactionId,
            transactionType,
            amount,
            createdAt
        FROM credit_transactions
        WHERE creditCustomerId = ?
          AND transactionType IN ('CREDIT', 'REPAYMENT')
        ORDER BY
            createdAt ASC,
            creditTransactionId ASC
    """, (
        credit_customer["creditCustomerId"],
    ))

    outstanding_balance = 0.0

    for transaction in cursor.fetchall():
        transaction_type = str(
            transaction["transactionType"] or ""
        ).upper()

        try:
            amount = float(transaction["amount"] or 0)
        except (TypeError, ValueError):
            amount = 0.0

        if amount <= 0:
            continue

        if transaction_type == "CREDIT":
            outstanding_balance += amount

        elif transaction_type == "REPAYMENT":
            outstanding_balance = max(
                0.0,
                outstanding_balance - amount
            )

    credit_limit = float(
        credit_customer["creditLimit"] or 0
    )

    balance = max(
        0.0,
        outstanding_balance
    )

    available_credit = max(
        0.0,
        credit_limit - balance
    )

    return {
        "credit_limit": credit_limit,
        "balance": balance,
        "available_credit": available_credit,
        "status": str(
            credit_customer["creditStatus"] or "inactive"
        ),
        "credit_enabled": int(
            credit_customer["creditEnabled"] or 0
        ),
        "credit_customer": credit_customer
    }


def validate_customer_credit(user_id, order_total, cursor):
    """
    Validate whether a customer can place an order on credit.

    The authoritative credit limit comes from credit_customers
    and the outstanding balance comes from credit_transactions.

    Returns:
        (True, None, credit_customer, balance_info)
    or:
        (False, error_message, None, None)
    """

    try:
        order_total = float(order_total)
    except (TypeError, ValueError):
        return (
            False,
            "Invalid credit order amount.",
            None,
            None
        )

    if order_total <= 0:
        return (
            False,
            "Credit order amount must be greater than zero.",
            None,
            None
        )

    credit_customer = get_credit_customer_for_user(
        user_id,
        cursor
    )

    if not credit_customer:
        return (
            False,
            "This customer is not authorized to purchase on credit.",
            None,
            None
        )

    if int(credit_customer["creditEnabled"] or 0) != 1:
        return (
            False,
            "Credit purchasing is disabled for this customer.",
            None,
            None
        )

    if str(
        credit_customer["creditStatus"] or ""
    ).lower() != "active":
        return (
            False,
            "This customer's credit account is not active.",
            None,
            None
        )

    balance_info = get_customer_credit_balance(
        user_id,
        cursor
    )

    available_credit = float(
        balance_info["available_credit"]
    )

    if order_total > available_credit:
        return (
            False,
            (
                "Credit limit exceeded. "
                f"Available credit: {available_credit:,.0f} XAF."
            ),
            None,
            None
        )

    return (
        True,
        None,
        credit_customer,
        balance_info
    )


def create_credit_transaction(
    user_id,
    command_id,
    amount,
    cursor,
    credit_customer=None
):
    """
    Create a CREDIT ledger transaction.

    The ledger is authoritative. No legacy credit-account
    balance is updated.
    """

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError(
            "Invalid credit transaction amount."
        )

    if amount <= 0:
        raise ValueError(
            "Credit transaction amount must be greater than zero."
        )

    if credit_customer is None:
        credit_customer = get_credit_customer_for_user(
            user_id,
            cursor
        )

    if not credit_customer:
        raise ValueError(
            "Credit customer record not found."
        )

    if int(credit_customer["creditEnabled"] or 0) != 1:
        raise ValueError(
            "Credit purchasing is disabled for this customer."
        )

    if str(
        credit_customer["creditStatus"] or ""
    ).lower() != "active":
        raise ValueError(
            "This customer's credit account is not active."
        )

    credit_customer_id = credit_customer[
        "creditCustomerId"
    ]

    payment_terms_days = int(
        credit_customer["paymentTermsDays"] or 0
    )

    due_date = (
        datetime.now()
        + timedelta(days=payment_terms_days)
    ).strftime("%Y-%m-%d")

    cursor.execute("""
        INSERT INTO credit_transactions (
            creditCustomerId,
            commandId,
            transactionType,
            amount,
            dueDate,
            description,
            createdAt
        )
        VALUES (?, ?, 'CREDIT', ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        credit_customer_id,
        command_id,
        amount,
        due_date,
        "Credit purchase"
    ))

    if cursor.rowcount != 1:
        raise ValueError(
            "Credit transaction could not be created."
        )

    return due_date


def create_credit_repayment(
    user_id,
    amount,
    payment_method,
    reference,
    notes,
    cursor
):
    """
    Record a repayment.

    The outstanding balance is calculated from the authoritative
    credit ledger. New payments are recorded in both
    credit_payments and credit_transactions.

    Existing historical transactions are preserved.
    """

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError(
            "Invalid repayment amount."
        )

    if amount <= 0:
        raise ValueError(
            "Repayment amount must be greater than zero."
        )

    credit_customer = get_credit_customer_for_user(
        user_id,
        cursor
    )

    if not credit_customer:
        raise ValueError(
            "Credit customer record not found."
        )

    credit_customer_id = credit_customer[
        "creditCustomerId"
    ]

    balance_info = get_customer_credit_balance(
        user_id,
        cursor
    )

    current_balance = float(
        balance_info["balance"]
    )

    if current_balance <= 0:
        raise ValueError(
            "This customer has no outstanding debt."
        )

    if amount > current_balance:
        amount = current_balance

    cursor.execute("""
        INSERT INTO credit_payments (
            creditCustomerId,
            amount,
            paymentDate,
            paymentMethod,
            reference,
            notes,
            createdAt
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        credit_customer_id,
        amount,
        datetime.now().strftime("%Y-%m-%d"),
        payment_method or "CASH",
        reference or None,
        notes or None
    ))

    if cursor.rowcount != 1:
        raise ValueError(
            "Credit payment could not be recorded."
        )

    cursor.execute("""
        INSERT INTO credit_transactions (
            creditCustomerId,
            commandId,
            transactionType,
            amount,
            dueDate,
            description,
            createdAt
        )
        VALUES (?, NULL, 'REPAYMENT', ?, NULL, ?, CURRENT_TIMESTAMP)
    """, (
        credit_customer_id,
        amount,
        "Customer credit repayment"
    ))

    if cursor.rowcount != 1:
        raise ValueError(
            "Credit repayment transaction could not be recorded."
        )

    return amount


def get_overdue_credit_customers(cursor):
    """
    Return active customers with outstanding debt and at least
    one credit transaction whose due date has passed.

    Outstanding debt is calculated from the authoritative ledger.
    """

    cursor.execute("""
        SELECT
            cc.creditCustomerId,
            cc.userId,
            u.userName,
            u.userSurname,
            u.userPhone,
            cc.creditLimit AS credit_limit,
            (
                SELECT COALESCE(
                    SUM(
                        CASE
                            WHEN ct2.transactionType = 'CREDIT'
                            THEN ct2.amount
                            WHEN ct2.transactionType = 'REPAYMENT'
                            THEN -ct2.amount
                            ELSE 0
                        END
                    ),
                    0
                )
                FROM credit_transactions ct2
                WHERE ct2.creditCustomerId =
                      cc.creditCustomerId
            ) AS balance,
            cc.creditStatus AS status,
            MIN(ct.dueDate) AS oldestDueDate,
            CAST(
                julianday(date('now'))
                - julianday(MIN(ct.dueDate))
                AS INTEGER
            ) AS daysOverdue
        FROM credit_customers cc
        JOIN Users u
            ON u.userId = cc.userId
        JOIN credit_transactions ct
            ON ct.creditCustomerId =
               cc.creditCustomerId
        WHERE cc.creditEnabled = 1
          AND cc.creditStatus = 'active'
          AND ct.transactionType = 'CREDIT'
          AND ct.dueDate IS NOT NULL
          AND date(ct.dueDate) < date('now')
        GROUP BY
            cc.creditCustomerId,
            cc.userId,
            u.userName,
            u.userSurname,
            u.userPhone,
            cc.creditLimit,
            cc.creditStatus
        HAVING (
            SELECT COALESCE(
                SUM(
                    CASE
                        WHEN ct2.transactionType = 'CREDIT'
                        THEN ct2.amount
                        WHEN ct2.transactionType = 'REPAYMENT'
                        THEN -ct2.amount
                        ELSE 0
                    END
                ),
                0
            )
            FROM credit_transactions ct2
            WHERE ct2.creditCustomerId =
                  cc.creditCustomerId
        ) > 0
        ORDER BY
            date(oldestDueDate) ASC
    """)

    return cursor.fetchall()




# =========================================================
# LIVREUR - DASHBOARD
# =========================================================

@app.route("/livreur/dashboard")
def livreur_dashboard():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -------------------------------------------------
        # Verify logged-in user is an ACTIVE Livreur
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                userId,
                userName,
                userSurname,
                userMail,
                userPhone,
                userActive,
                userRole
            FROM users
            WHERE userId = ?
              AND userRole = 3
        """, (user_id,))

        livreur = cursor.fetchone()

        if not livreur:

            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        if not livreur["userActive"]:

            session.pop("user_id", None)

            return render_template(
                "/auth/login.html",
                error="Your delivery account has been deactivated."
            )

        # -------------------------------------------------
        # Get orders assigned to this Livreur
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                c.cmdId,
                c.cmdNum,
                c.cmdStatut,
                c.cmdPayementMethod,
                c.cmdTotal,
                c.cmdLivAddress,
                c.cmdDate,
                c.cmdZoneId,
                c.cmdLivreurId,
                c.cmdLivreurConfirmed,
                c.cmdLivreurConfirmedAt,

                u.userName AS customerName,
                u.userSurname AS customerSurname,
                u.userPhone AS customerPhone,
                u.userMail AS customerMail,

                dz.zoneName,
                dz.deliveryFee,
                dz.estimatedMinutes

            FROM Commandes c

            JOIN users u
                ON u.userId = c.cmdClientId

            LEFT JOIN delivery_zones dz
                ON dz.zoneId = c.cmdZoneId

            WHERE c.cmdLivreurId = ?

            ORDER BY c.cmdId DESC
        """, (user_id,))

        orders = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Delivery timing
        # -------------------------------------------------

        for order in orders:

            timing = get_delivery_timing(
                order["cmdId"],
                cursor
            )

            order["preparation_seconds"] = timing["preparation_seconds"]
            order["delivery_seconds"] = timing["delivery_seconds"]
            order["total_seconds"] = timing["total_seconds"]

            order["preparation_duration"] = format_delivery_duration(
                timing["preparation_seconds"]
            )

            order["delivery_duration"] = format_delivery_duration(
                timing["delivery_seconds"]
            )

            order["total_duration"] = format_delivery_duration(
                timing["total_seconds"]
            )

        # -------------------------------------------------
        # Statistics
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                COUNT(*) AS total_orders,

                SUM(
                    CASE
                        WHEN cmdLivreurConfirmed = 0
                         AND cmdStatut IN (
                             'en_attente',
                             'en_preparation'
                         )
                        THEN 1
                        ELSE 0
                    END
                ) AS pending_confirmation_orders,

                SUM(
                    CASE
                        WHEN cmdStatut = 'en_attente'
                        THEN 1
                        ELSE 0
                    END
                ) AS pending_orders,

                SUM(
                    CASE
                        WHEN cmdStatut = 'en_preparation'
                        THEN 1
                        ELSE 0
                    END
                ) AS preparation_orders,

                SUM(
                    CASE
                        WHEN cmdStatut = 'en_livraison'
                        THEN 1
                        ELSE 0
                    END
                ) AS delivery_orders,

                SUM(
                    CASE
                        WHEN cmdStatut = 'livree'
                        THEN 1
                        ELSE 0
                    END
                ) AS completed_orders

            FROM Commandes

            WHERE cmdLivreurId = ?
        """, (user_id,))

        statistics = dict(cursor.fetchone())

        return render_template(
            "livreur_dashboard.html",
            livreur=dict(livreur),
            orders=orders,
            statistics=statistics
        )

    finally:

        conn.close()

@app.route("/dashboard")
def dashboard():
    # -----------------------------------------------------
    # DASHBOARD DATE
    # -----------------------------------------------------
    today = datetime.now()

    jours = [
        "lundi", "mardi", "mercredi", "jeudi",
        "vendredi", "samedi", "dimanche"
    ]

    mois = [
        "janvier", "\u00e9vrier", "mars", "avril", "mai", "juin",
        "juillet", "ao\u00fbt", "septembre", "octobre", "novembre", "d\u00e9cembre"
    ]

    dashboard_date = (
        f"{jours[today.weekday()]} "
        f"{today.day} "
        f"{mois[today.month - 1]} {today.year}"
    ).capitalize()

    connexion = get_connexion()
    curseur = connexion.cursor()

    try:
        # -----------------------------------------------------
        # ORDERS / SALES
        # -----------------------------------------------------

        curseur.execute("""
            SELECT
                COUNT(*) AS count,
                COALESCE(SUM(cmdTotal), 0) AS total
            FROM Commandes
            WHERE cmdStatut = 'livree'
        """)
        sales = curseur.fetchone()

        chiffre_affaires = sales["total"]
        commandes_livrees = sales["count"]

        curseur.execute("""
            SELECT COUNT(*)
            FROM Commandes
            WHERE cmdStatut = 'en_attente'
        """)
        commandes_en_attente = curseur.fetchone()[0]

        curseur.execute("""
            SELECT COUNT(*)
            FROM Commandes
            WHERE date(cmdDate) = date('now', 'localtime')
        """)
        commandes_aujourd_hui = curseur.fetchone()[0]

        curseur.execute("""
            SELECT
                COUNT(*) AS count,
                COALESCE(SUM(cmdTotal), 0) AS total
            FROM Commandes
            WHERE cmdStatut = 'livree'
              AND date(cmdDate) = date('now', 'localtime')
        """)
        ventes_aujourd_hui = curseur.fetchone()

        livraisons_aujourd_hui = ventes_aujourd_hui["count"]

        # -----------------------------------------------------
        # PRODUCTS / STOCK
        # -----------------------------------------------------

        curseur.execute("""
            SELECT COUNT(*)
            FROM articles
            WHERE articleState = 1
        """)
        produits = curseur.fetchone()[0]

        # Low stock threshold deliberately kept explicit.
        # This can be changed later according to the business rule.
        stock_faible_seuil = 10

        curseur.execute("""
            SELECT COUNT(*)
            FROM articles
            WHERE articleState = 1
              AND articleStock <= ?
        """, (stock_faible_seuil,))
        stock_faible = curseur.fetchone()[0]

        curseur.execute("""
            SELECT
                a.articleId,
                a.articleProductId,
                a.articleStock,
                a.articlePrice,
                a.articleVolume,
                a.articlePackage,
                a.articleImage,
                p.productName,
                p.productMark,
                p.productMainImage
            FROM articles a
            INNER JOIN products p
                ON p.productId = a.articleProductId
            WHERE a.articleState = 1
              AND p.productState = 1
              AND a.articleStock <= ?
            ORDER BY a.articleStock ASC, a.articleId ASC
            LIMIT 10
        """, (stock_faible_seuil,))
        stock_a_surveille = [dict(row) for row in curseur.fetchall()]

        # -----------------------------------------------------
        # CLIENTS
        # -----------------------------------------------------

        curseur.execute("""
            SELECT COUNT(*)
            FROM users
            WHERE userRole = 2
              AND userActive = 1
        """)
        clients = curseur.fetchone()[0]

        # -----------------------------------------------------
        # CREDIT
        # Outstanding credit = credit transactions - payments.
        # Only credit transactions representing amounts owed are
        # included. Payments reduce the outstanding balance.
        # -----------------------------------------------------

        curseur.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM credit_transactions
            WHERE transactionType = 'CREDIT'
        """)
        credit_total = curseur.fetchone()[0]

        curseur.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM credit_transactions
            WHERE transactionType = 'REPAYMENT'
        """)
        credit_remboursements = curseur.fetchone()[0]

        credit_a_recouvrer = max(0, credit_total - credit_remboursements)
        # -----------------------------------------------------
        # RECENT ORDERS
        # -----------------------------------------------------

        curseur.execute("""
            SELECT
                c.cmdId,
                c.cmdNum,
                c.cmdStatut,
                c.cmdTotal,
                c.cmdDate,
                c.cmdClientId,
                u.userName,
                u.userSurname
            FROM Commandes c
            LEFT JOIN users u
                ON u.userId = c.cmdClientId
            ORDER BY c.cmdId DESC
            LIMIT 10
        """)
        dernieres_commandes = [dict(row) for row in curseur.fetchall()]

        # -----------------------------------------------------
        # POPULAR PRODUCTS
        # Only delivered orders are counted as completed sales.
        # -----------------------------------------------------

        curseur.execute("""
            SELECT
                ac.artcmdArticleId AS articleId,
                a.articleProductId,
                p.productName,
                p.productMark,
                a.articleVolume,
                a.articlePackage,
                COALESCE(SUM(ac.artcmdQuantity), 0) AS quantity,
                COALESCE(SUM(ac.artcmdSubTotal), 0) AS total
            FROM articlesCommandes ac
            INNER JOIN Commandes c
                ON c.cmdId = ac.artcmdCmdId
            INNER JOIN articles a
                ON a.articleId = ac.artcmdArticleId
            INNER JOIN products p
                ON p.productId = a.articleProductId
            WHERE c.cmdStatut = 'livree'
              AND p.productState = 1
            GROUP BY
                ac.artcmdArticleId,
                a.articleProductId,
                p.productName,
                p.productMark,
                a.articleVolume,
                a.articlePackage
            ORDER BY quantity DESC, total DESC
            LIMIT 10
        """)
        produits_populaires = [dict(row) for row in curseur.fetchall()]

        # -----------------------------------------------------
        # SALES EVOLUTION - LAST 7 DAYS
        # -----------------------------------------------------

        curseur.execute("""
            WITH RECURSIVE dates(saleDate) AS (
                SELECT date('now', 'localtime', '-6 days')
                UNION ALL
                SELECT date(saleDate, '+1 day')
                FROM dates
                WHERE saleDate < date('now', 'localtime')
            )
            SELECT
                dates.saleDate AS saleDate,
                COUNT(c.cmdId) AS orderCount,
                COALESCE(SUM(c.cmdTotal), 0) AS total
            FROM dates
            LEFT JOIN Commandes c
                ON date(c.cmdDate) = dates.saleDate
                AND c.cmdStatut = 'livree'
            GROUP BY dates.saleDate
            ORDER BY dates.saleDate ASC
        """)
        evolution_ventes = [dict(row) for row in curseur.fetchall()]

        # -----------------------------------------------------
        # PURCHASE / SUPPLY STATUS
        # -----------------------------------------------------

        curseur.execute("""
            SELECT COUNT(*)
            FROM purchase_orders
            WHERE status NOT IN ('received', 'cancelled')
        """)
        approvisionnements_en_cours = curseur.fetchone()[0]

        curseur.execute("""
            SELECT COUNT(*)
            FROM purchase_receipts
            WHERE date(receivedDate) = date('now', 'localtime')
        """)
        receptions_aujourd_hui = curseur.fetchone()[0]

        # -----------------------------------------------------
        # STOCK MOVEMENTS
        # -----------------------------------------------------

        curseur.execute("""
            SELECT COUNT(*)
            FROM stock_movements
            WHERE date(createdAt) = date('now', 'localtime')
        """)
        mouvements_stock_aujourd_hui = curseur.fetchone()[0]

        return render_template(
            "dashboard.html",
            dashboard_date=dashboard_date,
            chiffre_affaires=chiffre_affaires,
            commandes_livrees=commandes_livrees,
            commandes_aujourd_hui=commandes_aujourd_hui,
            commandes_en_attente=commandes_en_attente,
            ventes_aujourd_hui=dict(ventes_aujourd_hui),
            livraisons_aujourd_hui=livraisons_aujourd_hui,
            produits=produits,
            stock_faible=stock_faible,
            stock_faible_seuil=stock_faible_seuil,
            stock_a_surveille=stock_a_surveille,
            clients=clients,
            credit_a_recouvrer=credit_a_recouvrer,
            dernieres_commandes=dernieres_commandes,
            produits_populaires=produits_populaires,
            evolution_ventes=evolution_ventes,
            approvisionnements_en_cours=approvisionnements_en_cours,
            receptions_aujourd_hui=receptions_aujourd_hui,
            mouvements_stock_aujourd_hui=mouvements_stock_aujourd_hui
        )

    finally:
        connexion.close()

@app.route("/articles")
def liste_articles():
    connexion = sqlite3.connect(DATABASE_PATH)
    connexion.row_factory = sqlite3.Row
    curseur = connexion.cursor()
    curseur.execute("SELECT * FROM articles")
    lignes = curseur.fetchall()
    connexion.close()
    resultat = [dict(ligne) for ligne in lignes]
    return jsonify(resultat)





# =========================================================
# ADMIN - STOCK MANAGEMENT
# =========================================================

@app.route("/admin/stock")
def admin_stock():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        # -------------------------------------------------
        # Verify administrator
        # -------------------------------------------------
        cursor.execute("""
            SELECT userId, userRole, userActive
            FROM users
            WHERE userId = ?
        """, (user_id,))

        current_user = cursor.fetchone()

        if (
            not current_user
            or current_user["userRole"] != 1
            or current_user["userActive"] != 1
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Global inventory
        #
        # Articles.articleStock is authoritative.
        # Do NOT derive this value from Lots.
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                a.articleId,
                a.articleProductId,
                p.productName,
                a.articleStock,
                a.articlePrice,
                a.articleVolume,
                a.articlePackage,
                a.articleState,
                CASE
                    WHEN a.articleStock <= 0 THEN 'rupture'
                    WHEN a.articleStock <= 10 THEN 'faible'
                    ELSE 'normal'
                END AS stockStatus
            FROM Articles a
            LEFT JOIN products p
                ON p.productId = a.articleProductId
            WHERE a.articleState = 1
            ORDER BY
                a.articleStock ASC,
                p.productName ASC,
                a.articleId ASC
        """)

        inventory = [dict(row) for row in cursor.fetchall()]

        # -------------------------------------------------
        # Depot inventory
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                ds.depotStockId,
                ds.depotId,
                d.depotName,
                d.depotCode,
                ds.articleId,
                p.productName,
                a.articleVolume,
                a.articlePackage,
                a.articleStock AS globalStock,
                ds.quantity AS depotQuantity,
                (a.articleStock - ds.quantity) AS stockDifference,
                CASE
                    WHEN ds.quantity <= 0 THEN 'rupture'
                    WHEN ds.quantity <= 10 THEN 'faible'
                    ELSE 'normal'
                END AS stockStatus,
                ds.updatedAt
            FROM depot_stocks ds
            INNER JOIN depots d
                ON d.depotId = ds.depotId
            LEFT JOIN Articles a
                ON a.articleId = ds.articleId
            LEFT JOIN products p
                ON p.productId = a.articleProductId
            ORDER BY
                d.depotName ASC,
                ds.quantity ASC,
                p.productName ASC,
                ds.articleId ASC
        """)

        depot_inventory = [dict(row) for row in cursor.fetchall()]

        # -------------------------------------------------
        # Active lots
        #
        # FEFO ordering:
        # earliest expiration first, then receipt date.
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                l.lotId,
                l.lotArticleId,
                p.productName,
                a.articleVolume,
                a.articlePackage,
                l.lotQuantity,
                l.lotExpDate,
                l.lotRecepDate,
                l.lotState,
                CASE
                    WHEN l.lotQuantity <= 0 THEN 'epuise'
                    WHEN l.lotExpDate < date('now', 'localtime')
                        THEN 'expire'
                    WHEN l.lotExpDate <= date('now', 'localtime', '+30 days')
                        THEN 'expire_bientot'
                    ELSE 'normal'
                END AS lotStatus
            FROM Lots l
            LEFT JOIN Articles a
                ON a.articleId = l.lotArticleId
            LEFT JOIN products p
                ON p.productId = a.articleProductId
            WHERE l.lotState = 1
              AND l.lotQuantity > 0
            ORDER BY
                l.lotExpDate ASC,
                l.lotRecepDate ASC,
                l.lotId ASC
        """)

        lots = [dict(row) for row in cursor.fetchall()]

        # -------------------------------------------------
        # Global stock movement history
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                sm.movementId,
                sm.articleId,
                p.productName,
                a.articleVolume,
                a.articlePackage,
                sm.lotId,
                sm.movementType,
                sm.quantity,
                sm.referenceType,
                sm.referenceId,
                sm.stockBefore,
                sm.stockAfter,
                sm.depotId,
                d.depotName,
                sm.createdBy,
                TRIM(
                    COALESCE(u.userName, '') || ' ' ||
                    COALESCE(u.userSurname, '')
                ) AS userName,
                sm.createdAt
            FROM stock_movements sm
            LEFT JOIN Articles a
                ON a.articleId = sm.articleId
            LEFT JOIN products p
                ON p.productId = a.articleProductId
            LEFT JOIN depots d
                ON d.depotId = sm.depotId
            LEFT JOIN users u
                ON u.userId = sm.createdBy
            ORDER BY
                sm.createdAt DESC,
                sm.movementId DESC
            LIMIT 100
        """)

        movements = [dict(row) for row in cursor.fetchall()]

        # -------------------------------------------------
        # KPIs
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                COUNT(*) AS activeArticles,
                COALESCE(SUM(articleStock), 0) AS totalUnits,
                COALESCE(SUM(
                    CASE
                        WHEN articleStock > 0
                         AND articleStock <= 10
                        THEN 1 ELSE 0
                    END
                ), 0) AS lowStockArticles,
                COALESCE(SUM(
                    CASE
                        WHEN articleStock <= 0
                        THEN 1 ELSE 0
                    END
                ), 0) AS outOfStockArticles
            FROM Articles
            WHERE articleState = 1
        """)

        stock_kpis = dict(cursor.fetchone())

        cursor.execute("""
            SELECT
                COUNT(*) AS activeLots,
                COALESCE(SUM(lotQuantity), 0) AS lotUnits
            FROM Lots
            WHERE lotState = 1
              AND lotQuantity > 0
        """)

        lot_kpis = dict(cursor.fetchone())

        cursor.execute("""
            SELECT
                COUNT(*) AS movementCount,
                COALESCE(SUM(
                    CASE
                        WHEN movementType = 'IN'
                        THEN quantity ELSE 0
                    END
                ), 0) AS totalIn,
                COALESCE(SUM(
                    CASE
                        WHEN movementType = 'OUT'
                        THEN quantity ELSE 0
                    END
                ), 0) AS totalOut
            FROM stock_movements
        """)

        movement_kpis = dict(cursor.fetchone())

        # -------------------------------------------------
        # Depots
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                depotId,
                depotName,
                depotCode,
                depotActive
            FROM depots
            ORDER BY depotName ASC
        """)

        depots = [dict(row) for row in cursor.fetchall()]

        return render_template(
            "admin_stock.html",
            inventory=inventory,
            depot_inventory=depot_inventory,
            lots=lots,
            movements=movements,
            stock_kpis=stock_kpis,
            lot_kpis=lot_kpis,
            movement_kpis=movement_kpis,
            depots=depots,
            low_stock_threshold=10
        )

    finally:
        conn.close()



# =========================================================
# PHASE 3 - ADMIN REPORTS
# =========================================================

@app.route("/admin/reports")
def admin_reports():
    """
    Administrative reporting page.

    Read-only reporting module:
    - sales KPIs
    - sales evolution
    - top products
    - top clients
    - payment methods
    - order statuses
    - stock movements
    - purchase orders

    Optional query parameters:
    ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
    """

    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        # -------------------------------------------------
        # Verify active administrator
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                userId,
                userRole,
                userActive
            FROM users
            WHERE userId = ?
        """, (admin_id,))

        admin = cursor.fetchone()

        if (
            not admin
            or int(admin["userRole"] or 0) != 1
            or int(admin["userActive"] or 0) != 1
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Date filters
        # -------------------------------------------------

        start_date = request.args.get("start_date", "").strip()
        end_date = request.args.get("end_date", "").strip()

        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

        if not date_pattern.match(start_date):
            start_date = ""

        if not date_pattern.match(end_date):
            end_date = ""

        date_conditions = []
        date_params = []

        if start_date:
            date_conditions.append("date(c.cmdDate) >= date(?)")
            date_params.append(start_date)

        if end_date:
            date_conditions.append("date(c.cmdDate) <= date(?)")
            date_params.append(end_date)

        order_date_filter = ""

        if date_conditions:
            order_date_filter = " AND " + " AND ".join(date_conditions)

        # -------------------------------------------------
        # Sales KPIs
        # Only delivered orders represent completed sales.
        # -------------------------------------------------

        cursor.execute(f"""
            SELECT
                COUNT(*) AS totalOrders,
                COALESCE(SUM(c.cmdTotal), 0) AS totalRevenue,
                COALESCE(AVG(c.cmdTotal), 0) AS averageOrder
            FROM Commandes c
            WHERE c.cmdStatut = 'livree'
            {order_date_filter}
        """, date_params)

        sales_summary = dict(cursor.fetchone())

        # -------------------------------------------------
        # All order count
        # -------------------------------------------------

        cursor.execute(f"""
            SELECT COUNT(*) AS totalOrders
            FROM Commandes c
            WHERE 1 = 1
            {order_date_filter}
        """, date_params)

        all_order_summary = dict(cursor.fetchone())

        # -------------------------------------------------
        # Sales evolution
        # -------------------------------------------------

        cursor.execute(f"""
            SELECT
                date(c.cmdDate) AS saleDate,
                COUNT(*) AS orderCount,
                COALESCE(SUM(c.cmdTotal), 0) AS total
            FROM Commandes c
            WHERE c.cmdStatut = 'livree'
            {order_date_filter}
            GROUP BY date(c.cmdDate)
            ORDER BY saleDate ASC
        """, date_params)

        sales_evolution = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Top products
        # -------------------------------------------------

        cursor.execute(f"""
            SELECT
                ac.artcmdArticleId AS articleId,
                p.productName,
                a.articleVolume,
                SUM(ac.artcmdQuantity) AS quantity,
                COALESCE(SUM(ac.artcmdSubTotal), 0) AS revenue
            FROM articlesCommandes ac
            JOIN Commandes c
                ON c.cmdId = ac.artcmdCmdId
            LEFT JOIN Articles a
                ON a.articleId = ac.artcmdArticleId
            LEFT JOIN products p
                ON p.productId = a.articleProductId
            WHERE c.cmdStatut = 'livree'
            {order_date_filter}
            GROUP BY
                ac.artcmdArticleId,
                p.productName,
                a.articleVolume
            ORDER BY revenue DESC, quantity DESC
            LIMIT 10
        """, date_params)

        top_products = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Top clients
        # -------------------------------------------------

        cursor.execute(f"""
            SELECT
                c.cmdClientId AS userId,
                COALESCE(u.userName, '') AS userName,
                COALESCE(u.userSurname, '') AS userSurname,
                COUNT(*) AS orderCount,
                COALESCE(SUM(c.cmdTotal), 0) AS revenue
            FROM Commandes c
            LEFT JOIN users u
                ON u.userId = c.cmdClientId
            WHERE c.cmdStatut = 'livree'
              AND u.userRole = 2
            {order_date_filter}
            GROUP BY
                c.cmdClientId,
                u.userName,
                u.userSurname
            ORDER BY revenue DESC
            LIMIT 10
        """, date_params)

        top_clients = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Payment methods
        # -------------------------------------------------

        cursor.execute(f"""
            SELECT
                COALESCE(c.cmdPayementMethod, 'Non renseign?') AS method,
                COUNT(*) AS orderCount,
                COALESCE(SUM(c.cmdTotal), 0) AS revenue
            FROM Commandes c
            WHERE c.cmdStatut = 'livree'
            {order_date_filter}
            GROUP BY c.cmdPayementMethod
            ORDER BY revenue DESC
        """, date_params)

        payment_methods = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Order statuses
        # -------------------------------------------------

        cursor.execute(f"""
            SELECT
                c.cmdStatut AS status,
                COUNT(*) AS orderCount,
                COALESCE(SUM(c.cmdTotal), 0) AS total
            FROM Commandes c
            WHERE 1 = 1
            {order_date_filter}
            GROUP BY c.cmdStatut
            ORDER BY orderCount DESC
        """, date_params)

        order_statuses = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Stock movement summary
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                COUNT(*) AS movementCount,
                COALESCE(
                    SUM(
                        CASE
                            WHEN movementType = 'IN'
                            THEN quantity
                            ELSE 0
                        END
                    ),
                    0
                ) AS totalIn,
                COALESCE(
                    SUM(
                        CASE
                            WHEN movementType = 'OUT'
                            THEN quantity
                            ELSE 0
                        END
                    ),
                    0
                ) AS totalOut
            FROM stock_movements
        """)

        stock_summary = dict(cursor.fetchone())

        # -------------------------------------------------
        # Purchase order summary
        # -------------------------------------------------

        cursor.execute("""
            SELECT
                COUNT(*) AS totalPurchaseOrders,
                COALESCE(
                    SUM(
                        CASE
                            WHEN status = 'draft'
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS draftOrders,
                COALESCE(
                    SUM(
                        CASE
                            WHEN status = 'submitted'
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS submittedOrders,
                COALESCE(
                    SUM(
                        CASE
                            WHEN status = 'received'
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS receivedOrders
            FROM purchase_orders
        """)

        purchase_summary = dict(cursor.fetchone())

        # -------------------------------------------------
        # Report metadata
        # -------------------------------------------------

        return render_template(
            "admin_reports.html",
            sales_summary=sales_summary,
            all_order_summary=all_order_summary,
            sales_evolution=sales_evolution,
            top_products=top_products,
            top_clients=top_clients,
            payment_methods=payment_methods,
            order_statuses=order_statuses,
            stock_summary=stock_summary,
            purchase_summary=purchase_summary,
            start_date=start_date,
            end_date=end_date
        )

    finally:
        conn.close()



# =========================================================
# ADMIN - FID?LIT?
# =========================================================

@app.route("/admin/fidelite")
def admin_fidelite():
    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        # -------------------------------------------------
        # Verify active administrator
        # -------------------------------------------------
        cursor.execute("""
            SELECT userId
            FROM users
            WHERE userId = ?
              AND userRole = 1
              AND userActive = 1
            LIMIT 1
        """, (admin_id,))

        admin = cursor.fetchone()

        if not admin:
            return redirect(url_for("getlogin"))

        # -------------------------------------------------
        # Customer loyalty summary
        #
        # IMPORTANT:
        # This query is read-only and intentionally does
        # NOT call get_loyalty_account(), so simply viewing
        # the page never creates loyalty accounts.
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                u.userId,
                u.userName,
                u.userSurname,
                u.userPhone,
                u.userMail,

                COALESCE(
                    la.points_balance,
                    0
                ) AS points_balance,

                COALESCE(
                    SUM(
                        CASE
                            WHEN lt.type = 'PURCHASE'
                            THEN lt.points
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_points_earned,

                COUNT(
                    CASE
                        WHEN lt.type = 'PURCHASE'
                        THEN lt.id
                    END
                ) AS rewards_count,

                MAX(
                    CASE
                        WHEN lt.type = 'PURCHASE'
                        THEN lt.created_at
                    END
                ) AS last_reward

            FROM users u

            LEFT JOIN loyalty_accounts la
                ON la.user_id = u.userId

            LEFT JOIN loyalty_transactions lt
                ON lt.user_id = u.userId

            WHERE u.userRole = 2

            GROUP BY
                u.userId,
                u.userName,
                u.userSurname,
                u.userPhone,
                u.userMail,
                la.points_balance

            ORDER BY
                points_balance DESC,
                total_points_earned DESC,
                u.userSurname ASC,
                u.userName ASC
        """)

        customers = cursor.fetchall()

        # -------------------------------------------------
        # KPI: enrolled customers
        # -------------------------------------------------
        cursor.execute("""
            SELECT COUNT(*)
            FROM loyalty_accounts la
            INNER JOIN users u
                ON u.userId = la.user_id
            WHERE u.userRole = 2
        """)

        enrolled_customers = cursor.fetchone()[0]

        # -------------------------------------------------
        # KPI: total points currently in circulation
        # -------------------------------------------------
        cursor.execute("""
            SELECT COALESCE(
                SUM(la.points_balance),
                0
            )
            FROM loyalty_accounts la
            INNER JOIN users u
                ON u.userId = la.user_id
            WHERE u.userRole = 2
        """)

        total_points_balance = cursor.fetchone()[0] or 0

        # -------------------------------------------------
        # KPI: total points ever earned by customers
        # -------------------------------------------------
        cursor.execute("""
            SELECT COALESCE(
                SUM(lt.points),
                0
            )
            FROM loyalty_transactions lt
            INNER JOIN users u
                ON u.userId = lt.user_id
            WHERE u.userRole = 2
              AND lt.type = 'PURCHASE'
        """)

        total_points_earned = cursor.fetchone()[0] or 0

        # -------------------------------------------------
        # KPI: total purchase rewards
        # -------------------------------------------------
        cursor.execute("""
            SELECT COUNT(*)
            FROM loyalty_transactions lt
            INNER JOIN users u
                ON u.userId = lt.user_id
            WHERE u.userRole = 2
              AND lt.type = 'PURCHASE'
        """)

        total_rewards = cursor.fetchone()[0] or 0

        # -------------------------------------------------
        # Transaction history
        # Only role-2 customers are included.
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                lt.id,
                lt.user_id,
                u.userName,
                u.userSurname,
                lt.command_id,
                lt.type,
                lt.points,
                lt.description,
                lt.created_at
            FROM loyalty_transactions lt
            INNER JOIN users u
                ON u.userId = lt.user_id
            WHERE u.userRole = 2
            ORDER BY
                lt.created_at DESC,
                lt.id DESC
            LIMIT 100
        """)

        transactions = cursor.fetchall()

        return render_template(
            "admin_fidelite.html",
            customers=customers,
            transactions=transactions,
            enrolled_customers=enrolled_customers,
            total_points_balance=total_points_balance,
            total_points_earned=total_points_earned,
            total_rewards=total_rewards,
            points_per_reward=LOYALTY_XAF_PER_POINT,
            point_value=LOYALTY_POINT_VALUE
        )

    finally:
        conn.close()

@app.route("/admin/clients")
def admin_clients():
    """
    Administrative customer management page.

    Read-only dashboard:
    - customer identity/contact
    - order count
    - delivered order count
    - delivered sales
    - purchased quantities
    - loyalty balance
    - credit balance
    - account status
    """
    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        # -------------------------------------------------
        # Verify active administrator
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                userId,
                userRole,
                userActive
            FROM users
            WHERE userId = ?
        """, (admin_id,))

        admin = cursor.fetchone()

        if (
            not admin
            or int(admin["userRole"] or 0) != 1
            or int(admin["userActive"] or 0) != 1
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Customer overview
        #
        # IMPORTANT:
        # Orders/revenue and purchased quantities are
        # aggregated independently to prevent duplicated
        # command totals when an order contains multiple
        # article rows.
        #
        # Loyalty and credit accounts are read-only here.
        # Missing accounts must NOT be created automatically.
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                u.userId,
                u.userName,
                u.userSurname,
                u.userMail,
                u.userPhone,
                u.userAddress,
                u.userActive,

                COALESCE(o.orderCount, 0) AS orderCount,

                COALESCE(
                    o.deliveredOrderCount,
                    0
                ) AS deliveredOrderCount,

                COALESCE(
                    o.totalSpent,
                    0
                ) AS totalSpent,

                COALESCE(
                    items.totalItemsPurchased,
                    0
                ) AS totalItemsPurchased,

                o.lastOrderDate,

                COALESCE(
                    la.points_balance,
                    0
                ) AS loyaltyPoints,

                COALESCE(
                    cc.creditLimit,
                    0
                ) AS creditLimit,

                COALESCE(
                    (
                        SELECT
                            SUM(
                                CASE
                                    WHEN ct.transactionType = 'CREDIT'
                                    THEN ct.amount
                                    WHEN ct.transactionType = 'REPAYMENT'
                                    THEN -ct.amount
                                    ELSE 0
                                END
                            )
                        FROM credit_transactions ct
                        WHERE ct.creditCustomerId =
                              cc.creditCustomerId
                    ),
                    0
                ) AS creditBalance,

                COALESCE(
                    cc.creditStatus,
                    'inactive'
                ) AS creditStatus

            FROM users u

            LEFT JOIN (
                SELECT
                    c.cmdClientId,

                    COUNT(c.cmdId) AS orderCount,

                    COUNT(
                        CASE
                            WHEN c.cmdStatut = 'livree'
                            THEN c.cmdId
                        END
                    ) AS deliveredOrderCount,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN c.cmdStatut = 'livree'
                                THEN c.cmdTotal
                                ELSE 0
                            END
                        ),
                        0
                    ) AS totalSpent,

                    MAX(c.cmdDate) AS lastOrderDate

                FROM Commandes c

                GROUP BY
                    c.cmdClientId

            ) o
                ON o.cmdClientId = u.userId

            LEFT JOIN (
                SELECT
                    c.cmdClientId,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN c.cmdStatut = 'livree'
                                THEN ac.artcmdQuantity
                                ELSE 0
                            END
                        ),
                        0
                    ) AS totalItemsPurchased

                FROM Commandes c

                LEFT JOIN articlesCommandes ac
                    ON ac.artcmdCmdId = c.cmdId

                GROUP BY
                    c.cmdClientId

            ) items
                ON items.cmdClientId = u.userId

            LEFT JOIN loyalty_accounts la
                ON la.user_id = u.userId

            LEFT JOIN credit_customers cc
                ON cc.userId = u.userId

            WHERE u.userRole = 2

            ORDER BY
                u.userName ASC,
                u.userSurname ASC
        """)

        clients = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Summary statistics
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                COUNT(*) AS totalClients,
                SUM(
                    CASE
                        WHEN userActive = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS activeClients,
                SUM(
                    CASE
                        WHEN userActive = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS inactiveClients
            FROM users
            WHERE userRole = 2
        """)

        client_summary = dict(cursor.fetchone())

        cursor.execute("""
            SELECT
                COUNT(DISTINCT c.cmdId) AS totalOrders,
                COALESCE(
                    SUM(
                        CASE
                            WHEN c.cmdStatut = 'livree'
                            THEN c.cmdTotal
                            ELSE 0
                        END
                    ),
                    0
                ) AS totalRevenue
            FROM Commandes c
            JOIN users u
                ON u.userId = c.cmdClientId
            WHERE u.userRole = 2
        """)

        sales_summary = dict(cursor.fetchone())

        return render_template(
            "admin_clients.html",
            clients=clients,
            client_summary=client_summary,
            sales_summary=sales_summary
        )

    finally:
        conn.close()




# ============================================================
# ADMIN INVOICE PDF ? VIEW
# ============================================================

@app.route("/admin/invoices/<int:invoice_id>/pdf")
def admin_invoice_pdf(invoice_id):
    """
    Active administrator only.

    Displays an existing invoice PDF inline.
    """

    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                userId,
                userRole,
                userActive
            FROM users
            WHERE userId = ?
        """, (admin_id,))

        admin = cursor.fetchone()

        if (
            not admin
            or int(admin["userRole"] or 0) != 1
            or int(admin["userActive"] or 0) != 1
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        cursor.execute("""
            SELECT
                invoiceId,
                invoiceNumber,
                commandId,
                clientId,
                pdfPath
            FROM invoices
            WHERE invoiceId = ?
            LIMIT 1
        """, (invoice_id,))

        invoice = cursor.fetchone()

        if not invoice:
            return "Facture introuvable.", 404

        pdf_path = resolve_invoice_pdf_path(invoice["pdfPath"])

        if not pdf_path:
            return "PDF de la facture indisponible.", 404

        pdf_file = Path(pdf_path)

        if not pdf_file.is_file():
            return "Fichier PDF de la facture introuvable.", 404

        return send_file(
            str(pdf_file),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=f"{invoice['invoiceNumber']}.pdf"
        )

    finally:
        conn.close()


# ============================================================
# ADMIN INVOICE PDF ? DOWNLOAD
# ============================================================

@app.route("/admin/invoices/<int:invoice_id>/download")
def admin_invoice_download(invoice_id):
    """
    Active administrator only.

    Downloads an existing invoice PDF.
    """

    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                userId,
                userRole,
                userActive
            FROM users
            WHERE userId = ?
        """, (admin_id,))

        admin = cursor.fetchone()

        if (
            not admin
            or int(admin["userRole"] or 0) != 1
            or int(admin["userActive"] or 0) != 1
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        cursor.execute("""
            SELECT
                invoiceId,
                invoiceNumber,
                commandId,
                clientId,
                pdfPath
            FROM invoices
            WHERE invoiceId = ?
            LIMIT 1
        """, (invoice_id,))

        invoice = cursor.fetchone()

        if not invoice:
            return "Facture introuvable.", 404

        pdf_path = resolve_invoice_pdf_path(invoice["pdfPath"])

        if not pdf_path:
            return "PDF de la facture indisponible.", 404

        pdf_file = Path(pdf_path)

        if not pdf_file.is_file():
            return "Fichier PDF de la facture introuvable.", 404

        return send_file(
            str(pdf_file),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{invoice['invoiceNumber']}.pdf"
        )

    finally:
        conn.close()


@app.route("/admin/clients/<int:user_id>")
def admin_client_detail(user_id):
    """
    Administrative customer profile.

    Read-only CRM view:
    - customer identity/contact
    - account status
    - order KPIs
    - recent orders
    - loyalty balance and transactions
    - credit account, transactions and payments
    """
    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        # -------------------------------------------------
        # Verify active administrator
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                userId,
                userRole,
                userActive
            FROM users
            WHERE userId = ?
        """, (admin_id,))

        admin = cursor.fetchone()

        if (
            not admin
            or int(admin["userRole"] or 0) != 1
            or int(admin["userActive"] or 0) != 1
        ):
            return jsonify({
                "success": False,
                "error": "Unauthorized."
            }), 403

        # -------------------------------------------------
        # Client identity
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                userId,
                userName,
                userSurname,
                userMail,
                userPhone,
                userGender,
                userPicture,
                userBirthDate,
                userAddress,
                userActive,
                invoiceDeliveryMethod
            FROM users
            WHERE userId = ?
              AND userRole = 2
        """, (user_id,))

        client_row = cursor.fetchone()

        if not client_row:
            return "Client introuvable.", 404

        client = dict(client_row)

        # -------------------------------------------------
        # Order KPIs
        #
        # Orders are aggregated independently from item
        # quantities to prevent duplicated cmdTotal values.
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                COUNT(c.cmdId) AS orderCount,

                COUNT(
                    CASE
                        WHEN c.cmdStatut = 'livree'
                        THEN c.cmdId
                    END
                ) AS deliveredOrderCount,

                COALESCE(
                    SUM(
                        CASE
                            WHEN c.cmdStatut = 'livree'
                            THEN c.cmdTotal
                            ELSE 0
                        END
                    ),
                    0
                ) AS totalSpent,

                MAX(c.cmdDate) AS lastOrderDate

            FROM Commandes c
            WHERE c.cmdClientId = ?
        """, (user_id,))

        order_stats = dict(cursor.fetchone())

        # -------------------------------------------------
        # Delivered item quantity
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                COALESCE(
                    SUM(ac.artcmdQuantity),
                    0
                ) AS totalItemsPurchased

            FROM Commandes c
            JOIN articlesCommandes ac
                ON ac.artcmdCmdId = c.cmdId

            WHERE c.cmdClientId = ?
              AND c.cmdStatut = 'livree'
        """, (user_id,))

        item_stats = dict(cursor.fetchone())

        # -------------------------------------------------
        # Loyalty account
        #
        # Read-only. Do not create missing accounts here.
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                id,
                points_balance,
                created_at,
                updated_at
            FROM loyalty_accounts
            WHERE user_id = ?
            LIMIT 1
        """, (user_id,))

        loyalty_account_row = cursor.fetchone()

        loyalty_account = (
            dict(loyalty_account_row)
            if loyalty_account_row
            else {
                "id": None,
                "points_balance": 0,
                "created_at": None,
                "updated_at": None
            }
        )

        # -------------------------------------------------
        # Loyalty transactions
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                id,
                command_id,
                type,
                points,
                description,
                created_at
            FROM loyalty_transactions
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 20
        """, (user_id,))

        loyalty_transactions = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Credit account
        #
        # credit_customers is the authoritative credit
        # configuration table. Outstanding debt is derived
        # from credit_transactions.
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                creditCustomerId,
                userId,
                customerType,
                creditEnabled,
                creditLimit,
                paymentTermsDays,
                creditStatus,
                notes,
                createdAt,
                updatedAt
            FROM credit_customers
            WHERE userId = ?
            LIMIT 1
        """, (user_id,))

        credit_customer_row = cursor.fetchone()

        if credit_customer_row:
            credit_customer = dict(
                credit_customer_row
            )

            cursor.execute("""
                SELECT
                    COALESCE(
                        SUM(
                            CASE
                                WHEN transactionType = 'CREDIT'
                                THEN amount
                                WHEN transactionType = 'REPAYMENT'
                                THEN -amount
                                ELSE 0
                            END
                        ),
                        0
                    ) AS balance
                FROM credit_transactions
                WHERE creditCustomerId = ?
            """, (
                credit_customer[
                    "creditCustomerId"
                ],
            ))

            credit_balance_row = cursor.fetchone()

            credit_balance = float(
                credit_balance_row["balance"] or 0
            )

            credit_account = {
                "id": credit_customer[
                    "creditCustomerId"
                ],
                "user_id": user_id,
                "credit_limit": float(
                    credit_customer["creditLimit"] or 0
                ),
                "balance": max(
                    0.0,
                    credit_balance
                ),
                "status": credit_customer[
                    "creditStatus"
                ],
                "credit_enabled": int(
                    credit_customer["creditEnabled"] or 0
                ),
                "payment_terms_days": int(
                    credit_customer["paymentTermsDays"] or 0
                ),
                "created_at": credit_customer[
                    "createdAt"
                ],
                "updated_at": credit_customer[
                    "updatedAt"
                ]
            }

            credit_customer_id = credit_customer[
                "creditCustomerId"
            ]

        else:
            credit_account = {
                "id": None,
                "user_id": user_id,
                "credit_limit": 0,
                "balance": 0,
                "status": "inactive",
                "credit_enabled": 0,
                "payment_terms_days": 0,
                "created_at": None,
                "updated_at": None
            }

            credit_customer_id = None

        # -------------------------------------------------
        # Credit transactions
        # -------------------------------------------------
        credit_transactions = []

        if credit_customer_id:
            cursor.execute("""
                SELECT
                    creditTransactionId,
                    creditCustomerId,
                    commandId,
                    transactionType,
                    amount,
                    dueDate,
                    description,
                    createdAt
                FROM credit_transactions
                WHERE creditCustomerId = ?
                ORDER BY createdAt DESC,
                         creditTransactionId DESC
                LIMIT 20
            """, (credit_customer_id,))

            credit_transactions = [
                dict(row)
                for row in cursor.fetchall()
            ]

        # -------------------------------------------------
        # Credit payments
        # -------------------------------------------------
        credit_payments = []

        if credit_customer_id:
            cursor.execute("""
                SELECT
                    creditPaymentId,
                    creditCustomerId,
                    amount,
                    paymentDate,
                    paymentMethod,
                    reference,
                    notes,
                    createdAt
                FROM credit_payments
                WHERE creditCustomerId = ?
                ORDER BY createdAt DESC,
                         creditPaymentId DESC
                LIMIT 20
            """, (credit_customer_id,))

            credit_payments = [
                dict(row)
                for row in cursor.fetchall()
            ]

        # -------------------------------------------------
        # Recent orders
        # -------------------------------------------------
        cursor.execute("""
            SELECT
                c.cmdId,
                c.cmdNum,
                c.cmdStatut,
                c.cmdPayementMethod,
                c.cmdTotal,
                c.cmdLivAddress,
                c.cmdDate,
                c.cmdType,
                c.cmdZoneId,
                c.cmdLivreurId,
                c.cmdDepotId,

                COALESCE(
                    items.itemCount,
                    0
                ) AS itemCount,

                COALESCE(
                    items.totalQuantity,
                    0
                ) AS totalQuantity

            FROM Commandes c

            LEFT JOIN (
                SELECT
                    artcmdCmdId,
                    COUNT(*) AS itemCount,
                    COALESCE(
                        SUM(artcmdQuantity),
                        0
                    ) AS totalQuantity
                FROM articlesCommandes
                GROUP BY artcmdCmdId
            ) items
                ON items.artcmdCmdId = c.cmdId

            WHERE c.cmdClientId = ?

            ORDER BY
                c.cmdDate DESC,
                c.cmdId DESC

            LIMIT 20
        """, (user_id,))

        recent_orders = [
            dict(row)
            for row in cursor.fetchall()
        ]

        # -------------------------------------------------
        # Render profile
        # -------------------------------------------------
        return render_template(
            "admin_client_detail.html",
            client=client,
            order_stats=order_stats,
            item_stats=item_stats,
            loyalty_account=loyalty_account,
            loyalty_transactions=loyalty_transactions,
            credit_account=credit_account,
            credit_transactions=credit_transactions,
            credit_payments=credit_payments,
            recent_orders=recent_orders
        )

    finally:
        conn.close()



# =============================================================
# ADMIN - CLIENT INVOICE DELIVERY PREFERENCE
# =============================================================
@app.route(
    "/admin/clients/<int:user_id>/invoice-delivery",
    methods=["POST"]
)
def admin_client_invoice_delivery(user_id):
    """
    Update the invoice delivery preference for a client.

    This is a CLIENT-LEVEL preference.
    It does not modify existing invoices.
    It applies to future invoices only.
    """

    admin_id = session.get("user_id")

    if not admin_id:
        return jsonify({
            "success": False,
            "message": "Administrator authentication required."
        }), 401

    data = request.get_json(silent=True) or {}

    delivery_method = (
        str(data.get("delivery_method", ""))
        .strip()
        .lower()
    )

    if delivery_method not in {"email", "whatsapp"}:
        return jsonify({
            "success": False,
            "message": "Invalid invoice delivery method."
        }), 400

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        # -----------------------------------------------------
        # Verify active administrator
        # -----------------------------------------------------
        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 1
              AND userActive = 1
        """, (admin_id,))

        admin = cursor.fetchone()

        if not admin:
            return jsonify({
                "success": False,
                "message": "Administrator access required."
            }), 403

        # -----------------------------------------------------
        # Verify target customer
        # -----------------------------------------------------
        cursor.execute("""
            SELECT
                userId,
                userName,
                userSurname,
                userMail,
                userPhone,
                userRole,
                userActive
            FROM Users
            WHERE userId = ?
              AND userRole = 2
            LIMIT 1
        """, (user_id,))

        customer = cursor.fetchone()

        if not customer:
            return jsonify({
                "success": False,
                "message": "Client account not found."
            }), 404

        # -----------------------------------------------------
        # Validate selected contact method
        # -----------------------------------------------------
        if delivery_method == "email":

            email_address = (
                str(customer["userMail"] or "")
                .strip()
            )

            email_pattern = (
                r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
            )

            if not re.match(email_pattern, email_address):
                return jsonify({
                    "success": False,
                    "message": (
                        "A valid email address is required "
                        "for invoice delivery by email."
                    )
                }), 400

        else:

            phone = (
                str(customer["userPhone"] or "")
                .strip()
            )

            if not phone:
                return jsonify({
                    "success": False,
                    "message": (
                        "A phone number is required "
                        "for WhatsApp invoice delivery."
                    )
                }), 400

            normalized_phone = normalize_whatsapp_number(
                phone
            )

            if not normalized_phone:
                return jsonify({
                    "success": False,
                    "message": (
                        "The phone number is not valid "
                        "for WhatsApp invoice delivery."
                    )
                }), 400

        # -----------------------------------------------------
        # Save client-level preference
        #
        # Existing invoices are historical records.
        # They must not be modified.
        # -----------------------------------------------------
        cursor.execute("""
            UPDATE Users
            SET invoiceDeliveryMethod = ?
            WHERE userId = ?
              AND userRole = 2
        """, (
            delivery_method,
            user_id
        ))

        if cursor.rowcount != 1:
            conn.rollback()

            return jsonify({
                "success": False,
                "message": (
                    "Unable to update invoice delivery preference."
                )
            }), 500

        conn.commit()

        response_data = {
            "success": True,
            "message": (
                "Invoice delivery preference updated successfully."
            ),
            "userId": user_id,
            "deliveryMethod": delivery_method
        }

        # -----------------------------------------------------
        # WhatsApp invoice action
        #
        # Build the CURRENT consolidated client invoice so the
        # WhatsApp message represents all delivered commands.
        #
        # Business rule:
        # - all delivered command products are consolidated
        # - delivery is charged only once
        # - command-level invoices are untouched
        # -----------------------------------------------------
        if delivery_method == "whatsapp":

            normalized_phone = normalize_whatsapp_number(
                str(customer["userPhone"] or "").strip()
            )

            response_data["whatsappUrl"] = (
                f"https://wa.me/{normalized_phone}"
            )

            try:
                whatsapp_invoice = (
                    create_client_consolidated_invoice(
                        user_id
                    )
                )

                if whatsapp_invoice.get("success"):

                    client_invoice = (
                        whatsapp_invoice.get(
                            "clientInvoice"
                        )
                    )

                    if client_invoice:

                        commands = whatsapp_invoice.get(
                            "commands",
                            []
                        )

                        subtotal = float(
                            whatsapp_invoice.get(
                                "subtotal",
                                client_invoice["subtotal"]
                            ) or 0
                        )

                        delivery_fee = float(
                            whatsapp_invoice.get(
                                "delivery_fee",
                                client_invoice["deliveryFee"]
                            ) or 0
                        )

                        total = float(
                            whatsapp_invoice.get(
                                "total",
                                client_invoice["total"]
                            ) or 0
                        )

                        invoice_number = str(
                            client_invoice[
                                "invoiceNumber"
                            ]
                        )

                        client_name = (
                            f"{customer['userName'] or ''} "
                            f"{customer['userSurname'] or ''}"
                        ).strip()

                        if not client_name:
                            client_name = "Client"

                        response_data[
                            "whatsappInvoice"
                        ] = {
                            "invoiceNumber": invoice_number,
                            "commandCount": len(commands),
                            "subtotal": subtotal,
                            "deliveryFee": delivery_fee,
                            "total": total,
                            "clientName": client_name
                        }

                else:
                    print(
                        "[WHATSAPP INVOICE WARNING]",
                        whatsapp_invoice.get(
                            "error",
                            "Unable to create consolidated invoice."
                        )
                    )

            except Exception as invoice_exc:
                print(
                    "[WHATSAPP INVOICE ERROR]",
                    repr(invoice_exc)
                )

        return jsonify(response_data), 200

    except Exception as exc:
        conn.rollback()

        print(
            "[ADMIN INVOICE DELIVERY ERROR]",
            repr(exc)
        )

        return jsonify({
            "success": False,
            "message": (
                "Unable to save invoice delivery preference."
            )
        }), 500

    finally:
        conn.close()



# ============================================================
# ADMIN CLIENT - INVOICE ACTIONS
# ============================================================

@app.route("/admin/clients/<int:user_id>/invoice")
def admin_client_latest_invoice(user_id):
    """
    Open the client's consolidated invoice.

    BUSINESS RULE:
    - One invoice covers all delivered commands of the client.
    - Product subtotals are consolidated.
    - Delivery is charged only once.
    - Existing command-level invoices remain untouched.
    """

    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()

    try:
        cursor = conn.cursor()

        # ----------------------------------------------------
        # Verify active administrator
        # ----------------------------------------------------
        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 1
              AND userActive = 1
            LIMIT 1
        """, (admin_id,))

        admin = cursor.fetchone()

        if not admin:
            return "Administrator access required.", 403

        # ----------------------------------------------------
        # Verify client
        # ----------------------------------------------------
        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 2
            LIMIT 1
        """, (user_id,))

        client = cursor.fetchone()

        if not client:
            return "Client account not found.", 404

        # ----------------------------------------------------
        # Find the latest consolidated invoice
        # ----------------------------------------------------
        cursor.execute("""
            SELECT
                clientInvoiceId,
                invoiceNumber,
                pdfPath
            FROM client_invoices
            WHERE clientId = ?
            ORDER BY
                invoiceDate DESC,
                clientInvoiceId DESC
            LIMIT 1
        """, (user_id,))

        invoice = cursor.fetchone()

        # ----------------------------------------------------
        # Create consolidated invoice when none exists
        # ----------------------------------------------------
        if not invoice:
            result = create_client_consolidated_invoice(
                user_id
            )

            if not result.get("success"):
                return (
                    result.get(
                        "error",
                        "Impossible de cr?er la facture consolid?e."
                    ),
                    400
                )

            invoice = result.get("clientInvoice")

            if not invoice:
                return (
                    "La facture consolid?e n'a pas pu ?tre cr??e.",
                    500
                )

        invoice_id = invoice["clientInvoiceId"]

        # ----------------------------------------------------
        # Generate PDF when necessary
        # ----------------------------------------------------
        pdf_path = resolve_invoice_pdf_path(invoice["pdfPath"])

        if not pdf_path or not Path(pdf_path).is_file():
            pdf_path = generate_client_consolidated_invoice_pdf(
                invoice_id
            )

        if not pdf_path:
            return "PDF de la facture consolid?e indisponible.", 404

        pdf_file = Path(pdf_path)

        if not pdf_file.is_file():
            return (
                "Fichier PDF de la facture consolid?e introuvable.",
                404
            )

        return send_file(
            str(pdf_file),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=f"{invoice['invoiceNumber']}.pdf"
        )

    except Exception as exc:
        traceback.print_exc()
        return (
            f"Erreur lors de l'ouverture de la facture consolid?e : {exc}",
            500
        )

    finally:
        conn.close()


@app.route("/admin/clients/<int:user_id>/invoice/download")
def admin_client_latest_invoice_download(user_id):
    """
    Download the client's consolidated invoice.

    BUSINESS RULE:
    - One invoice covers all delivered commands of the client.
    - Product subtotals are consolidated.
    - Delivery is charged only once.
    - Existing command-level invoices remain untouched.
    """

    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()

    try:
        cursor = conn.cursor()

        # ----------------------------------------------------
        # Verify active administrator
        # ----------------------------------------------------
        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 1
              AND userActive = 1
            LIMIT 1
        """, (admin_id,))

        admin = cursor.fetchone()

        if not admin:
            return "Administrator access required.", 403

        # ----------------------------------------------------
        # Verify client
        # ----------------------------------------------------
        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 2
            LIMIT 1
        """, (user_id,))

        client = cursor.fetchone()

        if not client:
            return "Client account not found.", 404

        # ----------------------------------------------------
        # Find the latest consolidated invoice
        # ----------------------------------------------------
        cursor.execute("""
            SELECT
                clientInvoiceId,
                invoiceNumber,
                pdfPath
            FROM client_invoices
            WHERE clientId = ?
            ORDER BY
                invoiceDate DESC,
                clientInvoiceId DESC
            LIMIT 1
        """, (user_id,))

        invoice = cursor.fetchone()

        # ----------------------------------------------------
        # Create consolidated invoice when none exists
        # ----------------------------------------------------
        if not invoice:
            result = create_client_consolidated_invoice(
                user_id
            )

            if not result.get("success"):
                return (
                    result.get(
                        "error",
                        "Impossible de cr?er la facture consolid?e."
                    ),
                    400
                )

            invoice = result.get("clientInvoice")

            if not invoice:
                return (
                    "La facture consolid?e n'a pas pu ?tre cr??e.",
                    500
                )

        invoice_id = invoice["clientInvoiceId"]

        # ----------------------------------------------------
        # Generate PDF when necessary
        # ----------------------------------------------------
        pdf_path = resolve_invoice_pdf_path(invoice["pdfPath"])

        if not pdf_path or not Path(pdf_path).is_file():
            pdf_path = generate_client_consolidated_invoice_pdf(
                invoice_id
            )

        if not pdf_path:
            return "PDF de la facture consolid?e indisponible.", 404

        pdf_file = Path(pdf_path)

        if not pdf_file.is_file():
            return (
                "Fichier PDF de la facture consolid?e introuvable.",
                404
            )

        return send_file(
            str(pdf_file),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{invoice['invoiceNumber']}.pdf"
        )

    except Exception as exc:
        traceback.print_exc()
        return (
            f"Erreur lors du t?l?chargement de la facture consolid?e : {exc}",
            500
        )

    finally:
        conn.close()


@app.route("/admin/credit")
def admin_credit():
    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 1
              AND userActive = 1
        """, (admin_id,))

        admin = cursor.fetchone()

        if not admin:
            flash(
                "Administrator access required.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )

        cursor.execute("""
            SELECT
                u.userId,
                u.userName,
                u.userSurname,
                u.userMail,
                u.userPhone,
                u.userActive,

                COALESCE(
                    cc.creditLimit,
                    0
                ) AS credit_limit,

                COALESCE(
                    (
                        SELECT
                            SUM(
                                CASE
                                    WHEN ct.transactionType = 'CREDIT'
                                    THEN ct.amount
                                    WHEN ct.transactionType = 'REPAYMENT'
                                    THEN -ct.amount
                                    ELSE 0
                                END
                            )
                        FROM credit_transactions ct
                        WHERE ct.creditCustomerId =
                              cc.creditCustomerId
                    ),
                    0
                ) AS balance,

                COALESCE(
                    cc.creditStatus,
                    'inactive'
                ) AS credit_status,

                COALESCE(
                    cc.creditEnabled,
                    0
                ) AS credit_enabled,

                COALESCE(
                    cc.paymentTermsDays,
                    0
                ) AS payment_terms_days,

                cc.creditCustomerId

            FROM Users u

            LEFT JOIN credit_customers cc
                ON cc.userId = u.userId

            WHERE u.userRole = 2

            ORDER BY
                u.userName,
                u.userSurname
        """)

        customers = [
            dict(row)
            for row in cursor.fetchall()
        ]

        for customer in customers:
            customer["credit_limit"] = float(
                customer["credit_limit"] or 0
            )

            customer["balance"] = max(
                0.0,
                float(customer["balance"] or 0)
            )

            customer["available_credit"] = max(
                0.0,
                customer["credit_limit"]
                - customer["balance"]
            )

        return render_template(
            "admin_credit.html",
            customers=customers
        )

    finally:
        conn.close()


@app.route("/admin/credit/<int:user_id>", methods=["POST"])
def update_customer_credit(user_id):
    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    try:
        credit_limit = float(
            request.form.get("credit_limit", 0)
        )
    except (TypeError, ValueError):
        flash(
            "Invalid credit limit.",
            "error"
        )
        return redirect(
            url_for("admin_credit")
        )

    if credit_limit < 0:
        flash(
            "Credit limit cannot be negative.",
            "error"
        )
        return redirect(
            url_for("admin_credit")
        )

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        # -------------------------------------------------
        # Verify active administrator
        # -------------------------------------------------
        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 1
              AND userActive = 1
        """, (admin_id,))

        if not cursor.fetchone():
            flash(
                "Administrator access required.",
                "error"
            )
            return redirect(
                url_for("dashboard")
            )

        # -------------------------------------------------
        # Verify customer
        # -------------------------------------------------
        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 2
        """, (user_id,))

        if not cursor.fetchone():
            flash(
                "Customer not found.",
                "error"
            )
            return redirect(
                url_for("admin_credit")
            )

        # -------------------------------------------------
        # Update or create authoritative credit configuration
        # -------------------------------------------------
        cursor.execute("""
            SELECT creditCustomerId
            FROM credit_customers
            WHERE userId = ?
            LIMIT 1
        """, (user_id,))

        credit_customer = cursor.fetchone()

        if credit_customer:
            cursor.execute("""
                UPDATE credit_customers
                SET creditLimit = ?,
                    updatedAt = CURRENT_TIMESTAMP
                WHERE creditCustomerId = ?
            """, (
                credit_limit,
                credit_customer[
                    "creditCustomerId"
                ]
            ))

        else:
            cursor.execute("""
                INSERT INTO credit_customers (
                    userId,
                    customerType,
                    creditEnabled,
                    creditLimit,
                    paymentTermsDays,
                    creditStatus,
                    notes,
                    createdAt,
                    updatedAt
                )
                VALUES (
                    ?,
                    'revendeur',
                    1,
                    ?,
                    30,
                    'active',
                    NULL,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
            """, (
                user_id,
                credit_limit
            ))

        conn.commit()

        flash(
            "Customer credit limit updated.",
            "success"
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    return redirect(
        url_for("admin_credit")
    )


@app.route("/admin/credit/<int:user_id>/repayment", methods=["POST"])
def record_credit_repayment(user_id):

    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        # -----------------------------------------------------
        # Verify administrator
        # -----------------------------------------------------

        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 1
              AND userActive = 1
        """, (admin_id,))

        admin = cursor.fetchone()

        if not admin:

            flash(
                "Administrator access required.",
                "error"
            )

            return redirect(
                url_for("admin_credit")
            )

        # -----------------------------------------------------
        # Verify customer
        # -----------------------------------------------------

        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 2
        """, (user_id,))

        customer = cursor.fetchone()

        if not customer:

            flash(
                "Customer not found.",
                "error"
            )

            return redirect(
                url_for("admin_credit")
            )

        # -----------------------------------------------------
        # Read repayment information
        # -----------------------------------------------------

        try:

            amount = float(
                request.form.get(
                    "amount",
                    0
                )
            )

        except (TypeError, ValueError):

            flash(
                "Invalid repayment amount.",
                "error"
            )

            return redirect(
                url_for("admin_credit")
            )

        payment_method = (
            request.form.get(
                "payment_method",
                "CASH"
            )
            .strip()
        )

        reference = (
            request.form.get(
                "reference",
                ""
            )
            .strip()
        )

        notes = (
            request.form.get(
                "notes",
                ""
            )
            .strip()
        )

        if amount <= 0:

            flash(
                "Repayment amount must be greater than zero.",
                "error"
            )

            return redirect(
                url_for("admin_credit")
            )

        # -----------------------------------------------------
        # Record payment
        # -----------------------------------------------------

        actual_amount = create_credit_repayment(
            user_id=user_id,
            amount=amount,
            payment_method=payment_method,
            reference=reference,
            notes=notes,
            cursor=cursor
        )

        conn.commit()

        flash(
            (
                f"Repayment of "
                f"{actual_amount:,.0f} XAF recorded successfully."
            ),
            "success"
        )

    except ValueError as exc:

        conn.rollback()

        flash(
            str(exc),
            "error"
        )

    except Exception:

        conn.rollback()

        flash(
            "Unable to record the credit repayment.",
            "error"
        )

        raise

    finally:

        conn.close()

    return redirect(
        url_for("admin_credit")
    )


@app.route("/admin/credit/overdue")
def admin_credit_overdue():

    admin_id = session.get("user_id")

    if not admin_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT userId
            FROM Users
            WHERE userId = ?
              AND userRole = 1
              AND userActive = 1
        """, (admin_id,))

        admin = cursor.fetchone()

        if not admin:

            flash(
                "Administrator access required.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )

        customers = get_overdue_credit_customers(
            cursor
        )

        return render_template(
            "admin_credit_overdue.html",
            customers=customers
        )

    finally:

        conn.close()


@app.route("/account/credit")
def account_credit():
    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("getlogin"))

    conn = get_connexion()
    cursor = conn.cursor()

    try:
        # -----------------------------------------------------
        # Credit account summary
        # -----------------------------------------------------
        # The credit balance is derived from the new ledger:
        # CREDIT - REPAYMENT.
        # No legacy customer_credit_accounts table is used.
        credit_balance = get_customer_credit_balance(
            user_id,
            cursor
        )

        if not credit_balance["credit_customer"]:
            return render_template(
                "account_credit.html",
                account={
                    "credit_limit": 0,
                    "balance": 0,
                    "available_credit": 0,
                    "status": "inactive",
                    "credit_enabled": 0,
                    "payment_terms_days": 0
                },
                transactions=[]
            )

        account = {
            "credit_limit": credit_balance["credit_limit"],
            "balance": credit_balance["balance"],
            "available_credit": credit_balance["available_credit"],
            "status": credit_balance["status"],
            "credit_enabled": credit_balance["credit_enabled"],
            "payment_terms_days": (
                credit_balance["credit_customer"]["paymentTermsDays"] or 0
            )
        }

        # -----------------------------------------------------
        # Credit transaction history
        # -----------------------------------------------------
        cursor.execute("""
            SELECT
                ct.*,
                c.cmdNum
            FROM credit_transactions ct
            LEFT JOIN Commandes c
                ON c.cmdId = ct.commandId
            INNER JOIN credit_customers cc
                ON cc.creditCustomerId = ct.creditCustomerId
            WHERE cc.userId = ?
            ORDER BY ct.creditTransactionId DESC
        """, (user_id,))

        transactions = [
            dict(row)
            for row in cursor.fetchall()
        ]

        return render_template(
            "account_credit.html",
            account=account,
            transactions=transactions
        )

    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
import traceback
from flask import Flask, jsonify, request, session

# --- API ENDPOINTS ---


