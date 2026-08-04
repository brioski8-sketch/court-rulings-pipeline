#!/bin/bash
cd /home/thebevans/.hermes/court-rulings
python3 -c "
import urllib.request, xml.etree.ElementTree as ET

# Fetch ONCJ RSS feed
req = urllib.request.Request('https://www.canlii.org/en/on/oncj/rss_new.xml', 
    headers={'User-Agent': 'Mozilla/5.0'})
resp = urllib.request.urlopen(req, timeout=30)
raw = resp.read().decode('utf-8', errors='replace')
root = ET.fromstring(raw)

items = []
for item in root.findall('.//item'):
    title = item.find('title')
    link = item.find('link')
    desc = item.find('description')
    pubDate = item.find('pubDate')
    items.append({
        'title': title.text if title is not None else '',
        'link': link.text if link is not None else '',
        'desc': desc.text if desc is not None else '',
        'date': pubDate.text if pubDate is not None else ''
    })

# Find our four cases
targets = ['2026oncj294', '2026oncj265', '2026oncj300', '2026oncj272']
for item in items:
    for t in targets:
        if t in item['link']:
            print(f\"=== {item['title']} ===\")
            print(f\"Link: {item['link']}\")
            print(f\"Date: {item['date']}\")
            print(f\"Description: {item['desc'][:2000]}\")
            print()
            break
" 2>&1
