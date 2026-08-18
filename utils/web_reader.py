"""
Jarvis Web Reader Module
Provides enhanced internet access by fetching and reading actual web page content.
Combines search results with page content extraction for comprehensive answers.
"""

import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlparse
import re

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


def search_and_read(query, max_results=3):
    """
    Searches the web and reads actual page content for comprehensive answers.
    
    Args:
        query: The search query
        max_results: Number of pages to read
        
    Returns:
        str: Combined content from web pages
    """
    try:
        # Step 1: Get search results from DuckDuckGo
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        
        response = requests.get(search_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract result URLs
        urls = []
        for link in soup.find_all('a', class_='result__a'):
            href = link.get('href', '')
            if href.startswith('http'):
                urls.append(href)
            elif 'uddg=' in href:
                # DuckDuckGo redirect URL
                import urllib.parse
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                if 'uddg' in parsed:
                    urls.append(parsed['uddg'][0])
        
        if not urls:
            return "I couldn't find any relevant web pages for that query."
        
        # Step 2: Fetch and extract content from top pages
        all_content = []
        for url in urls[:max_results]:
            try:
                content = extract_page_content(url)
                if content and len(content) > 100:
                    all_content.append(f"From {urlparse(url).netloc}:\n{content[:2000]}")
            except:
                continue
        
        if not all_content:
            return "I found some pages but couldn't extract their content."
        
        return "\n\n---\n\n".join(all_content)
        
    except Exception as e:
        print(f"Search and read error: {e}")
        return f"I couldn't access the web: {str(e)}"


def extract_page_content(url):
    """
    Extracts readable text content from a web page.
    
    Args:
        url: The page URL
        
    Returns:
        str: Cleaned text content
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove script, style, nav, footer, ads
        for element in soup(['script', 'style', 'nav', 'footer', 'header', 
                             'aside', 'iframe', 'noscript', 'form']):
            element.decompose()
        
        # Try to find main content
        main_content = None
        
        # Look for article or main content areas
        for selector in ['article', 'main', '[role="main"]', '.content', 
                         '.post-content', '.article-body', '.entry-content']:
            main_content = soup.select_one(selector)
            if main_content:
                break
        
        # Fall back to body
        if not main_content:
            main_content = soup.body
        
        if not main_content:
            return ""
        
        # Get text
        text = main_content.get_text(separator='\n', strip=True)
        
        # Clean up
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        # Remove very short lines (likely menu items)
        lines = [line for line in lines if len(line) > 30 or line.endswith('.')]
        
        return '\n'.join(lines[:50])  # Limit to first 50 meaningful lines
        
    except Exception as e:
        print(f"Page extraction error for {url}: {e}")
        return ""


def read_url(url):
    """
    Reads content from a specific URL.
    
    Args:
        url: The URL to read
        
    Returns:
        str: Page content
    """
    if not url.startswith('http'):
        url = 'https://' + url
    
    content = extract_page_content(url)
    if content:
        return content
    return "I couldn't read the content from that page."


def get_answer_from_web(query):
    """
    Gets a comprehensive answer by searching and reading web pages.
    This is the main function to use for general knowledge questions.
    
    Args:
        query: The user's question
        
    Returns:
        str: Detailed information from the web
    """
    print(f"Getting web answer for: {query}")
    
    # Special handling for common query types
    query_lower = query.lower()
    
    # For weather, use wttr.in
    if 'weather' in query_lower:
        try:
            location = ""
            for word in ['in', 'at', 'for']:
                if word in query_lower:
                    parts = query_lower.split(word)
                    if len(parts) > 1:
                        location = parts[1].strip().split()[0] if parts[1].strip() else ""
                        break
            
            url = f"https://wttr.in/{location}?format=3" if location else "https://wttr.in/?format=3"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                return f"Current weather: {response.text.strip()}"
        except:
            pass
    
    # For definitions, try Wikipedia first
    if any(x in query_lower for x in ['what is', 'who is', 'define', 'meaning of']):
        wiki_content = get_wikipedia_summary(query)
        if wiki_content:
            return wiki_content
    
    # General search and read
    return search_and_read(query)


def get_top_headlines(limit=8, region=None):
    """
    Fetches today's top headlines from Google News RSS.

    Args:
        limit: Number of headlines to return
        region: Optional country code (e.g. 'IN' for India, 'US' for USA)

    Returns:
        str: Bullet list of headlines, or a friendly error message
    """
    try:
        feed_url = "https://news.google.com/rss"
        if region:
            feed_url = f"https://news.google.com/rss/headlines/section/geo/{region}"
        response = requests.get(feed_url, headers=HEADERS, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'xml')
        items = soup.find_all('item')
        if not items:
            return "I couldn't find any headlines right now."

        headlines = []
        for item in items[:limit]:
            title = item.find('title')
            if title and title.get_text(strip=True):
                # Google News prefixes source names like "Title - Source"
                headlines.append(title.get_text(strip=True))
        if not headlines:
            return "I couldn't parse the headlines right now."
        return "Top headlines:\n" + "\n".join(f"• {h}" for h in headlines)
    except Exception as e:
        print(f"Headlines error: {e}")
        return f"I couldn't fetch the news: {e}"


def get_wikipedia_summary(query):
    """
    Gets a summary from Wikipedia for definition-type queries.
    
    Args:
        query: The search query
        
    Returns:
        str or None: Wikipedia summary if found
    """
    try:
        # Extract the subject from queries like "what is X" or "who is X"
        subject = query.lower()
        for prefix in ['what is', 'who is', 'define', 'meaning of', 'what are', 'who are']:
            if prefix in subject:
                subject = subject.split(prefix)[-1].strip()
                break
        
        # Remove trailing punctuation
        subject = subject.rstrip('?.,!')
        
        # Search Wikipedia
        search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={quote_plus(subject)}&format=json"
        response = requests.get(search_url, timeout=5)
        data = response.json()
        
        if data.get('query', {}).get('search'):
            title = data['query']['search'][0]['title']
            
            # Get summary
            summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote_plus(title)}"
            response = requests.get(summary_url, timeout=5)
            summary_data = response.json()
            
            if summary_data.get('extract'):
                return f"{title}: {summary_data['extract']}"
        
        return None
        
    except Exception as e:
        print(f"Wikipedia error: {e}")
        return None
