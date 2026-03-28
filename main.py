import os
import shutil
import logging
import traceback
import json
import requests
import urllib.parse
import uuid
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payslip_automation")

app = FastAPI(title="MainstreamTek Multi-User Pipeline")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip('/')

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True, 
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Session Storage ---
USER_SESSIONS: Dict[str, dict] = {}

def add_log(session_id: str, message: str):
    if session_id in USER_SESSIONS:
        timestamp = datetime.now().strftime("%H:%M:%S")
        USER_SESSIONS[session_id]["logs"].append(f"[{timestamp}] {message}")
        logger.info(f"Session {session_id[:8]}: {message}")

def get_creds_from_session(session_id: str):
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

# --- Endpoints ---

@app.get("/auth/login")
def login():
    """Starts OAuth flow and passes a new session_id as the 'state' parameter."""
    # Generate a temporary session ID to track this login attempt
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
        "state": session_id,  # <--- CRITICAL: Pass session ID to Google
        "include_granted_scopes": "true"
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return {"url": url}

@app.get("/auth/callback")
def auth_callback(code: str, state: str, response: Response):
    """Google returns the 'state' (session_id). We use it to save the token."""
    session_id = state 
    if session_id not in USER_SESSIONS:
        return RedirectResponse(url=f"{FRONTEND_URL}?error=invalid_state")

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

        # Save credentials to this specific session
        USER_SESSIONS[session_id]["creds"] = {
            "token": res['access_token'],
            "refresh_token": res.get('refresh_token'),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "scopes": config.SCOPES
        }
        
        # Now set the cookie so the frontend can poll /status
        response = RedirectResponse(url=FRONTEND_URL)
        response.set_cookie(
            key="session_id", 
            value=session_id, 
            httponly=True, 
            samesite="none", 
            secure=True, 
            max_age=86400
        )
        return response
    except Exception as e:
        logger.error(f"Callback Error: {e}")
        return RedirectResponse(url=f"{FRONTEND_URL}?error=auth_failed")

@app.get("/auth/status")
def get_auth_status(session_id: str = Cookie(None)):
    creds = get_creds_from_session(session_id)
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
    session = USER_SESSIONS.get(session_id)
    if not session:
        return {"is_running": False, "logs": [], "last_run": None}
    return {
        "is_running": session["is_running"],
        "logs": session["logs"][-50:],
        "last_run": session["last_run"]
    }

@app.post("/start")
def start_process(background_tasks: BackgroundTasks, session_id: str = Cookie(None)):
    session = USER_SESSIONS.get(session_id)
    creds = get_creds_from_session(session_id)
    if not creds or not session:
        raise HTTPException(status_code=401, detail="Please login first.")
    if session["is_running"]:
        raise HTTPException(status_code=400, detail="Running...")
    
    background_tasks.add_task(run_automation_pipeline, session_id, creds)
    return {"message": "Started"}

# --- Pipeline Logic ---

def run_automation_pipeline(session_id, creds):
    session = USER_SESSIONS[session_id]
    session["is_running"] = True
    session["logs"] = []
    add_log(session_id, "🚀 INITIALIZING PIPELINE...")

    try:
        sheets_service = build('sheets', 'v4', credentials=creds)
        res = sheets_service.spreadsheets().values().get(
            spreadsheetId=config.MASTER_SHEET_ID, range="Sheet1!A1:Z200"
        ).execute()
        
        raw_rows = res.get('values', [])
        rows = [r for r in raw_rows if any(cell.strip() for cell in r if cell)]
        headers = [h.strip() for h in rows[0]]
        master_df = pd.DataFrame([dict(zip(headers, r + [""]*(len(headers)-len(r)))) for r in rows[1:]])
        add_log(session_id, f"✅ Data Loaded: {len(master_df)} records.")

        zip_path = fetch_zip_from_mail(creds)
        if not zip_path:
            add_log(session_id, "⚠️ No ZIP found.")
            return

        pdf_folder = extract_zip(zip_path)
        pdf_files = [os.path.join(r, f) for r, d, fs in os.walk(pdf_folder) for f in fs if f.lower().endswith('.pdf')]

        for path in pdf_files:
            name = os.path.basename(path)
            add_log(session_id, f"Processing: {name}")
            fid = upload_to_drive(path, creds)
            data = extract_employee_name(path)
            if not data: continue
            emp, mon = data.get("Employee Name", "Unknown"), data.get("Month", "Unknown")
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
    finally:
        session["is_running"] = False
        session["last_run"] = datetime.now().isoformat()
        try:
            if os.path.exists(config.TEMP_FOLDER): shutil.rmtree(config.TEMP_FOLDER)
            if os.path.exists(config.EXTRACTED_FOLDER): shutil.rmtree(config.EXTRACTED_FOLDER)
        except: pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))