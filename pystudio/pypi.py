"""Package search for the Package Manager (VS: NuGet's Browse tab).

PyPI's own search page only works in a web browser, so Browse searches this catalog of popular
packages by name, purpose and category, and looks up any exact name live with PyPI's JSON API
(latest version, summary, all versions)."""
import json
import re
import urllib.error
import urllib.request

# (name, category, what it's for)
CATALOG = [
    # Office files
    ("openpyxl", "Office files", "Read and write Excel .xlsx files"),
    ("xlsxwriter", "Office files", "Write Excel .xlsx files with formatting and charts"),
    ("xlrd", "Office files", "Read old Excel .xls files"),
    ("python-docx", "Office files", "Read and write Word .docx documents"),
    ("python-pptx", "Office files", "Create and edit PowerPoint .pptx files"),
    ("extract-msg", "Office files", "Read Outlook .msg email files"),
    ("libpff-python", "Office files", "Read Outlook PST/OST archives"),
    # PDF
    ("pypdf", "PDF", "Read, split, merge and fill PDF files"),
    ("reportlab", "PDF", "Create PDF documents and reports"),
    ("pdfplumber", "PDF", "Extract text and tables from PDFs"),
    ("pymupdf", "PDF", "Fast PDF reading, rendering and editing (fitz)"),
    ("fpdf2", "PDF", "Simple PDF generation"),
    # Data
    ("pandas", "Data", "Data tables (DataFrames): load CSV/Excel/SQL, filter, group, summarize"),
    ("numpy", "Data", "Fast arrays and math"),
    ("polars", "Data", "Very fast DataFrames"),
    ("matplotlib", "Charts", "Charts and plots"),
    ("plotly", "Charts", "Interactive charts"),
    ("seaborn", "Charts", "Statistical charts on top of matplotlib"),
    ("pyqtgraph", "Charts", "Fast live charts for Qt apps"),
    ("tabulate", "Data", "Print nice text tables"),
    ("python-dateutil", "Data", "Parse and calculate dates"),
    ("pytz", "Data", "Time zones"),
    ("pendulum", "Data", "Easier dates and time zones"),
    # Databases
    ("pyodbc", "Databases", "ODBC databases: SQL Server, DB2 for i (AS400), Access..."),
    ("ibm_db", "Databases", "IBM DB2 driver"),
    ("snowflake-connector-python", "Databases", "Snowflake data warehouse"),
    ("psycopg", "Databases", "PostgreSQL"),
    ("mysql-connector-python", "Databases", "MySQL"),
    ("oracledb", "Databases", "Oracle Database"),
    ("pymssql", "Databases", "Microsoft SQL Server"),
    ("sqlalchemy", "Databases", "SQL toolkit and ORM for any database"),
    ("duckdb", "Databases", "Fast SQL on files (CSV, Parquet) and DataFrames"),
    ("pymongo", "Databases", "MongoDB"),
    ("redis", "Databases", "Redis"),
    # Web / network
    ("requests", "Web & network", "HTTP: call web APIs, download files"),
    ("httpx", "Web & network", "HTTP with async support"),
    ("beautifulsoup4", "Web & network", "Read data out of web pages (HTML)"),
    ("lxml", "Web & network", "Fast XML and HTML parsing"),
    ("selenium", "Web & network", "Drive a real web browser"),
    ("playwright", "Web & network", "Browser automation and testing"),
    ("flask", "Web & network", "Small web apps and APIs"),
    ("fastapi", "Web & network", "Fast web APIs"),
    ("websockets", "Web & network", "WebSocket clients and servers"),
    ("paramiko", "Web & network", "SSH and SFTP"),
    ("pysnmp", "Web & network", "SNMP (network devices)"),
    ("scapy", "Web & network", "Network packets"),
    ("dnspython", "Web & network", "DNS lookups"),
    ("feedparser", "Web & network", "RSS/Atom feeds"),
    ("m3u8", "Web & network", "Parse HLS/IPTV .m3u8 playlists"),
    # Email & messaging
    ("exchangelib", "Email", "Microsoft Exchange / Outlook 365 mail and calendars"),
    ("yagmail", "Email", "Send email easily (Gmail)"),
    ("twilio", "Email", "Send SMS text messages"),
    ("slack-sdk", "Email", "Post to Slack"),
    # Images & media
    ("pillow", "Images & media", "Open, resize, convert and edit images (PIL)"),
    ("opencv-python", "Images & media", "Computer vision, webcams, video frames"),
    ("qrcode", "Images & media", "Make QR codes"),
    ("python-barcode", "Images & media", "Make barcodes"),
    ("pytesseract", "Images & media", "Read text from images (OCR, needs tesseract)"),
    ("imageio", "Images & media", "Read and write images and video"),
    ("moviepy", "Images & media", "Edit video: cut, join, add audio"),
    ("ffmpeg-python", "Images & media", "Use ffmpeg from Python (convert/trim video)"),
    ("yt-dlp", "Images & media", "Download online videos"),
    ("mutagen", "Images & media", "Read and write audio tags (MP3, MP4, FLAC)"),
    ("pydub", "Images & media", "Edit audio (needs ffmpeg)"),
    ("sounddevice", "Images & media", "Play and record audio"),
    ("python-vlc", "Images & media", "Play media with VLC"),
    ("cairosvg", "Images & media", "Convert SVG to PNG/PDF"),
    # Files & system
    ("psutil", "Files & system", "CPU, memory, disks, processes"),
    ("watchdog", "Files & system", "Watch folders for file changes"),
    ("send2trash", "Files & system", "Delete files to the trash"),
    ("pyserial", "Files & system", "Serial ports (USB devices, GRBL, Arduino)"),
    ("pyusb", "Files & system", "USB devices"),
    ("pyudev", "Files & system", "Linux device events (USB plugged in)"),
    ("python-magic", "Files & system", "Detect file types"),
    ("py7zr", "Files & system", "7-Zip archives"),
    ("rarfile", "Files & system", "RAR archives"),
    ("keyring", "Files & system", "Store passwords in the system keyring"),
    ("python-dotenv", "Files & system", "Load settings from a .env file"),
    ("pyyaml", "Files & system", "YAML files"),
    ("tomli-w", "Files & system", "Write TOML files"),
    ("chardet", "Files & system", "Guess a text file's encoding"),
    ("schedule", "Files & system", "Run jobs on a schedule"),
    ("apscheduler", "Files & system", "Advanced job scheduling"),
    ("pyinstaller", "Files & system", "Package apps (PyStudio's Publish uses it)"),
    # GUI
    ("PySide6", "GUI", "Qt for Python - what PyStudio apps are built with"),
    ("qtawesome", "GUI", "Font Awesome icons for Qt"),
    ("qdarkstyle", "GUI", "Dark theme for Qt apps"),
    ("pyqtdarktheme", "GUI", "Dark/light themes for Qt apps"),
    ("pynput", "GUI", "Control and watch the mouse and keyboard"),
    ("pyautogui", "GUI", "Automate the mouse and keyboard, screenshots"),
    ("plyer", "GUI", "Desktop notifications"),
    ("pystray", "GUI", "System tray icons"),
    # Text & AI
    ("rapidfuzz", "Text", "Fuzzy text matching"),
    ("regex", "Text", "More powerful regular expressions"),
    ("jinja2", "Text", "Text/HTML templates"),
    ("markdown", "Text", "Convert Markdown to HTML"),
    ("anthropic", "Text", "Claude API"),
    ("openai", "Text", "OpenAI API"),
    ("scikit-learn", "Text", "Machine learning"),
    # Testing & quality
    ("pytest", "Testing & tools", "Unit tests (PyStudio's Test Explorer uses it)"),
    ("pytest-qt", "Testing & tools", "Test Qt apps with pytest"),
    ("black", "Testing & tools", "Code formatter (Format Document)"),
    ("ruff", "Testing & tools", "Very fast linter and formatter"),
    ("mypy", "Testing & tools", "Check type hints"),
    ("faker", "Testing & tools", "Fake names, addresses... for test data"),
    ("rich", "Testing & tools", "Colorful console output and tables"),
    ("tqdm", "Testing & tools", "Progress bars"),
    ("loguru", "Testing & tools", "Easy logging"),
    ("click", "Testing & tools", "Command-line apps"),
    ("typer", "Testing & tools", "Command-line apps with type hints"),
    # Maps & geo
    ("geopy", "Maps & geo", "Geocoding and distances between places"),
    ("folium", "Maps & geo", "Interactive maps (HTML, show in a WebBrowser)"),
    ("haversine", "Maps & geo", "Distance between two GPS points"),
    ("timezonefinder", "Maps & geo", "Time zone for a GPS position"),
]


