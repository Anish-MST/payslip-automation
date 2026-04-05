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
from modules.mail_sender import send_employee_mail # <--- ENSURE THIS IS HERE

# --- Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payslip_automation")
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="MainstreamTek Production Pipeline")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

USER_SESSIONS: Dict[str, dict] = {}

def get_session_id(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]
    return None

def add_log(session_id: str, message: str):
    if session_id in USER_SESSIONS:
        ist_now = datetime.now(IST).strftime("%H:%M:%S")
        USER_SESSIONS[session_id]["logs"].append(f"[{ist_now}] {message}")
        logger.info(f"Session {session_id[:8]}: {message}")

def get_creds_from_session(session_id: str):
    session = USER_SESSIONS.get(session_id)
    if not session or not session.get("creds"): return None
    try:
        creds_dict = session["creds"]
        creds = Credentials(**creds_dict)
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            session['creds']['token'] = creds.token
        return creds
    except Exception as e:
        logger.error(f"Cred error: {e}")
        return None

# --- API ---

@app.get("/auth/login")
def login():
    session_id = str(uuid.uuid4())
    USER_SESSIONS[session_id] = {"creds": None, "logs": [], "is_running": False, "last_run": None}
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": f"{os.getenv('BACKEND_URL').rstrip('/')}/auth/callback",
        "response_type": "code",
        "scope": " ".join(list(config.SCOPES)),
        "access_type": "offline", "prompt": "consent", "state": session_id, "include_granted_scopes": "true"
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return {"url": url}

@app.get("/auth/callback")
def auth_callback(code: str, state: str):
    if state not in USER_SESSIONS: return RedirectResponse(url=f"{os.getenv('FRONTEND_URL')}?error=session")
    try:
        token_data = {
            'code': code, 'client_id': os.getenv("GOOGLE_CLIENT_ID"),
            'client_secret': os.getenv("GOOGLE_CLIENT_SECRET"),
            'redirect_uri': f"{os.getenv('BACKEND_URL').rstrip('/')}/auth/callback",
            'grant_type': 'authorization_code',
        }
        res = requests.post("https://oauth2.googleapis.com/token", data=token_data).json()
        USER_SESSIONS[state]["creds"] = {
            "token": res['access_token'], "refresh_token": res.get('refresh_token'),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"), "scopes": config.SCOPES
        }
        return RedirectResponse(url=f"{os.getenv('FRONTEND_URL')}/?session_id={state}")
    except Exception as e:
        return RedirectResponse(url=f"{os.getenv('FRONTEND_URL')}?error=auth")

@app.get("/auth/status")
def get_auth_status(request: Request):
    creds = get_creds_from_session(get_session_id(request))
    if creds:
        try:
            user_info = build('oauth2', 'v2', credentials=creds).userinfo().get().execute()
            return {"authenticated": True, "email": user_info['email']}
        except: pass
    return {"authenticated": False}

@app.post("/auth/logout")
def logout(request: Request):
    sid = get_session_id(request)
    if sid in USER_SESSIONS: del USER_SESSIONS[sid]
    return {"message": "Logged out"}

@app.get("/status")
def get_status(request: Request):
    session = USER_SESSIONS.get(get_session_id(request))
    if not session: return {"is_running": False, "logs": [], "last_run": None}
    return {"is_running": session["is_running"], "logs": session["logs"][-50:], "last_run": session["last_run"]}

@app.post("/start")
def start_process(background_tasks: BackgroundTasks, request: Request):
    sid = get_session_id(request)
    session = USER_SESSIONS.get(sid)
    creds = get_creds_from_session(sid)
    if not creds or not session: raise HTTPException(status_code=401)
    if session["is_running"]: raise HTTPException(status_code=400)
    background_tasks.add_task(run_automation_pipeline, sid, creds)
    return {"message": "Started"}

# --- Pipeline ---

def run_automation_pipeline(session_id, creds):
    session = USER_SESSIONS[session_id]
    session["is_running"] = True
    session["logs"] = []
    add_log(session_id, "🚀 INITIALIZING PIPELINE (IST MODE)...")

    try:
        # 1. Master Sheet
        sheets = build('sheets', 'v4', credentials=creds)
        res = sheets.spreadsheets().values().get(spreadsheetId=config.MASTER_SHEET_ID, range="Sheet1!A1:Z200").execute()
        rows = [r for r in res.get('values', []) if any(c.strip() for c in r if c)]
        headers = [h.strip() for h in rows[0]]
        master_df = pd.DataFrame([dict(zip(headers, r + [""]*(len(headers)-len(r)))) for r in rows[1:]])
        add_log(session_id, f"✅ Master Data Loaded: {len(master_df)} employees.")

        # 2. Get ZIPs
        zip_paths = fetch_zip_from_mail(creds)
        if not zip_paths:
            add_log(session_id, "⚠️ No new unread payroll emails found.")
            return
        add_log(session_id, f"📬 Found {len(zip_paths)} email(s) to process.")

        # 3. Processing Loop
        month_cache = {}
        for zip_path in zip_paths:
            if not zip_path or not os.path.isfile(zip_path):
                continue

            zip_name = os.path.basename(zip_path)
            add_log(session_id, f"📦 Processing ZIP: {zip_name}")
            
            try:
                pdf_folder = extract_zip(zip_path)
                pdf_files = [os.path.join(r, f) for r, d, fs in os.walk(pdf_folder) for f in fs if f.lower().endswith('.pdf')]
                
                for path in pdf_files:
                    data = extract_employee_name(path)
                    if not data: continue
                    
                    emp_name = data.get("Employee Name", "Unknown")
                    month = data.get("Month", "Unknown")

                    # DUPLICATE PREVENTION: Skip if PASS
                    if month not in month_cache:
                        month_cache[month] = get_already_passed_employees(month, creds)
                    
                    if emp_name in month_cache[month]:
                        add_log(session_id, f"   - ⏭️ SKIP: {emp_name} (Already PASS)")
                        continue

                    add_log(session_id, f"   - Validating: {emp_name}")
                    file_id = upload_to_drive(path, creds)
                    val_res = validate_employee(data, master_df)
                    
                    if val_res["status"] == "PASS":
                        add_log(session_id, f"   - ✅ SENDING EMAIL...")
                        # FIXED: Using emp_name variable instead of 'name'
                        send_employee_mail(val_res["email"], path, emp_name, month, creds)
                        move_file(file_id, config.SENT_FOLDER_ID, month, creds)
                    else:
                        add_log(session_id, f"   - ❌ FAIL: {val_res.get('reason')}")
                        move_file(file_id, config.ERROR_FOLDER_ID, month, creds)

                    # Update or Overwrite Log Sheet
                    update_report(emp_name, val_res, month, creds)

                if os.path.exists(pdf_folder): shutil.rmtree(pdf_folder)
            except Exception as e:
                add_log(session_id, f"   - ❌ Error in ZIP {zip_name}: {str(e)}")

        add_log(session_id, "🎉 ALL TASKS FINISHED.")

    except Exception as e:
        add_log(session_id, f"🚨 CRITICAL ERROR: {str(e)}")
        logger.error(traceback.format_exc())
    finally:
        session["is_running"] = False
        session["last_run"] = datetime.now(IST).isoformat()
        try:
            if os.path.exists(config.TEMP_FOLDER): shutil.rmtree(config.TEMP_FOLDER)
        except: pass

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)