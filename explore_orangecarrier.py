#!/usr/bin/env python3
import os
import asyncio
import httpx
from bs4 import BeautifulSoup

ORANGE_EMAIL = os.getenv("ORANGE_EMAIL", "")
ORANGE_PASSWORD = os.getenv("ORANGE_PASSWORD", "")

async def explore():
    async with httpx.AsyncClient(timeout=30.0) as client:
        client.headers.update({"User-Agent": "Mozilla/5.0 (compatible; OrangeBot/1.0)"})
        
        # Login
        login_url = "https://www.orangecarrier.com/login"
        r = await client.get(login_url)
        soup = BeautifulSoup(r.text, "html.parser")
        inp = soup.find("input", {"name": "_token"})
        token = inp.get("value") if inp else None
        
        payload = {"email": ORANGE_EMAIL, "password": ORANGE_PASSWORD}
        if token:
            payload["_token"] = token
        
        await client.post(login_url, data=payload, follow_redirects=True)
        
        # Explore dashboard and main pages
        urls = [
            "https://www.orangecarrier.com/dashboard",
            "https://www.orangecarrier.com/",
            "https://www.orangecarrier.com/account",
            "https://www.orangecarrier.com/billing",
            "https://www.orangecarrier.com/settings",
            "https://www.orangecarrier.com/numbers",
            "https://www.orangecarrier.com/calls",
            "https://www.orangecarrier.com/messages",
            "https://www.orangecarrier.com/reports",
        ]
        
        print("=== ORANGECARRIER.COM EXPLORATION ===\n")
        
        for url in urls:
            try:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    
                    # Extract title
                    title = soup.find("title")
                    title_text = title.text if title else "N/A"
                    
                    # Extract links
                    links = []
                    for a in soup.find_all("a", href=True):
                        href = a.get("href", "")
                        text = a.get_text(strip=True)
                        if href and text and not href.startswith("#"):
                            links.append((text, href))
                    
                    # Extract navigation menu items
                    nav_items = []
                    for nav in soup.find_all(["nav", "aside", "div"], class_=lambda x: x and "nav" in x.lower()):
                        for li in nav.find_all("li"):
                            li_text = li.get_text(strip=True)
                            if li_text:
                                nav_items.append(li_text)
                    
                    # Extract buttons
                    buttons = []
                    for btn in soup.find_all(["button", "a"], class_=lambda x: x and "btn" in x.lower()):
                        btn_text = btn.get_text(strip=True)
                        if btn_text:
                            buttons.append(btn_text)
                    
                    print(f"📄 URL: {url}")
                    print(f"   Title: {title_text}")
                    if nav_items:
                        print(f"   Menu Items: {', '.join(set(nav_items))}")
                    if buttons:
                        print(f"   Buttons: {', '.join(set(buttons[:5]))}")
                    print()
                    
            except Exception as e:
                print(f"❌ {url} - Error: {str(e)}\n")

if __name__ == "__main__":
    asyncio.run(explore())
