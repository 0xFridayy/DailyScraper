"""Ownership login uses only existing NeoBDM credentials; no messaging import."""
import ast
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

class SourcePaused(RuntimeError):
    pass

def legacy_tickers():
    tree = ast.parse(Path(__file__).with_name('neobdm_scraper.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'STOCK_OWNER' for t in node.targets):
            return sorted(ast.literal_eval(node.value))
    raise ValueError('Legacy ticker configuration missing')

def login(page):
    username, password = os.environ.get('NEOBDM_USERNAME'), os.environ.get('NEOBDM_PASSWORD')
    if not username or not password:
        raise RuntimeError('Existing NeoBDM credentials required')
    page.goto('https://neobdm.tech/accounts/login/?next=/home/', wait_until='domcontentloaded', timeout=60000)
    page.locator('input[name="username"],#id_username,input[type="text"]').first.fill(username)
    page.locator('input[name="password"],#id_password,input[type="password"]').first.fill(password)
    submit = page.locator('button[type="submit"],input[type="submit"]')
    if submit.count():
        submit.first.click()
    else:
        page.keyboard.press('Enter')
    page.wait_for_url(lambda url: 'login' not in url.lower(), timeout=30000)
