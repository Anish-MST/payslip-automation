import os
import config
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

def upload_to_drive(file_path, creds):
    service = build('drive', 'v3', credentials=creds)
    file_name = os.path.basename(file_path)
    
    file_metadata = {
        'name': file_name,
        'parents': [config.GENERAL_FOLDER_ID]
    }
    
    media = MediaFileUpload(file_path, resumable=True)
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    ).execute()
    
    return file.get('id')

def get_or_create_subfolder(service, parent_id, folder_name):
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])
    
    if files:
        return files[0]['id']
    else:
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')

def move_file(file_id, target_parent_id, month_name, creds):
    service = build('drive', 'v3', credentials=creds)
    target_folder_id = get_or_create_subfolder(service, target_parent_id, month_name)
    
    # Get current parents
    file = service.files().get(fileId=file_id, fields='parents').execute()
    parents_list = file.get('parents')

    # FIX: Check if parents_list exists before joining
    if parents_list:
        previous_parents = ",".join(parents_list)
        service.files().update(
            fileId=file_id,
            addParents=target_folder_id,
            removeParents=previous_parents,
            fields='id, parents'
        ).execute()
    else:
        # If no parents found, just add the new one
        service.files().update(
            fileId=file_id,
            addParents=target_folder_id,
            fields='id, parents'
        ).execute()