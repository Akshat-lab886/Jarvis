"""
Jarvis File Organizer Module
Helps keep the user's Downloads folder organized by sorting files into categories.
"""

import os
import shutil

# Path to user's Downloads folder
DOWNLOADS_PATH = os.path.expanduser("~/Downloads")

# File type categories
FILE_CATEGORIES = {
    'Images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico', '.heic'],
    'Docs': ['.pdf', '.docx', '.doc', '.txt', '.xlsx', '.xls', '.pptx', '.ppt', '.csv', '.rtf', '.odt'],
    'Media': ['.mp4', '.mp3', '.mov', '.avi', '.mkv', '.wav', '.flac', '.aac', '.m4a', '.wmv'],
    'Archives': ['.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.dmg', '.iso'],
    'Code': ['.py', '.js', '.html', '.css', '.json', '.xml', '.sh', '.java', '.cpp', '.c', '.ts'],
    'Apps': ['.app', '.exe', '.pkg', '.deb', '.rpm']
}


def clean_downloads():
    """
    Organizes files in the Downloads folder by moving them into categorized subfolders.
    
    Returns:
        str: A message indicating how many files were moved.
    """
    if not os.path.exists(DOWNLOADS_PATH):
        return "Downloads folder not found."
    
    moved_count = 0
    errors = []
    
    # Get all files in Downloads (not directories)
    try:
        items = os.listdir(DOWNLOADS_PATH)
    except PermissionError:
        return "I don't have permission to access the Downloads folder."
    
    for item in items:
        item_path = os.path.join(DOWNLOADS_PATH, item)
        
        # Skip directories and hidden files
        if os.path.isdir(item_path) or item.startswith('.'):
            continue
        
        # Get file extension
        _, ext = os.path.splitext(item)
        ext = ext.lower()
        
        # Find matching category
        target_category = None
        for category, extensions in FILE_CATEGORIES.items():
            if ext in extensions:
                target_category = category
                break
        
        if target_category:
            # Create category folder if it doesn't exist
            category_path = os.path.join(DOWNLOADS_PATH, target_category)
            if not os.path.exists(category_path):
                os.makedirs(category_path)
            
            # Move file
            dest_path = os.path.join(category_path, item)
            
            # Handle duplicate filenames
            if os.path.exists(dest_path):
                base, extension = os.path.splitext(item)
                counter = 1
                while os.path.exists(dest_path):
                    new_name = f"{base}_{counter}{extension}"
                    dest_path = os.path.join(category_path, new_name)
                    counter += 1
            
            try:
                shutil.move(item_path, dest_path)
                moved_count += 1
            except Exception as e:
                errors.append(f"{item}: {str(e)}")
    
    # Build response message
    if moved_count == 0:
        return "Your Downloads folder is already organized. No files needed to be moved."
    elif errors:
        return f"Cleanup complete. Moved {moved_count} files. {len(errors)} files couldn't be moved."
    else:
        return f"Cleanup complete. Moved {moved_count} files into organized folders."


def get_downloads_summary():
    """
    Returns a summary of what's in the Downloads folder.
    
    Returns:
        str: A summary of files by category.
    """
    if not os.path.exists(DOWNLOADS_PATH):
        return "Downloads folder not found."
    
    summary = {}
    uncategorized = 0
    
    try:
        items = os.listdir(DOWNLOADS_PATH)
    except PermissionError:
        return "I don't have permission to access the Downloads folder."
    
    for item in items:
        item_path = os.path.join(DOWNLOADS_PATH, item)
        
        # Skip directories and hidden files
        if os.path.isdir(item_path) or item.startswith('.'):
            continue
        
        # Get file extension
        _, ext = os.path.splitext(item)
        ext = ext.lower()
        
        # Find matching category
        found = False
        for category, extensions in FILE_CATEGORIES.items():
            if ext in extensions:
                summary[category] = summary.get(category, 0) + 1
                found = True
                break
        
        if not found:
            uncategorized += 1
    
    if not summary and uncategorized == 0:
        return "Your Downloads folder is empty."
    
    parts = [f"{count} {cat}" for cat, count in summary.items()]
    if uncategorized:
        parts.append(f"{uncategorized} other files")
    
    return f"Downloads contains: {', '.join(parts)}."


def delete_file(filename):
    """
    Deletes a file from the Downloads folder or Desktop (for screenshots).
    
    Args:
        filename: Name or partial name of the file to delete (e.g., "Screenshot", "invoice.pdf")
    
    Returns:
        str: A message indicating success or failure.
    """
    import glob
    from send2trash import send2trash  # Safe delete to Trash
    
    # Search locations
    search_paths = [
        os.path.expanduser("~/Downloads"),
        os.path.expanduser("~/Desktop"),
    ]
    
    deleted_count = 0
    found_files = []
    
    for search_path in search_paths:
        if not os.path.exists(search_path):
            continue
            
        try:
            items = os.listdir(search_path)
        except PermissionError:
            continue
        
        for item in items:
            item_path = os.path.join(search_path, item)
            
            # Skip directories
            if os.path.isdir(item_path):
                continue
            
            # Check if filename matches (case-insensitive partial match)
            if filename.lower() in item.lower():
                found_files.append(item_path)
    
    if not found_files:
        return f"I couldn't find any file matching '{filename}'."
    
    # If multiple files found, only delete the most recent one
    if len(found_files) > 1:
        # Sort by modification time, newest first
        found_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        file_to_delete = found_files[0]
        
        try:
            send2trash(file_to_delete)
            basename = os.path.basename(file_to_delete)
            return f"Deleted the most recent file: {basename}. (Found {len(found_files)} matching files total.)"
        except Exception as e:
            return f"Couldn't delete the file: {str(e)}"
    else:
        file_to_delete = found_files[0]
        try:
            send2trash(file_to_delete)
            basename = os.path.basename(file_to_delete)
            return f"Deleted {basename}."
        except Exception as e:
            return f"Couldn't delete the file: {str(e)}"


def delete_screenshots(count=1):
    """
    Deletes the most recent screenshot(s).
    
    Args:
        count: Number of screenshots to delete (default 1)
    
    Returns:
        str: A message indicating success or failure.
    """
    from send2trash import send2trash
    
    desktop_path = os.path.expanduser("~/Desktop")
    
    # Find all screenshots (macOS names them "Screenshot...")
    screenshots = []
    
    try:
        items = os.listdir(desktop_path)
    except PermissionError:
        return "I don't have permission to access the Desktop."
    
    for item in items:
        if item.lower().startswith("screenshot") or "screen shot" in item.lower():
            item_path = os.path.join(desktop_path, item)
            if os.path.isfile(item_path):
                screenshots.append(item_path)
    
    if not screenshots:
        return "I couldn't find any screenshots on your Desktop."
    
    # Sort by modification time, newest first
    screenshots.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    
    # Delete the requested number
    to_delete = screenshots[:count]
    deleted = 0
    
    for screenshot in to_delete:
        try:
            send2trash(screenshot)
            deleted += 1
        except Exception as e:
            pass
    
    if deleted == 1:
        return "Deleted the most recent screenshot."
    elif deleted > 1:
        return f"Deleted {deleted} screenshots."
    else:
        return "Couldn't delete the screenshots."
