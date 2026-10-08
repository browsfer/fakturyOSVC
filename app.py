from __future__ import annotations

# Jednoplikowa aplikacja V8: faktury, podatki/składki (w tym zmiana vedlejší → hlavní) i DPH/VIES. Przy pierwszym uruchomieniu automatycznie doinstaluje
# Flask i ReportLab, jeżeli nie są jeszcze dostępne w tym Pythonie.
import importlib.util
import subprocess
import sys
import calendar
import csv
import xml.etree.ElementTree as ET


def _ensure_dependencies() -> None:
    required = []
    if importlib.util.find_spec("flask") is None:
        required.append("Flask>=3.1.3,<4")
    if importlib.util.find_spec("reportlab") is None:
        required.append("reportlab>=4.0,<5")
    if importlib.util.find_spec("tzdata") is None:
        required.append("tzdata>=2025.2")
    if not required:
        return
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *required])
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
            subprocess.check_call([sys.executable, "-m", "pip", "install", *required])
        except Exception as exc:
            print("Nie udało się zainstalować wymaganych bibliotek:", ", ".join(required))
            print("Uruchom ręcznie: python -m pip install flask reportlab")
            input("Naciśnij Enter, aby zakończyć...")
            raise SystemExit(1) from exc


_ensure_dependencies()

import os
import tempfile as _test_tempfile
import atexit as _test_atexit
if '--self-test' in sys.argv:
    _v82_test_dir = _test_tempfile.TemporaryDirectory(prefix='faktury_v82_test_')
    _test_atexit.register(_v82_test_dir.cleanup)
    for _key in ('RENDER', 'APP_CLOUD_MODE', 'SUPABASE_URL', 'SUPABASE_SERVICE_KEY', 'APP_PASSWORD'):
        os.environ.pop(_key, None)
    os.environ['INVOICE_APP_DATA'] = _v82_test_dir.name
    os.environ['INVOICE_APP_DB'] = os.path.join(_v82_test_dir.name, 'self-test.sqlite3')
    os.environ['INVOICE_PDF_DIR'] = os.path.join(_v82_test_dir.name, 'pdf')

import secrets

# V8: web/PWA z modułem Prop firmy i darmową synchronizacją Supabase.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or secrets.token_hex(32)
CLOUD_MODE = bool(os.environ.get("RENDER") or os.environ.get("APP_CLOUD_MODE"))

# Trwałość danych w darmowym wariancie:
# Render Free = aplikacja, Supabase Storage Free = prywatna kopia pliku SQLite.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "faktury-osvc").strip() or "faktury-osvc"
SUPABASE_DB_OBJECT = os.environ.get("SUPABASE_DB_OBJECT", "data/invoice_app.db").strip() or "data/invoice_app.db"
REMOTE_DB_ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
SUPABASE_KEY_KIND = (
    "new_secret" if SUPABASE_SERVICE_KEY.startswith("sb_secret_")
    else "legacy_service_role" if SUPABASE_SERVICE_KEY.startswith("eyJ")
    else "unknown" if SUPABASE_SERVICE_KEY
    else "missing"
)

import shutil
import tempfile
import re
import sqlite3
import threading
import unicodedata
import webbrowser
import hmac
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
    session,
)
from jinja2 import DictLoader

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# Szablony, CSS i JavaScript są osadzone w tym jednym pliku.
EMBEDDED_TEMPLATES = {'404.html': '{% extends "base.html" %}\n'
             '{% block title %}Nie znaleziono - Faktury OSVČ{% endblock %}\n'
             '{% block content %}<div class="empty-state page-empty"><h1>404</h1><h2>Nie znaleziono strony</h2><a '
             'class="btn btn-primary" href="{{ url_for(\'dashboard\') }}">Wróć na start</a></div>{% endblock %}\n',
 'base.html': '<!doctype html>\n'
              '<html lang="pl">\n'
              '<head>\n'
              '    <meta charset="utf-8">\n'
              '    <meta name="viewport" content="width=device-width, initial-scale=1">\n'
              '    <title>{% block title %}Faktury OSVČ{% endblock %}</title>\n'
              '    <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">\n'
              '</head>\n'
              '<body>\n'
              '<header class="topbar">\n'
              '    <div class="topbar-inner">\n'
              '        <a class="brand" href="{{ url_for(\'dashboard\') }}">Faktury OSVČ</a>\n'
              '        <nav class="nav">\n'
              '            <a href="{{ url_for(\'dashboard\') }}">Start</a>\n'
              '            <a href="{{ url_for(\'invoices_list\') }}">Faktury</a>\n'
              '            <a href="{{ url_for(\'contractors_list\') }}">Kontrahenci</a>\n'
              '            <a href="{{ url_for(\'expenses_page\') }}">Koszty</a>\n'
              '            <a href="{{ url_for(\'dph_dashboard\') }}">DPH / UE</a>\n'
              '            <a href="{{ url_for(\'company_edit\') }}">Moja firma</a>\n'
              '            <a href="{{ url_for(\'tools_page\') }}">Narzędzia</a>\n'
              '        </nav>\n'
              '        <a class="btn btn-primary btn-small" href="{{ url_for(\'invoice_new\') }}">+ Nowa faktura</a>\n'
              '    </div>\n'
              '</header>\n'
              '\n'
              '<main class="container">\n'
              '    {% with messages = get_flashed_messages(with_categories=true) %}\n'
              '        {% if messages %}\n'
              '            <div class="flash-stack">\n'
              '                {% for category, message in messages %}\n'
              '                    <div class="flash flash-{{ category }}">{{ message }}</div>\n'
              '                {% endfor %}\n'
              '            </div>\n'
              '        {% endif %}\n'
              '    {% endwith %}\n'
              '    {% block content %}{% endblock %}\n'
              '</main>\n'
              '\n'
              '<footer class="footer">\n'
              '    <div class="container footer-inner">\n'
              '        <span>Dane są przechowywane lokalnie. Aktualizacja programu nie usuwa kontrahentów, faktur ani '
              'kosztów.</span>\n'
              '        <a href="{{ url_for(\'backup_database\') }}">Pobierz kopię bazy</a>\n'
              '    </div>\n'
              '</footer>\n'
              '<script src="{{ url_for(\'static\', filename=\'app.js\') }}"></script>\n'
              '{% block scripts %}{% endblock %}\n'
              '</body>\n'
              '</html>\n',
 'company_form.html': '{% extends "base.html" %}\n'
                      '{% block title %}Moja firma - Faktury OSVČ{% endblock %}\n'
                      '{% block content %}\n'
                      '<div class="page-header">\n'
                      '    <div><h1>Dane własnej firmy</h1><p class="muted">Te informacje będą drukowane na fakturach '
                      'PDF.</p></div>\n'
                      '</div>\n'
                      '<form method="post" class="panel form-panel">\n'
                      '    <h2>Dane identyfikacyjne</h2>\n'
                      '    <div class="form-grid two">\n'
                      '        <label class="field span-2"><span>Nazwa / imię i nazwisko *</span><input name="name" '
                      'value="{{ company.name }}" required></label>\n'
                      '        <label class="field"><span>IČO / numer firmy</span><input name="company_id" value="{{ '
                      'company.company_id }}"></label>\n'
                      '        <label class="field"><span>DIČ / VAT ID</span><input name="vat_id" value="{{ '
                      'company.vat_id }}"></label>\n'
                      '        <label class="field span-2"><span>Ulica i numer</span><input name="address" value="{{ '
                      'company.address }}"></label>\n'
                      '        <label class="field"><span>Kod pocztowy</span><input name="postal_code" value="{{ '
                      'company.postal_code }}"></label>\n'
                      '        <label class="field"><span>Miasto</span><input name="city" value="{{ company.city '
                      '}}"></label>\n'
                      '        <label class="field span-2"><span>Kraj</span><input name="country" value="{{ '
                      'company.country }}"></label>\n'
                      '        <label class="field"><span>E-mail</span><input type="email" name="email" value="{{ '
                      'company.email }}"></label>\n'
                      '        <label class="field"><span>Telefon</span><input name="phone" value="{{ company.phone '
                      '}}"></label>\n'
                      '    </div>\n'
                      '\n'
                      '    <hr>\n'
                      '    <h2>Dane bankowe</h2>\n'
                      '    <div class="form-grid two">\n'
                      '        <label class="field"><span>Nazwa banku</span><input name="bank_name" value="{{ '
                      'company.bank_name }}"></label>\n'
                      '        <label class="field"><span>Numer rachunku lokalnego</span><input name="bank_account" '
                      'value="{{ company.bank_account }}"></label>\n'
                      '        <label class="field"><span>IBAN</span><input name="iban" value="{{ company.iban '
                      '}}"></label>\n'
                      '        <label class="field"><span>BIC / SWIFT</span><input name="bic" value="{{ company.bic '
                      '}}"></label>\n'
                      '    </div>\n'
                      '\n'
                      '    <hr>\n'
                      '    <h2>Ustawienia faktur</h2>\n'
                      '    <div class="form-grid three">\n'
                      '        <label class="field"><span>Domyślna waluta</span><input name="default_currency" '
                      'value="{{ company.default_currency }}" maxlength="6"></label>\n'
                      '        <label class="field"><span>Termin płatności (dni)</span><input type="number" min="0" '
                      'max="365" name="default_due_days" value="{{ company.default_due_days }}"></label>\n'
                      '        <label class="field"><span>Prefiks numeru</span><input name="invoice_prefix" value="{{ '
                      'company.invoice_prefix }}" maxlength="12"></label>\n'
                      '        <label class="field"><span>Domyślny język PDF</span><select name="default_language">{% '
                      'for code, label in LANGUAGES.items() %}<option value="{{ code }}" {% if '
                      'company.default_language == code %}selected{% endif %}>{{ label }}</option>{% endfor '
                      '%}</select></label>\n'
                      '        <label class="field"><span>Domyślny tryb VAT</span><select name="default_tax_mode">{% '
                      'for code, label in TAX_MODES.items() %}<option value="{{ code }}" {% if '
                      'company.default_tax_mode == code %}selected{% endif %}>{{ label }}</option>{% endfor '
                      '%}</select></label>\n'
                      '        <div></div>\n'
                      '        <label class="field span-3"><span>Domyślna adnotacja reverse charge</span><textarea '
                      'name="default_reverse_charge_note" rows="3">{{ company.default_reverse_charge_note '
                      '}}</textarea></label>\n'
                      '    </div>\n'
                      '    <hr>\n'
                      '    <h2>Ustawienia Souhrnné hlášení VIES</h2>\n'
                      '    <div class="callout info">Te dane służą do przygotowania pliku XML do MOJE daně. Kod 451 oznacza '
                      'Finanční úřad pro hlavní město Prahu, a kod 2002 jego Územní pracoviště pro Prahu 2. Dla osoby '
                      'fizycznej właściwość wynika z miejsca zamieszkania, a nie z adresu siedziby OSVČ; dlatego Praha 2 '
                      'pozostaje prawidłowa przy Twoim mieszkaniu na Mánesovej.</div>\n'
                      '    <div class="form-grid two">\n'
                      '        <label class="field"><span>Imię / imiona podatnika</span><input name="tax_first_name" '
                      'value="{{ company.tax_first_name }}"></label>\n'
                      '        <label class="field"><span>Nazwisko podatnika</span><input name="tax_last_name" '
                      'value="{{ company.tax_last_name }}"></label>\n'
                      '        <label class="field"><span>Kod finančního úřadu (c_ufo)</span><input '
                      'name="tax_office_code" value="{{ company.tax_office_code }}" maxlength="3"></label>\n'
                      '        <label class="field"><span>Kod územního pracoviště (c_pracufo)</span><input '
                      'name="tax_branch_code" value="{{ company.tax_branch_code }}" maxlength="4"></label>\n'
                      '    </div>\n'
                      '    <div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz dane '
                      'firmy</button></div>\n'
                      '</form>\n'
                      '{% endblock %}\n',
 'contractor_form.html': '{% extends "base.html" %}\n'
                         "{% block title %}{{ 'Edytuj kontrahenta' if is_edit else 'Nowy kontrahent' }} - Faktury "
                         'OSVČ{% endblock %}\n'
                         '{% block content %}\n'
                         '<div class="page-header"><div><h1>{{ \'Edytuj kontrahenta\' if is_edit else \'Nowy '
                         'kontrahent\' }}</h1><p class="muted">Dane zostaną użyte w sekcji nabywcy na '
                         'fakturze.</p></div></div>\n'
                         '<form method="post" class="panel form-panel">\n'
                         '    <div class="form-grid two">\n'
                         '        <label class="field span-2"><span>Nazwa firmy *</span><input name="name" value="{{ '
                         'contractor.name }}" required autofocus></label>\n'
                         '        <label class="field"><span>VAT ID / UID</span><input name="vat_id" value="{{ '
                         'contractor.vat_id }}" placeholder="ATU12345678"></label>\n'
                         '        <label class="field"><span>Numer firmy</span><input name="company_id" value="{{ '
                         'contractor.company_id }}"></label>\n'
                         '        <label class="field span-2"><span>Ulica i numer</span><input name="address" '
                         'value="{{ contractor.address }}"></label>\n'
                         '        <label class="field"><span>Kod pocztowy</span><input name="postal_code" value="{{ '
                         'contractor.postal_code }}"></label>\n'
                         '        <label class="field"><span>Miasto</span><input name="city" value="{{ contractor.city '
                         '}}"></label>\n'
                         '        <label class="field span-2"><span>Kraj</span><input name="country" value="{{ '
                         'contractor.country }}"></label>\n'
                         '        <label class="field"><span>E-mail</span><input type="email" name="email" value="{{ '
                         'contractor.email }}"></label>\n'
                         '        <label class="field"><span>Telefon</span><input name="phone" value="{{ '
                         'contractor.phone }}"></label>\n'
                         '        <label class="field span-2"><span>Notatki wewnętrzne</span><textarea name="notes" '
                         'rows="3">{{ contractor.notes }}</textarea></label>\n'
                         '    </div>\n'
                         '    <div class="form-actions">\n'
                         '        <a class="btn btn-ghost" href="{{ url_for(\'contractors_list\') }}">Anuluj</a>\n'
                         '        <button class="btn btn-primary" type="submit">{{ \'Zapisz zmiany\' if is_edit else '
                         "'Dodaj kontrahenta' }}</button>\n"
                         '    </div>\n'
                         '</form>\n'
                         '{% endblock %}\n',
 'contractors_list.html': '{% extends "base.html" %}\n'
                          '{% block title %}Kontrahenci - Faktury OSVČ{% endblock %}\n'
                          '{% block content %}\n'
                          '<div class="page-header">\n'
                          '    <div><h1>Kontrahenci</h1><p class="muted">Dodaj firmę raz, a później wybieraj ją przy '
                          'wystawianiu faktury.</p></div>\n'
                          '    <a class="btn btn-primary" href="{{ url_for(\'contractor_new\') }}">+ Dodaj '
                          'kontrahenta</a>\n'
                          '</div>\n'
                          '<form class="toolbar" method="get">\n'
                          '    <input type="search" name="q" value="{{ q }}" placeholder="Szukaj po nazwie, VAT ID lub '
                          'mieście">\n'
                          '    <button class="btn btn-light" type="submit">Szukaj</button>\n'
                          '    {% if q %}<a class="btn btn-ghost" href="{{ url_for(\'contractors_list\') '
                          '}}">Wyczyść</a>{% endif %}\n'
                          '</form>\n'
                          '<section class="panel">\n'
                          '{% if contractors %}\n'
                          '<div class="table-wrap">\n'
                          '<table>\n'
                          '    <thead><tr><th>Nazwa</th><th>Miasto / kraj</th><th>VAT '
                          'ID</th><th>E-mail</th><th></th></tr></thead>\n'
                          '    <tbody>\n'
                          '    {% for contractor in contractors %}\n'
                          '    <tr>\n'
                          '        <td><strong>{{ contractor.name }}</strong><div class="muted small">{{ '
                          'contractor.address }}</div></td>\n'
                          '        <td>{{ contractor.city }}{% if contractor.country %}, {{ contractor.country }}{% '
                          'endif %}</td>\n'
                          '        <td>{{ contractor.vat_id }}</td>\n'
                          '        <td>{{ contractor.email }}</td>\n'
                          '        <td class="actions">\n'
                          '            <a class="btn btn-light btn-small" href="{{ url_for(\'invoice_new\', '
                          'contractor_id=contractor.id) }}">Faktura</a>\n'
                          '            <a class="btn btn-light btn-small" href="{{ url_for(\'contractor_edit\', '
                          'contractor_id=contractor.id) }}">Edytuj</a>\n'
                          '            <form method="post" action="{{ url_for(\'contractor_delete\', '
                          'contractor_id=contractor.id) }}" class="inline-form" onsubmit="return confirm(\'Usunąć tego '
                          'kontrahenta?\')"><button class="btn btn-danger btn-small" '
                          'type="submit">Usuń</button></form>\n'
                          '        </td>\n'
                          '    </tr>\n'
                          '    {% endfor %}\n'
                          '    </tbody>\n'
                          '</table>\n'
                          '</div>\n'
                          '{% else %}\n'
                          '<div class="empty-state"><h3>Brak kontrahentów</h3><p>Dodaj pierwszą firmę, aby wystawić '
                          'dla niej fakturę.</p><a class="btn btn-primary" href="{{ url_for(\'contractor_new\') '
                          '}}">Dodaj kontrahenta</a></div>\n'
                          '{% endif %}\n'
                          '</section>\n'
                          '{% endblock %}\n',
 'dashboard.html': '{% extends "base.html" %}\n'
                   '{% block title %}Start - Faktury OSVČ{% endblock %}\n'
                   '{% block content %}\n'
                   '<div class="page-header">\n'
                   '    <div>\n'
                   '        <h1>Panel działalności</h1>\n'
                   '        <p class="muted">{{ company.name }} · IČO {{ company.company_id }} · DIČ {{ company.vat_id '
                   '}}</p>\n'
                   '    </div>\n'
                   '    <a class="btn btn-primary" href="{{ url_for(\'invoice_new\') }}">Wystaw fakturę</a>\n'
                   '</div>\n'
                   '\n'
                   '{% if dph_reminder %}\n'
                   '<div class="callout warning">\n'
                   '    <strong>DPH / UE:</strong> masz {{ dph_reminder.invoice_count }} fakturę/faktury do '
                   'sprawdzenia za {{ dph_reminder.period_label }}.\n'
                   '    Podstawowy termin: <strong>{{ dph_reminder.due_date|date_pl }}</strong>.\n'
                   '    <a href="{{ url_for(\'dph_dashboard\', year=dph_reminder.year, month=dph_reminder.month) '
                   '}}">Otwórz asystenta DPH</a>.\n'
                   '</div>\n'
                   '{% endif %}\n'
                   '\n'
                   '<div class="stats-grid dashboard-stats">\n'
                   '    <div class="stat-card"><span class="stat-value compact-value">{{ '
                   'month_summary.revenue|money(\'CZK\') }}</span><span class="stat-label">Przychód w tym '
                   'miesiącu</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value compact-value">{{ '
                   'month_summary.costs|money(\'CZK\') }}</span><span class="stat-label">Koszty w tym '
                   'miesiącu</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value compact-value">{{ '
                   'month_summary.result|money(\'CZK\') }}</span><span class="stat-label">Wynik przed '
                   'podatkami</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value">{{ counts.unpaid }}</span><span '
                   'class="stat-label">Nieopłacone faktury</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value">{{ counts.invoices }}</span><span '
                   'class="stat-label">Wszystkie faktury</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value">{{ counts.contractors }}</span><span '
                   'class="stat-label">Kontrahenci</span></div>\n'
                   '</div>\n'
                   '\n'
                   '<section class="panel">\n'
                   '    <div class="panel-header">\n'
                   '        <h2>Ostatnie faktury</h2>\n'
                   '        <a href="{{ url_for(\'invoices_list\') }}">Pokaż wszystkie</a>\n'
                   '    </div>\n'
                   '    {% if recent %}\n'
                   '    <div class="table-wrap">\n'
                   '        <table>\n'
                   '            '
                   '<thead><tr><th>Numer</th><th>Kontrahent</th><th>Data</th><th>Kwota</th><th>Status</th><th></th></tr></thead>\n'
                   '            <tbody>\n'
                   '            {% for invoice in recent %}\n'
                   '                <tr>\n'
                   '                    <td><a href="{{ url_for(\'invoice_view\', invoice_id=invoice.id) '
                   '}}"><strong>{{ invoice.invoice_number }}</strong></a></td>\n'
                   '                    <td>{{ invoice.contractor_name }}</td>\n'
                   '                    <td>{{ invoice.issue_date|date_pl }}</td>\n'
                   '                    <td>{{ invoice.total|money(invoice.currency) }}</td>\n'
                   '                    <td><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if '
                   "invoice.status == 'paid' else 'Nieopłacona' }}</span></td>\n"
                   '                    <td class="actions"><a class="btn btn-light btn-small" href="{{ '
                   'url_for(\'invoice_pdf\', invoice_id=invoice.id) }}">PDF</a></td>\n'
                   '                </tr>\n'
                   '            {% endfor %}\n'
                   '            </tbody>\n'
                   '        </table>\n'
                   '    </div>\n'
                   '    {% else %}\n'
                   '        <div class="empty-state"><h3>Nie masz jeszcze faktur</h3><p>Dodaj kontrahenta, a następnie '
                   'wystaw pierwszą fakturę.</p></div>\n'
                   '    {% endif %}\n'
                   '</section>\n'
                   '{% endblock %}\n',
 'dph_dashboard.html': '{% extends "base.html" %}\n'
                       '{% block title %}DPH / UE - Faktury OSVČ{% endblock %}\n'
                       '{% block content %}\n'
                       '<div class="page-header"><div><h1>Asystent DPH / UE</h1><p class="muted">Przygotowanie '
                       'Souhrnné hlášení VIES dla usług B2B w UE.</p></div><div class="button-row"><a class="btn '
                       'btn-light" target="_blank" rel="noopener" href="{{ vies_url }}">Sprawdź VAT ID w VIES</a><a '
                       'class="btn btn-primary" target="_blank" rel="noopener" href="{{ moje_dane_url }}">Otwórz MOJE '
                       'daně</a></div></div>\n'
                       '<div class="callout info"><strong>Proces:</strong> sprawdź dane, pobierz XML, otwórz MOJE '
                       'daně, wybierz wczytanie pliku, wykonaj kontrolę i dopiero wtedy wyślij. Program nie loguje się '
                       'do urzędu i nie wysyła zgłoszenia automatycznie.</div>\n'
                       '<div class="callout warning"><strong>Zakres automatyki:</strong> moduł zakłada zwykłą usługę '
                       'B2B w UE według § 9 odst. 1. Usługi związane z nieruchomością lub budową, zaliczki, korekty i '
                       'inne szczególne przypadki wymagają ręcznego sprawdzenia.</div>\n'
                       '<form class="toolbar" method="get"><select name="month">{% for value, label in MONTHS.items() '
                       '%}<option value="{{ value }}" {% if selected_month == value %}selected{% endif %}>{{ label '
                       '}}</option>{% endfor %}</select><input type="number" name="year" value="{{ selected_year }}" '
                       'min="2020" max="2100"><button class="btn btn-light" type="submit">Pokaż okres</button></form>\n'
                       '<div class="stats-grid dashboard-stats"><div class="stat-card"><span class="stat-value">{{ '
                       'report.invoice_count }}</span><span class="stat-label">Faktury / transakcje</span></div><div '
                       'class="stat-card"><span class="stat-value compact-value">{{ report.total_czk|money(\'CZK\') '
                       '}}</span><span class="stat-label">Wartość do SH</span></div><div class="stat-card"><span '
                       'class="stat-value compact-value">{{ due_date|date_pl }}</span><span '
                       'class="stat-label">Podstawowy termin</span></div><div class="stat-card"><span '
                       'class="stat-value status-value">{{ \'Wysłane\' if filing and filing.status == \'filed\' else '
                       '\'Do wysłania\' }}</span><span class="stat-label">Status {{ period_label '
                       '}}</span></div></div>\n'
                       '{% if report.warnings %}<div class="callout warning"><strong>Przed eksportem '
                       'popraw:</strong><ul class="compact-list">{% for warning in report.warnings %}<li>{{ warning '
                       '}}</li>{% endfor %}</ul></div>{% endif %}\n'
                       '{% if report.rows %}<section class="panel"><div class="panel-header"><h2>Wiersze Souhrnné '
                       'hlášení</h2><div class="button-row"><a class="btn btn-light" href="{{ '
                       'url_for(\'dph_export_csv\', year=selected_year, month=selected_month) }}">CSV kontrolny</a><a '
                       'class="btn btn-primary {% if report.block_export %}disabled-link{% endif %}" {% if not '
                       'report.block_export %}href="{{ url_for(\'dph_export_shv\', year=selected_year, '
                       'month=selected_month) }}"{% endif %}>Pobierz XML do MOJE daně</a></div></div><div '
                       'class="table-wrap"><table><thead><tr><th>Kraj</th><th>VAT ID bez prefiksu</th><th>Kod '
                       'plnění</th><th>Liczba transakcji</th><th>Wartość CZK</th></tr></thead><tbody>{% for row in '
                       'report.rows %}<tr><td>{{ row.country_code }}</td><td><strong>{{ row.vat_number '
                       "}}</strong></td><td>3</td><td>{{ row.count }}</td><td>{{ row.value_czk|money('CZK') "
                       '}}</td></tr>{% endfor %}</tbody></table></div></section>\n'
                       '<section class="panel"><div class="panel-header"><h2>Faktury źródłowe</h2></div><div '
                       'class="table-wrap"><table><thead><tr><th>Faktura</th><th>Kontrahent</th><th>Data '
                       'usługi</th><th>Kwota</th><th>Kurs</th><th>W CZK</th></tr></thead><tbody>{% for invoice in '
                       'report.invoices %}<tr><td><a href="{{ url_for(\'invoice_view\', invoice_id=invoice.id) '
                       '}}"><strong>{{ invoice.invoice_number }}</strong></a></td><td>{{ invoice.contractor_name '
                       '}}<br><span class="small muted">{{ invoice.contractor_vat_id }}</span></td><td>{{ '
                       'invoice.supply_date|date_pl }}</td><td>{{ invoice.total_net|money(invoice.currency) '
                       "}}</td><td>{{ invoice.rate or 'brak' }}</td><td>{{ invoice.value_czk|money('CZK') if "
                       "invoice.value_czk is not none else '—' }}</td></tr>{% endfor "
                       '%}</tbody></table></div></section>{% else %}<section class="panel"><div '
                       'class="empty-state"><h3>Brak faktur do SH VIES za {{ period_label }}</h3><p>Zerowego '
                       'souhrnného hlášení nie przygotowuje się.</p></div></section>{% endif %}\n'
                       '{% if filing and filing.status == \'filed\' %}<section class="panel"><div '
                       'class="panel-header"><h2>Oznaczone jako wysłane</h2><span class="badge badge-paid">{{ '
                       'filing.filed_date|date_pl }}</span></div><p>Numer potwierdzenia: <strong>{{ '
                       'filing.confirmation_number or \'nie podano\' }}</strong></p><form method="post" action="{{ '
                       'url_for(\'dph_reopen_filing\') }}"><input type="hidden" name="year" value="{{ selected_year '
                       '}}"><input type="hidden" name="month" value="{{ selected_month }}"><button class="btn '
                       'btn-light" type="submit">Oznacz jako niewysłane</button></form></section>{% else %}<section '
                       'class="panel"><div class="panel-header"><h2>Po wysłaniu w MOJE daně</h2></div><form '
                       'method="post" action="{{ url_for(\'dph_mark_filed\') }}" class="form-panel '
                       'compact-form"><input type="hidden" name="year" value="{{ selected_year }}"><input '
                       'type="hidden" name="month" value="{{ selected_month }}"><div class="form-grid two"><label '
                       'class="field"><span>Data wysłania</span><input type="date" name="filed_date" value="{{ today '
                       '}}" required></label><label class="field"><span>ID podání / numer potwierdzenia</span><input '
                       'name="confirmation_number"></label><label class="field span-2"><span>Uwagi</span><textarea '
                       'name="notes" rows="2"></textarea></label></div><div class="form-actions"><button class="btn '
                       'btn-primary" type="submit">Oznacz jako wysłane</button></div></form></section>{% endif %}\n'
                       '{% if foreign_expenses %}<section class="panel"><div class="panel-header"><h2>Koszty '
                       'wymagające sprawdzenia DPH</h2></div><div '
                       'class="table-wrap"><table><thead><tr><th>Data</th><th>Opis</th><th>Dostawca</th><th>Wartość</th></tr></thead><tbody>{% '
                       'for expense in foreign_expenses %}<tr><td>{{ expense.expense_date|date_pl }}</td><td>{{ '
                       "expense.description }}</td><td>{{ expense.supplier or '—' }}</td><td>{{ "
                       "expense.amount_czk|money('CZK') }}</td></tr>{% endfor %}</tbody></table></div><p "
                       'class="small muted">Zakup zagranicznej usługi może wymagać czeskiego přiznání k DPH. Ten moduł '
                       'przygotowuje obecnie SH VIES dla sprzedaży; koszt oznacza tylko jako '
                       'ostrzeżenie.</p></section>{% endif %}\n'
                       '<p class="small muted">XML jest przygotowywany według struktury DPHSHV 02.01.04. Portal MOJE '
                       'daně pozostaje ostatecznym miejscem kontroli i wysłania.</p>\n'
                       '{% endblock %}\n',
 'expense_form.html': '{% extends "base.html" %}\n'
                      "{% block title %}{{ 'Edytuj koszt' if is_edit else 'Nowy koszt' }} - Faktury OSVČ{% endblock "
                      '%}\n'
                      '{% block content %}\n'
                      '<div class="page-header"><div><h1>{{ \'Edytuj koszt\' if is_edit else \'Nowy koszt\' }}</h1><p '
                      'class="muted">Wprowadź rzeczywiście poniesiony wydatek związany z '
                      'działalnością.</p></div></div>\n'
                      '<form method="post" class="panel form-panel expense-form">\n'
                      '<div class="form-grid three">\n'
                      '<label class="field"><span>Data *</span><input type="date" name="expense_date" value="{{ '
                      'expense.expense_date }}" required></label>\n'
                      '<label class="field"><span>Dostawca</span><input name="supplier" value="{{ expense.supplier '
                      '}}"></label>\n'
                      '<label class="field"><span>Numer dokumentu</span><input name="document_number" value="{{ '
                      'expense.document_number }}"></label>\n'
                      '<label class="field span-2"><span>Opis kosztu *</span><input name="description" value="{{ '
                      'expense.description }}" required></label>\n'
                      '<label class="field"><span>Kategoria</span><select name="category">{% for code, label in '
                      'EXPENSE_CATEGORIES.items() %}<option value="{{ code }}" {% if expense.category == code '
                      '%}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n'
                      '<label class="field"><span>Kwota *</span><input id="expense-amount" type="number" step="0.01" '
                      'min="0" name="amount" value="{{ expense.amount }}" required></label>\n'
                      '<label class="field"><span>Waluta</span><input id="expense-currency" name="currency" value="{{ '
                      'expense.currency }}" maxlength="6"></label>\n'
                      '<label class="field"><span>Kurs do CZK</span><input id="expense-rate" type="number" '
                      'step="0.0001" min="0" name="czk_rate" value="{{ expense.czk_rate }}"></label>\n'
                      '<label class="field"><span>Część związana z działalnością (%)</span><input type="number" '
                      'min="0" max="100" step="1" name="business_percent" value="{{ expense.business_percent '
                      '}}"></label>\n'
                      '<label class="field"><span>Sposób płatności</span><select name="payment_method">{% for code, '
                      'label in PAYMENT_METHODS.items() %}<option value="{{ code }}" {% if expense.payment_method == '
                      'code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n'
                      '<label class="field"><span>Klasyfikacja DPH</span><select name="dph_obligation">{% for code, '
                      'label in EXPENSE_DPH_OBLIGATIONS.items() %}<option value="{{ code }}" {% if '
                      'expense.dph_obligation == code %}selected{% endif %}>{{ label }}</option>{% endfor '
                      '%}</select></label>\n'
                      '<label class="field"><span>VAT ID dostawcy</span><input name="supplier_vat_id" value="{{ '
                      'expense.supplier_vat_id }}"></label>\n'
                      '<label class="field"><span>Kraj dostawcy</span><input name="country" value="{{ expense.country '
                      '}}"></label>\n'
                      '<label class="field span-3"><span>Uwagi</span><textarea name="notes" rows="3">{{ expense.notes '
                      '}}</textarea></label>\n'
                      '</div>\n'
                      '<div class="callout info">Wartość w CZK: <strong id="expense-czk-preview">0,00 '
                      'CZK</strong></div>\n'
                      '<div class="form-actions"><a class="btn btn-ghost" href="{{ url_for(\'expenses_page\') '
                      '}}">Anuluj</a><button class="btn btn-primary" type="submit">Zapisz koszt</button></div>\n'
                      '</form>\n'
                      '{% if is_edit %}<div class="danger-zone"><span></span><form method="post" action="{{ '
                      'url_for(\'expense_delete\', expense_id=expense.id) }}" onsubmit="return confirm(\'Usunąć ten '
                      'koszt?\')"><button class="btn btn-danger" type="submit">Usuń koszt</button></form></div>{% '
                      'endif %}\n'
                      '{% endblock %}\n',
 'expenses.html': '{% extends "base.html" %}\n'
                  '{% block title %}Koszty - Faktury OSVČ{% endblock %}\n'
                  '{% block content %}\n'
                  '<div class="page-header"><div><h1>Koszty działalności</h1><p class="muted">Bieżąca ewidencja '
                  'wydatków i prosty wynik miesięczny.</p></div><div class="button-row"><a class="btn btn-light" '
                  'href="{{ url_for(\'expenses_export_csv\', year=selected_year, month=selected_month) }}">Eksport '
                  'CSV</a><a class="btn btn-primary" href="{{ url_for(\'expense_new\') }}">+ Dodaj '
                  'koszt</a></div></div>\n'
                  '<form class="toolbar" method="get"><select name="month">{% for value, label in MONTHS.items() '
                  '%}<option value="{{ value }}" {% if selected_month == value %}selected{% endif %}>{{ label '
                  '}}</option>{% endfor %}</select><input type="number" name="year" value="{{ selected_year }}" '
                  'min="2020" max="2100"><button class="btn btn-light" type="submit">Pokaż okres</button></form>\n'
                  '<div class="stats-grid dashboard-stats"><div class="stat-card"><span class="stat-value '
                  'compact-value">{{ totals.revenue|money(\'CZK\') }}</span><span class="stat-label">Przychód z '
                  'faktur</span></div><div class="stat-card"><span class="stat-value compact-value">{{ '
                  'totals.actual|money(\'CZK\') }}</span><span class="stat-label">Wydatki rzeczywiste</span></div><div '
                  'class="stat-card"><span class="stat-value compact-value">{{ totals.deductible|money(\'CZK\') '
                  '}}</span><span class="stat-label">Część kosztowa</span></div><div class="stat-card"><span '
                  'class="stat-value compact-value">{{ totals.result|money(\'CZK\') }}</span><span '
                  'class="stat-label">Wynik przed podatkami</span></div></div>\n'
                  '{% if missing_rates %}<div class="callout warning">Nie wszystkie faktury lub koszty mają kurs do '
                  'CZK. Suma może być niepełna.</div>{% endif %}\n'
                  '{% if foreign_dph_count %}<div class="callout warning"><strong>DPH:</strong> {{ foreign_dph_count '
                  '}} kosztów oznaczono jako zagraniczne usługi lub do sprawdzenia. Zobacz zakładkę <a href="{{ '
                  'url_for(\'dph_dashboard\', year=selected_year, month=selected_month) }}">DPH / UE</a>.</div>{% '
                  'endif %}\n'
                  '{% if category_totals %}<section class="panel"><div class="panel-header"><h2>Podział kosztów - {{ '
                  'period_label }}</h2></div><div class="category-grid">{% for item in category_totals %}<div '
                  'class="category-card"><span>{{ EXPENSE_CATEGORIES[item.category] }}</span><strong>{{ '
                  "item.total|money('CZK') }}</strong></div>{% endfor %}</div></section>{% endif %}\n"
                  '<section class="panel"><div class="panel-header"><h2>Wydatki - {{ period_label }}</h2></div>{% if '
                  'expenses %}<div '
                  'class="table-wrap"><table><thead><tr><th>Data</th><th>Opis</th><th>Dostawca</th><th>Kategoria</th><th>Dokument</th><th>Kwota</th><th>W '
                  'CZK</th><th></th></tr></thead><tbody>{% for expense in expenses %}<tr><td>{{ '
                  'expense.expense_date|date_pl }}</td><td><strong>{{ expense.description }}</strong>{% if '
                  'expense.dph_obligation != \'none\' %}<br><span class="badge badge-warning">DPH do '
                  "sprawdzenia</span>{% endif %}</td><td>{{ expense.supplier or '—' }}</td><td>{{ "
                  "EXPENSE_CATEGORIES[expense.category] }}</td><td>{{ expense.document_number or '—' }}</td><td>{{ "
                  "expense.amount|money(expense.currency) }}</td><td>{{ expense.amount_czk|money('CZK') if "
                  'expense.amount_czk is not none else \'brak kursu\' }}</td><td><a class="btn btn-light btn-small" '
                  'href="{{ url_for(\'expense_edit\', expense_id=expense.id) }}">Edytuj</a></td></tr>{% endfor '
                  '%}</tbody></table></div>{% else %}<div class="empty-state"><h3>Brak kosztów w tym miesiącu</h3><a '
                  'class="btn btn-primary" href="{{ url_for(\'expense_new\') }}">Dodaj pierwszy koszt</a></div>{% '
                  'endif %}</section>\n'
                  '{% endblock %}\n',
 'invoice_detail.html': '{% extends "base.html" %}\n'
                        '{% block title %}{{ invoice.invoice_number }} - Faktury OSVČ{% endblock %}\n'
                        '{% block content %}\n'
                        '<div class="page-header"><div><h1>{{ invoice.invoice_number }}</h1><p class="muted">{{ '
                        'invoice.contractor_name }} · {{ invoice.issue_date|date_pl }}</p></div><div '
                        'class="button-row"><a class="btn btn-primary" href="{{ url_for(\'invoice_pdf\', '
                        'invoice_id=invoice.id) }}">Pobierz PDF A4</a><a class="btn btn-light" href="{{ '
                        'url_for(\'invoice_edit\', invoice_id=invoice.id) }}">Edytuj</a></div></div>\n'
                        '\n'
                        '{% if dph_info.kind == \'eu_service\' %}<div class="callout info"><strong>DPH / UE:</strong> '
                        'ta faktura zostanie ujęta w Souhrnné hlášení za {{ dph_info.period_label }} z kodem plnění 3. '
                        'Podstawowy termin: {{ dph_info.due_date|date_pl }}. <a href="{{ url_for(\'dph_dashboard\', '
                        'year=invoice.supply_date[:4], month=invoice.supply_date[5:7]) }}">Otwórz raport</a>.</div>{% '
                        'elif dph_info.warning %}<div class="callout warning"><strong>DPH do sprawdzenia:</strong> {{ '
                        'dph_info.warning }}</div>{% endif %}\n'
                        '\n'
                        '<div class="detail-grid">\n'
                        '<section class="panel"><div class="panel-header"><h2>Dane faktury</h2><span class="badge '
                        'badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == \'paid\' else '
                        '\'Nieopłacona\' }}</span></div><dl class="details">\n'
                        '<div><dt>Kontrahent</dt><dd>{{ invoice.contractor_name }}</dd></div><div><dt>VAT '
                        "ID</dt><dd>{{ invoice.contractor_vat_id or '—' }}</dd></div><div><dt>Data "
                        'wystawienia</dt><dd>{{ invoice.issue_date|date_pl }}</dd></div><div><dt>Data '
                        'usługi</dt><dd>{{ invoice.supply_date|date_pl }}</dd></div><div><dt>Termin '
                        'płatności</dt><dd>{{ invoice.due_date|date_pl }}</dd></div><div><dt>Tryb VAT</dt><dd>{{ '
                        'TAX_MODES[invoice.tax_mode] }}</dd></div><div><dt>Kurs do CZK</dt><dd>{{ invoice.czk_rate or '
                        "('1' if invoice.currency == 'CZK' else '—') }}</dd></div><div><dt>DPH / VIES</dt><dd>{{ "
                        'dph_info.label }}</dd></div></dl></section>\n'
                        '<section class="panel total-card"><span class="muted">Do zapłaty</span><strong>{{ '
                        'totals.total_gross|money(invoice.currency) }}</strong>{% if dph_info.value_czk is not none '
                        "%}<small>Do ewidencji: {{ dph_info.value_czk|money('CZK') }}</small>{% endif %}</section>\n"
                        '</div>\n'
                        '<section class="panel"><h2>Pozycje</h2><div '
                        'class="table-wrap"><table><thead><tr><th>Opis</th><th>Ilość</th><th>Jedn.</th><th>Cena</th>{% '
                        "if invoice.tax_mode == 'vat' %}<th>VAT</th>{% endif %}<th>Wartość</th></tr></thead><tbody>{% "
                        'for item in items %}<tr><td>{{ item.description }}</td><td>{{ item.quantity_decimal '
                        '}}</td><td>{{ item.unit }}</td><td>{{ item.unit_price_decimal|money(invoice.currency) '
                        "}}</td>{% if invoice.tax_mode == 'vat' %}<td>{{ item.vat_rate_decimal }}%</td>{% endif "
                        '%}<td>{{ item.gross|money(invoice.currency) }}</td></tr>{% endfor '
                        '%}</tbody></table></div></section>\n'
                        '{% if invoice.reverse_charge_note or invoice.notes %}<section class="panel">{% if '
                        'invoice.reverse_charge_note %}<h2>Adnotacja</h2><p>{{ invoice.reverse_charge_note }}</p>{% '
                        'endif %}{% if invoice.notes %}<h2>Uwagi</h2><p class="preline">{{ invoice.notes }}</p>{% '
                        'endif %}</section>{% endif %}\n'
                        '<div class="danger-zone"><form method="post" action="{{ url_for(\'invoice_toggle_paid\', '
                        'invoice_id=invoice.id) }}"><button class="btn btn-light" type="submit">{{ \'Oznacz jako '
                        "nieopłaconą' if invoice.status == 'paid' else 'Oznacz jako opłaconą' }}</button></form><form "
                        'method="post" action="{{ url_for(\'invoice_delete\', invoice_id=invoice.id) }}" '
                        'onsubmit="return confirm(\'Usunąć fakturę {{ invoice.invoice_number }}?\')"><button '
                        'class="btn btn-danger" type="submit">Usuń fakturę</button></form></div>\n'
                        '{% endblock %}\n',
 'invoice_form.html': '{% extends "base.html" %}\n'
                      "{% block title %}{{ 'Edytuj fakturę' if is_edit else 'Nowa faktura' }} - Faktury OSVČ{% "
                      'endblock %}\n'
                      '{% block content %}\n'
                      '<div class="page-header">\n'
                      "    <div><h1>{{ 'Edytuj fakturę ' ~ invoice.invoice_number if is_edit else 'Nowa faktura' "
                      '}}</h1><p class="muted">PDF zostanie wygenerowany w formacie A4.</p></div>\n'
                      '</div>\n'
                      '{% if not contractors %}<div class="callout warning"><strong>Najpierw dodaj '
                      'kontrahenta.</strong> <a href="{{ url_for(\'contractor_new\') }}">Dodaj teraz</a>.</div>{% '
                      'endif %}\n'
                      '<form method="post" class="panel form-panel invoice-form" data-default-due-days="{{ '
                      'company.default_due_days }}">\n'
                      '    <h2>Dane dokumentu</h2>\n'
                      '    <div class="form-grid three">\n'
                      '        <label class="field span-2"><span>Kontrahent *</span><select id="contractor-select" '
                      'name="contractor_id" required><option value="">-- wybierz --</option>{% for contractor in '
                      'contractors %}<option value="{{ contractor.id }}" data-vat="{{ contractor.vat_id }}" '
                      'data-country="{{ contractor.country }}" {% if invoice.contractor_id|string == '
                      'contractor.id|string %}selected{% endif %}>{{ contractor.name }}{% if contractor.vat_id %} ({{ '
                      'contractor.vat_id }}){% endif %}</option>{% endfor %}</select></label>\n'
                      '        <div class="field field-button"><span>&nbsp;</span><a class="btn btn-light" href="{{ '
                      'url_for(\'contractor_new\') }}">+ Nowy kontrahent</a></div>\n'
                      '        <label class="field"><span>Data wystawienia *</span><input id="issue-date" type="date" '
                      'name="issue_date" value="{{ invoice.issue_date }}" required></label>\n'
                      '        <label class="field"><span>Data wykonania usługi *</span><input type="date" '
                      'name="supply_date" value="{{ invoice.supply_date }}" required></label>\n'
                      '        <label class="field"><span>Termin płatności *</span><input id="due-date" type="date" '
                      'name="due_date" value="{{ invoice.due_date }}" required></label>\n'
                      '        <label class="field"><span>Waluta</span><input id="invoice-currency" name="currency" '
                      'value="{{ invoice.currency }}" maxlength="6" list="currency-list"><datalist '
                      'id="currency-list"><option value="EUR"><option value="CZK"><option value="PLN"><option '
                      'value="USD"></datalist></label>\n'
                      '        <label class="field"><span>Kurs do CZK</span><input id="czk-rate" type="number" '
                      'step="0.0001" min="0" name="czk_rate" value="{{ invoice.czk_rate }}" placeholder="np. '
                      '24,85"><small>1 jednostka waluty faktury = ... CZK</small></label>\n'
                      '        <label class="field"><span>Język PDF</span><select name="language">{% for code, label '
                      'in LANGUAGES.items() %}<option value="{{ code }}" {% if invoice.language == code %}selected{% '
                      'endif %}>{{ label }}</option>{% endfor %}</select></label>\n'
                      '        <label class="field"><span>Tryb VAT</span><select id="tax-mode" name="tax_mode">{% for '
                      'code, label in TAX_MODES.items() %}<option value="{{ code }}" {% if invoice.tax_mode == code '
                      '%}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n'
                      '        <label class="field span-2"><span>Klasyfikacja DPH / VIES</span><select '
                      'id="dph-category" name="dph_category">{% for code, label in DPH_CATEGORIES.items() %}<option '
                      'value="{{ code }}" {% if invoice.dph_category == code %}selected{% endif %}>{{ label '
                      '}}</option>{% endfor %}</select></label>\n'
                      '        <label class="field"><span>Sposób płatności</span><select name="payment_method">{% for '
                      'code, label in PAYMENT_METHODS.items() %}<option value="{{ code }}" {% if '
                      'invoice.payment_method == code %}selected{% endif %}>{{ label }}</option>{% endfor '
                      '%}</select></label>\n'
                      '        <label class="field"><span>Numer zamówienia</span><input name="order_number" value="{{ '
                      'invoice.order_number }}"></label>\n'
                      '        <label class="field"><span>Symbol / referencja płatności</span><input '
                      'name="variable_symbol" value="{{ invoice.variable_symbol }}" placeholder="Wygeneruje się '
                      'automatycznie"></label>\n'
                      '    </div>\n'
                      '    <div id="dph-hint" class="callout info dph-hint">Program sprawdzi VAT ID kontrahenta i tryb '
                      'faktury.</div>\n'
                      '    <p class="small muted">Automatyczna klasyfikacja jest podpowiedzią. Dla usług związanych z '
                      'nieruchomością lub budową, zaliczek, korekt oraz nietypowych transakcji wybierz „Do ręcznego '
                      'sprawdzenia”.</p>\n'
                      '\n'
                      '    <hr>\n'
                      '    <div class="panel-header"><h2>Pozycje faktury</h2><button id="add-item" class="btn '
                      'btn-light btn-small" type="button">+ Dodaj pozycję</button></div>\n'
                      '    <div class="table-wrap invoice-items-wrap">\n'
                      '        <table class="invoice-items">\n'
                      '            <thead><tr><th>Opis</th><th>Ilość</th><th>Jedn.</th><th>Cena jedn.</th><th '
                      'class="vat-column">VAT %</th><th>Wartość</th><th></th></tr></thead>\n'
                      '            <tbody id="invoice-items-body">\n'
                      '                {% for item in items %}\n'
                      '                <tr class="invoice-item-row">\n'
                      '                    <td><input name="item_description" value="{{ item.description }}" '
                      'placeholder="Opis usługi" required></td>\n'
                      '                    <td><input class="qty-input" type="number" step="0.01" min="0.01" '
                      'name="item_quantity" value="{{ item.quantity }}" required></td>\n'
                      '                    <td><input name="item_unit" value="{{ item.unit }}" placeholder="h"></td>\n'
                      '                    <td><input class="price-input" type="number" step="0.01" min="0" '
                      'name="item_unit_price" value="{{ item.unit_price }}" required></td>\n'
                      '                    <td class="vat-column"><input class="vat-input" type="number" step="0.01" '
                      'min="0" max="100" name="item_vat_rate" value="{{ item.vat_rate or 0 }}"></td>\n'
                      '                    <td class="line-total">0.00</td>\n'
                      '                    <td><button class="icon-button remove-item" type="button" title="Usuń '
                      'pozycję">×</button></td>\n'
                      '                </tr>\n'
                      '                {% endfor %}\n'
                      '            </tbody>\n'
                      '            <tfoot><tr><td colspan="5" class="total-label">Razem</td><td '
                      'id="invoice-total">0.00 {{ invoice.currency }}</td><td></td></tr></tfoot>\n'
                      '        </table>\n'
                      '    </div>\n'
                      '\n'
                      '    <div id="reverse-charge-section" class="form-grid one"><label class="field"><span>Adnotacja '
                      'reverse charge</span><textarea name="reverse_charge_note" rows="3">{{ '
                      'invoice.reverse_charge_note }}</textarea></label></div>\n'
                      '    <label class="field"><span>Uwagi na fakturze</span><textarea name="notes" rows="4">{{ '
                      'invoice.notes }}</textarea></label>\n'
                      '    <div class="form-actions"><a class="btn btn-ghost" href="{{ url_for(\'invoices_list\') '
                      '}}">Anuluj</a><button class="btn btn-primary" type="submit" {% if not contractors %}disabled{% '
                      "endif %}>{{ 'Zapisz zmiany' if is_edit else 'Wystaw fakturę' }}</button></div>\n"
                      '</form>\n'
                      '\n'
                      '<template id="invoice-item-template"><tr class="invoice-item-row"><td><input '
                      'name="item_description" placeholder="Opis usługi" required></td><td><input class="qty-input" '
                      'type="number" step="0.01" min="0.01" name="item_quantity" value="1" required></td><td><input '
                      'name="item_unit" value="h"></td><td><input class="price-input" type="number" step="0.01" '
                      'min="0" name="item_unit_price" required></td><td class="vat-column"><input class="vat-input" '
                      'type="number" step="0.01" min="0" max="100" name="item_vat_rate" value="0"></td><td '
                      'class="line-total">0.00</td><td><button class="icon-button remove-item" '
                      'type="button">×</button></td></tr></template>\n'
                      '{% endblock %}\n',
 'invoices_list.html': '{% extends "base.html" %}\n'
                       '{% block title %}Faktury - Faktury OSVČ{% endblock %}\n'
                       '{% block content %}\n'
                       '<div class="page-header">\n'
                       '    <div><h1>Faktury</h1><p class="muted">Lista wystawionych dokumentów.</p></div>\n'
                       '    <a class="btn btn-primary" href="{{ url_for(\'invoice_new\') }}">+ Nowa faktura</a>\n'
                       '</div>\n'
                       '<form class="toolbar" method="get">\n'
                       '    <input type="search" name="q" value="{{ q }}" placeholder="Numer, kontrahent lub VAT ID">\n'
                       '    <select name="status"><option value="">Wszystkie statusy</option><option value="unpaid" {% '
                       'if status == \'unpaid\' %}selected{% endif %}>Nieopłacone</option><option value="paid" {% if '
                       "status == 'paid' %}selected{% endif %}>Opłacone</option></select>\n"
                       '    <button class="btn btn-light" type="submit">Filtruj</button>\n'
                       '    {% if q or status %}<a class="btn btn-ghost" href="{{ url_for(\'invoices_list\') '
                       '}}">Wyczyść</a>{% endif %}\n'
                       '</form>\n'
                       '<section class="panel">\n'
                       '{% if invoices %}\n'
                       '<div class="table-wrap"><table>\n'
                       '<thead><tr><th>Numer</th><th>Kontrahent</th><th>Wystawiona</th><th>Termin</th><th>Kwota</th><th>Status</th><th></th></tr></thead>\n'
                       '<tbody>\n'
                       '{% for invoice in invoices %}\n'
                       '<tr>\n'
                       '    <td><a href="{{ url_for(\'invoice_view\', invoice_id=invoice.id) }}"><strong>{{ '
                       'invoice.invoice_number }}</strong></a></td>\n'
                       '    <td>{{ invoice.contractor_name }}</td>\n'
                       '    <td>{{ invoice.issue_date|date_pl }}</td>\n'
                       '    <td>{{ invoice.due_date|date_pl }}</td>\n'
                       '    <td>{{ invoice.total|money(invoice.currency) }}</td>\n'
                       '    <td><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == '
                       "'paid' else 'Nieopłacona' }}</span></td>\n"
                       '    <td class="actions">\n'
                       '        <a class="btn btn-light btn-small" href="{{ url_for(\'invoice_pdf\', '
                       'invoice_id=invoice.id) }}">PDF</a>\n'
                       '        <a class="btn btn-light btn-small" href="{{ url_for(\'invoice_edit\', '
                       'invoice_id=invoice.id) }}">Edytuj</a>\n'
                       '    </td>\n'
                       '</tr>\n'
                       '{% endfor %}\n'
                       '</tbody></table></div>\n'
                       '{% else %}\n'
                       '<div class="empty-state"><h3>Brak faktur</h3><p>Wystaw pierwszą fakturę w formacie A4 '
                       'PDF.</p><a class="btn btn-primary" href="{{ url_for(\'invoice_new\') }}">Nowa '
                       'faktura</a></div>\n'
                       '{% endif %}\n'
                       '</section>\n'
                       '{% endblock %}\n',
 'tools.html': '{% extends "base.html" %}\n'
               '{% block title %}Narzędzia - Faktury OSVČ{% endblock %}\n'
               '{% block content %}\n'
               '<div class="page-header"><div><h1>Narzędzia i kopie zapasowe</h1><p class="muted">Reset faktur nie '
               'usuwa kontrahentów, kosztów ani danych firmy.</p></div></div>\n'
               '<div class="stats-grid"><div class="stat-card"><span class="stat-value">{{ counts.invoices '
               '}}</span><span class="stat-label">Faktury</span></div><div class="stat-card"><span '
               'class="stat-value">{{ counts.contractors }}</span><span '
               'class="stat-label">Kontrahenci</span></div><div class="stat-card"><span class="stat-value">{{ '
               'counts.expenses }}</span><span class="stat-label">Koszty</span></div><div class="stat-card"><span '
               'class="stat-value">{{ next_numbers }}</span><span class="stat-label">Kolejne '
               'numery</span></div></div>\n'
               '<section class="panel"><div class="panel-header"><h2>Folder faktur PDF</h2></div><p class="muted '
               'preline">{{ invoices_path }}</p><div class="button-row"><a class="btn btn-primary" href="{{ '
               'url_for(\'open_invoices_folder\') }}">Otwórz folder faktur</a></div></section>\n'
               '<section class="panel"><div class="panel-header"><h2>Kopia bazy danych</h2></div><p>Plik zawiera dane '
               'firmy, kontrahentów, faktury, koszty i statusy zgłoszeń DPH.</p><div class="button-row"><a class="btn '
               'btn-primary" href="{{ url_for(\'backup_database\') }}">Pobierz kopię bazy</a></div></section>\n'
               '<section class="panel"><div class="panel-header"><h2>Import starej bazy</h2></div><p>Wybierz stary '
               'plik <code>data/invoice_app.db</code> albo kopię <code>.sqlite3</code>. Program automatycznie doda '
               'nowe tabele i pola bez usuwania kontrahentów.</p><form method="post" action="{{ '
               'url_for(\'import_database\') }}" enctype="multipart/form-data" class="form-panel compact-form"><label '
               'class="field"><span>Plik starej bazy</span><input type="file" name="database" '
               'accept=".db,.sqlite,.sqlite3" required></label><div class="form-actions"><button class="btn btn-light" '
               'type="submit" onclick="return confirm(\'Zaimportować wybraną bazę?\')">Importuj '
               'bazę</button></div></form></section>\n'
               '<section class="panel danger-panel"><div class="panel-header"><h2>Usuń faktury próbne i zacznij '
               'numerację od 1</h2></div><div class="callout warning"><strong>Tylko dla dokumentów testowych.</strong> '
               'Faktury i statusy SH zostaną usunięte. Kontrahenci, ustawienia podatków, składek i dane firmy pozostaną.</div><form '
               'method="post" action="{{ url_for(\'reset_invoices\') }}" class="form-panel compact-form" '
               'onsubmit="return confirm(\'Usunąć wszystkie faktury próbne?\')"><label class="field"><span>Wpisz '
               'RESET</span><input name="confirmation" placeholder="RESET" required></label><div '
               'class="form-actions"><button class="btn btn-danger" type="submit">Usuń faktury i zresetuj '
               'numerację</button></div></form></section>\n'
               '<section class="panel"><h2>Chmura / trwałość danych</h2>'
               '<p>{% if remote_db_enabled %}<strong>Aktywna:</strong> baza jest synchronizowana z prywatnym Supabase Storage po każdej zmianie.'
               '<br><span class="muted">Bucket: {{ remote_bucket }}</span>{% else %}<strong>Nieaktywna.</strong> W wersji lokalnej dane są tylko na tym komputerze.{% endif %}</p>'
               '</section>'
               '<section class="panel"><h2>Gdzie są dane?</h2><p class="muted preline">{{ db_path }}</p><p '
               'class="small muted">Sam program jest jednym plikiem, natomiast baza musi pozostać osobno, aby dane '
               'przetrwały aktualizacje.</p></section>\n'
               '{% endblock %}\n'}

EMBEDDED_CSS = ':root {\n    --bg: #f5f6f8;\n    --panel: #ffffff;\n    --text: #20252b;\n    --muted: #6d737c;\n    --line: #dfe3e8;\n    --primary: #7a1735;\n    --primary-dark: #5b1027;\n    --primary-soft: #f4eaf0;\n    --danger: #b42318;\n    --success: #147a43;\n    --warning: #a15c00;\n    --shadow: 0 8px 24px rgba(25, 31, 40, .07);\n}\n* { box-sizing: border-box; }\nhtml { min-height: 100%; }\nbody { margin: 0; min-height: 100vh; display: flex; flex-direction: column; font-family: Inter, Segoe UI, Arial, sans-serif; color: var(--text); background: var(--bg); }\na { color: var(--primary); text-decoration: none; }\na:hover { text-decoration: underline; }\n.topbar { position: sticky; top: 0; z-index: 20; background: #fff; border-bottom: 1px solid var(--line); }\n.topbar-inner { max-width: 1180px; margin: 0 auto; min-height: 68px; padding: 0 24px; display: flex; align-items: center; gap: 26px; }\n.brand { font-size: 21px; font-weight: 800; color: var(--primary); letter-spacing: -.4px; white-space: nowrap; }\n.brand:hover { text-decoration: none; }\n.nav { display: flex; gap: 8px; flex: 1; }\n.nav a { padding: 10px 12px; color: #39404a; border-radius: 8px; }\n.nav a:hover { background: var(--primary-soft); text-decoration: none; }\n.container { width: min(1180px, calc(100% - 32px)); margin: 0 auto; }\nmain.container { flex: 1; padding-top: 34px; padding-bottom: 52px; }\n.footer { border-top: 1px solid var(--line); background: #fff; color: var(--muted); font-size: 13px; }\n.footer-inner { padding-top: 18px; padding-bottom: 18px; display: flex; justify-content: space-between; gap: 16px; }\nh1, h2, h3 { margin-top: 0; line-height: 1.2; }\nh1 { margin-bottom: 8px; font-size: 30px; letter-spacing: -.6px; }\nh2 { margin-bottom: 18px; font-size: 19px; }\np { line-height: 1.55; }\n.muted { color: var(--muted); }\n.small { font-size: 12px; }\n.preline { white-space: pre-line; }\n.page-header { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 24px; }\n.button-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }\n.button-row.center { justify-content: center; }\n.btn { display: inline-flex; align-items: center; justify-content: center; min-height: 42px; padding: 10px 17px; border-radius: 8px; border: 1px solid transparent; font: inherit; font-weight: 700; cursor: pointer; text-decoration: none; transition: .15s ease; }\n.btn:hover { text-decoration: none; transform: translateY(-1px); }\n.btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }\n.btn-primary { color: #fff; background: var(--primary); }\n.btn-primary:hover { background: var(--primary-dark); }\n.btn-light { color: #2f3640; background: #fff; border-color: #cfd4da; }\n.btn-light:hover { background: #f6f7f8; }\n.btn-ghost { color: #4c535d; background: transparent; }\n.btn-danger { color: #fff; background: var(--danger); }\n.btn-small { min-height: 34px; padding: 7px 11px; font-size: 13px; }\n.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); padding: 24px; margin-bottom: 24px; }\n.panel-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }\n.panel-header h2 { margin-bottom: 0; }\n.stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; margin-bottom: 24px; }\n.stat-card { background: #fff; border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); padding: 24px; display: flex; flex-direction: column; gap: 5px; }\n.stat-value { font-size: 32px; font-weight: 800; color: var(--primary); }\n.stat-label { color: var(--muted); }\n.table-wrap { width: 100%; overflow-x: auto; }\ntable { width: 100%; border-collapse: collapse; }\nth, td { padding: 13px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }\nth { color: #555d67; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; background: #fafbfc; }\ntbody tr:hover { background: #fcfcfd; }\n.actions { text-align: right; white-space: nowrap; }\n.inline-form { display: inline; }\n.badge { display: inline-block; padding: 5px 9px; border-radius: 999px; font-size: 12px; font-weight: 800; }\n.badge-paid { background: #e9f7ef; color: var(--success); }\n.badge-unpaid { background: #fff1e6; color: var(--warning); }\n.empty-state { text-align: center; padding: 48px 24px; color: var(--muted); }\n.empty-state h3, .empty-state h2, .empty-state h1 { color: var(--text); }\n.page-empty { padding-top: 100px; }\n.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 18px; }\n.toolbar input, .toolbar select { min-width: 240px; }\ninput, select, textarea { width: 100%; border: 1px solid #cbd1d8; border-radius: 8px; background: #fff; color: var(--text); font: inherit; padding: 10px 12px; outline: none; }\ninput:focus, select:focus, textarea:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(122, 23, 53, .11); }\ntextarea { resize: vertical; }\n.form-panel hr { border: 0; border-top: 1px solid var(--line); margin: 28px 0; }\n.form-grid { display: grid; gap: 18px; }\n.form-grid.one { grid-template-columns: 1fr; }\n.form-grid.two { grid-template-columns: repeat(2, 1fr); }\n.form-grid.three { grid-template-columns: repeat(3, 1fr); }\n.field { display: flex; flex-direction: column; gap: 7px; }\n.field > span { font-size: 13px; font-weight: 700; color: #4b535d; }\n.field-button { justify-content: flex-end; }\n.span-2 { grid-column: span 2; }\n.span-3 { grid-column: span 3; }\n.form-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 28px; padding-top: 22px; border-top: 1px solid var(--line); }\n.callout { padding: 15px 17px; border-radius: 9px; margin-bottom: 20px; border: 1px solid; }\n.callout.warning { background: #fff8e7; border-color: #ebcf8b; color: #664400; }\n.flash-stack { margin-bottom: 20px; display: grid; gap: 10px; }\n.flash { padding: 14px 16px; border-radius: 8px; border: 1px solid; }\n.flash-success { background: #ecf8f1; color: #116c3a; border-color: #b6dfc8; }\n.flash-error { background: #fff0ef; color: #9d1c15; border-color: #efbab6; }\n.invoice-items-wrap { margin-bottom: 20px; }\n.invoice-items { min-width: 920px; }\n.invoice-items input { min-width: 80px; padding: 8px 9px; }\n.invoice-items td:first-child input { min-width: 300px; }\n.invoice-items tfoot td { font-weight: 800; background: var(--primary-soft); }\n.total-label { text-align: right; }\n.line-total { white-space: nowrap; font-weight: 700; }\n.icon-button { width: 32px; height: 32px; border: 1px solid #d8dde3; border-radius: 7px; background: #fff; color: var(--danger); font-size: 20px; cursor: pointer; }\n.detail-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 24px; }\n.details { display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px 26px; margin: 0; }\n.details div { border-bottom: 1px solid var(--line); padding-bottom: 10px; }\n.details dt { color: var(--muted); font-size: 12px; margin-bottom: 4px; }\n.details dd { margin: 0; font-weight: 700; }\n.total-card { display: flex; flex-direction: column; justify-content: center; text-align: center; gap: 10px; min-height: 200px; }\n.total-card strong { font-size: 30px; color: var(--primary); }\n.total-card small { color: var(--muted); }\n.danger-zone { display: flex; justify-content: space-between; align-items: center; gap: 15px; margin-top: 30px; }\n@media (max-width: 850px) {\n    .topbar-inner { flex-wrap: wrap; padding-top: 12px; padding-bottom: 12px; gap: 10px; }\n    .nav { order: 3; width: 100%; overflow-x: auto; }\n    .page-header { align-items: flex-start; flex-direction: column; }\n    .stats-grid, .detail-grid { grid-template-columns: 1fr; }\n    .form-grid.two, .form-grid.three { grid-template-columns: 1fr; }\n    .span-2, .span-3 { grid-column: span 1; }\n    .details { grid-template-columns: 1fr; }\n}\n@media (max-width: 560px) {\n    .container { width: min(100% - 20px, 1180px); }\n    .panel { padding: 17px; }\n    .footer-inner { flex-direction: column; }\n    .danger-zone { align-items: stretch; flex-direction: column; }\n    .danger-zone form, .danger-zone button { width: 100%; }\n}\n\n\n.compact-form { max-width: 760px; padding: 0; box-shadow: none; border: 0; }\n.danger-panel { border: 1px solid #f3b4b4; }\n.danger-panel h2 { color: #9d1c1c; }\ncode { background: #f2f4f7; padding: 2px 6px; border-radius: 5px; }\n\n\n/* Moduły kosztów i DPH */\n.dashboard-stats { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }\n.compact-value { font-size: 22px; word-break: break-word; }\n.status-value { font-size: 21px; }\n.callout.info { background: #edf6ff; border-color: #b8d6ef; color: #244b68; }\n.badge-warning { background: #fff1e6; color: var(--warning); }\n.category-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }\n.category-card { border: 1px solid var(--line); border-radius: 9px; padding: 14px; display: flex; flex-direction: column; gap: 7px; }\n.category-card span { color: var(--muted); font-size: 13px; }\n.category-card strong { font-size: 18px; }\n.compact-form { max-width: 760px; padding: 0; box-shadow: none; border: 0; }\n.danger-panel { border: 1px solid #f3b4b4; }\n.danger-panel h2 { color: #9d1c1c; }\n.compact-list { margin: 8px 0 0; padding-left: 20px; }\n.disabled-link { opacity: .45; pointer-events: none; }\n.field small { color: var(--muted); font-size: 11px; line-height: 1.35; }\n.dph-hint { margin-top: 18px; margin-bottom: 0; }\ncode { background: #f2f4f7; padding: 2px 6px; border-radius: 5px; }\n'

EMBEDDED_JS = '(function () {\n    function parseNumber(value) {\n        const normalized = String(value || \'\').replace(/\\s/g, \'\').replace(\',\', \'.\');\n        const parsed = Number.parseFloat(normalized);\n        return Number.isFinite(parsed) ? parsed : 0;\n    }\n    function currency() {\n        const input = document.querySelector(\'input[name="currency"]\');\n        return input ? input.value.trim().toUpperCase() : \'\';\n    }\n    function recalculate() {\n        const mode = document.getElementById(\'tax-mode\');\n        const vatEnabled = mode && mode.value === \'vat\';\n        let total = 0;\n        document.querySelectorAll(\'.invoice-item-row\').forEach((row) => {\n            const qty = parseNumber(row.querySelector(\'.qty-input\')?.value);\n            const price = parseNumber(row.querySelector(\'.price-input\')?.value);\n            const vatRate = vatEnabled ? parseNumber(row.querySelector(\'.vat-input\')?.value) : 0;\n            const gross = qty * price * (1 + vatRate / 100);\n            total += gross;\n            const cell = row.querySelector(\'.line-total\');\n            if (cell) cell.textContent = `${gross.toFixed(2)} ${currency()}`;\n        });\n        const totalCell = document.getElementById(\'invoice-total\');\n        if (totalCell) totalCell.textContent = `${total.toFixed(2)} ${currency()}`;\n    }\n    function updateTaxMode() {\n        const mode = document.getElementById(\'tax-mode\');\n        if (!mode) return;\n        const vatEnabled = mode.value === \'vat\';\n        document.querySelectorAll(\'.vat-column\').forEach((el) => { el.style.display = vatEnabled ? \'\' : \'none\'; });\n        document.querySelectorAll(\'.vat-input\').forEach((input) => { input.disabled = !vatEnabled; if (!vatEnabled) input.value = \'0\'; });\n        const reverse = document.getElementById(\'reverse-charge-section\');\n        if (reverse) reverse.style.display = mode.value === \'reverse_charge\' ? \'\' : \'none\';\n        updateDphHint();\n        recalculate();\n    }\n    function addItem() {\n        const template = document.getElementById(\'invoice-item-template\');\n        const body = document.getElementById(\'invoice-items-body\');\n        if (!template || !body) return;\n        body.appendChild(template.content.cloneNode(true));\n        updateTaxMode();\n        body.querySelector(\'tr:last-child input[name="item_description"]\')?.focus();\n    }\n    function updateDphHint() {\n        const hint = document.getElementById(\'dph-hint\');\n        const contractor = document.getElementById(\'contractor-select\');\n        const taxMode = document.getElementById(\'tax-mode\');\n        const category = document.getElementById(\'dph-category\');\n        if (!hint || !contractor || !taxMode || !category) return;\n        const option = contractor.options[contractor.selectedIndex];\n        const vat = String(option?.dataset?.vat || \'\').replace(/[^A-Za-z0-9]/g, \'\').toUpperCase();\n        const prefix = vat.slice(0, 2);\n        const eu = [\'AT\',\'BE\',\'BG\',\'CY\',\'CZ\',\'DE\',\'DK\',\'EE\',\'EL\',\'ES\',\'FI\',\'FR\',\'HR\',\'HU\',\'IE\',\'IT\',\'LT\',\'LU\',\'LV\',\'MT\',\'NL\',\'PL\',\'PT\',\'RO\',\'SE\',\'SI\',\'SK\'];\n        if (category.value === \'eu_service\') {\n            hint.innerHTML = \'<strong>Ręcznie wybrano:</strong> usługa B2B UE - Souhrnné hlášení, kod 3.\';\n        } else if (category.value === \'not_required\') {\n            hint.innerHTML = \'<strong>Ręcznie wybrano:</strong> nie ujmuj w Souhrnné hlášení.\';\n        } else if (category.value === \'review\') {\n            hint.innerHTML = \'<strong>Ręcznie wybrano:</strong> faktura wymaga sprawdzenia.\';\n        } else if (taxMode.value === \'reverse_charge\' && eu.includes(prefix) && prefix !== \'CZ\') {\n            hint.innerHTML = `<strong>Automatycznie:</strong> VAT ID ${vat} wygląda na unijny. Faktura trafi do Souhrnné hlášení z kodem 3.`;\n        } else if (taxMode.value === \'reverse_charge\') {\n            hint.innerHTML = \'<strong>Uwaga:</strong> reverse charge, ale nie rozpoznano VAT ID z innego państwa UE. Po wystawieniu pojawi się ostrzeżenie.\';\n        } else {\n            hint.innerHTML = \'Ta faktura nie będzie automatycznie ujęta w Souhrnné hlášení.\';\n        }\n    }\n    function syncInvoiceRate() {\n        const currencyInput = document.getElementById(\'invoice-currency\');\n        const rate = document.getElementById(\'czk-rate\');\n        if (!currencyInput || !rate) return;\n        if (currencyInput.value.trim().toUpperCase() === \'CZK\' && !rate.value) rate.value = \'1\';\n    }\n    function updateExpensePreview() {\n        const amount = document.getElementById(\'expense-amount\');\n        const currencyInput = document.getElementById(\'expense-currency\');\n        const rate = document.getElementById(\'expense-rate\');\n        const preview = document.getElementById(\'expense-czk-preview\');\n        if (!amount || !currencyInput || !rate || !preview) return;\n        if (currencyInput.value.trim().toUpperCase() === \'CZK\' && !rate.value) rate.value = \'1\';\n        const value = parseNumber(amount.value) * parseNumber(rate.value);\n        preview.textContent = `${value.toFixed(2).replace(\'.\', \',\')} CZK`;\n    }\n    document.addEventListener(\'click\', (event) => {\n        const target = event.target;\n        if (!(target instanceof Element)) return;\n        if (target.id === \'add-item\') addItem();\n        if (target.classList.contains(\'remove-item\')) {\n            const rows = document.querySelectorAll(\'.invoice-item-row\');\n            if (rows.length <= 1) { alert(\'Faktura musi zawierać co najmniej jedną pozycję.\'); return; }\n            target.closest(\'.invoice-item-row\')?.remove(); recalculate();\n        }\n    });\n    document.addEventListener(\'input\', (event) => {\n        const target = event.target;\n        if (!(target instanceof Element)) return;\n        if (target.matches(\'.qty-input, .price-input, .vat-input, input[name="currency"]\')) recalculate();\n        if (target.matches(\'#invoice-currency\')) { syncInvoiceRate(); updateDphHint(); }\n        if (target.matches(\'#expense-amount, #expense-currency, #expense-rate\')) updateExpensePreview();\n    });\n    document.getElementById(\'tax-mode\')?.addEventListener(\'change\', updateTaxMode);\n    document.getElementById(\'contractor-select\')?.addEventListener(\'change\', updateDphHint);\n    document.getElementById(\'dph-category\')?.addEventListener(\'change\', updateDphHint);\n    const form = document.querySelector(\'.invoice-form\');\n    const issueDate = document.getElementById(\'issue-date\');\n    const dueDate = document.getElementById(\'due-date\');\n    if (form && issueDate && dueDate) {\n        let dueManuallyChanged = false;\n        dueDate.addEventListener(\'change\', () => { dueManuallyChanged = true; });\n        issueDate.addEventListener(\'change\', () => {\n            if (dueManuallyChanged || !issueDate.value) return;\n            const days = Number.parseInt(form.dataset.defaultDueDays || \'14\', 10);\n            const parsed = new Date(`${issueDate.value}T12:00:00`); parsed.setDate(parsed.getDate() + days);\n            dueDate.value = parsed.toISOString().slice(0, 10);\n        });\n    }\n    syncInvoiceRate(); updateTaxMode(); updateDphHint(); updateExpensePreview(); recalculate();\n})();\n'

# V4 template overrides.
EMBEDDED_TEMPLATES['dashboard.html'] = '{% extends "base.html" %}\n{% block title %}Start - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header">\n    <div>\n        <h1>Panel działalności</h1>\n        <p class="muted">{{ company.name }} · IČO {{ company.company_id }} · DIČ {{ company.vat_id }}</p>\n    </div>\n    <a class="btn btn-primary" href="{{ url_for(\'invoice_new\') }}">Wystaw fakturę</a>\n</div>\n\n{% if dph_reminder %}\n<div class="callout warning">\n    <strong>DPH / UE:</strong> masz {{ dph_reminder.invoice_count }} fakturę/faktury do sprawdzenia za {{ dph_reminder.period_label }}.\n    Podstawowy termin: <strong>{{ dph_reminder.due_date|date_pl }}</strong>.\n    <a href="{{ url_for(\'dph_dashboard\', year=dph_reminder.year, month=dph_reminder.month) }}">Otwórz asystenta DPH</a>.\n</div>\n{% endif %}\n\n<div class="callout info">\n    <strong>Podatki i składki {{ tax_snapshot.year }}:</strong>\n    na podstawie zaksięgowanych faktur program szacuje, że należy odłożyć jeszcze\n    <strong>{{ tax_snapshot.remaining_total|money(\'CZK\') }}</strong>.\n    <a href="{{ url_for(\'taxes_dashboard\', year=tax_snapshot.year) }}">Zobacz wyliczenie i ustawienia</a>.\n</div>\n\n<div class="stats-grid dashboard-stats">\n    <div class="stat-card"><span class="stat-value compact-value">{{ tax_snapshot.revenue|money(\'CZK\') }}</span><span class="stat-label">Przychód podatkowy {{ tax_snapshot.year }}</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ tax_snapshot.total_obligation|money(\'CZK\') }}</span><span class="stat-label">Szacowany podatek + ČSSZ + VZP</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ tax_snapshot.after_obligations|money(\'CZK\') }}</span><span class="stat-label">Po pełnej rezerwie</span></div>\n    <div class="stat-card"><span class="stat-value">{{ counts.unpaid }}</span><span class="stat-label">Nieopłacone faktury</span></div>\n    <div class="stat-card"><span class="stat-value">{{ counts.invoices }}</span><span class="stat-label">Wszystkie faktury</span></div>\n    <div class="stat-card"><span class="stat-value">{{ counts.contractors }}</span><span class="stat-label">Kontrahenci</span></div>\n</div>\n\n<section class="panel">\n    <div class="panel-header"><h2>Ostatnie faktury</h2><a href="{{ url_for(\'invoices_list\') }}">Pokaż wszystkie</a></div>\n    {% if recent %}\n    <div class="table-wrap"><table>\n        <thead><tr><th>Numer</th><th>Kontrahent</th><th>Data</th><th>Kwota</th><th>Status</th><th></th></tr></thead>\n        <tbody>{% for invoice in recent %}<tr>\n            <td><a href="{{ url_for(\'invoice_view\', invoice_id=invoice.id) }}"><strong>{{ invoice.invoice_number }}</strong></a></td>\n            <td>{{ invoice.contractor_name }}</td><td>{{ invoice.issue_date|date_pl }}</td>\n            <td>{{ invoice.total|money(invoice.currency) }}</td>\n            <td><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == \'paid\' else \'Nieopłacona\' }}</span></td>\n            <td class="actions"><a class="btn btn-light btn-small" href="{{ url_for(\'invoice_pdf\', invoice_id=invoice.id) }}">PDF</a></td>\n        </tr>{% endfor %}</tbody>\n    </table></div>\n    {% else %}<div class="empty-state"><h3>Nie masz jeszcze faktur</h3><p>Dodaj kontrahenta, a następnie wystaw pierwszą fakturę.</p></div>{% endif %}\n</section>\n{% endblock %}\n'
EMBEDDED_TEMPLATES['taxes_dashboard.html'] = '{% extends "base.html" %}\n{% block title %}Podatki i składki - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header">\n    <div><h1>Podatki i składki</h1><p class="muted">Bieżąca rezerwa na podatek dochodowy, ČSSZ i VZP.</p></div>\n    <form class="toolbar year-toolbar" method="get"><input type="number" name="year" min="2020" max="2100" value="{{ snapshot.year }}"><button class="btn btn-light" type="submit">Pokaż rok</button></form>\n</div>\n\n<div class="callout warning"><strong>Wyliczenie orientacyjne.</strong> Program liczy rezerwę z danych faktur i ustawień poniżej. Ostateczne kwoty wynikają z rocznego zeznania podatkowego oraz Přehledów dla ČSSZ i VZP.</div>\n{% if snapshot.year == 2026 %}\n<div class="callout info"><strong>Ustawiony przebieg 2026:</strong> działalność od 08.07.2026; ČSSZ i VZP bez minimum w lipcu z powodu zatrudnienia, a od 01.08.2026 działalność główna. Kalkulator uwzględnia {{ snapshot.cssz_secondary_months }} mies. vedlejší i {{ snapshot.cssz_main_months }} mies. hlavní.</div>\n{% endif %}\n{% if snapshot.warnings %}<div class="callout warning"><strong>Sprawdź:</strong><ul class="compact-list">{% for warning in snapshot.warnings %}<li>{{ warning }}</li>{% endfor %}</ul></div>{% endif %}\n\n<div class="stats-grid dashboard-stats tax-stats">\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.revenue|money(\'CZK\') }}</span><span class="stat-label">Przychód przyjęty do obliczeń</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.flat_expenses|money(\'CZK\') }}</span><span class="stat-label">Paušální výdaje {{ snapshot.settings.expense_percent }}%</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.profit|money(\'CZK\') }}</span><span class="stat-label">Szacowany zysk / dílčí základ</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.income_tax|money(\'CZK\') }}</span><span class="stat-label">Podatek dochodowy do dopłaty</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.cssz|money(\'CZK\') }}</span><span class="stat-label">Szacowane ČSSZ za rok</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.vzp|money(\'CZK\') }}</span><span class="stat-label">Szacowane VZP za rok</span></div>\n    <div class="stat-card emphasis"><span class="stat-value compact-value">{{ snapshot.remaining_total|money(\'CZK\') }}</span><span class="stat-label">Do odłożenia teraz</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.after_obligations|money(\'CZK\') }}</span><span class="stat-label">Przychód po pełnej rezerwie</span></div>\n</div>\n\n<section class="panel">\n    <div class="panel-header"><h2>Rozliczenie rezerwy {{ snapshot.year }}</h2><span class="badge badge-warning">{{ snapshot.active_months }} mies. działalności</span></div>\n    <div class="table-wrap"><table>\n        <thead><tr><th>Rodzaj</th><th>Szacowana należność</th><th>Zapisane wpłaty</th><th>Pozostało</th></tr></thead>\n        <tbody>\n            <tr><td><strong>Podatek dochodowy</strong></td><td>{{ snapshot.income_tax|money(\'CZK\') }}</td><td>{{ snapshot.paid.income_tax|money(\'CZK\') }}</td><td><strong>{{ snapshot.remaining.income_tax|money(\'CZK\') }}</strong></td></tr>\n            <tr><td><strong>ČSSZ</strong></td><td>{{ snapshot.cssz|money(\'CZK\') }}</td><td>{{ snapshot.paid.cssz|money(\'CZK\') }}</td><td><strong>{{ snapshot.remaining.cssz|money(\'CZK\') }}</strong></td></tr>\n            <tr><td><strong>VZP</strong></td><td>{{ snapshot.vzp|money(\'CZK\') }}</td><td>{{ snapshot.paid.vzp|money(\'CZK\') }}</td><td><strong>{{ snapshot.remaining.vzp|money(\'CZK\') }}</strong></td></tr>\n        </tbody>\n        <tfoot><tr><th>Razem</th><th>{{ snapshot.total_obligation|money(\'CZK\') }}</th><th>{{ snapshot.total_paid|money(\'CZK\') }}</th><th>{{ snapshot.remaining_total|money(\'CZK\') }}</th></tr></tfoot>\n    </table></div>\n    <div class="callout info"><strong>Bieżące zaliczki od 01.08.2026:</strong> ČSSZ {{ snapshot.settings.cssz_monthly_advance|money(\'CZK\') }} za dany miesiąc do jego ostatniego dnia; VZP {{ snapshot.settings.vzp_monthly_advance|money(\'CZK\') }} za dany miesiąc do 8. dnia następnego miesiąca. Każdą zapłatę zapisz niżej.</div>\n</section>\n\n<section class="panel">\n    <div class="panel-header"><h2>Faktury uwzględnione w przychodzie</h2><span class="muted">Podstawa: {{ \'zapłacone faktury\' if snapshot.settings.revenue_basis == \'paid\' else \'wystawione faktury\' }}</span></div>\n    {% if snapshot.invoice_rows %}<div class="table-wrap"><table>\n        <thead><tr><th>Faktura</th><th>Kontrahent</th><th>Status</th><th>Data przychodu</th><th>Kwota CZK</th></tr></thead>\n        <tbody>{% for row in snapshot.invoice_rows %}<tr>\n            <td><a href="{{ url_for(\'invoice_view\', invoice_id=row.id) }}"><strong>{{ row.invoice_number }}</strong></a></td><td>{{ row.contractor_name }}</td>\n            <td><span class="badge badge-{{ row.status }}">{{ \'Opłacona\' if row.status == \'paid\' else \'Nieopłacona\' }}</span></td>\n            <td>{{ row.recognized_date|date_pl }}</td><td>{{ row.value_czk|money(\'CZK\') }}</td>\n        </tr>{% endfor %}</tbody>\n    </table></div>{% else %}<div class="empty-state"><h3>Brak przychodu w tym roku</h3><p>Przy ustawieniu „zapłacone” faktura pojawi się po oznaczeniu jej jako opłaconej.</p></div>{% endif %}\n</section>\n\n<section class="panel">\n    <div class="panel-header"><h2>Zapisane wpłaty do urzędów</h2></div>\n    <form method="post" action="{{ url_for(\'tax_payment_add\') }}" class="form-grid four tax-payment-form">\n        <input type="hidden" name="year" value="{{ snapshot.year }}">\n        <label class="field"><span>Data wpłaty</span><input type="date" name="payment_date" value="{{ today }}" required></label>\n        <label class="field"><span>Rodzaj</span><select name="payment_type">{% for code, label in TAX_PAYMENT_TYPES.items() %}<option value="{{ code }}">{{ label }}</option>{% endfor %}</select></label>\n        <label class="field"><span>Kwota CZK</span><input type="number" step="0.01" min="0.01" name="amount" required></label>\n        <label class="field"><span>Notatka</span><input name="note" placeholder="np. zaliczka za sierpień"></label>\n        <div class="span-4 form-actions compact-actions"><button class="btn btn-primary" type="submit">Dodaj wpłatę</button></div>\n    </form>\n    {% if payments %}<div class="table-wrap"><table><thead><tr><th>Data</th><th>Rodzaj</th><th>Kwota</th><th>Notatka</th><th></th></tr></thead><tbody>\n    {% for payment in payments %}<tr><td>{{ payment.payment_date|date_pl }}</td><td>{{ TAX_PAYMENT_TYPES[payment.payment_type] }}</td><td>{{ payment.amount|money(\'CZK\') }}</td><td>{{ payment.note or \'—\' }}</td><td class="actions"><form method="post" action="{{ url_for(\'tax_payment_delete\', payment_id=payment.id) }}" onsubmit="return confirm(\'Usunąć tę wpłatę?\')"><input type="hidden" name="year" value="{{ snapshot.year }}"><button class="btn btn-light btn-small" type="submit">Usuń</button></form></td></tr>{% endfor %}\n    </tbody></table></div>{% else %}<p class="muted">Nie zapisano jeszcze żadnych wpłat.</p>{% endif %}\n</section>\n\n<details class="panel settings-panel" open>\n    <summary><strong>Ustawienia obliczeń {{ snapshot.year }}</strong></summary>\n    <form method="post" action="{{ url_for(\'tax_settings_save\') }}" class="form-panel tax-settings-form">\n        <input type="hidden" name="year" value="{{ snapshot.year }}">\n        <h3>Przychód i podatek</h3>\n        <div class="form-grid three">\n            <label class="field"><span>Początek działalności</span><input type="date" name="activity_start_date" value="{{ snapshot.settings.activity_start_date }}"></label>\n            <label class="field"><span>Koniec działalności (opcjonalnie)</span><input type="date" name="activity_end_date" value="{{ snapshot.settings.activity_end_date }}"></label>\n            <label class="field"><span>Do przychodu licz</span><select name="revenue_basis"><option value="paid" {% if snapshot.settings.revenue_basis == \'paid\' %}selected{% endif %}>Zapłacone faktury</option><option value="issued" {% if snapshot.settings.revenue_basis == \'issued\' %}selected{% endif %}>Wystawione faktury</option></select></label>\n            <label class="field"><span>Paušální výdaje (%)</span><input type="number" step="0.01" min="0" max="100" name="expense_percent" value="{{ snapshot.settings.expense_percent }}"></label>\n            <label class="field"><span>Maksymalny paušál CZK</span><input type="number" step="1" min="0" name="expense_limit" value="{{ snapshot.settings.expense_limit }}"></label>\n            <label class="field"><span>Roczna podstawowa ulga podatkowa</span><input type="number" step="1" min="0" name="income_tax_credit" value="{{ snapshot.settings.income_tax_credit }}"><small>Dla 2026 domyślnie 30 840 CZK.</small></label>\n            <label class="field"><span>Podstawa podatku z Accenture</span><input type="number" step="1" min="0" name="employment_tax_base" value="{{ snapshot.settings.employment_tax_base }}"><small>Przepisz z „Potvrzení o zdanitelných příjmech”.</small></label>\n            <label class="field"><span>Zaliczki podatku pobrane przez Accenture</span><input type="number" step="1" min="0" name="employment_tax_withheld" value="{{ snapshot.settings.employment_tax_withheld }}"><small>Także z „Potvrzení o zdanitelných příjmech”.</small></label>\n            <label class="field"><span>Próg 23% w CZK</span><input type="number" step="1" min="0" name="tax_threshold" value="{{ snapshot.settings.tax_threshold }}"></label>\n        </div>\n        <hr><h3>ČSSZ</h3>\n        <div class="form-grid three">\n            <label class="field"><span>Tryb działalności</span><select name="cssz_mode"><option value="secondary" {% if snapshot.settings.cssz_mode == \'secondary\' %}selected{% endif %}>Tylko vedlejší</option><option value="mixed" {% if snapshot.settings.cssz_mode == \'mixed\' %}selected{% endif %}>Zmiana vedlejší → hlavní w roku</option><option value="main" {% if snapshot.settings.cssz_mode == \'main\' %}selected{% endif %}>Tylko hlavní</option><option value="custom" {% if snapshot.settings.cssz_mode == \'custom\' %}selected{% endif %}>Własne ustawienia bez minimum</option></select></label>\n            <label class="field"><span>Hlavní od dnia</span><input type="date" name="cssz_main_from_date" value="{{ snapshot.settings.cssz_main_from_date }}"><small>U Ciebie 01.08.2026.</small></label>\n            <label class="field"><span>Rozhodná částka roczna</span><input type="number" step="1" name="cssz_threshold_annual" value="{{ snapshot.settings.cssz_threshold_annual }}"></label>\n            <label class="field"><span>Pomniejszenie za miesiąc bez vedlejší</span><input type="number" step="1" name="cssz_threshold_reduction_month" value="{{ snapshot.settings.cssz_threshold_reduction_month }}"></label>\n            <label class="field"><span>Podstawa z zysku (%)</span><input type="number" step="0.01" name="cssz_assessment_percent" value="{{ snapshot.settings.cssz_assessment_percent }}"></label>\n            <label class="field"><span>Stawka ČSSZ (%)</span><input type="number" step="0.01" name="cssz_rate_percent" value="{{ snapshot.settings.cssz_rate_percent }}"></label>\n            <label class="field"><span>Min. podstawa miesięczna hlavní</span><input type="number" step="0.01" name="cssz_min_monthly_base" value="{{ snapshot.settings.cssz_min_monthly_base }}"><small>17 139 CZK od lipca 2026.</small></label>\n            <label class="field"><span>Min. podstawa miesięczna vedlejší</span><input type="number" step="0.01" name="cssz_secondary_min_monthly_base" value="{{ snapshot.settings.cssz_secondary_min_monthly_base }}"></label>\n            <label class="field"><span>Aktualna zaliczka miesięczna ČSSZ</span><input type="number" step="0.01" min="0" name="cssz_monthly_advance" value="{{ snapshot.settings.cssz_monthly_advance }}"></label>\n        </div>\n        <hr><h3>VZP</h3>\n        <div class="form-grid three">\n            <label class="field"><span>Tryb ubezpieczenia</span><select name="vzp_mode"><option value="secondary" {% if snapshot.settings.vzp_mode == \'secondary\' %}selected{% endif %}>Tylko obok zatrudnienia / bez minimum</option><option value="mixed" {% if snapshot.settings.vzp_mode == \'mixed\' %}selected{% endif %}>Zmiana na główną OSVČ w roku</option><option value="main" {% if snapshot.settings.vzp_mode == \'main\' %}selected{% endif %}>Główna OSVČ przez cały okres</option><option value="custom" {% if snapshot.settings.vzp_mode == \'custom\' %}selected{% endif %}>Własne ustawienia bez minimum</option></select></label>\n            <label class="field"><span>Minimum VZP od dnia</span><input type="date" name="vzp_main_from_date" value="{{ snapshot.settings.vzp_main_from_date }}"><small>U Ciebie 01.08.2026.</small></label>\n            <label class="field"><span>Podstawa z zysku (%)</span><input type="number" step="0.01" name="vzp_assessment_percent" value="{{ snapshot.settings.vzp_assessment_percent }}"></label>\n            <label class="field"><span>Stawka VZP (%)</span><input type="number" step="0.01" name="vzp_rate_percent" value="{{ snapshot.settings.vzp_rate_percent }}"></label>\n            <label class="field"><span>Minimalna podstawa miesięczna</span><input type="number" step="0.01" name="vzp_min_monthly_base" value="{{ snapshot.settings.vzp_min_monthly_base }}"></label>\n            <label class="field"><span>Aktualna zaliczka miesięczna VZP</span><input type="number" step="0.01" min="0" name="vzp_monthly_advance" value="{{ snapshot.settings.vzp_monthly_advance }}"></label>\n        </div>\n        <div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz ustawienia i przelicz</button></div>\n    </form>\n</details>\n{% endblock %}\n'
EMBEDDED_TEMPLATES['invoice_detail.html'] = '{% extends "base.html" %}\n{% block title %}{{ invoice.invoice_number }} - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header"><div><h1>{{ invoice.invoice_number }}</h1><p class="muted">{{ invoice.contractor_name }} · {{ invoice.issue_date|date_pl }}</p></div><div class="button-row"><a class="btn btn-primary" href="{{ url_for(\'invoice_pdf\', invoice_id=invoice.id) }}">Pobierz PDF A4</a><a class="btn btn-light" href="{{ url_for(\'invoice_edit\', invoice_id=invoice.id) }}">Edytuj</a></div></div>\n{% if dph_info.kind == \'eu_service\' %}<div class="callout info"><strong>DPH / UE:</strong> ta faktura zostanie ujęta w Souhrnné hlášení za {{ dph_info.period_label }} z kodem plnění 3. Podstawowy termin: {{ dph_info.due_date|date_pl }}. <a href="{{ url_for(\'dph_dashboard\', year=invoice.supply_date[:4], month=invoice.supply_date[5:7]) }}">Otwórz raport</a>.</div>{% elif dph_info.warning %}<div class="callout warning"><strong>DPH do sprawdzenia:</strong> {{ dph_info.warning }}</div>{% endif %}\n<div class="detail-grid">\n<section class="panel"><div class="panel-header"><h2>Dane faktury</h2><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == \'paid\' else \'Nieopłacona\' }}</span></div><dl class="details">\n<div><dt>Kontrahent</dt><dd>{{ invoice.contractor_name }}</dd></div><div><dt>VAT ID</dt><dd>{{ invoice.contractor_vat_id or \'—\' }}</dd></div>\n<div><dt>Data wystawienia</dt><dd>{{ invoice.issue_date|date_pl }}</dd></div><div><dt>Data usługi</dt><dd>{{ invoice.supply_date|date_pl }}</dd></div>\n<div><dt>Termin płatności</dt><dd>{{ invoice.due_date|date_pl }}</dd></div><div><dt>Data zapłaty</dt><dd>{{ invoice.paid_date|date_pl if invoice.paid_date else \'—\' }}</dd></div>\n<div><dt>Tryb VAT</dt><dd>{{ TAX_MODES[invoice.tax_mode] }}</dd></div><div><dt>Kurs do CZK</dt><dd>{{ invoice.czk_rate or (\'1\' if invoice.currency == \'CZK\' else \'—\') }}</dd></div>\n<div><dt>DPH / VIES</dt><dd>{{ dph_info.label }}</dd></div></dl></section>\n<section class="panel total-card"><span class="muted">Do zapłaty</span><strong>{{ totals.total_gross|money(invoice.currency) }}</strong>{% if dph_info.value_czk is not none %}<small>Do ewidencji: {{ dph_info.value_czk|money(\'CZK\') }}</small>{% endif %}</section>\n</div>\n<section class="panel"><h2>Pozycje</h2><div class="table-wrap"><table><thead><tr><th>Opis</th><th>Ilość</th><th>Jedn.</th><th>Cena</th>{% if invoice.tax_mode == \'vat\' %}<th>VAT</th>{% endif %}<th>Wartość</th></tr></thead><tbody>{% for item in items %}<tr><td>{{ item.description }}</td><td>{{ item.quantity_decimal }}</td><td>{{ item.unit }}</td><td>{{ item.unit_price_decimal|money(invoice.currency) }}</td>{% if invoice.tax_mode == \'vat\' %}<td>{{ item.vat_rate_decimal }}%</td>{% endif %}<td>{{ item.gross|money(invoice.currency) }}</td></tr>{% endfor %}</tbody></table></div></section>\n{% if invoice.reverse_charge_note or invoice.notes %}<section class="panel">{% if invoice.reverse_charge_note %}<h2>Adnotacja</h2><p>{{ invoice.reverse_charge_note }}</p>{% endif %}{% if invoice.notes %}<h2>Uwagi</h2><p class="preline">{{ invoice.notes }}</p>{% endif %}</section>{% endif %}\n<div class="danger-zone paid-actions">\n    {% if invoice.status == \'paid\' %}\n    <form method="post" action="{{ url_for(\'invoice_toggle_paid\', invoice_id=invoice.id) }}"><button class="btn btn-light" type="submit">Oznacz jako nieopłaconą</button></form>\n    {% else %}\n    <form method="post" action="{{ url_for(\'invoice_toggle_paid\', invoice_id=invoice.id) }}" class="paid-date-form"><label class="field"><span>Data otrzymania zapłaty</span><input type="date" name="paid_date" value="{{ today }}" required></label><button class="btn btn-primary" type="submit">Oznacz jako opłaconą</button></form>\n    {% endif %}\n    <form method="post" action="{{ url_for(\'invoice_delete\', invoice_id=invoice.id) }}" onsubmit="return confirm(\'Usunąć fakturę {{ invoice.invoice_number }}?\')"><button class="btn btn-danger" type="submit">Usuń fakturę</button></form>\n</div>\n{% endblock %}\n'
EMBEDDED_CSS += '\n/* V4: podatek, ČSSZ i VZP */\n.tax-stats .emphasis { border-color: #d9b263; background: #fffaf0; }\n.form-grid.four { grid-template-columns: repeat(4, 1fr); }\n.span-4 { grid-column: span 4; }\n.year-toolbar { margin: 0; }\n.year-toolbar input { min-width: 120px; width: 120px; }\n.settings-panel summary { cursor: pointer; font-size: 18px; margin-bottom: 20px; }\n.tax-settings-form { margin-top: 20px; }\n.tax-payment-form { align-items: end; }\n.compact-actions { margin-top: 0; padding-top: 0; border-top: 0; }\n.paid-date-form { display: flex; align-items: end; gap: 12px; }\n.paid-date-form .field { min-width: 220px; }\n.paid-actions { align-items: end; }\ntfoot th { background: var(--primary-soft); }\n@media (max-width: 850px) {\n    .form-grid.four { grid-template-columns: 1fr; }\n    .span-4 { grid-column: span 1; }\n    .paid-date-form { width: 100%; flex-direction: column; align-items: stretch; }\n    .paid-date-form .field { min-width: 0; }\n}\n'

EMBEDDED_TEMPLATES['base.html'] = (
    EMBEDDED_TEMPLATES['base.html']
    .replace("<a href=\"{{ url_for('expenses_page') }}\">Koszty</a>", "<a href=\"{{ url_for('taxes_dashboard') }}\">Podatki i składki</a>")
    .replace("kontrahentów, faktur ani kosztów.", "kontrahentów, faktur ani ustawień podatków i składek.")
)
EMBEDDED_TEMPLATES['tools.html'] = (
    EMBEDDED_TEMPLATES['tools.html']
    .replace("Kontrahenci, koszty i dane firmy pozostaną.", "Kontrahenci, ustawienia podatków i składek oraz dane firmy pozostaną.")
)



# ---------------------------------------------------------------------------
# V6: osobna sekcja Ustawienia + czytelne rozliczenie podatków i składek
# ---------------------------------------------------------------------------

EMBEDDED_TEMPLATES["taxes_dashboard.html"] = r"""{% extends "base.html" %}
{% block title %}Podatki i składki - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header">
    <div>
        <h1>Podatki i składki</h1>
        <p class="muted">Wyliczone zobowiązania, zapisane wpłaty i bieżące minima.</p>
    </div>
    <div class="button-row">
        <form class="toolbar year-toolbar" method="get">
            <input type="number" name="year" min="2020" max="2100" value="{{ snapshot.year }}">
            <button class="btn btn-light" type="submit">Pokaż rok</button>
        </form>
        <a class="btn btn-light" href="{{ url_for('settings_page', year=snapshot.year) }}">Ustawienia obliczeń</a>
    </div>
</div>

{% if snapshot.warnings %}
<div class="callout warning">
    <strong>Sprawdź:</strong>
    <ul class="compact-list">{% for warning in snapshot.warnings %}<li>{{ warning }}</li>{% endfor %}</ul>
</div>
{% endif %}

<div class="stats-grid dashboard-stats tax-stats">
    <div class="stat-card {% if snapshot.income_tax_overpayment > 0 %}tax-good{% elif snapshot.income_tax_underpayment > 0 %}tax-warning{% endif %}">
        <span class="stat-value compact-value">
            {% if snapshot.income_tax_overpayment > 0 %}
                {{ snapshot.income_tax_overpayment|money('CZK') }}
            {% else %}
                {{ snapshot.income_tax_underpayment|money('CZK') }}
            {% endif %}
        </span>
        <span class="stat-label">
            {% if snapshot.income_tax_overpayment > 0 %}Szacowana nadpłata podatku
            {% elif snapshot.income_tax_underpayment > 0 %}Szacowany podatek do dopłaty
            {% else %}Podatek rozliczony na zero{% endif %}
        </span>
    </div>
    <div class="stat-card">
        <span class="stat-value compact-value">{{ snapshot.remaining.cssz|money('CZK') }}</span>
        <span class="stat-label">ČSSZ pozostało wg szacunku</span>
    </div>
    <div class="stat-card">
        <span class="stat-value compact-value">{{ snapshot.remaining.vzp|money('CZK') }}</span>
        <span class="stat-label">VZP pozostało wg szacunku</span>
    </div>
    <div class="stat-card emphasis">
        <span class="stat-value compact-value">{{ snapshot.remaining_total|money('CZK') }}</span>
        <span class="stat-label">Łącznie do odłożenia / dopłaty</span>
    </div>
</div>

<section class="panel">
    <div class="panel-header">
        <h2>Minimalne miesięczne zaliczki</h2>
        {% if snapshot.year == 2026 %}<span class="badge badge-warning">hlavní od 01.08.2026</span>{% endif %}
    </div>
    <div class="minimum-grid">
        <div class="minimum-card">
            <span class="minimum-name">ČSSZ</span>
            <strong>{{ snapshot.settings.cssz_monthly_advance|money('CZK') }}</strong>
            <span class="muted">minimalna / bieżąca zaliczka miesięczna</span>
            <small>Za dany miesiąc do jego ostatniego dnia.</small>
        </div>
        <div class="minimum-card">
            <span class="minimum-name">VZP</span>
            <strong>{{ snapshot.settings.vzp_monthly_advance|money('CZK') }}</strong>
            <span class="muted">minimalna / bieżąca zaliczka miesięczna</span>
            <small>Za dany miesiąc do 8. dnia następnego miesiąca.</small>
        </div>
    </div>
</section>

<section class="panel">
    <div class="panel-header"><h2>Rozliczenie {{ snapshot.year }}</h2><span class="badge badge-warning">{{ snapshot.active_months }} mies. działalności</span></div>
    <div class="table-wrap"><table>
        <thead><tr><th>Rodzaj</th><th>Szacowana należność roczna</th><th>Zapłacono / pobrano</th><th>Bilans</th></tr></thead>
        <tbody>
            <tr>
                <td><strong>Podatek dochodowy</strong></td>
                <td>{{ snapshot.annual_tax_after_credit|money('CZK') }}</td>
                <td>{{ snapshot.income_tax_paid_total|money('CZK') }}<br><small class="muted">w tym Accenture {{ snapshot.employment_tax_withheld|money('CZK') }}</small></td>
                <td>
                    {% if snapshot.income_tax_overpayment > 0 %}
                        <strong class="text-success">Nadpłata {{ snapshot.income_tax_overpayment|money('CZK') }}</strong>
                    {% elif snapshot.income_tax_underpayment > 0 %}
                        <strong class="text-warning">Do dopłaty {{ snapshot.income_tax_underpayment|money('CZK') }}</strong>
                    {% else %}
                        <strong>0.00 CZK</strong>
                    {% endif %}
                </td>
            </tr>
            <tr>
                <td><strong>ČSSZ</strong></td>
                <td>{{ snapshot.cssz|money('CZK') }}</td>
                <td>{{ snapshot.paid.cssz|money('CZK') }}</td>
                <td><strong>{{ snapshot.remaining.cssz|money('CZK') }}</strong></td>
            </tr>
            <tr>
                <td><strong>VZP</strong></td>
                <td>{{ snapshot.vzp|money('CZK') }}</td>
                <td>{{ snapshot.paid.vzp|money('CZK') }}</td>
                <td><strong>{{ snapshot.remaining.vzp|money('CZK') }}</strong></td>
            </tr>
        </tbody>
    </table></div>
    <div class="callout info">
        <strong>Podstawa bieżącego wyliczenia:</strong>
        przychód {{ snapshot.revenue|money('CZK') }},
        paušální výdaje {{ snapshot.flat_expenses|money('CZK') }},
        szacowany zysk z OSVČ {{ snapshot.profit|money('CZK') }}.
        Dane wejściowe zmienisz w zakładce <a href="{{ url_for('settings_page', year=snapshot.year) }}">Ustawienia</a>.
    </div>
</section>

<section class="panel">
    <div class="panel-header"><h2>Zapisane wpłaty do urzędów</h2></div>
    <form method="post" action="{{ url_for('tax_payment_add') }}" class="form-grid four tax-payment-form">
        <input type="hidden" name="year" value="{{ snapshot.year }}">
        <label class="field"><span>Data wpłaty</span><input type="date" name="payment_date" value="{{ today }}" required></label>
        <label class="field"><span>Rodzaj</span><select name="payment_type">{% for code, label in TAX_PAYMENT_TYPES.items() %}<option value="{{ code }}">{{ label }}</option>{% endfor %}</select></label>
        <label class="field"><span>Kwota CZK</span><input type="number" step="0.01" min="0.01" name="amount" required></label>
        <label class="field"><span>Notatka</span><input name="note" placeholder="np. zaliczka za sierpień"></label>
        <div class="span-4 form-actions compact-actions"><button class="btn btn-primary" type="submit">Dodaj wpłatę</button></div>
    </form>
    {% if payments %}
    <div class="table-wrap"><table>
        <thead><tr><th>Data</th><th>Rodzaj</th><th>Kwota</th><th>Notatka</th><th></th></tr></thead>
        <tbody>
        {% for payment in payments %}
        <tr>
            <td>{{ payment.payment_date|date_pl }}</td>
            <td>{{ TAX_PAYMENT_TYPES[payment.payment_type] }}</td>
            <td>{{ payment.amount|money('CZK') }}</td>
            <td>{{ payment.note or '—' }}</td>
            <td class="actions"><form method="post" action="{{ url_for('tax_payment_delete', payment_id=payment.id) }}" onsubmit="return confirm('Usunąć tę wpłatę?')"><input type="hidden" name="year" value="{{ snapshot.year }}"><button class="btn btn-light btn-small" type="submit">Usuń</button></form></td>
        </tr>
        {% endfor %}
        </tbody>
    </table></div>
    {% else %}<p class="muted">Nie zapisano jeszcze żadnych wpłat.</p>{% endif %}
</section>
{% endblock %}
"""

EMBEDDED_TEMPLATES["settings.html"] = r"""{% extends "base.html" %}
{% block title %}Ustawienia - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header">
    <div><h1>Ustawienia</h1><p class="muted">Dane podatkowe oraz parametry ČSSZ i VZP używane do obliczeń.</p></div>
    <form class="toolbar year-toolbar" method="get"><input type="number" name="year" min="2020" max="2100" value="{{ snapshot.year }}"><button class="btn btn-light" type="submit">Pokaż rok</button></form>
</div>
<div class="callout info">Zmiana tych wartości wpływa tylko na kalkulacje w aplikacji — nie wysyła żadnej zmiany do urzędu.</div>

<section class="panel">
<form method="post" action="{{ url_for('tax_settings_save') }}" class="form-panel tax-settings-form">
<input type="hidden" name="year" value="{{ snapshot.year }}">

<h2>Przychód i podatek</h2>
<div class="form-grid three">
<label class="field"><span>Początek działalności</span><input type="date" name="activity_start_date" value="{{ snapshot.settings.activity_start_date }}"></label>
<label class="field"><span>Koniec działalności (opcjonalnie)</span><input type="date" name="activity_end_date" value="{{ snapshot.settings.activity_end_date }}"></label>
<label class="field"><span>Do przychodu licz</span><select name="revenue_basis"><option value="paid" {% if snapshot.settings.revenue_basis == 'paid' %}selected{% endif %}>Zapłacone faktury / otrzymane płatności</option><option value="issued" {% if snapshot.settings.revenue_basis == 'issued' %}selected{% endif %}>Wystawione faktury</option></select></label>
<label class="field"><span>Paušální výdaje (%)</span><input type="number" step="0.01" min="0" max="100" name="expense_percent" value="{{ snapshot.settings.expense_percent }}"></label>
<label class="field"><span>Maksymalny paušál CZK</span><input type="number" step="1" min="0" name="expense_limit" value="{{ snapshot.settings.expense_limit }}"></label>
<label class="field"><span>Roczna podstawowa ulga podatkowa</span><input type="number" step="1" min="0" name="income_tax_credit" value="{{ snapshot.settings.income_tax_credit }}"></label>
<label class="field"><span>Podstawa podatku z Accenture</span><input type="number" step="1" min="0" name="employment_tax_base" value="{{ snapshot.settings.employment_tax_base }}"><small>Z „Potvrzení o zdanitelných příjmech”.</small></label>
<label class="field"><span>Zaliczki podatku pobrane przez Accenture</span><input type="number" step="1" min="0" name="employment_tax_withheld" value="{{ snapshot.settings.employment_tax_withheld }}"><small>Z tego samego dokumentu.</small></label>
<label class="field"><span>Próg 23% w CZK</span><input type="number" step="1" min="0" name="tax_threshold" value="{{ snapshot.settings.tax_threshold }}"></label>
</div>

<hr><h2>ČSSZ</h2>
<div class="form-grid three">
<label class="field"><span>Tryb działalności</span><select name="cssz_mode"><option value="secondary" {% if snapshot.settings.cssz_mode == 'secondary' %}selected{% endif %}>Tylko vedlejší</option><option value="mixed" {% if snapshot.settings.cssz_mode == 'mixed' %}selected{% endif %}>Zmiana vedlejší → hlavní w roku</option><option value="main" {% if snapshot.settings.cssz_mode == 'main' %}selected{% endif %}>Tylko hlavní</option><option value="custom" {% if snapshot.settings.cssz_mode == 'custom' %}selected{% endif %}>Własne ustawienia bez minimum</option></select></label>
<label class="field"><span>Hlavní od dnia</span><input type="date" name="cssz_main_from_date" value="{{ snapshot.settings.cssz_main_from_date }}"></label>
<label class="field"><span>Rozhodná částka roczna</span><input type="number" step="1" name="cssz_threshold_annual" value="{{ snapshot.settings.cssz_threshold_annual }}"></label>
<label class="field"><span>Pomniejszenie za miesiąc bez vedlejší</span><input type="number" step="1" name="cssz_threshold_reduction_month" value="{{ snapshot.settings.cssz_threshold_reduction_month }}"></label>
<label class="field"><span>Podstawa z zysku (%)</span><input type="number" step="0.01" name="cssz_assessment_percent" value="{{ snapshot.settings.cssz_assessment_percent }}"></label>
<label class="field"><span>Stawka ČSSZ (%)</span><input type="number" step="0.01" name="cssz_rate_percent" value="{{ snapshot.settings.cssz_rate_percent }}"></label>
<label class="field"><span>Min. podstawa miesięczna hlavní</span><input type="number" step="0.01" name="cssz_min_monthly_base" value="{{ snapshot.settings.cssz_min_monthly_base }}"></label>
<label class="field"><span>Min. podstawa miesięczna vedlejší</span><input type="number" step="0.01" name="cssz_secondary_min_monthly_base" value="{{ snapshot.settings.cssz_secondary_min_monthly_base }}"></label>
<label class="field"><span>Minimalna / bieżąca zaliczka miesięczna ČSSZ</span><input type="number" step="0.01" min="0" name="cssz_monthly_advance" value="{{ snapshot.settings.cssz_monthly_advance }}"></label>
</div>

<hr><h2>VZP</h2>
<div class="form-grid three">
<label class="field"><span>Tryb ubezpieczenia</span><select name="vzp_mode"><option value="secondary" {% if snapshot.settings.vzp_mode == 'secondary' %}selected{% endif %}>Tylko obok zatrudnienia / bez minimum</option><option value="mixed" {% if snapshot.settings.vzp_mode == 'mixed' %}selected{% endif %}>Zmiana na główną OSVČ w roku</option><option value="main" {% if snapshot.settings.vzp_mode == 'main' %}selected{% endif %}>Główna OSVČ przez cały okres</option><option value="custom" {% if snapshot.settings.vzp_mode == 'custom' %}selected{% endif %}>Własne ustawienia bez minimum</option></select></label>
<label class="field"><span>Minimum VZP od dnia</span><input type="date" name="vzp_main_from_date" value="{{ snapshot.settings.vzp_main_from_date }}"></label>
<label class="field"><span>Podstawa z zysku (%)</span><input type="number" step="0.01" name="vzp_assessment_percent" value="{{ snapshot.settings.vzp_assessment_percent }}"></label>
<label class="field"><span>Stawka VZP (%)</span><input type="number" step="0.01" name="vzp_rate_percent" value="{{ snapshot.settings.vzp_rate_percent }}"></label>
<label class="field"><span>Minimalna podstawa miesięczna</span><input type="number" step="0.01" name="vzp_min_monthly_base" value="{{ snapshot.settings.vzp_min_monthly_base }}"></label>
<label class="field"><span>Minimalna / bieżąca zaliczka miesięczna VZP</span><input type="number" step="0.01" min="0" name="vzp_monthly_advance" value="{{ snapshot.settings.vzp_monthly_advance }}"></label>
</div>

<div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz ustawienia</button><a class="btn btn-light" href="{{ url_for('taxes_dashboard', year=snapshot.year) }}">Wróć do wyliczeń</a></div>
</form>
</section>
{% endblock %}
"""

_tools_link = """<a href="{{ url_for('tools_page') }}">Narzędzia</a>"""
_settings_and_tools = """<a href="{{ url_for('settings_page') }}">Ustawienia</a>
            <a href="{{ url_for('tools_page') }}">Narzędzia</a>"""
if "url_for('settings_page')" not in EMBEDDED_TEMPLATES["base.html"]:
    EMBEDDED_TEMPLATES["base.html"] = EMBEDDED_TEMPLATES["base.html"].replace(_tools_link, _settings_and_tools)

EMBEDDED_TEMPLATES["dashboard.html"] = EMBEDDED_TEMPLATES["dashboard.html"].replace("Zobacz wyliczenie i ustawienia", "Zobacz wyliczenie")

EMBEDDED_CSS += r"""
/* V6 */
.minimum-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
.minimum-card { border:1px solid var(--border); border-radius:12px; padding:18px; display:flex; flex-direction:column; gap:5px; background:#fff; }
.minimum-card strong { font-size:25px; }
.minimum-name { font-weight:800; color:var(--primary); }
.tax-good { border-color:#9fd6b4 !important; background:#f2fbf5 !important; }
.tax-warning { border-color:#e8c47c !important; background:#fffaf0 !important; }
.text-success { color:#16843d; }
.text-warning { color:#a55a00; }
@media (max-width:760px) { .minimum-grid { grid-template-columns:1fr; } }
"""



# ---------------------------------------------------------------------------
# V7: szablony web/PWA, logowanie i projekty
# ---------------------------------------------------------------------------

EMBEDDED_TEMPLATES["login.html"] = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#8b1538">
<title>Logowanie - Faktury OSVČ</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f6f8;margin:0;min-height:100vh;display:grid;place-items:center;color:#19202a}
.card{width:min(92vw,420px);background:#fff;border:1px solid #e1e5ea;border-radius:18px;padding:28px;box-shadow:0 15px 40px rgba(0,0,0,.08)}
h1{margin:0 0 8px;color:#8b1538}.muted{color:#68717d}.field{display:flex;flex-direction:column;gap:7px;margin:20px 0}.field input{font-size:18px;padding:13px;border:1px solid #ccd2d9;border-radius:10px}
button{width:100%;border:0;border-radius:10px;padding:13px;background:#8b1538;color:#fff;font-weight:700;font-size:16px}
.flash{padding:10px 12px;border-radius:8px;background:#fff0f0;color:#9d1c1c;margin-bottom:14px}
</style>
</head>
<body>
<div class="card">
<h1>Faktury OSVČ</h1>
<p class="muted">Zaloguj się do swojej aplikacji.</p>
{% with messages = get_flashed_messages(with_categories=true) %}
{% for category,message in messages %}<div class="flash">{{ message }}</div>{% endfor %}
{% endwith %}
<form method="post">
<label class="field"><span>Hasło</span><input type="password" name="password" autocomplete="current-password" autofocus required></label>
<button type="submit">Zaloguj</button>
</form>
</div>
</body>
</html>"""

EMBEDDED_TEMPLATES["base.html"] = r"""<!doctype html>
<html lang="pl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta name="theme-color" content="#8b1538">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="Faktury OSVČ">
    <link rel="manifest" href="{{ url_for('manifest') }}">
    <link rel="icon" href="{{ url_for('app_icon') }}">
    <link rel="apple-touch-icon" href="{{ url_for('app_icon') }}">
    <title>{% block title %}Faktury OSVČ{% endblock %}</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
<header class="topbar">
    <div class="topbar-inner">
        <a class="brand" href="{{ url_for('dashboard') }}">Faktury OSVČ</a>
        <button class="mobile-menu-button" id="mobile-menu-button" type="button" aria-label="Menu">☰</button>
        <nav class="nav" id="main-nav">
            <a href="{{ url_for('dashboard') }}">Start</a>
            <a href="{{ url_for('invoices_list') }}">Faktury</a>
            <a href="{{ url_for('contractors_list') }}">Kontrahenci</a>
            <a href="{{ url_for('projects_list') }}">Projekty</a>
            <a href="{{ url_for('taxes_dashboard') }}">Podatki i składki</a>
            <a href="{{ url_for('dph_dashboard') }}">DPH / UE</a>
            <a href="{{ url_for('company_edit') }}">Moja firma</a>
            <a href="{{ url_for('settings_page') }}">Ustawienia</a>
            <a href="{{ url_for('tools_page') }}">Narzędzia</a>
            {% if auth_enabled %}<a href="{{ url_for('logout') }}">Wyloguj</a>{% endif %}
        </nav>
        <a class="btn btn-primary btn-small desktop-new-invoice" href="{{ url_for('invoice_new') }}">+ Nowa faktura</a>
    </div>
</header>

<main class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <div class="flash-stack">
                {% for category, message in messages %}
                    <div class="flash flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            </div>
        {% endif %}
    {% endwith %}
    {% block content %}{% endblock %}
</main>

<a class="mobile-fab" href="{{ url_for('invoice_new') }}" aria-label="Nowa faktura">＋</a>
<script src="{{ url_for('static', filename='app.js') }}"></script>
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('{{ url_for("service_worker") }}').catch(()=>{}));
}
</script>
</body>
</html>"""

EMBEDDED_TEMPLATES["projects_list.html"] = r"""{% extends "base.html" %}
{% block title %}Projekty - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header">
  <div><h1>Projekty</h1><p class="muted">Zapisz numery projektów raz i wybieraj je później przy wystawianiu faktur.</p></div>
  <a class="btn btn-primary" href="{{ url_for('project_new') }}">+ Dodaj projekt</a>
</div>
<section class="panel">
{% if projects %}
<div class="table-wrap"><table>
<thead><tr><th>Numer</th><th>Nazwa</th><th>Kontrahent</th><th>Status</th><th></th></tr></thead>
<tbody>
{% for p in projects %}
<tr>
<td><strong>{{ p.project_number }}</strong></td>
<td>{{ p.project_name or '—' }}</td>
<td>{{ p.contractor_name or 'Wszyscy / bez przypisania' }}</td>
<td>{% if p.active %}<span class="badge badge-paid">Aktywny</span>{% else %}<span class="badge">Nieaktywny</span>{% endif %}</td>
<td class="actions"><a class="btn btn-light btn-small" href="{{ url_for('project_edit', project_id=p.id) }}">Edytuj</a>
<form class="inline-form" method="post" action="{{ url_for('project_delete', project_id=p.id) }}" onsubmit="return confirm('Usunąć projekt {{ p.project_number }}?')"><button class="btn btn-light btn-small" type="submit">Usuń</button></form></td>
</tr>
{% endfor %}
</tbody></table></div>
{% else %}
<div class="empty-state"><h2>Brak zapisanych projektów</h2><p>Dodaj pierwszy numer projektu.</p><a class="btn btn-primary" href="{{ url_for('project_new') }}">Dodaj projekt</a></div>
{% endif %}
</section>
{% endblock %}"""

EMBEDDED_TEMPLATES["project_form.html"] = r"""{% extends "base.html" %}
{% block title %}{{ 'Edytuj projekt' if is_edit else 'Nowy projekt' }} - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header"><div><h1>{{ 'Edytuj projekt' if is_edit else 'Nowy projekt' }}</h1></div></div>
<form method="post" class="panel form-panel">
<div class="form-grid two">
<label class="field"><span>Numer projektu *</span><input name="project_number" value="{{ project.project_number }}" required autofocus placeholder="np. HK348"></label>
<label class="field"><span>Kontrahent</span><select name="contractor_id"><option value="">— bez przypisania —</option>{% for c in contractors %}<option value="{{ c.id }}" {% if project.contractor_id|string == c.id|string %}selected{% endif %}>{{ c.name }}</option>{% endfor %}</select></label>
<label class="field span-2"><span>Nazwa / opis projektu</span><input name="project_name" value="{{ project.project_name }}" placeholder="np. Vienna - modernization"></label>
<label class="field span-2"><span>Notatki</span><textarea name="notes" rows="3">{{ project.notes }}</textarea></label>
<label class="checkbox-row span-2"><input type="checkbox" name="active" value="1" {% if project.active %}checked{% endif %}> <span>Projekt aktywny</span></label>
</div>
<div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz</button><a class="btn btn-ghost" href="{{ url_for('projects_list') }}">Anuluj</a></div>
</form>
{% endblock %}"""

# Dodaj wybór projektu do formularza faktury.
_invoice_form = EMBEDDED_TEMPLATES["invoice_form.html"]
_order_field = """        <label class="field"><span>Numer zamówienia</span><input name="order_number" value="{{ invoice.order_number }}"></label>
"""
_project_fields = """        <label class="field"><span>Projekt</span>
            <select id="project-select">
                <option value="">— wybierz zapisany projekt —</option>
                {% for project in projects %}
                <option value="{{ project.project_number }}" data-contractor="{{ project.contractor_id or '' }}" {% if invoice.order_number == project.project_number %}selected{% endif %}>{{ project.project_number }}{% if project.project_name %} — {{ project.project_name }}{% endif %}</option>
                {% endfor %}
            </select>
            <small>Lista filtruje się po wybranym kontrahencie.</small>
        </label>
        <label class="field"><span>Numer projektu / zamówienia</span><input id="order-number" name="order_number" value="{{ invoice.order_number }}" placeholder="Wybierz projekt lub wpisz ręcznie"></label>
"""
if _order_field in _invoice_form:
    _invoice_form = _invoice_form.replace(_order_field, _project_fields, 1)
else:
    raise RuntimeError("Nie znaleziono pola numeru zamówienia w invoice_form.")
EMBEDDED_TEMPLATES["invoice_form.html"] = _invoice_form

# Mobilny interfejs.
EMBEDDED_CSS += r"""
/* V7 mobile / PWA */
.mobile-menu-button,.mobile-fab{display:none}
@media (max-width: 900px){
  body{padding-bottom:78px}
  .container{padding:16px 12px}
  .topbar-inner{min-height:58px;position:relative;padding:0 12px}
  .mobile-menu-button{display:inline-flex;border:0;background:transparent;font-size:27px;color:var(--primary);padding:6px 10px;cursor:pointer}
  .desktop-new-invoice{display:none}
  .nav{display:none;position:absolute;left:10px;right:10px;top:58px;z-index:1000;background:#fff;border:1px solid var(--border);border-radius:14px;padding:8px;box-shadow:0 16px 40px rgba(0,0,0,.14);flex-direction:column;align-items:stretch}
  .nav.is-open{display:flex}
  .nav a{padding:11px 12px;border-radius:9px}
  .nav a:hover{background:#f6f1f3}
  .page-header{align-items:flex-start;gap:12px;flex-direction:column}
  .page-header>.button-row,.button-row{width:100%;flex-wrap:wrap}
  .form-grid.two,.form-grid.three,.form-grid.four,.stats-grid,.detail-grid,.minimum-grid{grid-template-columns:1fr!important}
  .span-2,.span-3,.span-4{grid-column:auto!important}
  .panel{padding:16px 12px;border-radius:14px}
  .table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{min-width:660px}
  .invoice-items{min-width:760px}
  input,select,textarea{font-size:16px!important}
  .mobile-fab{display:flex;position:fixed;right:18px;bottom:18px;width:56px;height:56px;border-radius:50%;background:var(--primary);color:#fff;align-items:center;justify-content:center;font-size:31px;text-decoration:none;box-shadow:0 10px 26px rgba(0,0,0,.25);z-index:900}
}
@media (display-mode: standalone){
  .topbar{padding-top:env(safe-area-inset-top)}
}
"""

# Rozszerz JS o menu i projekty.
EMBEDDED_JS += r"""
document.addEventListener('DOMContentLoaded', function(){
  const menuButton = document.getElementById('mobile-menu-button');
  const nav = document.getElementById('main-nav');
  if(menuButton && nav){
    menuButton.addEventListener('click', ()=>nav.classList.toggle('is-open'));
    nav.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>nav.classList.remove('is-open')));
  }

  const contractor = document.getElementById('contractor-select');
  const project = document.getElementById('project-select');
  const order = document.getElementById('order-number');

  function filterProjects(){
    if(!project) return;
    const cid = contractor ? String(contractor.value || '') : '';
    Array.from(project.options).forEach((opt, idx)=>{
      if(idx===0){ opt.hidden=false; return; }
      const pcid = String(opt.dataset.contractor || '');
      opt.hidden = !!pcid && !!cid && pcid !== cid;
    });
    const selected = project.options[project.selectedIndex];
    if(selected && selected.hidden) project.value = '';
  }
  if(contractor){ contractor.addEventListener('change', filterProjects); }
  if(project){
    project.addEventListener('change', function(){
      if(order && project.value) order.value = project.value;
    });
    filterProjects();
  }
});
"""


# ---------------------------------------------------------------------------
# V8: ewidencja prop firm, payoutów oraz wspólny kalkulator OSVČ
# ---------------------------------------------------------------------------

# Dodaj zakładkę do menu bez naruszania pozostałych pozycji.
_prop_nav_anchor = '<a href="{{ url_for(\'projects_list\') }}">Projekty</a>'
if _prop_nav_anchor in EMBEDDED_TEMPLATES["base.html"] and "prop_firms_dashboard" not in EMBEDDED_TEMPLATES["base.html"]:
    EMBEDDED_TEMPLATES["base.html"] = EMBEDDED_TEMPLATES["base.html"].replace(
        _prop_nav_anchor,
        _prop_nav_anchor + '\n            <a href="{{ url_for(\'prop_firms_dashboard\') }}">Prop firmy</a>',
        1,
    )

EMBEDDED_TEMPLATES["prop_firms.html"] = r"""{% extends "base.html" %}
{% block title %}Prop firmy - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header">
  <div>
    <h1>Prop firmy</h1>
    <p class="muted">Ewidencja rzeczywiście otrzymanych payoutów. Wirtualny wynik rachunku nie jest tu przychodem.</p>
  </div>
  <div class="button-row">
    <a class="btn btn-light" href="{{ url_for('prop_firm_new') }}">+ Dodaj firmę</a>
    <a class="btn btn-primary" href="{{ url_for('prop_payout_new') }}">+ Dodaj payout</a>
  </div>
</div>

<div class="callout info">
  <strong>Zasada ewidencji:</strong> przychód to kwota należna Tobie po profit split, ale przed opłatą operatora,
  przeliczona na CZK w dniu, w którym środki stały się dostępne. Późniejszy przelew na własny bank lub portfel nie tworzy drugiego przychodu.
  Klasyfikacja PIT nie ustala automatycznie DPH – DPH jest przechowywane osobno jako status do sprawdzenia.
</div>

<div class="callout warning">
  <strong>LucidFlex:</strong> domyślny profil dotyczy wyłącznie konta symulowanego objętego umową
  Lucid Trading Group LLC z 28.11.2025. Przy podziale 90/10 wynik symulowany 1 000 USD oznacza
  payout/przychód 900 USD przed opłatą operatora. Konto live wymaga dodania oddzielnej pozycji
  i ponownej analizy nowej umowy.
</div>

<form class="toolbar" method="get">
  <input type="number" name="year" min="2020" max="2100" value="{{ selected_year }}">
  <select name="firm_id">
    <option value="">Wszystkie firmy</option>
    {% for firm in firms %}<option value="{{ firm.id }}" {% if selected_firm_id == firm.id %}selected{% endif %}>{{ firm.name }}</option>{% endfor %}
  </select>
  <button class="btn btn-light" type="submit">Filtruj</button>
  {% if selected_firm_id %}<a class="btn btn-ghost" href="{{ url_for('prop_firms_dashboard', year=selected_year) }}">Wyczyść firmę</a>{% endif %}
</form>

<div class="stats-grid dashboard-stats">
  <div class="stat-card"><span class="stat-value compact-value">{{ summary.gross_czk|money('CZK') }}</span><span class="stat-label">Przychód brutto do PIT</span></div>
  <div class="stat-card"><span class="stat-value compact-value">{{ summary.fee_czk|money('CZK') }}</span><span class="stat-label">Opłaty operatorów – informacyjnie</span></div>
  <div class="stat-card"><span class="stat-value compact-value">{{ summary.net_czk|money('CZK') }}</span><span class="stat-label">Otrzymano netto</span></div>
  <div class="stat-card"><span class="stat-value">{{ summary.count }}</span><span class="stat-label">Liczba payoutów</span></div>
</div>

<section class="panel">
  <div class="panel-header"><h2>Payouty {{ selected_year }}</h2></div>
  {% if payouts %}
  <div class="table-wrap"><table>
    <thead><tr><th>Data</th><th>Firma / identyfikator</th><th>Należne po split</th><th>Opłata</th><th>Netto</th><th>Przychód CZK</th><th>Kwalifikacja</th><th>Faktura</th><th></th></tr></thead>
    <tbody>
    {% for p in payouts %}
      <tr>
        <td>{{ p.received_date|date_pl }}</td>
        <td><strong>{{ p.firm_name }}</strong><br><span class="muted">{{ p.payout_identifier or '—' }}</span></td>
        <td>{{ p.gross_amount|money(p.currency) }}</td>
        <td>{{ p.operator_fee|money(p.currency) }}</td>
        <td>{{ p.net_amount|money(p.currency) }}</td>
        <td><strong>{{ p.income_czk|money('CZK') }}</strong><br><span class="muted">kurs {{ p.czk_rate }}</span></td>
        <td>
          <span class="badge {% if p.qualification_status == 'unconfirmed' %}badge-warning{% elif p.qualification_status == 'confirmed' %}badge-paid{% endif %}">{{ PROP_TAX_CLASSIFICATIONS[p.tax_classification] }}</span>
          <br><small>{{ PROP_QUALIFICATION_STATUSES[p.qualification_status] }}</small>
          <br><small class="muted">DPH: {{ PROP_DPH_TREATMENTS[p.dph_treatment] }}</small>
        </td>
        <td>{% if p.invoice_number %}<a href="{{ url_for('invoice_view', invoice_id=p.linked_invoice_id) }}">{{ p.invoice_number }}</a>{% else %}—{% endif %}</td>
        <td class="actions"><a class="btn btn-light btn-small" href="{{ url_for('prop_payout_edit', payout_id=p.id) }}">Edytuj</a></td>
      </tr>
    {% endfor %}
    </tbody>
  </table></div>
  {% else %}<div class="empty-state"><h3>Brak payoutów w wybranym okresie</h3><a class="btn btn-primary" href="{{ url_for('prop_payout_new') }}">Dodaj payout</a></div>{% endif %}
</section>

<section class="panel">
  <div class="panel-header"><h2>Konfiguracja prop firm</h2><a class="btn btn-light btn-small" href="{{ url_for('prop_firm_new') }}">Dodaj firmę</a></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Firma</th><th>Domyślna kwalifikacja PIT</th><th>Status oceny</th><th>Kontrahent/faktury</th><th>DPH</th><th></th></tr></thead>
    <tbody>
    {% for firm in firms_all %}
      <tr>
        <td><strong>{{ firm.name }}</strong>{% if not firm.active %}<br><span class="badge">Nieaktywna</span>{% endif %}
          {% if firm.notes %}<details class="prop-assumption"><summary>Podstawa oceny</summary><div>{{ firm.notes }}</div></details>{% endif %}
        </td>
        <td>{{ PROP_TAX_CLASSIFICATIONS[firm.default_tax_classification] }}</td>
        <td><span class="badge {% if firm.qualification_status == 'unconfirmed' %}badge-warning{% elif firm.qualification_status == 'confirmed' %}badge-paid{% endif %}">{{ PROP_QUALIFICATION_STATUSES[firm.qualification_status] }}</span></td>
        <td>{{ firm.contractor_name or 'Nie przypisano' }}</td>
        <td>{{ PROP_DPH_TREATMENTS[firm.dph_treatment] }}</td>
        <td class="actions"><a class="btn btn-light btn-small" href="{{ url_for('prop_firm_edit', firm_id=firm.id) }}">Edytuj</a></td>
      </tr>
    {% endfor %}
    </tbody>
  </table></div>
</section>
{% endblock %}
"""

EMBEDDED_TEMPLATES["prop_firm_form.html"] = r"""{% extends "base.html" %}
{% block title %}{{ 'Edytuj prop firmę' if is_edit else 'Nowa prop firma' }} - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header"><div><h1>{{ 'Edytuj prop firmę' if is_edit else 'Nowa prop firma' }}</h1><p class="muted">Ustawienia są domyślne dla nowych payoutów. Istniejące payouty zachowują własną kwalifikację.</p></div></div>
<form method="post" class="panel form-panel">
<div class="form-grid two">
  <label class="field span-2"><span>Nazwa firmy *</span><input name="name" value="{{ firm.name }}" required autofocus></label>
  <label class="field"><span>Powiązany kontrahent fakturowy</span><select name="contractor_id"><option value="">— bez przypisania —</option>{% for c in contractors %}<option value="{{ c.id }}" {% if firm.contractor_id|string == c.id|string %}selected{% endif %}>{{ c.name }}</option>{% endfor %}</select><small>Ułatwia wybór faktury przy dodawaniu payoutu.</small></label>
  <label class="field"><span>Domyślna kwalifikacja PIT</span><select name="default_tax_classification">{% for code,label in PROP_TAX_CLASSIFICATIONS.items() %}<option value="{{ code }}" {% if firm.default_tax_classification == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>
  <label class="field"><span>Status kwalifikacji</span><select name="qualification_status">{% for code,label in PROP_QUALIFICATION_STATUSES.items() %}<option value="{{ code }}" {% if firm.qualification_status == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>
  <label class="field"><span>Klasyfikacja DPH</span><select name="dph_treatment">{% for code,label in PROP_DPH_TREATMENTS.items() %}<option value="{{ code }}" {% if firm.dph_treatment == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select><small>Nie wpływa automatycznie na raport DPH/VIES.</small></label>
  <label class="field span-2"><span>Notatka / podstawa oceny</span><textarea name="notes" rows="4">{{ firm.notes }}</textarea></label>
  <label class="checkbox-row span-2"><input type="checkbox" name="active" value="1" {% if firm.active %}checked{% endif %}> <span>Firma aktywna</span></label>
</div>
<div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz</button><a class="btn btn-ghost" href="{{ url_for('prop_firms_dashboard') }}">Anuluj</a></div>
</form>
{% if is_edit %}<div class="danger-zone"><span>Usunięcie jest możliwe tylko, jeśli firma nie ma payoutów.</span><form method="post" action="{{ url_for('prop_firm_delete', firm_id=firm.id) }}" onsubmit="return confirm('Usunąć tę prop firmę?')"><button class="btn btn-danger" type="submit">Usuń firmę</button></form></div>{% endif %}
{% endblock %}
"""

EMBEDDED_TEMPLATES["prop_payout_form.html"] = r"""{% extends "base.html" %}
{% block title %}{{ 'Edytuj payout' if is_edit else 'Nowy payout' }} - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header"><div><h1>{{ 'Edytuj payout' if is_edit else 'Nowy payout' }}</h1><p class="muted">Wpisuj kwotę należną Tobie po profit split, przed opłatą operatora.</p></div></div>
<form method="post" class="panel form-panel" id="prop-payout-form" data-is-edit="{{ 1 if is_edit else 0 }}">
<div class="form-grid three">
  <label class="field"><span>Firma *</span><select name="prop_firm_id" id="prop-firm-select" required>{% for f in firms %}<option value="{{ f.id }}" data-contractor="{{ f.contractor_id or '' }}" data-tax="{{ f.default_tax_classification }}" data-status="{{ f.qualification_status }}" data-dph="{{ f.dph_treatment }}" data-name="{{ f.name|e }}" data-notes="{{ f.notes|e }}" {% if payout.prop_firm_id|string == f.id|string %}selected{% endif %}>{{ f.name }}</option>{% endfor %}</select></label>
  <label class="field"><span>Konto / identyfikator payoutu</span><input name="payout_identifier" value="{{ payout.payout_identifier }}" placeholder="np. account ID / payout #"></label>
  <label class="field"><span>Data otrzymania dostępnych środków *</span><input type="date" name="received_date" value="{{ payout.received_date }}" required><small>Data na Rise/WorkMarket/wallecie, nie późniejszy przelew na własny bank.</small></label>
  <div id="prop-firm-profile-note" class="callout info span-3" hidden></div>

  <label class="field"><span>Waluta *</span><input id="payout-currency" name="currency" value="{{ payout.currency }}" maxlength="8" required placeholder="USD, EUR, USDT"></label>
  <label class="field"><span>Kwota należna po profit split *</span><input id="payout-gross" type="number" step="0.00000001" min="0" name="gross_amount" value="{{ payout.gross_amount }}" required><small>Nie wpisuj wirtualnego wyniku rachunku. LucidFlex: 1 000 USD wyniku przy 90/10 → wpisz 900 USD.</small></label>
  <label class="field"><span>Opłata operatora</span><input id="payout-fee" type="number" step="0.00000001" min="0" name="operator_fee" value="{{ payout.operator_fee }}"></label>

  <label class="field"><span>Kwota otrzymana netto *</span><input id="payout-net" type="number" step="0.00000001" min="0" name="net_amount" value="{{ payout.net_amount }}" required><small>Informacyjnie; nie pomniejsza przychodu przy kosztach procentowych.</small></label>
  <label class="field"><span>Kurs 1 jednostki waluty do CZK *</span><input id="payout-rate" type="number" step="0.000001" min="0.000001" name="czk_rate" value="{{ payout.czk_rate }}" required></label>
  <label class="field"><span>Przychód w CZK</span><input id="payout-income-czk" value="{{ payout.income_czk }}" readonly><small>Kwota po split × kurs; przed opłatą operatora.</small></label>

  <label class="field span-2"><span>Powiązana faktura</span><select name="linked_invoice_id" id="prop-invoice-select"><option value="">— bez faktury —</option>{% for inv in invoices %}<option value="{{ inv.id }}" data-contractor="{{ inv.contractor_id }}" {% if payout.linked_invoice_id|string == inv.id|string %}selected{% endif %}>{{ inv.invoice_number }} — {{ inv.contractor_name }} — {{ inv.total|money(inv.currency) }} — {{ inv.supply_date|date_pl }}</option>{% endfor %}</select><small>Powiązana faktura i payout są jednym przychodem. Faktura nie zostanie doliczona drugi raz.</small></label>
  <div class="field"><span>&nbsp;</span><a class="btn btn-light" href="{{ url_for('invoice_new') }}">Wystaw nową fakturę</a></div>

  <label class="field"><span>Kwalifikacja PIT</span><select name="tax_classification" id="prop-tax-classification">{% for code,label in PROP_TAX_CLASSIFICATIONS.items() %}<option value="{{ code }}" {% if payout.tax_classification == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>
  <label class="field"><span>Status kwalifikacji</span><select name="qualification_status" id="prop-qualification-status">{% for code,label in PROP_QUALIFICATION_STATUSES.items() %}<option value="{{ code }}" {% if payout.qualification_status == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>
  <label class="field"><span>DPH – osobna kwalifikacja</span><select name="dph_treatment" id="prop-dph-treatment">{% for code,label in PROP_DPH_TREATMENTS.items() %}<option value="{{ code }}" {% if payout.dph_treatment == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>
  <label class="field span-3"><span>Notatka</span><textarea name="notes" rows="4">{{ payout.notes }}</textarea></label>
</div>
<div class="callout warning"><strong>Ważne:</strong> opłata operatora, challenge fee oraz składki ČSSZ/VZP nie są dodatkowo odejmowane, gdy dla tej grupy stosujesz koszty procentowe. Dla konta live nie używaj automatycznie profilu LucidFlex – najpierw przeanalizuj nową umowę i ustaw rozliczenie ręczne lub oddzielną firmę.</div>
<div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz payout</button><a class="btn btn-ghost" href="{{ url_for('prop_firms_dashboard') }}">Anuluj</a></div>
</form>
{% if is_edit %}<div class="danger-zone"><span></span><form method="post" action="{{ url_for('prop_payout_delete', payout_id=payout.id) }}" onsubmit="return confirm('Usunąć ten payout?')"><button class="btn btn-danger" type="submit">Usuń payout</button></form></div>{% endif %}
{% endblock %}
"""

EMBEDDED_TEMPLATES["taxes_dashboard.html"] = r"""{% extends "base.html" %}
{% block title %}Podatki i składki - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header">
  <div><h1>Podatki i składki</h1><p class="muted">Rzeczywiście otrzymane przychody, procentowe koszty, podatek, ČSSZ i VZP.</p></div>
  <div class="button-row"><form class="toolbar year-toolbar" method="get"><input type="number" name="year" min="2020" max="2100" value="{{ snapshot.year }}"><button class="btn btn-light" type="submit">Pokaż rok</button></form><a class="btn btn-light" href="{{ url_for('settings_page', year=snapshot.year) }}">Ustawienia obliczeń</a></div>
</div>

<div class="callout warning"><strong>Wyliczenie orientacyjne.</strong> Kwalifikacja prop firm jest konfigurowalną oceną dokumentów, a nie wiążącą interpretacją urzędu. DPH jest analizowane osobno.</div>
{% if snapshot.assumptions %}<div class="callout info"><strong>Założenia użyte w kalkulacji:</strong><ul class="compact-list">{% for item in snapshot.assumptions %}<li>{{ item }}</li>{% endfor %}</ul></div>{% endif %}
{% if snapshot.warnings %}<div class="callout warning"><strong>Sprawdź:</strong><ul class="compact-list">{% for warning in snapshot.warnings %}<li>{{ warning }}</li>{% endfor %}</ul></div>{% endif %}

<div class="stats-grid dashboard-stats tax-stats">
  <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.welding_revenue|money('CZK') }}</span><span class="stat-label">Faktury usługowe / spawanie</span></div>
  <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.prop_revenue|money('CZK') }}</span><span class="stat-label">Prop firmy – przychód brutto</span></div>
  <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.revenue|money('CZK') }}</span><span class="stat-label">Łączny przychód §7</span></div>
  <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.flat_expenses|money('CZK') }}</span><span class="stat-label">Koszty procentowe razem</span></div>
  <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.profit|money('CZK') }}</span><span class="stat-label">Dochód podatkowy OSVČ</span></div>
  <div class="stat-card {% if snapshot.income_tax_overpayment > 0 %}tax-good{% elif snapshot.income_tax_underpayment > 0 %}tax-warning{% endif %}"><span class="stat-value compact-value">{{ (snapshot.income_tax_overpayment if snapshot.income_tax_overpayment > 0 else snapshot.income_tax_underpayment)|money('CZK') }}</span><span class="stat-label">{% if snapshot.income_tax_overpayment > 0 %}Nadpłata podatku{% elif snapshot.income_tax_underpayment > 0 %}Podatek do dopłaty{% else %}Podatek: bilans 0{% endif %}</span></div>
  <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.remaining.cssz|money('CZK') }}</span><span class="stat-label">ČSSZ do dopłaty / rezerwy</span></div>
  <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.remaining.vzp|money('CZK') }}</span><span class="stat-label">VZP do dopłaty / rezerwy</span></div>
  <div class="stat-card emphasis"><span class="stat-value compact-value">{{ snapshot.remaining_total|money('CZK') }}</span><span class="stat-label">Pozostała rezerwa po wpłatach</span></div>
</div>

<section class="panel">
  <div class="panel-header"><h2>Wspólne koszty procentowe {{ snapshot.year }}</h2></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Grupa</th><th>Przychód</th><th>Stawka</th><th>Limit roczny</th><th>Wykorzystane koszty</th><th>Dochód</th></tr></thead>
    <tbody>
      <tr><td><strong>60% – spawanie + payouty zakwalifikowane do 60%</strong></td><td>{{ snapshot.group60_revenue|money('CZK') }}</td><td>{{ snapshot.settings.expense_percent }}%</td><td>{{ snapshot.settings.expense_limit|money('CZK') }}</td><td><strong>{{ snapshot.flat_expenses_60|money('CZK') }}</strong><br><small>{{ snapshot.group60_limit_usage_percent }}% limitu</small></td><td>{{ snapshot.profit_60|money('CZK') }}</td></tr>
      <tr><td><strong>40% – inne przychody §7</strong></td><td>{{ snapshot.group40_revenue|money('CZK') }}</td><td>{{ snapshot.settings.expense_40_percent }}%</td><td>{{ snapshot.settings.expense_40_limit|money('CZK') }}</td><td><strong>{{ snapshot.flat_expenses_40|money('CZK') }}</strong></td><td>{{ snapshot.profit_40|money('CZK') }}</td></tr>
    </tbody>
    <tfoot><tr><th>Razem</th><th>{{ snapshot.revenue|money('CZK') }}</th><th></th><th></th><th>{{ snapshot.flat_expenses|money('CZK') }}</th><th>{{ snapshot.profit|money('CZK') }}</th></tr></tfoot>
  </table></div>
  <div class="callout info">Limit 1 200 000 CZK jest jeden dla całej grupy 60% – nie osobno dla spawania i każdej prop firmy oraz bez proporcjonalnego skracania za niepełny rok.</div>
</section>

<section class="panel">
  <div class="panel-header"><h2>Rozliczenie zobowiązań</h2><span class="badge badge-warning">{{ snapshot.active_months }} mies. działalności</span></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Rodzaj</th><th>Należność roczna</th><th>Zapłacono / pobrano</th><th>Bilans</th></tr></thead>
    <tbody>
      <tr><td><strong>Podatek dochodowy</strong></td><td>{{ snapshot.annual_tax_after_credit|money('CZK') }}</td><td>{{ snapshot.income_tax_paid_total|money('CZK') }}<br><small>w tym Accenture {{ snapshot.employment_tax_withheld|money('CZK') }}</small></td><td>{% if snapshot.income_tax_overpayment > 0 %}<strong class="text-success">Nadpłata {{ snapshot.income_tax_overpayment|money('CZK') }}</strong>{% else %}<strong>Do dopłaty {{ snapshot.income_tax_underpayment|money('CZK') }}</strong>{% endif %}</td></tr>
      <tr><td><strong>ČSSZ</strong></td><td>{{ snapshot.cssz|money('CZK') }}</td><td>{{ snapshot.paid.cssz|money('CZK') }}</td><td>{% if snapshot.overpayments.cssz > 0 %}<strong class="text-success">Nadpłata {{ snapshot.overpayments.cssz|money('CZK') }}</strong>{% else %}<strong>Do dopłaty {{ snapshot.remaining.cssz|money('CZK') }}</strong>{% endif %}</td></tr>
      <tr><td><strong>VZP</strong></td><td>{{ snapshot.vzp|money('CZK') }}</td><td>{{ snapshot.paid.vzp|money('CZK') }}</td><td>{% if snapshot.overpayments.vzp > 0 %}<strong class="text-success">Nadpłata {{ snapshot.overpayments.vzp|money('CZK') }}</strong>{% else %}<strong>Do dopłaty {{ snapshot.remaining.vzp|money('CZK') }}</strong>{% endif %}</td></tr>
    </tbody>
  </table></div>
</section>

<section class="panel">
  <div class="panel-header"><h2>Minimalne miesięczne zaliczki</h2></div>
  <div class="minimum-grid"><div class="minimum-card"><span class="minimum-name">ČSSZ</span><strong>{{ snapshot.settings.cssz_monthly_advance|money('CZK') }}</strong><small>Za dany miesiąc do jego ostatniego dnia.</small></div><div class="minimum-card"><span class="minimum-name">VZP</span><strong>{{ snapshot.settings.vzp_monthly_advance|money('CZK') }}</strong><small>Za dany miesiąc do 8. dnia następnego miesiąca.</small></div></div>
</section>

{% if snapshot.forecast_total > 0 %}
<section class="panel forecast-panel"><div class="panel-header"><h2>Prognoza – poza rzeczywistym rozliczeniem</h2><span class="badge">Nie wpływa na wynik</span></div><div class="stats-grid"><div class="stat-card"><span class="stat-value compact-value">{{ snapshot.forecast_welding_revenue|money('CZK') }}</span><span class="stat-label">Prognoza spawanie</span></div><div class="stat-card"><span class="stat-value compact-value">{{ snapshot.forecast_prop_revenue|money('CZK') }}</span><span class="stat-label">Prognoza prop firmy</span></div><div class="stat-card"><span class="stat-value compact-value">{{ snapshot.forecast_total|money('CZK') }}</span><span class="stat-label">Prognoza razem</span></div></div></section>
{% endif %}

<section class="panel">
  <div class="panel-header"><h2>Faktury uwzględnione jako przychód usługowy</h2><span class="muted">{{ 'według zapłaty' if snapshot.settings.revenue_basis == 'paid' else 'według wystawienia' }}</span></div>
  {% if snapshot.invoice_rows %}<div class="table-wrap"><table><thead><tr><th>Faktura</th><th>Kontrahent</th><th>Data przychodu</th><th>Kwota CZK</th></tr></thead><tbody>{% for row in snapshot.invoice_rows %}<tr><td><a href="{{ url_for('invoice_view', invoice_id=row.id) }}"><strong>{{ row.invoice_number }}</strong></a></td><td>{{ row.contractor_name }}</td><td>{{ row.recognized_date|date_pl }}</td><td>{{ row.value_czk|money('CZK') }}</td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="muted">Brak faktur spełniających wybraną podstawę przychodu. Faktury powiązane z payoutami są wyłączone, aby nie liczyć ich drugi raz.</p>{% endif %}
</section>

<section class="panel">
  <div class="panel-header"><h2>Payouty uwzględnione w przychodzie</h2><a class="btn btn-light btn-small" href="{{ url_for('prop_firms_dashboard', year=snapshot.year) }}">Otwórz ewidencję</a></div>
  {% if snapshot.prop_rows_included %}<div class="table-wrap"><table><thead><tr><th>Data</th><th>Firma</th><th>Przychód CZK</th><th>Grupa</th><th>Faktura</th></tr></thead><tbody>{% for row in snapshot.prop_rows_included %}<tr><td>{{ row.received_date|date_pl }}</td><td>{{ row.firm_name }}</td><td>{{ row.income_czk|money('CZK') }}</td><td>{{ PROP_TAX_CLASSIFICATIONS[row.tax_classification] }}</td><td>{{ row.invoice_number or '—' }}</td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="muted">Brak payoutów ujętych w §7 w tym roku.</p>{% endif %}
</section>

<section class="panel">
  <div class="panel-header"><h2>Zapisane wpłaty do urzędów</h2></div>
  <form method="post" action="{{ url_for('tax_payment_add') }}" class="form-grid four tax-payment-form"><input type="hidden" name="year" value="{{ snapshot.year }}"><label class="field"><span>Data wpłaty</span><input type="date" name="payment_date" value="{{ today }}" required></label><label class="field"><span>Rodzaj</span><select name="payment_type">{% for code,label in TAX_PAYMENT_TYPES.items() %}<option value="{{ code }}">{{ label }}</option>{% endfor %}</select></label><label class="field"><span>Kwota CZK</span><input type="number" step="0.01" min="0.01" name="amount" required></label><label class="field"><span>Notatka</span><input name="note"></label><div class="span-4 form-actions compact-actions"><button class="btn btn-primary" type="submit">Dodaj wpłatę</button></div></form>
  {% if payments %}<div class="table-wrap"><table><thead><tr><th>Data</th><th>Rodzaj</th><th>Kwota</th><th>Notatka</th><th></th></tr></thead><tbody>{% for payment in payments %}<tr><td>{{ payment.payment_date|date_pl }}</td><td>{{ TAX_PAYMENT_TYPES[payment.payment_type] }}</td><td>{{ payment.amount|money('CZK') }}</td><td>{{ payment.note or '—' }}</td><td><form method="post" action="{{ url_for('tax_payment_delete', payment_id=payment.id) }}" onsubmit="return confirm('Usunąć tę wpłatę?')"><input type="hidden" name="year" value="{{ snapshot.year }}"><button class="btn btn-light btn-small">Usuń</button></form></td></tr>{% endfor %}</tbody></table></div>{% else %}<p class="muted">Brak zapisanych wpłat.</p>{% endif %}
</section>
{% endblock %}
"""

EMBEDDED_TEMPLATES["settings.html"] = r"""{% extends "base.html" %}
{% block title %}Ustawienia - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header"><div><h1>Ustawienia</h1><p class="muted">Parametry są przechowywane osobno dla każdego roku.</p></div><form class="toolbar year-toolbar" method="get"><input type="number" name="year" min="2020" max="2100" value="{{ snapshot.year }}"><button class="btn btn-light">Pokaż rok</button></form></div>
<div class="callout info">Zmiany wpływają wyłącznie na kalkulacje aplikacji – nie wysyłają żadnych danych do urzędów.</div>
<section class="panel"><form method="post" action="{{ url_for('tax_settings_save') }}" class="form-panel tax-settings-form"><input type="hidden" name="year" value="{{ snapshot.year }}">
<h2>Przychód, koszty procentowe i podatek</h2>
<div class="form-grid three">
  <label class="field"><span>Początek działalności</span><input type="date" name="activity_start_date" value="{{ snapshot.settings.activity_start_date }}"></label>
  <label class="field"><span>Koniec działalności</span><input type="date" name="activity_end_date" value="{{ snapshot.settings.activity_end_date }}"></label>
  <label class="field"><span>Faktury usługowe licz</span><select name="revenue_basis"><option value="paid" {% if snapshot.settings.revenue_basis == 'paid' %}selected{% endif %}>Według zapłaty</option><option value="issued" {% if snapshot.settings.revenue_basis == 'issued' %}selected{% endif %}>Według wystawienia</option></select><small>Payouty zawsze według faktycznej daty otrzymania.</small></label>
  <label class="field"><span>Grupa 60% – stawka kosztów</span><input type="number" step="0.01" min="0" max="100" name="expense_percent" value="{{ snapshot.settings.expense_percent }}"></label>
  <label class="field"><span>Grupa 60% – wspólny limit kosztów CZK</span><input type="number" step="1" min="0" name="expense_limit" value="{{ snapshot.settings.expense_limit }}"><small>Wspólny dla spawania i payoutów 60%; nieproporcjonalny.</small></label>
  <label class="field"><span>Grupa 40% – stawka kosztów</span><input type="number" step="0.01" min="0" max="100" name="expense_40_percent" value="{{ snapshot.settings.expense_40_percent }}"></label>
  <label class="field"><span>Grupa 40% – limit kosztów CZK</span><input type="number" step="1" min="0" name="expense_40_limit" value="{{ snapshot.settings.expense_40_limit }}"></label>
  <label class="field"><span>Roczna ulga podatnika</span><input type="number" step="1" min="0" name="income_tax_credit" value="{{ snapshot.settings.income_tax_credit }}"></label>
  <label class="field"><span>Próg 23% w CZK</span><input type="number" step="1" min="0" name="tax_threshold" value="{{ snapshot.settings.tax_threshold }}"></label>
  <label class="field"><span>Podstawa podatku z zatrudnienia</span><input type="number" step="1" min="0" name="employment_tax_base" value="{{ snapshot.settings.employment_tax_base }}"></label>
  <label class="field"><span>Zaliczki pobrane przez pracodawcę</span><input type="number" step="1" min="0" name="employment_tax_withheld" value="{{ snapshot.settings.employment_tax_withheld }}"></label>
</div>
<hr><h2>Prognoza opcjonalna – nie wpływa na rozliczenie</h2>
<div class="form-grid two"><label class="field"><span>Prognozowany przychód ze spawania CZK</span><input type="number" step="1" min="0" name="forecast_welding_revenue" value="{{ snapshot.settings.forecast_welding_revenue }}"></label><label class="field"><span>Prognozowany przychód z prop firm CZK</span><input type="number" step="1" min="0" name="forecast_prop_revenue" value="{{ snapshot.settings.forecast_prop_revenue }}"></label></div>
<hr><h2>ČSSZ</h2>
<div class="form-grid three">
  <label class="field"><span>Tryb działalności</span><select name="cssz_mode"><option value="secondary" {% if snapshot.settings.cssz_mode == 'secondary' %}selected{% endif %}>Tylko vedlejší</option><option value="mixed" {% if snapshot.settings.cssz_mode == 'mixed' %}selected{% endif %}>Vedlejší → hlavní</option><option value="main" {% if snapshot.settings.cssz_mode == 'main' %}selected{% endif %}>Tylko hlavní</option><option value="custom" {% if snapshot.settings.cssz_mode == 'custom' %}selected{% endif %}>Własne ustawienia</option></select></label>
  <label class="field"><span>Hlavní od dnia</span><input type="date" name="cssz_main_from_date" value="{{ snapshot.settings.cssz_main_from_date }}"></label>
  <label class="field"><span>Rozhodná částka roczna</span><input type="number" step="1" name="cssz_threshold_annual" value="{{ snapshot.settings.cssz_threshold_annual }}"></label>
  <label class="field"><span>Pomniejszenie progu za miesiąc</span><input type="number" step="1" name="cssz_threshold_reduction_month" value="{{ snapshot.settings.cssz_threshold_reduction_month }}"></label>
  <label class="field"><span>Podstawa z dochodu (%)</span><input type="number" step="0.01" name="cssz_assessment_percent" value="{{ snapshot.settings.cssz_assessment_percent }}"></label>
  <label class="field"><span>Stawka ČSSZ (%)</span><input type="number" step="0.01" name="cssz_rate_percent" value="{{ snapshot.settings.cssz_rate_percent }}"></label>
  <label class="field"><span>Min. podstawa miesięczna hlavní</span><input type="number" step="0.01" name="cssz_min_monthly_base" value="{{ snapshot.settings.cssz_min_monthly_base }}"></label>
  <label class="field"><span>Min. podstawa miesięczna vedlejší</span><input type="number" step="0.01" name="cssz_secondary_min_monthly_base" value="{{ snapshot.settings.cssz_secondary_min_monthly_base }}"></label>
  <label class="field"><span>Bieżąca zaliczka miesięczna ČSSZ</span><input type="number" step="0.01" name="cssz_monthly_advance" value="{{ snapshot.settings.cssz_monthly_advance }}"></label>
</div>
<hr><h2>VZP</h2>
<div class="form-grid three">
  <label class="field"><span>Tryb ubezpieczenia</span><select name="vzp_mode"><option value="secondary" {% if snapshot.settings.vzp_mode == 'secondary' %}selected{% endif %}>Bez minimum / zatrudnienie</option><option value="mixed" {% if snapshot.settings.vzp_mode == 'mixed' %}selected{% endif %}>Zmiana na główną OSVČ</option><option value="main" {% if snapshot.settings.vzp_mode == 'main' %}selected{% endif %}>Główna OSVČ</option><option value="custom" {% if snapshot.settings.vzp_mode == 'custom' %}selected{% endif %}>Własne ustawienia</option></select></label>
  <label class="field"><span>Minimum od dnia</span><input type="date" name="vzp_main_from_date" value="{{ snapshot.settings.vzp_main_from_date }}"></label>
  <label class="field"><span>Podstawa z dochodu (%)</span><input type="number" step="0.01" name="vzp_assessment_percent" value="{{ snapshot.settings.vzp_assessment_percent }}"></label>
  <label class="field"><span>Stawka VZP (%)</span><input type="number" step="0.01" name="vzp_rate_percent" value="{{ snapshot.settings.vzp_rate_percent }}"></label>
  <label class="field"><span>Minimalna podstawa miesięczna</span><input type="number" step="0.01" name="vzp_min_monthly_base" value="{{ snapshot.settings.vzp_min_monthly_base }}"></label>
  <label class="field"><span>Bieżąca zaliczka miesięczna VZP</span><input type="number" step="0.01" name="vzp_monthly_advance" value="{{ snapshot.settings.vzp_monthly_advance }}"></label>
</div>
<div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz ustawienia</button><a class="btn btn-light" href="{{ url_for('taxes_dashboard', year=snapshot.year) }}">Wróć do wyników</a></div>
</form></section>
{% endblock %}
"""

# Informacja o powiązanym payoutcie na fakturze.
_invoice_detail_v8 = EMBEDDED_TEMPLATES["invoice_detail.html"]
_detail_anchor = '<div class="detail-grid">'
if _detail_anchor in _invoice_detail_v8:
    _invoice_detail_v8 = _invoice_detail_v8.replace(
        _detail_anchor,
        """{% if linked_payout %}<div class="callout info"><strong>Prop firma:</strong> faktura jest powiązana z payoutem {{ linked_payout.firm_name }} otrzymanym {{ linked_payout.received_date|date_pl }}. W kalkulatorze przychodu liczony jest payout {{ linked_payout.income_czk|money('CZK') }}, a faktura nie jest liczona drugi raz. <a href="{{ url_for('prop_payout_edit', payout_id=linked_payout.id) }}">Otwórz payout</a>.</div>{% endif %}""" + _detail_anchor,
        1,
    )
EMBEDDED_TEMPLATES["invoice_detail.html"] = _invoice_detail_v8

# Narzędzia: pokaż także dane prop firm.
_tools_v8 = EMBEDDED_TEMPLATES["tools.html"]
_tools_v8 = _tools_v8.replace('<span class="stat-label">Koszty</span>', '<span class="stat-label">Koszty</span>', 1)
EMBEDDED_TEMPLATES["tools.html"] = _tools_v8

EMBEDDED_CSS += r"""
/* V8 Prop firmy */
.forecast-panel{border-style:dashed}.badge-warning{background:#fff0dd;color:#995200}.prop-assumption{font-size:.9rem}.field small{line-height:1.35}.danger-zone{margin-top:18px;display:flex;justify-content:space-between;gap:12px;align-items:center}.text-success{color:#16843d}.text-warning{color:#a55a00}
@media(max-width:900px){.danger-zone{align-items:stretch;flex-direction:column}.danger-zone form,.danger-zone button{width:100%}}
"""

EMBEDDED_JS += r"""
document.addEventListener('DOMContentLoaded', function(){
  const form=document.getElementById('prop-payout-form');
  if(!form) return;
  const firm=document.getElementById('prop-firm-select');
  const gross=document.getElementById('payout-gross');
  const fee=document.getElementById('payout-fee');
  const net=document.getElementById('payout-net');
  const rate=document.getElementById('payout-rate');
  const currency=document.getElementById('payout-currency');
  const income=document.getElementById('payout-income-czk');
  const inv=document.getElementById('prop-invoice-select');
  const tax=document.getElementById('prop-tax-classification');
  const status=document.getElementById('prop-qualification-status');
  const dph=document.getElementById('prop-dph-treatment');
  const profileNote=document.getElementById('prop-firm-profile-note');
  let netTouched=!!(net && net.value);
  if(net) net.addEventListener('input',()=>{netTouched=true});
  function n(el){const v=parseFloat(String(el&&el.value||'').replace(',','.'));return Number.isFinite(v)?v:0}
  function recalc(){
    if(currency && currency.value.trim().toUpperCase()==='CZK' && rate && (!rate.value || rate.value==='0')) rate.value='1';
    if(net && !netTouched) net.value=Math.max(n(gross)-n(fee),0).toFixed(2);
    if(income) income.value=(n(gross)*n(rate)).toFixed(2);
  }
  [gross,fee,rate,currency].forEach(el=>{if(el)el.addEventListener('input',recalc)});
  function filterInvoices(){
    if(!firm || !inv) return;
    const opt=firm.options[firm.selectedIndex];
    const cid=String((opt && opt.dataset.contractor)||'');
    Array.from(inv.options).forEach((o,i)=>{
      const selected=o.selected;
      o.hidden=i>0 && !selected && !!cid && String(o.dataset.contractor||'')!==cid;
    });
  }
  function showFirmProfile(){
    if(!profileNote || !firm) return;
    const opt=firm.options[firm.selectedIndex];
    const notes=String((opt && opt.dataset.notes)||'').trim();
    if(notes){
      profileNote.textContent=notes;
      profileNote.hidden=false;
    }else{
      profileNote.textContent='';
      profileNote.hidden=true;
    }
  }
  function applyFirmDefaults(){
    if(!firm) return; const opt=firm.options[firm.selectedIndex]; if(!opt) return;
    if(tax) tax.value=opt.dataset.tax||tax.value;
    if(status) status.value=opt.dataset.status||status.value;
    if(dph) dph.value=opt.dataset.dph||dph.value;
    filterInvoices();
    showFirmProfile();
  }
  if(firm) firm.addEventListener('change',applyFirmDefaults);
  if(form.dataset.isEdit!=='1') applyFirmDefaults(); else { filterInvoices(); showFirmProfile(); }
  recalc();
});
"""


# V8.2 navigation and forecast templates
EMBEDDED_TEMPLATES['settings_tabs.html'] = '<nav class="settings-tabs" aria-label="Sekcje ustawień">\n <a class="{% if request.endpoint == \'settings_page\' and settings_tab|default(\'tax\') == \'tax\' %}active{% endif %}" href="{{ url_for(\'settings_page\', year=selected_year|default(snapshot.year if snapshot is defined else current_year), tab=\'tax\') }}">Podatki i składki</a>\n <a class="{% if settings_tab|default(\'\') == \'contractors\' or request.endpoint in [\'contractors_list\',\'contractor_new\',\'contractor_edit\'] %}active{% endif %}" href="{{ url_for(\'contractors_list\') }}">Kontrahenci</a>\n <a class="{% if settings_tab|default(\'\') == \'projects\' or request.endpoint in [\'projects_list\',\'project_new\',\'project_edit\'] %}active{% endif %}" href="{{ url_for(\'projects_list\') }}">Projekty</a>\n <a class="{% if settings_tab|default(\'\') == \'forecast\' %}active{% endif %}" href="{{ url_for(\'settings_page\', year=selected_year|default(snapshot.year if snapshot is defined else current_year), tab=\'forecast\') }}">Prognoza przyszłych składek</a>\n</nav>\n'
EMBEDDED_TEMPLATES['contribution_forecast.html'] = '<section class="panel contribution-forecast" id="prognoza-skladek">\n <div class="panel-header"><div><div class="eyebrow">PLANOWANIE · {{ forecast.source_year }} → {{ forecast.target_year }}</div><h2>Prognoza zaliczek ČSSZ / VZP na {{ forecast.target_year }}</h2></div><span class="badge badge-warning">Szacunek, nie wezwanie do zapłaty</span></div>\n <p class="muted">Stan otrzymanych środków na {{ forecast.as_of|date_pl }}. Działalność w roku źródłowym: {{ forecast.cssz_main_months }} mies. hlavní + {{ forecast.cssz_secondary_months }} mies. vedlejší. Wynagrodzenie z Accenture nie zwiększa podstawy składek OSVČ.</p>\n <div class="forecast-stats">\n  <div><span>Zapłacone faktury usługowe</span><strong>{{ forecast.welding_revenue|money(\'CZK\') }}</strong></div>\n  <div><span>Otrzymane payouty §7</span><strong>{{ forecast.prop_revenue|money(\'CZK\') }}</strong></div>\n  <div><span>Dochód po kosztach procentowych</span><strong>{{ forecast.actual.profit|money(\'CZK\') }}</strong></div>\n </div>\n {% if forecast.actual.advances %}\n <div class="table-wrap"><table class="forecast-table">\n <thead><tr><th>Etap / wariant</th><th>ČSSZ / mies.</th><th>VZP / mies.</th><th>Razem / mies.</th><th>Zmiana łącznie</th></tr></thead>\n <tbody>\n <tr><td>Bieżące zaliczki zapisane w ustawieniach {{ forecast.source_year }}</td><td>{{ forecast.current_cssz|money(\'CZK\') }}</td><td>{{ forecast.current_vzp|money(\'CZK\') }}</td><td>{{ forecast.current_total|money(\'CZK\') }}</td><td>—</td></tr>\n <tr><td><strong>Od stycznia {{ forecast.target_year }}</strong><small>Dotychczasowy przedpis, nie mniej niż nowe minimum</small></td><td>{{ forecast.january.cssz|money(\'CZK\') }}</td><td>{{ forecast.january.vzp|money(\'CZK\') }}</td><td><strong>{{ forecast.january.total|money(\'CZK\') }}</strong></td><td>{{ forecast.january.difference|money(\'CZK\') }}</td></tr>\n <tr><td><strong>Po Přehledzie: bez kolejnych przychodów</strong><small>Otrzymane do {{ forecast.as_of|date_pl }}; miesiące według pełnego okresu działalności</small></td><td>{{ forecast.actual.advances.cssz|money(\'CZK\') }}</td><td>{{ forecast.actual.advances.vzp|money(\'CZK\') }}</td><td><strong>{{ forecast.actual.advances.total|money(\'CZK\') }}</strong></td><td>{{ forecast.actual.advances.difference|money(\'CZK\') }}</td></tr>\n {% if forecast.has_scenario %}<tr class="scenario-row"><td><strong>Po Přehledzie: Twój scenariusz</strong><small>Dodatkowe wpływy {{ forecast.simulated.additional|money(\'CZK\') }}; łącznie {{ forecast.simulated.revenue|money(\'CZK\') }} przychodu</small></td><td>{{ forecast.simulated.advances.cssz|money(\'CZK\') }}</td><td>{{ forecast.simulated.advances.vzp|money(\'CZK\') }}</td><td><strong>{{ forecast.simulated.advances.total|money(\'CZK\') }}</strong></td><td>{{ forecast.simulated.advances.difference|money(\'CZK\') }}</td></tr>{% endif %}\n </tbody></table></div>\n <p><strong>Kiedy zmiana po rozliczeniu?</strong> ČSSZ: od miesiąca następującego po miesiącu, w którym złożysz lub powinieneś złożyć Přehled. VZP: już za miesiąc złożenia lub wymaganej daty złożenia. Podwyżka minimum obowiązuje wcześniej, od stycznia.</p>\n <details class="forecast-details"><summary>Wzory, założenia i jakość danych</summary>\n <p>ČSSZ: 55% dochodu §7 ÷ {{ forecast.cssz_months }} mies.; następnie 29,2%, minimum i maksimum oraz zaokrąglenia w górę. VZP: 50% dochodu §7 ÷ {{ forecast.vzp_months }} mies. × 13,5%, z minimum i zaokrągleniem w górę. Przy zmienionych ręcznie stawkach używane są parametry z ustawień.</p>\n <p>Wpłacone zaliczki zmniejszają rozliczenie roczne, ale nie obniżają wysokości zaliczek na następny rok. Nie dzielimy wyniku przez 12, jeżeli działalność trwała tylko część roku.</p>\n <p>{{ forecast.parameters.source_note }}</p>\n {% if forecast.parameters.status == \'manual\' %}<p class="callout warning">Parametry roku docelowego wprowadzono ręcznie. Sprawdź ich zgodność z przepisami.</p>{% endif %}\n <p>Źródła: <a href="https://www.zakonyprolidi.cz/cs/2026-177" target="_blank" rel="noopener noreferrer">NV 177/2026 Sb.</a> · <a href="https://www.cssz.gov.cz/zalohy-na-pojistne-na-duchodove-pojisteni" target="_blank" rel="noopener noreferrer">ČSSZ — zaliczki</a> · <a href="https://www.vzp.cz/platci/informace/osvc/zalohy-na-pojistne/vypocet-zaloh-na-pojistne" target="_blank" rel="noopener noreferrer">VZP — zaliczki</a></p>\n </details>\n {% else %}<div class="callout warning">Brak liczby miesięcy działalności lub parametrów roku docelowego. Uzupełnij ustawienia prognozy. Nie pokazujemy zgadywanych stawek.</div>{% endif %}\n {% if forecast.warnings %}<div class="callout warning"><strong>Ważne dla tej prognozy:</strong><ul class="compact-list">{% for w in forecast.warnings %}<li>{{ w }}</li>{% endfor %}</ul></div>{% endif %}\n {% if forecast.pending %}<details class="forecast-details"><summary>Nieopłacone faktury — wyłączone z otrzymanych przychodów ({{ forecast.pending|length }})</summary>\n <div class="table-wrap"><table><thead><tr><th>Numer</th><th>Kwota</th><th>Wartość CZK (pomocniczo)</th></tr></thead><tbody>{% for p in forecast.pending %}<tr><td><a href="{{ url_for(\'invoice_view\', invoice_id=p.id) }}">{{ p.number }}</a></td><td>{{ p.amount|money(p.currency) }}</td><td>{{ p.value_czk|money(\'CZK\') if p.value_czk is not none else \'Brak kursu – do uzupełnienia\' }}</td></tr>{% endfor %}</tbody></table></div>\n <p class="muted">Nie są automatycznie doliczane do scenariusza. Oczekiwane wpływy możesz sam wpisać w ustawieniach prognozy.</p></details>{% endif %}\n <a class="btn btn-light" href="{{ url_for(\'settings_page\', year=forecast.source_year, tab=\'forecast\') }}">Zmień założenia prognozy</a>\n</section>\n'
EMBEDDED_TEMPLATES['forecast_settings.html'] = '<section class="panel">\n <div class="panel-header"><h2>Założenia prognozy {{ forecast.source_year }} → {{ forecast.target_year }}</h2></div>\n <p>Prognoza nie zmienia faktur, payoutów, zapisanych wpłat ani podstawy rozliczenia. Liczy opłacone przychody niezależnie od trybu „według wystawienia” w kalkulatorze.</p>\n <form method="post" action="{{ url_for(\'contribution_forecast_save\') }}" class="form-panel">\n <input type="hidden" name="source_year" value="{{ forecast.source_year }}">\n <div class="form-grid three">\n <label class="field"><span>Stan na dzień (puste = dzisiaj)</span><input type="date" name="as_of_date" value="{{ forecast.options.as_of_date }}" min="{{ forecast.source_year }}-01-01" max="{{ forecast.source_year }}-12-31"></label>\n <label class="field"><span>Dodatkowe wpływy do końca roku — grupa 60%, CZK</span><input type="number" name="additional_revenue_60" step="0.01" min="0" value="{{ forecast.options.additional_revenue_60 }}"><small>Tylko wpływy jeszcze nieotrzymane na dzień zestawienia; wspólnie spawanie i prop firmy 60%.</small></label>\n <label class="field"><span>Dodatkowe wpływy do końca roku — grupa 40%, CZK</span><input type="number" name="additional_revenue_40" step="0.01" min="0" value="{{ forecast.options.additional_revenue_40 }}"><small>Bez challenge i opłat — nie są dodatkowymi kosztami przy paušálu.</small></label>\n </div>\n <details class="forecast-details"><summary>Zaawansowane: miesiące, wyjątki i notatka</summary>\n <p class="muted">Domyślnie kontynuacja hlavní. Nie zaznaczaj zwolnień tylko dlatego, że działalność ponownie rozpoczęła się w 2026. Wcześniejsza działalność może wykluczać ulgę.</p>\n <div class="form-grid two">\n <label class="field"><span>Miesiące dla ČSSZ (0 = automatycznie {{ forecast.cssz_main_months + forecast.cssz_secondary_months }})</span><input type="number" min="0" max="12" name="cssz_months_override" value="{{ forecast.options.cssz_months_override }}"><small>Uwzględnij wszystkie miesiące hlavní i vedlejší z poprzedniego roku; nie tylko główne.</small></label>\n <label class="field"><span>Miesiące dla VZP (0 = automatycznie)</span><input type="number" min="0" max="12" name="vzp_months_override" value="{{ forecast.options.vzp_months_override }}"><small>Zmień tylko na podstawie właściwego Přehledu i rzeczywistego okresu.</small></label>\n <label class="checkbox-row"><input type="checkbox" name="cssz_advance_exempt" value="1" {% if forecast.options.cssz_advance_exempt %}checked{% endif %}> Potwierdzone zwolnienie z zaliczek ČSSZ</label>\n <label class="checkbox-row"><input type="checkbox" name="vzp_advance_exempt" value="1" {% if forecast.options.vzp_advance_exempt %}checked{% endif %}> Potwierdzone zwolnienie z zaliczek VZP</label>\n <label class="checkbox-row"><input type="checkbox" name="vzp_minimum_applies" value="1" {% if forecast.options.vzp_minimum_applies %}checked{% endif %}> Obowiązuje minimalna podstawa VZP</label>\n <label class="field span-2"><span>Notatka / podstawa wyjątku</span><textarea name="note" rows="2">{{ forecast.options.note }}</textarea></label>\n </div></details>\n <div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz scenariusz</button><a class="btn btn-light" href="{{ url_for(\'taxes_dashboard\', year=forecast.source_year) }}#prognoza-skladek">Pokaż prognozę</a></div>\n </form>\n</section>\n<section class="panel">\n <h2>Parametry składek na {{ forecast.target_year }}</h2>\n <p class="muted">Przechowywane osobno według roku. Nie zastępują Twoich obecnych zaliczek ani historycznych ustawień podatku. Obecny model zakłada hlavní; indywidualny předpis może być wyższy.</p>\n {% set fp = forecast.parameters or {} %}\n <p>{{ fp.get(\'source_note\', \'Brak zweryfikowanych parametrów. Wpisz je z oficjalnego źródła.\') }}</p>\n <details class="forecast-details"><summary>Edytuj parametry roku docelowego</summary>\n <form method="post" action="{{ url_for(\'contribution_parameters_save\') }}" class="form-panel">\n <input type="hidden" name="target_year" value="{{ forecast.target_year }}">\n <div class="form-grid three">\n {% for key,label in [(\'cssz_min_base\',\'Minimalna podstawa miesięczna ČSSZ\'),(\'cssz_max_base\',\'Maksymalna podstawa miesięczna ČSSZ\'),(\'vzp_min_base\',\'Minimalna podstawa miesięczna VZP\'),(\'cssz_assessment_percent\',\'ČSSZ: procent dochodu\'),(\'cssz_rate_percent\',\'ČSSZ: stawka procentowa\'),(\'vzp_assessment_percent\',\'VZP: procent dochodu\'),(\'vzp_rate_percent\',\'VZP: stawka procentowa\')] %}\n <label class="field"><span>{{ label }}</span><input type="number" step="0.01" min="0" name="{{ key }}" value="{{ fp.get(key,\'\') }}" required></label>\n {% endfor %}\n <label class="field span-3"><span>Źródło / uzasadnienie parametrów</span><textarea name="source_note" rows="2">{{ fp.get(\'source_note\',\'\') }}</textarea></label>\n </div><div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz parametry {{ forecast.target_year }}</button></div>\n </form></details>\n</section>\n{% include \'contribution_forecast.html\' %}\n'
EMBEDDED_TEMPLATES['base.html'] = EMBEDDED_TEMPLATES['base.html'].replace('<a href="{{ url_for(\'contractors_list\') }}">Kontrahenci</a>', '')
EMBEDDED_TEMPLATES['base.html'] = EMBEDDED_TEMPLATES['base.html'].replace('<a href="{{ url_for(\'projects_list\') }}">Projekty</a>', '')

_settings_tax_v82 = EMBEDDED_TEMPLATES['settings.html']
_settings_tax_v82 = _settings_tax_v82.replace('{% block content %}', "{% block content %}{% include 'settings_tabs.html' %}{% if settings_tab == 'forecast' %}<div class=\"page-header\"><div><h1>Ustawienia prognozy składek</h1><p class=\"muted\">Wybrany rok źródłowy: {{ snapshot.year }}</p></div></div>{% include 'forecast_settings.html' %}{% else %}", 1)
_settings_tax_v82 = _settings_tax_v82.replace('{% endblock %}', '{% endif %}{% endblock %}', 1) if False else _settings_tax_v82
# Close the conditional only at the END of the content block, not at the title.
_pos = _settings_tax_v82.rfind('{% endblock %}')
_settings_tax_v82 = _settings_tax_v82[:_pos] + '{% endif %}' + _settings_tax_v82[_pos:]
EMBEDDED_TEMPLATES['settings.html'] = _settings_tax_v82
for _name in ('contractors_list.html','contractor_form.html','projects_list.html','project_form.html'):
    if _name in EMBEDDED_TEMPLATES:
        EMBEDDED_TEMPLATES[_name] = EMBEDDED_TEMPLATES[_name].replace(
            '{% block content %}', "{% block content %}<div class=\"settings-context\">Ustawienia / zarządzanie danymi</div>{% include 'settings_tabs.html' %}", 1)
EMBEDDED_TEMPLATES['taxes_dashboard.html'] = EMBEDDED_TEMPLATES['taxes_dashboard.html'].replace(
    '{% if snapshot.forecast_total > 0 %}', "{% include 'contribution_forecast.html' %}\\n{% if snapshot.forecast_total > 0 %}", 1)
EMBEDDED_TEMPLATES['taxes_dashboard.html'] = EMBEDDED_TEMPLATES['taxes_dashboard.html'].replace(
    'Rzeczywiście otrzymane przychody, procentowe koszty, podatek, ČSSZ i VZP.',
    "Przychody {{ 'według zapłaty' if snapshot.settings.revenue_basis == 'paid' else 'według wystawienia (wariant prognozowy)' }}, podatek, ČSSZ i VZP.")
EMBEDDED_TEMPLATES['taxes_dashboard.html'] = EMBEDDED_TEMPLATES['taxes_dashboard.html'].replace(
    '<h2>Minimalne miesięczne zaliczki</h2>', '<h2>Bieżące miesięczne zaliczki z ustawień</h2>')
EMBEDDED_CSS += r"""
.contribution-forecast{scroll-margin-top:95px}
.settings-tabs{display:flex;gap:8px;flex-wrap:wrap;padding:8px;background:#fff;border:1px solid #dfe4eb;border-radius:14px;margin-bottom:24px}
.settings-tabs a{padding:11px 15px;text-decoration:none;border-radius:9px;color:#334155;font-weight:600}
.settings-tabs a.active{background:#84183c;color:white}.settings-tabs a:hover{background:#f1e7ec;color:#761333}
.settings-context{font-size:13px;color:#64748b;margin-bottom:9px}.eyebrow{font-size:11px;letter-spacing:.09em;font-weight:800;color:#7a2042;margin-bottom:8px}
.forecast-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:18px 0}
.forecast-stats>div{padding:15px;border:1px solid #e2e8f0;background:#fafbfd;border-radius:10px}.forecast-stats span{display:block;color:#64748b;font-size:12px}.forecast-stats strong{display:block;font-size:22px;margin-top:6px}
.forecast-details{margin:15px 0}.forecast-details summary{cursor:pointer;font-weight:700;color:#59263c;padding:7px 0}
.forecast-table td small{display:block;max-width:380px;color:#64748b;margin-top:4px}.scenario-row{background:#fcf5ea}
@media(max-width:900px){.settings-tabs{display:grid;grid-template-columns:1fr 1fr;gap:4px}.settings-tabs a{padding:10px;font-size:13px}.forecast-stats{grid-template-columns:1fr}.forecast-table{min-width:740px}}
"""
EMBEDDED_TEMPLATES['settings.html'] = EMBEDDED_TEMPLATES['settings.html'].replace('<p class="muted">Wybrany rok źródłowy: {{ snapshot.year }}</p></div></div>', '<p class="muted">Wybrany rok źródłowy: {{ snapshot.year }}</p></div><form class="toolbar year-toolbar" method="get"><input type="hidden" name="tab" value="forecast"><input type="number" name="year" min="2020" max="2099" value="{{ snapshot.year }}"><button class="btn btn-light" type="submit">Pokaż rok</button></form></div>', 1)


# V8.3 UI: independent rate sources for invoice and cash receipt.
EMBEDDED_TEMPLATES['fx_controls.html'] = '<div class="fx-controls span-3" data-fx-currency="{{ fx_currency_id }}" data-fx-rate="{{ fx_rate_id }}" data-fx-business-date="{{ fx_business_date }}" data-fx-kind="{{ fx_kind }}" data-fx-endpoint="{{ url_for(\'cnb_rate_api\') }}">\n  <div class="form-grid three">\n    <label class="field"><span>Źródło kursu</span><select name="fx_mode" class="fx-mode">\n      {% set mode = fx_form_mode(fx_is_edit) %}\n      {% if fx_is_edit %}<option value="keep" {% if mode == \'keep\' %}selected{% endif %}>Zachowaj zapisany kurs</option>{% endif %}\n      <option value="cnb" {% if mode == \'cnb\' %}selected{% endif %}>Automatycznie — dzienny kurs ČNB</option>\n      <option value="manual" {% if mode == \'manual\' %}selected{% endif %}>Wpisz kurs ręcznie</option>\n    </select></label>\n    <label class="field"><span>{{ \'Data kursu faktury / DPH\' if fx_kind == \'invoice\' else \'Data kursu przychodu\' }}</span><input class="fx-date" type="date" name="fx_date" value="{{ request.form.get(\'fx_date\', fx_date_value) }}" {% if fx_kind != \'invoice\' %}readonly{% endif %} required>\n      <small>{{ \'Domyślnie data wykonania usługi. Zmień, gdy właściwe rozliczenie wymaga innej daty.\' if fx_kind == \'invoice\' else \'Ta sama data co otrzymanie dostępnych środków.\' }}</small></label>\n    <div class="field"><span>&nbsp;</span><button type="button" class="btn btn-light fx-fetch">Pobierz kurs ČNB</button></div>\n  </div>\n  <p class="fx-message" role="status" aria-live="polite">{{ \'Zapisany kurs zostaje bez zmian.\' if fx_is_edit else \'Wybierz walutę i datę.\' }}</p>\n  <small class="muted">Kurs oznacza CZK za 1 jednostkę waluty. To kurs dzienny, nie roczny „jednotný kurz”. Nie pobieramy wyceny USDT/USDC z tabeli USD.</small>\n</div>\n'
EMBEDDED_TEMPLATES['fx_settings.html'] = '{% extends \'base.html\' %}\n{% block title %}Kursy walut — Faktury OSVČ{% endblock %}\n{% block content %}\n{% include \'settings_tabs.html\' %}\n<div class="page-header"><div><h1>Kursy walut</h1><p class="muted">Dzienny kurs ČNB; osobna wycena dokumentu i otrzymanej zapłaty.</p></div></div>\n<section class="panel">\n<form method="post">\n<label class="checkbox-row"><input type="checkbox" name="default_auto" value="1" {% if default_auto %}checked{% endif %}> Automatycznie pobieraj kurs ČNB w nowych fakturach, wypłatach i płatnościach</label>\n<p>Na starych dokumentach domyślnie zachowywany jest zapisany kurs. Pobranie nowej tabeli nie zmienia kursów innych faktur ani ustawień podatkowych.</p>\n<button class="btn btn-primary" type="submit">Zapisz ustawienie</button>\n</form>\n</section>\n<section class="panel"><h2>Jak działa przeliczenie</h2>\n<p><strong>Faktura / DPH:</strong> domyślnie pobierany jest kurs na dzień wykonania usługi. Data może być zmieniona, jeśli właściwe zasady dla transakcji wymagają innego dnia. Wystawienie faktury w EUR nadal oznacza należność w EUR.</p>\n<p><strong>Przychód według zapłaty:</strong> osobny kurs z daty otrzymania pieniędzy zapiszesz w „Data i kurs zapłaty”. Nie zmienia on kursu używanego dla DPH. Przy powiązanym payoucie źródłem przychodu jest payout, nie druga wpłata.</p>\n<p><strong>Starsze płatności:</strong> bez osobnego kursu przychodu pozostawiamy wcześniejsze wyliczenie i ostrzeżenie. Uzupełnij datę oraz kurs, zamiast automatycznie przeliczać historię.</p>\n<p><strong>Publikacja ČNB:</strong> około 14:30 w czeskie dni robocze. Weekend i święto korzystają z tabeli obowiązującej na dany dzień. Brak dzisiejszej tabeli albo awaria nie oznaczają zgody na losowy kurs z innego dnia.</p>\n<p><strong>Jednotný kurz:</strong> dzienny kurs nie jest rocznym kursem podatkowym. Ta aktualizacja nie zmienia samodzielnie Twojej rocznej metody rozliczenia; stosuj wybraną metodę spójnie. Roczny jednotný kurz nie jest automatycznie wyliczany ani stosowany przez ten moduł.</p>\n<p><strong>Tokeny:</strong> dla USDT, USDC i innych kryptoaktywów wpisz udokumentowaną wycenę ręcznie. Jeżeli dokument źródłowy określa przychód rzeczywiście w USD, ewidencjonuj walutę tego przychodu zgodnie z dokumentem.</p>\n<p class="muted">Dane kursowe pochodzą wyłącznie z oficjalnego serwisu www.cnb.cz. Nie jest wymagany płatny abonament ani klucz API. Rozpoznanie obowiązku DPH pozostaje osobną decyzją.</p>\n</section>\n{% endblock %}\n'
EMBEDDED_TEMPLATES['invoice_payment.html'] = '{% extends \'base.html\' %}\n{% block title %}Data i kurs zapłaty — {{ invoice.invoice_number }}{% endblock %}\n{% block content %}\n<div class="page-header"><div><h1>Data i kurs zapłaty</h1><p class="muted">{{ invoice.invoice_number }} · {{ invoice.contractor_name }} · {{ totals.total_gross|money(invoice.currency) }}</p></div></div>\n<div class="callout info">Zapis dotyczy pełnej zapłaty faktury, a nie zaliczki ani częściowej płatności. Kurs otrzymanego przychodu jest oddzielny od kursu faktury / DPH.</div>\n<form method="post" class="panel form-panel">\n<div class="form-grid three">\n<label class="field"><span>Data otrzymania pieniędzy</span><input type="date" name="paid_date" value="{{ paid_date }}" required></label>\n<label class="field"><span>Waluta</span><input id="payment-currency" value="{{ invoice.currency }}" readonly></label>\n<label class="field"><span>Kurs zapłaty: CZK za 1 jednostkę</span><input id="payment-rate" name="czk_rate" type="number" step="any" min="0" value="{{ rate }}"></label>\n{% set fx_currency_id = \'payment-currency\' %}{% set fx_rate_id = \'payment-rate\' %}{% set fx_kind = \'payment\' %}{% set fx_business_date = \'paid_date\' %}{% set fx_is_edit = is_edit %}{% set fx_date_value = paid_date %}\n{% include \'fx_controls.html\' %}\n</div>\n<div class="form-actions"><a href="{{ url_for(\'invoice_view\',invoice_id=invoice.id) }}" class="btn btn-light">Anuluj</a><button class="btn btn-primary" type="submit">Zapisz zapłatę i kurs</button></div>\n</form>\n{% endblock %}\n'
EMBEDDED_TEMPLATES['invoice_form.html'] = '{% extends "base.html" %}\n{% block title %}{{ \'Edytuj fakturę\' if is_edit else \'Nowa faktura\' }} - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header">\n    <div><h1>{{ \'Edytuj fakturę \' ~ invoice.invoice_number if is_edit else \'Nowa faktura\' }}</h1><p class="muted">PDF zostanie wygenerowany w formacie A4.</p></div>\n</div>\n{% if not contractors %}<div class="callout warning"><strong>Najpierw dodaj kontrahenta.</strong> <a href="{{ url_for(\'contractor_new\') }}">Dodaj teraz</a>.</div>{% endif %}\n<form method="post" class="panel form-panel invoice-form" data-default-due-days="{{ company.default_due_days }}">\n    <h2>Dane dokumentu</h2>\n    <div class="form-grid three">\n        <label class="field span-2"><span>Kontrahent *</span><select id="contractor-select" name="contractor_id" required><option value="">-- wybierz --</option>{% for contractor in contractors %}<option value="{{ contractor.id }}" data-vat="{{ contractor.vat_id }}" data-country="{{ contractor.country }}" {% if invoice.contractor_id|string == contractor.id|string %}selected{% endif %}>{{ contractor.name }}{% if contractor.vat_id %} ({{ contractor.vat_id }}){% endif %}</option>{% endfor %}</select></label>\n        <div class="field field-button"><span>&nbsp;</span><a class="btn btn-light" href="{{ url_for(\'contractor_new\') }}">+ Nowy kontrahent</a></div>\n        <label class="field"><span>Data wystawienia *</span><input id="issue-date" type="date" name="issue_date" value="{{ invoice.issue_date }}" required></label>\n        <label class="field"><span>Data wykonania usługi *</span><input type="date" name="supply_date" value="{{ invoice.supply_date }}" required></label>\n        <label class="field"><span>Termin płatności *</span><input id="due-date" type="date" name="due_date" value="{{ invoice.due_date }}" required></label>\n        <label class="field"><span>Waluta</span><input id="invoice-currency" name="currency" value="{{ invoice.currency }}" maxlength="6" list="currency-list"><datalist id="currency-list"><option value="EUR"><option value="CZK"><option value="PLN"><option value="USD"></datalist></label>\n        <label class="field"><span>Kurs faktury / DPH do CZK</span><input id="czk-rate" type="number" step="any" min="0" name="czk_rate" value="{{ invoice.czk_rate }}" placeholder="np. 24,85"><small>1 jednostka waluty faktury = ... CZK</small></label>\n        <label class="field"><span>Język PDF</span><select name="language">{% for code, label in LANGUAGES.items() %}<option value="{{ code }}" {% if invoice.language == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n        <label class="field"><span>Tryb VAT</span><select id="tax-mode" name="tax_mode">{% for code, label in TAX_MODES.items() %}<option value="{{ code }}" {% if invoice.tax_mode == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n        <label class="field span-2"><span>Klasyfikacja DPH / VIES</span><select id="dph-category" name="dph_category">{% for code, label in DPH_CATEGORIES.items() %}<option value="{{ code }}" {% if invoice.dph_category == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n        <label class="field"><span>Sposób płatności</span><select name="payment_method">{% for code, label in PAYMENT_METHODS.items() %}<option value="{{ code }}" {% if invoice.payment_method == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n        <label class="field"><span>Projekt</span>\n            <select id="project-select">\n                <option value="">— wybierz zapisany projekt —</option>\n                {% for project in projects %}\n                <option value="{{ project.project_number }}" data-contractor="{{ project.contractor_id or \'\' }}" {% if invoice.order_number == project.project_number %}selected{% endif %}>{{ project.project_number }}{% if project.project_name %} — {{ project.project_name }}{% endif %}</option>\n                {% endfor %}\n            </select>\n            <small>Lista filtruje się po wybranym kontrahencie.</small>\n        </label>\n        <label class="field"><span>Numer projektu / zamówienia</span><input id="order-number" name="order_number" value="{{ invoice.order_number }}" placeholder="Wybierz projekt lub wpisz ręcznie"></label>\n        <label class="field"><span>Symbol / referencja płatności</span><input name="variable_symbol" value="{{ invoice.variable_symbol }}" placeholder="Wygeneruje się automatycznie"></label>\n    </div>\n    {% set fx_currency_id = \'invoice-currency\' %}{% set fx_rate_id = \'czk-rate\' %}{% set fx_kind = \'invoice\' %}{% set fx_business_date = \'supply_date\' %}{% set fx_is_edit = is_edit %}\n    {% set q = fx_quote(\'invoice\',invoice.id,\'document\') %}\n    {% set fx_date_value = q.requested_date if q else invoice.supply_date %}\n    {% include \'fx_controls.html\' %}\n    <div id="dph-hint" class="callout info dph-hint">Program sprawdzi VAT ID kontrahenta i tryb faktury.</div>\n    <p class="small muted">Automatyczna klasyfikacja jest podpowiedzią. Dla usług związanych z nieruchomością lub budową, zaliczek, korekt oraz nietypowych transakcji wybierz „Do ręcznego sprawdzenia”.</p>\n\n    <hr>\n    <div class="panel-header"><h2>Pozycje faktury</h2><button id="add-item" class="btn btn-light btn-small" type="button">+ Dodaj pozycję</button></div>\n    <div class="table-wrap invoice-items-wrap">\n        <table class="invoice-items">\n            <thead><tr><th>Opis</th><th>Ilość</th><th>Jedn.</th><th>Cena jedn.</th><th class="vat-column">VAT %</th><th>Wartość</th><th></th></tr></thead>\n            <tbody id="invoice-items-body">\n                {% for item in items %}\n                <tr class="invoice-item-row">\n                    <td><input name="item_description" value="{{ item.description }}" placeholder="Opis usługi" required></td>\n                    <td><input class="qty-input" type="number" step="0.01" min="0.01" name="item_quantity" value="{{ item.quantity }}" required></td>\n                    <td><input name="item_unit" value="{{ item.unit }}" placeholder="h"></td>\n                    <td><input class="price-input" type="number" step="0.01" min="0" name="item_unit_price" value="{{ item.unit_price }}" required></td>\n                    <td class="vat-column"><input class="vat-input" type="number" step="0.01" min="0" max="100" name="item_vat_rate" value="{{ item.vat_rate or 0 }}"></td>\n                    <td class="line-total">0.00</td>\n                    <td><button class="icon-button remove-item" type="button" title="Usuń pozycję">×</button></td>\n                </tr>\n                {% endfor %}\n            </tbody>\n            <tfoot><tr><td colspan="5" class="total-label">Razem</td><td id="invoice-total">0.00 {{ invoice.currency }}</td><td></td></tr></tfoot>\n        </table>\n    </div>\n\n    <div id="reverse-charge-section" class="form-grid one"><label class="field"><span>Adnotacja reverse charge</span><textarea name="reverse_charge_note" rows="3">{{ invoice.reverse_charge_note }}</textarea></label></div>\n    <label class="field"><span>Uwagi na fakturze</span><textarea name="notes" rows="4">{{ invoice.notes }}</textarea></label>\n    <div class="form-actions"><a class="btn btn-ghost" href="{{ url_for(\'invoices_list\') }}">Anuluj</a><button class="btn btn-primary" type="submit" {% if not contractors %}disabled{% endif %}>{{ \'Zapisz zmiany\' if is_edit else \'Wystaw fakturę\' }}</button></div>\n</form>\n\n<template id="invoice-item-template"><tr class="invoice-item-row"><td><input name="item_description" placeholder="Opis usługi" required></td><td><input class="qty-input" type="number" step="0.01" min="0.01" name="item_quantity" value="1" required></td><td><input name="item_unit" value="h"></td><td><input class="price-input" type="number" step="0.01" min="0" name="item_unit_price" required></td><td class="vat-column"><input class="vat-input" type="number" step="0.01" min="0" max="100" name="item_vat_rate" value="0"></td><td class="line-total">0.00</td><td><button class="icon-button remove-item" type="button">×</button></td></tr></template>\n{% endblock %}\n'
EMBEDDED_TEMPLATES['prop_payout_form.html'] = '{% extends "base.html" %}\n{% block title %}{{ \'Edytuj payout\' if is_edit else \'Nowy payout\' }} - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header"><div><h1>{{ \'Edytuj payout\' if is_edit else \'Nowy payout\' }}</h1><p class="muted">Wpisuj kwotę należną Tobie po profit split, przed opłatą operatora.</p></div></div>\n<form method="post" class="panel form-panel" id="prop-payout-form" data-is-edit="{{ 1 if is_edit else 0 }}">\n<div class="form-grid three">\n  <label class="field"><span>Firma *</span><select name="prop_firm_id" id="prop-firm-select" required>{% for f in firms %}<option value="{{ f.id }}" data-contractor="{{ f.contractor_id or \'\' }}" data-tax="{{ f.default_tax_classification }}" data-status="{{ f.qualification_status }}" data-dph="{{ f.dph_treatment }}" data-name="{{ f.name|e }}" data-notes="{{ f.notes|e }}" {% if payout.prop_firm_id|string == f.id|string %}selected{% endif %}>{{ f.name }}</option>{% endfor %}</select></label>\n  <label class="field"><span>Konto / identyfikator payoutu</span><input name="payout_identifier" value="{{ payout.payout_identifier }}" placeholder="np. account ID / payout #"></label>\n  <label class="field"><span>Data otrzymania dostępnych środków *</span><input type="date" name="received_date" value="{{ payout.received_date }}" required><small>Data na Rise/WorkMarket/wallecie, nie późniejszy przelew na własny bank.</small></label>\n  <div id="prop-firm-profile-note" class="callout info span-3" hidden></div>\n\n  <label class="field"><span>Waluta *</span><input id="payout-currency" name="currency" value="{{ payout.currency }}" maxlength="8" required placeholder="USD, EUR, USDT"></label>\n  <label class="field"><span>Kwota należna po profit split *</span><input id="payout-gross" type="number" step="0.00000001" min="0" name="gross_amount" value="{{ payout.gross_amount }}" required><small>Nie wpisuj wirtualnego wyniku rachunku. LucidFlex: 1 000 USD wyniku przy 90/10 → wpisz 900 USD.</small></label>\n  <label class="field"><span>Opłata operatora</span><input id="payout-fee" type="number" step="0.00000001" min="0" name="operator_fee" value="{{ payout.operator_fee }}"></label>\n\n  <label class="field"><span>Kwota otrzymana netto *</span><input id="payout-net" type="number" step="0.00000001" min="0" name="net_amount" value="{{ payout.net_amount }}" required><small>Informacyjnie; nie pomniejsza przychodu przy kosztach procentowych.</small></label>\n  <label class="field"><span>Kurs 1 jednostki waluty do CZK *</span><input id="payout-rate" type="number" step="0.000001" min="0.000001" name="czk_rate" value="{{ payout.czk_rate }}" required></label>\n  <label class="field"><span>Przychód w CZK</span><input id="payout-income-czk" value="{{ payout.income_czk }}" readonly><small>Kwota po split × kurs; przed opłatą operatora.</small></label>\n\n  <label class="field span-2"><span>Powiązana faktura</span><select name="linked_invoice_id" id="prop-invoice-select"><option value="">— bez faktury —</option>{% for inv in invoices %}<option value="{{ inv.id }}" data-contractor="{{ inv.contractor_id }}" {% if payout.linked_invoice_id|string == inv.id|string %}selected{% endif %}>{{ inv.invoice_number }} — {{ inv.contractor_name }} — {{ inv.total|money(inv.currency) }} — {{ inv.supply_date|date_pl }}</option>{% endfor %}</select><small>Powiązana faktura i payout są jednym przychodem. Faktura nie zostanie doliczona drugi raz.</small></label>\n  <div class="field"><span>&nbsp;</span><a class="btn btn-light" href="{{ url_for(\'invoice_new\') }}">Wystaw nową fakturę</a></div>\n\n  <label class="field"><span>Kwalifikacja PIT</span><select name="tax_classification" id="prop-tax-classification">{% for code,label in PROP_TAX_CLASSIFICATIONS.items() %}<option value="{{ code }}" {% if payout.tax_classification == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n  <label class="field"><span>Status kwalifikacji</span><select name="qualification_status" id="prop-qualification-status">{% for code,label in PROP_QUALIFICATION_STATUSES.items() %}<option value="{{ code }}" {% if payout.qualification_status == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n  <label class="field"><span>DPH – osobna kwalifikacja</span><select name="dph_treatment" id="prop-dph-treatment">{% for code,label in PROP_DPH_TREATMENTS.items() %}<option value="{{ code }}" {% if payout.dph_treatment == code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n  {% set fx_currency_id = \'payout-currency\' %}{% set fx_rate_id = \'payout-rate\' %}{% set fx_kind = \'payout\' %}{% set fx_business_date = \'received_date\' %}{% set fx_is_edit = is_edit %}{% set fx_date_value = payout.received_date %}\n  {% include \'fx_controls.html\' %}\n  <label class="field span-3"><span>Notatka</span><textarea name="notes" rows="4">{{ payout.notes }}</textarea></label>\n</div>\n<div class="callout warning"><strong>Ważne:</strong> opłata operatora, challenge fee oraz składki ČSSZ/VZP nie są dodatkowo odejmowane, gdy dla tej grupy stosujesz koszty procentowe. Dla konta live nie używaj automatycznie profilu LucidFlex – najpierw przeanalizuj nową umowę i ustaw rozliczenie ręczne lub oddzielną firmę.</div>\n<div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz payout</button><a class="btn btn-ghost" href="{{ url_for(\'prop_firms_dashboard\') }}">Anuluj</a></div>\n</form>\n{% if is_edit %}<div class="danger-zone"><span></span><form method="post" action="{{ url_for(\'prop_payout_delete\', payout_id=payout.id) }}" onsubmit="return confirm(\'Usunąć ten payout?\')"><button class="btn btn-danger" type="submit">Usuń payout</button></form></div>{% endif %}\n{% endblock %}\n'
EMBEDDED_TEMPLATES['invoice_detail.html'] = '{% extends "base.html" %}\n{% block title %}{{ invoice.invoice_number }} - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header"><div><h1>{{ invoice.invoice_number }}</h1><p class="muted">{{ invoice.contractor_name }} · {{ invoice.issue_date|date_pl }}</p></div><div class="button-row"><a class="btn btn-primary" href="{{ url_for(\'invoice_pdf\', invoice_id=invoice.id) }}">Pobierz PDF A4</a><a class="btn btn-light" href="{{ url_for(\'invoice_edit\', invoice_id=invoice.id) }}">Edytuj</a></div></div>\n{% if dph_info.kind == \'eu_service\' %}<div class="callout info"><strong>DPH / UE:</strong> ta faktura zostanie ujęta w Souhrnné hlášení za {{ dph_info.period_label }} z kodem plnění 3. Podstawowy termin: {{ dph_info.due_date|date_pl }}. <a href="{{ url_for(\'dph_dashboard\', year=invoice.supply_date[:4], month=invoice.supply_date[5:7]) }}">Otwórz raport</a>.</div>{% elif dph_info.warning %}<div class="callout warning"><strong>DPH do sprawdzenia:</strong> {{ dph_info.warning }}</div>{% endif %}\n{% if linked_payout %}<div class="callout info"><strong>Prop firma:</strong> faktura jest powiązana z payoutem {{ linked_payout.firm_name }} otrzymanym {{ linked_payout.received_date|date_pl }}. W kalkulatorze przychodu liczony jest payout {{ linked_payout.income_czk|money(\'CZK\') }}, a faktura nie jest liczona drugi raz. <a href="{{ url_for(\'prop_payout_edit\', payout_id=linked_payout.id) }}">Otwórz payout</a>.</div>{% endif %}<div class="detail-grid">\n<section class="panel"><div class="panel-header"><h2>Dane faktury</h2><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == \'paid\' else \'Nieopłacona\' }}</span></div><dl class="details">\n<div><dt>Kontrahent</dt><dd>{{ invoice.contractor_name }}</dd></div><div><dt>VAT ID</dt><dd>{{ invoice.contractor_vat_id or \'—\' }}</dd></div>\n<div><dt>Data wystawienia</dt><dd>{{ invoice.issue_date|date_pl }}</dd></div><div><dt>Data usługi</dt><dd>{{ invoice.supply_date|date_pl }}</dd></div>\n<div><dt>Termin płatności</dt><dd>{{ invoice.due_date|date_pl }}</dd></div><div><dt>Data zapłaty</dt><dd>{{ invoice.paid_date|date_pl if invoice.paid_date else \'—\' }}</dd></div>\n<div><dt>Tryb VAT</dt><dd>{{ TAX_MODES[invoice.tax_mode] }}</dd></div><div><dt>Kurs do CZK</dt><dd>{{ invoice.czk_rate or (\'1\' if invoice.currency == \'CZK\' else \'—\') }}</dd></div>\n<div><dt>DPH / VIES</dt><dd>{{ dph_info.label }}</dd></div></dl></section>\n<section class="panel total-card"><span class="muted">Do zapłaty</span><strong>{{ totals.total_gross|money(invoice.currency) }}</strong>{% if dph_info.value_czk is not none %}<small>Do ewidencji: {{ dph_info.value_czk|money(\'CZK\') }}</small>{% endif %}</section>\n</div>\n<section class="panel"><h2>Pozycje</h2><div class="table-wrap"><table><thead><tr><th>Opis</th><th>Ilość</th><th>Jedn.</th><th>Cena</th>{% if invoice.tax_mode == \'vat\' %}<th>VAT</th>{% endif %}<th>Wartość</th></tr></thead><tbody>{% for item in items %}<tr><td>{{ item.description }}</td><td>{{ item.quantity_decimal }}</td><td>{{ item.unit }}</td><td>{{ item.unit_price_decimal|money(invoice.currency) }}</td>{% if invoice.tax_mode == \'vat\' %}<td>{{ item.vat_rate_decimal }}%</td>{% endif %}<td>{{ item.gross|money(invoice.currency) }}</td></tr>{% endfor %}</tbody></table></div></section>\n{% if invoice.reverse_charge_note or invoice.notes %}<section class="panel">{% if invoice.reverse_charge_note %}<h2>Adnotacja</h2><p>{{ invoice.reverse_charge_note }}</p>{% endif %}{% if invoice.notes %}<h2>Uwagi</h2><p class="preline">{{ invoice.notes }}</p>{% endif %}</section>{% endif %}\n<section class="panel"><div class="panel-header"><h2>Kursy zapisane dla tej faktury</h2>\n<a class="btn btn-light" href="{{ url_for(\'invoice_payment\',invoice_id=invoice.id) }}">Data i kurs zapłaty</a></div>\n{% set dq = fx_quote(\'invoice\',invoice.id,\'document\') %}{% set pq = fx_quote(\'invoice\',invoice.id,\'payment\') %}\n<p><strong>Faktura / DPH:</strong> {{ invoice.czk_rate or \'brak\' }} CZK za 1 {{ invoice.currency }}.\n{% if dq %}Data kursu {{ dq.requested_date|date_pl }}, źródło {{ \'ČNB, tabela z \' ~ (dq.published_date|date_pl) if dq.source == \'cnb\' else \'kurs ręczny / CZK\' }}.{% else %}Starszy zapis bez metadanych; nie został automatycznie zmieniony.{% endif %}</p>\n{% if linked_payout %}<p><strong>PIT:</strong> przychód jest liczony z powiązanego payoutu — nie z tej faktury drugi raz.</p>\n{% elif pq %}<p><strong>Przychód według zapłaty:</strong> {{ pq.rate }} CZK za 1 {{ pq.currency }}, otrzymano {{ pq.requested_date|date_pl }}.\nŹródło: {{ \'ČNB, tabela z \' ~ (pq.published_date|date_pl) if pq.source == \'cnb\' else \'kurs ręczny / CZK\' }}.</p>\n{% elif invoice.status == \'paid\' and invoice.currency != \'CZK\' %}<p class="fx-error">Brak osobnego kursu zapłaty. Do czasu jego uzupełnienia kalkulator zachowuje starszy kurs faktury i pokazuje ostrzeżenie.</p>\n{% endif %}\n<p class="muted">Zmiana kursu otrzymanej zapłaty nie zmienia kursu dokumentu, DPH ani kwoty do zapłaty w walucie.</p></section>\n<div class="danger-zone paid-actions">\n    {% if invoice.status == \'paid\' %}\n    <form method="post" action="{{ url_for(\'invoice_toggle_paid\', invoice_id=invoice.id) }}"><button class="btn btn-light" type="submit">Oznacz jako nieopłaconą</button></form>\n    {% else %}\n    <form method="post" action="{{ url_for(\'invoice_toggle_paid\', invoice_id=invoice.id) }}" class="paid-date-form"><label class="field"><span>Data otrzymania zapłaty</span><input type="date" name="paid_date" value="{{ today }}" required></label><button class="btn btn-primary" type="submit">Oznacz jako opłaconą</button></form>\n    {% endif %}\n    <form method="post" action="{{ url_for(\'invoice_delete\', invoice_id=invoice.id) }}" onsubmit="return confirm(\'Usunąć fakturę {{ invoice.invoice_number }}?\')"><button class="btn btn-danger" type="submit">Usuń fakturę</button></form>\n</div>\n{% endblock %}\n'
EMBEDDED_TEMPLATES['settings_tabs.html'] = '<nav class="settings-tabs" aria-label="Sekcje ustawień">\n <a class="{% if request.endpoint == \'settings_page\' and settings_tab|default(\'tax\') == \'tax\' %}active{% endif %}" href="{{ url_for(\'settings_page\', year=selected_year|default(snapshot.year if snapshot is defined else current_year), tab=\'tax\') }}">Podatki i składki</a>\n <a class="{% if settings_tab|default(\'\') == \'contractors\' or request.endpoint in [\'contractors_list\',\'contractor_new\',\'contractor_edit\'] %}active{% endif %}" href="{{ url_for(\'contractors_list\') }}">Kontrahenci</a>\n <a class="{% if settings_tab|default(\'\') == \'projects\' or request.endpoint in [\'projects_list\',\'project_new\',\'project_edit\'] %}active{% endif %}" href="{{ url_for(\'projects_list\') }}">Projekty</a>\n <a class="{% if settings_tab|default(\'\') == \'forecast\' %}active{% endif %}" href="{{ url_for(\'settings_page\', year=selected_year|default(snapshot.year if snapshot is defined else current_year), tab=\'forecast\') }}">Prognoza przyszłych składek</a>\n <a class="{% if settings_tab|default(\'\') == \'fx\' %}active{% endif %}" href="{{ url_for(\'fx_settings_page\') }}">Kursy walut</a>\n</nav>\n'
EMBEDDED_JS += '\n// V8.3: debounced, race-safe requests. Server revalidates all automatic rates.\ndocument.addEventListener(\'DOMContentLoaded\', function(){\n  document.querySelectorAll(\'.fx-controls\').forEach(function(box){\n    const form=box.closest(\'form\'), currency=document.getElementById(box.dataset.fxCurrency),\n      rate=document.getElementById(box.dataset.fxRate), dateInput=box.querySelector(\'.fx-date\'),\n      business=form.querySelector(\'[name="\'+box.dataset.fxBusinessDate+\'"]\'),\n      mode=box.querySelector(\'.fx-mode\'), button=box.querySelector(\'.fx-fetch\'),\n      status=box.querySelector(\'.fx-message\');\n    if(!form||!currency||!rate||!mode||!dateInput) return;\n    const savedRate=rate.value, savedCurrency=currency.value.trim().toUpperCase(), savedDate=dateInput.value;\n    let serial=0, controller=null, timer=null, pending=false, failed=false;\n    function message(text,error=false){status.textContent=text;status.classList.toggle(\'fx-error\',error);}\n    async function refresh(){\n      clearTimeout(timer); const token=++serial;\n      if(controller) controller.abort(); controller=null; pending=false; failed=false;\n      rate.setCustomValidity(\'\'); rate.readOnly=mode.value!==\'manual\';\n      button.disabled=false; rate.required=mode.value!==\'keep\';\n      if(mode.value===\'keep\'){\n        rate.value=savedRate;\n        if(currency.value.trim().toUpperCase()!==savedCurrency || dateInput.value!==savedDate){\n          failed=true; message(\'Zmieniono walutę lub datę. Wybierz ponownie ČNB albo kurs ręczny.\',true);\n        } else message(\'Zachowano wcześniej zapisany kurs. Otwarcie dokumentu nie przelicza historii.\');\n        rate.dispatchEvent(new Event(\'input\',{bubbles:true})); return;\n      }\n      if(mode.value===\'manual\'){\n        message(\'Wpisz własny udokumentowany kurs. Nie zostanie nadpisany przez automat.\');\n        return;\n      }\n      rate.value=\'\'; rate.dispatchEvent(new Event(\'input\',{bubbles:true}));\n      const code=currency.value.trim().toUpperCase(), day=dateInput.value;\n      if(!code||!day){failed=true;message(\'Uzupełnij walutę i datę.\');return;}\n      pending=true; message(\'Pobieranie kursu ČNB…\'); button.disabled=true;\n      controller=new AbortController();\n      try{\n        const query=new URLSearchParams({currency:code,date:day});\n        const result=await fetch(box.dataset.fxEndpoint+\'?\'+query.toString(),{signal:controller.signal,headers:{\'Accept\':\'application/json\'},cache:\'no-store\'});\n        const data=await result.json();\n        if(token!==serial) return;\n        if(!result.ok||!data.ok) throw new Error(data.error||\'Nie udało się pobrać kursu ČNB.\');\n        rate.value=data.rate;\n        rate.dispatchEvent(new Event(\'input\',{bubbles:true}));\n        message(data.notice);\n      }catch(error){\n        if(token!==serial||error.name===\'AbortError\') return;\n        failed=true; rate.value=\'\'; rate.dispatchEvent(new Event(\'input\',{bubbles:true}));\n        message(error.message||\'Błąd połączenia. Spróbuj ponownie albo wybierz kurs ręczny.\',true);\n      }finally{if(token===serial){pending=false;button.disabled=false;}}\n    }\n    mode.addEventListener(\'change\',refresh);\n    dateInput.addEventListener(\'change\',refresh);\n    currency.addEventListener(\'input\',function(){\n      if(controller) controller.abort(); serial++; pending=true;\n      if(mode.value===\'cnb\') {rate.value=\'\';message(\'Oczekiwanie na kod waluty…\');}\n      clearTimeout(timer);timer=setTimeout(refresh,400);\n    });\n    if(business) business.addEventListener(\'change\',function(){dateInput.value=business.value;refresh();});\n    button.addEventListener(\'click\',function(){mode.value=\'cnb\';refresh();});\n    form.addEventListener(\'submit\',function(event){\n      if(pending || (failed&&mode.value!==\'manual\')){\n        event.preventDefault();message(pending?\'Poczekaj na pobranie kursu.\':\'Nie zapisano kursu. Pobierz ponownie albo wybierz kurs ręczny.\',true);\n      }\n    });\n    refresh();\n  });\n});\n'
EMBEDDED_CSS += '\n.fx-controls{padding:16px;border:1px solid #cbd5e1;border-radius:12px;background:#f8fafc;margin:12px 0 18px}\n.fx-message{font-size:14px;line-height:1.5;margin:10px 0;color:#155e75;min-height:22px}\n.fx-error{color:#a32a18!important;font-weight:600}\n.fx-controls .form-grid{margin:0}.fx-controls .field{min-width:0}\ninput[readonly]{background:#f1f5f9}\n@media(max-width:900px){.fx-controls{padding:12px}.fx-fetch{width:100%}}\n'

APP_DIR = Path(__file__).resolve().parent


def _default_data_dir() -> Path:
    if CLOUD_MODE:
        return Path("/tmp") / "FakturyOSVC"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / "FakturyOSVC"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FakturyOSVC"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "FakturyOSVC"


DATA_DIR = Path(os.environ.get("INVOICE_APP_DATA", str(_default_data_dir())))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("INVOICE_APP_DB", str(DATA_DIR / "invoice_app.db")))

# PDF-y są przechowywane w łatwo dostępnym katalogu Dokumenty.
# Strukturę tworzy program automatycznie:
# Dokumenty/Faktury OSVC/Nazwa kontrahenta/Rok/Faktura_....pdf
DEFAULT_PDF_DIR = (DATA_DIR / "invoices") if CLOUD_MODE else (Path.home() / "Documents" / "Faktury OSVC")
INVOICE_PDF_DIR = Path(os.environ.get("INVOICE_PDF_DIR", str(DEFAULT_PDF_DIR)))
INVOICE_PDF_DIR.mkdir(parents=True, exist_ok=True)



_REMOTE_SYNC_LOCK = threading.RLock()


def _supabase_object_url(object_path: str, authenticated: bool = False) -> str:
    safe_bucket = urllib.parse.quote(SUPABASE_BUCKET, safe="")
    safe_path = urllib.parse.quote(object_path.lstrip("/"), safe="/")
    if authenticated:
        return f"{SUPABASE_URL}/storage/v1/object/authenticated/{safe_bucket}/{safe_path}"
    return f"{SUPABASE_URL}/storage/v1/object/{safe_bucket}/{safe_path}"


def _supabase_headers(content_type: str | None = None) -> dict[str, str]:
    """
    Obsługuje oba formaty kluczy Supabase:
    - nowy secret key: sb_secret_... -> tylko nagłówek apikey
    - legacy service_role JWT: eyJ... -> apikey + Authorization Bearer
    """
    headers = {"apikey": SUPABASE_SERVICE_KEY}
    if not SUPABASE_SERVICE_KEY.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def download_remote_database() -> bool:
    """Pobiera najnowszą bazę z prywatnego Supabase Storage przy starcie."""
    if not REMOTE_DB_ENABLED:
        return False
    with _REMOTE_SYNC_LOCK:
        req = urllib.request.Request(
            _supabase_object_url(SUPABASE_DB_OBJECT, authenticated=True),
            headers=_supabase_headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print("Supabase: brak zdalnej bazy — przy pierwszym uruchomieniu zostanie utworzona nowa.")
                return False
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            print(f"Supabase: nie udało się pobrać bazy (HTTP {exc.code}): {body}")
            return False
        except Exception as exc:
            print(f"Supabase: nie udało się pobrać bazy: {exc}")
            return False

        if not data:
            return False
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = DB_PATH.with_suffix(".download")
        temp_path.write_bytes(data)
        # Szybka kontrola, czy to rzeczywiście poprawna baza SQLite.
        try:
            check = sqlite3.connect(temp_path)
            check.execute("PRAGMA schema_version").fetchone()
            check.close()
        except Exception as exc:
            try:
                temp_path.unlink()
            except OSError:
                pass
            print(f"Supabase: pobrany plik nie jest poprawną bazą SQLite: {exc}")
            return False
        os.replace(temp_path, DB_PATH)
        print(f"Supabase: pobrano bazę ({len(data)} bajtów).")
        return True


def _sqlite_snapshot_file() -> Path:
    """Tworzy spójny snapshot otwartej/aktywnej bazy SQLite."""
    fd, temp_name = tempfile.mkstemp(prefix="faktury_cloud_", suffix=".sqlite3")
    os.close(fd)
    target = Path(temp_name)
    source = sqlite3.connect(DB_PATH)
    destination = sqlite3.connect(target)
    try:
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def upload_file_to_supabase(local_path: Path, object_path: str) -> bool:
    if not REMOTE_DB_ENABLED or not local_path.exists():
        return False
    with _REMOTE_SYNC_LOCK:
        data = local_path.read_bytes()
        req = urllib.request.Request(
            _supabase_object_url(object_path, authenticated=False),
            data=data,
            headers={
                **_supabase_headers("application/octet-stream"),
                "x-upsert": "true",
                "cache-control": "no-cache",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                response.read()
            return True
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            print(f"Supabase: upload nieudany HTTP {exc.code}: {body}")
            return False
        except Exception as exc:
            print(f"Supabase: upload nieudany: {exc}")
            return False


def upload_remote_database() -> bool:
    """Serializuj snapshot i upload w jednym procesie (jeden worker)."""
    with _REMOTE_SYNC_LOCK:
        return _upload_remote_database_locked()


def _upload_remote_database_locked() -> bool:
    """Wysyła spójny snapshot bazy po każdej zmianie."""
    if not REMOTE_DB_ENABLED or not DB_PATH.exists():
        return False
    snapshot = None
    try:
        snapshot = _sqlite_snapshot_file()
        ok = upload_file_to_supabase(snapshot, SUPABASE_DB_OBJECT)
        if ok:
            print("Supabase: baza zsynchronizowana.")
        return ok
    finally:
        if snapshot is not None:
            try:
                snapshot.unlink()
            except OSError:
                pass


# Na darmowym Renderze lokalny dysk znika po uśpieniu/restarcie.
# Dlatego pobieramy bazę zanim uruchomimy migracje i init_db().
REMOTE_DB_DOWNLOADED = download_remote_database()
if CLOUD_MODE and REMOTE_DB_ENABLED and not REMOTE_DB_DOWNLOADED:
    raise RuntimeError(
        "Nie pobrano istniejącej bazy z Supabase. Wersja V8.2 nie utworzy ani nie nadpisze pustej bazy w chmurze. "
        "Sprawdź połączenie, klucz i obiekt data/invoice_app.db; dla nowego wdrożenia wgraj prywatnie posiadaną bazę."
    )

def _migrate_legacy_database() -> str | None:
    """Kopiuje bazę ze starej wieloplikowej wersji przy pierwszym uruchomieniu."""
    if DB_PATH.exists():
        return None
    candidates = [
        APP_DIR / "data" / "invoice_app.db",
        APP_DIR / "invoice_app.db",
        Path.cwd() / "data" / "invoice_app.db",
        Path.cwd() / "invoice_app.db",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or resolved == DB_PATH.resolve():
            continue
        seen.add(resolved)
        if candidate.is_file():
            shutil.copy2(candidate, DB_PATH)
            return str(candidate)
    return None


MIGRATED_FROM = None if '--self-test' in sys.argv else _migrate_legacy_database()

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=CLOUD_MODE)
app.jinja_loader = DictLoader(EMBEDDED_TEMPLATES)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


@app.route("/static/<path:filename>", endpoint="static")
def embedded_static(filename: str):
    if filename == "style.css":
        return Response(EMBEDDED_CSS, mimetype="text/css")
    if filename == "app.js":
        return Response(EMBEDDED_JS, mimetype="application/javascript")
    abort(404)

TWOPLACES = Decimal("0.01")

LANGUAGES = {
    "pl": "Polski",
    "de": "Deutsch",
    "en": "English",
    "cs": "Čeština",
}

TAX_MODES = {
    "reverse_charge": "Reverse charge (odwrotne obciążenie)",
    "no_vat": "Bez VAT",
    "vat": "VAT naliczany",
}

PAYMENT_METHODS = {
    "bank_transfer": "Przelew bankowy",
    "cash": "Gotówka",
    "card": "Karta",
}


DPH_CATEGORIES = {
    "auto": "Automatycznie",
    "eu_service": "Usługa B2B w UE - SH VIES, kod 3",
    "not_required": "Nie ujmuj w SH VIES",
    "review": "Do ręcznego sprawdzenia",
}

EXPENSE_CATEGORIES = {
    "tools": "Narzędzia i sprzęt",
    "materials": "Materiały",
    "transport": "Transport i paliwo",
    "rental": "Wynajem sprzętu / lokalu",
    "phone": "Telefon i internet",
    "accounting": "Księgowość",
    "insurance": "Ubezpieczenia",
    "fees": "Opłaty urzędowe",
    "software": "Oprogramowanie i reklama",
    "other": "Inne",
}

EXPENSE_DPH_OBLIGATIONS = {
    "none": "Brak szczególnego obowiązku DPH",
    "foreign_service": "Usługa kupiona z zagranicy - możliwe přiznání k DPH",
    "review": "Do ręcznego sprawdzenia",
}

TAX_PAYMENT_TYPES = {
    'income_tax': 'Podatek dochodowy',
    'cssz': 'ČSSZ',
    'vzp': 'VZP',
}


PROP_TAX_CLASSIFICATIONS = {
    "s7_60": "§7 – działalność / koszty procentowe 60%",
    "s7_40": "§7 – inna samostatná činnost / koszty 40%",
    "exclude": "Nie uwzględniaj automatycznie – rozliczenie ręczne",
}

PROP_QUALIFICATION_STATUSES = {
    "confirmed": "Potwierdzona",
    "recommended": "Rekomendowana ocena dokumentów – niewiążąca",
    "unconfirmed": "Kwalifikacja do potwierdzenia",
}

PROP_DPH_TREATMENTS = {
    "review": "DPH do osobnej kwalifikacji",
    "outside_eu": "Usługa dla kontrahenta spoza UE – bez SH VIES",
    "eu_service": "Usługa B2B UE – możliwe SH VIES",
    "not_required": "Nie ujmuj w DPH / SH VIES",
}


LUCIDFLEX_PROFILE_NAME = "LucidFlex"
LUCIDFLEX_MIGRATION_KEY = "v8_1_lucidflex_agreement_2025_11_28"
LUCID_LEGACY_DEFAULT_NOTE = (
    "Kwalifikacja do potwierdzenia. Domyślnie przyjęto §7/60% wyłącznie jako konfigurowalne założenie kalkulatora."
)
LUCIDFLEX_PROFILE_NOTES = (
    "Umowa Lucid Trading Group LLC z 28.11.2025 – LucidFlex, konto symulowane należące do podatnika. "
    "Reward jest wynagrodzeniem za dane generowane podczas symulowanego tradingu (wstęp C i §11). "
    "Rekomendowana, niewiążąca kwalifikacja: §7 OSVČ, koszty procentowe 60%, pod warunkiem wykonywania "
    "usługi w ramach živnosti volné. Przychód stanowi payout należny po podziale 90/10, przed rzeczywistą "
    "opłatą operatora; wirtualny wynik rachunku nie jest przychodem. Data przychodu to dzień otrzymania "
    "lub udostępnienia środków u operatora, a późniejszy transfer na własny bank/portfel nie tworzy "
    "drugiego przychodu. Konto live wymaga osobnej analizy nowej umowy."
)

MONTHS = {
    1: "Styczeń", 2: "Luty", 3: "Marzec", 4: "Kwiecień", 5: "Maj", 6: "Czerwiec",
    7: "Lipiec", 8: "Sierpień", 9: "Wrzesień", 10: "Październik", 11: "Listopad", 12: "Grudzień",
}

EU_VAT_PREFIXES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK",
}

MOJE_DANE_SHV_URL = "https://adisspr.mfcr.cz/pmd/epo/novy/DPH_SHV"
VIES_URL = "https://ec.europa.eu/taxation_customs/vies/"

PDF_PAYMENT_METHODS = {
    "pl": {"bank_transfer": "Przelew bankowy", "cash": "Gotówka", "card": "Karta"},
    "de": {"bank_transfer": "Banküberweisung", "cash": "Barzahlung", "card": "Kartenzahlung"},
    "en": {"bank_transfer": "Bank transfer", "cash": "Cash", "card": "Card"},
    "cs": {"bank_transfer": "Bankovní převod", "cash": "Hotově", "card": "Kartou"},
}

PDF_LABELS: dict[str, dict[str, str]] = {
    "pl": {
        "invoice": "FAKTURA",
        "invoice_no": "Numer faktury",
        "seller": "Sprzedawca",
        "buyer": "Nabywca",
        "issue_date": "Data wystawienia",
        "supply_date": "Data wykonania usługi",
        "due_date": "Termin płatności",
        "order_no": "Numer zamówienia",
        "payment_method": "Sposób płatności",
        "variable_symbol": "Symbol płatności",
        "item_no": "Lp.",
        "description": "Opis",
        "quantity": "Ilość",
        "unit": "Jedn.",
        "unit_price": "Cena jedn.",
        "vat_rate": "VAT %",
        "net": "Netto",
        "vat": "VAT",
        "gross": "Brutto",
        "total_due": "Do zapłaty",
        "bank_details": "Dane do płatności",
        "bank": "Bank",
        "account": "Rachunek",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
        "notes": "Uwagi",
        "page": "Strona",
        "company_id": "IČO / nr firmy",
        "vat_id": "DIČ / VAT ID",
        "email": "E-mail",
        "phone": "Telefon",
        "reverse_charge_default": "Odwrotne obciążenie - VAT rozlicza nabywca. Reverse charge.",
        "tax_mode": "Rozliczenie podatku",
        "reverse_charge": "Reverse charge",
        "no_vat": "Bez VAT",
    },
    "de": {
        "invoice": "RECHNUNG",
        "invoice_no": "Rechnungsnummer",
        "seller": "Leistungserbringer",
        "buyer": "Leistungsempfänger",
        "issue_date": "Rechnungsdatum",
        "supply_date": "Leistungsdatum",
        "due_date": "Fälligkeitsdatum",
        "order_no": "Bestellnummer",
        "payment_method": "Zahlungsart",
        "variable_symbol": "Zahlungsreferenz",
        "item_no": "Pos.",
        "description": "Beschreibung",
        "quantity": "Menge",
        "unit": "Einheit",
        "unit_price": "Einzelpreis",
        "vat_rate": "USt. %",
        "net": "Netto",
        "vat": "USt.",
        "gross": "Brutto",
        "total_due": "Zahlbetrag",
        "bank_details": "Bankverbindung",
        "bank": "Bank",
        "account": "Kontonummer",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
        "notes": "Hinweise",
        "page": "Seite",
        "company_id": "IČO / Firmen-ID",
        "vat_id": "UID / VAT ID",
        "email": "E-Mail",
        "phone": "Telefon",
        "reverse_charge_default": "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).",
        "tax_mode": "Umsatzsteuer",
        "reverse_charge": "Reverse Charge",
        "no_vat": "Ohne Umsatzsteuer",
    },
    "en": {
        "invoice": "INVOICE",
        "invoice_no": "Invoice number",
        "seller": "Supplier",
        "buyer": "Customer",
        "issue_date": "Issue date",
        "supply_date": "Service date",
        "due_date": "Due date",
        "order_no": "Order number",
        "payment_method": "Payment method",
        "variable_symbol": "Payment reference",
        "item_no": "No.",
        "description": "Description",
        "quantity": "Qty",
        "unit": "Unit",
        "unit_price": "Unit price",
        "vat_rate": "VAT %",
        "net": "Net",
        "vat": "VAT",
        "gross": "Gross",
        "total_due": "Amount due",
        "bank_details": "Payment details",
        "bank": "Bank",
        "account": "Account",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
        "notes": "Notes",
        "page": "Page",
        "company_id": "Company ID",
        "vat_id": "VAT ID",
        "email": "Email",
        "phone": "Phone",
        "reverse_charge_default": "Reverse charge - VAT is payable by the recipient.",
        "tax_mode": "VAT treatment",
        "reverse_charge": "Reverse charge",
        "no_vat": "No VAT",
    },
    "cs": {
        "invoice": "FAKTURA",
        "invoice_no": "Číslo faktury",
        "seller": "Dodavatel",
        "buyer": "Odběratel",
        "issue_date": "Datum vystavení",
        "supply_date": "Datum poskytnutí služby",
        "due_date": "Datum splatnosti",
        "order_no": "Číslo objednávky",
        "payment_method": "Způsob úhrady",
        "variable_symbol": "Variabilní symbol",
        "item_no": "Č.",
        "description": "Popis",
        "quantity": "Množství",
        "unit": "Jedn.",
        "unit_price": "Jedn. cena",
        "vat_rate": "DPH %",
        "net": "Základ",
        "vat": "DPH",
        "gross": "Celkem",
        "total_due": "K úhradě",
        "bank_details": "Platební údaje",
        "bank": "Banka",
        "account": "Účet",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
        "notes": "Poznámky",
        "page": "Strana",
        "company_id": "IČO",
        "vat_id": "DIČ",
        "email": "E-mail",
        "phone": "Telefon",
        "reverse_charge_default": "Daň odvede zákazník (reverse charge).",
        "tax_mode": "Režim DPH",
        "reverse_charge": "Přenesení daňové povinnosti",
        "no_vat": "Bez DPH",
    },
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


@app.teardown_appcontext
def close_db(_exception: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS company (
            id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL,
            address TEXT NOT NULL DEFAULT '', postal_code TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '',
            company_id TEXT NOT NULL DEFAULT '', vat_id TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
            bank_name TEXT NOT NULL DEFAULT '', bank_account TEXT NOT NULL DEFAULT '',
            iban TEXT NOT NULL DEFAULT '', bic TEXT NOT NULL DEFAULT '',
            default_currency TEXT NOT NULL DEFAULT 'EUR', default_due_days INTEGER NOT NULL DEFAULT 14,
            invoice_prefix TEXT NOT NULL DEFAULT 'FV', default_language TEXT NOT NULL DEFAULT 'de',
            default_tax_mode TEXT NOT NULL DEFAULT 'reverse_charge',
            default_reverse_charge_note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS contractors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            address TEXT NOT NULL DEFAULT '', postal_code TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '', company_id TEXT NOT NULL DEFAULT '', vat_id TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contractor_id INTEGER,
            project_number TEXT NOT NULL,
            project_name TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contractor_id) REFERENCES contractors(id) ON DELETE SET NULL,
            UNIQUE(contractor_id, project_number)
        );
        CREATE TABLE IF NOT EXISTS invoice_sequences (year INTEGER PRIMARY KEY, next_number INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT NOT NULL UNIQUE,
            invoice_year INTEGER NOT NULL, sequence_no INTEGER NOT NULL, contractor_id INTEGER NOT NULL,
            issue_date TEXT NOT NULL, supply_date TEXT NOT NULL, due_date TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'EUR', language TEXT NOT NULL DEFAULT 'de',
            tax_mode TEXT NOT NULL DEFAULT 'reverse_charge', payment_method TEXT NOT NULL DEFAULT 'bank_transfer',
            variable_symbol TEXT NOT NULL DEFAULT '', order_number TEXT NOT NULL DEFAULT '',
            reverse_charge_note TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unpaid', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contractor_id) REFERENCES contractors(id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL, position INTEGER NOT NULL,
            description TEXT NOT NULL, quantity TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'h',
            unit_price TEXT NOT NULL, vat_rate TEXT NOT NULL DEFAULT '0',
            FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, expense_date TEXT NOT NULL,
            supplier TEXT NOT NULL DEFAULT '', description TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other', document_number TEXT NOT NULL DEFAULT '',
            amount TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'CZK', czk_rate TEXT NOT NULL DEFAULT '1',
            business_percent TEXT NOT NULL DEFAULT '100', payment_method TEXT NOT NULL DEFAULT 'bank_transfer',
            dph_obligation TEXT NOT NULL DEFAULT 'none', supplier_vat_id TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS dph_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, period_year INTEGER NOT NULL, period_month INTEGER NOT NULL,
            filing_kind TEXT NOT NULL DEFAULT 'SHV', status TEXT NOT NULL DEFAULT 'draft',
            filed_date TEXT NOT NULL DEFAULT '', confirmation_number TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(period_year, period_month, filing_kind)
        );
        """
    )

    def columns(table: str) -> set[str]:
        return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}

    migrations: list[tuple[str, str, str]] = []
    for column, definition in (
        ("tax_first_name", "TEXT NOT NULL DEFAULT 'Jakub Karol'"),
        ("tax_last_name", "TEXT NOT NULL DEFAULT 'Maćkowski'"),
        ("tax_office_code", "TEXT NOT NULL DEFAULT '451'"),
        ("tax_branch_code", "TEXT NOT NULL DEFAULT '2002'"),
    ):
        if column not in columns("company"):
            migrations.append(("company", column, definition))
    for column, definition in (
        ("czk_rate", "TEXT NOT NULL DEFAULT ''"),
        ("dph_category", "TEXT NOT NULL DEFAULT 'auto'"),
    ):
        if column not in columns("invoices"):
            migrations.append(("invoices", column, definition))

    if migrations and DB_PATH.exists() and DB_PATH.stat().st_size:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"przed_aktualizacja_V3_{timestamp}.sqlite3"
        backup_conn = sqlite3.connect(backup_path)
        try:
            db.backup(backup_conn)
        finally:
            backup_conn.close()
    for table, column, definition in migrations:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    existing = db.execute("SELECT id FROM company WHERE id = 1").fetchone()
    if existing is None:
        db.execute(
            """INSERT INTO company (
                id, name, address, postal_code, city, country, company_id, vat_id,
                email, phone, bank_name, bank_account, iban, bic,
                default_currency, default_due_days, invoice_prefix, default_language,
                default_tax_mode, default_reverse_charge_note
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, '', '', '', '', '', '', 'EUR', 14, 'FV', 'de', 'reverse_charge', ?)""",
            ("Jakub Karol Maćkowski", "Uralská 689/7", "160 00", "Praha 6 - Bubeneč",
             "Česká republika", "19506180", "CZ686656725",
             "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge). / Daň odvede zákazník."),
        )
    db.commit()


def init_tax_module() -> None:
    db = get_db()
    invoice_columns = {row["name"] for row in db.execute("PRAGMA table_info(invoices)").fetchall()}
    tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    tax_columns = {row["name"] for row in db.execute("PRAGMA table_info(tax_settings)").fetchall()} if "tax_settings" in tables else set()
    required_tax_columns = {"employment_tax_withheld", "cssz_main_from_date", "cssz_secondary_min_monthly_base", "vzp_main_from_date"}
    changes_needed = (
        "paid_date" not in invoice_columns
        or "tax_settings" not in tables
        or "tax_payments" not in tables
        or not required_tax_columns.issubset(tax_columns)
    )
    if changes_needed and DB_PATH.exists() and DB_PATH.stat().st_size:
        backup_path = BACKUP_DIR / f"przed_aktualizacja_V5_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
        backup_conn = sqlite3.connect(backup_path)
        try:
            db.backup(backup_conn)
        finally:
            backup_conn.close()
    if "paid_date" not in invoice_columns:
        db.execute("ALTER TABLE invoices ADD COLUMN paid_date TEXT NOT NULL DEFAULT ''")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tax_settings (
            year INTEGER PRIMARY KEY,
            activity_start_date TEXT NOT NULL DEFAULT '', activity_end_date TEXT NOT NULL DEFAULT '',
            revenue_basis TEXT NOT NULL DEFAULT 'paid',
            expense_percent TEXT NOT NULL DEFAULT '60', expense_limit TEXT NOT NULL DEFAULT '1200000',
            income_tax_credit TEXT NOT NULL DEFAULT '30840', employment_tax_base TEXT NOT NULL DEFAULT '0',
            employment_tax_withheld TEXT NOT NULL DEFAULT '0', tax_threshold TEXT NOT NULL DEFAULT '1762812',
            cssz_mode TEXT NOT NULL DEFAULT 'main', cssz_main_from_date TEXT NOT NULL DEFAULT '',
            cssz_threshold_annual TEXT NOT NULL DEFAULT '117521',
            cssz_threshold_reduction_month TEXT NOT NULL DEFAULT '9794',
            cssz_assessment_percent TEXT NOT NULL DEFAULT '55', cssz_rate_percent TEXT NOT NULL DEFAULT '29.2',
            cssz_min_monthly_base TEXT NOT NULL DEFAULT '17139',
            cssz_secondary_min_monthly_base TEXT NOT NULL DEFAULT '5387',
            cssz_monthly_advance TEXT NOT NULL DEFAULT '5005',
            vzp_mode TEXT NOT NULL DEFAULT 'main', vzp_main_from_date TEXT NOT NULL DEFAULT '',
            vzp_assessment_percent TEXT NOT NULL DEFAULT '50', vzp_rate_percent TEXT NOT NULL DEFAULT '13.5',
            vzp_min_monthly_base TEXT NOT NULL DEFAULT '24483.50', vzp_monthly_advance TEXT NOT NULL DEFAULT '3306',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tax_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, payment_date TEXT NOT NULL,
            payment_type TEXT NOT NULL, amount TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    tax_columns = {row["name"] for row in db.execute("PRAGMA table_info(tax_settings)").fetchall()}
    additions = {
        "employment_tax_withheld": "TEXT NOT NULL DEFAULT '0'",
        "cssz_main_from_date": "TEXT NOT NULL DEFAULT ''",
        "cssz_secondary_min_monthly_base": "TEXT NOT NULL DEFAULT '5387'",
        "vzp_main_from_date": "TEXT NOT NULL DEFAULT ''",
    }
    added_any = False
    for name, definition in additions.items():
        if name not in tax_columns:
            db.execute(f"ALTER TABLE tax_settings ADD COLUMN {name} {definition}")
            added_any = True

    # Jednorazowa migracja ustawień użytkownika: lipiec 2026 vedlejší, od sierpnia hlavní.
    if added_any:
        row_2026 = db.execute("SELECT * FROM tax_settings WHERE year=2026").fetchone()
        if row_2026 is not None:
            current = dict(row_2026)
            updates = {
                "income_tax_credit": "30840" if str(current.get("income_tax_credit", "0")).strip() in {"", "0", "0.0", "0.00"} else current.get("income_tax_credit"),
                "employment_tax_withheld": current.get("employment_tax_withheld", "0") or "0",
                "cssz_main_from_date": "2026-08-01",
                "cssz_secondary_min_monthly_base": "5387",
                "vzp_main_from_date": "2026-08-01",
            }
            if current.get("cssz_mode") != "custom":
                updates.update({"cssz_mode": "mixed", "cssz_min_monthly_base": "17139", "cssz_monthly_advance": "5005"})
            if current.get("vzp_mode") != "custom":
                updates.update({"vzp_mode": "mixed", "vzp_min_monthly_base": "24483.50", "vzp_monthly_advance": "3306"})
            keys = list(updates)
            db.execute("UPDATE tax_settings SET " + ",".join(f"{key}=?" for key in keys) + " WHERE year=2026", tuple(updates[key] for key in keys))
    db.commit()



def migrate_lucidflex_profile(db: sqlite3.Connection) -> int:
    """
    Jednorazowo migruje wcześniejszy profil „Lucid Trading” do „LucidFlex”.
    Zwraca id docelowej firmy. Nie nadpisuje ręcznie zmienionej klasyfikacji payoutu.
    """
    rows = [
        dict(row) for row in db.execute(
            """SELECT * FROM prop_firms
               WHERE lower(replace(name, ' ', '')) IN ('lucidtrading','lucidflex','lucidflex(lucidtradinggroupllc)')
               ORDER BY id"""
        ).fetchall()
    ]
    flex = next((row for row in rows if "lucidflex" in row["name"].lower().replace(" ", "")), None)
    legacy = [row for row in rows if row["name"].strip().lower() == "lucid trading"]

    if flex is None and legacy:
        flex = legacy.pop(0)
        db.execute("UPDATE prop_firms SET name=? WHERE id=?", (LUCIDFLEX_PROFILE_NAME, flex["id"]))
        flex["name"] = LUCIDFLEX_PROFILE_NAME
    elif flex is None:
        cursor = db.execute(
            """INSERT INTO prop_firms
               (name, default_tax_classification, qualification_status, dph_treatment, notes, active)
               VALUES (?,?,?,?,?,1)""",
            (LUCIDFLEX_PROFILE_NAME, "s7_60", "recommended", "review", LUCIDFLEX_PROFILE_NOTES),
        )
        flex_id = int(cursor.lastrowid)
        flex = dict(db.execute("SELECT * FROM prop_firms WHERE id=?", (flex_id,)).fetchone())

    flex_id = int(flex["id"])

    # Jeżeli po wcześniejszych wersjach istnieją dwa profile, połącz payouty i zachowaj kontrahenta.
    for old in legacy:
        if not flex.get("contractor_id") and old.get("contractor_id"):
            db.execute("UPDATE prop_firms SET contractor_id=? WHERE id=?", (old["contractor_id"], flex_id))
            flex["contractor_id"] = old["contractor_id"]
        db.execute("UPDATE prop_payouts SET prop_firm_id=? WHERE prop_firm_id=?", (flex_id, old["id"]))
        db.execute("DELETE FROM prop_firms WHERE id=?", (old["id"],))

    current = dict(db.execute("SELECT * FROM prop_firms WHERE id=?", (flex_id,)).fetchone())
    updates: dict[str, Any] = {}

    # Zachowaj ręcznie wybrany wariant 40%/wyłączenie. Domyślny stary wariant 60% pozostaje 60%.
    if current.get("default_tax_classification") in {"", None, "s7_60"}:
        updates["default_tax_classification"] = "s7_60"
    # Stary status V8 był „unconfirmed”; po analizie umowy przechodzi na „recommended”.
    if current.get("qualification_status") in {"", None, "unconfirmed"}:
        updates["qualification_status"] = "recommended"
    if not current.get("dph_treatment"):
        updates["dph_treatment"] = "review"

    existing_notes = (current.get("notes") or "").strip()
    if "28.11.2025" not in existing_notes or "Konto live wymaga osobnej analizy" not in existing_notes:
        if not existing_notes or existing_notes == LUCID_LEGACY_DEFAULT_NOTE:
            updates["notes"] = LUCIDFLEX_PROFILE_NOTES
        elif existing_notes != LUCIDFLEX_PROFILE_NOTES:
            updates["notes"] = existing_notes + "\n\n" + LUCIDFLEX_PROFILE_NOTES

    updates["name"] = LUCIDFLEX_PROFILE_NAME
    if updates:
        keys = list(updates)
        db.execute(
            "UPDATE prop_firms SET " + ",".join(f"{key}=?" for key in keys) + ",updated_at=CURRENT_TIMESTAMP WHERE id=?",
            tuple(updates[key] for key in keys) + (flex_id,),
        )

    # Dotychczasowe wpisy o domyślnej konfiguracji mogą bezpiecznie przejść z unconfirmed na recommended.
    # Nie zmieniamy payoutów, które użytkownik ustawił na 40%, exclude albo confirmed.
    db.execute(
        """UPDATE prop_payouts
           SET qualification_status='recommended', updated_at=CURRENT_TIMESTAMP
           WHERE prop_firm_id=? AND tax_classification='s7_60' AND qualification_status='unconfirmed'""",
        (flex_id,),
    )
    return flex_id

def init_prop_firms_module() -> None:
    """Dodaje moduł prop firm bez naruszania istniejących danych."""
    db = get_db()
    tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    tax_columns = {row["name"] for row in db.execute("PRAGMA table_info(tax_settings)").fetchall()} if "tax_settings" in tables else set()
    required_tax_columns = {"expense_40_percent", "expense_40_limit", "forecast_welding_revenue", "forecast_prop_revenue"}
    schema_change = "prop_firms" not in tables or "prop_payouts" not in tables or not required_tax_columns.issubset(tax_columns)
    lucid_migration_applied = False
    if "app_migrations" in tables:
        lucid_migration_applied = db.execute(
            "SELECT 1 FROM app_migrations WHERE migration_key=?",
            (LUCIDFLEX_MIGRATION_KEY,),
        ).fetchone() is not None
    migration_needed = schema_change or not lucid_migration_applied

    migration_backup: Path | None = None
    if migration_needed and DB_PATH.exists() and DB_PATH.stat().st_size:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        migration_backup = BACKUP_DIR / f"przed_aktualizacja_V8_1_LucidFlex_{timestamp}.sqlite3"
        target = sqlite3.connect(migration_backup)
        try:
            db.backup(target)
        finally:
            target.close()
        if REMOTE_DB_ENABLED:
            upload_file_to_supabase(migration_backup, f"backups/migration_V8_1_LucidFlex_{timestamp}.sqlite3")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS prop_firms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            contractor_id INTEGER,
            default_tax_classification TEXT NOT NULL DEFAULT 's7_60',
            qualification_status TEXT NOT NULL DEFAULT 'unconfirmed',
            dph_treatment TEXT NOT NULL DEFAULT 'review',
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contractor_id) REFERENCES contractors(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS prop_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prop_firm_id INTEGER NOT NULL,
            payout_identifier TEXT NOT NULL DEFAULT '',
            received_date TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            gross_amount TEXT NOT NULL,
            operator_fee TEXT NOT NULL DEFAULT '0',
            net_amount TEXT NOT NULL,
            czk_rate TEXT NOT NULL,
            income_czk TEXT NOT NULL,
            linked_invoice_id INTEGER UNIQUE,
            tax_classification TEXT NOT NULL DEFAULT 's7_60',
            qualification_status TEXT NOT NULL DEFAULT 'unconfirmed',
            dph_treatment TEXT NOT NULL DEFAULT 'review',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (prop_firm_id) REFERENCES prop_firms(id) ON DELETE RESTRICT,
            FOREIGN KEY (linked_invoice_id) REFERENCES invoices(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prop_payouts_received_date ON prop_payouts(received_date);
        CREATE INDEX IF NOT EXISTS idx_prop_payouts_firm ON prop_payouts(prop_firm_id);
        CREATE TABLE IF NOT EXISTS app_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)

    tax_columns = {row["name"] for row in db.execute("PRAGMA table_info(tax_settings)").fetchall()}
    additions = {
        "expense_40_percent": "TEXT NOT NULL DEFAULT '40'",
        "expense_40_limit": "TEXT NOT NULL DEFAULT '800000'",
        "forecast_welding_revenue": "TEXT NOT NULL DEFAULT '0'",
        "forecast_prop_revenue": "TEXT NOT NULL DEFAULT '0'",
    }
    for column, definition in additions.items():
        if column not in tax_columns:
            db.execute(f"ALTER TABLE tax_settings ADD COLUMN {column} {definition}")

    seed_firms = (
        (
            "MyFundedFutures (MFF)", "s7_60", "recommended", "review",
            "Rekomendowana kwalifikacja na podstawie przeanalizowanej umowy: regularny przychód §7, koszty 60%. Ocena niewiążąca dla urzędu.",
        ),
    )
    for name, classification, status, dph, notes in seed_firms:
        exists = db.execute("SELECT id FROM prop_firms WHERE lower(name)=lower(?)", (name,)).fetchone()
        if exists is None:
            db.execute(
                "INSERT INTO prop_firms (name, default_tax_classification, qualification_status, dph_treatment, notes, active) VALUES (?,?,?,?,?,1)",
                (name, classification, status, dph, notes),
            )

    if not lucid_migration_applied:
        migrate_lucidflex_profile(db)
        db.execute(
            "INSERT OR REPLACE INTO app_migrations (migration_key, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
            (LUCIDFLEX_MIGRATION_KEY,),
        )
    db.commit()

# V8.2: initialization is at the end of this file.


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_company() -> dict[str, Any]:
    row = get_db().execute("SELECT * FROM company WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("Brak danych firmy.")
    return dict(row)


def parse_decimal(value: Any, field_name: str = "wartość") -> Decimal:
    raw = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not raw:
        raw = "0"
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Nieprawidłowa {field_name}: {value}") from exc


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def format_decimal(value: Any, decimals: int = 2) -> str:
    try:
        number = parse_decimal(value)
    except ValueError:
        return str(value)
    formatted = f"{number:,.{decimals}f}"
    return formatted.replace(",", " ")


@app.template_filter("money")
def money_filter(value: Any, currency: str = "") -> str:
    return f"{format_decimal(value)} {currency}".strip()


@app.template_filter("date_pl")
def date_pl_filter(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return value or ""


def normalize_vat_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def split_eu_vat_id(value: Any) -> tuple[str, str] | None:
    normalized = normalize_vat_id(value)
    if len(normalized) < 4:
        return None
    prefix = normalized[:2]
    if prefix == "GR":
        prefix = "EL"
    if prefix not in EU_VAT_PREFIXES:
        return None
    number = normalized[2:]
    if not number or len(number) > 12 or not re.fullmatch(r"[A-Z0-9]+", number):
        return None
    return prefix, number


def decimal_rate(currency: Any, rate: Any) -> Decimal | None:
    code = str(currency or "").strip().upper()
    if code == "CZK":
        return Decimal("1")
    try:
        parsed = parse_decimal(rate, "kurs do CZK")
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def month_bounds(year: int, month: int) -> tuple[str, str]:
    return date(year, month, 1).isoformat(), date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def period_label(year: int, month: int) -> str:
    return f"{MONTHS.get(month, str(month))} {year}"


def dph_due_date(year: int, month: int) -> date:
    return date(year + 1, 1, 25) if month == 12 else date(year, month + 1, 25)


def effective_dph_kind(invoice: dict[str, Any]) -> tuple[str, str | None]:
    manual = invoice.get("dph_category", "auto")
    vat_split = split_eu_vat_id(invoice.get("contractor_vat_id", ""))
    if manual == "not_required":
        return "not_required", None
    if manual == "review":
        return "review", "Faktura została oznaczona do ręcznej oceny."
    if manual == "eu_service":
        if invoice.get("tax_mode") != "reverse_charge":
            return "review", "Wybrano SH VIES, ale faktura nie ma trybu reverse charge."
        if vat_split is None or vat_split[0] == "CZ":
            return "review", "Brak poprawnego VAT ID kontrahenta z innego państwa UE."
        return "eu_service", None
    if invoice.get("tax_mode") == "reverse_charge":
        if vat_split is not None and vat_split[0] != "CZ":
            return "eu_service", None
        return "review", "Reverse charge bez rozpoznanego VAT ID z innego państwa UE."
    return "not_required", None


def invoice_value_czk(invoice: dict[str, Any], totals: dict[str, Any]) -> Decimal | None:
    rate = decimal_rate(invoice.get("currency", ""), invoice.get("czk_rate", ""))
    return q2(totals["total_net"] * rate) if rate is not None else None


def invoice_dph_info(invoice: dict[str, Any], totals: dict[str, Any]) -> dict[str, Any]:
    kind, warning = effective_dph_kind(invoice)
    value = invoice_value_czk(invoice, totals)
    try:
        service_date = date.fromisoformat(invoice["supply_date"])
    except (ValueError, TypeError):
        service_date = date.today()
    if kind == "eu_service" and value is None:
        warning = "Uzupełnij kurs waluty do CZK przed przygotowaniem Souhrnné hlášení."
    labels = {"eu_service": "SH VIES - usługa UE, kod 3", "not_required": "Bez SH VIES", "review": "Do sprawdzenia"}
    return {
        "kind": kind, "label": labels[kind], "warning": warning, "value_czk": value,
        "period_label": period_label(service_date.year, service_date.month),
        "due_date": dph_due_date(service_date.year, service_date.month).isoformat(),
    }


def build_dph_month_report(year: int, month: int) -> dict[str, Any]:
    start, end = month_bounds(year, month)
    db = get_db()
    invoice_rows = db.execute(
        """SELECT i.*, c.name AS contractor_name, c.vat_id AS contractor_vat_id, c.country AS contractor_country
           FROM invoices i JOIN contractors c ON c.id=i.contractor_id
           WHERE i.supply_date BETWEEN ? AND ? ORDER BY i.supply_date, i.id""", (start, end)
    ).fetchall()
    report_invoices: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    warnings: list[str] = []
    for row in invoice_rows:
        invoice = dict(row)
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
        totals = calculate_items([dict(item) for item in items], invoice["tax_mode"])
        kind, warning = effective_dph_kind(invoice)
        if kind == "review":
            warnings.append(f"{invoice['invoice_number']}: {warning}")
            continue
        if kind != "eu_service":
            continue
        vat_split = split_eu_vat_id(invoice.get("contractor_vat_id", ""))
        value_czk = invoice_value_czk(invoice, totals)
        rate = decimal_rate(invoice.get("currency", ""), invoice.get("czk_rate", ""))
        record = dict(invoice)
        record.update({"total_net": totals["total_net"], "value_czk": value_czk, "rate": rate})
        report_invoices.append(record)
        if vat_split is None or vat_split[0] == "CZ":
            warnings.append(f"{invoice['invoice_number']}: nieprawidłowy unijny VAT ID.")
            continue
        if value_czk is None:
            warnings.append(f"{invoice['invoice_number']}: uzupełnij kurs {invoice['currency']} do CZK.")
            continue
        key = vat_split
        group = groups.setdefault(key, {"country_code": key[0], "vat_number": key[1], "count": 0, "exact": Decimal("0")})
        group["count"] += 1
        group["exact"] += value_czk
    rows: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda item: (item["country_code"], item["vat_number"])):
        rows.append({**group, "value_czk": group["exact"].quantize(Decimal("1"), rounding=ROUND_CEILING)})
    total = sum((row["value_czk"] for row in rows), Decimal("0"))
    return {"invoices": report_invoices, "invoice_count": len(report_invoices), "rows": rows,
            "warnings": warnings, "total_czk": total, "block_export": bool(warnings) or not rows}


def parse_company_address(address: str) -> tuple[str, str, str]:
    value = (address or "").strip()
    match = re.match(r"^(.*?)\s+(\d+)(?:/(\d+[A-Za-z]?))?$", value)
    return (match.group(1).strip(), match.group(2), match.group(3) or "") if match else (value, "", "")


def build_shv_xml(year: int, month: int, report: dict[str, Any], company: dict[str, Any]) -> bytes:
    if report["block_export"]:
        raise ValueError("Raport zawiera błędy albo nie ma wierszy do eksportu.")
    dic = normalize_vat_id(company.get("vat_id", ""))
    if dic.startswith("CZ"):
        dic = dic[2:]
    if not dic or not dic.isdigit():
        raise ValueError("W danych firmy wpisz poprawne czeskie DIČ.")
    first_name = str(company.get("tax_first_name", "")).strip()
    last_name = str(company.get("tax_last_name", "")).strip()
    office = str(company.get("tax_office_code", "")).strip()
    branch = str(company.get("tax_branch_code", "")).strip()
    if not first_name or not last_name:
        raise ValueError("Uzupełnij imię i nazwisko podatnika w danych firmy.")
    if not office.isdigit() or not branch.isdigit():
        raise ValueError("Uzupełnij numery urzędu i placówki w danych firmy.")
    street, c_pop, c_orient = parse_company_address(company.get("address", ""))
    root = ET.Element("Pisemnost", {"nazevSW": "Faktury OSVC", "verzeSW": "4.0"})
    doc = ET.SubElement(root, "DPHSHV", {"verzePis": "02.01.04"})
    ET.SubElement(doc, "VetaD", {"dokument": "SHV", "k_uladis": "DPH", "shvies_forma": "R",
                                      "d_poddp": date.today().strftime("%d.%m.%Y"), "rok": str(year), "mesic": str(month)})
    subject = {"c_ufo": office, "c_pracufo": branch, "dic": dic, "typ_ds": "F",
               "prijmeni": last_name, "jmeno": first_name, "naz_obce": company.get("city", ""),
               "ulice": street, "psc": re.sub(r"\s+", "", company.get("postal_code", "")),
               "stat": "ČESKÁ REPUBLIKA", "sest_prijmeni": last_name, "sest_jmeno": first_name}
    if c_pop:
        subject["c_pop"] = c_pop
    if c_orient:
        subject["c_orient"] = c_orient
    phone = re.sub(r"\s+", "", company.get("phone", ""))
    if phone:
        subject["sest_telef"] = phone[:14]
    ET.SubElement(doc, "VetaP", subject)
    for index, row in enumerate(report["rows"], start=1):
        ET.SubElement(doc, "VetaR", {"por_c_stran": "1", "c_rad": str(index),
            "k_stat": row["country_code"], "c_vat": row["vat_number"], "k_pln_eu": "3",
            "pln_pocet": str(row["count"]), "pln_hodnota": str(int(row["value_czk"]))})
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def expense_value_czk(expense: dict[str, Any]) -> Decimal | None:
    rate = decimal_rate(expense.get("currency", ""), expense.get("czk_rate", ""))
    if rate is None:
        return None
    try:
        return q2(parse_decimal(expense.get("amount"), "kwota") * rate)
    except ValueError:
        return None


def month_revenue_czk(year: int, month: int) -> tuple[Decimal, int]:
    start, end = month_bounds(year, month)
    db = get_db()
    rows = db.execute("SELECT * FROM invoices WHERE supply_date BETWEEN ? AND ?", (start, end)).fetchall()
    total = Decimal("0")
    missing = 0
    for row in rows:
        invoice = dict(row)
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
        totals = calculate_items([dict(item) for item in items], invoice["tax_mode"])
        rate = decimal_rate(invoice["currency"], invoice.get("czk_rate", ""))
        if rate is None:
            missing += 1
        else:
            total += q2(totals["total_gross"] * rate)
    return q2(total), missing


def get_pending_dph_reminder() -> dict[str, Any] | None:
    db = get_db()
    periods = db.execute("SELECT DISTINCT substr(supply_date,1,7) AS period FROM invoices ORDER BY period").fetchall()
    for row in periods:
        try:
            year, month = map(int, row["period"].split("-"))
        except (ValueError, AttributeError):
            continue
        report = build_dph_month_report(year, month)
        if not report["invoices"] and not report["warnings"]:
            continue
        filing = db.execute("SELECT status FROM dph_filings WHERE period_year=? AND period_month=? AND filing_kind='SHV'", (year, month)).fetchone()
        if filing is None or filing["status"] != "filed":
            return {"year": year, "month": month, "invoice_count": report["invoice_count"],
                    "period_label": period_label(year, month), "due_date": dph_due_date(year, month).isoformat()}
    return None

def calculate_items(items: list[dict[str, Any]], tax_mode: str) -> dict[str, Any]:
    calculated: list[dict[str, Any]] = []
    total_net = Decimal("0")
    total_vat = Decimal("0")
    total_gross = Decimal("0")

    for item in items:
        quantity = parse_decimal(item.get("quantity"), "ilość")
        unit_price = parse_decimal(item.get("unit_price"), "cena jednostkowa")
        vat_rate = parse_decimal(item.get("vat_rate"), "stawka VAT")
        if tax_mode != "vat":
            vat_rate = Decimal("0")
        net = q2(quantity * unit_price)
        vat = q2(net * vat_rate / Decimal("100"))
        gross = q2(net + vat)
        calculated_item = dict(item)
        calculated_item.update(
            {
                "quantity_decimal": quantity,
                "unit_price_decimal": unit_price,
                "vat_rate_decimal": vat_rate,
                "net": net,
                "vat": vat,
                "gross": gross,
            }
        )
        calculated.append(calculated_item)
        total_net += net
        total_vat += vat
        total_gross += gross

    return {
        "items": calculated,
        "total_net": q2(total_net),
        "total_vat": q2(total_vat),
        "total_gross": q2(total_gross),
    }


def get_invoice_bundle(invoice_id: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    db = get_db()
    invoice_row = db.execute(
        """
        SELECT i.*, c.name AS contractor_name, c.address AS contractor_address,
               c.postal_code AS contractor_postal_code, c.city AS contractor_city,
               c.country AS contractor_country, c.company_id AS contractor_company_id,
               c.vat_id AS contractor_vat_id, c.email AS contractor_email,
               c.phone AS contractor_phone
        FROM invoices i
        JOIN contractors c ON c.id = i.contractor_id
        WHERE i.id = ?
        """,
        (invoice_id,),
    ).fetchone()
    if invoice_row is None:
        abort(404)
    items_rows = db.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position, id",
        (invoice_id,),
    ).fetchall()
    invoice = dict(invoice_row)
    items = [dict(row) for row in items_rows]
    totals = calculate_items(items, invoice["tax_mode"])
    return invoice, items, totals


def next_invoice_identity(db: sqlite3.Connection, issue_date: str, prefix: str) -> tuple[str, int, int]:
    try:
        year = date.fromisoformat(issue_date).year
    except ValueError as exc:
        raise ValueError("Nieprawidłowa data wystawienia.") from exc

    sequence_row = db.execute(
        "SELECT next_number FROM invoice_sequences WHERE year = ?", (year,)
    ).fetchone()
    if sequence_row is None:
        sequence = 1
        db.execute(
            "INSERT INTO invoice_sequences (year, next_number) VALUES (?, ?)",
            (year, 2),
        )
    else:
        sequence = int(sequence_row["next_number"])
        db.execute(
            "UPDATE invoice_sequences SET next_number = ? WHERE year = ?",
            (sequence + 1, year),
        )
    clean_prefix = (prefix or "FV").strip() or "FV"
    return f"{clean_prefix}-{year}-{sequence:03d}", year, sequence


def invoice_form_from_request(existing_invoice: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    invoice = {
        "id": existing_invoice.get("id") if existing_invoice else None,
        "invoice_number": existing_invoice.get("invoice_number", "") if existing_invoice else "",
        "contractor_id": request.form.get("contractor_id", ""),
        "issue_date": request.form.get("issue_date", ""), "supply_date": request.form.get("supply_date", ""),
        "due_date": request.form.get("due_date", ""), "currency": request.form.get("currency", "EUR").strip().upper(),
        "language": request.form.get("language", "de"), "tax_mode": request.form.get("tax_mode", "reverse_charge"),
        "payment_method": request.form.get("payment_method", "bank_transfer"),
        "variable_symbol": request.form.get("variable_symbol", "").strip(),
        "order_number": request.form.get("order_number", "").strip(),
        "reverse_charge_note": request.form.get("reverse_charge_note", "").strip(),
        "notes": request.form.get("notes", "").strip(), "czk_rate": request.form.get("czk_rate", "").strip(),
        "dph_category": request.form.get("dph_category", "auto"),
        "status": existing_invoice.get("status", "unpaid") if existing_invoice else "unpaid",
    }
    descriptions = request.form.getlist("item_description"); quantities = request.form.getlist("item_quantity")
    units = request.form.getlist("item_unit"); unit_prices = request.form.getlist("item_unit_price")
    vat_rates = request.form.getlist("item_vat_rate")
    count = max(len(descriptions), len(quantities), len(units), len(unit_prices), len(vat_rates), 1)
    items: list[dict[str, Any]] = []
    for index in range(count):
        description = descriptions[index].strip() if index < len(descriptions) else ""
        quantity = quantities[index].strip() if index < len(quantities) else "1"
        unit = units[index].strip() if index < len(units) else "h"
        price = unit_prices[index].strip() if index < len(unit_prices) else "0"
        vat = vat_rates[index].strip() if index < len(vat_rates) else "0"
        if description or price:
            items.append({"description": description, "quantity": quantity or "1", "unit": unit or "szt.",
                          "unit_price": price or "0", "vat_rate": vat or "0"})
    if not items:
        items = [{"description": "", "quantity": "1", "unit": "h", "unit_price": "", "vat_rate": "0"}]
    return invoice, items


def validate_invoice(invoice: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    db = get_db()
    try:
        contractor_id = int(invoice["contractor_id"])
    except (TypeError, ValueError):
        errors.append("Wybierz kontrahenta.")
    else:
        if db.execute("SELECT 1 FROM contractors WHERE id=?", (contractor_id,)).fetchone() is None:
            errors.append("Wybrany kontrahent nie istnieje.")
    for key, label in (("issue_date", "data wystawienia"), ("supply_date", "data wykonania usługi"), ("due_date", "termin płatności")):
        try:
            date.fromisoformat(invoice.get(key, ""))
        except ValueError:
            errors.append(f"Uzupełnij poprawnie pole: {label}.")
    if not invoice.get("currency") or len(invoice["currency"]) > 6:
        errors.append("Podaj poprawny kod waluty.")
    if invoice.get("language") not in LANGUAGES:
        errors.append("Wybierz język faktury.")
    if invoice.get("tax_mode") not in TAX_MODES:
        errors.append("Wybierz sposób rozliczenia VAT.")
    if invoice.get("payment_method") not in PAYMENT_METHODS:
        errors.append("Wybierz sposób płatności.")
    if invoice.get("dph_category") not in DPH_CATEGORIES:
        errors.append("Wybierz klasyfikację DPH / VIES.")
    if invoice.get("currency") == "CZK":
        invoice["czk_rate"] = "1"
    elif invoice.get("czk_rate"):
        try:
            rate_value = parse_decimal(invoice["czk_rate"], "kurs")
            if not rate_value.is_finite() or rate_value <= 0:
                raise ValueError
        except ValueError:
            errors.append("Kurs do CZK musi być większy od zera.")
    valid = False
    for index, item in enumerate(items, start=1):
        if not item.get("description", "").strip():
            errors.append(f"Pozycja {index}: wpisz opis.")
            continue
        try:
            quantity = parse_decimal(item.get("quantity"), "ilość"); price = parse_decimal(item.get("unit_price"), "cena")
            vat = parse_decimal(item.get("vat_rate"), "stawka VAT")
        except ValueError as exc:
            errors.append(f"Pozycja {index}: {exc}"); continue
        if quantity <= 0: errors.append(f"Pozycja {index}: ilość musi być większa od zera.")
        if price < 0: errors.append(f"Pozycja {index}: cena nie może być ujemna.")
        if vat < 0 or vat > 100: errors.append(f"Pozycja {index}: stawka VAT musi być 0-100.")
        valid = True
    if not valid:
        errors.append("Dodaj przynajmniej jedną prawidłową pozycję.")
    return errors


@app.context_processor
def inject_globals() -> dict[str, Any]:
    return {"LANGUAGES": LANGUAGES, "TAX_MODES": TAX_MODES, "PAYMENT_METHODS": PAYMENT_METHODS,
            "DPH_CATEGORIES": DPH_CATEGORIES, "EXPENSE_CATEGORIES": EXPENSE_CATEGORIES,
            "EXPENSE_DPH_OBLIGATIONS": EXPENSE_DPH_OBLIGATIONS, "MONTHS": MONTHS,
            "TAX_PAYMENT_TYPES": TAX_PAYMENT_TYPES}



@app.context_processor
def inject_prop_globals() -> dict[str, Any]:
    return {
        "PROP_TAX_CLASSIFICATIONS": PROP_TAX_CLASSIFICATIONS,
        "PROP_QUALIFICATION_STATUSES": PROP_QUALIFICATION_STATUSES,
        "PROP_DPH_TREATMENTS": PROP_DPH_TREATMENTS,
        "LUCIDFLEX_PROFILE_NAME": LUCIDFLEX_PROFILE_NAME,
    }

@app.context_processor
def inject_v7_globals() -> dict[str, Any]:
    return {"auth_enabled": bool(APP_PASSWORD), "cloud_mode": CLOUD_MODE, "current_year": date.today().year}



@app.before_request
def v72_track_database_state():
    try:
        g.db_mtime_before = DB_PATH.stat().st_mtime_ns if DB_PATH.exists() else 0
        g.db_size_before = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    except OSError:
        g.db_mtime_before = 0
        g.db_size_before = 0


@app.after_request
def v72_sync_database_after_change(response):
    if not REMOTE_DB_ENABLED or response.status_code >= 500:
        return response
    try:
        current_mtime = DB_PATH.stat().st_mtime_ns if DB_PATH.exists() else 0
        current_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        changed = (
            current_mtime != getattr(g, "db_mtime_before", current_mtime)
            or current_size != getattr(g, "db_size_before", current_size)
        )
        if changed:
            upload_remote_database()
    except Exception as exc:
        print(f"Supabase: błąd automatycznej synchronizacji: {exc}")
    return response


@app.after_request
def v82_no_private_cache(response):
    if request.endpoint not in {"static", "app_icon", "manifest", "service_worker"}:
        response.headers["Cache-Control"] = "no-store, private"
    return response


@app.before_request
def v7_auth_guard():
    # PWA assets i logowanie muszą działać przed zalogowaniem.
    allowed = {"login", "manifest", "service_worker", "app_icon", "static", "health"}
    if not APP_PASSWORD or request.endpoint in allowed:
        return None
    if session.get("authenticated") is True:
        return None
    return redirect(url_for("login", next=request.full_path if request.query_string else request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied, APP_PASSWORD):
            session["authenticated"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Nieprawidłowe hasło.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/manifest.webmanifest")
def manifest():
    payload = """{
      "name":"Faktury OSVČ",
      "short_name":"Faktury",
      "start_url":"/",
      "display":"standalone",
      "background_color":"#f4f6f8",
      "theme_color":"#8b1538",
      "icons":[{"src":"/app-icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any maskable"}]
    }"""
    return Response(payload, mimetype="application/manifest+json")


@app.route("/service-worker.js")
def service_worker():
    # Nie cache'ujemy danych finansowych offline. SW daje instalowalność PWA
    # i prosty fallback dla powłoki aplikacji.
    script = """const CACHE='faktury-shell-v83';
self.addEventListener('install',e=>{self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(self.clients.claim());});
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET') return;
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});"""
    return Response(script, mimetype="application/javascript")


@app.route("/app-icon.svg")
def app_icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
    <rect width="512" height="512" rx="112" fill="#8b1538"/>
    <text x="256" y="310" text-anchor="middle" font-family="Arial,sans-serif" font-size="220" font-weight="700" fill="white">F</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.route("/health")
def health():
    return {
        "status": "ok",
        "cloud_mode": CLOUD_MODE,
        "supabase_configured": REMOTE_DB_ENABLED,
        "supabase_bucket": SUPABASE_BUCKET if REMOTE_DB_ENABLED else None,
        "supabase_key_kind": SUPABASE_KEY_KIND,
        "database_exists": DB_PATH.exists(),
    }



@app.route("/")
def dashboard() -> str:
    db = get_db(); today = date.today()
    counts = {"contractors": db.execute("SELECT COUNT(*) AS c FROM contractors").fetchone()["c"],
              "invoices": db.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"],
              "unpaid": db.execute("SELECT COUNT(*) AS c FROM invoices WHERE status='unpaid'").fetchone()["c"]}
    rows = db.execute("""SELECT i.*, c.name AS contractor_name FROM invoices i JOIN contractors c ON c.id=i.contractor_id
                       ORDER BY i.issue_date DESC, i.id DESC LIMIT 8""").fetchall()
    recent = []
    for row in rows:
        invoice = dict(row); items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
        invoice["total"] = calculate_items([dict(item) for item in items], invoice["tax_mode"])["total_gross"]
        recent.append(invoice)
    return render_template("dashboard.html", counts=counts, recent=recent, company=get_company(),
                           tax_snapshot=compute_tax_snapshot(today.year), dph_reminder=get_pending_dph_reminder())


@app.route("/company", methods=["GET", "POST"])
def company_edit() -> str:
    db = get_db(); company = get_company()
    if request.method == "POST":
        data = {key: request.form.get(key, "").strip() for key in (
            "name", "address", "postal_code", "city", "country", "company_id", "email", "phone",
            "bank_name", "bank_account", "invoice_prefix", "default_reverse_charge_note",
            "tax_first_name", "tax_last_name", "tax_office_code", "tax_branch_code")}
        data["vat_id"] = request.form.get("vat_id", "").strip().upper()
        data["iban"] = request.form.get("iban", "").replace(" ", "").strip().upper()
        data["bic"] = request.form.get("bic", "").replace(" ", "").strip().upper()
        data["default_currency"] = request.form.get("default_currency", "EUR").strip().upper()
        data["default_due_days"] = request.form.get("default_due_days", "14").strip()
        data["default_language"] = request.form.get("default_language", "de")
        data["default_tax_mode"] = request.form.get("default_tax_mode", "reverse_charge")
        errors = []
        if not data["name"]: errors.append("Nazwa firmy / imię i nazwisko jest wymagane.")
        try:
            due = int(data["default_due_days"])
            if due < 0 or due > 365: raise ValueError
        except ValueError:
            due = 14; errors.append("Termin płatności musi mieć 0-365 dni.")
        if data["default_language"] not in LANGUAGES: errors.append("Wybierz poprawny język.")
        if data["default_tax_mode"] not in TAX_MODES: errors.append("Wybierz poprawny tryb VAT.")
        if data["tax_office_code"] and not data["tax_office_code"].isdigit(): errors.append("Kod c_ufo musi być liczbą.")
        if data["tax_branch_code"] and not data["tax_branch_code"].isdigit(): errors.append("Kod c_pracufo musi być liczbą.")
        if errors:
            for error in errors: flash(error, "error")
            company.update(data); company["default_due_days"] = due
            return render_template("company_form.html", company=company)
        db.execute("""UPDATE company SET name=?, address=?, postal_code=?, city=?, country=?, company_id=?, vat_id=?,
            email=?, phone=?, bank_name=?, bank_account=?, iban=?, bic=?, default_currency=?, default_due_days=?,
            invoice_prefix=?, default_language=?, default_tax_mode=?, default_reverse_charge_note=?, tax_first_name=?,
            tax_last_name=?, tax_office_code=?, tax_branch_code=? WHERE id=1""",
            (data["name"], data["address"], data["postal_code"], data["city"], data["country"], data["company_id"],
             data["vat_id"], data["email"], data["phone"], data["bank_name"], data["bank_account"], data["iban"],
             data["bic"], data["default_currency"], due, data["invoice_prefix"], data["default_language"],
             data["default_tax_mode"], data["default_reverse_charge_note"], data["tax_first_name"],
             data["tax_last_name"], data["tax_office_code"], data["tax_branch_code"]))
        db.commit(); flash("Dane firmy zostały zapisane.", "success")
        return redirect(url_for("company_edit"))
    return render_template("company_form.html", company=company)



@app.route("/projects")
@app.route("/settings/projects")
def projects_list() -> str:
    rows = get_db().execute(
        """SELECT p.*, c.name AS contractor_name
           FROM projects p
           LEFT JOIN contractors c ON c.id=p.contractor_id
           ORDER BY p.active DESC, p.project_number COLLATE NOCASE"""
    ).fetchall()
    return render_template("projects_list.html", projects=[dict(r) for r in rows])


def _project_payload(existing_id: int | None = None) -> dict[str, Any]:
    contractor_raw = request.form.get("contractor_id", "").strip()
    return {
        "id": existing_id,
        "project_number": request.form.get("project_number", "").strip(),
        "project_name": request.form.get("project_name", "").strip(),
        "contractor_id": int(contractor_raw) if contractor_raw.isdigit() else None,
        "notes": request.form.get("notes", "").strip(),
        "active": 1 if request.form.get("active") == "1" else 0,
    }


@app.route("/projects/new", methods=["GET", "POST"])
@app.route("/settings/projects/new", methods=["GET", "POST"])
def project_new() -> str:
    db = get_db()
    contractors = [dict(r) for r in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    project = {"id": None, "project_number": "", "project_name": "", "contractor_id": "", "notes": "", "active": 1}
    if request.method == "POST":
        project = _project_payload()
        if not project["project_number"]:
            flash("Podaj numer projektu.", "error")
            return render_template("project_form.html", project=project, contractors=contractors, is_edit=False)
        try:
            db.execute(
                """INSERT INTO projects (contractor_id, project_number, project_name, notes, active)
                   VALUES (?, ?, ?, ?, ?)""",
                (project["contractor_id"], project["project_number"], project["project_name"], project["notes"], project["active"]),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Taki numer projektu jest już zapisany dla tego kontrahenta.", "error")
            return render_template("project_form.html", project=project, contractors=contractors, is_edit=False)
        flash("Projekt został zapisany.", "success")
        return redirect(url_for("projects_list"))
    return render_template("project_form.html", project=project, contractors=contractors, is_edit=False)


@app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@app.route("/settings/projects/<int:project_id>/edit", methods=["GET", "POST"])
def project_edit(project_id: int) -> str:
    db = get_db()
    row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        abort(404)
    contractors = [dict(r) for r in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    project = dict(row)
    if request.method == "POST":
        project = _project_payload(project_id)
        try:
            db.execute(
                """UPDATE projects SET contractor_id=?, project_number=?, project_name=?, notes=?, active=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (project["contractor_id"], project["project_number"], project["project_name"], project["notes"], project["active"], project_id),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Taki numer projektu jest już zapisany dla tego kontrahenta.", "error")
            return render_template("project_form.html", project=project, contractors=contractors, is_edit=True)
        flash("Projekt został zaktualizowany.", "success")
        return redirect(url_for("projects_list"))
    return render_template("project_form.html", project=project, contractors=contractors, is_edit=True)


@app.post("/projects/<int:project_id>/delete")
@app.post("/settings/projects/<int:project_id>/delete")
def project_delete(project_id: int):
    db = get_db()
    db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    db.commit()
    flash("Projekt został usunięty. Numery zapisane na starych fakturach pozostają bez zmian.", "success")
    return redirect(url_for("projects_list"))



@app.route("/contractors")
@app.route("/settings/contractors")
def contractors_list() -> str:
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        like = f"%{q}%"
        rows = db.execute(
            """
            SELECT * FROM contractors
            WHERE name LIKE ? OR vat_id LIKE ? OR company_id LIKE ? OR city LIKE ?
            ORDER BY name COLLATE NOCASE
            """,
            (like, like, like, like),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("contractors_list.html", contractors=[dict(row) for row in rows], q=q)


def contractor_from_request(existing_id: int | None = None) -> dict[str, Any]:
    return {
        "id": existing_id,
        "name": request.form.get("name", "").strip(),
        "address": request.form.get("address", "").strip(),
        "postal_code": request.form.get("postal_code", "").strip(),
        "city": request.form.get("city", "").strip(),
        "country": request.form.get("country", "").strip(),
        "company_id": request.form.get("company_id", "").strip(),
        "vat_id": request.form.get("vat_id", "").strip().upper().replace(" ", ""),
        "email": request.form.get("email", "").strip(),
        "phone": request.form.get("phone", "").strip(),
        "notes": request.form.get("notes", "").strip(),
    }


@app.route("/contractors/new", methods=["GET", "POST"])
@app.route("/settings/contractors/new", methods=["GET", "POST"])
def contractor_new() -> str:
    contractor: dict[str, Any] = {
        "name": "", "address": "", "postal_code": "", "city": "", "country": "Österreich",
        "company_id": "", "vat_id": "ATU", "email": "", "phone": "", "notes": "",
    }
    if request.method == "POST":
        contractor = contractor_from_request()
        if not contractor["name"]:
            flash("Nazwa kontrahenta jest wymagana.", "error")
            return render_template("contractor_form.html", contractor=contractor, is_edit=False)
        db = get_db()
        db.execute(
            """
            INSERT INTO contractors (name, address, postal_code, city, country, company_id, vat_id, email, phone, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(contractor[key] for key in ("name", "address", "postal_code", "city", "country", "company_id", "vat_id", "email", "phone", "notes")),
        )
        db.commit()
        flash("Kontrahent został dodany.", "success")
        return redirect(url_for("contractors_list"))
    return render_template("contractor_form.html", contractor=contractor, is_edit=False)


@app.route("/contractors/<int:contractor_id>/edit", methods=["GET", "POST"])
@app.route("/settings/contractors/<int:contractor_id>/edit", methods=["GET", "POST"])
def contractor_edit(contractor_id: int) -> str:
    db = get_db()
    row = db.execute("SELECT * FROM contractors WHERE id = ?", (contractor_id,)).fetchone()
    if row is None:
        abort(404)
    contractor = dict(row)
    if request.method == "POST":
        contractor = contractor_from_request(contractor_id)
        if not contractor["name"]:
            flash("Nazwa kontrahenta jest wymagana.", "error")
            return render_template("contractor_form.html", contractor=contractor, is_edit=True)
        db.execute(
            """
            UPDATE contractors SET name = ?, address = ?, postal_code = ?, city = ?, country = ?,
                company_id = ?, vat_id = ?, email = ?, phone = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            tuple(contractor[key] for key in ("name", "address", "postal_code", "city", "country", "company_id", "vat_id", "email", "phone", "notes")) + (contractor_id,),
        )
        db.commit()
        flash("Dane kontrahenta zostały zapisane.", "success")
        return redirect(url_for("contractors_list"))
    return render_template("contractor_form.html", contractor=contractor, is_edit=True)


@app.post("/contractors/<int:contractor_id>/delete")
@app.post("/settings/contractors/<int:contractor_id>/delete")
def contractor_delete(contractor_id: int):
    db = get_db()
    try:
        cursor = db.execute("DELETE FROM contractors WHERE id = ?", (contractor_id,))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Nie można usunąć kontrahenta użytego na fakturze.", "error")
    else:
        if cursor.rowcount:
            flash("Kontrahent został usunięty.", "success")
    return redirect(url_for("contractors_list"))


def default_expense() -> dict[str, Any]:
    return {"id": None, "expense_date": date.today().isoformat(), "supplier": "", "description": "",
            "category": "other", "document_number": "", "amount": "", "currency": "CZK", "czk_rate": "1",
            "business_percent": "100", "payment_method": "bank_transfer", "dph_obligation": "none",
            "supplier_vat_id": "", "country": "", "notes": ""}


def expense_from_request(expense_id: int | None = None) -> dict[str, Any]:
    currency = request.form.get("currency", "CZK").strip().upper()
    rate = request.form.get("czk_rate", "").strip() or ("1" if currency == "CZK" else "")
    return {"id": expense_id, "expense_date": request.form.get("expense_date", "").strip(),
            "supplier": request.form.get("supplier", "").strip(), "description": request.form.get("description", "").strip(),
            "category": request.form.get("category", "other"), "document_number": request.form.get("document_number", "").strip(),
            "amount": request.form.get("amount", "").strip(), "currency": currency, "czk_rate": rate,
            "business_percent": request.form.get("business_percent", "100").strip(),
            "payment_method": request.form.get("payment_method", "bank_transfer"),
            "dph_obligation": request.form.get("dph_obligation", "none"),
            "supplier_vat_id": normalize_vat_id(request.form.get("supplier_vat_id", "")),
            "country": request.form.get("country", "").strip(), "notes": request.form.get("notes", "").strip()}


def validate_expense(expense: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try: date.fromisoformat(expense["expense_date"])
    except ValueError: errors.append("Podaj poprawną datę kosztu.")
    if not expense["description"]: errors.append("Wpisz opis kosztu.")
    try:
        if parse_decimal(expense["amount"]) < 0: raise ValueError
    except ValueError: errors.append("Kwota kosztu musi być liczbą nieujemną.")
    try:
        percent = parse_decimal(expense["business_percent"])
        if percent < 0 or percent > 100: raise ValueError
    except ValueError: errors.append("Część związana z działalnością musi wynosić 0-100%.")
    if expense["currency"] != "CZK":
        try:
            if parse_decimal(expense["czk_rate"]) <= 0: raise ValueError
        except ValueError: errors.append("Dla waluty obcej podaj kurs do CZK.")
    if expense["category"] not in EXPENSE_CATEGORIES: errors.append("Wybierz kategorię kosztu.")
    if expense["payment_method"] not in PAYMENT_METHODS: errors.append("Wybierz sposób płatności.")
    if expense["dph_obligation"] not in EXPENSE_DPH_OBLIGATIONS: errors.append("Wybierz klasyfikację DPH.")
    return errors


def prop_firm_payload(existing_id: int | None = None) -> dict[str, Any]:
    contractor_raw = request.form.get("contractor_id", "").strip()
    return {
        "id": existing_id,
        "name": request.form.get("name", "").strip(),
        "contractor_id": int(contractor_raw) if contractor_raw.isdigit() else None,
        "default_tax_classification": request.form.get("default_tax_classification", "s7_60"),
        "qualification_status": request.form.get("qualification_status", "unconfirmed"),
        "dph_treatment": request.form.get("dph_treatment", "review"),
        "notes": request.form.get("notes", "").strip(),
        "active": 1 if request.form.get("active") == "1" else 0,
    }


def validate_prop_firm(firm: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not firm.get("name"):
        errors.append("Podaj nazwę prop firmy.")
    if firm.get("default_tax_classification") not in PROP_TAX_CLASSIFICATIONS:
        errors.append("Wybierz prawidłową kwalifikację PIT.")
    if firm.get("qualification_status") not in PROP_QUALIFICATION_STATUSES:
        errors.append("Wybierz prawidłowy status kwalifikacji.")
    if firm.get("dph_treatment") not in PROP_DPH_TREATMENTS:
        errors.append("Wybierz prawidłową kwalifikację DPH.")
    return errors


def available_prop_invoices(current_payout_id: int | None = None) -> list[dict[str, Any]]:
    db = get_db()
    rows = db.execute(
        """SELECT i.*, c.name AS contractor_name, pp.id AS linked_payout_id
             FROM invoices i
             JOIN contractors c ON c.id=i.contractor_id
             LEFT JOIN prop_payouts pp ON pp.linked_invoice_id=i.id
             WHERE pp.id IS NULL OR pp.id=?
             ORDER BY i.issue_date DESC, i.id DESC""",
        (current_payout_id or -1,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        item_rows = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (item["id"],)).fetchall()
        totals = calculate_items([dict(row) for row in item_rows], item["tax_mode"])
        item["total"] = totals["total_gross"]
        result.append(item)
    return result


def prop_payout_payload(existing_id: int | None = None) -> dict[str, Any]:
    firm_raw = request.form.get("prop_firm_id", "").strip()
    invoice_raw = request.form.get("linked_invoice_id", "").strip()
    gross = parse_decimal(request.form.get("gross_amount", ""), "kwota należna po profit split")
    fee = parse_decimal(request.form.get("operator_fee", "0"), "opłata operatora")
    net_raw = request.form.get("net_amount", "").strip()
    net = parse_decimal(net_raw, "kwota netto") if net_raw else gross - fee
    currency = request.form.get("currency", "USD").strip().upper()
    existing_row = get_db().execute("SELECT * FROM prop_payouts WHERE id=?", (existing_id,)).fetchone() if existing_id else None
    existing = dict(existing_row) if existing_row else None
    previous = fx_quote('payout', existing_id, 'income') if existing else None
    rate_text, fx = resolve_form_fx(currency, request.form.get('received_date','').strip(),
                                   request.form.get('czk_rate',''), existing, previous,
                                   existing['received_date'] if existing else None)
    rate = parse_decimal(rate_text, 'kurs payoutu')
    amounts = payout_amount_breakdown(gross, fee, net, rate)
    return {
        "id": existing_id,
        "_fx_quote": fx,
        "prop_firm_id": int(firm_raw) if firm_raw.isdigit() else None,
        "payout_identifier": request.form.get("payout_identifier", "").strip(),
        "received_date": request.form.get("received_date", "").strip(),
        "currency": currency,
        "gross_amount": str(gross),
        "operator_fee": str(fee),
        "net_amount": str(net),
        "czk_rate": str(rate),
        "income_czk": str(amounts["income_czk"]),
        "linked_invoice_id": int(invoice_raw) if invoice_raw.isdigit() else None,
        "tax_classification": request.form.get("tax_classification", "s7_60"),
        "qualification_status": request.form.get("qualification_status", "unconfirmed"),
        "dph_treatment": request.form.get("dph_treatment", "review"),
        "notes": request.form.get("notes", "").strip(),
    }


def prop_payout_form_values(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Zachowuje wpisane pola formularza, gdy walidacja kwot zakończy się błędem."""
    values = dict(base or {})
    for key in (
        "prop_firm_id", "payout_identifier", "received_date", "currency",
        "gross_amount", "operator_fee", "net_amount", "czk_rate",
        "linked_invoice_id", "tax_classification", "qualification_status",
        "dph_treatment", "notes",
    ):
        if key in request.form:
            values[key] = request.form.get(key, "")
    try:
        gross = parse_decimal(values.get("gross_amount", "0"))
        rate = Decimal("1") if str(values.get("currency", "")).upper() == "CZK" else parse_decimal(values.get("czk_rate", "0"))
        values["income_czk"] = str(q2(gross * rate)) if gross >= 0 and rate >= 0 else ""
    except ValueError:
        values["income_czk"] = ""
    return values


def validate_prop_payout(payout: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    db = get_db()
    if payout.get("prop_firm_id") is None or db.execute("SELECT 1 FROM prop_firms WHERE id=?", (payout.get("prop_firm_id"),)).fetchone() is None:
        errors.append("Wybierz prop firmę.")
    try:
        date.fromisoformat(payout.get("received_date", ""))
    except ValueError:
        errors.append("Podaj prawidłową datę otrzymania payoutu.")
    try:
        gross = parse_decimal(payout.get("gross_amount")); fee = parse_decimal(payout.get("operator_fee")); net = parse_decimal(payout.get("net_amount")); rate = parse_decimal(payout.get("czk_rate"))
        if gross <= 0: errors.append("Kwota payoutu musi być większa od zera.")
        if fee < 0 or net < 0: errors.append("Opłata i kwota netto nie mogą być ujemne.")
        if fee > gross: errors.append("Opłata operatora nie może przewyższać kwoty należnej.")
        if net > gross: errors.append("Kwota netto nie może przewyższać kwoty należnej po profit split.")
        if rate <= 0: errors.append("Kurs do CZK musi być większy od zera.")
    except ValueError as exc:
        errors.append(str(exc))
    if not payout.get("currency") or len(payout["currency"]) > 8:
        errors.append("Podaj poprawny kod waluty.")
    if payout.get("tax_classification") not in PROP_TAX_CLASSIFICATIONS:
        errors.append("Wybierz kwalifikację PIT.")
    if payout.get("qualification_status") not in PROP_QUALIFICATION_STATUSES:
        errors.append("Wybierz status kwalifikacji.")
    if payout.get("dph_treatment") not in PROP_DPH_TREATMENTS:
        errors.append("Wybierz kwalifikację DPH.")
    linked = payout.get("linked_invoice_id")
    if linked is not None:
        if db.execute("SELECT 1 FROM invoices WHERE id=?", (linked,)).fetchone() is None:
            errors.append("Wybrana faktura nie istnieje.")
        duplicate = db.execute("SELECT id FROM prop_payouts WHERE linked_invoice_id=? AND id<>?", (linked, payout.get("id") or -1)).fetchone()
        if duplicate is not None:
            errors.append("Ta faktura jest już powiązana z innym payoutem.")
    return errors


@app.route("/prop-firms")
def prop_firms_dashboard() -> str:
    db = get_db()
    try:
        selected_year = int(request.args.get("year", date.today().year))
    except ValueError:
        selected_year = date.today().year
    try:
        selected_firm_id = int(request.args.get("firm_id", "")) if request.args.get("firm_id") else None
    except ValueError:
        selected_firm_id = None
    firms_all = [dict(row) for row in db.execute("""SELECT f.*, c.name AS contractor_name FROM prop_firms f LEFT JOIN contractors c ON c.id=f.contractor_id ORDER BY f.active DESC,f.name COLLATE NOCASE""").fetchall()]
    firms = [firm for firm in firms_all if firm["active"]]
    sql = """SELECT p.*, f.name AS firm_name, i.invoice_number FROM prop_payouts p JOIN prop_firms f ON f.id=p.prop_firm_id LEFT JOIN invoices i ON i.id=p.linked_invoice_id WHERE substr(p.received_date,1,4)=?"""
    params: list[Any] = [str(selected_year)]
    if selected_firm_id is not None:
        sql += " AND p.prop_firm_id=?"; params.append(selected_firm_id)
    sql += " ORDER BY p.received_date DESC,p.id DESC"
    payouts = [dict(row) for row in db.execute(sql, params).fetchall()]
    gross_czk = fee_czk = net_czk = Decimal("0")
    for payout in payouts:
        amounts = payout_amount_breakdown(parse_decimal(payout["gross_amount"]), parse_decimal(payout["operator_fee"]), parse_decimal(payout["net_amount"]), parse_decimal(payout["czk_rate"]))
        payout.update(amounts); gross_czk += amounts["income_czk"]; fee_czk += amounts["fee_czk"]; net_czk += amounts["net_czk"]
    summary = {"gross_czk": q2(gross_czk), "fee_czk": q2(fee_czk), "net_czk": q2(net_czk), "count": len(payouts)}
    return render_template("prop_firms.html", firms=firms, firms_all=firms_all, payouts=payouts, summary=summary, selected_year=selected_year, selected_firm_id=selected_firm_id)


@app.route("/prop-firms/new", methods=["GET", "POST"])
def prop_firm_new() -> str:
    db = get_db(); contractors = [dict(row) for row in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    firm = {"id": None, "name": "", "contractor_id": "", "default_tax_classification": "s7_60", "qualification_status": "unconfirmed", "dph_treatment": "review", "notes": "", "active": 1}
    if request.method == "POST":
        firm = prop_firm_payload(); errors = validate_prop_firm(firm)
        if errors:
            for error in errors: flash(error, "error")
            return render_template("prop_firm_form.html", firm=firm, contractors=contractors, is_edit=False)
        try:
            db.execute("INSERT INTO prop_firms (name,contractor_id,default_tax_classification,qualification_status,dph_treatment,notes,active) VALUES (?,?,?,?,?,?,?)", (firm["name"], firm["contractor_id"], firm["default_tax_classification"], firm["qualification_status"], firm["dph_treatment"], firm["notes"], firm["active"]))
            db.commit()
        except sqlite3.IntegrityError:
            flash("Firma o tej nazwie już istnieje.", "error")
            return render_template("prop_firm_form.html", firm=firm, contractors=contractors, is_edit=False)
        flash("Prop firma została dodana.", "success"); return redirect(url_for("prop_firms_dashboard"))
    return render_template("prop_firm_form.html", firm=firm, contractors=contractors, is_edit=False)


@app.route("/prop-firms/<int:firm_id>/edit", methods=["GET", "POST"])
def prop_firm_edit(firm_id: int) -> str:
    db = get_db(); row = db.execute("SELECT * FROM prop_firms WHERE id=?", (firm_id,)).fetchone()
    if row is None: abort(404)
    contractors = [dict(r) for r in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    firm = dict(row)
    if request.method == "POST":
        firm = prop_firm_payload(firm_id); errors = validate_prop_firm(firm)
        if errors:
            for error in errors: flash(error, "error")
            return render_template("prop_firm_form.html", firm=firm, contractors=contractors, is_edit=True)
        try:
            db.execute("""UPDATE prop_firms SET name=?,contractor_id=?,default_tax_classification=?,qualification_status=?,dph_treatment=?,notes=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (firm["name"], firm["contractor_id"], firm["default_tax_classification"], firm["qualification_status"], firm["dph_treatment"], firm["notes"], firm["active"], firm_id)); db.commit()
        except sqlite3.IntegrityError:
            flash("Firma o tej nazwie już istnieje.", "error")
            return render_template("prop_firm_form.html", firm=firm, contractors=contractors, is_edit=True)
        flash("Ustawienia prop firmy zostały zapisane.", "success"); return redirect(url_for("prop_firms_dashboard"))
    return render_template("prop_firm_form.html", firm=firm, contractors=contractors, is_edit=True)


@app.post("/prop-firms/<int:firm_id>/delete")
def prop_firm_delete(firm_id: int):
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS c FROM prop_payouts WHERE prop_firm_id=?", (firm_id,)).fetchone()["c"]
    if count:
        flash("Nie można usunąć firmy mającej payouty. Oznacz ją jako nieaktywną.", "error")
    else:
        db.execute("DELETE FROM prop_firms WHERE id=?", (firm_id,)); db.commit(); flash("Prop firma została usunięta.", "success")
    return redirect(url_for("prop_firms_dashboard"))


@app.route("/prop-payouts/new", methods=["GET", "POST"])
def prop_payout_new() -> str:
    db = get_db(); firms = [dict(row) for row in db.execute("SELECT * FROM prop_firms WHERE active=1 ORDER BY name COLLATE NOCASE").fetchall()]
    if not firms:
        flash("Najpierw dodaj prop firmę.", "error"); return redirect(url_for("prop_firm_new"))
    selected_firm = next((f for f in firms if str(f["id"]) == request.args.get("firm_id")), firms[0])
    payout = {"id": None, "prop_firm_id": selected_firm["id"], "payout_identifier": "", "received_date": date.today().isoformat(), "currency": "USD", "gross_amount": "", "operator_fee": "0", "net_amount": "", "czk_rate": "", "income_czk": "", "linked_invoice_id": request.args.get("invoice_id", ""), "tax_classification": selected_firm["default_tax_classification"], "qualification_status": selected_firm["qualification_status"], "dph_treatment": selected_firm["dph_treatment"], "notes": ""}
    invoices = available_prop_invoices()
    if request.method == "POST":
        try: payout = prop_payout_payload()
        except ValueError as exc:
            flash(str(exc), "error"); return render_template("prop_payout_form.html", payout=prop_payout_form_values(payout), firms=firms, invoices=invoices, is_edit=False)
        errors = validate_prop_payout(payout)
        if errors:
            for error in errors: flash(error, "error")
            return render_template("prop_payout_form.html", payout=payout, firms=firms, invoices=invoices, is_edit=False)
        cursor = db.execute("""INSERT INTO prop_payouts (prop_firm_id,payout_identifier,received_date,currency,gross_amount,operator_fee,net_amount,czk_rate,income_czk,linked_invoice_id,tax_classification,qualification_status,dph_treatment,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(payout[key] for key in ("prop_firm_id","payout_identifier","received_date","currency","gross_amount","operator_fee","net_amount","czk_rate","income_czk","linked_invoice_id","tax_classification","qualification_status","dph_treatment","notes")))
        save_fx_quote("payout", int(cursor.lastrowid), "income", payout.get("_fx_quote"))
        db.commit()
        flash("Payout został zapisany.", "success"); return redirect(url_for("prop_firms_dashboard", year=payout["received_date"][:4]))
    return render_template("prop_payout_form.html", payout=payout, firms=firms, invoices=invoices, is_edit=False)


@app.route("/prop-payouts/<int:payout_id>/edit", methods=["GET", "POST"])
def prop_payout_edit(payout_id: int) -> str:
    db = get_db(); row = db.execute("SELECT * FROM prop_payouts WHERE id=?", (payout_id,)).fetchone()
    if row is None: abort(404)
    firms = [dict(r) for r in db.execute("SELECT * FROM prop_firms ORDER BY active DESC,name COLLATE NOCASE").fetchall()]
    payout = dict(row); invoices = available_prop_invoices(payout_id)
    if request.method == "POST":
        try: payout = prop_payout_payload(payout_id)
        except ValueError as exc:
            flash(str(exc), "error"); return render_template("prop_payout_form.html", payout=prop_payout_form_values(payout), firms=firms, invoices=invoices, is_edit=True)
        errors = validate_prop_payout(payout)
        if errors:
            for error in errors: flash(error, "error")
            return render_template("prop_payout_form.html", payout=payout, firms=firms, invoices=invoices, is_edit=True)
        db.execute("""UPDATE prop_payouts SET prop_firm_id=?,payout_identifier=?,received_date=?,currency=?,gross_amount=?,operator_fee=?,net_amount=?,czk_rate=?,income_czk=?,linked_invoice_id=?,tax_classification=?,qualification_status=?,dph_treatment=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", tuple(payout[key] for key in ("prop_firm_id","payout_identifier","received_date","currency","gross_amount","operator_fee","net_amount","czk_rate","income_czk","linked_invoice_id","tax_classification","qualification_status","dph_treatment","notes")) + (payout_id,))
        save_fx_quote("payout", payout_id, "income", payout.get("_fx_quote"))
        db.commit()
        flash("Payout został zaktualizowany.", "success"); return redirect(url_for("prop_firms_dashboard", year=payout["received_date"][:4]))
    return render_template("prop_payout_form.html", payout=payout, firms=firms, invoices=invoices, is_edit=True)


@app.post("/prop-payouts/<int:payout_id>/delete")
def prop_payout_delete(payout_id: int):
    db = get_db(); row = db.execute("SELECT received_date FROM prop_payouts WHERE id=?", (payout_id,)).fetchone()
    if row is None: abort(404)
    db.execute("DELETE FROM fx_quotes WHERE entity_type='payout' AND entity_id=?", (payout_id,))
    db.execute("DELETE FROM prop_payouts WHERE id=?", (payout_id,)); db.commit(); flash("Payout został usunięty.", "success")
    return redirect(url_for("prop_firms_dashboard", year=str(row["received_date"])[:4]))

@app.route("/expenses")
def expenses_page() -> str:
    today = date.today()
    try:
        year = int(request.args.get("year", today.year)); month = int(request.args.get("month", today.month))
        if month not in range(1, 13): raise ValueError
    except ValueError:
        year, month = today.year, today.month
    start, end = month_bounds(year, month); db = get_db()
    rows = db.execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date DESC,id DESC", (start, end)).fetchall()
    expenses = []; actual = Decimal("0"); deductible = Decimal("0"); missing = 0; foreign_count = 0
    category_map: dict[str, Decimal] = {}
    for row in rows:
        expense = dict(row); value = expense_value_czk(expense); expense["amount_czk"] = value; expenses.append(expense)
        if value is None: missing += 1; value = Decimal("0")
        actual += value
        try: deductible += value * parse_decimal(expense["business_percent"]) / Decimal("100")
        except ValueError: pass
        category_map[expense["category"]] = category_map.get(expense["category"], Decimal("0")) + value
        if expense["dph_obligation"] in {"foreign_service", "review"}: foreign_count += 1
    revenue, missing_invoices = month_revenue_czk(year, month)
    category_totals = [{"category": key, "total": q2(value)} for key, value in sorted(category_map.items(), key=lambda p: p[1], reverse=True)]
    totals = {"actual": q2(actual), "deductible": q2(deductible), "revenue": revenue, "result": q2(revenue - actual)}
    return render_template("expenses.html", expenses=expenses, totals=totals, category_totals=category_totals,
        selected_year=year, selected_month=month, period_label=period_label(year, month),
        missing_rates=missing + missing_invoices, foreign_dph_count=foreign_count)


@app.route("/expenses/new", methods=["GET", "POST"])
def expense_new() -> str:
    expense = default_expense()
    if request.method == "POST":
        expense = expense_from_request(); errors = validate_expense(expense)
        if errors:
            for error in errors: flash(error, "error")
            return render_template("expense_form.html", expense=expense, is_edit=False)
        db = get_db(); db.execute("""INSERT INTO expenses (expense_date,supplier,description,category,document_number,
            amount,currency,czk_rate,business_percent,payment_method,dph_obligation,supplier_vat_id,country,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(expense[key] for key in ("expense_date","supplier","description","category","document_number","amount","currency","czk_rate","business_percent","payment_method","dph_obligation","supplier_vat_id","country","notes")))
        db.commit(); flash("Koszt został zapisany.", "success")
        return redirect(url_for("expenses_page", year=expense["expense_date"][:4], month=int(expense["expense_date"][5:7])))
    return render_template("expense_form.html", expense=expense, is_edit=False)


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
def expense_edit(expense_id: int) -> str:
    db = get_db(); row = db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if row is None: abort(404)
    expense = dict(row)
    if request.method == "POST":
        expense = expense_from_request(expense_id); errors = validate_expense(expense)
        if errors:
            for error in errors: flash(error, "error")
            return render_template("expense_form.html", expense=expense, is_edit=True)
        db.execute("""UPDATE expenses SET expense_date=?,supplier=?,description=?,category=?,document_number=?,amount=?,
            currency=?,czk_rate=?,business_percent=?,payment_method=?,dph_obligation=?,supplier_vat_id=?,country=?,notes=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?""", tuple(expense[key] for key in ("expense_date","supplier","description","category","document_number","amount","currency","czk_rate","business_percent","payment_method","dph_obligation","supplier_vat_id","country","notes")) + (expense_id,))
        db.commit(); flash("Koszt został zaktualizowany.", "success")
        return redirect(url_for("expenses_page", year=expense["expense_date"][:4], month=int(expense["expense_date"][5:7])))
    return render_template("expense_form.html", expense=expense, is_edit=True)


@app.post("/expenses/<int:expense_id>/delete")
def expense_delete(expense_id: int):
    db = get_db(); db.execute("DELETE FROM expenses WHERE id=?", (expense_id,)); db.commit()
    flash("Koszt został usunięty.", "success"); return redirect(url_for("expenses_page"))


@app.route("/expenses/export.csv")
def expenses_export_csv():
    today = date.today()
    try: year = int(request.args.get("year", today.year)); month = int(request.args.get("month", today.month))
    except ValueError: year, month = today.year, today.month
    start, end = month_bounds(year, month)
    rows = get_db().execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date,id", (start, end)).fetchall()
    output = StringIO(); writer = csv.writer(output, delimiter=';')
    writer.writerow(["Data","Dostawca","Opis","Kategoria","Dokument","Kwota","Waluta","Kurs do CZK","Wartość CZK","Część firmowa %","DPH"])
    for row in rows:
        item = dict(row); value = expense_value_czk(item)
        writer.writerow([item["expense_date"], item["supplier"], item["description"], EXPENSE_CATEGORIES.get(item["category"], item["category"]), item["document_number"], item["amount"], item["currency"], item["czk_rate"], str(value or ""), item["business_percent"], item["dph_obligation"]])
    data = '\ufeff' + output.getvalue()
    return Response(data, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=koszty_{year}_{month:02d}.csv"})



def percentage_cost(revenue: Decimal, percent: Decimal, annual_limit: Decimal) -> Decimal:
    revenue = max(revenue, Decimal("0"))
    percent = min(max(percent, Decimal("0")), Decimal("100"))
    annual_limit = max(annual_limit, Decimal("0"))
    return q2(min(revenue * percent / Decimal("100"), annual_limit))


def calculate_grouped_percentage_costs(
    group60_revenue: Decimal,
    group40_revenue: Decimal,
    percent60: Decimal,
    limit60: Decimal,
    percent40: Decimal,
    limit40: Decimal,
) -> dict[str, Decimal]:
    expenses60 = percentage_cost(group60_revenue, percent60, limit60)
    expenses40 = percentage_cost(group40_revenue, percent40, limit40)
    return {
        "flat_expenses_60": expenses60,
        "flat_expenses_40": expenses40,
        "flat_expenses": q2(expenses60 + expenses40),
        "profit_60": q2(max(group60_revenue - expenses60, Decimal("0"))),
        "profit_40": q2(max(group40_revenue - expenses40, Decimal("0"))),
        "profit": q2(max(group60_revenue - expenses60, Decimal("0")) + max(group40_revenue - expenses40, Decimal("0"))),
    }


def payout_amount_breakdown(gross: Decimal, fee: Decimal, net: Decimal, rate: Decimal) -> dict[str, Decimal]:
    gross = max(gross, Decimal("0")); fee = max(fee, Decimal("0")); net = max(net, Decimal("0")); rate = max(rate, Decimal("0"))
    return {
        "income_czk": q2(gross * rate),
        "fee_czk": q2(fee * rate),
        "net_czk": q2(net * rate),
    }


def settlement_balance(obligation: Decimal, paid: Decimal) -> tuple[Decimal, Decimal]:
    balance = q2(obligation - paid)
    return q2(max(balance, Decimal("0"))), q2(max(-balance, Decimal("0")))


def exclude_linked_invoice_rows(rows: list[dict[str, Any]], linked_invoice_ids: set[int]) -> list[dict[str, Any]]:
    return [row for row in rows if int(row.get("id", 0)) not in linked_invoice_ids]


def prop_payout_rows(year: int) -> tuple[list[dict[str, Any]], list[str]]:
    db = get_db()
    rows = db.execute(
        """SELECT p.*, f.name AS firm_name, i.invoice_number
             FROM prop_payouts p
             JOIN prop_firms f ON f.id=p.prop_firm_id
             LEFT JOIN invoices i ON i.id=p.linked_invoice_id
             WHERE substr(p.received_date,1,4)=?
             ORDER BY p.received_date, p.id""",
        (str(year),),
    ).fetchall()
    result: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw in rows:
        item = dict(raw)
        try:
            gross = parse_decimal(item.get("gross_amount"), "kwota payoutu")
            fee = parse_decimal(item.get("operator_fee"), "opłata operatora")
            net = parse_decimal(item.get("net_amount"), "kwota netto")
            rate = parse_decimal(item.get("czk_rate"), "kurs payoutu")
            amounts = payout_amount_breakdown(gross, fee, net, rate)
        except ValueError as exc:
            warnings.append(f"Payout {item.get('payout_identifier') or item.get('id')}: {exc}; pominięto w kalkulacji.")
            continue
        item.update(amounts)
        # Przychód zapisany jest audytowalnie w bazie, ale kalkulator ponownie sprawdza wzór brutto × kurs.
        item["stored_income_czk"] = item.get("income_czk", "")
        item["income_czk"] = amounts["income_czk"]
        result.append(item)
    return result, warnings

# ---------------------------------------------------------------------------
# V8.2: read-only contribution forecast. Scenarios never create tax receipts.
# Parameters are stored by target year, independently of historical settings.
# ---------------------------------------------------------------------------
V82_MIGRATION_KEY = 'v8_2_settings_and_contribution_forecast'
CONTRIBUTION_PARAMETER_DEFAULTS = {
    2027: {
        'cssz_min_base': '18083', 'cssz_max_base': '206652',
        'vzp_min_base': '25831.50', 'cssz_assessment_percent': '55',
        'cssz_rate_percent': '29.2', 'vzp_assessment_percent': '50',
        'vzp_rate_percent': '13.5', 'status': 'law_derived',
        'source_note': 'Wyliczenie z NV 177/2026 Sb.: 48 900 × 1,0565 = 51 662,85 → 51 663 Kč. '
                       'Min. ČSSZ: ceil(35% × 51 663) = 18 083 Kč; VZP: 50% × 51 663 = 25 831,50 Kč. '
                       'Nie jest to indywidualny předpis zaliczek. Stan na 07.10.2026.',
    },
}


def v82_backup_before_migration() -> Path | None:
    """Back up a current database before any V8.2 schema change. Fail closed."""
    if not DB_PATH.exists() or not DB_PATH.stat().st_size:
        return None
    with sqlite3.connect(DB_PATH) as source:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='app_migrations'").fetchone()
        if exists and source.execute('SELECT 1 FROM app_migrations WHERE migration_key=?',
                                     (V82_MIGRATION_KEY,)).fetchone():
            return None
        name = 'przed_aktualizacja_V8_2_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.sqlite3'
        target = BACKUP_DIR / name
        with sqlite3.connect(target) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('V8.2: kopia bazy nie przeszła kontroli. Migracja zatrzymana.')
    if REMOTE_DB_ENABLED and not upload_file_to_supabase(target, 'backups/' + name):
        raise RuntimeError('V8.2: nie zapisano kopii przed migracją w Supabase. Migracja zatrzymana.')
    return target


def init_v82_module() -> None:
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS contribution_year_parameters (
            year INTEGER PRIMARY KEY,
            cssz_min_base TEXT NOT NULL,
            cssz_max_base TEXT NOT NULL,
            vzp_min_base TEXT NOT NULL,
            cssz_assessment_percent TEXT NOT NULL DEFAULT '55',
            cssz_rate_percent TEXT NOT NULL DEFAULT '29.2',
            vzp_assessment_percent TEXT NOT NULL DEFAULT '50',
            vzp_rate_percent TEXT NOT NULL DEFAULT '13.5',
            status TEXT NOT NULL DEFAULT 'unverified',
            source_note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS contribution_forecast_settings (
            source_year INTEGER PRIMARY KEY,
            as_of_date TEXT NOT NULL DEFAULT '',
            additional_revenue_60 TEXT NOT NULL DEFAULT '0',
            additional_revenue_40 TEXT NOT NULL DEFAULT '0',
            cssz_months_override INTEGER NOT NULL DEFAULT 0,
            vzp_months_override INTEGER NOT NULL DEFAULT 0,
            cssz_advance_exempt INTEGER NOT NULL DEFAULT 0,
            vzp_advance_exempt INTEGER NOT NULL DEFAULT 0,
            vzp_minimum_applies INTEGER NOT NULL DEFAULT 1,
            cssz_report_month INTEGER NOT NULL DEFAULT 0,
            vzp_report_month INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    for year, values in CONTRIBUTION_PARAMETER_DEFAULTS.items():
        keys = list(values)
        db.execute('INSERT OR IGNORE INTO contribution_year_parameters (year,' + ','.join(keys) + ') '
                   'VALUES (' + ','.join('?' for _ in range(len(keys) + 1)) + ')',
                   (year,) + tuple(values[k] for k in keys))
    db.execute('INSERT OR IGNORE INTO app_migrations (migration_key) VALUES (?)', (V82_MIGRATION_KEY,))
    db.commit()


def contribution_forecast_settings(source_year: int) -> dict[str, Any]:
    """GET does not create settings or overwrite tax records."""
    row = get_db().execute('SELECT * FROM contribution_forecast_settings WHERE source_year=?',
                           (source_year,)).fetchone()
    if row:
        return dict(row)
    return dict(source_year=source_year, as_of_date='', additional_revenue_60='0',
                additional_revenue_40='0', cssz_months_override=0, vzp_months_override=0,
                cssz_advance_exempt=0, vzp_advance_exempt=0, vzp_minimum_applies=1,
                cssz_report_month=0, vzp_report_month=0, note='')


def contribution_year_parameters(year: int) -> dict[str, Any] | None:
    row = get_db().execute('SELECT * FROM contribution_year_parameters WHERE year=?', (year,)).fetchone()
    return dict(row) if row else None


def estimate_next_advances(profit: Decimal, cssz_months: int, vzp_months: int,
                           parameters: dict[str, Any], options: dict[str, Any]) -> dict[str, Decimal]:
    """Monthly advances after the overview, for continuing hlavní SVČ.

    ČSSZ rounds the monthly assessment up first and the premium up again.
    Both months of hlavní and vedlejší belong in the source-year denominator.
    VZP has no upper assessment limit. Paid advances never reduce next year's rate.
    """
    if not 1 <= cssz_months <= 12 or not 1 <= vzp_months <= 12:
        raise ValueError('Prognoza wymaga od 1 do 12 miesięcy działalności.')
    d = lambda k: parse_decimal(parameters[k], k)
    profit = max(Decimal('0'), profit)
    cssz_rate = d('cssz_rate_percent') / Decimal('100')
    vzp_rate = d('vzp_rate_percent') / Decimal('100')
    cssz_raw = ceil_whole(profit * d('cssz_assessment_percent') / Decimal('100') / Decimal(cssz_months))
    cssz_base = min(max(cssz_raw, d('cssz_min_base')), d('cssz_max_base'))
    vzp_raw = profit * d('vzp_assessment_percent') / Decimal('100') / Decimal(vzp_months)
    vzp_min = d('vzp_min_base') if options.get('vzp_minimum_applies', 1) else Decimal('0')
    vzp_base = max(vzp_raw, vzp_min)
    cssz = ceil_whole(cssz_base * cssz_rate)
    vzp = ceil_whole(vzp_base * vzp_rate)
    if options.get('cssz_advance_exempt'):
        cssz = Decimal('0')
    if options.get('vzp_advance_exempt'):
        vzp = Decimal('0')
    return dict(cssz=cssz, vzp=vzp, total=cssz + vzp,
                cssz_raw=cssz_raw, cssz_base=cssz_base, vzp_base=q2(vzp_base),
                cssz_from_income=ceil_whole(min(cssz_raw, d('cssz_max_base')) * cssz_rate),
                vzp_from_income=ceil_whole(vzp_raw * vzp_rate))


def compute_contribution_forecast(source_year: int, as_of: date | None = None) -> dict[str, Any]:
    settings = ensure_tax_settings(source_year)
    options = contribution_forecast_settings(source_year)
    parameters = contribution_year_parameters(source_year + 1)
    raw_cutoff = options.get('as_of_date', '')
    cutoff = as_of or (date.fromisoformat(raw_cutoff) if raw_cutoff else date.today())
    cutoff = min(max(cutoff, date(source_year, 1, 1)), date(source_year, 12, 31))
    invoices, warnings = tax_invoice_rows(source_year, 'paid')
    invoices = [r for r in invoices if date.fromisoformat(r['recognized_date']) <= cutoff]
    payouts, payout_warnings = prop_payout_rows(source_year)
    warnings.extend(payout_warnings)
    payouts = [r for r in payouts if date.fromisoformat(r['received_date']) <= cutoff]
    included = [r for r in payouts if r['tax_classification'] in {'s7_60', 's7_40'}]
    excluded = [r for r in payouts if r['tax_classification'] == 'exclude']
    welding = q2(sum((r['value_czk'] for r in invoices), Decimal('0')))
    prop60 = q2(sum((r['income_czk'] for r in included if r['tax_classification'] == 's7_60'), Decimal('0')))
    prop40 = q2(sum((r['income_czk'] for r in included if r['tax_classification'] == 's7_40'), Decimal('0')))
    for r in included:
        if r.get('qualification_status') != 'confirmed':
            warnings.append(f"{r['firm_name']}: kwalifikacja kosztów jest oceną dokumentów, a nie decyzją urzędu.")
    if excluded:
        warnings.append(f'Wyłączono {len(excluded)} payout(y) według ich ręcznej kwalifikacji podatkowej.')
    if settings.get('revenue_basis') != 'paid':
        warnings.append('Zachowano ustawienie „według wystawienia” w głównym kalkulatorze. '
                        'Ta prognoza zawsze korzysta z zapłaconych faktur i otrzymanych payoutów.')
    months_auto = active_months_for_year(settings, source_year)
    cssz_months = options['cssz_months_override'] or months_auto
    vzp_months = options['vzp_months_override'] or months_auto
    cssz_main, cssz_secondary = status_month_counts(settings, source_year, 'cssz_mode', 'cssz_main_from_date')
    warnings.append('Zakłada się kontynuację głównej OSVČ w następnym roku i podleganie czeskim ubezpieczeniom. '
                    'Nie jest to wyliczenie dla przejścia na vedlejší ani paušální daň.')
    if source_year >= date.today().year and not settings.get('activity_end_date'):
        warnings.append('Brak daty zakończenia: liczba miesięcy zakłada działalność do 31 grudnia, '
                        'nawet w scenariuszu bez kolejnych przychodów.')
    pending = []
    linked_ids = {r[0] for r in get_db().execute('SELECT linked_invoice_id FROM prop_payouts WHERE linked_invoice_id IS NOT NULL')}
    for raw in get_db().execute("SELECT * FROM invoices WHERE status <> 'paid' ORDER BY issue_date, id"):
        inv = dict(raw)
        if inv['id'] in linked_ids or inv['issue_date'] > cutoff.isoformat():
            continue
        if not inv['supply_date'].startswith(str(source_year)):
            continue
        items = [dict(x) for x in get_db().execute('SELECT * FROM invoice_items WHERE invoice_id=?', (inv['id'],))]
        totals = calculate_items(items, inv['tax_mode'])
        amount = totals['total_net'] if inv['tax_mode'] == 'vat' else totals['total_gross']
        rate = decimal_rate(inv['currency'], inv.get('czk_rate'))
        pending.append(dict(id=inv['id'], number=inv['invoice_number'], amount=amount,
                            currency=inv['currency'], value_czk=q2(amount * rate) if rate is not None else None))
    unknown = [r['number'] for r in pending if r['value_czk'] is None]
    if unknown:
        warnings.append('Nieopłacone faktury bez kursu CZK: ' + ', '.join(unknown) + '. Nie są doliczane ani wyceniane zgadywanym kursem.')
    if not parameters:
        warnings.append(f'Nie ma zweryfikowanych parametrów składek dla {source_year+1}. Uzupełnij je w Ustawieniach; nie kopiujemy minimów poprzedniego roku.')
    def scenario(extra60: Decimal, extra40: Decimal) -> dict[str, Any]:
        g60, g40 = q2(welding + prop60 + extra60), q2(prop40 + extra40)
        grouped = calculate_grouped_percentage_costs(
            g60, g40, parse_decimal(settings['expense_percent']), parse_decimal(settings['expense_limit']),
            parse_decimal(settings.get('expense_40_percent', '40')), parse_decimal(settings.get('expense_40_limit', '800000')))
        result = dict(revenue=g60 + g40, group60=g60, group40=g40,
                      costs=grouped['flat_expenses'], profit=grouped['profit'],
                      additional=extra60 + extra40, advances=None)
        if parameters and cssz_months and vzp_months:
            result['advances'] = estimate_next_advances(grouped['profit'], cssz_months, vzp_months, parameters, options)
        return result
    actual = scenario(Decimal('0'), Decimal('0'))
    simulated = scenario(parse_decimal(options['additional_revenue_60']), parse_decimal(options['additional_revenue_40']))
    current_cssz = parse_decimal(settings['cssz_monthly_advance'])
    current_vzp = parse_decimal(settings['vzp_monthly_advance'])
    current_total = current_cssz + current_vzp
    january = None
    if parameters:
        minimum = estimate_next_advances(Decimal('0'), 1, 1, parameters, options)
        january_cssz = Decimal('0') if options['cssz_advance_exempt'] else max(current_cssz, minimum['cssz'])
        january_vzp = Decimal('0') if options['vzp_advance_exempt'] else max(current_vzp, minimum['vzp'])
        january = dict(cssz=january_cssz, vzp=january_vzp, total=january_cssz + january_vzp)
        for row in (actual, simulated):
            if row['advances']:
                row['advances']['difference'] = row['advances']['total'] - current_total
                row['advances']['difference_cssz'] = row['advances']['cssz'] - current_cssz
                row['advances']['difference_vzp'] = row['advances']['vzp'] - current_vzp
        january['difference'] = january['total'] - current_total
    else:
        minimum = None
    for flag in ('cssz_advance_exempt', 'vzp_advance_exempt'):
        if options[flag]:
            warnings.append('Zastosowano ręcznie zadeklarowane zwolnienie z zaliczek. Nie oznacza ono zwolnienia z rocznej składki.')
    return dict(source_year=source_year, target_year=source_year+1, as_of=cutoff.isoformat(),
                options=options, parameters=parameters, actual=actual, simulated=simulated,
                has_scenario=simulated['additional'] > 0, welding_revenue=welding,
                prop_revenue=prop60+prop40, cash_revenue=welding+prop60+prop40,
                cssz_months=cssz_months, vzp_months=vzp_months,
                cssz_main_months=cssz_main, cssz_secondary_months=cssz_secondary,
                current_cssz=current_cssz, current_vzp=current_vzp, current_total=current_total,
                january=january, minimum=minimum, pending=pending,
                warnings=list(dict.fromkeys(warnings)), invoices=invoices, payouts=included)


@app.post('/settings/contributions/forecast')
def contribution_forecast_save():
    try:
        year = int(request.form.get('source_year', date.today().year))
        if not 2020 <= year <= 2099:
            raise ValueError('Nieprawidłowy rok.')
        ensure_tax_settings(year)
        values = {'as_of_date': _valid_date_or_blank(request.form.get('as_of_date', ''), 'data prognozy')}
        if values['as_of_date'] and date.fromisoformat(values['as_of_date']).year != year:
            raise ValueError('Data prognozy musi należeć do wybranego roku.')
        for key in ('additional_revenue_60', 'additional_revenue_40'):
            n = parse_decimal(request.form.get(key, '0'), key)
            if not n.is_finite() or n < 0:
                raise ValueError('Przychód prognozowany nie może być ujemny ani nieskończony.')
            values[key] = str(q2(n))
        for key in ('cssz_months_override', 'vzp_months_override', 'cssz_report_month', 'vzp_report_month'):
            n = int(request.form.get(key, '0') or 0)
            if not 0 <= n <= 12:
                raise ValueError('Liczba miesięcy musi mieścić się w przedziale 0–12.')
            values[key] = n
        for key in ('cssz_advance_exempt', 'vzp_advance_exempt', 'vzp_minimum_applies'):
            values[key] = 1 if request.form.get(key) == '1' else 0
        values['note'] = request.form.get('note', '').strip()[:2000]
    except (ValueError, TypeError, InvalidOperation) as exc:
        flash(str(exc), 'error')
        return redirect(url_for('settings_page', tab='forecast'))
    db = get_db(); keys = list(values)
    db.execute('INSERT OR IGNORE INTO contribution_forecast_settings (source_year) VALUES (?)', (year,))
    db.execute('UPDATE contribution_forecast_settings SET ' + ','.join(k+'=?' for k in keys) +
               ',updated_at=CURRENT_TIMESTAMP WHERE source_year=?', tuple(values[k] for k in keys)+(year,))
    db.commit()
    flash('Założenia prognozy zapisane. Nie utworzono żadnych transakcji ani wpłat.', 'success')
    return redirect(url_for('settings_page', year=year, tab='forecast'))


@app.post('/settings/contributions/parameters')
def contribution_parameters_save():
    try:
        year = int(request.form.get('target_year', '0'))
        if not 2021 <= year <= 2100:
            raise ValueError('Nieprawidłowy rok parametrów.')
        keys = ('cssz_min_base', 'cssz_max_base', 'vzp_min_base', 'cssz_assessment_percent',
                'cssz_rate_percent', 'vzp_assessment_percent', 'vzp_rate_percent')
        values = {}
        for key in keys:
            value = parse_decimal(request.form.get(key, ''), key)
            if not value.is_finite() or value < 0 or (key.endswith('percent') and value > 100):
                raise ValueError('Nieprawidłowa wartość: '+key)
            values[key] = str(value)
        if parse_decimal(values['cssz_max_base']) < parse_decimal(values['cssz_min_base']):
            raise ValueError('Maksymalna podstawa ČSSZ nie może być niższa od minimalnej.')
        values['status'] = 'manual'
        values['source_note'] = request.form.get('source_note', '').strip()[:2000]
    except (ValueError, TypeError, InvalidOperation) as exc:
        flash(str(exc), 'error')
        return redirect(url_for('settings_page', tab='forecast'))
    keys = list(values); db = get_db()
    db.execute('INSERT INTO contribution_year_parameters (year,'+','.join(keys)+') VALUES ('+
               ','.join('?' for _ in range(len(keys)+1))+') ON CONFLICT(year) DO UPDATE SET '+
               ','.join(k+'=excluded.'+k for k in keys)+',updated_at=CURRENT_TIMESTAMP',
               (year,)+tuple(values[k] for k in keys))
    db.commit()
    flash('Parametry prognozy zapisane dla '+str(year)+'. Historyczne ustawienia rozliczenia bez zmian.', 'success')
    return redirect(url_for('settings_page', year=year-1, tab='forecast'))


def ensure_tax_settings(year: int) -> dict[str, Any]:
    db = get_db()
    row = db.execute("SELECT * FROM tax_settings WHERE year=?", (year,)).fetchone()
    if row is None:
        if year == 2026:
            db.execute("""INSERT INTO tax_settings (
                year, activity_start_date, revenue_basis, expense_percent, expense_limit,
                income_tax_credit, employment_tax_base, employment_tax_withheld, tax_threshold,
                cssz_mode, cssz_main_from_date, cssz_threshold_annual, cssz_threshold_reduction_month,
                cssz_assessment_percent, cssz_rate_percent, cssz_min_monthly_base,
                cssz_secondary_min_monthly_base, cssz_monthly_advance,
                vzp_mode, vzp_main_from_date, vzp_assessment_percent, vzp_rate_percent,
                vzp_min_monthly_base, vzp_monthly_advance
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                2026, "2026-07-08", "paid", "60", "1200000", "30840", "0", "0", "1762812",
                "mixed", "2026-08-01", "117521", "9794", "55", "29.2", "17139", "5387", "5005",
                "mixed", "2026-08-01", "50", "13.5", "24483.50", "3306"
            ))
        elif year == 2027:
            db.execute("""INSERT INTO tax_settings (
                year,activity_start_date,revenue_basis,income_tax_credit,employment_tax_base,
                employment_tax_withheld,tax_threshold,cssz_mode,cssz_main_from_date,
                cssz_threshold_annual,cssz_threshold_reduction_month,cssz_min_monthly_base,
                cssz_secondary_min_monthly_base,cssz_monthly_advance,vzp_mode,vzp_main_from_date,
                vzp_min_monthly_base,vzp_monthly_advance
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                year, "2027-01-01", "paid", "30840", "0", "0", "1859868", "main", "2027-01-01",
                "123992", "10333", "18083", "5683", "5281", "main", "2027-01-01", "25831.50", "3488"
            ))
        else:
            start = f"{year}-01-01"
            db.execute("""INSERT INTO tax_settings (
                year, activity_start_date, income_tax_credit, cssz_mode, cssz_main_from_date,
                cssz_min_monthly_base, cssz_secondary_min_monthly_base, cssz_monthly_advance,
                vzp_mode, vzp_main_from_date, vzp_monthly_advance
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                year, start, "30840", "main", start, "17139", "5387", "5005", "main", start, "3306"
            ))
        db.commit()
        row = db.execute("SELECT * FROM tax_settings WHERE year=?", (year,)).fetchone()
    return dict(row)


def _valid_date_or_blank(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Nieprawidłowa data: {label}.") from exc
    return value


def active_month_starts(settings: dict[str, Any], year: int) -> list[date]:
    year_start, year_end = date(year, 1, 1), date(year, 12, 31)
    try:
        start = date.fromisoformat(settings.get("activity_start_date") or year_start.isoformat())
    except ValueError:
        start = year_start
    try:
        end = date.fromisoformat(settings.get("activity_end_date") or year_end.isoformat())
    except ValueError:
        end = year_end
    start, end = max(start, year_start), min(end, year_end)
    if end < start:
        return []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    result: list[date] = []
    while cursor <= last:
        result.append(cursor)
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return result


def active_months_for_year(settings: dict[str, Any], year: int) -> int:
    return len(active_month_starts(settings, year))


def status_month_counts(settings: dict[str, Any], year: int, mode_key: str, main_from_key: str) -> tuple[int, int]:
    months = active_month_starts(settings, year)
    mode = settings.get(mode_key, "main")
    if mode == "main":
        return len(months), 0
    if mode in {"secondary", "custom"}:
        return 0, len(months)
    try:
        transition = date.fromisoformat(settings.get(main_from_key) or f"{year}-01-01")
        transition_month = date(transition.year, transition.month, 1)
    except ValueError:
        transition_month = date(year, 1, 1)
    main = sum(1 for month in months if month >= transition_month)
    return main, len(months) - main


def ceil_whole(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_CEILING)


def progressive_tax(base: Decimal, threshold: Decimal) -> Decimal:
    base = max(base, Decimal("0")); threshold = max(threshold, Decimal("0"))
    lower = min(base, threshold)
    higher = max(base - threshold, Decimal("0"))
    return q2(lower * Decimal("0.15") + higher * Decimal("0.23"))


def tax_invoice_rows(year: int, basis: str) -> tuple[list[dict[str, Any]], list[str]]:
    db = get_db()
    linked_invoice_ids = {
        int(row["linked_invoice_id"])
        for row in db.execute("SELECT linked_invoice_id FROM prop_payouts WHERE linked_invoice_id IS NOT NULL").fetchall()
    }
    rows = db.execute("""SELECT i.*, c.name AS contractor_name, c.vat_id AS contractor_vat_id
                         FROM invoices i JOIN contractors c ON c.id=i.contractor_id
                         ORDER BY i.issue_date, i.id""").fetchall()
    candidates = exclude_linked_invoice_rows([dict(row) for row in rows], linked_invoice_ids)
    result: list[dict[str, Any]] = []
    warnings: list[str] = []
    for invoice in candidates:
        if basis == "paid":
            if invoice.get("status") != "paid":
                continue
            recognized = invoice.get("paid_date", "")
            if not recognized:
                recognized = invoice.get("supply_date", "")
                warnings.append(f"{invoice['invoice_number']}: brak daty zapłaty; użyto daty wykonania usługi.")
        else:
            recognized = invoice.get("supply_date", "")
        try:
            recognized_date = date.fromisoformat(recognized)
        except (ValueError, TypeError):
            warnings.append(f"{invoice['invoice_number']}: nieprawidłowa data przychodu.")
            continue
        if recognized_date.year != year:
            continue
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
        totals = calculate_items([dict(item) for item in items], invoice["tax_mode"])
        rate = (income_rate_for_invoice(invoice, warnings) if basis == "paid"
                else decimal_rate(invoice.get("currency"), invoice.get("czk_rate")))
        if rate is None:
            warnings.append(f"{invoice['invoice_number']}: brak kursu {invoice.get('currency','')} do CZK; faktura nie została doliczona.")
            continue
        taxable_amount = totals["total_net"] if invoice.get("tax_mode") == "vat" else totals["total_gross"]
        invoice["recognized_date"] = recognized_date.isoformat()
        invoice["value_czk"] = q2(taxable_amount * rate)
        invoice["tax_classification"] = "s7_60"
        result.append(invoice)
    return result, warnings



def compute_tax_snapshot(year: int) -> dict[str, Any]:
    settings = ensure_tax_settings(year)
    warnings: list[str] = []
    assumptions: list[str] = []
    basis = settings.get("revenue_basis", "paid")

    invoice_rows, invoice_warnings = tax_invoice_rows(year, basis)
    payout_rows, payout_warnings = prop_payout_rows(year)
    warnings.extend(invoice_warnings); warnings.extend(payout_warnings)

    welding_revenue = q2(sum((row["value_czk"] for row in invoice_rows), Decimal("0")))
    prop_rows_60 = [row for row in payout_rows if row.get("tax_classification") == "s7_60"]
    prop_rows_40 = [row for row in payout_rows if row.get("tax_classification") == "s7_40"]
    prop_rows_excluded = [row for row in payout_rows if row.get("tax_classification") == "exclude"]
    prop_rows_included = prop_rows_60 + prop_rows_40
    prop_revenue_60 = q2(sum((row["income_czk"] for row in prop_rows_60), Decimal("0")))
    prop_revenue_40 = q2(sum((row["income_czk"] for row in prop_rows_40), Decimal("0")))
    prop_revenue = q2(prop_revenue_60 + prop_revenue_40)
    prop_excluded_revenue = q2(sum((row["income_czk"] for row in prop_rows_excluded), Decimal("0")))
    prop_operator_fees_czk = q2(sum((row["fee_czk"] for row in payout_rows), Decimal("0")))
    prop_net_czk = q2(sum((row["net_czk"] for row in payout_rows), Decimal("0")))

    for row in payout_rows:
        status = row.get("qualification_status")
        if status == "unconfirmed":
            warnings.append(f"{row['firm_name']}: payout {date_pl_filter(row['received_date'])} opiera się na kwalifikacji do potwierdzenia.")
        elif status == "recommended":
            message = f"{row['firm_name']}: zastosowano rekomendowaną, niewiążącą kwalifikację {PROP_TAX_CLASSIFICATIONS.get(row.get('tax_classification'), row.get('tax_classification'))}."
            if message not in assumptions:
                assumptions.append(message)
    if prop_rows_excluded:
        warnings.append(f"{len(prop_rows_excluded)} payoutów o wartości {format_decimal(prop_excluded_revenue)} CZK wyłączono z automatycznego kalkulatora.")

    def d(key: str, default: str = "0") -> Decimal:
        try:
            return parse_decimal(settings.get(key, default), key)
        except ValueError:
            warnings.append(f"Nieprawidłowe ustawienie {key}; użyto {default}.")
            return Decimal(default)

    group60_revenue = q2(welding_revenue + prop_revenue_60)
    group40_revenue = prop_revenue_40
    grouped = calculate_grouped_percentage_costs(
        group60_revenue,
        group40_revenue,
        d("expense_percent", "60"),
        d("expense_limit", "1200000"),
        d("expense_40_percent", "40"),
        d("expense_40_limit", "800000"),
    )
    flat_expenses_60 = grouped["flat_expenses_60"]
    flat_expenses_40 = grouped["flat_expenses_40"]
    flat_expenses = grouped["flat_expenses"]
    profit_60 = grouped["profit_60"]
    profit_40 = grouped["profit_40"]
    profit = grouped["profit"]
    revenue = q2(group60_revenue + group40_revenue)

    expense_limit = max(d("expense_limit", "1200000"), Decimal("0"))
    group60_limit_usage_percent = q2(flat_expenses_60 / expense_limit * Decimal("100")) if expense_limit else Decimal("0")

    employment_base = max(d("employment_tax_base"), Decimal("0"))
    employment_tax_withheld = max(d("employment_tax_withheld"), Decimal("0"))
    tax_threshold = max(d("tax_threshold", "1762812"), Decimal("0"))
    annual_credit = max(d("income_tax_credit", "30840"), Decimal("0"))
    annual_tax_before_credit = progressive_tax(employment_base + profit, tax_threshold)
    annual_tax_after_credit = q2(max(annual_tax_before_credit - annual_credit, Decimal("0")))
    if year == 2026 and employment_base == 0:
        warnings.append("Brak podstawy podatku z zatrudnienia w Accenture – roczne wyliczenie podatku jest niepełne.")

    active_months = active_months_for_year(settings, year)
    cssz_mode = settings.get("cssz_mode", "main")
    cssz_main_months, cssz_secondary_months = status_month_counts(settings, year, "cssz_mode", "cssz_main_from_date")
    cssz_assessment_percent = max(d("cssz_assessment_percent", "55"), Decimal("0"))
    cssz_rate = max(d("cssz_rate_percent", "29.2"), Decimal("0"))
    cssz_main_min_month = max(d("cssz_min_monthly_base", "17139"), Decimal("0"))
    cssz_secondary_min_month = max(d("cssz_secondary_min_monthly_base", "5387"), Decimal("0"))
    threshold_annual = d("cssz_threshold_annual", "117521")
    threshold_reduction = d("cssz_threshold_reduction_month", "9794")
    cssz_threshold = max(threshold_annual - threshold_reduction * Decimal(12 - cssz_secondary_months), Decimal("0")) if cssz_secondary_months else Decimal("0")
    average_profit = profit / Decimal(active_months) if active_months else Decimal("0")
    main_profit = average_profit * Decimal(cssz_main_months)
    secondary_profit = average_profit * Decimal(cssz_secondary_months)
    calc_main_assessment = ceil_whole(main_profit * cssz_assessment_percent / Decimal("100"))
    calc_secondary_assessment = ceil_whole(secondary_profit * cssz_assessment_percent / Decimal("100"))
    min_main_assessment = ceil_whole(cssz_main_min_month * Decimal(cssz_main_months))
    min_secondary_assessment = ceil_whole(cssz_secondary_min_month * Decimal(cssz_secondary_months))
    cssz_assessment = Decimal("0")
    if cssz_mode == "secondary":
        if active_months and profit >= cssz_threshold:
            cssz_assessment = max(ceil_whole(profit * cssz_assessment_percent / Decimal("100")), min_secondary_assessment)
        else:
            warnings.append(f"ČSSZ vedlejší: dochód jest poniżej progu {format_decimal(cssz_threshold,0)} CZK; wyliczono 0 CZK.")
    elif cssz_mode == "main":
        cssz_assessment = max(ceil_whole(profit * cssz_assessment_percent / Decimal("100")), min_main_assessment)
    elif cssz_mode == "mixed":
        secondary_part_required = cssz_secondary_months > 0 and secondary_profit >= cssz_threshold
        if secondary_part_required:
            cssz_assessment = max(calc_main_assessment + calc_secondary_assessment, min_main_assessment + min_secondary_assessment, min_main_assessment + calc_secondary_assessment)
        else:
            cssz_assessment = max(calc_main_assessment, min_main_assessment)
    else:
        cssz_assessment = ceil_whole(profit * cssz_assessment_percent / Decimal("100"))
    cssz = ceil_whole(cssz_assessment * cssz_rate / Decimal("100")) if cssz_assessment > 0 else Decimal("0")

    vzp_mode = settings.get("vzp_mode", "main")
    vzp_main_months, vzp_secondary_months = status_month_counts(settings, year, "vzp_mode", "vzp_main_from_date")
    vzp_assessment_percent = max(d("vzp_assessment_percent", "50"), Decimal("0"))
    vzp_rate = max(d("vzp_rate_percent", "13.5"), Decimal("0"))
    vzp_min_month = max(d("vzp_min_monthly_base", "24483.50"), Decimal("0"))
    vzp_assessment = profit * vzp_assessment_percent / Decimal("100")
    if vzp_mode == "main":
        vzp_assessment = max(vzp_assessment, vzp_min_month * Decimal(active_months))
    elif vzp_mode == "mixed":
        vzp_assessment = max(vzp_assessment, vzp_min_month * Decimal(vzp_main_months))
    vzp = q2(vzp_assessment * vzp_rate / Decimal("100"))

    db = get_db()
    payment_rows = [dict(row) for row in db.execute("SELECT * FROM tax_payments WHERE substr(payment_date,1,4)=? ORDER BY payment_date DESC,id DESC", (str(year),)).fetchall()]
    paid = {key: Decimal("0") for key in TAX_PAYMENT_TYPES}
    for payment in payment_rows:
        try:
            paid[payment["payment_type"]] += parse_decimal(payment["amount"])
        except (KeyError, ValueError):
            warnings.append("Pominięto nieprawidłowy zapis wpłaty.")
    paid = {key: q2(value) for key, value in paid.items()}

    income_tax_paid_total = q2(employment_tax_withheld + paid.get("income_tax", Decimal("0")))
    income_tax_underpayment, income_tax_overpayment = settlement_balance(annual_tax_after_credit, income_tax_paid_total)
    cssz_underpayment, cssz_overpayment = settlement_balance(q2(cssz), paid.get("cssz", Decimal("0")))
    vzp_underpayment, vzp_overpayment = settlement_balance(vzp, paid.get("vzp", Decimal("0")))
    remaining = {"income_tax": income_tax_underpayment, "cssz": cssz_underpayment, "vzp": vzp_underpayment}
    overpayments = {"income_tax": income_tax_overpayment, "cssz": cssz_overpayment, "vzp": vzp_overpayment}
    remaining_total = q2(sum(remaining.values(), Decimal("0")))
    total_overpayment = q2(sum(overpayments.values(), Decimal("0")))
    total_obligation = q2(annual_tax_after_credit + cssz + vzp)
    total_paid = q2(income_tax_paid_total + paid.get("cssz", Decimal("0")) + paid.get("vzp", Decimal("0")))
    after_obligations = q2(revenue - remaining_total)

    forecast_welding_revenue = max(d("forecast_welding_revenue"), Decimal("0"))
    forecast_prop_revenue = max(d("forecast_prop_revenue"), Decimal("0"))
    forecast_total = q2(forecast_welding_revenue + forecast_prop_revenue)

    if basis == "paid" and not invoice_rows and not payout_rows:
        warnings.append("Brak rzeczywiście otrzymanych przychodów w tym roku.")

    return {
        "year": year, "settings": settings, "warnings": warnings, "assumptions": assumptions,
        "invoice_rows": invoice_rows, "prop_rows": payout_rows, "prop_rows_included": prop_rows_included,
        "prop_rows_excluded": prop_rows_excluded,
        "welding_revenue": welding_revenue, "prop_revenue": prop_revenue,
        "prop_revenue_60": prop_revenue_60, "prop_revenue_40": prop_revenue_40,
        "prop_excluded_revenue": prop_excluded_revenue,
        "prop_operator_fees_czk": prop_operator_fees_czk, "prop_net_czk": prop_net_czk,
        "group60_revenue": group60_revenue, "group40_revenue": group40_revenue,
        "revenue": revenue, "flat_expenses_60": flat_expenses_60, "flat_expenses_40": flat_expenses_40,
        "flat_expenses": flat_expenses, "profit_60": profit_60, "profit_40": profit_40, "profit": profit,
        "group60_limit_usage_percent": group60_limit_usage_percent,
        "annual_tax_before_credit": annual_tax_before_credit, "annual_tax_after_credit": annual_tax_after_credit,
        "employment_tax_withheld": employment_tax_withheld, "income_tax_paid_total": income_tax_paid_total,
        "income_tax": income_tax_underpayment, "income_tax_underpayment": income_tax_underpayment,
        "income_tax_overpayment": income_tax_overpayment,
        "cssz": q2(cssz), "vzp": vzp, "cssz_threshold": q2(cssz_threshold),
        "cssz_assessment": q2(cssz_assessment), "vzp_assessment": q2(vzp_assessment),
        "active_months": active_months, "cssz_main_months": cssz_main_months,
        "cssz_secondary_months": cssz_secondary_months, "vzp_main_months": vzp_main_months,
        "vzp_secondary_months": vzp_secondary_months,
        "paid": paid, "payments": payment_rows, "remaining": remaining, "overpayments": overpayments,
        "total_obligation": total_obligation, "total_paid": total_paid,
        "remaining_total": remaining_total, "total_overpayment": total_overpayment,
        "after_obligations": after_obligations,
        "forecast_welding_revenue": forecast_welding_revenue,
        "forecast_prop_revenue": forecast_prop_revenue, "forecast_total": forecast_total,
    }



@app.route("/settings")
def settings_page() -> str:
    try:
        year = int(request.args.get("year", date.today().year))
        if year < 2020 or year > 2100:
            raise ValueError
    except ValueError:
        year = date.today().year
    snapshot = compute_tax_snapshot(year)
    tab = request.args.get("tab", "tax")
    if tab not in {"tax", "forecast"}:
        tab = "tax"
    return render_template("settings.html", snapshot=snapshot, selected_year=year,
                           settings_tab=tab, forecast=compute_contribution_forecast(year))


@app.route("/taxes")
def taxes_dashboard() -> str:
    try:
        year = int(request.args.get("year", date.today().year))
        if year < 2020 or year > 2100:
            raise ValueError
    except ValueError:
        year = date.today().year
    snapshot = compute_tax_snapshot(year)
    return render_template("taxes_dashboard.html", snapshot=snapshot, payments=snapshot["payments"],
                           today=date.today().isoformat(), forecast=compute_contribution_forecast(year))


@app.post("/taxes/settings")
def tax_settings_save():
    try:
        year = int(request.form.get("year", date.today().year))
        values = {
            "activity_start_date": _valid_date_or_blank(request.form.get("activity_start_date", ""), "początek działalności"),
            "activity_end_date": _valid_date_or_blank(request.form.get("activity_end_date", ""), "koniec działalności"),
            "cssz_main_from_date": _valid_date_or_blank(request.form.get("cssz_main_from_date", ""), "początek hlavní ČSSZ"),
            "vzp_main_from_date": _valid_date_or_blank(request.form.get("vzp_main_from_date", ""), "początek minimum VZP"),
            "revenue_basis": request.form.get("revenue_basis", "paid"),
            "cssz_mode": request.form.get("cssz_mode", "main"),
            "vzp_mode": request.form.get("vzp_mode", "main"),
        }
        allowed_modes = {"secondary", "mixed", "main", "custom"}
        if values["revenue_basis"] not in {"paid", "issued"} or values["cssz_mode"] not in allowed_modes or values["vzp_mode"] not in allowed_modes:
            raise ValueError("Nieprawidłowy tryb ustawień.")
        numeric = (
            "expense_percent", "expense_limit", "expense_40_percent", "expense_40_limit",
            "forecast_welding_revenue", "forecast_prop_revenue",
            "income_tax_credit", "employment_tax_base", "employment_tax_withheld", "tax_threshold",
            "cssz_threshold_annual", "cssz_threshold_reduction_month", "cssz_assessment_percent", "cssz_rate_percent",
            "cssz_min_monthly_base", "cssz_secondary_min_monthly_base", "cssz_monthly_advance",
            "vzp_assessment_percent", "vzp_rate_percent", "vzp_min_monthly_base", "vzp_monthly_advance",
        )
        for key in numeric:
            parsed = parse_decimal(request.form.get(key, "0"), key)
            if parsed < 0:
                raise ValueError(f"{key} nie może być ujemne.")
            values[key] = str(parsed)
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings_page", year=request.form.get("year", date.today().year)))
    db = get_db()
    ensure_tax_settings(year)
    keys = list(values)
    db.execute("UPDATE tax_settings SET " + ",".join(f"{key}=?" for key in keys) + ",updated_at=CURRENT_TIMESTAMP WHERE year=?", tuple(values[key] for key in keys) + (year,))
    db.commit()
    flash("Ustawienia podatków i składek zostały zapisane.", "success")
    return redirect(url_for("settings_page", year=year))


@app.post("/taxes/payments/add")
def tax_payment_add():
    try:
        year = int(request.form.get("year", date.today().year))
        payment_date = _valid_date_or_blank(request.form.get("payment_date", ""), "data wpłaty")
        payment_type = request.form.get("payment_type", "")
        amount = parse_decimal(request.form.get("amount", ""), "kwota wpłaty")
        if not payment_date or payment_type not in TAX_PAYMENT_TYPES or amount <= 0:
            raise ValueError("Uzupełnij poprawnie dane wpłaty.")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error"); return redirect(url_for("taxes_dashboard", year=request.form.get("year", date.today().year)))
    get_db().execute("INSERT INTO tax_payments (payment_date,payment_type,amount,note) VALUES (?,?,?,?)",
                     (payment_date, payment_type, str(amount), request.form.get("note", "").strip()))
    get_db().commit(); flash("Wpłata została zapisana.", "success")
    return redirect(url_for("taxes_dashboard", year=year))


@app.post("/taxes/payments/<int:payment_id>/delete")
def tax_payment_delete(payment_id: int):
    db = get_db(); db.execute("DELETE FROM tax_payments WHERE id=?", (payment_id,)); db.commit()
    flash("Wpłata została usunięta.", "success")
    return redirect(url_for("taxes_dashboard", year=request.form.get("year", date.today().year)))


@app.route("/dph")
def dph_dashboard() -> str:
    today = date.today()
    try:
        year = int(request.args.get("year", today.year)); month = int(request.args.get("month", today.month))
        if month not in range(1,13): raise ValueError
    except ValueError: year, month = today.year, today.month
    report = build_dph_month_report(year, month); db = get_db()
    filing_row = db.execute("SELECT * FROM dph_filings WHERE period_year=? AND period_month=? AND filing_kind='SHV'", (year, month)).fetchone()
    filing = dict(filing_row) if filing_row else None
    start, end = month_bounds(year, month)
    expense_rows = db.execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? AND dph_obligation IN ('foreign_service','review') ORDER BY expense_date", (start, end)).fetchall()
    foreign_expenses = []
    for row in expense_rows:
        item = dict(row); item["amount_czk"] = expense_value_czk(item) or Decimal("0"); foreign_expenses.append(item)
    return render_template("dph_dashboard.html", selected_year=year, selected_month=month, period_label=period_label(year, month),
        report=report, due_date=dph_due_date(year, month).isoformat(), filing=filing, today=today.isoformat(),
        foreign_expenses=foreign_expenses, moje_dane_url=MOJE_DANE_SHV_URL, vies_url=VIES_URL)


@app.route("/dph/shv/<int:year>/<int:month>.xml")
def dph_export_shv(year: int, month: int):
    if month not in range(1,13): abort(404)
    report = build_dph_month_report(year, month)
    try: xml_data = build_shv_xml(year, month, report, get_company())
    except ValueError as exc:
        flash(str(exc), "error"); return redirect(url_for("dph_dashboard", year=year, month=month))
    return send_file(BytesIO(xml_data), mimetype="application/xml", as_attachment=True, download_name=f"DPHSHV_{year}_{month:02d}.xml")


@app.route("/dph/shv/<int:year>/<int:month>.csv")
def dph_export_csv(year: int, month: int):
    if month not in range(1,13): abort(404)
    report = build_dph_month_report(year, month); output = StringIO(); writer = csv.writer(output, delimiter=';')
    writer.writerow(["Kraj","VAT ID bez prefiksu","Kod plnění","Liczba transakcji","Wartość CZK"])
    for row in report["rows"]: writer.writerow([row["country_code"], row["vat_number"], 3, row["count"], int(row["value_czk"])])
    return Response('\ufeff' + output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=SHV_{year}_{month:02d}.csv"})


@app.post("/dph/mark-filed")
def dph_mark_filed():
    try:
        year = int(request.form.get("year", "")); month = int(request.form.get("month", "")); date.fromisoformat(request.form.get("filed_date", ""))
        if month not in range(1,13): raise ValueError
    except ValueError:
        flash("Nieprawidłowe dane zgłoszenia.", "error"); return redirect(url_for("dph_dashboard"))
    db = get_db(); db.execute("""INSERT INTO dph_filings (period_year,period_month,filing_kind,status,filed_date,confirmation_number,notes)
        VALUES (?,?,'SHV','filed',?,?,?) ON CONFLICT(period_year,period_month,filing_kind) DO UPDATE SET status='filed',
        filed_date=excluded.filed_date,confirmation_number=excluded.confirmation_number,notes=excluded.notes,updated_at=CURRENT_TIMESTAMP""",
        (year, month, request.form.get("filed_date", ""), request.form.get("confirmation_number", "").strip(), request.form.get("notes", "").strip()))
    db.commit(); flash("Zgłoszenie oznaczono jako wysłane.", "success")
    return redirect(url_for("dph_dashboard", year=year, month=month))


@app.post("/dph/reopen-filing")
def dph_reopen_filing():
    year = int(request.form.get("year", date.today().year)); month = int(request.form.get("month", date.today().month))
    db = get_db(); db.execute("UPDATE dph_filings SET status='draft',updated_at=CURRENT_TIMESTAMP WHERE period_year=? AND period_month=? AND filing_kind='SHV'", (year, month)); db.commit()
    flash("Status zmieniono na niewysłane.", "success"); return redirect(url_for("dph_dashboard", year=year, month=month))

@app.route("/invoices")
def invoices_list() -> str:
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    sql = """
        SELECT i.*, c.name AS contractor_name
        FROM invoices i JOIN contractors c ON c.id = i.contractor_id
        WHERE 1 = 1
    """
    params: list[Any] = []
    if q:
        sql += " AND (i.invoice_number LIKE ? OR c.name LIKE ? OR c.vat_id LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like])
    if status in {"paid", "unpaid"}:
        sql += " AND i.status = ?"
        params.append(status)
    sql += " ORDER BY i.issue_date DESC, i.id DESC"
    db = get_db()
    rows = db.execute(sql, params).fetchall()
    invoices: list[dict[str, Any]] = []
    for row in rows:
        invoice = dict(row)
        item_rows = db.execute("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice["id"],)).fetchall()
        totals = calculate_items([dict(item) for item in item_rows], invoice["tax_mode"])
        invoice["total"] = totals["total_gross"]
        invoices.append(invoice)
    return render_template("invoices_list.html", invoices=invoices, q=q, status=status)


def default_new_invoice(company: dict[str, Any], contractor_id: str = "") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    today = date.today(); due = today + timedelta(days=int(company["default_due_days"]))
    invoice = {"id": None, "invoice_number": "", "contractor_id": contractor_id,
        "issue_date": today.isoformat(), "supply_date": today.isoformat(), "due_date": due.isoformat(),
        "currency": company["default_currency"], "language": company["default_language"],
        "tax_mode": company["default_tax_mode"], "payment_method": "bank_transfer", "variable_symbol": "",
        "order_number": "", "reverse_charge_note": company["default_reverse_charge_note"], "notes": "",
        "czk_rate": "1" if company["default_currency"] == "CZK" else "", "dph_category": "auto", "status": "unpaid"}
    return invoice, [{"description": "Prace spawalniczo-montażowe", "quantity": "1", "unit": "h", "unit_price": "", "vat_rate": "0"}]


@app.route("/invoices/new", methods=["GET", "POST"])
def invoice_new() -> str:
    db = get_db(); company = get_company()
    contractors = [dict(row) for row in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    projects = [dict(row) for row in db.execute("SELECT * FROM projects WHERE active=1 ORDER BY project_number COLLATE NOCASE").fetchall()]
    if request.method == "GET":
        invoice, items = default_new_invoice(company, request.args.get("contractor_id", ""))
        return render_template("invoice_form.html", invoice=invoice, items=items, contractors=contractors, projects=projects, company=company, is_edit=False)
    invoice, items = invoice_form_from_request(); errors = validate_invoice(invoice, items)
    if not errors: errors.extend(prepare_invoice_fx(invoice))
    if errors:
        for error in errors: flash(error, "error")
        return render_template("invoice_form.html", invoice=invoice, items=items, contractors=contractors, projects=projects, company=company, is_edit=False)
    try:
        number, year, sequence = next_invoice_identity(db, invoice["issue_date"], company["invoice_prefix"])
        variable = invoice["variable_symbol"] or f"{year}{sequence:03d}"
        cursor = db.execute("""INSERT INTO invoices (invoice_number, invoice_year, sequence_no, contractor_id,
            issue_date, supply_date, due_date, currency, language, tax_mode, payment_method, variable_symbol,
            order_number, reverse_charge_note, notes, czk_rate, dph_category, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unpaid')""",
            (number, year, sequence, int(invoice["contractor_id"]), invoice["issue_date"], invoice["supply_date"],
             invoice["due_date"], invoice["currency"], invoice["language"], invoice["tax_mode"], invoice["payment_method"],
             variable, invoice["order_number"], invoice["reverse_charge_note"], invoice["notes"], invoice["czk_rate"],
             invoice["dph_category"]))
        invoice_id = int(cursor.lastrowid)
        for pos, item in enumerate(items, 1):
            vat = "0" if invoice["tax_mode"] != "vat" else str(parse_decimal(item["vat_rate"]))
            db.execute("INSERT INTO invoice_items (invoice_id, position, description, quantity, unit, unit_price, vat_rate) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (invoice_id, pos, item["description"].strip(), str(parse_decimal(item["quantity"])), item["unit"].strip(), str(parse_decimal(item["unit_price"])), vat))
        save_fx_quote("invoice", invoice_id, "document", invoice.get("_fx_quote"))
        db.commit()
    except Exception:
        db.rollback(); raise
    try:
        path = save_invoice_pdf_to_disk(invoice_id); flash(f"Faktura {number} została wystawiona i zapisana w: {path.parent}", "success")
    except Exception as exc:
        flash(f"Faktura {number} została wystawiona, ale zapis PDF nie powiódł się: {exc}", "error")
    created, _, totals = get_invoice_bundle(invoice_id); info = invoice_dph_info(created, totals)
    if info["kind"] == "eu_service": flash(f"DPH: faktura trafi do SH VIES za {info['period_label']}. Termin {date_pl_filter(info['due_date'])}.", "success")
    elif info["warning"]: flash(f"DPH: {info['warning']}", "error")
    return redirect(url_for("invoice_view", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>")
def invoice_view(invoice_id: int) -> str:
    invoice, items, totals = get_invoice_bundle(invoice_id)
    linked = get_db().execute(
        """SELECT p.*, f.name AS firm_name FROM prop_payouts p JOIN prop_firms f ON f.id=p.prop_firm_id WHERE p.linked_invoice_id=?""",
        (invoice_id,),
    ).fetchone()
    return render_template("invoice_detail.html", invoice=invoice, items=totals["items"], totals=totals,
                           company=get_company(), dph_info=invoice_dph_info(invoice, totals),
                           linked_payout=dict(linked) if linked else None, today=date.today().isoformat())



@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
def invoice_edit(invoice_id: int) -> str:
    db = get_db(); row = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None: abort(404)
    existing = dict(row); company = get_company()
    contractors = [dict(r) for r in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    projects = [dict(r) for r in db.execute("SELECT * FROM projects WHERE active=1 ORDER BY project_number COLLATE NOCASE").fetchall()]
    if request.method == "GET":
        items = [dict(r) for r in db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY position,id", (invoice_id,)).fetchall()]
        return render_template("invoice_form.html", invoice=existing, items=items, contractors=contractors, projects=projects, company=company, is_edit=True)
    invoice, items = invoice_form_from_request(existing); errors = validate_invoice(invoice, items)
    if not errors: errors.extend(prepare_invoice_fx(invoice, existing))
    if errors:
        for error in errors: flash(error, "error")
        return render_template("invoice_form.html", invoice=invoice, items=items, contractors=contractors, projects=projects, company=company, is_edit=True)
    db.execute("""UPDATE invoices SET contractor_id=?, issue_date=?, supply_date=?, due_date=?, currency=?, language=?,
        tax_mode=?, payment_method=?, variable_symbol=?, order_number=?, reverse_charge_note=?, notes=?, czk_rate=?,
        dph_category=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (int(invoice["contractor_id"]), invoice["issue_date"], invoice["supply_date"], invoice["due_date"], invoice["currency"],
         invoice["language"], invoice["tax_mode"], invoice["payment_method"], invoice["variable_symbol"], invoice["order_number"],
         invoice["reverse_charge_note"], invoice["notes"], invoice["czk_rate"], invoice["dph_category"], invoice_id))
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    for pos, item in enumerate(items, 1):
        vat = "0" if invoice["tax_mode"] != "vat" else str(parse_decimal(item["vat_rate"]))
        db.execute("INSERT INTO invoice_items (invoice_id, position, description, quantity, unit, unit_price, vat_rate) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (invoice_id, pos, item["description"].strip(), str(parse_decimal(item["quantity"])), item["unit"].strip(), str(parse_decimal(item["unit_price"])), vat))
    save_fx_quote("invoice", invoice_id, "document", invoice.get("_fx_quote"))
    db.commit()
    try:
        path = save_invoice_pdf_to_disk(invoice_id); flash(f"Faktura {existing['invoice_number']} została zaktualizowana. PDF: {path.parent}", "success")
    except Exception as exc:
        flash(f"Faktura została zaktualizowana, ale zapis PDF nie powiódł się: {exc}", "error")
    return redirect(url_for("invoice_view", invoice_id=invoice_id))


@app.post("/invoices/<int:invoice_id>/toggle-paid")
def invoice_toggle_paid(invoice_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    if row["status"] != "paid":
        return redirect(url_for('invoice_payment', invoice_id=invoice_id,
                                date=request.form.get('paid_date','')))
    db.execute("UPDATE invoices SET status='unpaid',paid_date='',updated_at=CURRENT_TIMESTAMP WHERE id=?", (invoice_id,))
    db.execute("DELETE FROM fx_quotes WHERE entity_type='invoice' AND entity_id=? AND purpose='payment'", (invoice_id,))
    db.commit()
    flash("Faktura została oznaczona jako nieopłacona. Kurs dokumentu pozostał bez zmian.", "success")
    return redirect(url_for('invoice_view', invoice_id=invoice_id))


@app.post("/invoices/<int:invoice_id>/delete")
def invoice_delete(invoice_id: int):
    db = get_db()
    row = db.execute("SELECT invoice_number FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM fx_quotes WHERE entity_type='invoice' AND entity_id=?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    db.commit()
    flash(f"Faktura {row['invoice_number']} została usunięta. Numer nie zostanie użyty ponownie.", "success")
    return redirect(url_for("invoices_list"))


_FONT_CACHE: tuple[str, str, bool] | None = None


def register_pdf_fonts() -> tuple[str, str, bool]:
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE

    env_regular = os.environ.get("INVOICE_FONT_REGULAR")
    env_bold = os.environ.get("INVOICE_FONT_BOLD")
    candidates = [
        (env_regular, env_bold),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        (r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\calibrib.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ]
    for regular, bold in candidates:
        if regular and Path(regular).exists():
            bold_path = bold if bold and Path(bold).exists() else regular
            try:
                pdfmetrics.registerFont(TTFont("InvoiceFont", regular))
                pdfmetrics.registerFont(TTFont("InvoiceFontBold", bold_path))
                _FONT_CACHE = ("InvoiceFont", "InvoiceFontBold", True)
                return _FONT_CACHE
            except Exception:
                continue
    _FONT_CACHE = ("Helvetica", "Helvetica-Bold", False)
    return _FONT_CACHE


def pdf_safe_text(value: Any, unicode_ok: bool) -> str:
    text = str(value or "")
    if unicode_ok:
        return text
    replacements = {"ß": "ss", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def pdf_number(value: Any, language: str, decimals: int = 2) -> str:
    number = parse_decimal(value)
    text = f"{number:,.{decimals}f}"
    if language in {"pl", "de", "cs"}:
        text = text.replace(",", "X").replace(".", ",").replace("X", " ")
    return text


def pdf_date(value: str, language: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    if language == "en":
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%d.%m.%Y")


def paragraph_text(value: Any, unicode_ok: bool) -> str:
    text = pdf_safe_text(value, unicode_ok)
    return escape(text).replace("\n", "<br/>")


def build_invoice_pdf(
    company: dict[str, Any],
    invoice: dict[str, Any],
    contractor: dict[str, Any],
    items: list[dict[str, Any]],
) -> BytesIO:
    language = invoice.get("language") if invoice.get("language") in PDF_LABELS else "en"
    labels = PDF_LABELS[language]
    font_regular, font_bold, unicode_ok = register_pdf_fonts()
    currency = invoice.get("currency", "EUR")
    totals = calculate_items(items, invoice["tax_mode"])

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title=f"{labels['invoice']} {invoice['invoice_number']}",
        author=company.get("name", ""),
    )

    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "InvoiceBase",
        parent=styles["Normal"],
        fontName=font_regular,
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#20252B"),
    )
    small = ParagraphStyle("InvoiceSmall", parent=base, fontSize=8, leading=10)
    tiny = ParagraphStyle("InvoiceTiny", parent=base, fontSize=7, leading=9)
    bold = ParagraphStyle("InvoiceBold", parent=base, fontName=font_bold)
    heading = ParagraphStyle(
        "InvoiceHeading",
        parent=base,
        fontName=font_bold,
        fontSize=22,
        leading=25,
        alignment=TA_RIGHT,
        textColor=colors.HexColor("#7A1735"),
    )
    subheading = ParagraphStyle(
        "InvoiceSubheading",
        parent=base,
        fontName=font_bold,
        fontSize=10,
        leading=12,
        textColor=colors.HexColor("#7A1735"),
    )
    right = ParagraphStyle("InvoiceRight", parent=base, alignment=TA_RIGHT)
    right_bold = ParagraphStyle("InvoiceRightBold", parent=bold, alignment=TA_RIGHT, fontSize=11)
    center_small = ParagraphStyle("InvoiceCenterSmall", parent=small, alignment=TA_CENTER)
    header_cell = ParagraphStyle(
        "InvoiceHeaderCell", parent=small, fontName=font_bold, fontSize=7.2, alignment=TA_CENTER,
        textColor=colors.white, leading=8
    )

    def P(value: Any, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(paragraph_text(value, unicode_ok), style)

    company_address = "\n".join(filter(None, [company.get("address"), " ".join(filter(None, [company.get("postal_code"), company.get("city")])), company.get("country")]))
    contractor_address = "\n".join(filter(None, [contractor.get("address"), " ".join(filter(None, [contractor.get("postal_code"), contractor.get("city")])), contractor.get("country")]))

    story: list[Any] = []

    top_table = Table(
        [
            [
                P(company.get("name", ""), ParagraphStyle("CompanyName", parent=bold, fontSize=14, leading=17)),
                P(labels["invoice"], heading),
            ],
            [
                P(company_address, small),
                P(f"{labels['invoice_no']}:\n{invoice['invoice_number']}", right_bold),
            ],
        ],
        colWidths=[102 * mm, 78 * mm],
    )
    top_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ]
        )
    )
    story.extend([top_table, Spacer(1, 5 * mm)])

    seller_lines = [company.get("name", ""), company_address]
    if company.get("company_id"):
        seller_lines.append(f"{labels['company_id']}: {company['company_id']}")
    if company.get("vat_id"):
        seller_lines.append(f"{labels['vat_id']}: {company['vat_id']}")
    if company.get("email"):
        seller_lines.append(f"{labels['email']}: {company['email']}")
    if company.get("phone"):
        seller_lines.append(f"{labels['phone']}: {company['phone']}")

    buyer_lines = [contractor.get("name", ""), contractor_address]
    if contractor.get("company_id"):
        buyer_lines.append(f"{labels['company_id']}: {contractor['company_id']}")
    if contractor.get("vat_id"):
        buyer_lines.append(f"{labels['vat_id']}: {contractor['vat_id']}")
    if contractor.get("email"):
        buyer_lines.append(f"{labels['email']}: {contractor['email']}")
    if contractor.get("phone"):
        buyer_lines.append(f"{labels['phone']}: {contractor['phone']}")

    parties = Table(
        [
            [P(labels["seller"], subheading), P(labels["buyer"], subheading)],
            [P("\n".join(filter(None, seller_lines))), P("\n".join(filter(None, buyer_lines)))],
        ],
        colWidths=[88 * mm, 88 * mm],
        hAlign="LEFT",
    )
    parties.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#C9CDD2")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E1E4E8")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4EAF0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.extend([parties, Spacer(1, 5 * mm)])

    info_rows = [
        [P(labels["issue_date"], bold), P(pdf_date(invoice["issue_date"], language)), P(labels["supply_date"], bold), P(pdf_date(invoice["supply_date"], language))],
        [P(labels["due_date"], bold), P(pdf_date(invoice["due_date"], language)), P(labels["payment_method"], bold), P(PDF_PAYMENT_METHODS.get(language, PDF_PAYMENT_METHODS["en"]).get(invoice["payment_method"], invoice["payment_method"]))],
    ]
    if invoice.get("order_number") or invoice.get("variable_symbol"):
        info_rows.append(
            [
                P(labels["order_no"], bold), P(invoice.get("order_number", "")),
                P(labels["variable_symbol"], bold), P(invoice.get("variable_symbol", "")),
            ]
        )
    info_table = Table(info_rows, colWidths=[39 * mm, 51 * mm, 39 * mm, 51 * mm])
    info_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D8DCE0")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F7F7F7")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F7F7F7")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ]
        )
    )
    story.extend([info_table, Spacer(1, 6 * mm)])

    if invoice["tax_mode"] == "vat":
        header = [labels["item_no"], labels["description"], labels["quantity"], labels["unit"], labels["unit_price"], labels["vat_rate"], labels["net"], labels["gross"]]
        data: list[list[Any]] = [[P(cell, header_cell) for cell in header]]
        for index, item in enumerate(totals["items"], start=1):
            data.append(
                [
                    P(index, center_small),
                    P(item["description"], small),
                    P(pdf_number(item["quantity_decimal"], language, 2), right),
                    P(item.get("unit", ""), center_small),
                    P(f"{pdf_number(item['unit_price_decimal'], language)} {currency}", right),
                    P(pdf_number(item["vat_rate_decimal"], language, 2), right),
                    P(f"{pdf_number(item['net'], language)} {currency}", right),
                    P(f"{pdf_number(item['gross'], language)} {currency}", right),
                ]
            )
        item_table = LongTable(data, colWidths=[9 * mm, 57 * mm, 17 * mm, 14 * mm, 22 * mm, 13 * mm, 23 * mm, 25 * mm], repeatRows=1)
    else:
        header = [labels["item_no"], labels["description"], labels["quantity"], labels["unit"], labels["unit_price"], labels["net"]]
        data = [[P(cell, header_cell) for cell in header]]
        for index, item in enumerate(totals["items"], start=1):
            data.append(
                [
                    P(index, center_small),
                    P(item["description"], small),
                    P(pdf_number(item["quantity_decimal"], language, 2), right),
                    P(item.get("unit", ""), center_small),
                    P(f"{pdf_number(item['unit_price_decimal'], language)} {currency}", right),
                    P(f"{pdf_number(item['net'], language)} {currency}", right),
                ]
            )
        item_table = LongTable(data, colWidths=[9 * mm, 86 * mm, 18 * mm, 16 * mm, 25 * mm, 26 * mm], repeatRows=1)

    item_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7A1735")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), font_bold),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C9CDD2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
            ]
        )
    )
    story.extend([item_table, Spacer(1, 5 * mm)])

    if invoice["tax_mode"] == "vat":
        summary_rows = [
            [P(labels["net"], bold), P(f"{pdf_number(totals['total_net'], language)} {currency}", right)],
            [P(labels["vat"], bold), P(f"{pdf_number(totals['total_vat'], language)} {currency}", right)],
            [P(labels["total_due"], ParagraphStyle("TotalLabel", parent=bold, fontSize=12)), P(f"{pdf_number(totals['total_gross'], language)} {currency}", ParagraphStyle("TotalValue", parent=right_bold, fontSize=13, textColor=colors.HexColor('#7A1735')))],
        ]
    else:
        summary_rows = [
            [P(labels["total_due"], ParagraphStyle("TotalLabel2", parent=bold, fontSize=12)), P(f"{pdf_number(totals['total_gross'], language)} {currency}", ParagraphStyle("TotalValue2", parent=right_bold, fontSize=13, textColor=colors.HexColor('#7A1735')))],
        ]
    summary = Table(summary_rows, colWidths=[55 * mm, 45 * mm], hAlign="RIGHT")
    summary.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9CDD2")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F4EAF0")),
                ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ]
        )
    )
    story.extend([summary, Spacer(1, 5 * mm)])

    blocks: list[Any] = []
    if invoice["tax_mode"] == "reverse_charge":
        note = invoice.get("reverse_charge_note") or company.get("default_reverse_charge_note") or labels["reverse_charge_default"]
        note_table = Table([[P(note, bold)]], colWidths=[180 * mm])
        note_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#7A1735")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7FA")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                ]
            )
        )
        blocks.extend([note_table, Spacer(1, 4 * mm)])
    elif invoice["tax_mode"] == "no_vat":
        blocks.extend([P(labels["no_vat"], bold), Spacer(1, 3 * mm)])

    payment_lines = []
    if company.get("bank_name"):
        payment_lines.append(f"{labels['bank']}: {company['bank_name']}")
    if company.get("bank_account"):
        payment_lines.append(f"{labels['account']}: {company['bank_account']}")
    if company.get("iban"):
        payment_lines.append(f"{labels['iban']}: {company['iban']}")
    if company.get("bic"):
        payment_lines.append(f"{labels['bic']}: {company['bic']}")
    if invoice.get("variable_symbol"):
        payment_lines.append(f"{labels['variable_symbol']}: {invoice['variable_symbol']}")

    if payment_lines:
        payment_table = Table(
            [[P(labels["bank_details"], subheading)], [P("\n".join(payment_lines))]],
            colWidths=[180 * mm],
        )
        payment_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8DCE0")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7F7F7")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ]
            )
        )
        blocks.extend([payment_table, Spacer(1, 4 * mm)])

    if invoice.get("notes"):
        notes_table = Table(
            [[P(labels["notes"], subheading)], [P(invoice["notes"])]] ,
            colWidths=[180 * mm],
        )
        notes_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8DCE0")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7F7F7")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ]
            )
        )
        blocks.append(notes_table)
    if blocks:
        story.append(KeepTogether(blocks))

    footer_name = pdf_safe_text(company.get("name", ""), unicode_ok)
    footer_ids = " | ".join(filter(None, [
        f"IČO: {pdf_safe_text(company.get('company_id'), unicode_ok)}" if company.get("company_id") else "",
        f"DIČ: {pdf_safe_text(company.get('vat_id'), unicode_ok)}" if company.get("vat_id") else "",
    ]))

    def draw_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D8DCE0"))
        canvas.setLineWidth(0.4)
        canvas.line(15 * mm, 14 * mm, A4[0] - 15 * mm, 14 * mm)
        canvas.setFont(font_regular, 7.5)
        canvas.setFillColor(colors.HexColor("#666A70"))
        canvas.drawString(15 * mm, 9 * mm, f"{footer_name} | {footer_ids}".strip(" |"))
        page_text = f"{pdf_safe_text(labels['page'], unicode_ok)} {document.page}"
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, page_text)
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    buffer.seek(0)
    return buffer


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_folder_name(value: str, fallback: str = "Bez nazwy") -> str:
    """Tworzy bezpieczną nazwę folderu również dla Windows."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:120]


def invoice_pdf_destination(invoice: dict[str, Any]) -> Path:
    contractor_folder = safe_folder_name(invoice.get("contractor_name", ""), "Nieznany kontrahent")
    try:
        year = str(date.fromisoformat(invoice["issue_date"]).year)
    except (KeyError, TypeError, ValueError):
        year = str(invoice.get("invoice_year") or "Bez roku")
    safe_number = re.sub(r"[^A-Za-z0-9._-]+", "_", invoice["invoice_number"])
    folder = INVOICE_PDF_DIR / contractor_folder / year
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"Faktura_{safe_number}.pdf"


def save_invoice_pdf_to_disk(invoice_id: int) -> Path:
    """Buduje aktualny PDF i zapisuje go w folderze danego kontrahenta."""
    invoice, items, _totals = get_invoice_bundle(invoice_id)
    company = get_company()
    contractor = {
        "name": invoice["contractor_name"],
        "address": invoice["contractor_address"],
        "postal_code": invoice["contractor_postal_code"],
        "city": invoice["contractor_city"],
        "country": invoice["contractor_country"],
        "company_id": invoice["contractor_company_id"],
        "vat_id": invoice["contractor_vat_id"],
        "email": invoice["contractor_email"],
        "phone": invoice["contractor_phone"],
    }
    buffer = build_invoice_pdf(company, invoice, contractor, items)
    destination = invoice_pdf_destination(invoice)
    data = buffer.getvalue()

    # Gdy zmieniono kontrahenta lub jego nazwę, usuń starą kopię tej samej faktury.
    for old_copy in INVOICE_PDF_DIR.rglob(destination.name):
        try:
            if old_copy.resolve() != destination.resolve():
                old_copy.unlink()
        except OSError:
            pass

    temp_path = destination.with_name(destination.name + ".tmp")
    temp_path.write_bytes(data)
    os.replace(temp_path, destination)
    return destination


@app.route("/invoices/<int:invoice_id>/pdf")
def invoice_pdf(invoice_id: int):
    invoice, items, _totals = get_invoice_bundle(invoice_id)
    company = get_company()
    contractor = {
        "name": invoice["contractor_name"],
        "address": invoice["contractor_address"],
        "postal_code": invoice["contractor_postal_code"],
        "city": invoice["contractor_city"],
        "country": invoice["contractor_country"],
        "company_id": invoice["contractor_company_id"],
        "vat_id": invoice["contractor_vat_id"],
        "email": invoice["contractor_email"],
        "phone": invoice["contractor_phone"],
    }
    buffer = build_invoice_pdf(company, invoice, contractor, items)
    destination = invoice_pdf_destination(invoice)
    data = buffer.getvalue()
    temp_path = destination.with_name(destination.name + ".tmp")
    temp_path.write_bytes(data)
    os.replace(temp_path, destination)
    buffer.seek(0)

    safe_number = re.sub(r"[^A-Za-z0-9._-]+", "_", invoice["invoice_number"])
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"Faktura_{safe_number}.pdf",
    )


@app.route("/tools/open-invoices-folder")
def open_invoices_folder():
    INVOICE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if CLOUD_MODE:
            flash("W wersji online folder znajduje się na serwerze. PDF pobieraj z widoku konkretnej faktury.", "success")
        elif os.name == "nt":
            os.startfile(str(INVOICE_PDF_DIR))  # type: ignore[attr-defined]
            flash("Otworzono folder faktur.", "success")
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(INVOICE_PDF_DIR)])
            flash("Otworzono folder faktur.", "success")
        else:
            subprocess.Popen(["xdg-open", str(INVOICE_PDF_DIR)])
            flash("Otworzono folder faktur.", "success")
    except Exception as exc:
        flash(f"Nie udało się otworzyć folderu. Ścieżka: {INVOICE_PDF_DIR}. Błąd: {exc}", "error")
    return redirect(url_for("tools_page"))



def ensure_daily_backup() -> None:
    """Jedna automatyczna kopia bazy na dzień; zachowuj 14 ostatnich."""
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return
    target = BACKUP_DIR / f"auto_{date.today().isoformat()}.sqlite3"
    if target.exists():
        return
    try:
        source = sqlite3.connect(DB_PATH)
        destination = sqlite3.connect(target)
        with destination:
            source.backup(destination)
        destination.close()
        source.close()
        backups = sorted(BACKUP_DIR.glob("auto_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[14:]:
            try:
                old.unlink()
            except OSError:
                pass
        if REMOTE_DB_ENABLED:
            # 7 rotacyjnych slotów w chmurze, bez rosnącego zużycia miejsca.
            slot = date.today().weekday()
            upload_file_to_supabase(target, f"backups/slot_{slot}.sqlite3")
    except Exception as exc:
        print("Automatyczny backup nie powiódł się:", exc)


def create_database_backup(label: str = "backup") -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_") or "backup"
    destination = BACKUP_DIR / f"{safe_label}_{timestamp}.sqlite3"
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return destination


def validate_invoice_database(path: Path) -> tuple[bool, str]:
    required = {"company", "contractors", "invoice_sequences", "invoices", "invoice_items"}
    try:
        connection = sqlite3.connect(path)
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        connection.close()
    except sqlite3.Error as exc:
        return False, f"Nie można odczytać bazy SQLite: {exc}"
    tables = {row[0] for row in rows}
    missing = required - tables
    if missing:
        return False, "Wybrany plik nie jest bazą tej aplikacji. Brakuje tabel: " + ", ".join(sorted(missing))
    if not integrity or integrity[0] != "ok":
        return False, "Baza danych nie przeszła kontroli integralności."
    return True, "ok"


@app.route("/tools")
def tools_page() -> str:
    db = get_db()
    counts = {
        "invoices": db.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"],
        "contractors": db.execute("SELECT COUNT(*) AS c FROM contractors").fetchone()["c"],
        "expenses": db.execute("SELECT COUNT(*) AS c FROM expenses").fetchone()["c"],
        "prop_firms": db.execute("SELECT COUNT(*) AS c FROM prop_firms").fetchone()["c"],
        "prop_payouts": db.execute("SELECT COUNT(*) AS c FROM prop_payouts").fetchone()["c"],
    }
    sequences = db.execute("SELECT year,next_number FROM invoice_sequences ORDER BY year").fetchall()
    next_numbers = ", ".join(f"{row['year']}: {row['next_number']}" for row in sequences) or "1"
    return render_template("tools.html", counts=counts, next_numbers=next_numbers, db_path=str(DB_PATH), invoices_path=str(INVOICE_PDF_DIR), remote_db_enabled=REMOTE_DB_ENABLED, remote_bucket=SUPABASE_BUCKET)



@app.post("/tools/reset-invoices")
def reset_invoices():
    if request.form.get("confirmation", "").strip().upper() != "RESET":
        flash("Reset anulowany. Wpisz dokładnie RESET.", "error"); return redirect(url_for("tools_page"))
    db = get_db(); invoice_count = int(db.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"])
    contractor_count = int(db.execute("SELECT COUNT(*) AS c FROM contractors").fetchone()["c"])
    expense_count = int(db.execute("SELECT COUNT(*) AS c FROM expenses").fetchone()["c"])
    backup_path = create_database_backup("przed_resetem_faktur"); pdf_archive = None
    if INVOICE_PDF_DIR.exists() and any(INVOICE_PDF_DIR.iterdir()):
        pdf_archive = BACKUP_DIR / f"PDF_przed_resetem_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.move(str(INVOICE_PDF_DIR), str(pdf_archive)); INVOICE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    try:
        db.execute("DELETE FROM fx_quotes WHERE entity_type='invoice'")
        db.execute("DELETE FROM invoice_items"); db.execute("DELETE FROM invoices"); db.execute("DELETE FROM invoice_sequences")
        db.execute("DELETE FROM dph_filings"); db.execute("DELETE FROM sqlite_sequence WHERE name IN ('invoices','invoice_items')"); db.commit()
    except Exception:
        db.rollback(); raise
    flash(f"Usunięto {invoice_count} faktur. Zachowano {contractor_count} kontrahentów oraz ustawienia podatków i składek. Kopia: {backup_path.name}." + (f" PDF: {pdf_archive}" if pdf_archive else ""), "success")
    return redirect(url_for("tools_page"))


@app.post("/tools/import-database")
def import_database():
    uploaded = request.files.get("database")
    if uploaded is None or not uploaded.filename:
        flash("Wybierz plik bazy danych.", "error")
        return redirect(url_for("tools_page"))

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in {".db", ".sqlite", ".sqlite3"}:
        flash("Dozwolone są pliki .db, .sqlite i .sqlite3.", "error")
        return redirect(url_for("tools_page"))

    fd, temp_name = tempfile.mkstemp(prefix="faktury_import_", suffix=suffix, dir=DATA_DIR)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        uploaded.save(temp_path)
        valid, message = validate_invoice_database(temp_path)
        if not valid:
            flash(message, "error")
            return redirect(url_for("tools_page"))

        backup_path = create_database_backup("przed_importem") if DB_PATH.exists() else None
        current = g.pop("db", None)
        if current is not None:
            current.close()
        os.replace(temp_path, DB_PATH)
        ts_backup_before_migration()
        v83_backup_before_migration()
        v82_backup_before_migration()
        init_db()
        init_tax_module()
        init_prop_firms_module()
        init_v82_module()
        init_v83_module()
        init_timesheets_module()
        contractor_count = get_db().execute("SELECT COUNT(*) AS c FROM contractors").fetchone()["c"]
        backup_note = f" Poprzednia baza: {backup_path.name}." if backup_path else ""
        flash(f"Baza została zaimportowana. Wczytano {contractor_count} kontrahentów.{backup_note}", "success")
        return redirect(url_for("tools_page"))
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


@app.route("/backup")
def backup_database():
    db = get_db()
    db.commit()
    backup_path = create_database_backup("faktury_backup")
    return send_file(
        backup_path,
        as_attachment=True,
        download_name=f"faktury_backup_{date.today().isoformat()}.sqlite3",
    )


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


def open_browser() -> None:
    webbrowser.open_new("http://127.0.0.1:5000")


# ---------------- V8.3: official CNB daily rates, immutable snapshots ----------------
V83_MIGRATION_KEY = 'v8_3_cnb_exchange_rates'
CNB_DAILY_URL = ('https://www.cnb.cz/cs/financni-trhy/devizovy-trh/'
                 'kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt')
_FX_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_FX_CACHE_LOCK = threading.RLock()


class FxError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def fx_today() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Europe/Prague')).date()


def fx_easter(year: int) -> date:
    # Gregorian computus, used only to identify Czech bank holidays.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    gg = (b - f + 1) // 3
    h = (19*a + b - d - gg + 15) % 30
    i, k = c // 4, c % 4
    ll = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*ll) // 451
    n = h + ll - 7*m + 114
    return date(year, n // 31, n % 31 + 1)


def fx_is_business_day(day: date) -> bool:
    fixed = {(1,1), (5,1), (5,8), (7,5), (7,6), (9,28), (10,28), (11,17), (12,24), (12,25), (12,26)}
    easter = fx_easter(day.year)
    holidays = {easter + timedelta(days=1)}
    if day.year >= 2016:
        holidays.add(easter - timedelta(days=2))
    return day.weekday() < 5 and (day.month, day.day) not in fixed and day not in holidays


def fx_publication_date(day: date) -> date:
    while not fx_is_business_day(day):
        day -= timedelta(days=1)
    return day


def parse_cnb_daily_text(text: str) -> dict[str, Any]:
    """Parse documented Czech TXT format. Rates are CZK per ONE currency unit."""
    lines = [line.strip() for line in text.lstrip('\ufeff').splitlines() if line.strip()]
    match = re.fullmatch(r'(\d{2})\.(\d{2})\.(\d{4})\s+#(\d+)', lines[0] if lines else '')
    if not match or len(lines) < 3 or '|' not in lines[1]:
        raise FxError('ČNB zwrócił nieprawidłowy format danych. Żaden kurs nie został zapisany.', 502)
    try:
        published = date(int(match[3]), int(match[2]), int(match[1]))
    except ValueError as exc:
        raise FxError('Nieprawidłowa data tabeli ČNB.', 502) from exc
    rates = {}
    for line in lines[2:]:
        columns = line.split('|')
        if len(columns) != 5:
            raise FxError('Nieprawidłowa pozycja w tabeli ČNB.', 502)
        country, name, quantity, code, price = columns
        if not re.fullmatch(r'[A-Z]{3}', code):
            raise FxError('Nieprawidłowy kod waluty w danych ČNB.', 502)
        try:
            amount = Decimal(quantity)
            rate = Decimal(price.replace(',', '.'))
            if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
                raise ValueError
            if not rate.is_finite() or rate <= 0:
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            raise FxError('Nieprawidłowa wartość kursu ČNB.', 502) from exc
        if code in rates:
            raise FxError('Powtórzona waluta w danych ČNB.', 502)
        rates[code] = {'rate': format(rate / amount, 'f'), 'amount': int(amount),
                       'table_rate': format(rate, 'f')}
    if 'EUR' not in rates or 'USD' not in rates:
        raise FxError('Niekompletna tabela ČNB.', 502)
    return {'published_date': published.isoformat(), 'rates': rates}


def fetch_cnb_rate(currency: str, requested_date: str) -> dict[str, Any]:
    """No guessed/stale rates, no USD alias for USDT, no future dates."""
    import time
    currency = str(currency).strip().upper()
    try:
        day = date.fromisoformat(requested_date)
    except (ValueError, TypeError) as exc:
        raise FxError('Wybierz poprawną datę kursu.') from exc
    today = fx_today()
    if day > today:
        raise FxError('Kursu z przyszłości jeszcze nie ma. Wybierz rzeczywistą datę albo wpisz kurs ręcznie.')
    if day.year < 2002:
        raise FxError('Automatyczne kursy w tej wersji obsługują daty od 2002 roku.')
    if currency == 'CZK':
        return {'currency': 'CZK', 'rate': '1', 'requested_date': day.isoformat(),
                'published_date': day.isoformat(), 'source': 'identity', 'source_url': '',
                'fetched_at': datetime.now().isoformat(timespec='seconds'), 'notice': '1 CZK = 1 CZK'}
    if currency in {'USDT','USDC','BTC','ETH','DAI','WETH','MUSD'}:
        raise FxError('ČNB nie publikuje kursu tego tokena. Wpisz udokumentowaną wartość 1 tokena w CZK ręcznie. Nie przyjmujemy automatycznie USDT = USD.')
    if not re.fullmatch(r'[A-Z]{3}', currency):
        raise FxError('Podaj trzyznakowy kod waluty, np. EUR, USD lub PLN.')
    expected = fx_publication_date(day)
    key = expected.isoformat()
    url = CNB_DAILY_URL + '?' + urllib.parse.urlencode({'date': expected.strftime('%d.%m.%Y')})
    with _FX_CACHE_LOCK:
        cached = _FX_CACHE.get(key)
        ttl = 300 if expected == today else 86400
        table = cached[1] if cached and time.monotonic() - cached[0] < ttl else None
        if table is None:
            req = urllib.request.Request(url, headers={'Accept':'text/plain', 'User-Agent':'FakturyOSVC/8.3'})
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    host = urllib.parse.urlparse(response.geturl()).hostname
                    if host not in {'www.cnb.cz','cnb.cz'}:
                        raise FxError('Nieoczekiwane przekierowanie serwera ČNB.', 502)
                    raw = response.read(100001)
                    if len(raw) > 100000:
                        raise FxError('Odpowiedź ČNB jest zbyt duża.', 502)
                table = parse_cnb_daily_text(raw.decode('utf-8-sig'))
            except FxError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError, UnicodeError) as exc:
                raise FxError('Nie udało się połączyć z ČNB. Spróbuj ponownie później albo wybierz kurs ręczny; nie użyto przypadkowego kursu.', 503) from exc
            if table['published_date'] != key:
                if expected == today:
                    raise FxError('Kurs ČNB na ten dzień nie jest jeszcze opublikowany (zwykle około 14:30 czasu czeskiego). Nie podstawiamy kursu z wczoraj.', 409)
                raise FxError('Data tabeli ČNB nie odpowiada wybranemu dniowi. Sprawdź datę lub wpisz kurs ręcznie.', 502)
            if len(_FX_CACHE) >= 512:
                _FX_CACHE.pop(next(iter(_FX_CACHE)))
            _FX_CACHE[key] = (time.monotonic(), table)
    if currency not in table['rates']:
        raise FxError(f'Brak waluty {currency} w dziennej tabeli ČNB. Wybierz udokumentowany kurs ręcznie.')
    data = table['rates'][currency]
    notice = (f'Kurs ČNB z {expected.strftime("%d.%m.%Y")}, ważny na {day.strftime("%d.%m.%Y")}. '
              f'1 {currency} = {data["rate"]} CZK.')
    if day != expected:
        notice += ' Uwzględniono weekend / czeskie święto.'
    return {'currency':currency, **data, 'requested_date':day.isoformat(),
            'published_date':key, 'source':'cnb', 'source_url':url,
            'fetched_at':datetime.now().isoformat(timespec='seconds'), 'notice':notice}


def v83_backup_before_migration() -> Path | None:
    if not DB_PATH.exists() or not DB_PATH.stat().st_size:
        return None
    with sqlite3.connect(DB_PATH) as source:
        table = source.execute("SELECT 1 FROM sqlite_master WHERE name='app_migrations'").fetchone()
        if table and source.execute('SELECT 1 FROM app_migrations WHERE migration_key=?', (V83_MIGRATION_KEY,)).fetchone():
            return None
        target = BACKUP_DIR / ('przed_aktualizacja_V8_3_kursy_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.sqlite3')
        with sqlite3.connect(target) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Niepoprawna kopia bezpieczeństwa. Migracja V8.3 zatrzymana.')
    if REMOTE_DB_ENABLED and not upload_file_to_supabase(target, 'backups/' + target.name):
        raise RuntimeError('Nie zapisano kopii V8.3 w Supabase. Migracja zatrzymana.')
    return target


def init_v83_module() -> None:
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS fx_preferences (
            id INTEGER PRIMARY KEY CHECK(id=1), default_auto INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS fx_quotes (
            entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, purpose TEXT NOT NULL,
            currency TEXT NOT NULL, rate TEXT NOT NULL, requested_date TEXT NOT NULL,
            published_date TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(entity_type,entity_id,purpose)
        );
        CREATE TABLE IF NOT EXISTS fx_quote_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
            purpose TEXT NOT NULL, currency TEXT NOT NULL, rate TEXT NOT NULL, requested_date TEXT NOT NULL,
            published_date TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL DEFAULT '',
            saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO fx_preferences(id,default_auto) VALUES (1,1);
    ''')
    db.execute('INSERT OR IGNORE INTO app_migrations(migration_key) VALUES (?)', (V83_MIGRATION_KEY,))
    db.commit()


def fx_quote(entity_type: str, entity_id: int | None, purpose: str = 'document') -> dict[str, Any] | None:
    if not entity_id:
        return None
    row = get_db().execute('SELECT * FROM fx_quotes WHERE entity_type=? AND entity_id=? AND purpose=?',
                           (entity_type,entity_id,purpose)).fetchone()
    return dict(row) if row else None


def fx_default_auto() -> bool:
    row = get_db().execute('SELECT default_auto FROM fx_preferences WHERE id=1').fetchone()
    return bool(row and row['default_auto'])


def fx_form_mode(is_edit: bool) -> str:
    if request.method == 'POST' and request.form.get('fx_mode') in {'cnb','manual','keep'}:
        return request.form['fx_mode']
    return 'keep' if is_edit else ('cnb' if fx_default_auto() else 'manual')


def resolve_form_fx(currency: str, requested_date: str, raw_rate: str,
                    existing: dict[str, Any] | None = None,
                    previous_quote: dict[str, Any] | None = None,
                    old_date: str | None = None) -> tuple[str, dict[str, Any] | None]:
    """Server-authoritative lookup; request cannot label a made-up value as CNB."""
    mode = request.form.get('fx_mode', ('keep' if existing else ('manual' if raw_rate else 'cnb')))
    if mode not in {'cnb','manual','keep'}:
        raise FxError('Wybierz sposób ustalenia kursu.')
    if mode == 'keep':
        if not existing:
            raise FxError('Nie ma wcześniejszego kursu do zachowania.')
        if currency != existing['currency'] or (old_date is not None and requested_date != old_date):
            raise FxError('Zmieniono walutę lub datę kursu. Wybierz pobranie ČNB albo wpisz kurs ręcznie.')
        return str(existing.get('czk_rate') or ''), previous_quote
    try:
        date.fromisoformat(requested_date)
    except (ValueError, TypeError) as exc:
        raise FxError('Wybierz datę kursu.') from exc
    if mode == 'cnb':
        quote = fetch_cnb_rate(currency, requested_date)
        return quote['rate'], quote
    if currency == 'CZK':
        rate = Decimal('1')
    else:
        try:
            rate = Decimal(str(raw_rate).strip().replace(' ', '').replace(',', '.'))
            if not rate.is_finite() or rate <= 0:
                raise ValueError
        except (InvalidOperation,ValueError) as exc:
            raise FxError('Podaj dodatni, skończony kurs 1 jednostki waluty do CZK.') from exc
    return format(rate,'f'), {'currency':currency,'rate':format(rate,'f'),
        'requested_date':requested_date,'published_date':'','source':'manual',
        'source_url':'','fetched_at':datetime.now().isoformat(timespec='seconds')}


def save_fx_quote(entity_type: str, entity_id: int, purpose: str, quote: dict[str, Any] | None) -> None:
    if quote is None:
        return
    keys = ('currency','rate','requested_date','published_date','source','source_url','fetched_at')
    vals = (entity_type,entity_id,purpose) + tuple(str(quote.get(key) or '') for key in keys)
    db = get_db()
    old = fx_quote(entity_type,entity_id,purpose)
    if old and all(str(old.get(k) or '') == str(quote.get(k) or '') for k in keys):
        return
    db.execute('''INSERT INTO fx_quotes(entity_type,entity_id,purpose,currency,rate,requested_date,published_date,source,source_url,fetched_at)
      VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id,purpose) DO UPDATE SET
      currency=excluded.currency,rate=excluded.rate,requested_date=excluded.requested_date,
      published_date=excluded.published_date,source=excluded.source,source_url=excluded.source_url,fetched_at=excluded.fetched_at''', vals)
    db.execute('''INSERT INTO fx_quote_history(entity_type,entity_id,purpose,currency,rate,requested_date,published_date,source,source_url,fetched_at)
      VALUES (?,?,?,?,?,?,?,?,?,?)''', vals)


def prepare_invoice_fx(invoice: dict[str, Any], existing: dict[str, Any] | None = None) -> list[str]:
    old = fx_quote('invoice',existing['id'],'document') if existing else None
    target = request.form.get('fx_date','').strip() or invoice.get('supply_date','')
    invoice['fx_requested_date'] = target
    try:
        previous_date = (old['requested_date'] if old else existing['supply_date']) if existing else None
        if existing and invoice['supply_date'] != existing['supply_date'] and request.form.get('fx_mode','keep') == 'keep':
            raise FxError('Po zmianie daty usługi wybierz ponownie kurs ČNB lub zatwierdź kurs ręczny.')
        rate, quote = resolve_form_fx(invoice['currency'],target,invoice.get('czk_rate',''),existing,old,previous_date)
        invoice['czk_rate'], invoice['_fx_quote'] = rate, quote
        return []
    except FxError as exc:
        return [str(exc)]


def income_rate_for_invoice(invoice: dict[str, Any], warnings: list[str]) -> Decimal | None:
    if invoice.get('currency') == 'CZK':
        return Decimal('1')
    quote = fx_quote('invoice',invoice['id'],'payment')
    if quote:
        if quote['currency'] == invoice['currency'] and quote['requested_date'] == invoice.get('paid_date'):
            return decimal_rate(invoice['currency'],quote['rate'])
        warnings.append(f"{invoice['invoice_number']}: kurs otrzymanej zapłaty nie pasuje do waluty/daty. Popraw zapłatę; przychód pominięty.")
        return None
    # Keep historical calculations stable, but do not present the old rate as verified.
    warnings.append(f"{invoice['invoice_number']}: brak osobnego kursu zapłaty; zachowano starszy kurs faktury. Zweryfikuj go w 'Data i kurs zapłaty'.")
    return decimal_rate(invoice.get('currency'), invoice.get('czk_rate'))


@app.context_processor
def inject_fx_globals():
    return {'fx_quote':fx_quote,'fx_form_mode':fx_form_mode}


@app.get('/api/fx/cnb')
def cnb_rate_api():
    try:
        result = fetch_cnb_rate(request.args.get('currency',''), request.args.get('date',''))
        response = app.json.response({'ok':True, **result})
    except FxError as exc:
        response = app.json.response({'ok':False,'error':str(exc)})
        response.status_code = exc.status
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/settings/exchange-rates',methods=['GET','POST'])
def fx_settings_page():
    if request.method == 'POST':
        get_db().execute('UPDATE fx_preferences SET default_auto=? WHERE id=1',
                         (1 if request.form.get('default_auto') == '1' else 0,))
        get_db().commit()
        flash('Zapisano ustawienie automatycznych kursów. Starsze dokumenty pozostają bez zmian.','success')
        return redirect(url_for('fx_settings_page'))
    return render_template('fx_settings.html',default_auto=fx_default_auto(),settings_tab='fx')


@app.route('/invoices/<int:invoice_id>/payment',methods=['GET','POST'])
def invoice_payment(invoice_id: int):
    invoice, items, totals = get_invoice_bundle(invoice_id)
    linked = get_db().execute('SELECT id FROM prop_payouts WHERE linked_invoice_id=?',(invoice_id,)).fetchone()
    if linked:
        flash('Ta faktura jest powiązana z payoutem. Datę i kurs przychodu zapisuj w payoucie; nie dodajemy drugiej płatności.','success')
        return redirect(url_for('prop_payout_edit',payout_id=linked['id']))
    old = fx_quote('invoice',invoice_id,'payment')
    paid_date = invoice.get('paid_date') or request.args.get('date') or fx_today().isoformat()
    rate = old['rate'] if old else ''
    is_edit = bool(old)
    if request.method == 'POST':
        paid_date = request.form.get('paid_date','').strip()
        rate = request.form.get('czk_rate','').strip()
        try:
            day = date.fromisoformat(paid_date)
            if day > fx_today():
                raise FxError('Zapłata nie może mieć daty z przyszłości.')
            existing = {'currency':old['currency'],'czk_rate':old['rate']} if old else None
            rate, quote = resolve_form_fx(invoice['currency'],paid_date,rate,existing,old,old['requested_date'] if old else None)
        except (ValueError,TypeError) as exc:
            flash(str(exc) or 'Nieprawidłowa data zapłaty.','error')
        else:
            db = get_db()
            db.execute("UPDATE invoices SET status='paid',paid_date=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(paid_date,invoice_id))
            save_fx_quote('invoice',invoice_id,'payment',quote)
            db.commit()
            flash('Zapisano datę i kurs otrzymanej zapłaty. Kurs faktury / DPH pozostał bez zmian.','success')
            return redirect(url_for('invoice_view',invoice_id=invoice_id))
    return render_template('invoice_payment.html',invoice=invoice,totals=totals,paid_date=paid_date,rate=rate,is_edit=is_edit)



# V8.4: listy godzin; izolowany moduł, bez zmian w obliczeniach podatku.
import base64 as _ts_b64
import hashlib as _ts_hash
import json as _ts_json
import math as _ts_math
import random as _ts_random
import zipfile as _ts_zip
from reportlab.pdfgen import canvas as _ts_canvas
from reportlab.lib.utils import ImageReader as _ts_ImageReader
from PIL import Image as _ts_Image

TS_STYLE = 'loose5-v1'
TS_MONTHS_DE = ['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember']
TS_MAX_ROWS = 31
TS_MAX_IMAGE = 2 * 1024 * 1024


def ts_backup_before_migration() -> None:
    if not DB_PATH.exists() or not DB_PATH.stat().st_size:
        return
    db = sqlite3.connect(DB_PATH)
    try:
        done = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='timesheet_settings'").fetchone()
    finally:
        db.close()
    if not done:
        backup = create_database_backup('przed_aktualizacja_V8_4_listy_godzin')
        if REMOTE_DB_ENABLED and not upload_file_to_supabase(backup, 'backups/' + backup.name):
            raise RuntimeError('Brak zdalnej kopii przed migracją list godzin. Baza nie została zmieniona.')


def init_timesheets_module() -> None:
    db = get_db()
    db.executescript('''
    CREATE TABLE IF NOT EXISTS timesheet_settings (
        id INTEGER PRIMARY KEY CHECK(id=1),
        options_json TEXT NOT NULL,
        signature_png BLOB,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS timesheets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER UNIQUE REFERENCES invoices(id) ON DELETE SET NULL,
        current_revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS timesheet_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sheet_id INTEGER NOT NULL REFERENCES timesheets(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        pdf_blob BLOB NOT NULL,
        pdf_sha256 TEXT NOT NULL,
        total_minutes INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(sheet_id, revision)
    );
    ''')
    defaults = {
        'company_label': 'EQUANS', 'worker_name': get_company().get('name',''),
        'default_start': '06:30', 'default_end': '17:00', 'default_break': 30,
        'thursday_end': '13:00', 'thursday_break': 30, 'style': TS_STYLE,
        'show_project': False,
    }
    db.execute('INSERT OR IGNORE INTO timesheet_settings(id,options_json) VALUES(1,?)',
               (_ts_json.dumps(defaults,ensure_ascii=False),))
    db.commit()


def ts_settings() -> dict[str, Any]:
    row = get_db().execute('SELECT * FROM timesheet_settings WHERE id=1').fetchone()
    result = _ts_json.loads(row['options_json'])
    result['has_signature'] = bool(row['signature_png'])
    return result


def ts_current(sheet: dict[str, Any] | sqlite3.Row | None) -> dict[str, Any] | None:
    if not sheet or not sheet['current_revision']:
        return None
    row = get_db().execute('SELECT * FROM timesheet_versions WHERE sheet_id=? AND revision=?',
                           (sheet['id'],sheet['current_revision'])).fetchone()
    if row is None:
        return None
    d = dict(row); d['payload'] = _ts_json.loads(d['payload_json'])
    return d


def ts_text(value: Any, name: str, max_len: int = 80, required: bool = True) -> str:
    value = str(value or '').strip()
    if required and not value:
        raise ValueError('Uzupełnij: ' + name + '.')
    if len(value) > max_len or any(ord(ch)<32 for ch in value):
        raise ValueError(f'{name}: za długi tekst lub niedozwolone znaki.')
    return value


def ts_minutes(value: Any) -> int:
    if not re.fullmatch(r'\d{2}:\d{2}',str(value)):
        raise ValueError('Godziny wpisuj jako GG:MM, np. 06:30.')
    h,m = map(int,str(value).split(':'))
    if h>23 or m>59:
        raise ValueError('Nieprawidłowa godzina.')
    return h*60+m


def ts_integer(value: Any, label: str, low: int = 0, high: int = 1440) -> int:
    if not re.fullmatch(r'\d{1,4}',str('' if value is None else value).strip()):
        raise ValueError(label + ': wpisz całkowitą liczbę minut.')
    n = int(value)
    if not low <= n <= high:
        raise ValueError(label + ': wartość spoza dozwolonego zakresu.')
    return n


def ts_validate_rows(rows: list[dict[str, Any]], start: str, end: str) -> tuple[list[dict[str, Any]], int]:
    try:
        lo,hi = date.fromisoformat(start),date.fromisoformat(end)
    except (TypeError,ValueError):
        raise ValueError('Uzupełnij poprawną datę początku i końca okresu.') from None
    if hi < lo or (hi-lo).days > 30:
        raise ValueError('Jedna lista może obejmować od 1 do 31 kolejnych dni.')
    if not 1 <= len(rows) <= TS_MAX_ROWS:
        raise ValueError('Wpisz od 1 do 31 dni pracy.')
    seen = set(); result = []; total = 0
    for index,row in enumerate(rows,1):
        try:
            d = date.fromisoformat(str(row.get('date','')))
        except ValueError:
            raise ValueError(f'Wiersz {index}: nieprawidłowa data.') from None
        if d in seen:
            raise ValueError('Powtórzona data: ' + d.strftime('%d.%m.%Y'))
        if not lo<=d<=hi:
            raise ValueError('Data poza wybranym okresem: ' + d.strftime('%d.%m.%Y'))
        seen.add(d)
        begin,endm = ts_minutes(row.get('start')), ts_minutes(row.get('end'))
        next_day = row.get('next_day') in (True,1,'1','on')
        duration = endm - begin + (1440 if next_day else 0)
        if duration <= 0 or duration > 1440:
            raise ValueError(f'Wiersz {index}: koniec musi być po początku; dla nocy zaznacz „następny dzień”.')
        pause = ts_integer(row.get('break_minutes',0),f'Wiersz {index} – przerwa',0,1439)
        if pause >= duration:
            raise ValueError(f'Wiersz {index}: przerwa musi być krótsza od całej zmiany.')
        minutes = duration-pause
        total+=minutes
        result.append({'date':d.isoformat(),'start':f'{begin//60:02d}:{begin%60:02d}',
                       'end':f'{endm//60:02d}:{endm%60:02d}', 'break_minutes':pause,
                       'next_day':next_day,'minutes':minutes})
    result.sort(key=lambda r:r['date'])
    return result,total


def ts_hours(minutes: int) -> str:
    # Rachunki w minutach, bez utraty dokładności. Przy innych minutach niż
    # wielokrotność kwadransa drukuj h/min zamiast zaokrąglać rozliczany czas.
    if minutes % 15 == 0:
        d = Decimal(minutes)/60
        return format(d,'f').rstrip('0').rstrip('.') + ' h' if '.' in format(d,'f') else str(d)+' h'
    return f'{minutes//60} h {minutes%60:02d} min'


def ts_invoice_fingerprint(invoice: dict[str,Any], items: list[dict[str,Any]]) -> str:
    data = {'contractor_id':invoice.get('contractor_id'),'order_number':invoice.get('order_number'),
            'invoice_number':invoice.get('invoice_number'), 'currency':invoice.get('currency'),
            'items':[{k:str(r.get(k,'')) for k in ('description','quantity','unit','unit_price','vat_rate')} for r in items]}
    return _ts_hash.sha256(_ts_json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def ts_hours_warning(invoice: dict[str,Any], items: list[dict[str,Any]], minutes: int) -> str:
    units = {'h','hr','hrs','hour','hours','hod','hod.','hodina','hodiny','godz','godz.','std','std.','stunde','stunden'}
    hours = [parse_decimal(str(r['quantity'])) for r in items if str(r.get('unit','')).lower().strip() in units]
    if not hours:
        return 'Faktura nie ma pozycji w godzinach. Sprawdź ręcznie, czy lista dotyczy tego samego zakresu prac.'
    expected = sum(hours,Decimal('0'))
    actual = Decimal(minutes)/Decimal(60)
    if abs(expected-actual) > Decimal('0.000001'):
        return f'Różnica: faktura ma {format(expected,"f")} h, lista {ts_hours(minutes)}. Faktura nie zostanie zmieniona automatycznie.'
    return ''


def ts_signature_clean(data: bytes) -> bytes:
    if not data or len(data)>TS_MAX_IMAGE:
        raise ValueError('Podpis: prześlij PNG/JPG do 2 MB.')
    try:
        with _ts_Image.open(BytesIO(data)) as source:
            if source.width*source.height > 4_000_000:
                raise ValueError('Podpis: obraz może mieć najwyżej 4 mln pikseli.')
            if source.format not in {'PNG','JPEG','WEBP'}:
                raise ValueError('Podpis musi być plikiem PNG, JPG albo WEBP.')
            image = source.convert('RGBA')
            image.thumbnail((1000,360))
            out = BytesIO(); image.save(out,format='PNG',optimize=True)
            return out.getvalue()
    except (OSError,_ts_Image.DecompressionBombError):
        raise ValueError('Nie można odczytać obrazu podpisu.') from None


# Autorskie monoliniowe znaki wektorowe; bez dołączania plików czcionek.
# Punkty (x,y) względem linii bazowej, części krzywe wygładzane przez Catmull-Rom.
TS_GLYPHS = {
'0': (0.62, [[(.44,1),(.18,.95),(.06,.55),(.08,.12),(.30,0),(.50,.20),(.58,.72),(.44,1)]]),
'1': (0.44, [[(.02,.73),(.29,.98),(.25,.48),(.18,.0)]]),
'2': (0.62, [[(.05,.77),(.20,1),(.46,.95),(.52,.71),(.30,.38),(.04,.03),(.27,.05),(.55,.03)]]),
'3': (0.59, [[(.06,.86),(.30,.98),(.52,.81),(.39,.59),(.22,.52),(.46,.41),(.49,.13),(.28,-.01),(.03,.15)]]),
'4': (0.63, [[(.43,1),(.07,.38),(.57,.34)],[(.49,.90),(.41,.02)]]),
'5': (0.63, [[(.58,.96),(.16,.97),(.10,.54),(.37,.58),(.55,.38),(.43,.08),(.16,.02),(.03,.19)]]),
'6': (0.62, [[(.52,.96),(.27,.88),(.07,.51),(.09,.13),(.29,.02),(.49,.15),(.52,.40),(.30,.53),(.07,.35)]]),
'7': (0.63, [[(.03,.93),(.55,.95),(.31,.54),(.16,.02)],[(.16,.48),(.50,.50)]]),
'8': (0.60, [[(.34,.56),(.09,.80),(.26,.98),(.48,.84),(.31,.55),(.07,.27),(.18,.02),(.45,.07),(.49,.34),(.34,.56)]]),
'9': (0.61, [[(.51,.63),(.33,.97),(.11,.85),(.08,.59),(.31,.50),(.52,.68),(.48,.33),(.31,-.04)]]),
'.': (.20,[[(.09,.02),(.11,.03)]]), ',':(.21,[[(.14,.06),(.10,-.11)]]),
':':(.23,[[(.10,.65),(.12,.66)],[(.07,.14),(.09,.15)]]),
'/':(.46,[[(.02,-.02),(.48,1.02)]]), '-':(.42,[[(.05,.39),(.36,.42)]]),
'(': (.30,[[(.27,1.05),(.10,.71),(.08,.25),(.20,-.10)]]),
')': (.30,[[(.09,1.05),(.22,.72),(.18,.23),(.02,-.10)]]),
'+':(.62,[[(.02,.42),(.54,.43)],[(.29,.72),(.26,.13)]]),
'a':(.59,[[(.43,.61),(.19,.68),(.04,.42),(.05,.13),(.20,.03),(.41,.28),(.47,.63),(.43,.13),(.51,.03),(.59,.15)]]),
'b':(.59,[[(.06,.02),(.16,.73),(.28,1.03),(.26,.77),(.09,.24),(.23,.54),(.44,.62),(.53,.43),(.44,.16),(.21,.02),(.06,.07)]]),
'c':(.51,[[(.46,.57),(.27,.67),(.07,.42),(.04,.18),(.17,.03),(.46,.15)]]),
'd':(.64,[[(.46,.59),(.23,.66),(.07,.45),(.06,.17),(.22,.04),(.40,.22),(.54,.86),(.59,1.04),(.47,.48),(.43,.13),(.54,.02),(.63,.14)]]),
'e':(.54,[[(.06,.32),(.37,.41),(.44,.59),(.25,.66),(.06,.43),(.06,.17),(.25,.04),(.53,.18)]]),
'f':(.45,[[(.38,.97),(.21,1.05),(.09,.68),(.05,.11),(.0,-.28)],[(.01,.57),(.45,.58)]]),
'g':(.60,[[(.44,.56),(.24,.66),(.07,.43),(.07,.16),(.24,.06),(.43,.28),(.49,.60),(.44,.12),(.34,-.35),(.14,-.43),(.08,-.28),(.31,-.10),(.57,.03)]]),
'h':(.64,[[(.04,.02),(.16,.70),(.29,1.05),(.29,.84),(.11,.26),(.31,.58),(.47,.61),(.50,.44),(.44,.11),(.53,.01),(.64,.12)]]),
'i':(.31,[[(.14,.60),(.06,.12),(.15,.02),(.29,.14)],[(.17,.87),(.18,.89)]]),
'j':(.33,[[(.22,.59),(.13,-.25),(.01,-.42),(-.13,-.30)],[(.26,.87),(.27,.89)]]),
'k':(.59,[[(.05,.02),(.22,1.06)],[(.51,.65),(.12,.30),(.32,.39),(.39,.11),(.52,.02),(.60,.13)]]),
'l':(.35,[[(.10,.19),(.29,.77),(.29,1.05),(.15,.84),(.06,.31),(.11,.05),(.22,.01),(.34,.14)]]),
'm':(.86,[[(.06,.59),(.02,.06),(.21,.49),(.34,.59),(.37,.43),(.29,.04),(.48,.49),(.63,.59),(.68,.43),(.63,.12),(.71,.03),(.85,.13)]]),
'n':(.62,[[(.10,.60),(.02,.05),(.21,.46),(.39,.61),(.48,.48),(.43,.11),(.50,.02),(.62,.13)]]),
'o':(.57,[[(.36,.65),(.12,.58),(.03,.28),(.13,.04),(.33,.07),(.47,.31),(.45,.57),(.36,.65)]]),
'p':(.61,[[(.13,.65),(.06,.04),(-.01,-.38)],[(.10,.26),(.29,.59),(.47,.56),(.51,.35),(.33,.06),(.12,.11)]]),
'q':(.60,[[(.45,.61),(.21,.64),(.04,.35),(.13,.09),(.32,.10),(.46,.44),(.38,-.33),(.47,-.20),(.58,-.10)]]),
'r':(.47,[[(.11,.60),(.03,.05),(.21,.47),(.32,.65),(.37,.51),(.48,.49)]]),
's':(.48,[[(.44,.57),(.23,.66),(.10,.48),(.25,.31),(.37,.13),(.20,.02),(.02,.15)]]),
't':(.41,[[(.23,.90),(.12,.35),(.10,.10),(.21,.02),(.40,.14)],[(.02,.56),(.44,.58)]]),
'u':(.63,[[(.13,.62),(.05,.21),(.12,.05),(.31,.17),(.51,.59),(.44,.14),(.51,.03),(.63,.14)]]),
'v':(.55,[[(.06,.62),(.17,.02),(.51,.61)]]),
'w':(.80,[[(.06,.62),(.11,.07),(.26,.10),(.43,.55),(.42,.08),(.56,.09),(.78,.65)]]),
'x':(.56,[[(.09,.64),(.44,.04)],[(.49,.61),(.04,.03)]]),
'y':(.62,[[(.12,.60),(.06,.20),(.18,.04),(.37,.25),(.52,.61),(.44,.05),(.27,-.39),(.08,-.42),(.11,-.25),(.57,.07)]]),
'z':(.56,[[(.08,.57),(.50,.61),(.04,.04),(.50,.07)]]),
'A':(.73,[[(.02,.0),(.45,1.02),(.66,.02)],[(.18,.35),(.60,.38)]]),
'B':(.69,[[(.03,.02),(.16,.99)],[(.16,.97),(.47,.98),(.59,.78),(.36,.55),(.12,.52),(.46,.49),(.58,.24),(.39,.03),(.03,.02)]]),
'C':(.73,[[(.68,.84),(.45,1.02),(.15,.84),(.04,.43),(.17,.06),(.48,.02),(.66,.21)]]),
'D':(.72,[[(.02,.01),(.17,.99)],[(.16,.99),(.47,.91),(.66,.65),(.57,.23),(.29,.02),(.02,.01)]]),
'E':(.66,[[(.63,.97),(.16,.99),(.05,.04),(.59,.03)],[(.11,.53),(.50,.56)]]),
'F':(.63,[[(.07,0),(.17,.99),(.62,.98)],[(.13,.55),(.49,.56)]]),
'G':(.76,[[(.70,.86),(.45,1),(.14,.80),(.04,.43),(.15,.12),(.43,.02),(.66,.23),(.68,.52),(.39,.51)]]),
'H':(.76,[[(.12,.99),(.03,.01)],[(.70,1),(.59,.02)],[(.08,.49),(.64,.54)]]),
'I':(.39,[[(.23,.99),(.13,.02)],[(.06,.98),(.38,1)],[(0,.02),(.31,.02)]]),
'J':(.63,[[(.58,.97),(.46,.21),(.27,.01),(.07,.06),(.02,.24)],[(.28,.96),(.70,.99)]]),
'K':(.72,[[(.16,1),(.06,.01)],[(.69,.98),(.12,.41),(.33,.54),(.66,.02)]]),
'L':(.60,[[(.16,1),(.05,.03),(.57,.02)]]),
'M':(.93,[[(.02,.02),(.16,.97),(.39,.30),(.79,1.02),(.83,.03)]]),
'N':(.76,[[(.04,.02),(.16,1),(.64,.02),(.73,1.02)]]),
'O':(.77,[[(.51,1),(.21,.90),(.04,.49),(.12,.11),(.39,.02),(.63,.25),(.71,.69),(.51,1)]]),
'P':(.68,[[(.04,.0),(.17,1)],[(.17,.99),(.47,.98),(.60,.74),(.42,.54),(.11,.52)]]),
'Q':(.78,[[(.52,1),(.21,.90),(.04,.49),(.12,.11),(.39,.02),(.64,.25),(.71,.69),(.52,1)],[(.40,.22),(.70,-.13)]]),
'R':(.71,[[(.04,.0),(.17,1)],[(.17,.99),(.47,.98),(.60,.74),(.42,.54),(.11,.52)],[(.35,.53),(.63,.02)]]),
'S':(.70,[[(.64,.84),(.44,.99),(.16,.82),(.18,.61),(.50,.43),(.59,.21),(.36,.01),(.06,.15)]]),
'T':(.72,[[(.05,.96),(.74,1)],[(.43,.99),(.30,.01)]]),
'U':(.78,[[(.13,.99),(.06,.35),(.16,.06),(.39,.03),(.62,.30),(.74,1)]]),
'V':(.72,[[(.07,1),(.28,.02),(.72,1.03)]]),
'W':(1.02,[[(.04,1),(.16,.02),(.52,.87),(.63,.02),(1.,1.02)]]),
'X':(.71,[[(.07,.99),(.61,.02)],[(.71,.98),(.02,.02)]]),
'Y':(.73,[[(.07,1),(.33,.51),(.71,1)],[(.33,.51),(.25,.01)]]),
'Z':(.68,[[(.07,.99),(.68,1),(.03,.02),(.63,.03)]]),
'&':(.72,[[(.59,.78),(.43,1),(.19,.85),(.23,.60),(.63,.02)],[(.27,.54),(.08,.30),(.23,.03),(.47,.11),(.68,.50)]]),
}


def ts_glyph(ch: str) -> tuple[float,list]:
    if ch==' ': return .37,[]
    if ch in TS_GLYPHS: return TS_GLYPHS[ch]
    if ch in 'łŁ':
        width,strokes=TS_GLYPHS['l' if ch=='ł' else 'L']
        return width,strokes+[[(.02,.45),(.33,.63)]]
    parts=unicodedata.normalize('NFD',ch)
    if parts and parts[0] in TS_GLYPHS:
        width,strokes=TS_GLYPHS[parts[0]]; strokes=list(strokes)
        top=1.13 if parts[0].isupper() else .84
        for accent in parts[1:]:
            if accent=='\u0301': strokes.append([(.20,top),(.37,top+.19)])
            elif accent=='\u0308': strokes.extend([[ (.15,top),(.16,top+.02)],[(.37,top),(.38,top+.02)]])
            elif accent=='\u030c': strokes.append([(.11,top+.13),(.25,top),(.43,top+.16)])
            elif accent=='\u0307': strokes.append([(.25,top),(.27,top+.03)])
            elif accent=='\u0328': strokes.append([(.38,.04),(.26,-.16),(.40,-.17)])
            elif accent=='\u030a': strokes.append([(.23,top),(.16,top+.13),(.28,top+.20),(.38,top+.11),(.23,top)])
        return width,strokes
    if ch in '–—': return TS_GLYPHS['-']
    return .58, [[(.1,0),(.1,.9),(.5,.9),(.5,0),(.1,0)]]


def ts_handwrite(c, text: str, x: float, y: float, size: float, max_width: float,
                 seed: str, align: str = 'left') -> None:
    glyphs=[ts_glyph(ch) for ch in text]
    rng=_ts_random.Random(_ts_hash.sha256(seed.encode()).digest())
    widths=[(g[0]+.10)*rng.uniform(.94,1.07) for g in glyphs]
    total=sum(widths)*size
    if total>max_width:
        size*=max_width/total; total=max_width
    if align=='center': x-=total/2
    c.saveState(); c.setStrokeColorRGB(.08,.20,.74); c.setLineCap(1); c.setLineJoin(1)
    cursor=x
    for (width,strokes),advance in zip(glyphs,widths):
        scale=size*rng.uniform(.96,1.04); slant=rng.uniform(.06,.16)
        yj=y+rng.uniform(-.32,.32); angle=rng.uniform(-1.4,1.4)
        c.saveState(); c.translate(cursor,yj); c.rotate(angle)
        c.setLineWidth(max(.40,scale*.038)*rng.uniform(.92,1.08))
        for stroke in strokes:
            # Small per-character transformation, not destructive per-point noise.
            pts=[((px+slant*py)*scale,py*scale) for px,py in stroke]
            if not pts: continue
            p=c.beginPath(); p.moveTo(*pts[0])
            if len(pts)<4:
                for pt in pts[1:]: p.lineTo(*pt)
            else:
                for i in range(len(pts)-1):
                    a=pts[max(i-1,0)]; b=pts[i]; d=pts[i+1]; e=pts[min(i+2,len(pts)-1)]
                    p.curveTo(b[0]+(d[0]-a[0])/6,b[1]+(d[1]-a[1])/6,
                              d[0]-(e[0]-b[0])/6,d[1]-(e[1]-b[1])/6,*d)
            c.drawPath(p,stroke=1,fill=0)
        c.restoreState(); cursor+=advance*size
    c.restoreState()


def ts_build_pdf(payload: dict[str,Any], signature: bytes | None = None) -> bytes:
    rows,total=ts_validate_rows(payload['rows'],payload['period_start'],payload['period_end'])
    buf=BytesIO(); c=_ts_canvas.Canvas(buf,pagesize=A4,pageCompression=1,invariant=1)
    w,h=A4; k=w/1024; py=lambda v:h-v*k
    c.setTitle('Stundenzettel – '+payload['invoice_number'])
    c.setAuthor(payload['worker_name'])
    c.setSubject('Elektroniczna lista godzin, stylizowane pismo loose5; daty i czas zatwierdzone przez użytkownika.')
    c.setFont('Helvetica',10)
    c.drawString(420*k,py(77),'Firma:'); c.line(471*k,py(81),602*k,py(81))
    seed=payload.get('seed','sample')
    def hw(text,x,y,sz,maxw,field,align='left'):
        ts_handwrite(c,text,x*k,py(y),sz*k,maxw*k,seed+':'+field,align)
    hw(payload['company_label'],475,76,19,123,'company')
    c.setFont('Helvetica',20); c.drawString(298*k,py(180),'Name:')
    c.line(395*k,py(186),725*k,py(186))
    hw(payload['worker_name'],400,179,23,319,'worker')
    c.setLineWidth(.65); c.rect(49*k,py(220),925*k,25*k)
    c.line(511*k,py(195),511*k,py(220))
    c.setFont('Helvetica',9)
    c.drawRightString(351*k,py(212),'Stundenzettel Monat:')
    lo,hi=date.fromisoformat(payload['period_start']),date.fromisoformat(payload['period_end'])
    months=[]; cursor=date(lo.year,lo.month,1)
    while cursor<=hi:
        months.append(TS_MONTHS_DE[cursor.month-1])
        cursor=date(cursor.year+(cursor.month==12),1 if cursor.month==12 else cursor.month+1,1)
    hw(' / '.join(months),366,213,20,138,'month')
    c.drawString(612*k,py(212),'von:'); c.drawString(744*k,py(212),'bis:')
    hw(lo.strftime('%d.%m.%Y'),652,212,16,90,'startdate')
    hw(hi.strftime('%d.%m.%Y'),806,212,16,148,'enddate')
    if payload.get('show_project') and payload.get('project_number'):
        c.setFont('Helvetica',8);c.drawString(49*k,py(238),'Projekt:')
        hw(payload['project_number'],102,238,14,395,'project')
    xs=[49,229,409,589,769,974]; top=244; rh=(1272-top)/32
    # 1 header + 31 data rows; original blank layout preserved.
    for x in xs: c.line(x*k,py(top),x*k,py(1272))
    for i in range(33):
        y=top+i*rh; c.line(49*k,py(y),974*k,py(y))
    labels=['Datum','Beginn','Ende','Pause','Arbeitszeit (abzgl. Pause)']
    c.setFont('Helvetica',8.8)
    for i,label in enumerate(labels): c.drawCentredString((xs[i]+xs[i+1])/2*k,py(265),label)
    for i,row in enumerate(rows):
        y=top+rh*(i+1)+rh*.68
        vals=[date.fromisoformat(row['date']).strftime('%d.%m.'),str(int(row['start'][:2]))+row['start'][2:],
              str(int(row['end'][:2]))+row['end'][2:]+(' (+1)' if row['next_day'] else ''),
              ts_hours(row['break_minutes']),ts_hours(row['minutes'])]
        for j,v in enumerate(vals):
            hw(v,(xs[j]+xs[j+1])/2,y,19 if j!=2 or not row['next_day'] else 16,xs[j+1]-xs[j]-22,f'{row["date"]}:{j}','center')
    c.setFont('Helvetica',8.5)
    c.drawString(54*k,py(1294),'Vorlage von Arbeitszeiterfassung.com')
    c.drawString(650*k,py(1294),'Summe:')
    c.rect(769*k,py(1305),205*k,33*k)
    hw(ts_hours(total),872,1296,21,187,'total','center')
    if signature:
        # Exact uploaded raster, never a generated or re-drawn signature.
        im=_ts_Image.open(BytesIO(signature)); sw,sh=im.size
        dw=min(266*k,108*k*sw/sh); dh=dw*sh/sw
        c.drawImage(_ts_ImageReader(im),379*k,py(1404),dw,dh,mask='auto')
    c.setFont('Helvetica',8); c.drawCentredString(w/2,py(1405),'Seite 1/1')
    c.showPage();c.save()
    return buf.getvalue()


def ts_csrf_token() -> str:
    if '_ts_csrf' not in session:
        session['_ts_csrf']=secrets.token_urlsafe(32)
    return session['_ts_csrf']


def ts_verify_csrf() -> None:
    expected=session.get('_ts_csrf',''); supplied=request.form.get('_ts_csrf','')
    if not expected or not hmac.compare_digest(expected,supplied):
        abort(400,description='Sesja formularza wygasła. Odśwież stronę i spróbuj ponownie.')


@app.context_processor
def ts_inject_globals():
    return {'ts_csrf_token':ts_csrf_token,'ts_hours':ts_hours,'ts_settings':ts_settings}


def ts_form_payload(invoice:dict,items:list,current:dict|None) -> tuple[dict,bytes|None]:
    raw=request.form.get('rows_json','[]')
    if len(raw)>30000: raise ValueError('Lista jest zbyt duża.')
    try: rows=_ts_json.loads(raw)
    except (ValueError,TypeError): raise ValueError('Nieprawidłowy zapis wierszy.') from None
    if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows): raise ValueError('Nieprawidłowe wiersze listy.')
    rows,total=ts_validate_rows(rows,request.form.get('period_start',''),request.form.get('period_end',''))
    s=ts_settings()
    signature=None;mode=request.form.get('signature_mode','none')
    if mode=='keep' and current:
        signature=_ts_b64.b64decode(current['payload'].get('signature_base64','')) or None
    elif mode=='current':
        if request.form.get('signature_consent')!='1':
            raise ValueError('Potwierdź, że chcesz dodać swój zapisany podpis do tej listy.')
        row=get_db().execute('SELECT signature_png FROM timesheet_settings WHERE id=1').fetchone()
        signature=bytes(row['signature_png']) if row['signature_png'] else None
        if not signature: raise ValueError('Najpierw wgraj swój podpis w Ustawienia → Listy godzin.')
    elif mode!='none': raise ValueError('Nieprawidłowa opcja podpisu.')
    payload={'schema':1,'style':TS_STYLE,'company_label':ts_text(request.form.get('company_label'),'Firma',60),
             'worker_name':ts_text(request.form.get('worker_name'),'Imię i nazwisko',80),
             'period_start':request.form.get('period_start'),'period_end':request.form.get('period_end'),
             'project_number':ts_text(request.form.get('project_number'),'Projekt',80,False),
             'show_project':request.form.get('show_project')=='1','rows':rows,'total_minutes':total,
             'invoice_number':invoice['invoice_number'],'contractor_id':invoice['contractor_id'],
             'invoice_fingerprint':ts_invoice_fingerprint(invoice,items),
             'seed':current['payload'].get('seed') if current else secrets.token_hex(12),
             'signature_base64':_ts_b64.b64encode(signature).decode() if signature else ''}
    return payload,signature


def ts_save_version(invoice_id:int,payload:dict,pdf:bytes,expected_revision:int) -> int:
    db=get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        sheet=db.execute('SELECT * FROM timesheets WHERE invoice_id=?',(invoice_id,)).fetchone()
        actual=sheet['current_revision'] if sheet else 0
        if actual!=expected_revision:
            raise ValueError('Lista została zmieniona w innym oknie. Odśwież stronę przed ponownym zapisem.')
        if not sheet:
            sheet_id=db.execute('INSERT INTO timesheets(invoice_id) VALUES(?)',(invoice_id,)).lastrowid
        else: sheet_id=sheet['id']
        rev=actual+1
        db.execute('''INSERT INTO timesheet_versions(sheet_id,revision,payload_json,pdf_blob,pdf_sha256,total_minutes)
                      VALUES(?,?,?,?,?,?)''',
                   (sheet_id,rev,_ts_json.dumps(payload,ensure_ascii=False,sort_keys=True),pdf,
                    _ts_hash.sha256(pdf).hexdigest(),payload['total_minutes']))
        db.execute('UPDATE timesheets SET current_revision=? WHERE id=?',(rev,sheet_id));db.commit()
        return rev
    except Exception:
        db.rollback();raise


def ts_filename(payload:dict,revision:int) -> str:
    number=re.sub(r'[^A-Za-z0-9._-]+','_',payload['invoice_number'])
    return f'Stundenzettel_{number}_{payload["period_start"]}_{payload["period_end"]}_v{revision}.pdf'


def ts_cached_file(invoice:dict,version:dict) -> None:
    # Optional local archive. Authoritative, frozen PDF lives in SQLite and backup.
    dest=invoice_pdf_destination(invoice).parent/'Listy_godzin'/ts_filename(version['payload'],version['revision'])
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp=dest.with_suffix('.tmp');temp.write_bytes(version['pdf_blob']);os.replace(temp,dest)


@app.route('/invoices/<int:invoice_id>/timesheet',methods=['GET','POST'])
def timesheet_edit(invoice_id:int):
    invoice,items,totals=get_invoice_bundle(invoice_id)
    sheet=get_db().execute('SELECT * FROM timesheets WHERE invoice_id=?',(invoice_id,)).fetchone()
    current=ts_current(sheet); settings=ts_settings();errors=[]
    if request.method=='POST':
        ts_verify_csrf()
        try:
            payload,signature=ts_form_payload(invoice,items,current)
            if request.form.get('intent')=='save' and request.form.get('reviewed')!='1':
                raise ValueError('Potwierdź sprawdzenie rzeczywistych dat, godzin i przerw.')
            pdf=ts_build_pdf(payload,signature)
            if request.form.get('intent')=='preview':
                return send_file(BytesIO(pdf),mimetype='application/pdf',download_name='Podglad_listy_godzin.pdf',as_attachment=False)
            if request.form.get('intent')!='save': raise ValueError('Nieznana operacja.')
            expected=ts_integer(request.form.get('revision','0'),'Wersja formularza',0,9999)
            revision=ts_save_version(invoice_id,payload,pdf,expected)
            version=ts_current(get_db().execute('SELECT * FROM timesheets WHERE invoice_id=?',(invoice_id,)).fetchone())
            try: ts_cached_file(invoice,version)
            except OSError: flash('Lista jest zapisana w bazie. Lokalny zapis dodatkowego PDF nie powiódł się.','error')
            warning=ts_hours_warning(invoice,items,payload['total_minutes'])
            flash(f'Zapisano listę: {ts_hours(payload["total_minutes"])}. Wersja {revision}. Faktura pozostaje bez zmian.','success')
            if warning: flash(warning,'error')
            return redirect(url_for('timesheet_edit',invoice_id=invoice_id))
        except ValueError as exc: errors.append(str(exc))
    if current:
        form=dict(current['payload']); form['revision']=current['revision']
        form['signature_mode']='keep' if form.get('signature_base64') else 'none'
    else:
        d=date.fromisoformat(invoice['supply_date']);monday=d-timedelta(days=d.weekday())
        form={'company_label':settings['company_label'] if 'equans' in invoice.get('contractor_name','').casefold() else invoice.get('contractor_name',settings['company_label']),'worker_name':settings['worker_name'],
              'period_start':monday.isoformat(),'period_end':(monday+timedelta(days=3)).isoformat(),
              'project_number':invoice.get('order_number',''),'show_project':settings.get('show_project',False),
              'rows':[],'revision':0,'signature_mode':'none'}
        # Duplicating last list does not copy its signature/approvals; next form stays draft.
        if request.args.get('copy')=='previous':
            prior=get_db().execute('''SELECT v.payload_json FROM timesheets s
                 JOIN timesheet_versions v ON v.sheet_id=s.id AND v.revision=s.current_revision
                 JOIN invoices i ON i.id=s.invoice_id WHERE i.contractor_id=? AND i.id!=?
                 ORDER BY v.id DESC LIMIT 1''',(invoice['contractor_id'],invoice_id)).fetchone()
            if prior:
                prev=_ts_json.loads(prior['payload_json']); offset=date.fromisoformat(form['period_start'])-date.fromisoformat(prev['period_start'])
                form['period_end']=(date.fromisoformat(prev['period_end'])+offset).isoformat()
                form['rows']=[dict(r,date=(date.fromisoformat(r['date'])+offset).isoformat()) for r in prev['rows']]
                flash('Skopiowano dni i godziny na nowy okres. Sprawdź je; podpisu nie przeniesiono.','success')
            else: flash('Brak wcześniejszej listy tego kontrahenta.','error')
    if request.method=='POST':
        for k in ('company_label','worker_name','period_start','period_end','project_number','signature_mode','revision'):
            form[k]=request.form.get(k,form.get(k,''))
        form['show_project']=request.form.get('show_project')=='1'
        try:
            parsed=_ts_json.loads(request.form.get('rows_json','[]'))
            if isinstance(parsed,list) and len(parsed)<=31 and all(isinstance(r,dict) for r in parsed): form['rows']=parsed
        except ValueError: pass
    history=get_db().execute('SELECT id,revision,created_at,total_minutes FROM timesheet_versions WHERE sheet_id=? ORDER BY revision DESC',(sheet['id'],)).fetchall() if sheet else []
    warning=ts_hours_warning(invoice,items,current['total_minutes']) if current else ''
    changed=bool(current and current['payload'].get('invoice_fingerprint')!=ts_invoice_fingerprint(invoice,items))
    return render_template('timesheet_form.html',invoice=invoice,form=form,ts_options=settings,
                           current=current,history=history,errors=errors,hours_warning=warning,invoice_changed=changed)


@app.route('/invoices/<int:invoice_id>/timesheet/pdf')
def timesheet_pdf(invoice_id:int):
    get_invoice_bundle(invoice_id)
    sheet=get_db().execute('SELECT * FROM timesheets WHERE invoice_id=?',(invoice_id,)).fetchone()
    current=ts_current(sheet)
    if not current: abort(404)
    return send_file(BytesIO(current['pdf_blob']),mimetype='application/pdf',
                     download_name=ts_filename(current['payload'],current['revision']),
                     as_attachment=request.args.get('inline')!='1')


@app.route('/timesheets/revisions/<int:version_id>/pdf')
def timesheet_revision_pdf(version_id:int):
    row=get_db().execute('SELECT * FROM timesheet_versions WHERE id=?',(version_id,)).fetchone()
    if not row: abort(404)
    payload=_ts_json.loads(row['payload_json'])
    return send_file(BytesIO(row['pdf_blob']),mimetype='application/pdf',
                     download_name=ts_filename(payload,row['revision']),as_attachment=True)


@app.route('/invoices/<int:invoice_id>/bundle')
def invoice_timesheet_bundle(invoice_id:int):
    invoice,items,totals=get_invoice_bundle(invoice_id)
    current=ts_current(get_db().execute('SELECT * FROM timesheets WHERE invoice_id=?',(invoice_id,)).fetchone())
    if not current: abort(404)
    if current['payload']['invoice_fingerprint']!=ts_invoice_fingerprint(invoice,items) and request.args.get('confirm')!='1':
        flash('Faktura zmieniła się od zapisu listy. Sprawdź listę i zapisz nową wersję przed pobraniem paczki.','error')
        return redirect(url_for('timesheet_edit',invoice_id=invoice_id))
    contractor={k:invoice.get('contractor_'+k,'') for k in ('name','address','postal_code','city','country','company_id','vat_id','email','phone')}
    inv_pdf=build_invoice_pdf(get_company(),invoice,contractor,items).getvalue()
    buf=BytesIO();n=re.sub(r'[^A-Za-z0-9._-]+','_',invoice['invoice_number'])
    with _ts_zip.ZipFile(buf,'w',_ts_zip.ZIP_DEFLATED) as z:
        z.writestr('Faktura_'+n+'.pdf',inv_pdf)
        z.writestr(ts_filename(current['payload'],current['revision']),current['pdf_blob'])
    buf.seek(0)
    return send_file(buf,mimetype='application/zip',download_name='Faktura_i_godziny_'+n+'.zip',as_attachment=True)


@app.route('/settings/timesheets',methods=['GET','POST'])
def timesheet_settings_page():
    errors=[];s=ts_settings();db=get_db()
    if request.method=='POST':
        ts_verify_csrf()
        try:
            action=request.form.get('action','save')
            signature=None; replace_signature=False
            if action=='import':
                uploaded=request.files.get('config')
                if uploaded is None: raise ValueError('Wybierz prywatny plik ustawień .json.')
                raw=uploaded.read(2*1024*1024+1)
                if len(raw)>2*1024*1024: raise ValueError('Plik ustawień jest za duży.')
                try: data=_ts_json.loads(raw.decode('utf-8'))
                except Exception: raise ValueError('Nieprawidłowy plik JSON.') from None
                if not isinstance(data,dict) or data.get('format')!='FakturyOSVC-timesheets-1': raise ValueError('To nie jest plik ustawień list godzin.')
                incoming=data.get('settings',{})
                if not isinstance(incoming,dict): raise ValueError('Nieprawidłowe ustawienia.')
                if data.get('signature_base64'):
                    try: binary=_ts_b64.b64decode(data['signature_base64'],validate=True)
                    except Exception: raise ValueError('Nieprawidłowy zapis podpisu.') from None
                    if request.form.get('own_signature')!='1': raise ValueError('Potwierdź, że importowany podpis należy do Ciebie.')
                    signature=ts_signature_clean(binary);replace_signature=True
            else:
                incoming=dict(request.form)
                incoming['show_project']=request.form.get('show_project')=='1'
                if request.form.get('remove_signature')=='1': replace_signature=True
                image=request.files.get('signature')
                if image and image.filename:
                    if request.form.get('own_signature')!='1': raise ValueError('Potwierdź, że przesłany podpis należy do Ciebie.')
                    signature=ts_signature_clean(image.read(TS_MAX_IMAGE+1));replace_signature=True
            new={k:incoming.get(k,s[k]) for k in ('company_label','worker_name','default_start','default_end','default_break','thursday_end','thursday_break','show_project')}
            new['company_label']=ts_text(new['company_label'],'Firma',60)
            new['worker_name']=ts_text(new['worker_name'],'Imię i nazwisko',80)
            for k in ('default_start','default_end','thursday_end'): ts_minutes(new[k])
            new['default_break']=ts_integer(new['default_break'],'Domyślna przerwa',0,720)
            new['thursday_break']=ts_integer(new['thursday_break'],'Przerwa w czwartek',0,720)
            new['show_project']=bool(new['show_project']);new['style']=TS_STYLE
            ts_validate_rows([{'date':'2026-10-05','start':new['default_start'],'end':new['default_end'],'break_minutes':new['default_break']}],'2026-10-05','2026-10-05')
            ts_validate_rows([{'date':'2026-10-08','start':new['default_start'],'end':new['thursday_end'],'break_minutes':new['thursday_break']}],'2026-10-08','2026-10-08')
            with db:
                db.execute('UPDATE timesheet_settings SET options_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=1',(_ts_json.dumps(new,ensure_ascii=False),))
                if replace_signature: db.execute('UPDATE timesheet_settings SET signature_png=? WHERE id=1',(signature,))
            flash('Zapisano ustawienia list godzin. Istniejące PDF-y i podpisy w zapisanych wersjach nie zostały zmienione.','success')
            return redirect(url_for('timesheet_settings_page'))
        except ValueError as exc: errors.append(str(exc))
    archives=db.execute('''SELECT v.id,v.revision,v.created_at,v.total_minutes,v.payload_json,i.invoice_number,i.id AS invoice_id
        FROM timesheets s JOIN timesheet_versions v ON v.sheet_id=s.id AND v.revision=s.current_revision
        LEFT JOIN invoices i ON i.id=s.invoice_id ORDER BY v.id DESC LIMIT 100''').fetchall()
    return render_template('timesheet_settings.html',settings_tab='timesheets',ts_options=s,errors=errors,archives=archives)


@app.route('/settings/timesheets/signature.png')
def timesheet_signature_png():
    row=get_db().execute('SELECT signature_png FROM timesheet_settings WHERE id=1').fetchone()
    if not row or not row['signature_png']: abort(404)
    return send_file(BytesIO(row['signature_png']),mimetype='image/png')


@app.route('/settings/timesheets/sample.pdf')
def timesheet_sample_pdf():
    s=ts_settings(); rows=[]
    for i in range(4):
        rows.append({'date':f'2026-10-{5+i:02d}','start':s['default_start'],
                     'end':s['thursday_end'] if i==3 else s['default_end'],
                     'break_minutes':s['thursday_break'] if i==3 else s['default_break']})
    p={'invoice_number':'PRÓBKA-NIE-DO-WYSYŁKI','company_label':s['company_label'],'worker_name':s['worker_name'],
       'period_start':'2026-10-05','period_end':'2026-10-08','rows':rows,'show_project':False,'seed':'loose5-sample'}
    # Sample intentionally unsigned; doesn't create records.
    pdf=ts_build_pdf(p)
    return send_file(BytesIO(pdf),mimetype='application/pdf',as_attachment=False,download_name='Probka_styl_5.pdf')

@app.before_request
def ts_require_cloud_auth_configuration():
    if CLOUD_MODE and not APP_PASSWORD and (str(request.endpoint or '').startswith(('timesheet','ts_')) or request.endpoint=='invoice_timesheet_bundle'):
        abort(503,description='Ustaw APP_PASSWORD przed udostępnieniem list godzin i podpisu w chmurze.')


# V8.4 interface: same navigation, new settings sub-tab and per-invoice action.
EMBEDDED_TEMPLATES['timesheet_form.html'] = r'''{% extends 'base.html' %}
{% block title %}Lista godzin — {{ invoice.invoice_number }}{% endblock %}
{% block content %}
<div class="page-header"><div><h1>Lista godzin</h1><p class="muted">{{ invoice.invoice_number }} · {{ invoice.contractor_name }} · styl 5, niebieski długopis</p></div><div class="button-row"><a class="btn btn-light" href="{{ url_for('invoice_view',invoice_id=invoice.id) }}">Wróć do faktury</a><a class="btn btn-light" href="{{ url_for('timesheet_settings_page') }}">Ustawienia list</a></div></div>
{% for error in errors %}<div class="callout warning" role="alert">{{ error }}</div>{% endfor %}
{% if invoice_changed %}<div class="callout warning">Faktura została zmieniona od zapisu listy. Zapisany PDF pozostaje niezmienny; sprawdź zgodność i zapisz nową wersję.</div>{% endif %}
{% if hours_warning %}<div class="callout warning">{{ hours_warning }}</div>{% endif %}
{% if current %}<section class="panel ts-saved"><div><strong>Zapisana wersja {{ current.revision }} · {{ ts_hours(current.total_minutes) }}</strong><p class="muted">{{ current.created_at }}. Ten PDF zawiera wpisy i podpis z chwili zapisu.</p></div><div class="button-row"><a class="btn btn-primary" href="{{ url_for('timesheet_pdf',invoice_id=invoice.id) }}">Pobierz listę PDF</a><a class="btn btn-light" href="{{ url_for('invoice_timesheet_bundle',invoice_id=invoice.id) }}">Faktura + lista (ZIP)</a></div></section>{% endif %}
<form method="post" class="panel form-panel" id="ts-form" data-defaults='{{ ts_options|tojson }}'>
<input type="hidden" name="_ts_csrf" value="{{ ts_csrf_token() }}"><input type="hidden" name="revision" value="{{ form.revision }}">
<input type="hidden" name="rows_json" id="ts-rows-json" value='{{ form.rows|tojson }}'>
<h2>Dane listy</h2>
<div class="form-grid three">
<label class="field"><span>Firma na liście</span><input name="company_label" value="{{ form.company_label }}" required maxlength="60"></label>
<label class="field span-2"><span>Imię i nazwisko</span><input name="worker_name" value="{{ form.worker_name }}" required maxlength="80"></label>
<label class="field"><span>Od</span><input type="date" name="period_start" id="ts-from" value="{{ form.period_start }}" required></label>
<label class="field"><span>Do</span><input type="date" name="period_end" id="ts-to" value="{{ form.period_end }}" required></label>
<label class="field"><span>Projekt / zamówienie</span><input name="project_number" value="{{ form.project_number }}" maxlength="80"></label>
<label class="checkbox-row span-3"><input type="checkbox" name="show_project" value="1" {% if form.show_project %}checked{% endif %}>Drukuj projekt nad tabelą (opcjonalnie)</label>
</div>
<div class="ts-section-title"><div><h2>Dni pracy</h2><p class="muted">Przerwa w minutach. Praca netto = koniec − początek − przerwa.</p></div><div class="button-row"><button class="btn btn-light" id="ts-week" type="button">Wstaw pon.–czw.</button><button class="btn btn-light" id="ts-add" type="button">+ Dodaj dzień</button>{% if not current %}<a class="btn btn-light" href="{{ url_for('timesheet_edit',invoice_id=invoice.id,copy='previous') }}">Kopiuj poprzednią listę</a>{% endif %}</div></div>
<p class="ts-inline-warning" id="ts-row-error" role="alert"></p>
<div id="ts-rows" class="ts-rows"></div>
<div class="ts-total"><span>Razem po odjęciu przerw</span><strong id="ts-total-text">0 h</strong></div>
<h2>Podpis</h2>
<label class="field"><span>Podpis w tym dokumencie</span><select name="signature_mode" id="ts-signature-mode">
<option value="none" {% if form.signature_mode=='none' %}selected{% endif %}>Bez podpisu</option>
{% if current and current.payload.signature_base64 %}<option value="keep" {% if form.signature_mode=='keep' %}selected{% endif %}>Zachowaj podpis z zapisanej wersji</option>{% endif %}
{% if ts_options.has_signature %}<option value="current" {% if form.signature_mode=='current' %}selected{% endif %}>Dodaj mój podpis zapisany w ustawieniach</option>{% endif %}
</select></label>
{% if not ts_options.has_signature %}<p class="muted">Podpis wgrywasz jednorazowo w <a href="{{ url_for('timesheet_settings_page') }}">Ustawienia → Listy godzin</a>. Nie jest umieszczany w publicznym kodzie aplikacji.</p>{% endif %}
<label class="checkbox-row" id="ts-signature-consent"><input type="checkbox" name="signature_consent" value="1">Potwierdzam użycie mojego zapisanego podpisu na tej liście.</label>
<label class="checkbox-row ts-reviewed"><input type="checkbox" name="reviewed" value="1">Sprawdziłem rzeczywiste daty, godziny i przerwy. Chcę zapisać tę wersję listy.</label>
<p class="muted">Wpisy są elektronicznie stylizowane na pismo ręczne. Zapis nie zmienia godzin ani kwoty faktury. Zmiana ustawień nie modyfikuje starszych PDF-ów.</p>
<div class="form-actions"><button type="submit" name="intent" value="preview" formtarget="_blank" class="btn btn-light">Podgląd PDF</button><button type="submit" name="intent" value="save" class="btn btn-primary">Zapisz {{ 'nową wersję' if current else 'listę' }}</button></div>
</form>
{% if history %}<section class="panel"><h2>Historia zapisanych wersji</h2><p class="muted">Poprzednie dokumenty pozostają dostępne również po edycji listy.</p><div class="table-wrap"><table><thead><tr><th>Wersja</th><th>Zapisano</th><th>Godziny netto</th><th>PDF</th></tr></thead><tbody>{% for v in history %}<tr><td>{{ v.revision }}</td><td>{{ v.created_at }}</td><td>{{ ts_hours(v.total_minutes) }}</td><td><a href="{{ url_for('timesheet_revision_pdf',version_id=v.id) }}">Pobierz</a></td></tr>{% endfor %}</tbody></table></div></section>{% endif %}
{% endblock %}'''

EMBEDDED_TEMPLATES['timesheet_settings.html'] = r'''{% extends 'base.html' %}
{% block title %}Listy godzin — Ustawienia{% endblock %}
{% block content %}
{% include 'settings_tabs.html' %}
<div class="page-header"><div><h1>Listy godzin</h1><p class="muted">Szablon EQUANS · styl nr 5 — luźny · PDF A4 po niemiecku</p></div><a class="btn btn-light" target="_blank" href="{{ url_for('timesheet_sample_pdf') }}">Próbka stylu (bez podpisu)</a></div>
{% for error in errors %}<div class="callout warning" role="alert">{{ error }}</div>{% endfor %}
<div class="callout info">Listę tworzysz na stronie zapisanej faktury przyciskiem „Lista godzin”. Ustawienia poniżej są wartościami domyślnymi; nie zmieniają istniejących list.</div>
<form method="post" enctype="multipart/form-data" class="panel form-panel ts-settings-form">
<input type="hidden" name="_ts_csrf" value="{{ ts_csrf_token() }}"><input type="hidden" name="action" value="save">
<h2>Wpisy domyślne</h2><div class="form-grid two">
<label class="field"><span>Firma na formularzu</span><input name="company_label" value="{{ ts_options.company_label }}" maxlength="60" required></label>
<label class="field"><span>Imię i nazwisko</span><input name="worker_name" value="{{ ts_options.worker_name }}" maxlength="80" required></label>
<label class="field"><span>Początek pracy</span><input type="time" name="default_start" value="{{ ts_options.default_start }}" required></label>
<label class="field"><span>Koniec pon.–śr.</span><input type="time" name="default_end" value="{{ ts_options.default_end }}" required></label>
<label class="field"><span>Przerwa pon.–śr. (min)</span><input type="number" min="0" max="720" name="default_break" value="{{ ts_options.default_break }}" required></label>
<label class="field"><span>Koniec w czwartek</span><input type="time" name="thursday_end" value="{{ ts_options.thursday_end }}" required></label>
<label class="field"><span>Przerwa w czwartek (min)</span><input type="number" min="0" max="720" name="thursday_break" value="{{ ts_options.thursday_break }}" required></label>
<label class="field"><span>Wygląd</span><input value="Styl 5 · luźne pismo · niebieski długopis" readonly></label>
<label class="checkbox-row span-2"><input type="checkbox" name="show_project" value="1" {% if ts_options.show_project %}checked{% endif %}>Domyślnie drukuj numer projektu</label></div>
<h2>Mój podpis</h2><p class="muted">Wgraj przycięty obraz własnego podpisu, bez otaczającego dokumentu. PNG z przezroczystym tłem daje najlepszy rezultat. Podpis przechowywany jest w bazie, nie na GitHubie.</p>
{% if ts_options.has_signature %}<img class="ts-sign-preview" src="{{ url_for('timesheet_signature_png') }}" alt="Twój zapisany podpis"><label class="checkbox-row"><input type="checkbox" name="remove_signature" value="1">Usuń domyślny podpis (nie usuwa podpisów ze starych zapisanych PDF-ów)</label>{% endif %}
<label class="field"><span>Podpis PNG/JPG/WEBP, maks. 2 MB</span><input type="file" name="signature" accept="image/png,image/jpeg,image/webp"></label>
<label class="checkbox-row"><input type="checkbox" name="own_signature" value="1">Potwierdzam, że przesłany podpis jest moim własnym podpisem.</label>
<p class="muted">Każde dodanie podpisu do nowej listy wymaga osobnego zatwierdzenia. Kopiowanie poprzedniej listy nie kopiuje jej podpisu.</p>
<button class="btn btn-primary" type="submit">Zapisz ustawienia list</button>
</form>
<section class="panel"><h2>Jednorazowy import prywatnej konfiguracji</h2><p>Możesz zaimportować przygotowany plik ustawień z Twoim podpisem. Nie wgrywaj tego pliku na GitHub ani do publicznego bucketa.</p>
<form method="post" enctype="multipart/form-data" class="ts-settings-form"><input type="hidden" name="_ts_csrf" value="{{ ts_csrf_token() }}"><input type="hidden" name="action" value="import"><label class="field"><span>Plik konfiguracji .json</span><input type="file" name="config" accept=".json" required></label><label class="checkbox-row"><input type="checkbox" name="own_signature" value="1" required>Potwierdzam, że podpis w konfiguracji należy do mnie.</label><button class="btn btn-light" type="submit">Importuj ustawienia i podpis</button></form></section>
{% if archives %}<section class="panel"><h2>Zapisane listy</h2><div class="table-wrap"><table><thead><tr><th>Faktura</th><th>Wersja</th><th>Godziny</th><th>Dokument</th></tr></thead><tbody>{% for a in archives %}<tr><td>{% if a.invoice_id %}<a href="{{ url_for('timesheet_edit',invoice_id=a.invoice_id) }}">{{ a.invoice_number }}</a>{% else %}Odłączona lista — faktura usunięta{% endif %}</td><td>{{ a.revision }}</td><td>{{ ts_hours(a.total_minutes) }}</td><td><a href="{{ url_for('timesheet_revision_pdf',version_id=a.id) }}">PDF</a></td></tr>{% endfor %}</tbody></table></div></section>{% endif %}
{% endblock %}'''

# Append dedicated settings tab; do not move any existing tab.
_ts_link = ''' <a class="{% if settings_tab|default('') == 'timesheets' %}active{% endif %}" href="{{ url_for('timesheet_settings_page') }}">Listy godzin</a>'''
EMBEDDED_TEMPLATES['settings_tabs.html'] = EMBEDDED_TEMPLATES['settings_tabs.html'].replace('</nav>', _ts_link+'\n</nav>',1)
# The action is available on every invoice (default template EQUANS), without
# matching a contractor by mutable name or changing any contractor record.
_ts_invoice_action = '''
<section class="panel ts-saved"><div><h2>Lista godzin / Stundenzettel</h2><p class="muted">Szablon EQUANS, niebieskie wpisy w stylu 5 i Twój zatwierdzony podpis.</p></div><a class="btn btn-light" href="{{ url_for('timesheet_edit',invoice_id=invoice.id) }}">Lista godzin</a></section>
'''
EMBEDDED_TEMPLATES['invoice_detail.html'] = EMBEDDED_TEMPLATES['invoice_detail.html'].replace('{% block content %}','{% block content %}'+_ts_invoice_action,1)

EMBEDDED_CSS += r'''
/* V8.4 timesheets */
#ts-form .checkbox-row,.ts-settings-form .checkbox-row{display:flex;flex-direction:row;align-items:flex-start;gap:10px;margin:12px 0;line-height:1.4}
#ts-form .checkbox-row input[type=checkbox],.ts-settings-form .checkbox-row input[type=checkbox]{width:18px;min-width:18px;height:18px;padding:0;flex:0 0 18px;margin:1px 0 0}
#ts-signature-consent[hidden]{display:none!important}

.ts-saved,.ts-section-title{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.ts-section-title{margin:26px 0 12px}.ts-section-title h2{margin-bottom:4px}
.ts-row{display:grid;grid-template-columns:1.3fr 1fr 1fr .8fr 1fr auto;align-items:end;gap:10px;padding:14px 0;border-bottom:1px solid #e2e8f0}
.ts-row .field{min-width:0;margin:0}.ts-row input{width:100%;min-width:0;box-sizing:border-box}
.ts-row-total{align-self:center;font-weight:750;font-size:18px;white-space:nowrap}
.ts-row-extra{grid-column:1/-1;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.ts-total{margin:20px 0;padding:18px;border-radius:12px;background:#f4eff2;display:flex;justify-content:space-between;align-items:center;gap:12px}.ts-total strong{font-size:26px;color:#811737}
.ts-inline-warning{color:#a42727;min-height:1em}.ts-sign-preview{display:block;max-width:300px;max-height:150px;border:1px solid #eee;margin:12px 0;background:#fff}
.ts-reviewed{margin:20px 0;font-weight:650}.ts-row .ts-delete{align-self:center}
@media(max-width:700px){.ts-row{grid-template-columns:1fr 1fr;border:1px solid #d8e0e9;border-radius:12px;margin:12px 0;padding:14px}.ts-row .field:first-child{grid-column:1/-1}.ts-row-total{font-size:22px}.ts-row-extra{grid-column:1/-1}.ts-saved .button-row{width:100%}.ts-saved .button-row a{flex:1}.ts-section-title .button-row{display:flex;flex-direction:column;align-items:stretch}.ts-row input[type=date],.ts-row input[type=time]{font-size:16px!important}.ts-total strong{font-size:22px}}
'''

EMBEDDED_JS += r'''
document.addEventListener('DOMContentLoaded',()=>{
const form=document.getElementById('ts-form');if(!form)return;
const host=document.getElementById('ts-rows'), hidden=document.getElementById('ts-rows-json');
const defs=JSON.parse(form.dataset.defaults), err=document.getElementById('ts-row-error');
let rows=JSON.parse(hidden.value||'[]');
function isoAdd(s,n){const d=new Date(s+'T12:00:00Z');if(isNaN(d))return '';d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)}
function mins(t){if(!/^\d{2}:\d{2}$/.test(t||''))return NaN;let [h,m]=t.split(':').map(Number);return h<24&&m<60?h*60+m:NaN}
function net(r){let n=mins(r.end)-mins(r.start)+(r.next_day?1440:0)-Number(r.break_minutes);return Number.isFinite(n)&&n>0&&n<=1440&&Number(r.break_minutes)>=0?n:NaN}
function format(n){if(!Number.isFinite(n))return 'Sprawdź';return n%15===0?(n/60).toLocaleString('pl-PL')+' h':Math.floor(n/60)+' h '+String(n%60).padStart(2,'0')+' min'}
function sync(){hidden.value=JSON.stringify(rows);let sum=0,bad=false;rows.forEach((r,i)=>{let n=net(r);if(!Number.isFinite(n))bad=true;else sum+=n;const el=host.querySelector('[data-total="'+i+'"]');if(el)el.textContent=format(n)});document.getElementById('ts-total-text').textContent=format(sum);err.textContent=bad?'Sprawdź godziny i długość przerw w zaznaczonych dniach.':''}
function field(label,type,key,value,i){const l=document.createElement('label');l.className='field';const s=document.createElement('span');s.textContent=label;l.append(s);const input=document.createElement('input');input.type=type;input.value=value??'';input.required=true;if(type==='number'){input.min='0';input.max='1439';input.step='1'}input.addEventListener('input',()=>{rows[i][key]=type==='number'?input.value:input.value;sync()});l.append(input);return l}
function render(){host.replaceChildren();rows.forEach((r,i)=>{const row=document.createElement('div');row.className='ts-row';row.append(field('Data','date','date',r.date,i),field('Od','time','start',r.start,i),field('Do','time','end',r.end,i),field('Przerwa (min)','number','break_minutes',r.break_minutes,i));const total=document.createElement('div');total.className='ts-row-total';total.dataset.total=i;row.append(total);const del=document.createElement('button');del.type='button';del.className='btn btn-light btn-small ts-delete';del.textContent='Usuń';del.addEventListener('click',()=>{rows.splice(i,1);render()});row.append(del);const extra=document.createElement('div');extra.className='ts-row-extra';const l=document.createElement('label');l.className='checkbox-row';const ck=document.createElement('input');ck.type='checkbox';ck.checked=!!r.next_day;ck.addEventListener('change',()=>{r.next_day=ck.checked;sync()});l.append(ck,document.createTextNode('Koniec następnego dnia'));extra.append(l);const copy=document.createElement('button');copy.type='button';copy.className='btn btn-light btn-small';copy.textContent='Kopiuj na kolejny dzień';copy.addEventListener('click',()=>{if(rows.length>=31){err.textContent='Maksymalnie 31 dni.';return}const nd=isoAdd(r.date,1);if(rows.some(x=>x.date===nd)){err.textContent='Kolejny dzień jest już na liście.';return}rows.splice(i+1,0,{...r,date:nd});if(nd>document.getElementById('ts-to').value)document.getElementById('ts-to').value=nd;render()});extra.append(copy);row.append(extra);host.append(row)});sync()}
document.getElementById('ts-add').addEventListener('click',()=>{if(rows.length>=31){err.textContent='Maksymalnie 31 dni.';return}const date=rows.length?isoAdd(rows[rows.length-1].date,1):document.getElementById('ts-from').value;rows.push({date,start:defs.default_start,end:defs.default_end,break_minutes:defs.default_break,next_day:false});if(date>document.getElementById('ts-to').value)document.getElementById('ts-to').value=date;render()});
document.getElementById('ts-week').addEventListener('click',()=>{if(rows.length&&!confirm('Zastąpić wpisane dni domyślnym poniedziałkiem–czwartkiem?'))return;const s=document.getElementById('ts-from').value;let d=new Date(s+'T12:00:00Z');if(isNaN(d)){err.textContent='Wybierz datę początku.';return}const delta=(d.getUTCDay()+6)%7,monday=isoAdd(s,-delta);document.getElementById('ts-from').value=monday;document.getElementById('ts-to').value=isoAdd(monday,3);rows=Array.from({length:4},(_,i)=>({date:isoAdd(monday,i),start:defs.default_start,end:i===3?defs.thursday_end:defs.default_end,break_minutes:i===3?defs.thursday_break:defs.default_break,next_day:false}));render()});
form.addEventListener('submit',e=>{sync();if(e.submitter&&e.submitter.value==='save'&&!form.querySelector('[name=reviewed]').checked){e.preventDefault();err.textContent='Potwierdź sprawdzenie dat, godzin i przerw przed zapisem.'}});
function signMode(){document.getElementById('ts-signature-consent').hidden=document.getElementById('ts-signature-mode').value!=='current'}
document.getElementById('ts-signature-mode').addEventListener('change',signMode);signMode();render();
});
'''

# V8: wykonuj automatyczny backup dopiero po zdefiniowaniu wszystkich funkcji.
with app.app_context():
    ts_backup_before_migration()
    v83_backup_before_migration()
    v82_backup_before_migration()
    init_db()
    init_tax_module()
    init_prop_firms_module()
    init_v82_module()
    init_v83_module()
    init_timesheets_module()
    ensure_daily_backup()
    if REMOTE_DB_ENABLED and not upload_remote_database():
        raise RuntimeError("Nie udało się zapisać aktualizacji bazy w Supabase. Start zatrzymany; kopia lokalna zachowana.")


def run_v8_1_self_tests() -> list[str]:
    results: list[str] = []
    grouped = calculate_grouped_percentage_costs(Decimal("2200000"), Decimal("0"), Decimal("60"), Decimal("1200000"), Decimal("40"), Decimal("800000"))
    assert grouped["flat_expenses_60"] == Decimal("1200000.00")
    results.append("OK: wspólny limit 60% przy przychodzie 2,2 mln CZK = 1,2 mln CZK")

    invoices = [{"id": 1, "value_czk": Decimal("100000")}, {"id": 2, "value_czk": Decimal("50000")}]
    remaining = exclude_linked_invoice_rows(invoices, {1})
    assert [row["id"] for row in remaining] == [2]
    results.append("OK: faktura powiązana z payoutem jest wyłączona – brak podwójnego przychodu")

    assert date.fromisoformat("2027-01-05").year == 2027
    results.append("OK: styczniowy payout za grudniową fakturę trafia do roku faktycznego otrzymania")

    payout = payout_amount_breakdown(Decimal("1000"), Decimal("25"), Decimal("975"), Decimal("22"))
    assert payout["income_czk"] == Decimal("22000.00") and payout["fee_czk"] == Decimal("550.00") and payout["net_czk"] == Decimal("21450.00")
    results.append("OK: opłata operatora nie pomniejsza przychodu brutto")

    under, over = settlement_balance(Decimal("25023"), Decimal("5200"))
    assert under == Decimal("19823.00") and over == Decimal("0.00")
    results.append("OK: zapisane zaliczki są poprawnie odejmowane")

    lucid = payout_amount_breakdown(Decimal("900"), Decimal("18"), Decimal("882"), Decimal("22"))
    assert lucid["income_czk"] == Decimal("19800.00")
    assert lucid["fee_czk"] == Decimal("396.00")
    assert lucid["net_czk"] == Decimal("19404.00")
    results.append("OK: LucidFlex 1 000 USD wyniku przy 90/10 = 900 USD przychodu przed opłatą")

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.executescript("""
        CREATE TABLE prop_firms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, contractor_id INTEGER,
            default_tax_classification TEXT, qualification_status TEXT, dph_treatment TEXT,
            notes TEXT, active INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE prop_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, prop_firm_id INTEGER, tax_classification TEXT,
            qualification_status TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    old_id = mem.execute(
        "INSERT INTO prop_firms (name,default_tax_classification,qualification_status,dph_treatment,notes,active) VALUES (?,?,?,?,?,1)",
        ("Lucid Trading","s7_60","unconfirmed","review","stara notatka"),
    ).lastrowid
    mem.execute(
        "INSERT INTO prop_payouts (prop_firm_id,tax_classification,qualification_status) VALUES (?,?,?)",
        (old_id,"s7_60","unconfirmed"),
    )
    lucid_id = migrate_lucidflex_profile(mem)
    migrated = dict(mem.execute("SELECT * FROM prop_firms WHERE id=?", (lucid_id,)).fetchone())
    migrated_payout = dict(mem.execute("SELECT * FROM prop_payouts").fetchone())
    assert migrated["name"] == "LucidFlex"
    assert migrated["qualification_status"] == "recommended"
    assert migrated_payout["qualification_status"] == "recommended"
    mem.close()
    results.append("OK: migracja Lucid Trading → LucidFlex zachowuje payouty i ustawia rekomendowany status")
    return results

def run_v82_self_tests() -> list[str]:
    results = run_v8_1_self_tests()
    parameters = CONTRIBUTION_PARAMETER_DEFAULTS[2027]
    options = {'vzp_minimum_applies': 1, 'cssz_advance_exempt': 0, 'vzp_advance_exempt': 0}
    estimate = estimate_next_advances(Decimal('68648.81'), 6, 6, parameters, options)
    assert (estimate['cssz'], estimate['vzp']) == (Decimal('5281'), Decimal('3488'))
    results.append('OK: prognoza minimalnych zaliczek 2027: ČSSZ 5281 / VZP 3488')
    six = estimate_next_advances(Decimal('320000'), 6, 6, parameters, options)
    twelve = estimate_next_advances(Decimal('320000'), 12, 12, parameters, options)
    assert (six['cssz'], six['vzp']) == (Decimal('8566'), Decimal('3600'))
    assert six['cssz'] > twelve['cssz']
    results.append('OK: uwzględniono łączną liczbę miesięcy hlavní + vedlejší; brak dzielenia niepełnego roku przez 12')
    maximum = estimate_next_advances(Decimal('10000000'), 6, 6, parameters, options)
    assert maximum['cssz'] == ceil_whole(Decimal(parameters['cssz_max_base']) * Decimal('.292'))
    exempt = estimate_next_advances(Decimal('320000'), 6, 6, parameters,
                                    {**options, 'cssz_advance_exempt': 1, 'vzp_advance_exempt': 1})
    assert exempt['total'] == 0
    results.append('OK: maksimum ČSSZ, brak maksimum VZP i jawne zwolnienia z zaliczek')
    with app.test_request_context('/'):
        assert url_for('contractors_list') == '/settings/contractors'
        assert url_for('projects_list') == '/settings/projects'
    with app.test_client() as client:
        for url in ('/settings?year=2026', '/settings?year=2026&tab=forecast',
                    '/settings/contractors', '/settings/projects', '/taxes?year=2026'):
            response = client.get(url)
            assert response.status_code == 200, (url, response.status_code)
        response = client.post('/settings/contributions/forecast', data={
            'source_year': '2026', 'additional_revenue_60': '500000',
            'additional_revenue_40': '0', 'vzp_minimum_applies': '1',
        })
        assert response.status_code == 302
        response = client.get('/settings?year=2026&tab=forecast')
        assert response.status_code == 200
    results.append('OK: strony i zapis scenariusza przez test_client (wyłącznie tymczasowa baza testowa)')
    return results


def run_v83_self_tests() -> list[str]:
    """Runs only under --self-test, in the existing isolated test database."""
    from unittest.mock import patch
    results = run_v82_self_tests()
    text = ('02.10.2026 #190\nzemě|měna|množství|kód|kurz\n'
            'EMU|euro|1|EUR|24,500\nUSA|dolar|1|USD|21,000\n'
            'Japonsko|jen|100|JPY|14,200\nIndonesie|rupie|1000|IDR|1,330\n')
    parsed = parse_cnb_daily_text(text)
    assert Decimal(parsed['rates']['JPY']['rate']) == Decimal('.142')
    assert Decimal(parsed['rates']['IDR']['rate']) == Decimal('.00133')
    assert fx_publication_date(date(2026,10,4)) == date(2026,10,2)
    assert fx_publication_date(date(2026,9,28)) == date(2026,9,25)
    assert fx_publication_date(date(2026,4,6)) == date(2026,4,2)
    results.append('OK: format TXT ČNB, jednostki 100/1000, weekend, święto i Wielkanoc')
    for malformed in ('<html>ERROR</html>', text.replace('24,500','NaN')):
        try:
            parse_cnb_daily_text(malformed)
        except FxError:
            pass
        else:
            raise AssertionError('Przyjęto nieprawidłową tabelę')
    class FixtureResponse:
        def __init__(self, url, body): self.url, self.body = url, body
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def geturl(self): return self.url
        def read(self, *args): return self.body.encode('utf-8')
    def fixture_http(req, **kwargs):
        day = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)['date'][0]
        body = text.replace('02.10.2026', day)
        if day == '05.10.2026': body = body.replace('24,500', '25,000')
        return FixtureResponse(req.full_url, body)
    _FX_CACHE.clear()
    with patch(__name__+'.fx_today', return_value=date(2026,10,8)), patch('urllib.request.urlopen', side_effect=fixture_http) as transport:
        quote = fetch_cnb_rate('EUR', '2026-10-04')
        assert quote['published_date'] == '2026-10-02'
        count = transport.call_count
        fetch_cnb_rate('USD','2026-10-04')
        assert transport.call_count == count
        for currency, day in [('USDT','2026-10-02'), ('USDC','2026-10-02'), ('EUR','2026-10-09')]:
            try: fetch_cnb_rate(currency,day)
            except FxError: pass
            else: raise AssertionError('Przyjęto token / kurs z przyszłości')
        with app.app_context():
            db = get_db()
            contractor_id = db.execute("INSERT INTO contractors(name,vat_id) VALUES ('TEST FX','ATU12345678')").lastrowid
            db.commit()
        with app.test_client() as client:
            response = client.post('/invoices/new',data={
                'contractor_id':str(contractor_id), 'issue_date':'2026-10-02','supply_date':'2026-10-02',
                'due_date':'2026-10-16','currency':'EUR','language':'de','tax_mode':'reverse_charge',
                'payment_method':'bank_transfer','dph_category':'review','fx_mode':'cnb','fx_date':'2026-10-02',
                'czk_rate':'99999','item_description':'TEST FX','item_quantity':'1','item_unit':'h',
                'item_unit_price':'1000','item_vat_rate':'0'})
            assert response.status_code == 302
            with app.app_context():
                row = get_db().execute('SELECT * FROM invoices ORDER BY id DESC LIMIT 1').fetchone()
                invoice_id = row['id']
                assert Decimal(row['czk_rate']) == Decimal('24.5')
                assert fx_quote('invoice',invoice_id)['source'] == 'cnb'
            response = client.post(f'/invoices/{invoice_id}/payment',data={'paid_date':'2026-10-05','fx_mode':'cnb','czk_rate':'9999'})
            assert response.status_code == 302
            with app.app_context():
                row = get_db().execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
                assert Decimal(row['czk_rate']) == Decimal('24.5')
                assert Decimal(fx_quote('invoice',invoice_id,'payment')['rate']) == Decimal('25')
                rows, _ = tax_invoice_rows(2026,'paid')
                assert next(r for r in rows if r['id'] == invoice_id)['value_czk'] == Decimal('25000')
            for url in ('/settings/exchange-rates',f'/invoices/{invoice_id}/payment', f'/invoices/{invoice_id}/edit'):
                assert client.get(url).status_code == 200
        results.append('OK: serwer zapisuje kurs CNB zamiast podmienionej wartości; oddzielny kurs PIT bez zmiany kursu DPH')
    _FX_CACHE.clear()
    results.append('OK: cache, odrzucenie kursów przyszłych / tokenów; testy transportu na kontrolowanych danych, bez live API')
    return results


def run_v84_self_tests() -> list[str]:
    results = run_v83_self_tests()
    rows = [{'date':f'2026-10-{5+i:02d}','start':'06:30','end':'13:00' if i==3 else '17:00',
             'break_minutes':0 if i==3 else 30} for i in range(4)]
    assert ts_validate_rows(rows,'2026-10-05','2026-10-08')[1] == 2190
    assert ts_validate_rows([dict(r,break_minutes=30) for r in rows],'2026-10-05','2026-10-08')[1] == 2160
    for bad in ([rows[0],rows[0]],[dict(rows[0],break_minutes=631)],[dict(rows[0],end='05:30')]):
        try:
            ts_validate_rows(bad,'2026-10-05','2026-10-08')
        except ValueError:
            pass
        else:
            raise AssertionError('Zaakceptowano nieprawidłowe godziny')
    results.append('OK: 36,5h / 36h; odrzucenie powtórzonych dat i błędnych przerw')
    with app.app_context():
        db=get_db()
        cid=db.execute("INSERT INTO contractors(name) VALUES('TEST EQUANS')").lastrowid
        iid=db.execute('''INSERT INTO invoices(invoice_number,invoice_year,sequence_no,contractor_id,
             issue_date,supply_date,due_date,currency,czk_rate,tax_mode)
             VALUES(?,2026,9000,?,'2026-10-08','2026-10-08','2026-10-22','CZK','1','no_vat')''',
             ('TEST-TS-'+secrets.token_hex(4),cid)).lastrowid
        db.execute("INSERT INTO invoice_items(invoice_id,position,description,quantity,unit,unit_price) VALUES(?,1,'TEST','36.5','h','500')",(iid,))
        db.commit()
    with app.test_client() as client:
        assert client.get(f'/invoices/{iid}/timesheet').status_code==200
        with client.session_transaction() as s: token=s['_ts_csrf']
        data={'_ts_csrf':token,'intent':'save','revision':'0','company_label':'TEST EQUANS',
              'worker_name':'Jan Kowalski','period_start':'2026-10-05','period_end':'2026-10-08',
              'project_number':'TEST-PROJECT','rows_json':_ts_json.dumps(rows),'signature_mode':'none','reviewed':'1'}
        assert client.post(f'/invoices/{iid}/timesheet',data=data).status_code==302
        first=client.get(f'/invoices/{iid}/timesheet/pdf').data
        assert first.startswith(b'%PDF')
        assert client.get(f'/invoices/{iid}/bundle').status_code==200
        data['revision']='1';data['rows_json']=_ts_json.dumps([dict(r,break_minutes=30) for r in rows])
        assert client.post(f'/invoices/{iid}/timesheet',data=data).status_code==302
        with app.app_context():
            versions=get_db().execute('''SELECT v.* FROM timesheet_versions v JOIN timesheets s ON s.id=v.sheet_id
                    WHERE s.invoice_id=? ORDER BY revision''',(iid,)).fetchall()
            assert len(versions)==2 and versions[0]['pdf_blob']==first and versions[1]['total_minutes']==2160
        assert client.post(f'/invoices/{iid}/timesheet',data=dict(data,_ts_csrf='bad')).status_code==400
        for path in ('/settings/timesheets',f'/invoices/{iid}',f'/invoices/{iid}/timesheet'):
            assert client.get(path).status_code==200,path
    results.append('OK: Flask GET/POST, zapis, wersjonowanie PDF, ZIP, CSRF, strona ustawień')
    return results


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        for result in run_v84_self_tests():
            print(result)
        raise SystemExit(0)
    if MIGRATED_FROM:
        print(f"Zaimportowano dane ze starej aplikacji: {MIGRATED_FROM}")
        print(f"Nowa baza danych: {DB_PATH}")
    print("Faktury OSVČ V8.4 — Listy godzin, styl 5 + kursy ČNB Web/PWA")
    print(f"Baza danych: {DB_PATH}")
    print(f"Faktury PDF: {INVOICE_PDF_DIR}")
    print(f"Supabase configured: {REMOTE_DB_ENABLED}; bucket={SUPABASE_BUCKET}; key_kind={SUPABASE_KEY_KIND}")
    port = int(os.environ.get("PORT", "5000"))
    if not CLOUD_MODE:
        threading.Timer(1.2, open_browser).start()
    elif not APP_PASSWORD:
        print("UWAGA: APP_PASSWORD nie jest ustawione - aplikacja online nie ma ochrony hasłem!")
    if CLOUD_MODE and not REMOTE_DB_ENABLED:
        print("UWAGA: SUPABASE_URL/SUPABASE_SERVICE_KEY nie są ustawione. Dane na darmowym Renderze NIE będą trwałe!")
    app.run(host="0.0.0.0" if CLOUD_MODE else "127.0.0.1", port=port, debug=False, use_reloader=False)