def search_catalog(text, limit=60):
    """Best catalog matches for a search: [(name, category, description)]."""
    words = [w for w in re.split(r"\s+", text.lower().strip()) if w]
    if not words:
        return list(CATALOG[:limit])
    scored = []
    for name, cat, desc in CATALOG:
        n, hay = name.lower(), f"{name} {cat} {desc}".lower()
        if not all(w in hay for w in words):
            continue
        score = 0
        for w in words:
            score += 100 if n == w else 50 if n.startswith(w) else 20 if w in n else 5 if w in cat.lower() else 1
        scored.append((-score, name.lower(), (name, cat, desc)))
    scored.sort()
    return [x[2] for x in scored[:limit]]


def lookup(name, timeout=10):
    """PyPI JSON for one exact package name: {"name", "version", "summary", "home", "versions"}
    or None when there's no such package. Raises OSError on network problems."""
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return None
    req = urllib.request.Request(f"https://pypi.org/pypi/{name}/json",
                                 headers={"User-Agent": "PyStudio package manager"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise OSError(f"PyPI answered {e.code}") from None
    except urllib.error.URLError as e:
        raise OSError(f"Can't reach pypi.org ({e.reason})") from None
    info = data.get("info", {})
    versions = [v for v, files in data.get("releases", {}).items() if files]

    def key(v):
        return [int(p) if p.isdigit() else -1 for p in re.split(r"[.+-]", v)]
    try:
        versions.sort(key=key, reverse=True)
    except TypeError:
        versions.sort(reverse=True)
    home = info.get("home_page") or ""
    for k, v in (info.get("project_urls") or {}).items():
        if not home and k.lower() in ("homepage", "home", "source", "documentation"):
            home = v
    return {"name": info.get("name", name), "version": info.get("version", ""), "summary": info.get("summary") or "",
            "home": home or f"https://pypi.org/project/{name}/", "versions": versions[:40],
            "requires_python": info.get("requires_python") or "", "license": info.get("license") or ""}
