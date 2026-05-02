import os
import sqlite3
import html
import io
import requests
import tempfile
import csv
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, get_flashed_messages, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from fpdf import FPDF
from flask_mail import Mail, Message

# Attempt to import Pillow for circular image processing
try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Change in production
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

# Email configuration
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = app.config['MAIL_USERNAME']

mail = Mail(app)

def send_email(to, subject, body):
    """Send plain text email using Flask-Mail."""
    msg = Message(subject,
                  recipients=[to])
    msg.body = body
    mail.send(msg)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ---------- Database with Migration ----------
def get_db():
    conn = sqlite3.connect('grievance.db')
    conn.row_factory = sqlite3.Row
    return conn

def column_exists(table, column):
    conn = get_db()
    cursor = conn.execute(f"PRAGMA table_info({table})")
    cols = [row['name'] for row in cursor.fetchall()]
    conn.close()
    return column in cols

def add_column_if_not_exists(table, column, coltype):
    if not column_exists(table, column):
        conn = get_db()
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        conn.commit()
        conn.close()
        print(f"Added column {column} to {table}")

def init_db():
    conn = get_db()
    c = conn.cursor()
    # Students
    c.execute('''CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        roll_number TEXT UNIQUE NOT NULL,
        branch TEXT NOT NULL,
        phone TEXT,
        address TEXT,
        telegram TEXT,
        instagram TEXT,
        twitter TEXT,
        emergency_contact TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Admins
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )''')
    # Grievances
    c.execute('''CREATE TABLE IF NOT EXISTS grievances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        attachment TEXT,
        priority TEXT DEFAULT 'Medium',
        status TEXT DEFAULT 'Pending',
        remarks TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students (id)
    )''')
    # Help Desk
    c.execute('''CREATE TABLE IF NOT EXISTS helpdesk (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        message TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        address TEXT,
        location TEXT,
        telegram TEXT,
        instagram TEXT,
        twitter TEXT,
        emergency_number TEXT,
        priority TEXT DEFAULT 'Medium',
        status TEXT DEFAULT 'Open',
        admin_notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students (id)
    )''')
    conn.commit()
    conn.close()

    # Add missing columns
    add_column_if_not_exists('grievances', 'priority', 'TEXT DEFAULT "Medium"')
    add_column_if_not_exists('helpdesk', 'phone', 'TEXT')
    add_column_if_not_exists('helpdesk', 'email', 'TEXT')
    add_column_if_not_exists('helpdesk', 'address', 'TEXT')
    add_column_if_not_exists('helpdesk', 'location', 'TEXT')
    add_column_if_not_exists('helpdesk', 'telegram', 'TEXT')
    add_column_if_not_exists('helpdesk', 'instagram', 'TEXT')
    add_column_if_not_exists('helpdesk', 'twitter', 'TEXT')
    add_column_if_not_exists('helpdesk', 'emergency_number', 'TEXT')
    add_column_if_not_exists('helpdesk', 'priority', 'TEXT DEFAULT "Medium"')
    add_column_if_not_exists('helpdesk', 'admin_notes', 'TEXT')

    # Default admin
    conn = get_db()
    cur = conn.execute("SELECT * FROM admins WHERE username='admin'")
    if not cur.fetchone():
        hashed = generate_password_hash('admin123')
        conn.execute("INSERT INTO admins (username, password) VALUES (?, ?)", ('admin', hashed))
    conn.commit()
    conn.close()

init_db()

# ---------- Helpers ----------
def save_upload(file):
    """Safely save uploaded file, return filename or None."""
    if file is None:
        return None
    if file.filename is None or file.filename.strip() == "":
        return None
    filename = secure_filename(file.filename)
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(path)
    return filename

def h(text):
    return html.escape(str(text)) if text is not None else ''

# ---------- Professional PDF Generation with Logo ----------
LOGO_URL = "https://cdn.phototourl.com/uploads/2026-03-11-f500de33-9484-42b0-9a7a-8e26ef27272f.jpg"

def make_circular_image(img_bytes):
    """Convert image bytes to a circular image (RGB, square, circle mask)."""
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        # Make it square
        size = min(img.size)
        img = img.crop((0, 0, size, size))
        # Create mask
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, size, size), fill=255)
        # Apply mask
        result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        result.paste(img, (0, 0), mask)
        # Convert back to RGB (FPDF doesn't support alpha)
        bg = Image.new("RGB", result.size, (255, 255, 255))
        bg.paste(result, mask=result.split()[3])
        output = io.BytesIO()
        bg.save(output, format='PNG')
        output.seek(0)
        return output
    except Exception:
        return None

