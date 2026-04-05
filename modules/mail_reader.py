import os
import base64
import config
from googleapiclient.discovery import build

def fetch_zip_from_mail(creds):
    """
    Finds unread payroll emails and downloads ZIPs to /tmp/
    """
    service = build('gmail', 'v1', credentials=creds)
    query = 'subject:"Telangana Mainstreamtek" subject:"Payroll" is:unread has:attachment'
    zip_file_paths = []

    try:
        results = service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])

        if not messages:
            return []

        # Ensure we use the absolute /tmp path for Render compatibility
        temp_dir = config.TEMP_FOLDER 
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)

        for msg_info in messages:
            msg_id = msg_info['id']
            message = service.users().messages().get(userId='me', id=msg_id).execute()
            
            payload = message.get('payload', {})
            parts = payload.get('parts', [])

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

            if target_part and target_part.get('filename'):
                att_id = target_part['body']['attachmentId']
                # Create a specific filename using the message ID
                filename = f"payroll_{msg_id}.zip"
                
                attachment = service.users().messages().attachments().get(
                    userId='me', messageId=msg_id, id=att_id
                ).execute()
                
                file_data = base64.urlsafe_b64decode(attachment['data'].encode('UTF-8'))
                path = os.path.join(temp_dir, filename)
                
                with open(path, 'wb') as f:
                    f.write(file_data)
                
                # Only add if it's a valid file
                if os.path.isfile(path):
                    zip_file_paths.append(path)

            # Mark as READ regardless of whether a ZIP was found 
            # (Prevents broken emails from blocking the queue)
            service.users().messages().batchModify(
                userId='me',
                body={'ids': [msg_id], 'removeLabelIds': ['UNREAD']}
            ).execute()

        return zip_file_paths

    except Exception as e:
        print(f"Error in mail_reader: {e}")
        return []