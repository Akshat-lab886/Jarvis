"""
Jarvis Web Downloader Module
Enables Jarvis to search the web and download files (PDFs, images, etc.)
"""

import os
import re
import requests
from urllib.parse import quote_plus, urlparse, unquote
from bs4 import BeautifulSoup

# Download destination
DOWNLOADS_PATH = os.path.expanduser("~/Downloads")

# User agent to avoid being blocked
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


def search_and_download(query, file_type="pdf"):
    """
    Searches the web for a file and downloads it.
    
    Args:
        query: What to search for (e.g., "python programming book")
        file_type: Type of file to download (pdf, doc, etc.)
    
    Returns:
        str: Message indicating success or failure.
    """
    try:
        # Build search URL using DuckDuckGo HTML (doesn't require API key)
        search_query = f"{query} filetype:{file_type}"
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}"
        
        print(f"Searching: {search_url}")
        
        response = requests.get(search_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find result links
        results = soup.find_all('a', class_='result__a')
        
        if not results:
            # Try alternative parsing
            results = soup.find_all('a', href=True)
            results = [r for r in results if r.get('href', '').startswith('http') and f'.{file_type}' in r.get('href', '').lower()]
        
        # Look for direct file links
        file_url = None
        for result in results:
            href = result.get('href', '')
            
            # DuckDuckGo wraps URLs, extract the actual URL
            if 'uddg=' in href:
                import urllib.parse
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                if 'uddg' in parsed:
                    href = parsed['uddg'][0]
            
            # Check if it's a direct file link
            if f'.{file_type}' in href.lower():
                file_url = href
                break
        
        if not file_url:
            # If no direct link found, try to find any relevant link
            for result in results[:5]:  # Check first 5 results
                href = result.get('href', '')
                if 'uddg=' in href:
                    import urllib.parse
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                    if 'uddg' in parsed:
                        href = parsed['uddg'][0]
                
                # Try to download from the page
                if href.startswith('http'):
                    try:
                        page_file = find_file_on_page(href, file_type)
                        if page_file:
                            file_url = page_file
                            break
                    except:
                        continue
        
        if not file_url:
            return f"I couldn't find a {file_type.upper()} file for '{query}'. Try being more specific."
        
        # Download the file
        return download_file(file_url, query)
        
    except requests.RequestException as e:
        return f"Network error while searching: {str(e)}"
    except Exception as e:
        return f"Error searching for file: {str(e)}"


def find_file_on_page(url, file_type):
    """
    Looks for a downloadable file on a webpage.
    
    Args:
        url: The webpage URL to scan
        file_type: Type of file to look for
    
    Returns:
        str or None: Direct file URL if found
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Look for direct file links
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            if f'.{file_type}' in href.lower():
                # Make absolute URL
                if href.startswith('/'):
                    parsed = urlparse(url)
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                elif not href.startswith('http'):
                    href = url.rsplit('/', 1)[0] + '/' + href
                return href
        
        return None
    except:
        return None


def download_file(url, filename_hint=None):
    """
    Downloads a file from a URL to the Downloads folder.
    
    Args:
        url: Direct URL to the file
        filename_hint: Optional hint for the filename
    
    Returns:
        str: Message indicating success or failure.
    """
    try:
        print(f"Downloading: {url}")
        
        response = requests.get(url, headers=HEADERS, timeout=60, stream=True)
        response.raise_for_status()
        
        # Determine filename
        filename = None
        
        # Try to get from Content-Disposition header
        content_disp = response.headers.get('Content-Disposition', '')
        if 'filename=' in content_disp:
            filename = re.findall('filename="?([^";\n]+)"?', content_disp)
            if filename:
                filename = filename[0]
        
        # Fall back to URL path
        if not filename:
            parsed = urlparse(url)
            filename = os.path.basename(unquote(parsed.path))
        
        # If still no filename, generate one
        if not filename or filename == '/':
            ext = '.pdf'  # Default extension
            if 'content-type' in response.headers:
                content_type = response.headers['content-type']
                if 'pdf' in content_type:
                    ext = '.pdf'
                elif 'image' in content_type:
                    ext = '.jpg'
            
            safe_hint = re.sub(r'[^\w\s-]', '', filename_hint or 'download')[:50]
            filename = f"{safe_hint}{ext}"
        
        # Save to Downloads
        filepath = os.path.join(DOWNLOADS_PATH, filename)
        
        # Handle duplicates
        if os.path.exists(filepath):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(filepath):
                filepath = os.path.join(DOWNLOADS_PATH, f"{base}_{counter}{ext}")
                counter += 1
        
        # Write file
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        final_filename = os.path.basename(filepath)
        return f"Downloaded '{final_filename}' to your Downloads folder."
        
    except requests.RequestException as e:
        return f"Download failed: {str(e)}"
    except Exception as e:
        return f"Error saving file: {str(e)}"


def download_direct(url):
    """
    Downloads a file directly from a provided URL.
    
    Args:
        url: Direct URL to download
    
    Returns:
        str: Message indicating success or failure.
    """
    if not url.startswith('http'):
        return "Please provide a valid URL starting with http:// or https://"
    
    return download_file(url)