def generate_grievance_pdf(grievance, student):
    pdf = FPDF()
    pdf.add_page()

    # Try to fetch and embed logo (circular if possible, else square)
    logo_placed = False
    temp_file = None
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(LOGO_URL, headers=headers, timeout=10, verify=False)
        if response.status_code == 200:
            img_bytes = response.content

            # If Pillow is available, try circular
            if PIL_AVAILABLE:
                circular_logo = make_circular_image(img_bytes)
                if circular_logo:
                    img_bytes = circular_logo.read()

            # Save to temporary file
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
            temp_file.write(img_bytes)
            temp_file.close()

            # Embed image from temp file
            pdf.image(temp_file.name, x=10, y=8, w=25)
            logo_placed = True
    except Exception:
        pass
    finally:
        if temp_file:
            try:
                os.unlink(temp_file.name)
            except:
                pass

    if not logo_placed:
        # Fallback: draw a colored circle with "SIET"
        pdf.set_fill_color(30, 58, 138)  # Blue-900
        pdf.ellipse(10, 8, 25, 25, 'F')
        pdf.set_text_color(255, 255, 255)
        pdf.set_font('Arial', 'B', 12)
        pdf.set_xy(12, 15)
        pdf.cell(21, 10, 'SIET', 0, 0, 'C')

    pdf.set_y(38)  # logo height 25 + margin 5 from top of logo

    # College header
    pdf.set_text_color(30, 58, 138)
    pdf.set_font('Arial', 'B', 16)
    pdf.cell(0, 10, 'SYNERGY INSTITUTE OF ENGINEERING & TECHNOLOGY', 0, 1, 'C')
    pdf.set_font('Arial', '', 11)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 8, 'Student Grievance Redressal System', 0, 1, 'C')
    pdf.ln(5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(10)

    # Student details
    pdf.set_font('Arial', 'B', 12)
    pdf.set_text_color(30, 58, 138)
    pdf.cell(0, 10, 'Student Details', 0, 1)
    pdf.set_font('Arial', '', 11)
    pdf.set_text_color(50, 50, 50)

    # Helper to convert None to 'N/A'
    def safe_str(val):
        return str(val) if val is not None else 'N/A'

    fields = [
        ('Name', student.get('name')),
        ('Roll Number', student.get('roll_number')),
        ('Branch', student.get('branch')),
        ('Email', student.get('email')),
        ('Phone', student.get('phone')),
        ('Emergency', student.get('emergency_contact')),
    ]
    fill = False
    for label, value in fields:
        pdf.set_fill_color(255, 255, 255) if fill else pdf.set_fill_color(240, 240, 240)
        pdf.cell(50, 8, label, 1, 0, 'L', 1)
        pdf.cell(0, 8, safe_str(value), 1, 1, 'L', 1)
        fill = not fill
    pdf.ln(5)

    # Grievance details
    pdf.set_font('Arial', 'B', 12)
    pdf.set_text_color(30, 58, 138)
    pdf.cell(0, 10, 'Grievance Details', 0, 1)
    pdf.set_font('Arial', '', 11)
    pdf.set_text_color(50, 50, 50)

    # Safe created_at extraction
    created_at = grievance.get('created_at')
    created_at_str = created_at[:10] if created_at else 'N/A'

    g_fields = [
        ('Grievance ID', grievance.get('id')),
        ('Category', grievance.get('category')),
        ('Priority', grievance.get('priority', 'Medium')),
        ('Status', grievance.get('status', 'Pending')),
        ('Submitted On', created_at_str),
    ]
    fill = False
    for label, value in g_fields:
        pdf.set_fill_color(255, 255, 255) if fill else pdf.set_fill_color(240, 240, 240)
        pdf.cell(50, 8, label, 1, 0, 'L', 1)
        pdf.cell(0, 8, safe_str(value), 1, 1, 'L', 1)
        fill = not fill

    pdf.ln(5)
    pdf.set_font('Arial', 'B', 11)
    pdf.cell(0, 8, 'Description:', 0, 1)
    pdf.set_font('Arial', '', 11)
    description = grievance.get('description')
    pdf.multi_cell(0, 7, safe_str(description))

    remarks = grievance.get('remarks')
    if remarks:
        pdf.ln(3)
        pdf.set_font('Arial', 'B', 11)
        pdf.cell(0, 8, 'Admin Remarks:', 0, 1)
        pdf.set_font('Arial', '', 11)
        pdf.multi_cell(0, 7, safe_str(remarks))

    # Status badge (including Rejected)
    pdf.ln(5)
    status = grievance.get('status', 'Pending')
    color_map = {
        'Pending': (255, 193, 7),
        'In-Process': (13, 110, 253),
        'Resolved': (40, 167, 69),
        'Rejected': (220, 53, 69)
    }
    color = color_map.get(status, (108, 117, 125))
    pdf.set_fill_color(*color)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(50, 10, f'  {safe_str(status)}  ', 1, 1, 'C', 1)

    # Footer
    pdf.set_y(-30)
    pdf.set_font('Arial', 'I', 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 10, 'This is a system-generated official document. No signature required.', 0, 1, 'C')
    pdf.cell(0, 10, f'Generated on: {datetime.now().strftime("%d-%m-%Y %H:%M")}', 0, 1, 'C')

    return pdf.output(dest='S').encode('latin1')

# ---------- Base Layout with Enhanced Professional Styling ----------
def base_html(content, user_menu, nav_links, messages):
    flash_html = ''
    for category, message in messages:
        color = 'green' if category == 'success' else 'red' if category == 'danger' else 'yellow'
        flash_html += f'<div class="bg-{color}-100 border-l-4 border-{color}-500 text-{color}-700 p-4 mb-4 rounded animate__animated animate__shakeX" role="alert">{h(message)}</div>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Synergy Grievance System</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/animate.css/4.1.1/animate.min.css">
    <style>
        body {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
        }}
        .glass-card {{
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 1.5rem;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            border: 1px solid rgba(255, 255, 255, 0.3);
        }}
        .btn-primary {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            transition: all 0.3s ease;
            border: none;
            color: white;
            font-weight: 600;
        }}
        .btn-primary:hover {{
            transform: translateY(-2px);
            box-shadow: 0 10px 25px -5px rgba(102, 126, 234, 0.5);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 12px;
            font-weight: 600;
        }}
        td {{
            padding: 12px;
            border-bottom: 1px solid #e2e8f0;
        }}
        tr:hover {{
            background: #f7fafc;
        }}
        .stat-card {{
            background: white;
            border-radius: 1rem;
            padding: 1.5rem;
            text-align: center;
            box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1);
            transition: all 0.3s ease;
        }}
        .stat-card:hover {{
            transform: scale(1.05);
            box-shadow: 0 20px 25px -5px rgba(0,0,0,0.2);
        }}
        .nav-link {{
            position: relative;
            transition: color 0.2s;
        }}
        .nav-link::after {{
            content: '';
            position: absolute;
            width: 0;
            height: 2px;
            bottom: -4px;
            left: 0;
            background: white;
            transition: width 0.3s;
        }}
        .nav-link:hover::after {{
            width: 100%;
        }}
        .fade-in {{
            animation: fadeIn 0.8s ease-out;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(20px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .badge {{
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.875rem;
            font-weight: 600;
            display: inline-block;
        }}
    </style>
</head>
<body class="p-6 fade-in">
    <div class="container mx-auto">
        <!-- Header with College Logo and Animated Gradient -->
        <div class="flex items-center justify-between mb-8 bg-white/10 backdrop-blur-md rounded-2xl p-4 shadow-xl">
            <div class="flex items-center space-x-4">
                <div id="logo-container" class="w-20 h-20 rounded-full border-4 border-white shadow-2xl flex items-center justify-center bg-gradient-to-br from-blue-600 to-purple-600 text-white text-3xl font-bold overflow-hidden">
                    <i class="fas fa-university"></i>
                </div>
                <script>
                    (function() {{
                        var img = new Image();
                        img.onload = function() {{
                            var container = document.getElementById('logo-container');
                            container.innerHTML = '';
                            container.style.background = 'none';
                            container.appendChild(img);
                        }};
                        img.onerror = function() {{
                            // Keep the Font Awesome icon
                        }};
                        img.src = '{LOGO_URL}';
                        img.className = 'w-full h-full object-cover';
                    }})();
                </script>
                <div>
                    <h1 class="text-4xl font-bold text-white drop-shadow-lg">Synergy Institute of Engineering & Technology</h1>
                    <p class="text-indigo-200 text-lg font-medium">Student Grievance Redressal System</p>
                </div>
            </div>
            <!-- Fixed alignment: flex container with spacing and no wrapping -->
            <div class="text-white font-medium flex items-center space-x-2 whitespace-nowrap">
                {user_menu}
            </div>
        </div>

        <!-- Navigation with animated underline -->
        <div class="bg-white/10 backdrop-blur-md rounded-xl p-4 mb-8 text-white flex flex-wrap space-x-8 shadow-lg">
            <a href="/" class="nav-link hover:text-indigo-200 transition"><i class="fas fa-home mr-2"></i>Home</a>
            <a href="/about" class="nav-link hover:text-indigo-200 transition"><i class="fas fa-info-circle mr-2"></i>About Project</a>
            <a href="/uml" class="nav-link hover:text-indigo-200 transition"><i class="fas fa-diagram-project mr-2"></i>UML Diagrams</a>
            {nav_links}
        </div>

        <!-- Flash Messages with animation -->
        {flash_html}

        <!-- Main Content -->
        <div class="glass-card p-8 shadow-2xl">
            {content}
        </div>
    </div>
</body>
</html>'''

# ---------- Page Content Builders (only profile_content modified to use .get()) ----------
def index_content():
    return '''<div class="text-center fade-in"><h2 class="text-4xl font-bold bg-gradient-to-r from-blue-600 to-purple-600 bg-clip-text text-transparent mb-6">Welcome to the Digital Grievance Portal</h2>
    <p class="text-gray-600 mb-8 text-lg">A transparent, efficient, and student-friendly platform to voice your concerns.</p>
    <div class="grid md:grid-cols-3 gap-8">
        <div class="bg-gradient-to-br from-blue-50 to-indigo-50 p-8 rounded-2xl hover:shadow-2xl transition transform hover:-translate-y-2">
            <i class="fas fa-shield-alt text-5xl text-blue-600 mb-4"></i>
            <h3 class="font-bold text-2xl mb-2 text-gray-800">Confidential</h3>
            <p class="text-gray-600">Your identity is protected.</p>
        </div>
        <div class="bg-gradient-to-br from-blue-50 to-indigo-50 p-8 rounded-2xl hover:shadow-2xl transition transform hover:-translate-y-2">
            <i class="fas fa-clock text-5xl text-blue-600 mb-4"></i>
            <h3 class="font-bold text-2xl mb-2 text-gray-800">Real‑Time Tracking</h3>
            <p class="text-gray-600">Know your grievance status.</p>
        </div>
        <div class="bg-gradient-to-br from-blue-50 to-indigo-50 p-8 rounded-2xl hover:shadow-2xl transition transform hover:-translate-y-2">
            <i class="fas fa-headset text-5xl text-blue-600 mb-4"></i>
            <h3 class="font-bold text-2xl mb-2 text-gray-800">24/7 Help Desk</h3>
            <p class="text-gray-600">Always here to assist.</p>
        </div>
    </div></div>'''

def about_content():
    return '''<h2 class="text-3xl font-bold bg-gradient-to-r from-blue-600 to-purple-600 bg-clip-text text-transparent mb-6">📄 About This Project</h2>
    <div class="prose max-w-none text-gray-700 space-y-4">
        <h3 class="text-xl font-semibold text-indigo-700">Introduction / Problem Statement</h3>
        <p>In any educational institution, students frequently encounter issues related to academics, administration, infrastructure, or interpersonal interactions. These concerns, if not addressed properly, may lead to dissatisfaction, reduced academic performance, and a negative learning environment. Traditionally, grievance handling processes in colleges are manual, time-consuming, and lack transparency. Students often hesitate to report problems due to fear, lack of accessibility, or uncertainty about the resolution process.</p>
        <p>A Student Grievance Redressal System provides a structured digital platform where students can submit complaints, track their status, and receive timely resolutions. The system enhances communication between students and the institution, ensures accountability, and brings transparency to the grievance-handling process. By digitizing the workflow, colleges can significantly reduce delays, maintain proper records, and ensure that grievances are handled fairly and efficiently.</p>
        <h3 class="text-xl font-semibold text-indigo-700 mt-6">Feasibility Study</h3>
        <ul class="list-disc list-inside"><li><strong>Technical Feasibility:</strong> Built using Flask, SQLite, and Tailwind – all free and easily deployable.</li><li><strong>Operational Feasibility:</strong> User-friendly interface for students and admins; reduces manual work.</li><li><strong>Economic Feasibility:</strong> Minimal cost (open source tools).</li><li><strong>Legal Feasibility:</strong> Complies with data privacy norms.</li><li><strong>Schedule Feasibility:</strong> Developed within 2 weeks.</li></ul>
        <h3 class="text-xl font-semibold text-indigo-700 mt-6">Requirement Analysis & SRS</h3>
        <p><strong>Functional:</strong> User authentication, submit grievance, view status, admin dashboard, update/resolve, record management.</p>
        <p><strong>Non-Functional:</strong> Usability, security, performance, reliability, scalability, compatibility.</p>
        <h3 class="text-xl font-semibold text-indigo-700 mt-6">Advantages</h3>
        <ul class="list-disc list-inside"><li>24/7 accessibility, real-time tracking, confidentiality.</li><li>Centralized data, accountability, automated reports.</li><li>Improved student satisfaction and regulatory compliance.</li></ul>
        <h3 class="text-xl font-semibold text-indigo-700 mt-6">Limitations</h3>
        <ul class="list-disc list-inside"><li>Dependency on internet connectivity.</li><li>Human factor – system cannot force resolution.</li><li>Verification challenges for fake complaints.</li><li>No mobile app (yet).</li></ul>
        <h3 class="text-xl font-semibold text-indigo-700 mt-6">Future Prospects</h3>
        <ul class="list-disc list-inside"><li>AI Chatbots, sentiment analysis, auto-categorization.</li><li>Blockchain for tamper-proof records.</li><li>Multi-language and voice-based complaints.</li><li>Mobile app with push notifications.</li><li>Integration with college ERP.</li></ul>
        <h3 class="text-xl font-semibold text-indigo-700 mt-6">Conclusion</h3>
        <p>The Student Grievance Redressal System bridges the gap between students and administration, ensuring transparency, efficiency, and a responsive campus environment. It is a tool for institutional improvement, leveraging technology to prioritize student welfare.</p>
        <h3 class="text-xl font-semibold text-indigo-700 mt-6">Bibliography</h3>
        <p>Pressman, Silberschatz, W3Schools, GeeksforGeeks, Stack Overflow, Bootstrap docs, etc.</p>
    </div>'''

def uml_content():
    return '''<h2 class="text-3xl font-bold bg-gradient-to-r from-blue-600 to-purple-600 bg-clip-text text-transparent mb-6">📊 UML Diagrams</h2>
    <div class="grid md:grid-cols-2 gap-8">
        <div class="border p-4 rounded-lg shadow"><h3 class="text-xl font-semibold mb-2">Class Diagram</h3><pre class="bg-gray-100 p-4 rounded text-sm">+----------------+       +----------------+\n|    Student     |       |   Grievance    |\n+----------------+       +----------------+\n| -id            |       | -id            |\n| -name          |       | -category      |\n| -email         |       | -description   |\n| -roll_number   |       | -status        |\n| -branch        |       | -remarks       |\n+----------------+       +----------------+\n| +login()       |------>| +submit()      |\n| +viewStatus()  |       | +updateStatus()|\n+----------------+       +----------------+</pre></div>
        <div class="border p-4 rounded-lg shadow"><h3 class="text-xl font-semibold mb-2">Use Case Diagram</h3><pre class="bg-gray-100 p-4 rounded text-sm">    +-----------------------+\n    |   Grievance System    |\n    +-----------------------+\n    |  (Student)            |\n    |   - Login          ----+\n    |   - Register       ----+\n    |   - Submit Grievance--+|\n    |   - Track Status   ----+|\n    |   - Contact Help   ----+|\n    +-----------------------+|\n    |  (Admin)               |\n    |   - Admin Login        |\n    |   - View All           |\n    |   - Update Status      |\n    |   - Reply Help Desk    |\n    +-----------------------+</pre></div>
        <div class="border p-4 rounded-lg shadow"><h3 class="text-xl font-semibold mb-2">Admin Activity Diagram</h3><pre class="bg-gray-100 p-4 rounded text-sm">[Start] --> Login\n    --> View Pending\n    --> Verify Complaint\n    --> [Decision: Valid?]\n        --> Yes: Assign/Update\n        --> No: Reject with remark\n    --> Notify Student\n    --> [End]</pre></div>
        <div class="border p-4 rounded-lg shadow"><h3 class="text-xl font-semibold mb-2">Data Flow Diagram (Level 0)</h3><pre class="bg-gray-100 p-4 rounded text-sm">    +---------+      +-----------------+      +---------+\n    | Student |----->| Grievance System |<----| Admin   |\n    +---------+      +-----------------+      +---------+\n         ^                   |                      ^\n         |                   v                      |\n         +------------------[Database]--------------+</pre></div>
    </div>'''

def register_content():
    return '''<h2 class="text-2xl font-bold mb-6 text-gray-800">📝 Student Registration</h2>
    <form method="POST" class="space-y-4 max-w-md">
        <div><label class="block font-medium text-gray-700">Full Name</label><input type="text" name="name" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">Email</label><input type="email" name="email" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">Password</label><input type="password" name="password" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">Roll Number</label><input type="text" name="roll" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">Branch</label><input type="text" name="branch" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <button type="submit" class="btn-primary text-white px-6 py-2 rounded-lg w-full">Register</button>
    </form>
    <p class="mt-4">Already have an account? <a href="/login" class="text-blue-600 hover:underline">Login here</a>.</p>'''

def login_content():
    return '''<h2 class="text-2xl font-bold mb-6 text-gray-800">🔐 Student Login</h2>
    <form method="POST" class="space-y-4 max-w-md">
        <div><label class="block font-medium text-gray-700">Email</label><input type="email" name="email" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">Password</label><input type="password" name="password" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <button type="submit" class="btn-primary text-white px-6 py-2 rounded-lg w-full">Login</button>
    </form>
    <p class="mt-4">New student? <a href="/register" class="text-blue-600 hover:underline">Register here</a>.</p>'''

def profile_content(student):
    return f'''<h2 class="text-2xl font-bold mb-6 text-gray-800">👤 My Profile</h2>
    <form method="POST" class="space-y-4 max-w-2xl">
        <div class="grid grid-cols-2 gap-4">
            <div><label class="block font-medium text-gray-700">Full Name</label><input type="text" name="name" value="{h(student.get('name', ''))}" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Email</label><input type="email" name="email" value="{h(student.get('email', ''))}" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Roll Number</label><input type="text" name="roll" value="{h(student.get('roll_number', ''))}" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Branch</label><input type="text" name="branch" value="{h(student.get('branch', ''))}" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Phone</label><input type="text" name="phone" value="{h(student.get('phone', ''))}" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Address</label><input type="text" name="address" value="{h(student.get('address', ''))}" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Telegram</label><input type="text" name="telegram" value="{h(student.get('telegram', ''))}" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Instagram</label><input type="text" name="instagram" value="{h(student.get('instagram', ''))}" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">X (Twitter)</label><input type="text" name="twitter" value="{h(student.get('twitter', ''))}" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Emergency Contact</label><input type="text" name="emergency" value="{h(student.get('emergency_contact', ''))}" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        </div>
        <button type="submit" class="btn-primary text-white px-6 py-2 rounded-lg">Update Profile</button>
    </form>
    <p class="mt-4"><a href="/change-password" class="text-blue-600 hover:underline">Change Password</a></p>'''

def change_password_content():
    return '''<h2 class="text-2xl font-bold mb-6 text-gray-800">🔑 Change Password</h2>
    <form method="POST" class="space-y-4 max-w-md">
        <div><label class="block font-medium text-gray-700">Current Password</label><input type="password" name="current" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">New Password</label><input type="password" name="new" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">Confirm New Password</label><input type="password" name="confirm" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <button type="submit" class="btn-primary text-white px-6 py-2 rounded-lg w-full">Change Password</button>
    </form>'''

def submit_content():
    return '''<h2 class="text-2xl font-bold mb-6 text-gray-800">📌 Submit New Grievance</h2>
    <form method="POST" enctype="multipart/form-data" class="space-y-4 max-w-2xl">
        <div><label class="block font-medium text-gray-700">Category</label>
            <select name="category" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400">
                <option>Academic</option><option>Administrative</option><option>Infrastructure</option><option>Hostel</option><option>Other</option>
            </select>
        </div>
        <div><label class="block font-medium text-gray-700">Priority</label>
            <select name="priority" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400">
                <option value="Low">Low</option><option value="Medium" selected>Medium</option><option value="High">High</option>
            </select>
        </div>
        <div><label class="block font-medium text-gray-700">Description</label><textarea name="description" rows="5" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400" required></textarea></div>
        <div><label class="block font-medium text-gray-700">Attachment (optional)</label><input type="file" name="attachment" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <button type="submit" class="btn-primary text-white px-6 py-2 rounded-lg">Submit Grievance</button>
    </form>'''

def my_grievances_content(grievances):
    rows = ''
    for g in grievances:
        g_dict = dict(g)
        status = g_dict.get('status', 'Pending')
        status_class = {
            'Pending': 'bg-yellow-100 text-yellow-800',
            'In-Process': 'bg-blue-100 text-blue-800',
            'Resolved': 'bg-green-100 text-green-800',
            'Rejected': 'bg-red-100 text-red-800'
        }.get(status, 'bg-gray-100 text-gray-800')

        priority = g_dict.get('priority', 'Medium')
        priority_class = {
            'Low': 'bg-gray-100 text-gray-800',
            'Medium': 'bg-orange-100 text-orange-800',
            'High': 'bg-red-100 text-red-800'
        }.get(priority, 'bg-gray-100 text-gray-800')

        # Safe created_at
        created_at = g_dict.get('created_at')
        created_at_str = created_at[:10] if created_at else ''

        rows += f'''<tr class="hover:bg-gray-50">
            <td class="font-medium">{h(g_dict.get('id'))}</td>
            <td>{h(g_dict.get('category'))}</td>
            <td>{h(g_dict.get('description'))}</td>
            <td><span class="badge {priority_class}">{h(priority)}</span></td>
            <td><span class="badge {status_class}">{h(status)}</span></td>
            <td>{h(g_dict.get('remarks'))}</td>
            <td>{h(created_at_str)}</td>
            <td><a href="/download_grievance/{g_dict['id']}" class="bg-green-500 text-white px-3 py-1 rounded-lg text-sm hover:bg-green-600 transition">PDF</a></td>
        </tr>'''
    if not rows:
        return '<p class="text-gray-500 text-center py-8">No grievances found.</p>'
    return f'''<h2 class="text-2xl font-bold mb-6 text-gray-800">📋 My Grievances</h2>
    <div class="overflow-x-auto">
        <table class="min-w-full bg-white rounded-lg overflow-hidden">
            <thead class="bg-gradient-to-r from-blue-600 to-purple-600 text-white">
                <tr><th>ID</th><th>Category</th><th>Description</th><th>Priority</th><th>Status</th><th>Remarks</th><th>Submitted</th><th>PDF</th></tr>
            </thead>
            <tbody class="divide-y divide-gray-200">{rows}</tbody>
        </table>
    </div>'''

def help_content(tickets):
    ticket_rows = ''
    for t in tickets:
        t_dict = dict(t)
        priority = t_dict.get('priority', 'Medium')
        priority_class = {
            'Low': 'bg-gray-100 text-gray-800',
            'Medium': 'bg-orange-100 text-orange-800',
            'High': 'bg-red-100 text-red-800'
        }.get(priority, 'bg-gray-100 text-gray-800')
        status_class = {
            'Open': 'bg-yellow-100 text-yellow-800',
            'In Progress': 'bg-blue-100 text-blue-800',
            'Resolved': 'bg-green-100 text-green-800'
        }.get(t_dict.get('status'), 'bg-gray-100 text-gray-800')

        # Safe created_at
        created_at = t_dict.get('created_at')
        created_at_str = created_at[:10] if created_at else ''

        ticket_rows += f'''<tr class="hover:bg-gray-50">
            <td>{h(t_dict.get('id'))}</td>
            <td>{h(t_dict.get('subject'))}</td>
            <td>{h(t_dict.get('message'))}</td>
            <td><span class="badge {priority_class}">{h(priority)}</span></td>
            <td><span class="badge {status_class}">{h(t_dict.get('status'))}</span></td>
            <td>{h(t_dict.get('admin_notes'))}</td>
            <td>{h(created_at_str)}</td>
        </tr>'''
    return f'''<h2 class="text-2xl font-bold mb-6 text-gray-800">🆘 Help Desk</h2>
    <form method="POST" class="space-y-4 max-w-2xl bg-gray-50 p-6 rounded-lg shadow-inner">
        <div><label class="block font-medium text-gray-700">Subject</label><input type="text" name="subject" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">Message</label><textarea name="message" rows="5" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400" required></textarea></div>
        <div><label class="block font-medium text-gray-700">Priority</label><select name="priority" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"><option value="Low">Low</option><option value="Medium" selected>Medium</option><option value="High">High</option></select></div>
        <div class="grid grid-cols-2 gap-4">
            <div><label class="block font-medium text-gray-700">Phone</label><input type="text" name="phone" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Email</label><input type="email" name="email" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Address</label><input type="text" name="address" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Location</label><input type="text" name="location" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Telegram</label><input type="text" name="telegram" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Instagram</label><input type="text" name="instagram" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">X (Twitter)</label><input type="text" name="twitter" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
            <div><label class="block font-medium text-gray-700">Emergency Number</label><input type="text" name="emergency" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        </div>
        <button type="submit" class="btn-primary text-white px-6 py-2 rounded-lg w-full">Send Query</button>
    </form>
    <h3 class="text-xl font-bold mt-8 mb-4 text-gray-800">Your Previous Tickets</h3>
    <div class="overflow-x-auto">
        <table class="min-w-full bg-white rounded-lg overflow-hidden">
            <thead class="bg-gradient-to-r from-blue-600 to-purple-600 text-white">
                <tr><th>ID</th><th>Subject</th><th>Message</th><th>Priority</th><th>Status</th><th>Admin Notes</th><th>Date</th></tr>
            </thead>
            <tbody class="divide-y divide-gray-200">{ticket_rows if ticket_rows else '<tr><td colspan="7" class="text-center py-4">No tickets yet.</td></tr>'}</tbody>
        </table>
    </div>'''

def admin_login_content():
    return '''<h2 class="text-2xl font-bold mb-6 text-gray-800">👤 Admin Login</h2>
    <form method="POST" class="space-y-4 max-w-md">
        <div><label class="block font-medium text-gray-700">Username</label><input type="text" name="username" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <div><label class="block font-medium text-gray-700">Password</label><input type="password" name="password" required class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-400"></div>
        <button type="submit" class="btn-primary text-white px-6 py-2 rounded-lg w-full">Login</button>
    </form>'''

def admin_dashboard_content(total_g, pending_g, resolved_g, total_t, open_t):
    return f'''<h2 class="text-2xl font-bold mb-6 text-gray-800">📊 Admin Dashboard</h2>
    <div class="grid md:grid-cols-5 gap-4 mb-8">
        <div class="stat-card"><span class="text-3xl font-bold text-blue-600">{total_g}</span><br>Total Grievances</div>
        <div class="stat-card"><span class="text-3xl font-bold text-yellow-600">{pending_g}</span><br>Pending Grievances</div>
        <div class="stat-card"><span class="text-3xl font-bold text-green-600">{resolved_g}</span><br>Resolved Grievances</div>
        <div class="stat-card"><span class="text-3xl font-bold text-blue-600">{total_t}</span><br>Help Tickets</div>
        <div class="stat-card"><span class="text-3xl font-bold text-red-600">{open_t}</span><br>Open Tickets</div>
    </div>'''

def admin_grievances_content(grievances):
    rows = ''
    for g in grievances:
        g_dict = dict(g)
        status = g_dict.get('status', 'Pending')
        status_class = {
            'Pending': 'bg-yellow-100 text-yellow-800',
            'In-Process': 'bg-blue-100 text-blue-800',
            'Resolved': 'bg-green-100 text-green-800',
            'Rejected': 'bg-red-100 text-red-800'
        }.get(status, 'bg-gray-100 text-gray-800')

        priority = g_dict.get('priority', 'Medium')
        priority_class = {
            'Low': 'bg-gray-100 text-gray-800',
            'Medium': 'bg-orange-100 text-orange-800',
            'High': 'bg-red-100 text-red-800'
        }.get(priority, 'bg-gray-100 text-gray-800')

        rows += f'''<tr class="hover:bg-gray-50">
            <td>{h(g_dict.get('id'))}</td>
            <td>{h(g_dict.get('name'))} ({h(g_dict.get('roll_number'))})</td>
            <td>{h(g_dict.get('category'))}</td>
            <td>{h(g_dict.get('description'))}</td>
            <td><span class="badge {priority_class}">{h(priority)}</span></td>
            <td><span class="badge {status_class}">{h(status)}</span></td>
            <td>{h(g_dict.get('remarks'))}</td>
            <td>
                <form action="/admin/update_grievance/{g_dict['id']}" method="POST" class="flex flex-col space-y-2">
                    <select name="status" class="border p-1 rounded text-sm">
                        <option value="Pending" {'selected' if status == 'Pending' else ''}>Pending</option>
                        <option value="In-Process" {'selected' if status == 'In-Process' else ''}>In-Process</option>
                        <option value="Resolved" {'selected' if status == 'Resolved' else ''}>Resolved</option>
                        <option value="Rejected" {'selected' if status == 'Rejected' else ''}>Rejected</option>
                    </select>
                    <input type="text" name="remarks" placeholder="Remarks" value="{h(g_dict.get('remarks'))}" class="border p-1 rounded text-sm">
                    <button type="submit" class="bg-blue-600 text-white px-2 py-1 rounded text-sm hover:bg-blue-700">Update</button>
                </form>
            </td>
        </tr>'''
    return f'''<h2 class="text-2xl font-bold mb-6 text-gray-800">📋 Manage Grievances</h2>
    <div class="overflow-x-auto">
        <table class="min-w-full bg-white rounded-lg overflow-hidden">
            <thead class="bg-gradient-to-r from-blue-600 to-purple-600 text-white">
                <tr><th>ID</th><th>Student</th><th>Category</th><th>Description</th><th>Priority</th><th>Status</th><th>Remarks</th><th>Action</th></tr>
            </thead>
            <tbody class="divide-y divide-gray-200">{rows}</tbody>
        </table>
    </div>'''

def admin_helpdesk_content(tickets):
    rows = ''
    for t in tickets:
        t_dict = dict(t)
        priority = t_dict.get('priority', 'Medium')
        priority_class = {
            'Low': 'bg-gray-100 text-gray-800',
            'Medium': 'bg-orange-100 text-orange-800',
            'High': 'bg-red-100 text-red-800'
        }.get(priority, 'bg-gray-100 text-gray-800')
        status_class = {
            'Open': 'bg-yellow-100 text-yellow-800',
            'In Progress': 'bg-blue-100 text-blue-800',
            'Resolved': 'bg-green-100 text-green-800'
        }.get(t_dict.get('status'), 'bg-gray-100 text-gray-800')
        rows += f'''<tr class="hover:bg-gray-50">
            <td>{h(t_dict.get('id'))}</td>
            <td>{h(t_dict.get('name'))} ({h(t_dict.get('roll_number', ''))})</td>
            <td>{h(t_dict.get('subject'))}</td>
            <td>{h(t_dict.get('message'))}</td>
            <td><span class="badge {priority_class}">{h(priority)}</span></td>
            <td>
                <form action="/admin/update_ticket/{t_dict['id']}" method="POST" class="flex flex-col space-y-2">
                    <select name="status" class="border p-1 rounded text-sm">
                        <option value="Open" {'selected' if t_dict.get('status') == 'Open' else ''}>Open</option>
                        <option value="In Progress" {'selected' if t_dict.get('status') == 'In Progress' else ''}>In Progress</option>
                        <option value="Resolved" {'selected' if t_dict.get('status') == 'Resolved' else ''}>Resolved</option>
                    </select>
                    <input type="text" name="admin_notes" placeholder="Notes" value="{h(t_dict.get('admin_notes'))}" class="border p-1 rounded text-sm">
                    <button type="submit" class="bg-blue-600 text-white px-2 py-1 rounded text-sm hover:bg-blue-700">Update</button>
                </form>
            </td>
            <td>
                <button onclick="toggleDetails({t_dict['id']})" class="bg-gray-600 text-white px-2 py-1 rounded text-sm hover:bg-gray-700">Contact</button>
                <div id="details-{t_dict['id']}" style="display:none;" class="absolute bg-white border p-4 rounded shadow-lg mt-2 z-10 max-w-xs">
                    <p><strong>Phone:</strong> {h(t_dict.get('phone'))}</p>
                    <p><strong>Email:</strong> {h(t_dict.get('email'))}</p>
                    <p><strong>Address:</strong> {h(t_dict.get('address'))}</p>
                    <p><strong>Location:</strong> {h(t_dict.get('location'))}</p>
                    <p><strong>Telegram:</strong> {h(t_dict.get('telegram'))}</p>
                    <p><strong>Instagram:</strong> {h(t_dict.get('instagram'))}</p>
                    <p><strong>X:</strong> {h(t_dict.get('twitter'))}</p>
                    <p><strong>Emergency:</strong> {h(t_dict.get('emergency_number'))}</p>
                </div>
            </td>
        </tr>'''
    return f'''<h2 class="text-2xl font-bold mb-6 text-gray-800">📬 Help Desk Queries</h2>
    <div class="overflow-x-auto">
        <table class="min-w-full bg-white rounded-lg overflow-hidden">
            <thead class="bg-gradient-to-r from-blue-600 to-purple-600 text-white">
                <tr><th>ID</th><th>Student</th><th>Subject</th><th>Message</th><th>Priority</th><th>Status/Notes</th><th>Contact</th></tr>
            </thead>
            <tbody class="divide-y divide-gray-200">{rows if rows else '<tr><td colspan="7" class="text-center py-4">No tickets.</td></tr>'}</tbody>
        </table>
    </div>
    <script>
    function toggleDetails(id) {{
        var el = document.getElementById('details-'+id);
        if (el.style.display === 'none' || el.style.display === '') {{
            el.style.display = 'block';
        }} else {{
            el.style.display = 'none';
        }}
    }}
    </script>'''

# ========== NEW: Admin Database Logs Page ==========
def admin_database_content(logs):
    rows = ''
    for log in logs:
        log_dict = dict(log)
        type_ = log_dict.get('type')
        if type_ == 'Grievance':
            desc = log_dict.get('description')
            extra = f"Category: {h(log_dict.get('category'))}"
        else:
            desc = log_dict.get('message')
            extra = f"Subject: {h(log_dict.get('subject'))}"
        status_class = {
            'Pending': 'bg-yellow-100 text-yellow-800',
            'In-Process': 'bg-blue-100 text-blue-800',
            'Resolved': 'bg-green-100 text-green-800',
            'Rejected': 'bg-red-100 text-red-800',
            'Open': 'bg-yellow-100 text-yellow-800',
            'In Progress': 'bg-blue-100 text-blue-800'
        }.get(log_dict.get('status'), 'bg-gray-100 text-gray-800')
        rows += f'''<tr>
            <td>{h(log_dict.get('id'))}</td>
            <td><span class="badge bg-blue-100 text-blue-800">{h(type_)}</span></td>
            <td>{h(log_dict.get('student_name'))} ({h(log_dict.get('roll_number'))})</td>
            <td>{h(desc)}</td>
            <td>{extra}</td>
            <td><span class="badge {status_class}">{h(log_dict.get('status'))}</span></td>
            <td>{h(log_dict.get('priority', ''))}</td>
            <td>{h(log_dict.get('created_at'))[:16]}</td>
            <td>{h(log_dict.get('updated_at'))[:16] if log_dict.get('updated_at') else ''}</td>
        </tr>'''
    if not rows:
        rows = '<tr><td colspan="9" class="text-center py-4">No records found.</td></tr>'
    return f'''<h2 class="text-2xl font-bold mb-6 text-gray-800">📋 Database Audit Log</h2>
    <div class="flex space-x-4 mb-6">
        <a href="/admin/database/download" class="bg-green-600 text-white px-4 py-2 rounded-lg hover:bg-green-700 transition"><i class="fas fa-download mr-2"></i>Download as CSV</a>
        <form method="POST" action="/admin/database/clear" onsubmit="return confirm('Are you sure you want to delete ALL grievances and helpdesk tickets? This action cannot be undone.');">
            <button type="submit" class="bg-red-600 text-white px-4 py-2 rounded-lg hover:bg-red-700 transition"><i class="fas fa-trash mr-2"></i>Clear All Records</button>
        </form>
    </div>
    <div class="overflow-x-auto">
        <table class="min-w-full bg-white rounded-lg overflow-hidden">
            <thead class="bg-gradient-to-r from-blue-600 to-purple-600 text-white">
                <tr><th>ID</th><th>Type</th><th>Student</th><th>Description/Message</th><th>Details</th><th>Status</th><th>Priority</th><th>Created</th><th>Updated</th></tr>
            </thead>
            <tbody class="divide-y divide-gray-200">{rows}</tbody>
        </table>
    </div>'''

# ---------- Routes ----------
def build_menu_nav():
    user_menu = ''
    nav_links = ''
    if session.get('user_role') == 'student':
        user_menu += f'<span class="mr-4 font-medium">Welcome, {h(session["user_name"])}</span>'
        user_menu += '<a href="/profile" class="bg-white/20 backdrop-blur-sm px-4 py-2 rounded-lg hover:bg-white/30 transition mr-2"><i class="fas fa-user mr-1"></i>Profile</a>'
        user_menu += '<a href="/logout" class="bg-red-500/80 backdrop-blur-sm px-4 py-2 rounded-lg hover:bg-red-600 transition"><i class="fas fa-sign-out-alt mr-1"></i>Logout</a>'
        nav_links += '<a href="/submit" class="nav-link"><i class="fas fa-plus-circle mr-1"></i>New Grievance</a>'
        nav_links += '<a href="/my-grievances" class="nav-link"><i class="fas fa-list mr-1"></i>My Grievances</a>'
        nav_links += '<a href="/help" class="nav-link"><i class="fas fa-headset mr-1"></i>Help Desk</a>'
    elif session.get('user_role') == 'admin':
        user_menu += f'<span class="mr-4 font-medium">Admin: {h(session["admin_user"])}</span>'
        user_menu += '<a href="/admin/logout" class="bg-red-500/80 backdrop-blur-sm px-4 py-2 rounded-lg hover:bg-red-600 transition"><i class="fas fa-sign-out-alt mr-1"></i>Logout</a>'
        nav_links += '<a href="/admin/dashboard" class="nav-link"><i class="fas fa-tachometer-alt mr-1"></i>Dashboard</a>'
        nav_links += '<a href="/admin/grievances" class="nav-link"><i class="fas fa-tasks mr-1"></i>All Grievances</a>'
        nav_links += '<a href="/admin/helpdesk" class="nav-link"><i class="fas fa-question-circle mr-1"></i>Help Desk</a>'
        nav_links += '<a href="/admin/database" class="nav-link"><i class="fas fa-database mr-1"></i>Database Logs</a>'
    else:
        user_menu += '<a href="/login" class="bg-white/20 backdrop-blur-sm px-4 py-2 rounded-lg hover:bg-white/30 transition mr-2"><i class="fas fa-sign-in-alt mr-1"></i>Student Login</a>'
        user_menu += '<a href="/register" class="bg-green-500/80 backdrop-blur-sm px-4 py-2 rounded-lg hover:bg-green-600 transition mr-2"><i class="fas fa-user-plus mr-1"></i>Register</a>'
        user_menu += '<a href="/admin/login" class="bg-gray-700/80 backdrop-blur-sm px-4 py-2 rounded-lg hover:bg-gray-800 transition"><i class="fas fa-lock mr-1"></i>Admin</a>'
        nav_links = ''
    return user_menu, nav_links

@app.route('/')
def index():
    u, n = build_menu_nav()
    return base_html(index_content(), u, n, get_flashed_messages(with_categories=True))

@app.route('/about')
def about():
    u, n = build_menu_nav()
    return base_html(about_content(), u, n, get_flashed_messages(with_categories=True))

@app.route('/uml')
def uml():
    u, n = build_menu_nav()
    return base_html(uml_content(), u, n, get_flashed_messages(with_categories=True))

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = generate_password_hash(request.form['password'])
        roll = request.form['roll']
        branch = request.form['branch']
        conn = get_db()
        try:
            conn.execute('INSERT INTO students (name,email,password,roll_number,branch) VALUES (?,?,?,?,?)',
                         (name,email,password,roll,branch))
            conn.commit()
            # Send registration confirmation email
            send_email(email,
                       "Registration Successful",
                       f"Hello {name},\n\nYou have successfully registered in Grievance System.")
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email or Roll Number already exists.', 'danger')
        finally:
            conn.close()
    u, n = build_menu_nav()
    return base_html(register_content(), u, n, get_flashed_messages(with_categories=True))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_db()
        user = conn.execute('SELECT * FROM students WHERE email = ?', (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['user_role'] = 'student'
            flash('Login successful!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid credentials.', 'danger')
    u, n = build_menu_nav()
    return base_html(login_content(), u, n, get_flashed_messages(with_categories=True))

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/profile', methods=['GET','POST'])
def profile():
    if 'user_id' not in session or session.get('user_role') != 'student':
        flash('Please login.', 'warning')
        return redirect(url_for('login'))
    conn = get_db()
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        roll = request.form['roll']
        branch = request.form['branch']
        phone = request.form['phone']
        address = request.form['address']
        telegram = request.form['telegram']
        instagram = request.form['instagram']
        twitter = request.form['twitter']
        emergency = request.form['emergency']
        try:
            conn.execute('''UPDATE students SET name=?, email=?, roll_number=?, branch=?, phone=?, address=?, telegram=?, instagram=?, twitter=?, emergency_contact=? WHERE id=?''',
                         (name, email, roll, branch, phone, address, telegram, instagram, twitter, emergency, session['user_id']))
            conn.commit()
            flash('Profile updated.', 'success')
        except sqlite3.IntegrityError:
            flash('Email or Roll Number already in use.', 'danger')
        finally:
            conn.close()
        return redirect(url_for('profile'))
    student = conn.execute('SELECT * FROM students WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    u, n = build_menu_nav()
    return base_html(profile_content(dict(student)), u, n, get_flashed_messages(with_categories=True))

@app.route('/change-password', methods=['GET','POST'])
def change_password():
    if 'user_id' not in session or session.get('user_role') != 'student':
        flash('Please login.', 'warning')
        return redirect(url_for('login'))
    if request.method == 'POST':
        current = request.form['current']
        new = request.form['new']
        confirm = request.form['confirm']
        conn = get_db()
        user = conn.execute('SELECT * FROM students WHERE id = ?', (session['user_id'],)).fetchone()
        if user and check_password_hash(user['password'], current):
            if new == confirm:
                hashed = generate_password_hash(new)
                conn.execute('UPDATE students SET password = ? WHERE id = ?', (hashed, session['user_id']))
                conn.commit()
                flash('Password changed.', 'success')
            else:
                flash('New passwords do not match.', 'danger')
        else:
            flash('Current password incorrect.', 'danger')
        conn.close()
        return redirect(url_for('profile'))
    u, n = build_menu_nav()
    return base_html(change_password_content(), u, n, get_flashed_messages(with_categories=True))

@app.route('/submit', methods=['GET','POST'])
def submit():
    if 'user_id' not in session or session.get('user_role') != 'student':
        flash('Please login as student.', 'warning')
        return redirect(url_for('login'))
    if request.method == 'POST':
        category = request.form['category']
        description = request.form['description']
        priority = request.form['priority']
        file = request.files.get('attachment')
        filename = save_upload(file)
        conn = get_db()
        conn.execute('INSERT INTO grievances (student_id, category, description, priority, attachment) VALUES (?,?,?,?,?)',
                     (session['user_id'], category, description, priority, filename))
        conn.commit()
        # Fetch student to get email
        student = conn.execute('SELECT * FROM students WHERE id=?', (session['user_id'],)).fetchone()
        conn.close()
        if student:
            send_email(student['email'],
                       "Grievance Submitted",
                       f"""Hello {student['name']},

Your grievance has been submitted successfully.

Category: {category}
Description: {description}
Priority: {priority}

We will update you soon.

Thank you.""")
        flash('Grievance submitted successfully.', 'success')
        return redirect(url_for('my_grievances'))
    u, n = build_menu_nav()
    return base_html(submit_content(), u, n, get_flashed_messages(with_categories=True))

@app.route('/my-grievances')
def my_grievances():
    if 'user_id' not in session or session.get('user_role') != 'student':
        flash('Please login.', 'warning')
        return redirect(url_for('login'))
    conn = get_db()
    grievances = conn.execute('SELECT * FROM grievances WHERE student_id = ? ORDER BY created_at DESC', (session['user_id'],)).fetchall()
    conn.close()
    u, n = build_menu_nav()
    return base_html(my_grievances_content(grievances), u, n, get_flashed_messages(with_categories=True))

@app.route('/download_grievance/<int:id>')
def download_grievance(id):
    if 'user_id' not in session or session.get('user_role') != 'student':
        flash('Please login.', 'warning')
        return redirect(url_for('login'))

    conn = get_db()
    grievance = conn.execute('SELECT * FROM grievances WHERE id = ? AND student_id = ?', (id, session['user_id'])).fetchone()
    student = conn.execute('SELECT * FROM students WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()

    if not grievance or not student:
        flash('Grievance not found.', 'danger')
        return redirect(url_for('my_grievances'))

    grievance_dict = dict(grievance)
    student_dict = dict(student)

    pdf_bytes = generate_grievance_pdf(grievance_dict, student_dict)
    return send_file(
        io.BytesIO(pdf_bytes),
        download_name=f'grievance_{id}.pdf',
        as_attachment=True,
        mimetype='application/pdf'
    )

@app.route('/help', methods=['GET','POST'])
def help():
    if 'user_id' not in session or session.get('user_role') != 'student':
        flash('Please login.', 'warning')
        return redirect(url_for('login'))
    if request.method == 'POST':
        subject = request.form['subject']
        message = request.form['message']
        priority = request.form['priority']
        phone = request.form['phone']
        email = request.form['email']
        address = request.form['address']
        location = request.form['location']
        telegram = request.form['telegram']
        instagram = request.form['instagram']
        twitter = request.form['twitter']
        emergency = request.form['emergency']
        conn = get_db()
        conn.execute('''INSERT INTO helpdesk 
            (student_id, subject, message, priority, phone, email, address, location, telegram, instagram, twitter, emergency_number)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (session['user_id'], subject, message, priority, phone, email, address, location, telegram, instagram, twitter, emergency))
        conn.commit()
        conn.close()
        flash('Query sent. We\'ll reply soon.', 'success')
        return redirect(url_for('help'))
    conn = get_db()
    tickets = conn.execute('SELECT * FROM helpdesk WHERE student_id = ? ORDER BY created_at DESC', (session['user_id'],)).fetchall()
    conn.close()
    u, n = build_menu_nav()
    return base_html(help_content(tickets), u, n, get_flashed_messages(with_categories=True))

# ---------- Admin Routes ----------
@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db()
        admin = conn.execute('SELECT * FROM admins WHERE username = ?', (username,)).fetchone()
        conn.close()
        if admin and check_password_hash(admin['password'], password):
            session['admin_id'] = admin['id']
            session['admin_user'] = admin['username']
            session['user_role'] = 'admin'
            flash('Admin login successful.', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid admin credentials.', 'danger')
    u, n = build_menu_nav()
    return base_html(admin_login_content(), u, n, get_flashed_messages(with_categories=True))

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    session.pop('admin_user', None)
    session['user_role'] = None
    flash('Admin logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'admin_id' not in session:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('admin_login'))
    conn = get_db()
    total_g = conn.execute('SELECT COUNT(*) FROM grievances').fetchone()[0]
    pending_g = conn.execute('SELECT COUNT(*) FROM grievances WHERE status="Pending"').fetchone()[0]
    resolved_g = conn.execute('SELECT COUNT(*) FROM grievances WHERE status="Resolved"').fetchone()[0]
    total_t = conn.execute('SELECT COUNT(*) FROM helpdesk').fetchone()[0]
    open_t = conn.execute('SELECT COUNT(*) FROM helpdesk WHERE status="Open"').fetchone()[0]
    conn.close()
    u, n = build_menu_nav()
    return base_html(admin_dashboard_content(total_g, pending_g, resolved_g, total_t, open_t), u, n, get_flashed_messages(with_categories=True))

@app.route('/admin/grievances')
def admin_grievances():
    if 'admin_id' not in session:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('admin_login'))
    conn = get_db()
    grievances = conn.execute('''
        SELECT grievances.*, students.name, students.roll_number
        FROM grievances JOIN students ON grievances.student_id = students.id
        ORDER BY grievances.created_at DESC
    ''').fetchall()
    conn.close()
    u, n = build_menu_nav()
    return base_html(admin_grievances_content(grievances), u, n, get_flashed_messages(with_categories=True))

@app.route('/admin/update_grievance/<int:id>', methods=['POST'])
def update_grievance(id):
    if 'admin_id' not in session:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('admin_login'))
    status = request.form['status']
    remarks = request.form['remarks']
    conn = get_db()
    # Fetch grievance to get student_id before update
    grievance = conn.execute('SELECT * FROM grievances WHERE id=?', (id,)).fetchone()
    if grievance:
        student_id = grievance['student_id']
        conn.execute('UPDATE grievances SET status=?, remarks=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (status, remarks, id))
        conn.commit()
        # Notify student via email
        student = conn.execute('SELECT * FROM students WHERE id=?', (student_id,)).fetchone()
        if student:
            send_email(student['email'],
                       "Grievance Status Updated",
                       f"""Hello {student['name']},

Your grievance status has been updated.

Status: {status}
Remarks: {remarks}

Thank you.""")
    conn.close()
    flash('Grievance updated.', 'success')
    return redirect(url_for('admin_grievances'))

@app.route('/admin/helpdesk')
def admin_helpdesk():
    if 'admin_id' not in session:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('admin_login'))
    conn = get_db()
    tickets = conn.execute('''
        SELECT helpdesk.*, students.name, students.roll_number
        FROM helpdesk JOIN students ON helpdesk.student_id = students.id
        ORDER BY helpdesk.created_at DESC
    ''').fetchall()
    conn.close()
    u, n = build_menu_nav()
    return base_html(admin_helpdesk_content(tickets), u, n, get_flashed_messages(with_categories=True))

@app.route('/admin/update_ticket/<int:id>', methods=['POST'])
def update_ticket(id):
    if 'admin_id' not in session:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('admin_login'))
    status = request.form['status']
    admin_notes = request.form['admin_notes']
    conn = get_db()
    conn.execute('UPDATE helpdesk SET status=?, admin_notes=? WHERE id=?', (status, admin_notes, id))
    conn.commit()
    conn.close()
    flash('Ticket updated.', 'success')
    return redirect(url_for('admin_helpdesk'))

# ========== NEW: Admin Database Routes ==========
@app.route('/admin/database')
def admin_database():
    if 'admin_id' not in session:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('admin_login'))
    conn = get_db()
    # Get grievances with student info
    grievances = conn.execute('''
        SELECT 'Grievance' as type, grievances.*, students.name as student_name, students.roll_number
        FROM grievances JOIN students ON grievances.student_id = students.id
    ''').fetchall()
    # Get helpdesk with student info
    helpdesk = conn.execute('''
        SELECT 'Helpdesk' as type, helpdesk.*, students.name as student_name, students.roll_number
        FROM helpdesk JOIN students ON helpdesk.student_id = students.id
    ''').fetchall()
    # Combine and sort by created_at desc
    logs = list(grievances) + list(helpdesk)
    logs.sort(key=lambda x: x['created_at'] if x['created_at'] else '', reverse=True)
    conn.close()
    u, n = build_menu_nav()
    return base_html(admin_database_content(logs), u, n, get_flashed_messages(with_categories=True))

