"""Tailor Shop App - Flask + MySQL.
Customer QR scan -> form -> order saved. Owner dashboard with details, prices, status, payments.
"""
import os
import re
import hmac
import uuid
import secrets
from io import BytesIO
from datetime import date
from functools import wraps
from urllib.parse import quote

import mysql.connector
import qrcode
from dotenv import load_dotenv
from flask import (Flask, render_template, request, redirect, url_for, session,
                   flash, abort, send_file, send_from_directory)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DB_CFG = dict(host=os.getenv("DB_HOST", "localhost"),
              user=os.getenv("DB_USER", "root"),
              password=os.getenv("DB_PASSWORD", ""))
DB_NAME = os.getenv("DB_NAME", "tailor_shop")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SHOP_NAME = os.getenv("SHOP_NAME", "Mera Tailor Shop")

STATUSES = ["Received", "Cutting", "Stitching", "Ready", "Delivered"]
MEAS = [("chest", "Chest / Chhati"), ("waist", "Waist / Kamar"), ("hip", "Hip"),
        ("shoulder", "Shoulder / Kandha"), ("sleeve", "Sleeve / Aasteen"),
        ("length", "Length / Lambai"), ("neck", "Neck / Gala"), ("thigh", "Thigh / Jaangh")]
DEFAULT_ITEMS = ["Shirt", "Pant", "Suit (2 pc)", "Suit (3 pc)", "Kurta-Pajama",
                 "Sherwani", "Waistcoat", "Alteration"]
DEFAULT_EXTRAS = ["Lining", "Double stitch", "Embroidery", "Extra pocket", "Urgent delivery"]
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
KINDS = {"items": ("price_list", "item_type", "base_price"),
         "extras": ("extras", "extra_name", "extra_price")}

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB photo limit


# ---------------------------------------------------------------- database
def get_conn(with_db=True):
    cfg = dict(DB_CFG)
    if with_db:
        cfg["database"] = DB_NAME
    return mysql.connector.connect(**cfg)


