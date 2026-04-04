import os
import shutil
import logging
import traceback
import json
import requests
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
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
from modules.sheet_logger import update_report, get_already_passed_employees

# --- Logging & Timezone Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payslip_automation")

# IST Timezone constant
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="MainstreamTek Production Pipeline")

# --- Middleware (CORS) ---
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip('/')
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # For production, use [FRONTEND_URL]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Session Storage ---
# Dictionary to store user sessions in RAM
USER_SESSIONS: Dict[str, dict] = {}

def get_session_id(request: Request) -> Optional[str]:
    """Helper to extract session_id from the Bearer token in headers."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]
    return None

def add_log(session_id: str, message: str):
    """Adds a log entry with IST timestamp to the user's specific session."""
    if session_id in USER_SESSIONS:
        ist_now = datetime.now(IST).strftime("%H:%M:%S")
        USER_SESSIONS[session_id]["logs"].append(f"[{ist_now}] {message}")
        logger.info(f"Session {session_id[:8]}: {message}")

def get_creds_from_session(session_id: str):
    """Refreshes and returns Google Credentials from the session dict."""
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
            # Update the stored token in RAM
            session['creds']['token'] = creds.token
        return creds
    except Exception as e:
        logger.error(f"Credential refresh failed: {e}")
        return None

# --- API Endpoints ---

@app.get("/auth/login")
def login():
    """Constructs a manual Login URL to bypass PKCE/verifier issues."""
    session_id = str(uuid.uuid4())
    # Pre-initialize session for the state
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
        "state": session_id, # Session ID is passed through Google as state
        "include_granted_scopes": "true"
    }
    
    base_url = "https://accounts.google.com/o/oauth2/v2/auth"
    auth_url = f"{base_url}?{urllib.parse.urlencode(params)}"
    return {"url": auth_url}

@app.get("/auth/callback")
def auth_callback(code: str, state: str):
    """Google returns the code and our session_id (state)."""
    session_id = state 
    if session_id not in USER_SESSIONS:
        return RedirectResponse(url=f"{FRONTEND_URL}?error=invalid_session")

    try:
        # Manual Token Exchange to bypass library-specific errors
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
        
        # Redirect back to Vercel with the session_id as a parameter
        return RedirectResponse(url=f"{FRONTEND_URL}/?session_id={session_id}")

    except Exception as e:
        logger.error(f"OAuth Callback Error: {e}")
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
    if session_id in USER_SESSIONS:
        del USER_SESSIONS[session_id]
    return {"message": "Logged out successfully"}

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
        raise HTTPException(status_code=400, detail="Automation already running.")
    
    background_tasks.add_task(run_automation_pipeline, session_id, creds)
    return {"message": "Pipeline started"}

# --- Core Pipeline Logic ---

def run_automation_pipeline(session_id, creds):
    session = USER_SESSIONS[session_id]
    session["is_running"] = True
    session["logs"] = [] # Clear logs for fresh run
    add_log(session_id, "🚀 INITIALIZING PIPELINE (IST MODE)...")

    try:
        # 1. Sync with Master Google Sheet
        sheets_service = build('sheets', 'v4', credentials=creds)
        res = sheets_service.spreadsheets().values().get(
            spreadsheetId=config.MASTER_SHEET_ID, range="Sheet1!A1:Z200"
        ).execute()
        
        raw_rows = res.get('values', [])
        rows = [r for r in raw_rows if any(cell.strip() for cell in r if cell)]
        headers = [h.strip() for h in rows[0]]
        master_df = pd.DataFrame([dict(zip(headers, r + [""]*(len(headers)-len(r)))) for r in rows[1:]])
        add_log(session_id, f"✅ Master Data Loaded: {len(master_df)} employees found.")

        # 2. Fetch all Unread Payroll ZIPs from Gmail
        zip_paths = fetch_zip_from_mail(creds)
        if not zip_paths:
            add_log(session_id, "⚠️ No new unread payroll ZIPs found in Gmail.")
            return
        add_log(session_id, f"📬 Found {len(zip_paths)} email(s) to process.")

        # 3. Processing Loop (For each ZIP)
        month_cache = {} # Used for duplicate prevention

        for zip_path in zip_paths:
            zip_name = os.path.basename(zip_path)
            add_log(session_id, f"📦 Processing ZIP: {zip_name}")
            
            pdf_folder = extract_zip(zip_path)
            pdf_files = [os.path.join(r, f) for r, d, fs in os.walk(pdf_folder) for f in fs if f.lower().endswith('.pdf')]
            
            for path in pdf_files:
                pdf_name = os.path.basename(path)
                
                # A. Parse PDF
                data = extract_employee_name(path)
                if not data:
                    add_log(session_id, f"   - ❌ Parse Error: {pdf_name}")
                    continue

                emp_name = data.get("Employee Name", "Unknown")
                month = data.get("Month", "Unknown")

                # B. DUPLICATE CHECK (Logger Sheet based)
                if month not in month_cache:
                    # Fetch list of people who are already marked 'PASS' for this month
                    month_cache[month] = get_already_passed_employees(month, creds)
                
                if emp_name in month_cache[month]:
                    add_log(session_id, f"   - ⏭️ SKIPPING: {emp_name} (Already PASS in {month})")
                    continue

                # C. Processing (Upload, Validate, Mail)
                add_log(session_id, f"   - Validating: {emp_name}")
                file_id = upload_to_drive(path, creds)
                val_res = validate_employee(data, master_df)
                
                if val_res["status"] == "PASS":
                    add_log(session_id, f"   - ✅ VALIDATED: Sending Email...")
                    send_employee_mail(val_res["email"], path, emp_name, month, creds)
                    move_file(file_id, config.SENT_FOLDER_ID, month, creds)
                else:
                    # On FAIL, move to Error folder and log reason
                    add_log(session_id, f"   - ❌ FAIL: {val_res.get('reason')}")
                    move_file(file_id, config.ERROR_FOLDER_ID, month, creds)

                # D. Update/Overwrite Log Sheet (IST + Update logic inside)
                update_report(emp_name, val_res, month, creds)

            # Clean up individual zip's extracted folder
            if os.path.exists(pdf_folder):
                shutil.rmtree(pdf_folder)

        add_log(session_id, "🎉 ALL TASKS FINISHED SUCCESSFULLY.")

    except Exception as e:
        add_log(session_id, f"🚨 CRITICAL ERROR: {str(e)}")
        logger.error(traceback.format_exc())
    finally:
        session["is_running"] = False
        # Save last run time in IST
        session["last_run"] = datetime.now(IST).isoformat()
        try:
            # Final cleanup of temp root
            if os.path.exists(config.TEMP_FOLDER):
                shutil.rmtree(config.TEMP_FOLDER)
        except: pass

if __name__ == "__main__":
    import uvicorn
    # Render provides PORT environment variable
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)