import os
import shutil
import logging
import traceback
import json
import requests
import urllib.parse
import uuid  # For generating unique session IDs
from datetime import datetime
from typing import Dict

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response, Cookie
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

app = FastAPI(title="MainstreamTek Multi-User Production Pipeline")

# --- Multi-User Session Storage ---
# Structure: { "session_uuid": { "creds": {}, "logs": [], "is_running": False, "last_run": None } }
USER_SESSIONS: Dict[str, dict] = {}

# --- Middleware (CORS) ---
# IMPORTANT: For cookies/sessions to work, allow_origins CANNOT be "*"
# It must be your specific Vercel URL.
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip('/')

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True, # Crucial for cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Helpers ---

def get_session(session_id: str):
    """Retrieves session data or returns None."""
    return USER_SESSIONS.get(session_id)

def add_log(session_id: str, message: str):
    """Adds a log to a specific user's session."""
    if session_id in USER_SESSIONS:
        timestamp = datetime.now().strftime("%H:%M:%S")
        USER_SESSIONS[session_id]["logs"].append(f"[{timestamp}] {message}")
        logger.info(f"Session {session_id[:8]}: {message}")

def get_creds_from_session(session_data: dict):
    """Converts session dict back to Google Credentials."""
    if not session_data or not session_data.get("creds"):
        return None
    try:
        creds_dict = session_data["creds"]
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
            session_data['creds']['token'] = creds.token
        return creds
    except Exception as e:
        logger.error(f"Credential Refresh Error: {e}")
        return None

# --- API Endpoints ---

@app.get("/auth/login")
def login(response: Response, session_id: str = Cookie(None)):
    """Starts OAuth flow and assigns a session_id cookie if missing."""
    if not session_id or session_id not in USER_SESSIONS:
        session_id = str(uuid.uuid4())
        USER_SESSIONS[session_id] = {
            "creds": None, "logs": [], "is_running": False, "last_run": None
        }
    
    # Set cookie (valid for 24 hours)
    response.set_cookie(
        key="session_id", 
        value=session_id, 
        httponly=True, 
        samesite="none", # Required for cross-site (Vercel to Render)
        secure=True,     # Required for samesite="none"
        max_age=86400
    )

    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": f"{os.getenv('BACKEND_URL').rstrip('/')}/auth/callback",
        "response_type": "code",
        "scope": " ".join(config.SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true"
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return {"url": url}

@app.get("/auth/callback")
def auth_callback(code: str, session_id: str = Cookie(None)):
    """Exchanges code for token and saves it to the specific session."""
    if not session_id:
        return RedirectResponse(url=f"{FRONTEND_URL}?error=missing_session")

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
        return RedirectResponse(url=FRONTEND_URL)
    except Exception as e:
        logger.error(f"Callback Error: {e}")
        return RedirectResponse(url=f"{FRONTEND_URL}?error=auth_failed")

@app.get("/auth/status")
def get_auth_status(session_id: str = Cookie(None)):
    session = get_session(session_id)
    creds = get_creds_from_session(session)
    if creds:
        try:
            service = build('oauth2', 'v2', credentials=creds)
            user_info = service.userinfo().get().execute()
            return {"authenticated": True, "email": user_info['email']}
        except:
            return {"authenticated": False}
    return {"authenticated": False}

@app.post("/auth/logout")
def logout(response: Response, session_id: str = Cookie(None)):
    if session_id in USER_SESSIONS:
        del USER_SESSIONS[session_id]
    response.delete_cookie("session_id", samesite="none", secure=True)
    return {"message": "Logged out"}

@app.get("/status")
def get_status(session_id: str = Cookie(None)):
    session = get_session(session_id)
    if not session:
        return {"is_running": False, "logs": [], "last_run": None}
    return {
        "is_running": session["is_running"],
        "logs": session["logs"][-50:],
        "last_run": session["last_run"]
    }

@app.post("/start")
def start_process(background_tasks: BackgroundTasks, session_id: str = Cookie(None)):
    session = get_session(session_id)
    creds = get_creds_from_session(session)
    
    if not creds:
        raise HTTPException(status_code=401, detail="Please login first.")
    if session["is_running"]:
        raise HTTPException(status_code=400, detail="Automation already running.")
    
    background_tasks.add_task(run_automation_pipeline, session_id, creds)
    return {"message": "Pipeline started"}

# --- Pipeline Logic ---

def run_automation_pipeline(session_id, creds):
    session = USER_SESSIONS[session_id]
    session["is_running"] = True
    session["logs"] = []
    
    add_log(session_id, "🚀 INITIALIZING PIPELINE...")

    try:
        # 1. Sheet Sync
        sheets_service = build('sheets', 'v4', credentials=creds)
        res = sheets_service.spreadsheets().values().get(
            spreadsheetId=config.MASTER_SHEET_ID, range="Sheet1!A1:Z200"
        ).execute()
        
        raw_rows = res.get('values', [])
        rows = [r for r in raw_rows if any(cell.strip() for cell in r if cell)]
        if len(rows) < 2:
            add_log(session_id, "❌ Master Sheet empty.")
            return

        headers = [h.strip() for h in rows[0]]
        master_df = pd.DataFrame([dict(zip(headers, r + [""]*(len(headers)-len(r)))) for r in rows[1:]])
        add_log(session_id, f"✅ Data Loaded: {len(master_df)} records.")

        # 2. Gmail
        zip_path = fetch_zip_from_mail(creds)
        if not zip_path:
            add_log(session_id, "⚠️ No unread ZIP found.")
            return

        # 3. Extraction
        pdf_folder = extract_zip(zip_path)
        pdf_files = [os.path.join(r, f) for r, d, fs in os.walk(pdf_folder) for f in fs if f.lower().endswith('.pdf')]
        add_log(session_id, f"🔎 Found {len(pdf_files)} PDFs.")

        # 4. Processing
        for path in pdf_files:
            name = os.path.basename(path)
            add_log(session_id, f"--- Processing: {name} ---")
            
            fid = upload_to_drive(path, creds)
            data = extract_employee_name(path)
            if not data:
                add_log(session_id, "   - ❌ Parse failed.")
                continue

            emp = data.get("Employee Name", "Unknown")
            mon = data.get("Month", "Unknown")
            val_res = validate_employee(data, master_df)
            
            if val_res["status"] == "PASS":
                add_log(session_id, f"   - ✅ PASS: {emp}")
                send_employee_mail(val_res["email"], path, emp, mon, creds)
                move_file(fid, config.SENT_FOLDER_ID, mon, creds)
            else:
                add_log(session_id, f"   - ❌ FAIL: {val_res.get('reason')}")
                move_file(fid, config.ERROR_FOLDER_ID, mon, creds)

            update_report(emp, val_res, mon, creds)

        add_log(session_id, "🎉 COMPLETED.")

    except Exception as e:
        add_log(session_id, f"🚨 ERROR: {str(e)}")
        logger.error(traceback.format_exc())
    finally:
        session["is_running"] = False
        session["last_run"] = datetime.now().isoformat()
        try:
            if os.path.exists(config.TEMP_FOLDER): shutil.rmtree(config.TEMP_FOLDER)
            if os.path.exists(config.EXTRACTED_FOLDER): shutil.rmtree(config.EXTRACTED_FOLDER)
        except: pass

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)