@app.route('/admin/database/download')
def admin_database_download():
    if 'admin_id' not in session:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('admin_login'))
    conn = get_db()
    # Fetch all grievances and helpdesk with student details
    grievances = conn.execute('''
        SELECT 'Grievance' as type, grievances.*, students.name as student_name, students.roll_number, students.email, students.branch, students.phone
        FROM grievances JOIN students ON grievances.student_id = students.id
    ''').fetchall()
    helpdesk = conn.execute('''
        SELECT 'Helpdesk' as type, helpdesk.*, students.name as student_name, students.roll_number, students.email, students.branch, students.phone
        FROM helpdesk JOIN students ON helpdesk.student_id = students.id
    ''').fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    # Write header
    writer.writerow(['Type', 'ID', 'Student Name', 'Roll Number', 'Branch', 'Student Email', 'Student Phone',
                     'Category/Subject', 'Description/Message', 'Priority', 'Status', 'Remarks/Notes',
                     'Attachment', 'Created At', 'Updated At'])
    for g in grievances:
        writer.writerow([
            g['type'],
            g['id'],
            g['student_name'],
            g['roll_number'],
            g['branch'],
            g['email'],
            g['phone'],
            g['category'],
            g['description'],
            g['priority'],
            g['status'],
            g['remarks'],
            g['attachment'],
            g['created_at'],
            g['updated_at']
        ])
    for h in helpdesk:
        writer.writerow([
            h['type'],
            h['id'],
            h['student_name'],
            h['roll_number'],
            h['branch'],
            h['email'],
            h['phone'],
            h['subject'],
            h['message'],
            h['priority'],
            h['status'],
            h['admin_notes'],
            '',  # attachment not applicable
            h['created_at'],
            h['updated_at']
        ])
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        download_name='database_export.csv',
        as_attachment=True,
        mimetype='text/csv'
    )

@app.route('/admin/database/clear', methods=['POST'])
def admin_database_clear():
    if 'admin_id' not in session:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('admin_login'))
    conn = get_db()
    conn.execute('DELETE FROM grievances')
    conn.execute('DELETE FROM helpdesk')
    conn.commit()
    conn.close()
    flash('All grievance and helpdesk records have been cleared.', 'success')
    return redirect(url_for('admin_database'))

if __name__ == '__main__':
    app.run(debug=True)
