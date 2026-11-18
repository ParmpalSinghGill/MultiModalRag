"""
URL and Target Parser for HuggingFace Dataset Viewer URLs.
"""

import re
import urllib.parse
from typing import Dict, Any, Optional


def parse_hf_viewer_url(url_or_path: str) -> Dict[str, Any]:
    """
    Parses a Hugging Face dataset viewer URL or dataset identifier.
    
    Examples:
        https://huggingface.co/datasets/Salesforce/UniDoc-Bench/viewer/default/commerce_manufacturing?row=0
        https://huggingface.co/datasets/Salesforce/UniDoc-Bench/viewer/commerce_manufacturing?row=0
        Salesforce/UniDoc-Bench
        
    Returns:
        dict with keys:
            - repo_id: e.g. 'Salesforce/UniDoc-Bench'
            - config: e.g. 'default'
            - split: e.g. 'commerce_manufacturing'
            - row: int or None (e.g. 0)
    """
    parsed = urllib.parse.urlparse(url_or_path.strip())
    path = parsed.path.strip('/')
    query = urllib.parse.parse_qs(parsed.query)
    
    row_idx = None
    if 'row' in query:
        try:
            row_idx = int(query['row'][0])
        except (ValueError, IndexError):
            row_idx = None

    # Handle dataset viewer URLs
    # Pattern: datasets/{owner}/{dataset_name}/viewer/{config}/{split}
    # Or: datasets/{owner}/{dataset_name}/viewer/{split}
    viewer_match = re.match(r'(?:datasets/)?([^/]+/[^/]+)/viewer(?:/([^/?]+))?(?:/([^/?]+))?', path)
    
    if viewer_match:
        repo_id = viewer_match.group(1)
        part1 = viewer_match.group(2)
        part2 = viewer_match.group(3)
        
        if part2:
            config = part1
            split = part2
        elif part1:
            # Could be /viewer/{split} or /viewer/{config}
            # For UniDoc-Bench, 'default' is the config and domain names are splits
            domains = {
                'commerce_manufacturing', 'construction', 'crm', 'education',
                'energy', 'finance', 'healthcare', 'legal'
            }
            if part1 in domains:
                config = 'default'
                split = part1
            else:
                config = part1
                split = None
        else:
            config = 'default'
            split = None
            
        return {
            'repo_id': repo_id,
            'config': config or 'default',
            'split': split,
            'row': row_idx
        }
        
    # Pattern: datasets/{owner}/{dataset_name}
    dataset_match = re.match(r'(?:datasets/)?([^/]+/[^/]+)', path)
    if dataset_match:
        return {
            'repo_id': dataset_match.group(1),
            'config': 'default',
            'split': None,
            'row': row_idx
        }
        
    # Fallback: treat raw string as repo_id if it matches owner/repo
    if '/' in url_or_path and not url_or_path.startswith(('http://', 'https://')):
        parts = url_or_path.split('/', 1)
        return {
            'repo_id': f"{parts[0]}/{parts[1]}",
            'config': 'default',
            'split': None,
            'row': row_idx
        }
        
    return {
        'repo_id': url_or_path,
        'config': 'default',
        'split': None,
        'row': row_idx
    }