def run(sql, args=(), fetch=None):
    """fetch='one' | 'all' | None (None = write; returns last inserted id)."""
    conn = get_conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, args)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def init_db():
    conn = get_conn(with_db=False)
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}`")
    conn.database = DB_NAME
    meas_cols = ", ".join(f"{k} DECIMAL(5,2) NULL" for k, _ in MEAS)
    tables = [
        """CREATE TABLE IF NOT EXISTS customers (
            customer_id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            phone VARCHAR(15) NOT NULL UNIQUE,
            address VARCHAR(255),
            created_at DATE)""",
        """CREATE TABLE IF NOT EXISTS price_list (
            item_type VARCHAR(50) PRIMARY KEY,
            base_price INT NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS extras (
            extra_name VARCHAR(50) PRIMARY KEY,
            extra_price INT NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS orders (
            order_id INT AUTO_INCREMENT PRIMARY KEY,
            customer_id INT NOT NULL,
            item_type VARCHAR(50) NOT NULL,
            extras VARCHAR(255),
            design_photo VARCHAR(255),
            notes TEXT,
            base_price INT NOT NULL DEFAULT 0,
            extras_price INT NOT NULL DEFAULT 0,
            total_price INT NOT NULL DEFAULT 0,
            advance_paid INT NOT NULL DEFAULT 0,
            status ENUM('Received','Cutting','Stitching','Ready','Delivered') NOT NULL DEFAULT 'Received',
            order_date DATE,
            delivery_date DATE NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id))""",
        f"""CREATE TABLE IF NOT EXISTS measurements (
            measurement_id INT AUTO_INCREMENT PRIMARY KEY,
            order_id INT NOT NULL,
            {meas_cols},
            FOREIGN KEY (order_id) REFERENCES orders(order_id))""",
    ]
    for t in tables:
        cur.execute(t)
    for name in DEFAULT_ITEMS:
        cur.execute("INSERT IGNORE INTO price_list (item_type, base_price) VALUES (%s, 0)", (name,))
    for name in DEFAULT_EXTRAS:
        cur.execute("INSERT IGNORE INTO extras (extra_name, extra_price) VALUES (%s, 0)", (name,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- helpers
def clean_phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    return d if re.fullmatch(r"\d{10}", d) else None


def parse_measurements(form):
    vals = {}
    for key, _ in MEAS:
        raw = form.get("m_" + key, "").strip()
        try:
            v = float(raw) if raw else None
        except ValueError:
            v = None
        if v is not None and not (0 < v < 200):
            v = None
        vals[key] = v
    return vals


def save_photo(file):
    """Returns (filename, error)."""
    if not file or not file.filename:
        return None, None
    ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        return None, "Photo sirf JPG, PNG ya WEBP honi chahiye."
    name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, name))
    return name, None


def whatsapp_link(order):
    phone = "91" + order["phone"]
    balance = order["total_price"] - order["advance_paid"]
    if order["status"] in ("Ready", "Delivered"):
        text = (f"Namaste {order['name']}, aapka order #{order['order_id']} "
                f"({order['item_type']}) ready hai. Baaki payment: Rs {balance}. Dhanyavaad!")
    else:
        text = (f"Namaste {order['name']}, aapka order #{order['order_id']} "
                f"({order['item_type']}) mil gaya hai. Status: {order['status']}. Dhanyavaad!")
    return f"https://wa.me/{phone}?text={quote(text)}"


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return wrapper


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("csrf", "")
        sent = request.form.get("csrf", "")
        if not token or not hmac.compare_digest(token, sent):
            abort(400)


@app.context_processor
def inject_globals():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return dict(csrf=session["csrf"], shop_name=SHOP_NAME, statuses=STATUSES)


# ---------------------------------------------------------------- customer side
@app.route("/")
def home():
    return redirect(url_for("order_form"))


@app.route("/order", methods=["GET", "POST"])
def order_form():
    items = run("SELECT item_type, base_price FROM price_list ORDER BY item_type", fetch="all")
    extras = run("SELECT extra_name, extra_price FROM extras ORDER BY extra_name", fetch="all")
    item_prices = {i["item_type"]: i["base_price"] for i in items}
    extra_prices = {e["extra_name"]: e["extra_price"] for e in extras}
    ctx = dict(items=items, extras=extras, item_prices=item_prices,
               extra_prices=extra_prices, meas=MEAS, form={}, form_extras=[])

    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()[:100]
        phone = clean_phone(form.get("phone"))
        address = form.get("address", "").strip()[:255]
        item = form.get("item_type", "")
        notes = form.get("notes", "").strip()[:1000]
        chosen = [e for e in form.getlist("extras") if e in extra_prices]
        ctx.update(form=form, form_extras=chosen)

        errors = []
        if not name:
            errors.append("Naam likhna zaroori hai.")
        if not phone:
            errors.append("10 digit ka sahi mobile number daalein.")
        if item not in item_prices:
            errors.append("Item chuniye.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("order_form.html", **ctx)

        photo, err = save_photo(request.files.get("design"))
        if err:
            flash(err, "error")
            return render_template("order_form.html", **ctx)

        base = item_prices[item]
        extra_total = sum(extra_prices[e] for e in chosen)
        total = base + extra_total

        cust = run("SELECT customer_id FROM customers WHERE phone=%s", (phone,), fetch="one")
        if cust:
            cid = cust["customer_id"]
            run("UPDATE customers SET name=%s, address=COALESCE(NULLIF(%s,''), address) "
                "WHERE customer_id=%s", (name, address, cid))
        else:
            cid = run("INSERT INTO customers (name, phone, address, created_at) VALUES (%s,%s,%s,%s)",
                      (name, phone, address or None, date.today()))

        oid = run("INSERT INTO orders (customer_id, item_type, extras, design_photo, notes, "
                  "base_price, extras_price, total_price, order_date) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (cid, item, ", ".join(chosen) or None, photo, notes or None,
                   base, extra_total, total, date.today()))

        m = parse_measurements(form)
        if any(v is not None for v in m.values()):
            cols = ", ".join(m.keys())
            marks = ", ".join(["%s"] * len(m))
            run(f"INSERT INTO measurements (order_id, {cols}) VALUES (%s, {marks})",
                (oid, *m.values()))

        session["last_order"] = {"id": oid, "total": total}
        return redirect(url_for("thanks"))

    return render_template("order_form.html", **ctx)


@app.route("/thanks")
def thanks():
    last = session.get("last_order")
    if not last:
        return redirect(url_for("order_form"))
    return render_template("thanks.html", last=last)


# ---------------------------------------------------------------- owner login
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if ADMIN_PASSWORD and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            nxt = request.args.get("next", "")
            session.clear()
            session["admin"] = True
            if not (nxt.startswith("/") and not nxt.startswith("//")):
                nxt = url_for("dashboard")
            return redirect(nxt)
        flash("Password galat hai.", "error")
    return render_template("login.html", no_password=not ADMIN_PASSWORD)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- owner pages
@app.route("/admin")
@login_required
def dashboard():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    sql = ("SELECT o.*, c.name, c.phone FROM orders o "
           "JOIN customers c ON c.customer_id=o.customer_id WHERE 1=1")
    args = []
    if q:
        sql += " AND (c.name LIKE %s OR c.phone LIKE %s OR o.order_id=%s)"
        like = f"%{q}%"
        args += [like, like, int(q) if q.isdigit() else -1]
    if status in STATUSES:
        sql += " AND o.status=%s"
        args.append(status)
    sql += " ORDER BY o.order_id DESC LIMIT 200"
    orders = run(sql, args, fetch="all")

    counts = {r["status"]: r["n"] for r in
              run("SELECT status, COUNT(*) AS n FROM orders GROUP BY status", fetch="all")}
    due = run("SELECT COALESCE(SUM(total_price - advance_paid),0) AS due FROM orders", fetch="one")
    late = run("SELECT COUNT(*) AS n FROM orders WHERE delivery_date IS NOT NULL "
               "AND delivery_date <= %s AND status <> 'Delivered'", (date.today(),), fetch="one")
    unpriced = run("SELECT COUNT(*) AS n FROM price_list WHERE base_price=0", fetch="one")
    return render_template("dashboard.html", orders=orders, q=q, status=status, counts=counts,
                           due=int(due["due"]), late=late["n"], unpriced=unpriced["n"])


def get_order(oid):
    o = run("SELECT o.*, c.name, c.phone, c.address FROM orders o "
            "JOIN customers c ON c.customer_id=o.customer_id WHERE o.order_id=%s",
            (oid,), fetch="one")
    if not o:
        abort(404)
    return o


@app.route("/admin/order/<int:oid>")
@login_required
def order_detail(oid):
    o = get_order(oid)
    m = run("SELECT * FROM measurements WHERE order_id=%s", (oid,), fetch="one")
    meas = [(label, m[k]) for k, label in MEAS if m and m[k] is not None]
    return render_template("order_detail.html", o=o, meas=meas, wa=whatsapp_link(o))


@app.route("/admin/order/<int:oid>/update", methods=["POST"])
@login_required
def order_update(oid):
    o = get_order(oid)
    status = request.form.get("status")
    if status not in STATUSES:
        abort(400)

    delivery = request.form.get("delivery_date", "").strip() or None
    if delivery:
        try:
            date.fromisoformat(delivery)
        except ValueError:
            delivery = None
            flash("Delivery date sahi nahi thi.", "error")

    total = o["total_price"]
    raw_total = request.form.get("total_price", "").strip()
    if raw_total.isdigit():
        total = int(raw_total)

    advance = o["advance_paid"]
    pay = request.form.get("add_payment", "").strip()
    if pay:
        if pay.isdigit():
            advance += int(pay)
        else:
            flash("Payment sirf number mein likhein.", "error")
    advance = min(advance, total)

    run("UPDATE orders SET status=%s, delivery_date=%s, total_price=%s, advance_paid=%s "
        "WHERE order_id=%s", (status, delivery, total, advance, oid))
    flash("Order update ho gaya.", "ok")
    return redirect(url_for("order_detail", oid=oid))


@app.route("/admin/customer/<int:cid>")
@login_required
def customer_page(cid):
    c = run("SELECT * FROM customers WHERE customer_id=%s", (cid,), fetch="one")
    if not c:
        abort(404)
    orders = run("SELECT * FROM orders WHERE customer_id=%s ORDER BY order_id DESC", (cid,), fetch="all")
    rows = run("SELECT o.item_type, m.* FROM measurements m JOIN orders o ON o.order_id=m.order_id "
               "WHERE o.customer_id=%s ORDER BY o.order_id DESC", (cid,), fetch="all")
    latest = {}
    for r in rows:
        latest.setdefault(r["item_type"], [(label, r[k]) for k, label in MEAS if r[k] is not None])
    balance = sum(o["total_price"] - o["advance_paid"] for o in orders)
    return render_template("customer.html", c=c, orders=orders, latest=latest, balance=balance)


@app.route("/admin/prices")
@login_required
def prices():
    items = run("SELECT * FROM price_list ORDER BY item_type", fetch="all")
    extras = run("SELECT * FROM extras ORDER BY extra_name", fetch="all")
    return render_template("prices.html", items=items, extras=extras)


@app.route("/admin/prices/save", methods=["POST"])
@login_required
def prices_save():
    kind = request.form.get("kind")
    if kind not in KINDS:
        abort(400)
    table, namecol, pricecol = KINDS[kind]
    name = request.form.get("name", "").strip()[:50]
    price = request.form.get("price", "").strip()
    if not name or not price.isdigit():
        flash("Naam aur price (number) dono likhein.", "error")
    else:
        run(f"REPLACE INTO {table} ({namecol}, {pricecol}) VALUES (%s, %s)", (name, int(price)))
        flash("Price save ho gaya.", "ok")
    return redirect(url_for("prices"))


@app.route("/admin/prices/delete", methods=["POST"])
@login_required
def prices_delete():
    kind = request.form.get("kind")
    if kind not in KINDS:
        abort(400)
    table, namecol, _ = KINDS[kind]
    run(f"DELETE FROM {table} WHERE {namecol}=%s", (request.form.get("name", ""),))
    flash("Hata diya gaya.", "ok")
    return redirect(url_for("prices"))


@app.route("/admin/qr")
@login_required
def qr_page():
    default = os.getenv("SHOP_URL") or request.host_url.rstrip("/") + "/order"
    url = request.args.get("url", default).strip()[:300]
    local = "localhost" in url or "127.0.0.1" in url
    return render_template("qr.html", url=url, local=local)


@app.route("/admin/qr.png")
@login_required
def qr_png():
    url = request.args.get("url", "")[:300]
    if not url.startswith(("http://", "https://")):
        abort(400)
    buf = BytesIO()
    qrcode.make(url, box_size=10, border=2).save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/uploads/<path:name>")
@login_required
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name)


if __name__ == "__main__":
    try:
        init_db()
    except mysql.connector.Error as e:
        raise SystemExit(f"\nMySQL se connect nahi hua: {e}\n"
                         ".env file mein DB_USER / DB_PASSWORD check karein "
                         "aur MySQL server chalu hai ya nahi dekhein.\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
