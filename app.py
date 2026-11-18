from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse
import os
import re
import json
import html
import hmac
import time
import secrets
import hashlib
import uuid
import csv
import io
from datetime import date, timedelta
from db import get_db_connection
from http import cookies

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, 'uploads')
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')


# ---------------------------------------------------------------
# .env file (GEMINI_API_KEY, SECRET_KEY waghera) load karna
# ---------------------------------------------------------------
def load_env():
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env()

import chatbot  # env load hone ke baad import kiya hai

# ---------------- STRIPE SETUP ----------------
import stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
SITE_URL = os.environ.get('SITE_URL', 'http://localhost:8000')

# Cookie sign karne ki secret key. .env mein SECRET_KEY likh do, warna har restart par naya banega
SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

# Agar database mein products na hon to ye default products dikhte hain
DEFAULT_PRODUCTS = {
    "Itachi Uchiha - Akatsuki Crimson Oversized T-Shirt": 1999.00,
    "Streetwear Black Track Pants with Minimalist Print": 2999.00,
}

ALLOWED_IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}
ALLOWED_SIZES = {'Small', 'Medium', 'Large', 'X-Large'}
MAX_UPLOAD_BYTES = 6 * 1024 * 1024


# ---------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------
def esc(value):
    """HTML mein safe dikhane ke liye (XSS se bachao)"""
    return html.escape(str(value), quote=True)


def sign(value):
    return hmac.new(SECRET_KEY.encode('utf-8'), value.encode('utf-8'), hashlib.sha256).hexdigest()


def make_session(username, role):
    value = f"{username}|{role}"
    return f"{value}|{sign(value)}"


def read_session(raw):
    """Signed cookie check karta hai. Kisi ne cookie badli to None milega."""
    try:
        username, role, signature = raw.split('|')
    except ValueError:
        return None
    if hmac.compare_digest(signature, sign(f"{username}|{role}")):
        return username, role
    return None


def get_chatbot_widget_html():
    widget_path = os.path.join(BASE_DIR, 'chatbot_widget.html')
    if os.path.exists(widget_path):
        with open(widget_path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""


def inject_chatbot(page):
    """Chatbot widget ko page ke </body> se pehle laga deta hai"""
    widget = get_chatbot_widget_html()
    if '</body>' in page:
        return page.replace('</body>', widget + '</body>', 1)
    return page + widget


def get_product_price(name):
    """Price hamesha server (database) se lete hain, browser se nahi"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT price FROM products WHERE name = %s LIMIT 1", (name,))
            row = cursor.fetchone()
            cursor.close()
            if row:
                return float(row['price'])
        except Exception as e:
            print(f"Price lookup error: {e}")
        finally:
            conn.close()
    return DEFAULT_PRODUCTS.get(name)


def product_card(name, price, img_src, badge_html):
    name_js = esc(json.dumps(str(name)))
    price_js = esc(json.dumps(str(price)))
    return f"""
                    <div class="product-card">
                        <div class="product-img-container">
                            {badge_html}
                            <img src="/uploads/{esc(img_src)}" alt="{esc(name)}" onerror="this.src='https://via.placeholder.com/300x280?text=Fit+Verse'">
                        </div>
                        <div class="product-info">
                            <h3>{esc(name)}</h3>
                            <div class="price-row">
                                <div class="price">${esc(price)} USD</div>
                                <button class="buy-btn" onclick="openBuyModal({name_js}, {price_js})">CHOOSE OPTIONS</button>
                            </div>
                        </div>
                    </div>
                    """


ORDER_STATUSES = ['Pending', 'Processing', 'Shipped', 'Delivered', 'Cancelled']


def ensure_orders_columns(cursor):
    """Orders table mein date ka column na ho to bana deta hai (purane orders ki date khali rahegi)"""
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN created_at DATETIME NULL")
    except Exception:
        pass  # column pehle se maujood hai ya table abhi bana hi nahi


def log_chat(username, message, reply):
    """Chatbot ki baat-cheet dashboard ke Messages section ke liye save karta hai"""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                message TEXT NOT NULL,
                reply TEXT,
                created_at DATETIME NULL
            )
        """)
        cursor.execute(
            "INSERT INTO chat_messages (username, message, reply, created_at) VALUES (%s, %s, %s, NOW())",
            (username, message, reply)
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"Chat log error: {e}")
    finally:
        conn.close()


def fmt_dt(value):
    if not value:
        return "-"
    try:
        return value.strftime('%d %b %Y, %H:%M')
    except Exception:
        return str(value)


def csv_safe(value):
    """Excel mein formula chalne se rokta hai"""
    text = "" if value is None else str(value)
    if text[:1] in ('=', '+', '-', '@'):
        text = "'" + text
    return text


# Chat ke liye simple rate limit (ek IP se 1 minute mein max 10 messages)
chat_hits = {}

