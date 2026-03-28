import os
import shutil
import logging
import traceback
import json
import requests
import urllib.parse  # Added for manual URL construction
from datetime import datetime
from typing import List

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import pandas as pd
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

# Local Modules
import config
from modules.mail_reader import fetch_zip_from_mail
from modules.zip_handler import extract_zip
from modules.drive_manager import upload_to_drive, move_file
from modules.pdf_parser import extract_employee_name 
from modules.validator import validate_employee      
from modules.sheet_logger import update_report
from modules.mail_sender import send_employee_mail

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payslip_automation")

app = FastAPI(title="MainstreamTek Production Pipeline")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GlobalState:
    def __init__(self):
        self.is_running = False
        self.logs = []
        self.last_run = None
        self.user_creds = None 

state = GlobalState()

def add_log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    state.logs.append(f"[{timestamp}] {message}")
    logger.info(message)

def get_current_creds():
    """Helper to convert stored dict back to Google Credentials object."""
    if not state.user_creds:
        return None
    try:
        # Load from the dictionary stored in RAM
        creds = Credentials(
            token=state.user_creds['token'],
            refresh_token=state.user_creds.get('refresh_token'),
            token_uri=state.user_creds['token_uri'],
            client_id=state.user_creds['client_id'],
            client_secret=state.user_creds['client_secret'],
            scopes=state.user_creds['scopes']
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            # Update RAM storage after refresh
            state.user_creds['token'] = creds.token
        return creds
    except Exception as e:
        logger.error(f"Error refreshing credentials: {e}")
        return None

# --- API Endpoints ---

@app.get("/auth/login")
def login():
    """
    MANUAL LOGIN URL CONSTRUCTION
    We avoid using the 'flow' library here to prevent it from injecting 
    PKCE (code_challenge) which causes the 'Missing code verifier' error.
    """
    try:
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        backend_url = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip('/')
        redirect_uri = f"{backend_url}/auth/callback"
        
        # Space-separated scopes
        scope_str = " ".join(config.SCOPES)
        
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope_str,
            "access_type": "offline", # Crucial for getting a refresh_token
            "prompt": "consent",
            "include_granted_scopes": "true"
        }
        
        base_url = "https://accounts.google.com/o/oauth2/v2/auth"
        auth_url = f"{base_url}?{urllib.parse.urlencode(params)}"
        
        return {"url": auth_url}
    except Exception as e:
        logger.error(f"Login URL Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/auth/callback")
def auth_callback(code: str):
    """
    MANUAL TOKEN EXCHANGE
    Receives the code from Google and exchanges it for an Access Token.
    """
    try:
        backend_url = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip('/')
        redirect_uri = f"{backend_url}/auth/callback"

        token_data = {
            'code': code,
            'client_id': os.getenv("GOOGLE_CLIENT_ID"),
            'client_secret': os.getenv("GOOGLE_CLIENT_SECRET"),
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        }

        # POST to Google's token service
        response = requests.post("https://oauth2.googleapis.com/token", data=token_data)
        token_json = response.json()

        if 'error' in token_json:
            raise Exception(f"Google Token Error: {token_json.get('error_description', token_json['error'])}")

        # Store exactly what the automation needs
        state.user_creds = {
            "token": token_json['access_token'],
            "refresh_token": token_json.get('refresh_token'),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "scopes": config.SCOPES
        }
        
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(url=frontend_url)

    except Exception as e:
        logger.error(f"OAuth Callback Error: {e}")
        return {"error": "Authentication failed", "details": str(e)}

@app.get("/auth/status")
def get_auth_status():
    creds = get_current_creds()
    if creds:
        try:
            # Check who is logged in
            service = build('oauth2', 'v2', credentials=creds)
            user_info = service.userinfo().get().execute()
            return {"authenticated": True, "email": user_info['email']}
        except Exception:
            return {"authenticated": False}
    return {"authenticated": False}

@app.get("/status")
def get_pipeline_status():
    return {
        "is_running": state.is_running,
        "logs": state.logs[-50:], 
        "last_run": state.last_run
    }

@app.post("/start")
def start_process(background_tasks: BackgroundTasks):
    creds = get_current_creds()
    if not creds:
        raise HTTPException(status_code=401, detail="User not authenticated.")
    
    if state.is_running:
        raise HTTPException(status_code=400, detail="Automation is already running.")
    
    background_tasks.add_task(run_automation_pipeline, creds)
    return {"message": "Pipeline started."}

# --- Core Pipeline ---

def run_automation_pipeline(creds):
    state.is_running = True
    state.logs = []
    add_log("🚀 INITIALIZING PRODUCTION PIPELINE...")

    try:
        # 1. Sync Sheet
        sheets_service = build('sheets', 'v4', credentials=creds)
        sheet_result = sheets_service.spreadsheets().values().get(
            spreadsheetId=config.MASTER_SHEET_ID,
            range="Sheet1!A1:Z200" 
        ).execute()
        
        raw_rows = sheet_result.get('values', [])
        rows = [r for r in raw_rows if any(cell.strip() for cell in r if cell)]

        if len(rows) < 2:
            add_log("❌ ERROR: Master Sheet is empty.")
            return

        headers = [h.strip() for h in rows[0]]
        master_data = []
        for r in rows[1:]:
            padded_row = r + [""] * (len(headers) - len(r))
            master_data.append(dict(zip(headers, padded_row)))
            
        master_df = pd.DataFrame(master_data)
        add_log(f"✅ Master Data Loaded: {len(master_df)} employees.")

        # 2. Gmail
        add_log("📬 Scanning Gmail for 'payslip' ZIP...")
        zip_path = fetch_zip_from_mail(creds)
        
        if not zip_path:
            add_log("⚠️ No unread ZIP found.")
            return

        # 3. ZIP
        pdf_folder = extract_zip(zip_path)
        pdf_files = []
        for root, _, filenames in os.walk(pdf_folder):
            for f in filenames:
                if f.lower().endswith(".pdf"):
                    pdf_files.append(os.path.join(root, f))
        
        add_log(f"🔎 Processing {len(pdf_files)} PDF(s).")

        # 4. Processing
        for pdf_path in pdf_files:
            pdf_name = os.path.basename(pdf_path)
            add_log(f"--- Processing: {pdf_name} ---")

            file_id = upload_to_drive(pdf_path, creds)
            extracted_data = extract_employee_name(pdf_path)
            
            if not extracted_data:
                add_log(f"   - ❌ Parse Error.")
                continue

            emp_name = extracted_data.get("Employee Name", "Unknown")
            month = extracted_data.get("Month", "Unknown")
            result = validate_employee(extracted_data, master_df)
            
            if result["status"] == "PASS":
                add_log(f"   - ✅ PASS: {emp_name}")
                send_employee_mail(result["email"], pdf_path, emp_name, month, creds)
                move_file(file_id, config.SENT_FOLDER_ID, month, creds)
            else:
                add_log(f"   - ❌ FAIL: {result.get('reason')}")
                move_file(file_id, config.ERROR_FOLDER_ID, month, creds)

            update_report(emp_name, result, month, creds)

        add_log("🎉 FINISHED.")

    except Exception as e:
        add_log(f"🚨 CRITICAL ERROR: {str(e)}")
        logger.error(traceback.format_exc())
    finally:
        state.is_running = False
        state.last_run = datetime.now().isoformat()
        try:
            if os.path.exists(config.TEMP_FOLDER): shutil.rmtree(config.TEMP_FOLDER)
            if os.path.exists(config.EXTRACTED_FOLDER): shutil.rmtree(config.EXTRACTED_FOLDER)
        except: pass

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)