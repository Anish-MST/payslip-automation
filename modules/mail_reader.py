import os
import base64
import config
from googleapiclient.discovery import build

def fetch_zip_from_mail(creds):
    """
    Fetches the latest ZIP attachment from unread emails matching the specific 
    Mainstreamtek payroll subject pattern.
    """
    # Initialize Gmail service with the dynamic credentials
    service = build('gmail', 'v1', credentials=creds)
    
    # NEW SEARCH QUERY: 
    # We search for "Telangana Mainstreamtek" and "Payroll" to be precise.
    # 'is:unread' ensures we don't process the same mail twice.
    # 'has:attachment' filters out emails without the ZIP.
    query = 'subject:"Telangana Mainstreamtek" subject:"Payroll" is:unread has:attachment'
    
    try:
        # Search for messages matching the query
        results = service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])

        if not messages:
            # No mail found matching the specific subject
            return None

        # Process the most recent matching email
        msg_id = messages[0]['id']
        message = service.users().messages().get(userId='me', id=msg_id).execute()
        
        payload = message.get('payload', {})
        parts = payload.get('parts', [])

        # Recursive helper to find ZIP in nested email parts (common in 'Re:' threads)
        def find_zip_attachment(parts_list):
            for part in parts_list:
                filename = part.get('filename', '')
                if filename and filename.lower().endswith('.zip'):
                    return part
                if 'parts' in part:
                    found = find_zip_attachment(part['parts'])
                    if found: return found
            return None

        target_part = find_zip_attachment(parts)

        if target_part:
            att_id = target_part['body']['attachmentId']
            filename = target_part['filename']
            
            # Download attachment
            attachment = service.users().messages().attachments().get(
                userId='me', messageId=msg_id, id=att_id
            ).execute()
            
            data = attachment['data']
            file_data = base64.urlsafe_b64decode(data.encode('UTF-8'))

            if not os.path.exists(config.TEMP_FOLDER):
                os.makedirs(config.TEMP_FOLDER, exist_ok=True)
                
            path = os.path.join(config.TEMP_FOLDER, filename)
            with open(path, 'wb') as f:
                f.write(file_data)
            
            # MARK AS READ: 
            # This is critical so the system doesn't pick up the same mail next time.
            service.users().messages().batchModify(
                userId='me',
                body={
                    'ids': [msg_id],
                    'removeLabelIds': ['UNREAD']
                }
            ).execute()
            
            return path

    except Exception as e:
        print(f"Error fetching mail with subject pattern: {e}")
        return None

    return None