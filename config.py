import os
from dotenv import load_dotenv

load_dotenv()

# --- DYNAMIC SETTINGS ---
# The Backend URL (e.g., https://your-backend.onrender.com)
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
# The Frontend URL (e.g., https://your-app.vercel.app)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# --- GOOGLE OAUTH ---
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# Create a dictionary for the flow to avoid needing a physical client_secret.json file
GOOGLE_CLIENT_CONFIG = {
    "web": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [f"{BACKEND_URL}/auth/callback"],
    }
}

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]

# --- DRIVE & SHEETS ---
GENERAL_FOLDER_ID = os.getenv("GENERAL_FOLDER_ID")
SENT_FOLDER_ID = os.getenv("SENT_FOLDER_ID")
ERROR_FOLDER_ID = os.getenv("ERROR_FOLDER_ID")
LOGGER_SHEET_ID = os.getenv("LOGGER_SHEET_ID")
MASTER_SHEET_ID = os.getenv("MASTER_SHEET_ID")

# --- PATHS ---
TEMP_FOLDER = "/tmp/payslip_temp" # Use /tmp for Render's ephemeral storage
EXTRACTED_FOLDER = "/tmp/payslip_extracted"