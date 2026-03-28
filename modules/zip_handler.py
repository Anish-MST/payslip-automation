import zipfile
import os
import config
import shutil

def extract_zip(zip_file):
    # Clean extracted folder if it exists to avoid mixing old files
    if os.path.exists(config.EXTRACTED_FOLDER):
        shutil.rmtree(config.EXTRACTED_FOLDER)
    
    os.makedirs(config.EXTRACTED_FOLDER, exist_ok=True)

    with zipfile.ZipFile(zip_file, 'r') as zip_ref:
        zip_ref.extractall(config.EXTRACTED_FOLDER)
    
    print(f"Extracted to: {config.EXTRACTED_FOLDER}")
    return config.EXTRACTED_FOLDER