def is_rate_limited(ip, limit=10, window=60):
    now = time.time()
    hits = [t for t in chat_hits.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        chat_hits[ip] = hits
        return True
    hits.append(now)
    chat_hits[ip] = hits
    return False


class EcommerceHandler(BaseHTTPRequestHandler):

    # ---------- small helpers ----------
    def get_user(self):
        """Signed cookie se (username, role) nikalta hai"""
        cookie_header = self.headers.get('Cookie')
        if cookie_header:
            try:
                cookie = cookies.SimpleCookie(cookie_header)
                if 'session' in cookie:
                    result = read_session(cookie['session'].value)
                    if result:
                        return result
            except Exception:
                pass
        return "guest", ""

    def send_html(self, page, status=200):
        self.send_response(status)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(page.encode('utf-8'))

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def redirect(self, location, set_cookies=None):
        self.send_response(302)
        for c in (set_cookies or []):
            self.send_header('Set-Cookie', c)
        self.send_header('Location', location)
        self.end_headers()

    def read_body(self, max_bytes=1024 * 1024):
        length = int(self.headers.get('Content-Length', 0))
        if length > max_bytes:
            raise ValueError("Request too large")
        return self.rfile.read(length)

    def read_form(self):
        return urllib.parse.parse_qs(self.read_body().decode('utf-8'))

    # ---------- GET ----------
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query_params = urllib.parse.parse_qs(parsed_path.query)

        # Cookie check karna ke kaun logged in hai (ab cookie signed hai, fake nahi ho sakti)
        username, user_role = self.get_user()

        # Serve uploaded images directly from 'uploads' folder
        if path.startswith('/uploads/'):
            file_name = os.path.basename(urllib.parse.unquote(path))  # '../' wali tricks band
            file_path = os.path.join(UPLOADS_DIR, file_name)
            ext = os.path.splitext(file_name)[1].lower()
            if os.path.isfile(file_path) and ext in IMAGE_TYPES:
                self.send_response(200)
                self.send_header('Content-type', IMAGE_TYPES[ext])
                self.end_headers()
                with open(file_path, 'rb') as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Image not found")
                return

        elif path == '/' or path == '/home':
            conn = get_db_connection()
            shirts = []
            pants = []
            if conn:
                cursor = conn.cursor(dictionary=True)
                try:
                    cursor.execute("SELECT * FROM products WHERE category = 'shirt' OR category IS NULL")
                    shirts = cursor.fetchall()
                except Exception:
                    cursor.execute("SELECT * FROM products")
                    shirts = cursor.fetchall()

                try:
                    cursor.execute("SELECT * FROM products WHERE category = 'pants'")
                    pants = cursor.fetchall()
                except Exception:
                    pants = []

                cursor.close()
                conn.close()

            shirt_badge = '<span class="sale-badge">SALE</span>'
            pants_badge = '<span class="sale-badge" style="background: #333; color: #fff;">NEW</span>'

            shirts_html = ""
            if shirts:
                for p in shirts:
                    img_src = p.get('image_url') if p.get('image_url') else "fitverse.jpg"
                    shirts_html += product_card(p['name'], p['price'], img_src, shirt_badge)
            else:
                shirts_html = product_card("Itachi Uchiha - Akatsuki Crimson Oversized T-Shirt", "1999.00", "fitverse.jpg", shirt_badge)

            pants_html = ""
            if pants:
                for p in pants:
                    img_src = p.get('image_url') if p.get('image_url') else "fitverse.jpg"
                    pants_html += product_card(p['name'], p['price'], img_src, pants_badge)
            else:
                pants_html = product_card("Streetwear Black Track Pants with Minimalist Print", "2999.00", "fitverse.jpg", pants_badge)

            file_path = os.path.join(TEMPLATES_DIR, 'home.html')
            home_page = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Fit Verse - Best Anime Streetwear</title>
                <style>
                    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
                    body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f8f8f8; color: #111; }}

                    /* NAVBAR */
                    header {{ background: #fff; border-bottom: 1px solid #e5e5e5; padding: 15px 40px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 15px; position: sticky; top: 0; z-index: 100; }}
                    .logo {{ font-size: 22px; font-weight: 900; letter-spacing: 2px; color: #000; text-transform: uppercase; }}
                    .nav-categories {{ display: flex; align-items: center; flex-wrap: wrap; gap: 25px; list-style: none; }}
                    .nav-categories a {{ text-decoration: none; color: #333; font-size: 13px; font-weight: 600; text-transform: uppercase; }}
                    .nav-categories a:hover {{ color: #000; border-bottom: 2px solid #000; }}
                    .nav-links {{ display: flex; align-items: center; gap: 15px; }}
                    .nav-links a {{ color: #000; text-decoration: none; font-size: 13px; font-weight: 700; text-transform: uppercase; }}
                    .nav-links a:hover {{ text-decoration: underline; }}
                    .btn-admin-dash {{ background: #000; color: #fff !important; padding: 6px 12px; border-radius: 4px; }}

                    /* HERO */
                    .hero-container {{ display: flex; max-width: 1200px; margin: 30px auto; background: #000; border-radius: 6px; overflow: hidden; min-height: 380px; }}
                    .hero-image {{ flex: 1.2; background: url('/uploads/fitverse.jpg') center/cover no-repeat; min-height: 350px; }}
                    .hero-content {{ flex: 1; background: #0d0d0d; color: white; padding: 60px 40px; display: flex; flex-direction: column; justify-content: center; align-items: flex-start; }}
                    .hero-content h1 {{ font-size: 38px; font-weight: 900; margin-bottom: 10px; }}
                    .hero-content p {{ color: #aaa; font-size: 15px; margin-bottom: 25px; }}
                    .hero-btn {{ background: #fff; color: #000; padding: 12px 28px; font-weight: 800; font-size: 13px; text-decoration: none; border-radius: 3px; text-transform: uppercase; transition: 0.2s; }}
                    .hero-btn:hover {{ background: #ddd; }}

                    /* CATEGORY BANNER CARDS */
                    .category-banner-grid {{ max-width: 1200px; margin: 40px auto; padding: 0 20px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; }}
                    .cat-card {{ position: relative; height: 200px; border-radius: 6px; overflow: hidden; text-decoration: none; display: flex; align-items: flex-end; padding: 25px; background-size: cover; background-position: center; box-shadow: 0 4px 10px rgba(0,0,0,0.08); transition: 0.3s; }}
                    .cat-card::before {{ content: ''; position: absolute; inset: 0; background: linear-gradient(to top, rgba(0,0,0,0.8), rgba(0,0,0,0.1)); z-index: 1; }}
                    .cat-card:hover {{ transform: translateY(-3px); }}
                    .cat-content {{ position: relative; z-index: 2; }}
                    .cat-content h3 {{ font-size: 20px; font-weight: 900; color: #fff; text-transform: uppercase; }}
                    .cat-content span {{ font-size: 12px; color: #ddd; text-transform: uppercase; letter-spacing: 1px; font-weight: 700; }}

                    /* MAIN CONTAINER */
                    .container {{ max-width: 1200px; width: 100%; margin: 40px auto; padding: 0 20px; }}
                    .section-header {{ margin-bottom: 25px; border-bottom: 2px solid #e5e5e5; padding-bottom: 10px; }}
                    .section-header h2 {{ font-size: 24px; font-weight: 900; text-transform: uppercase; letter-spacing: 1px; }}
                    .section-header p {{ color: #666; font-size: 13px; margin-top: 5px; }}

                    /* PRODUCT GRID */
                    .product-grid {{ display: grid !important; grid-template-columns: repeat(4, minmax(0, 1fr)) !important; gap: 24px !important; width: 100% !important; margin-bottom: 40px; align-items: stretch; }}
                    .product-card {{ display: flex; flex-direction: column; width: 100% !important; height: 100%; background: #0a0a0a; color: white; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12); transition: transform 0.3s ease; }}
                    .product-card:hover {{ transform: translateY(-4px); }}

                    .product-img-container {{ position: relative; width: 100% !important; height: 300px !important; background: #1a1a1a; overflow: hidden; }}
                    .product-img-container img {{ display: block; width: 100% !important; height: 300px !important; object-fit: cover !important; transition: transform 0.3s ease; }}
                    .product-card:hover .product-img-container img {{ transform: scale(1.04); }}
                    .sale-badge {{ position: absolute; top: 10px; left: 10px; background: white; color: black; font-size: 11px; font-weight: 800; padding: 4px 8px; border-radius: 3px; z-index: 2; text-transform: uppercase; }}

                    .product-info {{ display: flex; flex-direction: column; gap: 10px; width: 100% !important; padding: 16px !important; flex: 1; }}
                    .product-info h3 {{ font-size: 15px; font-weight: 700; line-height: 1.4; height: 42px; overflow: hidden; }}
                    .price-row {{ margin-top: auto; padding-top: 8px; }}
                    .price {{ font-size: 17px; font-weight: 800; color: #fff; }}
                    .buy-btn {{ margin-top: 12px; background: #1f1f1f; color: #fff; border: 1px solid #444; padding: 12px; width: 100%; border-radius: 4px; font-weight: 700; font-size: 12px; cursor: pointer; transition: 0.2s; text-transform: uppercase; }}
                    .buy-btn:hover {{ background: #fff; color: #000; }}

                    /* BUY MODAL */
                    .modal {{ display: none; position: fixed; z-index: 1000; inset: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.7); justify-content: center; align-items: center; padding: 15px; }}
                    .modal-content {{ background: #fff; color: #111; padding: 30px; border-radius: 8px; width: 360px; max-width: 100%; position: relative; box-shadow: 0 5px 20px rgba(0,0,0,0.2); }}
                    .close-btn {{ position: absolute; right: 15px; top: 10px; font-size: 22px; cursor: pointer; color: #888; }}
                    .confirm-btn {{ background: #000; color: #fff; border: none; padding: 12px; width: 100%; border-radius: 4px; cursor: pointer; font-size: 14px; font-weight: 800; margin-top: 15px; text-transform: uppercase; }}
                    .size-btn {{ padding: 8px 14px; border: 1px solid #ccc; background: #fff; cursor: pointer; border-radius: 4px; font-weight: 700; font-size: 12px; }}
                    .size-btn.active {{ background: #000; color: #fff; border-color: #000; }}
                    .qty-box {{ display: flex; align-items: center; gap: 15px; margin-top: 5px; }}
                    .qty-btn {{ width: 34px; height: 34px; background: #f0f0f0; border: 1px solid #ccc; cursor: pointer; font-weight: bold; font-size: 16px; border-radius: 4px; display: flex; align-items: center; justify-content: center; }}

                    @media (max-width: 1024px) {{ .product-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }} .category-banner-grid {{ grid-template-columns: 1fr; }} }}
                    @media (max-width: 650px) {{ .product-grid {{ grid-template-columns: 1fr !important; }} }}
                </style>
            </head>
            <body>
                <header>
                    <div class="logo">Fit Verse</div>
                    <ul class="nav-categories">
                        <li><a href="/">Home</a></li>
                        <li><a href="#shirts-section">Shirts</a></li>
                        <li><a href="#pants-section">Pants</a></li>
                    </ul>
                    <div class="nav-links">
                        <!-- NAV_LINKS_PLACEHOLDER -->
                    </div>
                </header>

                <!-- HERO BANNER -->
                <div class="hero-container">
                    <div class="hero-image"></div>
                    <div class="hero-content">
                        <h1>Fit Verse</h1>
                        <p>Stitched to Perfection.</p>
                        <a href="#shirts-section" class="hero-btn">Shop Now</a>
                    </div>
                </div>

                <!-- CATEGORY INTERACTIVE BANNER CARDS -->
                <div class="category-banner-grid">
                    <a href="#shirts-section" class="cat-card" style="background-image: url('/uploads/fitverse.jpg');">
                        <div class="cat-content">
                            <span>Collection</span>
                            <h3>Oversized Shirts</h3>
                        </div>
                    </a>
                    <a href="#pants-section" class="cat-card" style="background-image: url('/uploads/fitverse.jpg');">
                        <div class="cat-content">
                            <span>Apparel</span>
                            <h3>Pants & Bottoms</h3>
                        </div>
                    </a>
                </div>

                <!-- MAIN CONTAINER -->
                <div class="container">

                    <!-- SECTION 1: SHIRTS -->
                    <div id="shirts-section" class="section-header">
                        <h2>Oversized Shirts</h2>
                        <p>Unfading prints, with peak comfort to boast.</p>
                    </div>

                    <div class="product-grid">
                        <!-- SHIRTS_GRID_PLACEHOLDER -->
                    </div>

                    <div style="margin: 50px 0; border-top: 1px solid #e5e5e5;"></div>

                    <!-- SECTION 2: PANTS -->
                    <div id="pants-section" class="section-header">
                        <h2>Pants & Bottoms</h2>
                        <p>Modern streetwear bottoms crafted for utility and style.</p>
                    </div>

                    <div class="product-grid">
                        <!-- PANTS_GRID_PLACEHOLDER -->
                    </div>

                </div>

                <!-- BUY MODAL -->
                <div id="buyModal" class="modal">
                    <div class="modal-content">
                        <span class="close-btn" onclick="closeBuyModal()">&times;</span>
                        <h3 id="modalProductName" style="margin-top: 0; font-size: 18px; font-weight: 800;">Product Name</h3>
                        <p id="modalProductPrice" style="color: #000; font-weight: 800; font-size: 16px; margin-top: 5px; margin-bottom: 15px;"></p>

                        <form onsubmit="checkout(event)">
                            <div style="margin-bottom: 15px;">
                                <label style="font-weight: 700; font-size: 12px; display: block; margin-bottom: 8px;">Select Size:</label>
                                <div style="display: flex; gap: 8px;">
                                    <button type="button" class="size-btn" onclick="selectSize('Small', this)">S</button>
                                    <button type="button" class="size-btn" onclick="selectSize('Medium', this)">M</button>
                                    <button type="button" class="size-btn" onclick="selectSize('Large', this)">L</button>
                                    <button type="button" class="size-btn" onclick="selectSize('X-Large', this)">XL</button>
                                </div>
                                <input type="hidden" id="selectedSize" required>
                            </div>

                            <div style="margin-bottom: 20px;">
                                <label style="font-weight: 700; font-size: 12px; display: block; margin-bottom: 8px;">Quantity:</label>
                                <div class="qty-box">
                                    <button type="button" class="qty-btn" onclick="changeQty(-1)">-</button>
                                    <span id="qtyDisplay" style="font-size: 15px; font-weight: 700; min-width: 20px; text-align: center;">1</span>
                                    <button type="button" class="qty-btn" onclick="changeQty(1)">+</button>
                                </div>
                            </div>

                            <button type="submit" class="confirm-btn">CONFIRM ORDER</button>
                        </form>
                    </div>
                </div>

                <script>
                    let currentSize = "";
                    let currentQty = 1;

                    function openBuyModal(name, price) {{
                        document.getElementById("modalProductName").innerText = name;
                        document.getElementById("modalProductPrice").innerText = "$" + price + " USD";
                        document.getElementById("buyModal").style.display = "flex";

                        currentSize = "";
                        document.getElementById("selectedSize").value = "";
                        currentQty = 1;
                        document.getElementById("qtyDisplay").innerText = 1;

                        let btns = document.getElementsByClassName("size-btn");
                        for (let b of btns) {{ b.classList.remove("active"); }}
                    }}

                    function closeBuyModal() {{
                        document.getElementById("buyModal").style.display = "none";
                    }}

                    function selectSize(size, btn) {{
                        currentSize = size;
                        document.getElementById("selectedSize").value = size;

                        let btns = document.getElementsByClassName("size-btn");
                        for (let b of btns) {{ b.classList.remove("active"); }}
                        btn.classList.add("active");
                    }}

                    function changeQty(amount) {{
                        currentQty += amount;
                        if (currentQty < 1) {{ currentQty = 1; }}
                        document.getElementById("qtyDisplay").innerText = currentQty;
                    }}

                    function checkout(event) {{
                        event.preventDefault();
                        if (!currentSize) {{
                            alert("Please select a size first!");
                            return;
                        }}

                        // Price server khud database se lega, yahan sirf product name bhejna kafi hai
                        let name = document.getElementById("modalProductName").innerText;

                        let form = document.createElement('form');
                        form.method = 'POST';
                        form.action = '/add-to-cart';

                        let fields = {{
                            product_name: name,
                            size: currentSize,
                            quantity: currentQty
                        }};

                        for (let key in fields) {{
                            let input = document.createElement('input');
                            input.type = 'hidden';
                            input.name = key;
                            input.value = fields[key];
                            form.appendChild(input);
                        }}

                        document.body.appendChild(form);
                        form.submit();
                    }}

                    window.onclick = function (event) {{
                        let modal = document.getElementById("buyModal");
                        if (event.target === modal) {{
                            modal.style.display = "none";
                        }}
                    }};
                </script>
            </body>
            </html>
            """

            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    home_page = f.read()

            home_page = home_page.replace('<!-- SHIRTS_GRID_PLACEHOLDER -->', shirts_html)
            home_page = home_page.replace('<!-- PANTS_GRID_PLACEHOLDER -->', pants_html)

            # Yahan role ke mutabiq nav buttons set kiye hain
            if user_role == 'admin':
                nav_html = '<a href="/cart">Cart 🛒</a><a href="/admin" class="btn-admin-dash">Dashboard</a><a href="/logout">Logout</a>'
            elif user_role == 'user':
                nav_html = f'<a href="/cart">Cart 🛒 ({esc(username)})</a><a href="/logout">Logout</a>'
            else:
                nav_html = '<a href="/cart">Cart 🛒</a><a href="/login">Login</a><a href="/register">Register</a>'

            home_page = home_page.replace('<!-- NAV_LINKS_PLACEHOLDER -->', nav_html)

            # Chatbot widget home page par bhi laga diya
            home_page = inject_chatbot(home_page)

            self.send_html(home_page)

        elif path == '/cart':
            # Cart ke liye login zaroori hai (pehle sab guests ek hi cart share karte thay)
            if username == "guest":
                self.redirect('/login')
                return

            conn = get_db_connection()
            orders_html = ""
            grand_total = 0
            if conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute("SELECT * FROM orders WHERE username = %s", (username,))
                user_orders = cursor.fetchall()
                cursor.close()
                conn.close()

                for o in user_orders:
                    # STRIPE CHANGE: sirf Pending orders total mein ginte hain (paid wale nahi)
                    if o['status'] == 'Pending':
                        grand_total += float(o['total_price'])
                    orders_html += f"""
                    <tr style="border-bottom: 1px solid #262626;">
                        <td style="padding: 14px; font-weight: 600; color: #fff;">{esc(o['product_name'])}</td>
                        <td style="padding: 14px; color: #aaa;">{esc(o['size'])}</td>
                        <td style="padding: 14px; color: #aaa;">{esc(o['quantity'])}</td>
                        <td style="padding: 14px; font-weight: bold; color: #00f0ff;">${esc(o['total_price'])} USD</td>
                        <td style="padding: 14px;"><span style="background: #002b3d; color: #00f0ff; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 12px; border: 1px solid #00f0ff;">{esc(o['status'])}</span></td>
                        <td style="padding: 14px;">
                            <form method="POST" action="/cart/delete" style="margin: 0;">
                                <input type="hidden" name="id" value="{esc(o['id'])}">
                                <button type="submit" style="background: none; border: none; cursor: pointer; color: #ff0055; font-weight: bold; font-size: 13px; padding: 0;">Remove</button>
                            </form>
                        </td>
                    </tr>
                    """

            cart_page = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <title>Shopping Cart - Fit Verse</title>
                <style>
                    body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0d0d0d; margin: 0; padding: 40px; color: #fff; }}
                    .container {{ max-width: 950px; margin: auto; background: #161616; padding: 40px; border-radius: 8px; border: 1px solid #00f0ff; box-shadow: 0 0 25px rgba(0, 240, 255, 0.25); }}
                    h2 {{ color: #00f0ff; border-bottom: 2px solid #00f0ff; padding-bottom: 15px; margin-top: 0; text-transform: uppercase; font-weight: 900; letter-spacing: 1px; text-shadow: 0 0 10px rgba(0, 240, 255, 0.4); }}
                    table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                    th {{ background: #1f1f1f; color: #00f0ff; padding: 14px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #333; }}
                    tr:hover {{ background: #1a1a1a; }}
                    .btn-payment {{ display: block; width: 100%; background: #00f0ff; color: #000; text-align: center; padding: 15px; text-decoration: none; font-weight: 800; border-radius: 4px; margin-top: 30px; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; transition: 0.3s; box-sizing: border-box; box-shadow: 0 0 15px rgba(0, 240, 255, 0.4); }}
                    .btn-payment:hover {{ background: #fff; color: #000; box-shadow: 0 0 25px rgba(0, 240, 255, 0.8); }}
                    .back-link {{ display: inline-block; margin-top: 20px; color: #00f0ff; text-decoration: none; font-weight: 700; font-size: 13px; text-transform: uppercase; }}
                    .back-link:hover {{ color: #fff; text-decoration: underline; text-shadow: 0 0 8px rgba(0, 240, 255, 0.6); }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h2>Your Shopping Cart ({esc(username)})</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Product Name</th>
                                <th>Size</th>
                                <th>Quantity</th>
                                <th>Total Price</th>
                                <th>Status</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            {orders_html if orders_html else '<tr><td colspan="6" style="text-align:center; padding:30px; color:#777;">Your cart is empty.</td></tr>'}
                        </tbody>
                    </table>

                    {f'<div style="margin-top: 25px; text-align: right; font-size: 18px; font-weight: 900; color: #00f0ff; text-shadow: 0 0 8px rgba(0, 240, 255, 0.4);">Grand Total: ${grand_total:.2f} USD</div>' if grand_total > 0 else ''}

                    {'<a href="/payment" class="btn-payment">Proceed to Payment</a>' if grand_total > 0 else ''}
                    <a href="/" class="back-link">&larr; Continue Shopping</a>
                </div>
            </body>
            </html>
            """
            self.send_html(inject_chatbot(cart_page))

        # ============ STRIPE: payment start ============
        elif path == '/payment':
            if username == "guest":
                self.redirect('/login')
                return

            pending = []
            conn = get_db_connection()
            if conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute("SELECT * FROM orders WHERE username = %s AND status = 'Pending'", (username,))
                pending = cursor.fetchall()
                cursor.close()
                conn.close()

            if not pending:
                self.redirect('/cart')
                return

            try:
                line_items = []
                for o in pending:
                    line_items.append({
                        "price_data": {
                            "currency": "usd",
                            "product_data": {"name": f"{o['product_name']} ({o['size']})"},
                            "unit_amount": int(round(float(o['price']) * 100)),  # cents
                        },
                        "quantity": int(o['quantity']),
                    })

                session = stripe.checkout.Session.create(
                    mode="payment",
                    line_items=line_items,
                    client_reference_id=username,
                    metadata={"order_ids": ",".join(str(o['id']) for o in pending)},
                    success_url=f"{SITE_URL}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=f"{SITE_URL}/cart",
                )
                self.redirect(session.url)
            except Exception as e:
                print(f"Stripe error: {e}")
                self.send_html("Payment start nahi ho saki. Dobara try karein.", 500)

        # ============ STRIPE: payment ke baad verify ============
        elif path == '/payment-success':
            if username == "guest":
                self.redirect('/login')
                return

            session_id = query_params.get('session_id', [''])[0]
            paid = False
            try:
                # Stripe se direct poochte hain, URL pe bharosa nahi
                session = stripe.checkout.Session.retrieve(session_id)
                if session.payment_status == 'paid' and session.client_reference_id == username:
                    ids = [int(i) for i in session.metadata["order_ids"].split(',') if i.isdigit()]
                    if ids:
                        conn = get_db_connection()
                        if conn:
                            cursor = conn.cursor()
                            marks = ','.join(['%s'] * len(ids))
                            cursor.execute(
                                f"UPDATE orders SET status = 'Processing' WHERE username = %s AND status = 'Pending' AND id IN ({marks})",
                                [username] + ids
                            )
                            conn.commit()
                            cursor.close()
                            conn.close()
                    paid = True
            except Exception as e:
                print(f"Payment verify error: {e}")

            if paid:
                msg = "<h2>Payment Successful ✅</h2><p>Shukriya! Aapka order process ho raha hai.</p>"
            else:
                msg = "<h2>Payment verify nahi ho saki</h2><p>Agar paisa kata hai to admin se rabta karein.</p>"

            success_page = f"""
            <!DOCTYPE html>
            <html><head><meta charset="UTF-8"><title>Payment - Fit Verse</title>
            <style>
                body {{ font-family: 'Segoe UI', sans-serif; background:#0d0d0d; color:#fff; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }}
                .box {{ background:#161616; border:1px solid #262626; padding:40px; border-radius:8px; width:400px; text-align:center; }}
                a {{ display:inline-block; margin-top:20px; color:#00f0ff; text-decoration:none; font-weight:700; }}
            </style></head>
            <body><div class="box">{msg}<a href="/cart">View My Orders</a> &nbsp; <a href="/">Home</a></div></body></html>
            """
            self.send_html(success_page)

        elif path == '/admin/export-orders':
            if user_role != 'admin':
                self.redirect('/login')
                return

            orders = []
            conn = get_db_connection()
            if conn:
                try:
                    cursor = conn.cursor(dictionary=True)
                    cursor.execute("SELECT * FROM orders ORDER BY id DESC")
                    orders = cursor.fetchall()
                    cursor.close()
                except Exception as e:
                    print(f"Export error: {e}")
                finally:
                    conn.close()

            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(['ID', 'Customer', 'Product', 'Size', 'Quantity', 'Price', 'Total', 'Status', 'Date'])
            for o in orders:
                writer.writerow([
                    o.get('id'), csv_safe(o.get('username')), csv_safe(o.get('product_name')), csv_safe(o.get('size')),
                    o.get('quantity'), o.get('price'), o.get('total_price'), csv_safe(o.get('status')),
                    fmt_dt(o.get('created_at')) if o.get('created_at') else ''
                ])

            data = ('\ufeff' + buffer.getvalue()).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="fitverse-orders.csv"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif path == '/admin':
            if user_role != 'admin':
                self.redirect('/login')
                return

            # Dashboard ka design alag file mein hai: templates/admin_dashboard.html
            template_path = os.path.join(TEMPLATES_DIR, 'admin_dashboard.html')
            if not os.path.exists(template_path):
                self.send_html("templates/admin_dashboard.html file nahi mili.", 500)
                return
            with open(template_path, 'r', encoding='utf-8') as f:
                admin_page = f.read()

            products = []
            all_orders = []
            customers = []
            messages = []
            stats = {'orders': 0, 'users': 0, 'value': 0.0, 'messages': 0, 'messages_today': 0, 'undated': 0}
            by_day = {}

            conn = get_db_connection()
            if conn:
                cursor = conn.cursor(dictionary=True)

                try:
                    cursor.execute("SELECT * FROM products ORDER BY id DESC")
                    products = cursor.fetchall()
                except Exception as e:
                    print(f"Admin products error: {e}")

                # Orders
                ensure_orders_columns(cursor)
                try:
                    cursor.execute("SELECT COUNT(*) AS c, COALESCE(SUM(total_price), 0) AS v FROM orders")
                    row = cursor.fetchone()
                    stats['orders'] = int(row['c'])
                    stats['value'] = float(row['v'])
                    cursor.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200")
                    all_orders = cursor.fetchall()
                except Exception as e:
                    print(f"Admin orders error: {e}")

                # Sales chart (pichle 30 din)
                try:
                    first_day = date.today() - timedelta(days=29)
                    cursor.execute(
                        "SELECT DATE(created_at) AS d, COUNT(*) AS c, COALESCE(SUM(total_price), 0) AS v "
                        "FROM orders WHERE created_at IS NOT NULL AND created_at >= %s GROUP BY DATE(created_at)",
                        (first_day,)
                    )
                    for r in cursor.fetchall():
                        by_day[str(r['d'])[:10]] = (int(r['c']), float(r['v']))
                    cursor.execute("SELECT COUNT(*) AS c FROM orders WHERE created_at IS NULL")
                    stats['undated'] = int(cursor.fetchone()['c'])
                except Exception as e:
                    print(f"Admin chart error: {e}")

                # Customers
                try:
                    cursor.execute(
                        "SELECT u.username, u.email, u.role, COUNT(o.id) AS orders_count, COALESCE(SUM(o.total_price), 0) AS spent "
                        "FROM users u LEFT JOIN orders o ON o.username = u.username "
                        "GROUP BY u.username, u.email, u.role ORDER BY spent DESC"
                    )
                    customers = cursor.fetchall()
                except Exception as e:
                    print(f"Admin customers error (fallback): {e}")
                    try:
                        cursor.execute("SELECT username, email FROM users")
                        customers = [dict(c, role='user', orders_count=0, spent=0) for c in cursor.fetchall()]
                    except Exception as e2:
                        print(f"Admin users error: {e2}")
                stats['users'] = len(customers)

                # Messages (chatbot)
                try:
                    cursor.execute("SELECT username, message, reply, created_at FROM chat_messages ORDER BY id DESC LIMIT 50")
                    messages = cursor.fetchall()
                    cursor.execute("SELECT COUNT(*) AS c FROM chat_messages")
                    stats['messages'] = int(cursor.fetchone()['c'])
                    cursor.execute("SELECT COUNT(*) AS c FROM chat_messages WHERE DATE(created_at) = CURDATE()")
                    stats['messages_today'] = int(cursor.fetchone()['c'])
                except Exception:
                    pass  # chat_messages table abhi bani nahi (pehla chat aane par ban jati hai)

                cursor.close()
                conn.close()

            # ---- Products table + category counts ----
            rows_html = ""
            category_counts = {}
            for p in products:
                category = p.get('category') or 'shirt'
                category_counts[category] = category_counts.get(category, 0) + 1
                img_src = p.get('image_url') if p.get('image_url') else "fitverse.jpg"
                rows_html += f"""
                                    <tr data-search="{esc((str(p['name']) + ' ' + category).lower())}">
                                        <td><img class="thumb" src="/uploads/{esc(img_src)}" alt=""></td>
                                        <td class="pname">{esc(p['name'])}</td>
                                        <td><span class="tag">{esc(category)}</span></td>
                                        <td class="price">${esc(p['price'])}</td>
                                        <td>
                                            <form method="POST" action="/admin/delete" style="margin: 0;" onsubmit="return confirm('Delete this product?');">
                                                <input type="hidden" name="id" value="{esc(p['id'])}">
                                                <button type="submit" class="btn-delete">Delete</button>
                                            </form>
                                        </td>
                                    </tr>
                """
            if not rows_html:
                rows_html = '<tr><td colspan="5" class="empty">No products found. "Add Product" se pehla product add karein.</td></tr>'

            # ---- Recent orders (overview) ----
            recent_html = ""
            for o in all_orders[:6]:
                recent_html += f"""
                                    <tr>
                                        <td>{esc(o['username'])}</td>
                                        <td class="pname">{esc(o['product_name'])}</td>
                                        <td>{esc(o['size'])}</td>
                                        <td>{esc(o['quantity'])}</td>
                                        <td class="price">${esc(o['total_price'])}</td>
                                        <td><span class="tag">{esc(o['status'])}</span></td>
                                    </tr>
                """
            if not recent_html:
                recent_html = '<tr><td colspan="6" class="empty">Abhi koi order nahi aaya.</td></tr>'

            # ---- All orders (status badalne ke saath) ----
            orders_html = ""
            for o in all_orders:
                current = o.get('status') or 'Pending'
                statuses = ORDER_STATUSES if current in ORDER_STATUSES else ORDER_STATUSES + [current]
                options = "".join(
                    f'<option value="{esc(s)}"{" selected" if s == current else ""}>{esc(s)}</option>' for s in statuses
                )
                orders_html += f"""
                                    <tr>
                                        <td class="muted">#{esc(o['id'])}</td>
                                        <td>{esc(o['username'])}</td>
                                        <td class="pname">{esc(o['product_name'])}</td>
                                        <td>{esc(o['size'])}</td>
                                        <td>{esc(o['quantity'])}</td>
                                        <td class="price">${esc(o['total_price'])}</td>
                                        <td class="muted">{esc(fmt_dt(o.get('created_at')))}</td>
                                        <td>
                                            <form method="POST" action="/admin/order-status" style="margin: 0;">
                                                <input type="hidden" name="id" value="{esc(o['id'])}">
                                                <select name="status" class="pill-select" onchange="this.form.submit()">{options}</select>
                                            </form>
                                        </td>
                                    </tr>
                """
            if not orders_html:
                orders_html = '<tr><td colspan="8" class="empty">Abhi koi order nahi aaya.</td></tr>'

            # ---- Customers ----
            customers_html = ""
            for c in customers:
                customers_html += f"""
                                    <tr>
                                        <td><div class="cell-user"><span class="avatar sm">{esc(str(c['username'])[:1])}</span>{esc(c['username'])}</div></td>
                                        <td class="muted">{esc(c.get('email') or '-')}</td>
                                        <td><span class="tag">{esc(c.get('role') or 'user')}</span></td>
                                        <td>{esc(c.get('orders_count', 0))}</td>
                                        <td class="price">${float(c.get('spent') or 0):,.2f}</td>
                                    </tr>
                """
            if not customers_html:
                customers_html = '<tr><td colspan="5" class="empty">Abhi koi customer register nahi hua.</td></tr>'

            # ---- Messages ----
            messages_html = ""
            for m in messages:
                who = m.get('username') or 'guest'
                messages_html += f"""
                        <div class="msg">
                            <span class="avatar sm">{esc(str(who)[:1])}</span>
                            <div class="msg-body">
                                <div class="msg-head"><b>{esc(who)}</b><span>{esc(fmt_dt(m.get('created_at')))}</span></div>
                                <div class="bubble user">{esc(m.get('message'))}</div>
                                <div class="bubble bot"><small>CHATBOT REPLY</small>{esc(m.get('reply') or '')}</div>
                            </div>
                        </div>
                """
            if not messages_html:
                messages_html = '<div class="empty">Abhi kisi customer ne chatbot se baat nahi ki.</div>'

            badge_html = f'<span class="badge">{stats["messages_today"]}</span>' if stats['messages_today'] else ''

            # ---- Sales chart data (30 din) ----
            chart = {'labels': [], 'sales': [], 'orders': []}
            for i in range(29, -1, -1):
                day = date.today() - timedelta(days=i)
                count, value = by_day.get(day.isoformat(), (0, 0.0))
                chart['labels'].append(day.strftime('%d %b'))
                chart['sales'].append(round(value, 2))
                chart['orders'].append(count)

            if stats['undated']:
                chart_note = f"Note: {stats['undated']} purane orders ki date record nahi thi, isliye chart mein sirf naye orders dikhte hain (total mein ye sab shamil hain)."
            else:
                chart_note = ""

            # ---- Message (success / error) ----
            alert_html = ""
            if query_params.get('added'):
                alert_html = '<div class="alert success">Product successfully add ho gaya.</div>'
            elif query_params.get('deleted'):
                alert_html = '<div class="alert success">Product delete ho gaya.</div>'
            elif query_params.get('updated'):
                alert_html = '<div class="alert success">Order status update ho gaya.</div>'
            elif query_params.get('pw'):
                alert_html = '<div class="alert success">Password change ho gaya.</div>'
            elif query_params.get('error'):
                alert_html = f'<div class="alert error">{esc(query_params["error"][0])}</div>'

            def safe_json(obj):
                return json.dumps(obj).replace('<', '\\u003c')

            replacements = {
                '<!-- ADMIN_NAME_PLACEHOLDER -->': esc(username),
                '<!-- ADMIN_INITIAL_PLACEHOLDER -->': esc(username[:1].upper()),
                '<!-- ALERT_PLACEHOLDER -->': alert_html,
                '<!-- STAT_PRODUCTS_PLACEHOLDER -->': str(len(products)),
                '<!-- STAT_ORDERS_PLACEHOLDER -->': str(stats['orders']),
                '<!-- STAT_USERS_PLACEHOLDER -->': str(stats['users']),
                '<!-- STAT_VALUE_PLACEHOLDER -->': f"${stats['value']:,.2f}",
                '<!-- PRODUCT_COUNT_PLACEHOLDER -->': str(len(products)),
                '<!-- PRODUCTS_LIST_PLACEHOLDER -->': rows_html,
                '<!-- RECENT_ORDERS_PLACEHOLDER -->': recent_html,
                '<!-- ALL_ORDERS_PLACEHOLDER -->': orders_html,
                '<!-- CUSTOMERS_PLACEHOLDER -->': customers_html,
                '<!-- MESSAGES_PLACEHOLDER -->': messages_html,
                '<!-- MESSAGES_COUNT_PLACEHOLDER -->': str(stats['messages']),
                '<!-- MESSAGE_BADGE_PLACEHOLDER -->': badge_html,
                '<!-- CHART_NOTE_PLACEHOLDER -->': esc(chart_note),
                '<!-- CATEGORY_JSON_PLACEHOLDER -->': safe_json([{"name": k, "count": v} for k, v in category_counts.items()]),
                '<!-- CHART_JSON_PLACEHOLDER -->': safe_json(chart),
            }
            for placeholder, value in replacements.items():
                admin_page = admin_page.replace(placeholder, value)

            self.send_html(admin_page)

        elif path == '/login':
            error_html = ""
            if query_params.get('error'):
                error_html = '<div style="background:#3d0011; border:1px solid #ff0055; color:#ff9db8; padding:10px; border-radius:4px; margin-bottom:15px; font-size:13px;">Invalid username or password.</div>'

            login_page = """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <title>Login - Fit Verse</title>
                <style>
                    body { font-family: 'Segoe UI', sans-serif; background: #0d0d0d; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                    .box { background: #161616; border: 1px solid #262626; padding: 40px; border-radius: 8px; width: 350px; }
                    h2 { margin-top: 0; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 20px; }
                    input { width: 100%; padding: 12px; margin-bottom: 15px; background: #0d0d0d; border: 1px solid #333; color: #fff; border-radius: 4px; box-sizing: border-box; }
                    button { width: 100%; padding: 12px; background: #fff; color: #000; border: none; font-weight: 800; cursor: pointer; border-radius: 4px; text-transform: uppercase; }
                    button:hover { background: #333; color: #fff; }
                </style>
            </head>
            <body>
                <div class="box">
                    <h2>Login</h2>
                    <!-- ERROR_PLACEHOLDER -->
                    <form method="POST" action="/login">
                        <input type="text" name="username" placeholder="Username" required>
                        <input type="password" name="password" placeholder="Password" required>
                        <button type="submit">Login</button>
                    </form>
                </div>
            </body>
            </html>
            """
            login_page = login_page.replace('<!-- ERROR_PLACEHOLDER -->', error_html)
            self.send_html(login_page)

        elif path == '/register':
            error_html = ""
            err = query_params.get('error', [''])[0]
            if err == 'exists':
                msg = "Username ya email pehle se maujood hai."
            elif err == 'invalid':
                msg = "Username 3-30 characters (letters, numbers, _) ka ho aur password kam az kam 6 characters ka."
            elif err:
                msg = "Registration nahi ho saki. Dobara try karein."
            else:
                msg = ""
            if msg:
                error_html = f'<div style="background:#3d0011; border:1px solid #ff0055; color:#ff9db8; padding:10px; border-radius:4px; margin-bottom:15px; font-size:13px;">{esc(msg)}</div>'

            register_page = """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <title>Register - Fit Verse</title>
                <style>
                    body { font-family: 'Segoe UI', sans-serif; background: #0d0d0d; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                    .box { background: #161616; border: 1px solid #262626; padding: 40px; border-radius: 8px; width: 350px; }
                    h2 { margin-top: 0; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 20px; }
                    input { width: 100%; padding: 12px; margin-bottom: 15px; background: #0d0d0d; border: 1px solid #333; color: #fff; border-radius: 4px; box-sizing: border-box; }
                    button { width: 100%; padding: 12px; background: #fff; color: #000; border: none; font-weight: 800; cursor: pointer; border-radius: 4px; text-transform: uppercase; }
                    button:hover { background: #333; color: #fff; }
                </style>
            </head>
            <body>
                <div class="box">
                    <h2>Register</h2>
                    <!-- ERROR_PLACEHOLDER -->
                    <form method="POST" action="/register">
                        <input type="text" name="username" placeholder="Username" minlength="3" maxlength="30" pattern="[A-Za-z0-9_]+" required>
                        <input type="email" name="email" placeholder="Email" required>
                        <input type="password" name="password" placeholder="Password (min 6 characters)" minlength="6" required>
                        <button type="submit">Register</button>
                    </form>
                </div>
            </body>
            </html>
            """
            register_page = register_page.replace('<!-- ERROR_PLACEHOLDER -->', error_html)
            self.send_html(register_page)

        elif path == '/logout':
            expired = 'Expires=Thu, 01 Jan 1970 00:00:00 GMT'
            self.redirect('/', [
                f'session=; Path=/; {expired}',
                f'role=; Path=/; {expired}',       # purani insecure cookies bhi saaf
                f'username=; Path=/; {expired}',
            ])

        else:
            self.send_html("404 Page Not Found", 404)

    # ---------- POST ----------
    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        username, user_role = self.get_user()

        if path == '/api/chat':
            try:
                if is_rate_limited(self.client_address[0]):
                    self.send_json({"reply": "Bohat zyada messages ho gaye, thori der baad try karein."}, 429)
                    return

                data = json.loads(self.read_body(20000).decode('utf-8'))
                message = str(data.get('message', '')).strip()[:500]
                if not message:
                    self.send_json({"reply": "Please provide a message."}, 400)
                    return

                history = []
                for item in (data.get('history') or [])[-6:]:
                    if isinstance(item, dict) and item.get('role') in ('user', 'model'):
                        history.append({"role": item['role'], "text": str(item.get('text', ''))[:500]})

                reply = chatbot.ask_gemini(message, history)
                log_chat(username, message, reply)
                self.send_json({"reply": reply})
            except Exception as e:
                print(f"Chat Error: {e}")
                self.send_json({"reply": "Sorry, kuch masla aa gaya."}, 500)

        elif path == '/login':
            try:
                parsed_data = self.read_form()
                login_name = parsed_data.get('username', [''])[0]
                password = parsed_data.get('password', [''])[0]
                hashed_password = hashlib.sha256(password.encode('utf-8')).hexdigest()

                conn = get_db_connection()
                if conn:
                    cursor = conn.cursor(dictionary=True)
                    cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (login_name, hashed_password))
                    user = cursor.fetchone()
                    cursor.close()
                    conn.close()

                    if user:
                        role = user.get('role', 'user')
                        if not role:
                            role = 'user'
                        session_value = make_session(user['username'], role)
                        expired = 'Expires=Thu, 01 Jan 1970 00:00:00 GMT'
                        self.redirect('/', [
                            f"session={session_value}; Path=/; HttpOnly; SameSite=Lax",
                            f"role=; Path=/; {expired}",
                            f"username=; Path=/; {expired}",
                        ])
                        return

                self.redirect('/login?error=1')
            except Exception as e:
                print(f"Login Error: {e}")
                self.send_html("Login error. Please try again.", 500)

        elif path == '/register':
            try:
                parsed_data = self.read_form()
                reg_name = parsed_data.get('username', [''])[0].strip()
                email = parsed_data.get('email', [''])[0].strip()
                password = parsed_data.get('password', [''])[0]

                if not re.fullmatch(r'[A-Za-z0-9_]{3,30}', reg_name) or len(password) < 6 or not email or len(email) > 255:
                    self.redirect('/register?error=invalid')
                    return

                hashed_password = hashlib.sha256(password.encode('utf-8')).hexdigest()

                conn = get_db_connection()
                if conn:
                    try:
                        cursor = conn.cursor()
                        cursor.execute("INSERT INTO users (username, email, password) VALUES (%s, %s, %s)", (reg_name, email, hashed_password))
                        conn.commit()
                        cursor.close()
                        self.redirect('/login')
                        return
                    except Exception as e:
                        print(f"Register Error: {e}")
                        self.redirect('/register?error=exists')
                        return
                    finally:
                        conn.close()

                self.redirect('/register?error=failed')
            except Exception as e:
                print(f"Register Error: {e}")
                self.redirect('/register?error=failed')

        elif path == '/add-to-cart':
            try:
                if username == "guest":
                    self.redirect('/login')
                    return

                parsed_data = self.read_form()
                product_name = parsed_data.get('product_name', [''])[0]
                size = parsed_data.get('size', ['Medium'])[0]
                quantity = int(parsed_data.get('quantity', [1])[0])

                if size not in ALLOWED_SIZES:
                    size = 'Medium'
                quantity = max(1, min(quantity, 20))

                # Price browser se nahi, server (database) se aata hai
                price = get_product_price(product_name)
                if price is None:
                    self.send_html("Product not found.", 400)
                    return
                total_price = price * quantity

                conn = get_db_connection()
                if conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS orders (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            username VARCHAR(100) NOT NULL,
                            product_name VARCHAR(255) NOT NULL,
                            price DECIMAL(10,2) NOT NULL,
                            size VARCHAR(50) NOT NULL,
                            quantity INT NOT NULL,
                            total_price DECIMAL(10,2) NOT NULL,
                            status VARCHAR(50) DEFAULT 'Pending',
                            created_at DATETIME NULL
                        )
                    """)
                    ensure_orders_columns(cursor)
                    cursor.execute(
                        "INSERT INTO orders (username, product_name, price, size, quantity, total_price, created_at) VALUES (%s, %s, %s, %s, %s, %s, NOW())",
                        (username, product_name, price, size, quantity, total_price)
                    )
                    conn.commit()
                    cursor.close()
                    conn.close()

                self.redirect('/cart')
            except Exception as e:
                print(f"Add to cart error: {e}")
                self.send_html("Internal Server Error", 500)

        elif path == '/cart/delete':
            if username == "guest":
                self.redirect('/login')
                return
            try:
                order_id = self.read_form().get('id', [''])[0]
                if order_id:
                    conn = get_db_connection()
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM orders WHERE id = %s AND username = %s", (order_id, username))
                        conn.commit()
                        cursor.close()
                        conn.close()
            except Exception as e:
                print(f"Cart delete error: {e}")
            self.redirect('/cart')

        elif path == '/admin/delete':
            if user_role != 'admin':
                self.redirect('/login')
                return
            try:
                product_id = self.read_form().get('id', [''])[0]
                if product_id:
                    conn = get_db_connection()
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))
                        conn.commit()
                        cursor.close()
                        conn.close()
            except Exception as e:
                print(f"Admin delete error: {e}")
            self.redirect('/admin?deleted=1#products')

        elif path == '/admin/order-status':
            if user_role != 'admin':
                self.redirect('/login')
                return
            try:
                form = self.read_form()
                order_id = form.get('id', [''])[0]
                status = form.get('status', [''])[0]
                if order_id and status in ORDER_STATUSES:
                    conn = get_db_connection()
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("UPDATE orders SET status = %s WHERE id = %s", (status, order_id))
                        conn.commit()
                        cursor.close()
                        conn.close()
            except Exception as e:
                print(f"Order status error: {e}")
            self.redirect('/admin?updated=1#orders')

        elif path == '/admin/change-password':
            if user_role != 'admin':
                self.redirect('/login')
                return
            try:
                form = self.read_form()
                current = form.get('current_password', [''])[0]
                new = form.get('new_password', [''])[0]
                confirm = form.get('confirm_password', [''])[0]

                if len(new) < 6:
                    self.redirect('/admin?error=New+password+kam+az+kam+6+characters+ka+ho#settings')
                    return
                if new != confirm:
                    self.redirect('/admin?error=New+password+aur+confirm+password+match+nahi+kiye#settings')
                    return

                current_hash = hashlib.sha256(current.encode('utf-8')).hexdigest()
                new_hash = hashlib.sha256(new.encode('utf-8')).hexdigest()

                changed = False
                conn = get_db_connection()
                if conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE users SET password = %s WHERE username = %s AND password = %s", (new_hash, username, current_hash))
                    changed = cursor.rowcount > 0
                    conn.commit()
                    cursor.close()
                    conn.close()

                if changed:
                    self.redirect('/admin?pw=1#settings')
                else:
                    self.redirect('/admin?error=Current+password+galat+hai#settings')
            except Exception as e:
                print(f"Change password error: {e}")
                self.redirect('/admin?error=Password+change+nahi+ho+saka#settings')

        elif path == '/admin/add-product':
            if user_role != 'admin':
                self.redirect('/login')
                return
            try:
                content_type = self.headers.get('Content-Type', '')
                if 'multipart/form-data' in content_type:
                    boundary = content_type.split("boundary=")[1].encode('utf-8')
                    body = self.read_body(MAX_UPLOAD_BYTES + 100000)

                    parts = body.split(b'--' + boundary)
                    form_data = {}
                    filename = ""
                    file_content = b""

                    for part in parts:
                        if b'Content-Disposition' in part and b'\r\n\r\n' in part:
                            header_part, data_part = part.split(b'\r\n\r\n', 1)
                            data_part = data_part.rstrip(b'\r\n')

                            if b'name="name"' in header_part:
                                form_data['name'] = data_part.decode('utf-8', errors='ignore')
                            elif b'name="price"' in header_part:
                                form_data['price'] = data_part.decode('utf-8', errors='ignore')
                            elif b'name="category"' in header_part:
                                form_data['category'] = data_part.decode('utf-8', errors='ignore')
                            elif b'filename="' in header_part:
                                fn_start = header_part.find(b'filename="') + 10
                                fn_end = header_part.find(b'"', fn_start)
                                filename = header_part[fn_start:fn_end].decode('utf-8', errors='ignore')
                                file_content = data_part

                    # ---- validation ----
                    name = form_data.get('name', '').strip()[:255]
                    try:
                        price = float(form_data.get('price', ''))
                        if price <= 0:
                            raise ValueError
                    except ValueError:
                        self.redirect('/admin?error=Invalid+price#add-product')
                        return
                    if not name:
                        self.redirect('/admin?error=Product+name+required#add-product')
                        return

                    category = re.sub(r'[^a-z0-9 _-]', '', form_data.get('category', 'shirt').lower().strip())[:100] or 'shirt'

                    image_url = "fitverse.jpg"
                    if filename:
                        safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(filename))
                        ext = os.path.splitext(safe_name)[1].lower()
                        if ext not in ALLOWED_IMAGE_EXT:
                            self.redirect('/admin?error=Only+png,+jpg,+gif,+webp+images+allowed#add-product')
                            return
                        if len(file_content) > MAX_UPLOAD_BYTES:
                            self.redirect('/admin?error=Image+too+large+(max+5MB)#add-product')
                            return
                        if not os.path.exists(UPLOADS_DIR):
                            os.makedirs(UPLOADS_DIR)
                        unique_filename = f"{uuid.uuid4()}_{safe_name}"
                        with open(os.path.join(UPLOADS_DIR, unique_filename), 'wb') as f:
                            f.write(file_content)
                        image_url = unique_filename

                    conn = get_db_connection()
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            CREATE TABLE IF NOT EXISTS products (
                                id INT AUTO_INCREMENT PRIMARY KEY,
                                name VARCHAR(255) NOT NULL,
                                price DECIMAL(10,2) NOT NULL,
                                category VARCHAR(100) DEFAULT 'shirt',
                                image_url VARCHAR(255)
                            )
                        """)
                        try:
                            cursor.execute("ALTER TABLE products ADD COLUMN category VARCHAR(100) DEFAULT 'shirt'")
                        except Exception:
                            pass  # column pehle se maujood hai

                        cursor.execute(
                            "INSERT INTO products (name, price, category, image_url) VALUES (%s, %s, %s, %s)",
                            (name, price, category, image_url)
                        )
                        conn.commit()
                        cursor.close()
                        conn.close()

                self.redirect('/admin?added=1#products')
            except Exception as e:
                print(f"Add product error: {e}")
                self.send_html("Error adding product.", 500)

        else:
            self.send_html("404 Page Not Found", 404)


def run(server_class=ThreadingHTTPServer, handler_class=EcommerceHandler, port=8000):
    server_address = ('', port)
    httpd = server_class(server_address, handler_class)
    print(f"Fit Verse server running on http://localhost:{port}")
    httpd.serve_forever()

if __name__ == '__main__':
    run()