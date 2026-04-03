import os
import shutil
import logging
import traceback
import json
import requests
import urllib.parse
import uuid
from datetime import datetime
from typing import Dict, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response
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
from modules.sheet_logger import update_report, get_already_passed_employees # Added helper
from modules.mail_sender import send_employee_mail

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payslip_automation")

app = FastAPI(title="MainstreamTek Production Pipeline")

# --- Middleware ---
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip('/')

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Session Storage ---
USER_SESSIONS: Dict[str, dict] = {}

def get_session_id(request: Request) -> Optional[str]:
    """Extracts session_id from Bearer token in headers."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]
    return None

def add_log(session_id: str, message: str):
    """Adds logs to a specific user session."""
    if session_id in USER_SESSIONS:
        timestamp = datetime.now().strftime("%H:%M:%S")
        USER_SESSIONS[session_id]["logs"].append(f"[{timestamp}] {message}")
        logger.info(f"Session {session_id[:8]}: {message}")

def get_creds_from_session(session_id: str):
    """Refreshes and returns credentials for a session."""
    session = USER_SESSIONS.get(session_id)
    if not session or not session.get("creds"):
        return None
    try:
        creds_dict = session["creds"]
        creds = Credentials(
            token=creds_dict['token'],
            refresh_token=creds_dict.get('refresh_token'),
            token_uri=creds_dict['token_uri'],
            client_id=creds_dict['client_id'],
            client_secret=creds_dict['client_secret'],
            scopes=creds_dict['scopes']
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            session['creds']['token'] = creds.token
        return creds
    except Exception as e:
        logger.error(f"Cred refresh error: {e}")
        return None

# --- Auth Endpoints ---

@app.get("/auth/login")
def login():
    """Manual URL construction to bypass PKCE verifier issues on Render."""
    session_id = str(uuid.uuid4())
    USER_SESSIONS[session_id] = {
        "creds": None, "logs": [], "is_running": False, "last_run": None
    }
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": f"{os.getenv('BACKEND_URL').rstrip('/')}/auth/callback",
        "response_type": "code",
        "scope": " ".join(config.SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": session_id,
        "include_granted_scopes": "true"
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return {"url": url}

@app.get("/auth/callback")
def auth_callback(code: str, state: str):
    """Exchanges code for tokens and redirects with session_id in URL."""
    session_id = state 
    if session_id not in USER_SESSIONS:
        return RedirectResponse(url=f"{FRONTEND_URL}?error=invalid_session")
    try:
        token_data = {
            'code': code,
            'client_id': os.getenv("GOOGLE_CLIENT_ID"),
            'client_secret': os.getenv("GOOGLE_CLIENT_SECRET"),
            'redirect_uri': f"{os.getenv('BACKEND_URL').rstrip('/')}/auth/callback",
            'grant_type': 'authorization_code',
        }
        res = requests.post("https://oauth2.googleapis.com/token", data=token_data).json()
        if 'error' in res:
            raise Exception(res.get('error_description', res['error']))

        USER_SESSIONS[session_id]["creds"] = {
            "token": res['access_token'],
            "refresh_token": res.get('refresh_token'),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "scopes": config.SCOPES
        }
        return RedirectResponse(url=f"{FRONTEND_URL}/?session_id={session_id}")
    except Exception as e:
        logger.error(f"Callback Error: {e}")
        return RedirectResponse(url=f"{FRONTEND_URL}?error=auth_failed")

@app.get("/auth/status")
def get_auth_status(request: Request):
    session_id = get_session_id(request)
    creds = get_creds_from_session(session_id)
    if creds:
        try:
            service = build('oauth2', 'v2', credentials=creds)
            user_info = service.userinfo().get().execute()
            return {"authenticated": True, "email": user_info['email']}
        except: pass
    return {"authenticated": False}

@app.post("/auth/logout")
def logout(request: Request):
    session_id = get_session_id(request)
    if session_id and session_id in USER_SESSIONS:
        del USER_SESSIONS[session_id]
    return {"message": "Logged out"}

# --- Pipeline Endpoints ---

@app.get("/status")
def get_status(request: Request):
    session_id = get_session_id(request)
    session = USER_SESSIONS.get(session_id)
    if not session:
        return {"is_running": False, "logs": [], "last_run": None}
    return {
        "is_running": session["is_running"],
        "logs": session["logs"][-50:],
        "last_run": session["last_run"]
    }

@app.post("/start")
def start_process(background_tasks: BackgroundTasks, request: Request):
    session_id = get_session_id(request)
    session = USER_SESSIONS.get(session_id)
    creds = get_creds_from_session(session_id)
    if not creds or not session:
        raise HTTPException(status_code=401, detail="Please login first.")
    if session["is_running"]:
        raise HTTPException(status_code=400, detail="Automation is already running.")
    
    background_tasks.add_task(run_automation_pipeline, session_id, creds)
    return {"message": "Started"}

# --- Core Pipeline Logic ---

def run_automation_pipeline(session_id, creds):
    session = USER_SESSIONS[session_id]
    session["is_running"] = True
    session["logs"] = []
    add_log(session_id, "🚀 INITIALIZING PIPELINE...")

    try:
        # 1. Sync Master Sheet
        sheets_service = build('sheets', 'v4', credentials=creds)
        res = sheets_service.spreadsheets().values().get(
            spreadsheetId=config.MASTER_SHEET_ID, range="Sheet1!A1:Z200"
        ).execute()
        raw_rows = res.get('values', [])
        rows = [r for r in raw_rows if any(cell.strip() for cell in r if cell)]
        headers = [h.strip() for h in rows[0]]
        master_df = pd.DataFrame([dict(zip(headers, r + [""]*(len(headers)-len(r)))) for r in rows[1:]])
        add_log(session_id, f"✅ Master Data Loaded: {len(master_df)} records.")

        # 2. Fetch Multiple ZIPs from Gmail
        zip_paths = fetch_zip_from_mail(creds)
        if not zip_paths:
            add_log(session_id, "⚠️ No unread payroll ZIPs found in Gmail.")
            return
        add_log(session_id, f"📬 Found {len(zip_paths)} email(s) to process.")

        # 3. Process each ZIP
        for zip_path in zip_paths:
            add_log(session_id, f"📦 Extracting: {os.path.basename(zip_path)}")
            pdf_folder = extract_zip(zip_path)
            pdf_files = [os.path.join(r, f) for r, d, fs in os.walk(pdf_folder) for f in fs if f.lower().endswith('.pdf')]
            
            # Month-based caching for "already passed" employees to save API calls
            month_cache = {}

            for path in pdf_files:
                name = os.path.basename(path)
                
                # A. Parse PDF to get Name/Month
                extracted_data = extract_employee_name(path)
                if not extracted_data:
                    add_log(session_id, f"   - ❌ Parse Error: {name}")
                    continue

                emp_name = extracted_data.get("Employee Name", "Unknown")
                month = extracted_data.get("Month", "Unknown")

                # B. DUPLICATE CHECK: Skip if already PASS in Log Sheet
                if month not in month_cache:
                    month_cache[month] = get_already_passed_employees(month, creds)
                
                if emp_name in month_cache[month]:
                    add_log(session_id, f"   - ⏭️ SKIPPING: {emp_name} (Already PASS in {month})")
                    continue

                # C. Processing
                add_log(session_id, f"   - Processing: {emp_name}")
                file_id = upload_to_drive(path, creds)
                val_res = validate_employee(extracted_data, master_df)
                
                if val_res["status"] == "PASS":
                    add_log(session_id, f"   - ✅ VALIDATED: Sending Email...")
                    send_employee_mail(val_res["email"], path, emp_name, month, creds)
                    move_file(file_id, config.SENT_FOLDER_ID, month, creds)
                else:
                    add_log(session_id, f"   - ❌ FAIL: {val_res.get('reason')}")
                    move_file(file_id, config.ERROR_FOLDER_ID, month, creds)

                # D. Update Report
                update_report(emp_name, val_res, month, creds)

            if os.path.exists(pdf_folder):
                shutil.rmtree(pdf_folder)

        add_log(session_id, "🎉 PIPELINE FINISHED SUCCESSFULLY.")

    except Exception as e:
        add_log(session_id, f"🚨 CRITICAL ERROR: {str(e)}")
        logger.error(traceback.format_exc())
    finally:
        session["is_running"] = False
        session["last_run"] = datetime.now().isoformat()
        try:
            if os.path.exists(config.TEMP_FOLDER): shutil.rmtree(config.TEMP_FOLDER)
        except: pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))