import os
import base64
import config
from googleapiclient.discovery import build

def fetch_zip_from_mail(creds):
    """
    Fetches the latest ZIP attachment from unread emails.
    Uses 'creds' passed from the dynamic production login session.
    """
    # Initialize Gmail service with the dynamic credentials
    service = build('gmail', 'v1', credentials=creds)
    
    # 1. Broad Search: Subject contains "payslip", is unread, and has an attachment
    query = "subject:payslip is:unread has:attachment"
    
    try:
        results = service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])

        if not messages:
            # We return None so main.py can log "No unread emails found"
            return None

        # 2. Process the latest matching email
        msg_id = messages[0]['id']
        message = service.users().messages().get(userId='me', id=msg_id).execute()
        
        payload = message.get('payload', {})
        parts = payload.get('parts', [])

        # Recursive helper to find ZIP in nested email parts
        def find_zip_attachment(parts_list):
            for part in parts_list:
                filename = part.get('filename', '')
                # Check if part is a ZIP file
                if filename and filename.lower().endswith('.zip'):
                    return part
                # If this part has sub-parts (like an inline image + attachment), check them too
                if 'parts' in part:
                    found = find_zip_attachment(part['parts'])
                    if found: return found
            return None

        target_part = find_zip_attachment(parts)

        if target_part:
            att_id = target_part['body']['attachmentId']
            filename = target_part['filename']
            
            # Download attachment data
            attachment = service.users().messages().attachments().get(
                userId='me', messageId=msg_id, id=att_id
            ).execute()
            
            data = attachment['data']
            file_data = base64.urlsafe_b64decode(data.encode('UTF-8'))

            # Ensure temp folder exists (Production uses /tmp/...)
            if not os.path.exists(config.TEMP_FOLDER):
                os.makedirs(config.TEMP_FOLDER, exist_ok=True)
                
            path = os.path.join(config.TEMP_FOLDER, filename)
            with open(path, 'wb') as f:
                f.write(file_data)
            
            # 3. Mark email as READ (Remove 'UNREAD' label) 
            service.users().messages().batchModify(
                userId='me',
                body={
                    'ids': [msg_id],
                    'removeLabelIds': ['UNREAD']
                }
            ).execute()
            
            return path

    except Exception as e:
        # In production, we log the error but don't crash the loop
        print(f"Error fetching mail: {e}")
        return None

    return None